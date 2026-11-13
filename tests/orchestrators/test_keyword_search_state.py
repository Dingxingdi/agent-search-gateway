import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage, KeywordSearchHit
from agent_search_gateway.request_ids import bind_request_id
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeKeywordSearchProvider
from tests.support.logging import structured_test_logger


class _JudgeClient:
    name = "judge"

    def __init__(self) -> None:
        self.candidates: list[str] = []

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        candidate = messages[-1]["content"]
        self.candidates.append(candidate)
        if "explode-body" in candidate:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "judge exploded")
        if "reject-body" in candidate:
            return {"ok": False, "reason": "not usable"}
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return "unused"

    async def aclose(self) -> None:
        return None


class _OrderedProvider:
    def __init__(
        self,
        name: str,
        hits: list[KeywordSearchHit],
        *,
        wait_for: asyncio.Event | None = None,
        signal: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.hits = hits
        self.wait_for = wait_for
        self.signal = signal
        self.calls = 0

    async def search(self, query: str) -> list[KeywordSearchHit]:
        self.calls += 1
        if self.signal is not None:
            self.signal.set()
        if self.wait_for is not None:
            await self.wait_for.wait()
        return list(self.hits)


async def test_keyword_search_validates_body_then_commits_deterministic_first_write_state(
    tmp_path: Path,
) -> None:
    store = URLStore()
    unavailable_url = normalize_url("https://example.com/unavailable")
    store.admit(unavailable_url, "Stored unavailable", raw_content="old raw")
    store.mark_unavailable(unavailable_url)

    judge_client = _JudgeClient()
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": judge_client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )

    release_first = asyncio.Event()
    first_provider = _OrderedProvider(
        "first",
        [
            KeywordSearchHit(
                "https://EXAMPLE.com/a",
                title="Title A",
                snippet="First abstract",
                raw_content="raw-a",
                content="content-a",
            ),
            KeywordSearchHit("https://example.com/skip", title=" ", snippet=""),
            KeywordSearchHit(
                "https://example.com/raw",
                title="Raw title",
                raw_content="raw-only",
            ),
            KeywordSearchHit(
                "https://example.com/reject",
                snippet="Rejected body still admitted",
                raw_content="reject-body",
            ),
            KeywordSearchHit(
                "https://example.com/unavailable",
                snippet="New unavailable abstract",
                content="explode-body-unavailable-must-be-skipped",
            ),
            KeywordSearchHit(
                "https://example.com/a",
                snippet="Duplicate later abstract",
                raw_content="raw-later",
                content="content-later",
            ),
        ],
        wait_for=release_first,
    )
    second_provider = _OrderedProvider(
        "second",
        [
            KeywordSearchHit(
                "https://example.com/a",
                snippet="Second provider abstract",
                raw_content="raw-b",
                content="content-b",
            ),
            KeywordSearchHit("https://example.com/b", snippet="B abstract"),
        ],
        signal=release_first,
    )
    failed_provider = _OrderedProvider(
        "failed",
        [
            KeywordSearchHit("https://example.com/c", snippet="C would be staged"),
            KeywordSearchHit(
                "https://example.com/explode",
                snippet="Explodes judge",
                raw_content="explode-body",
            ),
            KeywordSearchHit(
                "https://example.com/d",
                snippet="D survives judge failure",
            ),
        ],
    )
    providers = [first_provider, second_provider, failed_provider]
    orchestrator = SearchOrchestrator(
        keyword_providers=providers,
        llm_invocations=(),
        quotas=ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages=stages,
        store=store,
        result_writer=ResultWriter(tmp_path / "results"),
    )

    first_path = Path(await orchestrator.keyword_search("query", request_id="11111111"))
    output = [json.loads(line) for line in first_path.read_text(encoding="utf-8").splitlines()]
    assert output == [
        {"url": "https://example.com/a", "abstract": "First abstract"},
        {"url": "https://example.com/raw", "abstract": "Raw title"},
        {"url": "https://example.com/reject", "abstract": "Rejected body still admitted"},
        {"url": "https://example.com/unavailable", "abstract": "Stored unavailable"},
        {"url": "https://example.com/b", "abstract": "B abstract"},
        {"url": "https://example.com/c", "abstract": "C would be staged"},
        {"url": "https://example.com/d", "abstract": "D survives judge failure"},
    ]

    a = store.get(normalize_url("https://example.com/a"))
    assert a is not None
    assert (a.raw_content, a.content, a.abstract) == ("raw-a", "content-a", "First abstract")
    raw = store.get(normalize_url("https://example.com/raw"))
    assert raw is not None and raw.raw_content == "raw-only" and raw.content == ""
    rejected = store.get(normalize_url("https://example.com/reject"))
    assert rejected is not None and rejected.raw_content == "" and rejected.available is True
    assert store.get(normalize_url("https://example.com/skip")) is None
    staged_before_failure = store.get(normalize_url("https://example.com/c"))
    assert staged_before_failure is not None
    assert staged_before_failure.abstract == "C would be staged"
    staged_after_failure = store.get(normalize_url("https://example.com/d"))
    assert staged_after_failure is not None
    assert staged_after_failure.abstract == "D survives judge failure"
    assert "explode-body-unavailable-must-be-skipped" not in "\n".join(judge_client.candidates)

    second_path = Path(await orchestrator.keyword_search("query", request_id="22222222"))
    assert second_path != first_path
    assert [provider.calls for provider in providers] == [2, 2, 2]


async def test_keyword_search_attributes_hit_staging_failures_without_duplicate_terminal_events(
    tmp_path: Path,
) -> None:
    logger, stream = structured_test_logger("tests.search.keyword-hit-staging-failures")
    judge_client = _JudgeClient()
    invocation = LLMInvocation("judge", "judge-model", {})
    stages = LLMStages(
        {"judge": judge_client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )
    partial = FakeKeywordSearchProvider(
        "partial",
        [
            KeywordSearchHit("https://example.com/kept", snippet="Kept result"),
            KeywordSearchHit(
                "https://example.com/partial-failure",
                snippet="Judge fails",
                raw_content="explode-body-partial",
            ),
        ],
    )
    all_judge_failed = FakeKeywordSearchProvider(
        "all-judge-failed",
        [
            KeywordSearchHit(
                "https://example.com/all-failed",
                snippet="Judge fails",
                raw_content="explode-body-all",
            )
        ],
    )
    invalid_hit = FakeKeywordSearchProvider(
        "invalid-hit",
        [KeywordSearchHit(cast(str, 1), snippet="Invalid hit")],
    )
    orchestrator = SearchOrchestrator(
        keyword_providers=[partial, all_judge_failed, invalid_hit],
        llm_invocations=(),
        quotas=ProviderQuotaManager(
            web_limits={"partial": 1, "all-judge-failed": 1, "invalid-hit": 1},
            llm_limits={},
        ),
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "judge-failure-results"),
        logger=logger,
    )

    await orchestrator.keyword_search("query", request_id="1234abcd")

    lines = stream.getvalue().splitlines()
    partial_lines = [line for line in lines if "provider=partial" in line]
    assert sum("event=provider_partial_failure" in line for line in partial_lines) == 1
    assert any(
        "event=provider_partial_failure" in line
        and "stage=judge" in line
        and "error_type=ExecutionFailure" in line
        and "failed_hits=1" in line
        for line in partial_lines
    )
    assert sum("event=provider_completed" in line for line in partial_lines) == 1
    assert not any("event=provider_failed" in line for line in partial_lines)

    failed_lines = [line for line in lines if "provider=all-judge-failed" in line]
    assert sum("event=provider_failed" in line for line in failed_lines) == 1
    assert any(
        "event=provider_failed" in line
        and "stage=judge" in line
        and "error_type=ExecutionFailure" in line
        for line in failed_lines
    )
    assert not any(
        "event=provider_failed" in line and "stage=search" in line for line in failed_lines
    )
    assert not any("event=provider_completed" in line for line in failed_lines)

    invalid_lines = [line for line in lines if "provider=invalid-hit" in line]
    assert sum("event=provider_failed" in line for line in invalid_lines) == 1
    assert any(
        "event=provider_failed" in line
        and "stage=hit_staging" in line
        and "error_type=TypeError" in line
        for line in invalid_lines
    )
    assert not any(
        "event=provider_failed" in line and "stage=search" in line for line in invalid_lines
    )
    assert not any("event=provider_completed" in line for line in invalid_lines)


async def test_keyword_search_debug_events_cover_provider_candidate_body_and_persistence(
    tmp_path: Path,
) -> None:
    logger, stream = structured_test_logger("tests.search.keyword-events")
    judge_client = _JudgeClient()
    invocation = LLMInvocation("judge", "judge-model", {})
    stages = LLMStages(
        {"judge": judge_client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )
    failed = FakeKeywordSearchProvider(
        "failed",
        failure=ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "PROVIDER_FAILURE_DETAIL_SENTINEL",
        ),
    )
    success = FakeKeywordSearchProvider(
        "success",
        [
            KeywordSearchHit("https://EXAMPLE.COM/empty?id=1", title=" ", snippet=""),
            KeywordSearchHit(
                "https://log-user:LOG_PASSWORD_SENTINEL@EXAMPLE.COM/no-body?id=42&mode=test#frag",
                snippet="No body",
            ),
            KeywordSearchHit(
                "https://example.com/space?id=2",
                snippet="Whitespace body",
                raw_content="   ",
            ),
            KeywordSearchHit(
                "https://example.com/reject?id=3",
                snippet="Rejected body",
                raw_content="reject-body PAGE_REJECT_SENTINEL",
            ),
            KeywordSearchHit(
                "https://example.com/accepted?id=4",
                snippet="Accepted body",
                raw_content="PAGE_ACCEPT_SENTINEL",
            ),
            KeywordSearchHit(
                "https://log-user:LOG_PASSWORD_SENTINEL@EXAMPLE.COM/no-body?id=42&mode=test#frag",
                snippet="Duplicate later",
            ),
        ],
    )
    orchestrator = SearchOrchestrator(
        keyword_providers=[failed, success],
        llm_invocations=(),
        quotas=ProviderQuotaManager(
            web_limits={"failed": 1, "success": 1},
            llm_limits={},
        ),
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "debug-results"),
        logger=logger,
    )

    with bind_request_id("abcddcba"):
        result_path = Path(
            await orchestrator.keyword_search(
                "QUERY_BODY_SENTINEL",
                request_id="abcddcba",
            )
        )

    records = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()]
    assert all(set(record) == {"url", "abstract"} for record in records)
    assert result_path.name == "keyword-abcddcba.jsonl"

    logged = stream.getvalue()
    lines = logged.splitlines()
    assert all("request=abcddcba" in line for line in lines)
    assert any(
        "event=provider_failed" in line
        and "provider=failed" in line
        and "error_type=ExecutionFailure" in line
        for line in lines
    )
    assert any(
        "event=provider_completed" in line and "provider=success" in line and "hits=6" in line
        for line in lines
    )
    assert any(
        "event=candidate_rejected" in line
        and "url=https://example.com/empty " in line
        and "reason=empty_abstract" in line
        for line in lines
    )
    assert "url=https://example.com/no-body " in logged
    assert "LOG_PASSWORD_SENTINEL" not in logged
    assert any("event=body_skipped" in line and "reason=no_body" in line for line in lines)
    assert any("event=body_rejected" in line and "reason=cheap_check" in line for line in lines)
    assert any("event=body_rejected" in line and "reason=judge_rejected" in line for line in lines)
    assert "decision_reason=" not in logged
    assert "not usable" not in logged
    assert any(
        "event=body_accepted" in line and "url=https://example.com/accepted " in line
        for line in lines
    )
    assert any(
        "event=candidate_rejected" in line
        and "url=https://example.com/no-body " in line
        and "reason=duplicate" in line
        for line in lines
    )
    assert any(
        "event=results_written" in line and "kind=keyword" in line and str(result_path) in line
        for line in lines
    )
    for sentinel in (
        "QUERY_BODY_SENTINEL",
        "PAGE_REJECT_SENTINEL",
        "PAGE_ACCEPT_SENTINEL",
        "PROVIDER_FAILURE_DETAIL_SENTINEL",
    ):
        assert sentinel not in logged
