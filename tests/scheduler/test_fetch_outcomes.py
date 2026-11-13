import asyncio
import logging
from collections.abc import Mapping, Sequence

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.request_ids import bind_request_id
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url
from tests.support.fakes import FakeLLMClient, FakeURLFetchProvider
from tests.support.logging import structured_test_logger


class _JudgeClient:
    name = "judge"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        content = messages[-1]["content"]
        if "judge-execution" in content:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "judge failed")
        if "judge-reject" in content:
            return {"ok": False, "reason": "rejected"}
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return "unused"

    async def aclose(self) -> None:
        return None


def _scheduler(
    providers: list[FakeURLFetchProvider], *, logger: logging.Logger | None = None
) -> FetchScheduler:
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": _JudgeClient()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    return FetchScheduler(
        providers,
        ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages,
        logger=logger,
    )


async def test_fetch_scheduler_classifies_execution_semantic_and_accepted_outcomes() -> None:
    url = normalize_url("https://example.com")

    failed = FakeURLFetchProvider(
        "failed",
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider failed"),
    )
    accepted = FakeURLFetchProvider(
        "accepted",
        URLFetchCandidate(raw_content="raw", content="clean"),
    )
    result = await _scheduler([failed, accepted]).fetch_until_accepted(url)
    assert result.kind == "accepted"
    assert result.candidate == URLFetchCandidate("raw", "clean")
    assert len(result.failures) == 1
    assert failed.calls == [url]
    assert accepted.calls == [url]

    malformed = FakeURLFetchProvider("malformed", URLFetchCandidate(raw_content=""))
    malformed_result = await _scheduler([malformed]).fetch_until_accepted(url)
    assert malformed_result.kind == "execution_failure"
    assert len(malformed_result.failures) == 1

    cheap_semantic = FakeURLFetchProvider("cheap", URLFetchCandidate(raw_content="   "))
    semantic_result = await _scheduler([cheap_semantic]).fetch_until_accepted(url)
    assert semantic_result.kind == "semantic_failure"

    judge_semantic = FakeURLFetchProvider(
        "judge-semantic",
        URLFetchCandidate(raw_content="judge-reject"),
    )
    semantic_result = await _scheduler([judge_semantic]).fetch_until_accepted(url)
    assert semantic_result.kind == "semantic_failure"

    judge_execution = FakeURLFetchProvider(
        "judge-execution",
        URLFetchCandidate(raw_content="judge-execution"),
    )
    later_success = FakeURLFetchProvider("later", URLFetchCandidate(raw_content="later-success"))
    with pytest.raises(ExecutionFailure) as caught:
        await _scheduler([judge_execution, later_success]).fetch_until_accepted(url)
    assert caught.value.message == "judge failed"
    assert later_success.calls == []

    mixed = await _scheduler(
        [
            FakeURLFetchProvider(
                "bad", failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "bad")
            ),
            FakeURLFetchProvider("semantic", URLFetchCandidate(raw_content="judge-reject")),
        ]
    ).fetch_until_accepted(url)
    assert mixed.kind == "semantic_failure"

    first_success = FakeURLFetchProvider("first", URLFetchCandidate(raw_content="first"))
    never_called = FakeURLFetchProvider("never", URLFetchCandidate(raw_content="second"))
    stopped = await _scheduler([first_success, never_called]).fetch_until_accepted(url)
    assert stopped.kind == "accepted"
    assert never_called.calls == []


