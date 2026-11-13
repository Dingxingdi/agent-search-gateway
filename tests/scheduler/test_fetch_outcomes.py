from collections.abc import Mapping, Sequence

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.request_ids import bind_request_id
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from tests.support.fakes import FakeURLFetchProvider
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


def _scheduler(providers: list[FakeURLFetchProvider]) -> FetchScheduler:
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
