import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate, ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from agent_search_gateway.request_ids import bind_request_id
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.logging import structured_test_logger


class _SearchClient:
    def __init__(
        self,
        name: str,
        text: str = "",
        *,
        failure: ExecutionFailure | None = None,
        wait_for: asyncio.Event | None = None,
        signal: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.text = text
        self.failure = failure
        self.wait_for = wait_for
        self.signal = signal
        self.text_calls: list[LLMInvocation] = []
        self.json_calls = 0

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        self.text_calls.append(invocation)
        if self.signal is not None:
            self.signal.set()
        if self.wait_for is not None:
            await self.wait_for.wait()
        if self.failure is not None:
            raise self.failure
        return self.text

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        self.json_calls += 1
        return {"ok": True}

    async def aclose(self) -> None:
        return None


def _orchestrator(
    tmp_path: Path,
    invocations: tuple[LLMInvocation, ...],
    clients: dict[str, _SearchClient],
    store: URLStore | None = None,
) -> SearchOrchestrator:
    fallback = invocations[0] if invocations else LLMInvocation("unused", "model", {})
    stages = LLMStages(
        clients,
        judge=fallback,
        safety=fallback,
        content_clean=fallback,
        focus_summary=fallback,
    )
    return SearchOrchestrator(
        keyword_providers=(),
        llm_invocations=invocations,
        quotas=ProviderQuotaManager(web_limits={}, llm_limits={}),
        stages=stages,
        store=store or URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
    )


async def test_llm_search_runs_independent_entries_and_isolates_pipeline_failures(
    tmp_path: Path,
) -> None:
    with pytest.raises(InputFailure) as empty:
        await _orchestrator(tmp_path, (), {}).llm_search("  ", request_id="00000011")
    assert empty.value.code is ErrorCode.EMPTY_QUERY

    with pytest.raises(ExecutionFailure) as absent:
        await _orchestrator(tmp_path, (), {}).llm_search("find", request_id="00000012")
    assert absent.value.code is ErrorCode.NO_LLM_SEARCH_PROVIDERS

    release_first = asyncio.Event()
    first_invocation = LLMInvocation("first", "model-1", {"entry": 1})
    parser_invocation = LLMInvocation("parser", "model-2", {"entry": 2})
    failed_invocation = LLMInvocation("failed", "model-3", {"entry": 3})
    later_invocation = LLMInvocation("later", "model-4", {"entry": 4})
    empty_invocation = LLMInvocation("empty", "model-5", {"entry": 5})
    invocations = (
        first_invocation,
        parser_invocation,
        failed_invocation,
        later_invocation,
        empty_invocation,
    )
    clients = {
        "first": _SearchClient(
            "first",
            "## Result\nURL: https://EXAMPLE.com/a\nAbstract: First\n",
            wait_for=release_first,
        ),
        "parser": _SearchClient("parser", "not restricted markdown", signal=release_first),
        "failed": _SearchClient(
            "failed",
            failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "LLM failed"),
        ),
        "later": _SearchClient(
            "later",
            "## Result\nURL: https://example.com/a\nAbstract: Later duplicate\n"
            "## Result\nURL: https://example.com/b\nAbstract: B result\n",
        ),
        "empty": _SearchClient(
            "empty",
            "## Result\nURL: https://example.com/empty\nAbstract:\n",
        ),
    }
    store = URLStore()
    orchestrator = _orchestrator(tmp_path, invocations, clients, store)

    first_path = Path(await orchestrator.llm_search(" find docs ", request_id="11111111"))
    output = [json.loads(line) for line in first_path.read_text(encoding="utf-8").splitlines()]
    assert output == [
        {"url": "https://example.com/a", "abstract": "First"},
        {"url": "https://example.com/b", "abstract": "B result"},
    ]
    assert [client.text_calls[0] for client in clients.values()] == list(invocations)
    assert all(client.json_calls == 0 for client in clients.values())
    stored_a = store.get(normalize_url("https://example.com/a"))
    assert stored_a is not None and stored_a.abstract == "First" and stored_a.raw_content == ""

    second_path = Path(await orchestrator.llm_search("find docs", request_id="22222222"))
    assert second_path != first_path

    all_failed_invocations = (
        LLMInvocation("bad-one", "m1", {}),
        LLMInvocation("bad-two", "m2", {}),
    )
    all_failed_clients = {
        "bad-one": _SearchClient("bad-one", "malformed"),
        "bad-two": _SearchClient(
            "bad-two",
            failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "failed"),
        ),
    }
    before = set((tmp_path / "results").glob("llm-*.jsonl"))
    with pytest.raises(ExecutionFailure) as all_failed:
        await _orchestrator(tmp_path, all_failed_invocations, all_failed_clients).llm_search(
            "x", request_id="00000013"
        )
    assert all_failed.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert set((tmp_path / "results").glob("llm-*.jsonl")) == before