async def test_fetch_scheduler_attributes_judge_dependency_failure_and_stops_fallback() -> None:
    logger, stream = structured_test_logger("tests.fetch.judge-failure")
    invocation = LLMInvocation("judge", "judge-model", {})
    stages = LLMStages(
        {"judge": _JudgeClient()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )
    failed_judge = FakeURLFetchProvider(
        "first",
        URLFetchCandidate(raw_content="judge-execution"),
    )
    later = FakeURLFetchProvider("later", URLFetchCandidate(raw_content="later-success"))
    scheduler = FetchScheduler(
        [failed_judge, later],
        ProviderQuotaManager(web_limits={"first": 1, "later": 1}, llm_limits={}),
        stages,
        logger=logger,
    )

    with pytest.raises(ExecutionFailure):
        await scheduler.fetch_until_accepted(normalize_url("https://example.com"))

    assert later.calls == []
    assert any(
        "event=provider_failed" in line
        and "provider=first" in line
        and "failed_stage=judge" in line
        and "dependency=judge" in line
        for line in stream.getvalue().splitlines()
    )


async def test_fetch_scheduler_debug_events_cover_fallback_semantics_and_acceptance() -> None:
    logger, stream = structured_test_logger("tests.fetch.scheduler-events")
    invocation = LLMInvocation("judge", "judge-model", {})
    stages = LLMStages(
        {"judge": _JudgeClient()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )
    providers = [
        FakeURLFetchProvider(
            "failed",
            failure=ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "PROVIDER_FAILURE_SENTINEL",
            ),
        ),
        FakeURLFetchProvider(
            "semantic",
            URLFetchCandidate(raw_content="judge-reject PAGE_REJECT_SENTINEL"),
        ),
        FakeURLFetchProvider(
            "accepted",
            URLFetchCandidate(
                raw_content="PAGE_ACCEPT_SENTINEL",
                content="CLEAN_ACCEPT_SENTINEL",
            ),
        ),
    ]
    quotas = ProviderQuotaManager(
        web_limits={provider.name: 1 for provider in providers},
        llm_limits={},
    )
    scheduler = FetchScheduler(providers, quotas, stages, logger=logger)
    url = normalize_url("https://example.com/fetch?id=42&mode=test")

    with bind_request_id("0badc0de"):
        outcome = await scheduler.fetch_until_accepted(url)

    assert outcome.kind == "accepted"
    assert outcome.candidate == URLFetchCandidate(
        raw_content="PAGE_ACCEPT_SENTINEL",
        content="CLEAN_ACCEPT_SENTINEL",
    )
    assert [provider.calls for provider in providers] == [[url], [url], [url]]

    logged = stream.getvalue()
    lines = logged.splitlines()
    assert all("request=0badc0de" in line for line in lines)
    assert "url=https://example.com/fetch " in logged
    assert "?id=42&mode=test" not in logged
    assert sum("event=provider_selected" in line for line in lines) == 3
    assert any(
        "event=provider_failed" in line
        and "provider=failed" in line
        and "error_type=ExecutionFailure" in line
        for line in lines
    )
    assert any(
        "event=provider_fallback" in line
        and "provider=failed" in line
        and "outcome=execution_failure" in line
        for line in lines
    )
    assert any(
        "event=body_rejected" in line
        and "provider=semantic" in line
        and "reason=judge_rejected" in line
        for line in lines
    )
    assert "decision_reason=" not in logged
    assert "reason=rejected" not in logged
    assert any(
        "event=provider_fallback" in line
        and "provider=semantic" in line
        and "outcome=semantic_failure" in line
        for line in lines
    )
    assert any(
        "event=body_accepted" in line
        and "provider=accepted" in line
        and "raw_chars=" in line
        and "content_chars=" in line
        for line in lines
    )
    assert any(
        "event=provider_completed" in line
        and "provider=accepted" in line
        and "outcome=accepted" in line
        for line in lines
    )
    for sentinel in (
        "PROVIDER_FAILURE_SENTINEL",
        "PAGE_REJECT_SENTINEL",
        "PAGE_ACCEPT_SENTINEL",
        "CLEAN_ACCEPT_SENTINEL",
    ):
        assert sentinel not in logged


async def test_fetch_failure_terminal_preserves_diagnostics_before_successful_fallback() -> None:
    logger, stream = structured_test_logger("tests.fetch.terminal-diagnostics")
    failure = ExecutionFailure(
        ErrorCode.ALL_PROVIDERS_FAILED,
        "MESSAGE_SENTINEL",
        reason="REASON_SENTINEL",
        diagnostics={
            "upstream_code": "quota_exhausted",
            "http_status": 429,
            "provider": "OWNER_SENTINEL",
            "category": "OWNER_SENTINEL",
        },
    )
    failed = FakeURLFetchProvider("failed", failure=failure)
    accepted = FakeURLFetchProvider("accepted", URLFetchCandidate("body"))
    scheduler = _scheduler([failed, accepted], logger=logger)
    outcome = await scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    assert outcome.kind == "accepted"
    assert outcome.failures == (failure,)
    assert len(failed.calls) == len(accepted.calls) == 1
    logged = stream.getvalue()
    terminal = [line for line in logged.splitlines() if "event=provider_failed " in line]
    assert len(terminal) == 1
    assert "provider=failed " in terminal[0]
    assert "upstream_code=quota_exhausted" in terminal[0]
    assert "http_status=429" in terminal[0]
    assert "reason=execution_failure" in terminal[0]
    assert logged.count("event=provider_completed ") == 1
    assert logged.count("event=provider_fallback ") == 1
    assert "SENTINEL" not in logged


async def test_all_failed_fetch_attempts_each_own_one_terminal() -> None:
    logger, stream = structured_test_logger("tests.fetch.all-failed-terminals")
    failures = [
        ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "MESSAGE_SENTINEL",
            reason="no_matching_result",
            diagnostics={"upstream_code": f"code_{i}"},
        )
        for i in range(2)
    ]
    providers = [
        FakeURLFetchProvider(f"fetch-{i}", failure=failure) for i, failure in enumerate(failures)
    ]
    scheduler = _scheduler(providers, logger=logger)
    outcome = await scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    assert outcome.kind == "execution_failure"
    assert outcome.failures == tuple(failures)
    logged = stream.getvalue()
    assert logged.count("event=provider_started") == 2
    assert logged.count("event=provider_failed ") == 2
    assert logged.count("event=provider_fallback ") == 2
    assert "event=provider_completed " not in logged
    for i in range(2):
        terminal = next(
            line
            for line in logged.splitlines()
            if "event=provider_failed " in line and f"provider=fetch-{i} " in line
        )
        assert f"upstream_code=code_{i}" in terminal
        assert "reason=no_matching_result" in terminal
    assert "SENTINEL" not in logged


