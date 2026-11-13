import asyncio
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import (
    AgentInvocation,
    KeywordSearchRequest,
    LLMInvocation,
    RetryPolicy,
    SuccessResponse,
)
from agent_search_gateway.observability import SecretValue, configure_debug_logging
from agent_search_gateway.orchestrators.paper import PaperSearchOrchestrator
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request
from agent_search_gateway.providers.contracts import ChatMessage, KeywordSearchHit, PaperSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore
from tests.support.acceptance import AcceptanceRuntime
from tests.support.agent_search import web_markdown
from tests.support.fakes import (
    FakeAcademicSearchProvider,
    FakeKeywordSearchProvider,
    FakeLLMClient,
    FakeOAResolver,
)


async def test_paper_workflow_logs_lifecycle_rejection_and_enrichment_without_payloads(
    tmp_path: Path,
) -> None:
    query = "QUERY_PAYLOAD_SENTINEL"
    title = "TITLE_PAYLOAD_SENTINEL"
    abstract = "ABSTRACT_PAYLOAD_SENTINEL"
    doi = "10.1000/doi-payload-sentinel"
    valid = PaperSearchHit(
        source="openalex",
        source_id="W123",
        title=title,
        authors=("A. Author",),
        abstract=abstract,
        doi=doi,
        url="https://example.test/paper",
    )
    invalid = PaperSearchHit(
        source="openalex",
        source_id="",
        title="INVALID_TITLE_SENTINEL",
        url="https://example.test/invalid",
    )
    good = FakeAcademicSearchProvider("openalex", [valid, invalid])
    failed = FakeAcademicSearchProvider(
        "core",
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider detail sentinel"),
    )
    resolver = FakeOAResolver(
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "resolver detail sentinel")
    )
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    try:
        orchestrator = PaperSearchOrchestrator(
            providers=(good, failed),
            quotas=ProviderQuotaManager(
                web_limits={},
                llm_limits={},
                academic_limits={"openalex": 1, "core": 1},
            ),
            aggregator=PaperAggregator(("openalex", "core")),
            resolver=resolver,
            store=URLStore(),
            result_writer=ResultWriter(tmp_path / "results"),
        )
        await orchestrator.paper_search(query, request_id="11111111")
    finally:
        session.close()

    text = log_file.read_text(encoding="utf-8")
    assert "event=provider_started" in text
    assert "event=provider_completed" in text
    assert "event=provider_failed" in text
    assert "stage=paper_search" in text
    assert "event=paper_candidate_rejected" in text
    assert "reason=missing_source_id" in text
    assert "event=paper_enrichment_failed" in text
    assert "stage=oa_resolve" in text
    for sentinel in (
        query,
        title,
        abstract,
        doi,
        "INVALID_TITLE_SENTINEL",
        "provider detail sentinel",
        "resolver detail sentinel",
    ):
        assert sentinel not in text


def test_transitive_cluster_merge_log_contains_reason_but_no_identifiers(tmp_path: Path) -> None:
    doi = "10.1000/bridge-log-sentinel"
    arxiv_id = "2401.98765"
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    try:
        PaperAggregator(("crossref", "arxiv", "semantic_scholar")).aggregate(
            [
                PaperSearchHit(
                    source="crossref",
                    source_id=doi,
                    title="DOI paper",
                    doi=doi,
                    url="https://doi.org/10.1000/bridge-log-sentinel",
                ),
                PaperSearchHit(
                    source="arxiv",
                    source_id=arxiv_id,
                    title="arXiv paper",
                    arxiv_id=arxiv_id,
                    url="https://arxiv.org/abs/2401.98765",
                ),
                PaperSearchHit(
                    source="semantic_scholar",
                    source_id="ABCDEF9876",
                    title="Bridge paper",
                    doi=doi,
                    arxiv_id=arxiv_id,
                    url="https://example.test/bridge",
                ),
            ]
        )
    finally:
        session.close()

    text = log_file.read_text(encoding="utf-8")
    assert "event=paper_clusters_merged" in text
    assert "reason=bridged_identity" in text
    assert "clusters=2" in text
    assert doi not in text
    assert arxiv_id not in text