async def test_llm_search_debug_events_cover_invocations_parse_failures_and_persistence(
    tmp_path: Path,
) -> None:
    logger, stream = structured_test_logger("tests.search.llm-events")
    good = LLMInvocation("good", "safe-good-model", {"temperature": 0.1})
    malformed = LLMInvocation("malformed", "safe-bad-model", {"top_p": 1})
    failed = LLMInvocation("failed", "safe-failed-model", {})
    clients = {
        "good": _SearchClient(
            "good",
            "## Result\nURL: https://example.com/good?id=42\nAbstract: Good\n",
        ),
        "malformed": _SearchClient("malformed", "MODEL_MALFORMED_SENTINEL"),
        "failed": _SearchClient(
            "failed",
            failure=ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "MODEL_FAILURE_SENTINEL",
            ),
        ),
    }
    fallback = good
    stages = LLMStages(
        clients,
        judge=fallback,
        safety=fallback,
        content_clean=fallback,
        focus_summary=fallback,
        logger=logger,
    )
    orchestrator = SearchOrchestrator(
        keyword_providers=(),
        llm_invocations=(good, malformed, failed),
        quotas=ProviderQuotaManager(web_limits={}, llm_limits={}),
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "llm-debug-results"),
        logger=logger,
    )

    with bind_request_id("deadc0de"):
        path = Path(
            await orchestrator.llm_search(
                "USER_PROMPT_SENTINEL",
                request_id="deadc0de",
            )
        )

    assert path.name == "llm-deadc0de.jsonl"
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [
        {"url": "https://example.com/good?id=42", "abstract": "Good"}
    ]
    logged = stream.getvalue()
    lines = logged.splitlines()
    assert all("request=deadc0de" in line for line in lines)
    assert any(
        "event=provider_completed" in line
        and "provider=good" in line
        and "model=safe-good-model" in line
        and "stage=llm_search" in line
        and "output_chars=" in line
        and "results=1" in line
        for line in lines
    )
    assert any(
        "event=provider_failed" in line
        and "provider=malformed" in line
        and "error_type=ParserFailure" in line
        for line in lines
    )
    assert any(
        "event=provider_failed" in line
        and "provider=failed" in line
        and "error_type=ExecutionFailure" in line
        for line in lines
    )
    assert any(
        "event=results_written" in line
        and "kind=llm" in line
        and "results=1" in line
        and str(path) in line
        for line in lines
    )
    assert "https://example.com/good?id=42" not in logged
    for sentinel in (
        "USER_PROMPT_SENTINEL",
        "MODEL_MALFORMED_SENTINEL",
        "MODEL_FAILURE_SENTINEL",
    ):
        assert sentinel not in logged


@pytest.mark.parametrize("exhausted", [False, True])
async def test_protocol_retry_has_one_provider_terminal_outcome(
    tmp_path: Path, exhausted: bool
) -> None:
    logger, stream = structured_test_logger("tests.search.protocol-retry")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        content = "## Result\nURL: https://example.com/result\nAbstract: ANSWER_SENTINEL\n"
        payload = (
            {"choices": []}
            if attempts == 1 or exhausted
            else {"choices": [{"message": {"content": content}}]}
        )
        return httpx.Response(200, json=payload, request=request)

    async def no_sleep(_delay: float) -> None:
        return None

    invocation = LLMInvocation("openai", "model", {})
    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAIChatCompletionsClient(
            name="openai",
            api_url="https://llm.example.test",
            secret=SecretValue("secret"),
            executor=HttpJsonExecutor(http_client, policy, provider_name="openai", logger=logger),
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=no_sleep,
            logger=logger,
        )
        stages = LLMStages(
            {"openai": client},
            judge=invocation,
            safety=invocation,
            content_clean=invocation,
            focus_summary=invocation,
            logger=logger,
        )
        orchestrator = SearchOrchestrator(
            keyword_providers=(),
            llm_invocations=(invocation,),
            quotas=ProviderQuotaManager(web_limits={}, llm_limits={}),
            stages=stages,
            store=URLStore(),
            result_writer=ResultWriter(tmp_path / "results"),
            logger=logger,
        )
        if exhausted:
            with pytest.raises(ExecutionFailure) as caught:
                await orchestrator.llm_search("PROMPT_SENTINEL", request_id="aa000001")
            assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
        else:
            path = Path(await orchestrator.llm_search("PROMPT_SENTINEL", request_id="aa000001"))
            assert path.is_file()
    assert attempts == 2
    logged = stream.getvalue()
    lines = logged.splitlines()
    retrying = [line for line in lines if "event=provider_retrying" in line]
    assert len(retrying) == 1
    assert "attempt=1" in retrying[0] and "next_attempt=2" in retrying[0]
    assert "retry_scope=protocol" in retrying[0] and "delay_ms=10" in retrying[0]
    completed = [line for line in lines if "event=provider_completed" in line]
    failed = [line for line in lines if "event=provider_failed" in line]
    stage_failed = [line for line in lines if "event=llm_stage_failed" in line]
    if exhausted:
        assert not completed
        assert len(failed) == 1 and "stage=llm_search" in failed[0]
        assert len(stage_failed) == 1 and "category=protocol" in stage_failed[0]
        assert "event=llm_protocol_completed" not in logged
    else:
        assert len(completed) == 1 and "stage=llm_search" in completed[0]
        assert not failed and not stage_failed
    assert "PROMPT_SENTINEL" not in logged and "ANSWER_SENTINEL" not in logged