@pytest.mark.parametrize("body", ["   ", "judge-reject"])
async def test_semantic_fetch_rejection_completes_once_before_fallback(body: str) -> None:
    logger, stream = structured_test_logger("tests.fetch.semantic-terminals")
    providers = [
        FakeURLFetchProvider("rejected", URLFetchCandidate(body)),
        FakeURLFetchProvider("accepted", URLFetchCandidate("body")),
    ]
    scheduler = _scheduler(providers, logger=logger)
    outcome = await scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    assert outcome.kind == "accepted"
    assert outcome.failures == ()
    logged = stream.getvalue()
    assert logged.count("event=provider_started") == 2
    assert logged.count("event=provider_completed ") == 2
    assert "event=provider_failed " not in logged
    assert logged.count("event=provider_fallback ") == 1
    assert logged.count("event=body_rejected ") == 1
    terminal = next(
        line
        for line in logged.splitlines()
        if "event=provider_completed " in line and "provider=rejected " in line
    )
    assert "outcome=semantic_failure" in terminal


async def test_judge_failure_keeps_diagnostics_and_public_failure_without_fallback() -> None:
    logger, stream = structured_test_logger("tests.fetch.judge-terminal")
    invocation = LLMInvocation("judge", "judge-model", {})
    original = ExecutionFailure(
        ErrorCode.LLM_STAGE_FAILED,
        "PUBLIC_MESSAGE_SENTINEL",
        reason="timeout",
        diagnostics={
            "upstream_code": "judge_busy",
            "upstream_type": "capacity",
            "http_status": 503,
            "stage": "OWNER_SENTINEL",
        },
    )
    stages = LLMStages(
        {"judge": FakeLLMClient("judge", failure=original)},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )
    first = FakeURLFetchProvider("first", URLFetchCandidate("body"))
    later = FakeURLFetchProvider("later")
    scheduler = FetchScheduler(
        [first, later],
        ProviderQuotaManager(web_limits={"first": 1, "later": 1}, llm_limits={}),
        stages,
        logger=logger,
    )
    with pytest.raises(ExecutionFailure) as caught:
        await scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    assert caught.value.code == original.code
    assert caught.value.message == original.message
    assert caught.value.reason == original.reason
    assert caught.value.__cause__ is original
    assert later.calls == []
    logged = stream.getvalue()
    assert logged.count("event=provider_started") == 1
    terminal = [line for line in logged.splitlines() if "event=provider_failed " in line]
    assert len(terminal) == 1
    assert all(
        field in terminal[0]
        for field in (
            "provider=first",
            "stage=fetch",
            "failed_stage=judge",
            "dependency=judge",
            "reason=timeout",
            "upstream_code=judge_busy",
            "upstream_type=capacity",
            "http_status=503",
        )
    )
    assert "upstream_status=" not in terminal[0]
    assert caught.value.diagnostics
    assert "event=provider_completed " not in logged
    assert "event=provider_fallback " not in logged
    assert "SENTINEL" not in logged


