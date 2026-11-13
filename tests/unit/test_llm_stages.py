import asyncio
from collections.abc import Sequence

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, StageDecision
from agent_search_gateway.providers.contracts import ChatMessage
from tests.support.fakes import FakeLLMClient
from tests.support.logging import structured_test_logger


class _CancelledTextClient(FakeLLMClient):
    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        raise asyncio.CancelledError


async def test_llm_stages_use_resolved_invocations_and_validate_outputs() -> None:
    judge_invocation = LLMInvocation("judge", "judge-model", {"stage": "judge"})
    safety_invocation = LLMInvocation("safety", "safety-model", {"stage": "safety"})
    clean_invocation = LLMInvocation("clean", "clean-model", {"stage": "clean"})
    focus_invocation = LLMInvocation("focus", "focus-model", {"stage": "focus"})
    search_invocation = LLMInvocation("search", "search-model", {"stage": "search"})

    judge_client = FakeLLMClient("judge", json_result={"ok": True})
    safety_client = FakeLLMClient("safety", json_result={"ok": False, "reason": "unsafe"})
    clean_client = FakeLLMClient("clean", text_result="  cleaned content  ")
    focus_client = FakeLLMClient("focus", text_result="  focused summary  ")
    search_client = FakeLLMClient(
        "search",
        text_result="## Result\nURL: https://example.com\nAbstract: result",
    )
    stages = LLMStages(
        {
            "judge": judge_client,
            "safety": safety_client,
            "clean": clean_client,
            "focus": focus_client,
            "search": search_client,
        },
        judge=judge_invocation,
        safety=safety_invocation,
        content_clean=clean_invocation,
        focus_summary=focus_invocation,
    )

    assert await stages.safety("final content") == StageDecision(False, "unsafe")
    assert await stages.content_clean("raw content") == "cleaned content"
    assert await stages.focus_summary("final content", "  pricing details  ") == "focused summary"
    markdown = await stages.llm_search_markdown(search_invocation, "find docs")
    assert markdown.startswith("## Result")

    assert safety_client.json_calls[0][0] == safety_invocation
    assert clean_client.text_calls[0][0] == clean_invocation
    assert focus_client.text_calls[0][0] == focus_invocation
    assert search_client.text_calls[0][0] == search_invocation

    focus_prompt = focus_client.text_calls[0][1][-1]["content"]
    assert "pricing details" in focus_prompt
    assert "final content" in focus_prompt
    search_prompt = "\n".join(message["content"] for message in search_client.text_calls[0][1])
    assert "## Result" in search_prompt
    assert "URL:" in search_prompt
    assert "Abstract:" in search_prompt

    safety_client.json_result = {"ok": "yes"}
    with pytest.raises(ExecutionFailure) as safety_error:
        await stages.safety("content")
    assert safety_error.value.code is ErrorCode.LLM_STAGE_FAILED

    clean_client.text_result = "   "
    with pytest.raises(ExecutionFailure) as clean_error:
        await stages.content_clean("raw")
    assert clean_error.value.code is ErrorCode.LLM_STAGE_FAILED

    focus_client.text_result = ""
    with pytest.raises(ExecutionFailure) as focus_error:
        await stages.focus_summary("content", "topic")
    assert focus_error.value.code is ErrorCode.LLM_STAGE_FAILED

    search_client.text_result = "   "
    with pytest.raises(ExecutionFailure) as search_error:
        await stages.llm_search_markdown(search_invocation, "find")
    assert search_error.value.code is ErrorCode.LLM_STAGE_FAILED

    upstream_failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "upstream")
    focus_client.failure = upstream_failure
    with pytest.raises(ExecutionFailure) as upstream:
        await stages.focus_summary("content", "topic")
    assert upstream.value is upstream_failure
    assert judge_client.text_calls == []