async def test_provider_terminal_event_preserves_upstream_code_after_retries(
    tmp_path: Path,
) -> None:
    responses = {
        "a": [
            httpx.Response(429, json={"error": {"code": "rate_limit"}}),
            httpx.Response(502, json={"error": {"code": "temporarily_unavailable"}}),
            httpx.Response(
                200,
                json={"results": [{"url": "https://example.test/a", "content": "A abstract"}]},
            ),
        ],
        "b": [httpx.Response(429, json={"error": {"code": "quota_exhausted"}}) for _ in range(3)],
        "c": [
            httpx.Response(
                200,
                json={
                    "results": [
                        *[
                            {"url": f"https://example.test/c/{i}", "content": "C abstract"}
                            for i in range(5)
                        ],
                        *[{"title": "MISSING_URL_SENTINEL"} for _ in range(3)],
                        "INVALID_RECORD_SENTINEL",
                        42,
                    ]
                },
            )
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return responses[request.url.host.split(".")[0]].pop(0)

    async def sleep(delay: float) -> None:
        await asyncio.sleep(0)

    paths = RuntimePaths.from_home(tmp_path)
    stderr = io.StringIO()
    session = configure_debug_logging(paths.debug_log_file, stderr=stderr)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = [
            TavilyAdapter(
                name=name,
                api_url=f"https://{name}.example.test",
                secret=SecretValue("CREDENTIAL_SENTINEL"),
                http_executor=HttpJsonExecutor(
                    client, RetryPolicy(3, 0.01, 2, 1), provider_name=name, sleep=sleep
                ),
            )
            for name in ("a", "b", "c")
        ]
        invocation = LLMInvocation("judge", "model", {})
        runtime = AcceptanceRuntime(paths)
        runtime.search_orchestrator = SearchOrchestrator(
            keyword_providers=providers,
            llm_invocations=(),
            quotas=ProviderQuotaManager(web_limits={"a": 1, "b": 1, "c": 1}, llm_limits={}),
            stages=LLMStages(
                {"judge": FakeLLMClient("judge")},
                judge=invocation,
                safety=invocation,
                content_clean=invocation,
                focus_summary=invocation,
            ),
            store=URLStore(),
            result_writer=ResultWriter(paths.results_dir),
        )
        daemon = ForegroundDaemon(
            paths,
            runtime_factory=lambda: runtime,
            request_id_factory=lambda: "abc12345",
            debug=True,
            logging_session=session,
        )
        task = asyncio.create_task(daemon.start())
        try:
            await asyncio.wait_for(daemon.ready.wait(), 1)
            response = await send_request(paths.socket_file, KeywordSearchRequest("QUERY_SENTINEL"))
        finally:
            await daemon.stop_for_test()
            await task
            session.close()

    assert isinstance(response, SuccessResponse)
    records = [json.loads(line) for line in Path(response.text).read_text().splitlines()]
    assert [record["url"] for record in records] == [
        "https://example.test/a",
        *[f"https://example.test/c/{i}" for i in range(5)],
    ]
    for logged in (paths.debug_log_file.read_text(), stderr.getvalue()):
        lines = logged.splitlines()

        def events(event: str, provider: str, lines: list[str] = lines) -> list[str]:
            return [
                line
                for line in lines
                if f"event={event} " in line and f"provider={provider} " in line
            ]

        assert len(events("http_retrying", "a")) == 2
        assert len(events("provider_response_diagnostic", "a")) == 3
        assert len(events("provider_completed", "a")) == 1
        assert not events("provider_failed", "a")
        assert len(events("http_failed", "b")) == 1
        assert "upstream_code=quota_exhausted" in events("http_failed", "b")[0]
        terminal = events("provider_failed", "b")
        assert len(terminal) == 1
        summaries = events("provider_candidates_summary", "c")
        assert len(summaries) == 1
        assert all(
            field in summaries[0]
            for field in (
                "phase=adapter",
                "received=10",
                "accepted=5",
                "rejected=5",
                'rejection_counts={"invalid_record":2,"missing_url":3}',
            )
        )
        assert len(events("provider_completed", "c")) == 1
        assert sum("event=workflow_completed " in line for line in lines) == 1
        assert "event=workflow_failed " not in logged
        assert "SENTINEL" not in logged
        assert "upstream_code=quota_exhausted" in terminal[0]


@pytest.mark.parametrize("reason", ["invalid_data_envelope", "REASON_SENTINEL"])
async def test_paper_terminal_preserves_safe_diagnostics_and_restricts_reason(
    tmp_path: Path,
    reason: str,
) -> None:
    provider = FakeAcademicSearchProvider(
        "academic",
        failure=ExecutionFailure(
            ErrorCode.PROTOCOL_ERROR,
            "MESSAGE_SENTINEL",
            reason=reason,
            diagnostics={
                "upstream_code": "schema_error",
                "upstream_type": "invalid_request",
                "http_status": 200,
                "category": "OWNER_SENTINEL",
                "model": "OWNER_SENTINEL",
            },
        ),
    )
    stderr = io.StringIO()
    session = configure_debug_logging(tmp_path / "paper.log", stderr=stderr)
    try:
        orchestrator = PaperSearchOrchestrator(
            providers=[provider],
            quotas=ProviderQuotaManager(
                web_limits={},
                llm_limits={},
                academic_limits={"academic": 1},
            ),
            aggregator=PaperAggregator(("academic",)),
            resolver=None,
            store=URLStore(),
            result_writer=ResultWriter(tmp_path / "results"),
        )
        with pytest.raises(ExecutionFailure):
            await orchestrator.paper_search("QUERY_SENTINEL", request_id="12345678")
    finally:
        session.close()
    for logged in (stderr.getvalue(), (tmp_path / "paper.log").read_text()):
        assert logged.count("event=provider_started") == 1
        terminal = [line for line in logged.splitlines() if "event=provider_failed " in line]
        assert len(terminal) == 1
        assert "event=provider_completed " not in logged
        assert "upstream_code=schema_error" in terminal[0]
        assert "upstream_type=invalid_request" in terminal[0]
        assert "http_status=200" in terminal[0]
        expected_reason = "execution_failure" if reason == "REASON_SENTINEL" else reason
        assert f"reason={expected_reason}" in terminal[0]
        assert "SENTINEL" not in logged


@pytest.mark.parametrize("kind", ["llm", "agent"])
async def test_repeated_text_invocations_own_each_scope_terminal(
    tmp_path: Path,
    kind: str,
) -> None:
    class ScopedClient(FakeLLMClient):
        async def complete_text(
            self,
            invocation: LLMInvocation,
            messages: Sequence[ChatMessage],
        ) -> str:
            if "## Paper" in messages[0]["content"]:
                raise ExecutionFailure(
                    ErrorCode.ALL_PROVIDERS_FAILED,
                    "MESSAGE_SENTINEL",
                    reason="REASON_SENTINEL",
                    diagnostics={
                        "upstream_code": "quota_exhausted",
                        "upstream_type": "capacity",
                        "upstream_status": "ERROR",
                        "stage": "OWNER_SENTINEL",
                        "model": "OWNER_SENTINEL",
                    },
                )
            return web_markdown()

    client = ScopedClient("same")
    invocation = LLMInvocation("same", "model")

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        return await client.complete_text(invocation, messages)

    stages = LLMStages(
        {"same": client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        agent_runners={"same": runner},
    )
    search = SearchOrchestrator(
        keyword_providers=(),
        llm_invocations=(invocation, invocation),
        agent_invocations=(AgentInvocation("same", "model"), AgentInvocation("same", "model")),
        quotas=ProviderQuotaManager(web_limits={}, llm_limits={"same": 2}),
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
    )
    stderr = io.StringIO()
    session = configure_debug_logging(tmp_path / "text.log", stderr=stderr)
    try:
        for request_id in ("12345678", "87654321"):
            operation = search.llm_search if kind == "llm" else search.agent_search
            result = Path(await operation("PROMPT_SENTINEL", request_id=request_id, scope="all"))
            records = [json.loads(line) for line in result.read_text().splitlines()]
            assert len(records) == 1 and records[0]["url"] == "https://example.test/web"
    finally:
        session.close()
    for logged in (stderr.getvalue(), (tmp_path / "text.log").read_text()):
        lines = logged.splitlines()
        assert sum("event=provider_started" in line for line in lines) == 8
        completed = [line for line in lines if "event=provider_completed " in line]
        failed = [line for line in lines if "event=provider_failed " in line]
        assert len(completed) == len(failed) == 4
        assert all(f"stage={kind}_search " in line for line in completed)
        assert all(f"stage={kind}_paper_search " in line for line in failed)
        for terminal in failed:
            assert "model=model" in terminal
            assert "upstream_code=quota_exhausted" in terminal
            assert "upstream_type=capacity" in terminal
            assert "upstream_status=ERROR" in terminal
            assert "reason=execution_failure" in terminal
        assert "SENTINEL" not in logged


@pytest.mark.parametrize(
    "phase",
    ["keyword", "keyword_judge", "paper", "llm_web", "llm_paper", "agent_web", "agent_paper"],
)
async def test_search_cancellation_closes_started_owner_once(tmp_path: Path, phase: str) -> None:
    entered = asyncio.Event()

    async def block() -> None:
        entered.set()
        await asyncio.Event().wait()

    class Keyword(FakeKeywordSearchProvider):
        async def search(self, query: str) -> list[KeywordSearchHit]:
            if phase == "keyword":
                await block()
            return [
                KeywordSearchHit("https://example.test", snippet="abstract", raw_content="body")
            ]

    class Academic(FakeAcademicSearchProvider):
        async def search(self, query: str) -> list[PaperSearchHit]:
            await block()
            return []

    class Client(FakeLLMClient):
        async def complete_text(
            self,
            invocation: LLMInvocation,
            messages: Sequence[ChatMessage],
        ) -> str:
            await block()
            return ""

        async def complete_json(
            self,
            invocation: LLMInvocation,
            messages: Sequence[ChatMessage],
        ) -> Mapping[str, object]:
            await block()
            return {"ok": True}

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        await block()
        return ""

    invocation = LLMInvocation("owner", "model")
    quotas = ProviderQuotaManager(
        web_limits={"owner": 1},
        llm_limits={},
        academic_limits={"owner": 1},
    )
    stages = LLMStages(
        {"owner": Client("owner")},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        agent_runners={"owner": runner},
    )
    search = SearchOrchestrator(
        keyword_providers=[Keyword("owner")],
        llm_invocations=[invocation],
        agent_invocations=[AgentInvocation("owner", "model")],
        quotas=quotas,
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path),
    )
    paper = PaperSearchOrchestrator(
        providers=[Academic("owner")],
        quotas=quotas,
        aggregator=PaperAggregator(("owner",)),
        resolver=None,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path),
    )
    stderr = io.StringIO()
    session = configure_debug_logging(tmp_path / "cancel.log", stderr=stderr)
    try:
        if phase.startswith("keyword"):
            operation = search.keyword_search("prompt", request_id="12345678")
        elif phase == "paper":
            operation = paper.paper_search("prompt", request_id="12345678")
        elif phase.startswith("llm"):
            operation = search.llm_search(
                "prompt",
                request_id="12345678",
                scope="paper" if phase.endswith("paper") else "web",
            )
        else:
            operation = search.agent_search(
                "prompt",
                request_id="12345678",
                scope="paper" if phase.endswith("paper") else "web",
            )
        task = asyncio.create_task(operation)
        async with asyncio.timeout(1):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        session.close()
    assert quotas.get_web("owner").in_use == 0
    assert quotas.get_academic("owner").in_use == 0
    for logged in (stderr.getvalue(), (tmp_path / "cancel.log").read_text()):
        assert logged.count("event=provider_started") == 1
        terminal = [line for line in logged.splitlines() if "event=provider_failed " in line]
        assert len(terminal) == 1
        assert "reason=cancelled" in terminal[0]
        assert "event=provider_completed " not in logged
        assert "traceback=" not in logged
        assert "results_written" not in logged