@pytest.mark.parametrize("phase", ["adapter", "judge"])
async def test_fetch_cancellation_records_one_terminal_and_never_falls_back(phase: str) -> None:
    entered = asyncio.Event()

    class BlockingProvider(FakeURLFetchProvider):
        async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
            if phase == "adapter":
                entered.set()
                await asyncio.Event().wait()
            return URLFetchCandidate("body")

    class BlockingJudge(_JudgeClient):
        async def complete_json(
            self, invocation: LLMInvocation, messages: Sequence[ChatMessage]
        ) -> Mapping[str, object]:
            entered.set()
            await asyncio.Event().wait()
            return {"ok": True}

    logger, stream = structured_test_logger("tests.fetch.cancel-terminal")
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": BlockingJudge()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    later = FakeURLFetchProvider("later")
    quotas = ProviderQuotaManager(web_limits={"first": 1, "later": 1}, llm_limits={})
    scheduler = FetchScheduler([BlockingProvider("first"), later], quotas, stages, logger=logger)
    task = asyncio.create_task(
        scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    )
    async with asyncio.timeout(1):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    logged = stream.getvalue()
    assert later.calls == []
    assert quotas.get_web("first").in_use == 0
    assert logged.count("event=provider_started") == 1
    assert logged.count("event=provider_failed ") == 1
    assert "reason=cancelled" in logged
    assert "event=provider_completed " not in logged
    assert "event=provider_fallback " not in logged
    if phase == "judge":
        assert "failed_stage=judge" in logged and "dependency=judge" in logged


async def test_unexpected_fetch_validation_error_logs_once_without_changing_propagation() -> None:
    unexpected = ValueError("UNEXPECTED_PAYLOAD_SENTINEL")

    class InvalidText(str):
        def strip(self, chars: str | None = None) -> str:
            raise unexpected

    logger, stream = structured_test_logger("tests.fetch.unexpected")
    first = FakeURLFetchProvider("first", URLFetchCandidate(InvalidText("body")))
    later = FakeURLFetchProvider("later")
    scheduler = _scheduler([first, later], logger=logger)
    with pytest.raises(ValueError) as caught:
        await scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    assert caught.value is unexpected
    assert later.calls == []
    logged = stream.getvalue()
    assert logged.count("event=provider_started") == 1
    assert logged.count("event=provider_failed ") == 1
    assert "reason=execution_failure" in logged
    assert "event=provider_completed " not in logged
    assert "event=provider_fallback " not in logged
    assert "SENTINEL" not in logged