async def test_llm_stage_debug_events_are_semantic_and_payload_safe() -> None:
    logger, stream = structured_test_logger("tests.llm.semantic-events")
    judge = LLMInvocation("judge", "judge-model", {})
    safety = LLMInvocation("safety", "safety-model", {})
    clean = LLMInvocation("clean", "clean-model", {})
    focus = LLMInvocation("focus", "focus-model", {})
    search = LLMInvocation("search", "search-model", {})
    long_reason = "MODEL_DECISION_REASON_SENTINEL unsafe\nreason\t" + ("r" * 220)
    clients = {
        "judge": FakeLLMClient("judge", json_result={"ok": True, "reason": "useful"}),
        "safety": FakeLLMClient("safety", json_result={"ok": False, "reason": long_reason}),
        "clean": FakeLLMClient("clean", text_result="MODEL_CLEAN_BODY_SENTINEL"),
        "focus": FakeLLMClient("focus", text_result="MODEL_FOCUS_BODY_SENTINEL"),
        "search": FakeLLMClient("search", text_result="MODEL_SEARCH_BODY_SENTINEL"),
    }
    stages = LLMStages(
        clients,
        judge=judge,
        safety=safety,
        content_clean=clean,
        focus_summary=focus,
        logger=logger,
    )

    assert await stages.judge("PAGE_BODY_SENTINEL") == StageDecision(True, "useful")
    assert await stages.safety("FINAL_PAGE_SENTINEL") == StageDecision(False, long_reason)
    assert await stages.content_clean("RAW_PAGE_SENTINEL") == "MODEL_CLEAN_BODY_SENTINEL"
    assert (
        await stages.focus_summary(
            "FINAL_PAGE_SENTINEL",
            "FOCUS_BODY_SENTINEL",
        )
        == "MODEL_FOCUS_BODY_SENTINEL"
    )
    assert (
        await stages.llm_search_markdown(
            search,
            "USER_PROMPT_SENTINEL",
        )
        == "MODEL_SEARCH_BODY_SENTINEL"
    )

    logged = stream.getvalue()
    lines = logged.splitlines()
    for stage_name, provider, model in (
        ("judge", "judge", "judge-model"),
        ("safety", "safety", "safety-model"),
        ("content_clean", "clean", "clean-model"),
        ("focus_summary", "focus", "focus-model"),
        ("llm_search", "search", "search-model"),
    ):
        assert any(
            "event=llm_stage_started" in line
            and f"stage={stage_name}" in line
            and f"provider={provider}" in line
            and f"model={model}" in line
            and "input_chars=" in line
            for line in lines
        )
        assert any(
            "event=llm_stage_completed" in line
            and f"stage={stage_name}" in line
            and "elapsed_ms=" in line
            for line in lines
        )
    assert any(
        "stage=judge" in line and "ok=true" in line and "reason_present=true" in line
        for line in lines
    )
    safety_line = next(
        line for line in lines if "event=llm_stage_completed" in line and "stage=safety" in line
    )
    assert "ok=false" in safety_line
    assert "reason_present=true" in safety_line
    assert "MODEL_DECISION_REASON_SENTINEL" not in logged
    assert "reason=useful" not in logged
    assert any("stage=content_clean" in line and "output_chars=25" in line for line in lines)
    assert any("stage=focus_summary" in line and "focus_present=true" in line for line in lines)
    assert any("stage=llm_search" in line and "output_chars=26" in line for line in lines)

    for sentinel in (
        "PAGE_BODY_SENTINEL",
        "FINAL_PAGE_SENTINEL",
        "RAW_PAGE_SENTINEL",
        "FOCUS_BODY_SENTINEL",
        "USER_PROMPT_SENTINEL",
        "MODEL_CLEAN_BODY_SENTINEL",
        "MODEL_FOCUS_BODY_SENTINEL",
        "MODEL_SEARCH_BODY_SENTINEL",
    ):
        assert sentinel not in logged

    clients["search"].failure = ExecutionFailure(
        ErrorCode.ALL_PROVIDERS_FAILED,
        "MODEL_FAILURE_DETAIL_SENTINEL",
    )
    with pytest.raises(ExecutionFailure):
        await stages.llm_search_markdown(search, "SECOND_PROMPT_SENTINEL")
    failure_log = stream.getvalue()
    assert "event=llm_stage_failed" in failure_log
    assert "stage=llm_search" in failure_log
    assert "error_type=ExecutionFailure" in failure_log
    assert "MODEL_FAILURE_DETAIL_SENTINEL" not in failure_log
    assert "SECOND_PROMPT_SENTINEL" not in failure_log


async def test_llm_stage_cancellation_is_logged_and_reraised_without_payload() -> None:
    logger, stream = structured_test_logger("tests.llm.cancelled")
    invocation = LLMInvocation("cancel", "cancel-model", {})
    stages = LLMStages(
        {"cancel": _CancelledTextClient("cancel")},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )

    with pytest.raises(asyncio.CancelledError):
        await stages.content_clean("CANCELLED_PAGE_SENTINEL")

    logged = stream.getvalue()
    assert "event=llm_stage_started" in logged
    assert "event=llm_stage_cancelled" in logged
    assert "stage=content_clean" in logged
    assert "CANCELLED_PAGE_SENTINEL" not in logged


async def test_llm_stage_failure_carries_safe_dependency_diagnostics() -> None:
    logger, stream = structured_test_logger("tests.llm.failure-diagnostics")
    invocation = LLMInvocation("openai", "stage-model", {})
    failure = ExecutionFailure(
        ErrorCode.LLM_STAGE_FAILED,
        "ARBITRARY_EXCEPTION_SENTINEL",
        diagnostics={
            "http_status": 429,
            "upstream_code": "rate_limit",
            "upstream_type": "rate_limit_error",
            "upstream_message": "Capacity exhausted",
            "category": "nonzero_exit",
            "provider": "WRONG_OWNER",
            "stage": "WRONG_STAGE",
            "model": "WRONG_MODEL",
            "answer": "ANSWER_SENTINEL",
        },
    )
    stages = LLMStages(
        {"openai": FakeLLMClient("openai", failure=failure)},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )

    with pytest.raises(ExecutionFailure) as caught:
        await stages.content_clean("PROMPT_SENTINEL")

    assert caught.value is failure
    logged = stream.getvalue()
    failed = [line for line in logged.splitlines() if "event=llm_stage_failed" in line]
    assert len(failed) == 1
    assert "http_status=429" in failed[0] and "upstream_code=rate_limit" in failed[0]
    assert "upstream_type=rate_limit_error" in failed[0]
    assert "upstream_message=" in failed[0] and "Capacity exhausted" in failed[0]
    assert "provider=openai" in failed[0] and "stage=content_clean" in failed[0]
    assert "model=stage-model" in failed[0] and "category=execution_failure" in failed[0]
    for sentinel in (
        "ARBITRARY_EXCEPTION_SENTINEL",
        "ANSWER_SENTINEL",
        "PROMPT_SENTINEL",
        "WRONG_",
    ):
        assert sentinel not in logged
    assert "event=provider_failed" not in logged
