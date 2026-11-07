import asyncio
from collections.abc import Sequence

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.prompts import llm_paper_search_messages, llm_search_messages
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import AgentInvocation, LLMInvocation
from agent_search_gateway.providers.contracts import ChatMessage
from tests.support.fakes import FakeLLMClient
from tests.support.logging import structured_test_logger


@pytest.mark.parametrize("model", [None, "configured-model"])
@pytest.mark.parametrize("paper", [False, True])
async def test_agent_web_stage_uses_existing_prompt_messages(
    model: str | None, paper: bool
) -> None:
    assert hasattr(LLMStages, "agent_search_markdown"), "Agent prompt facade must be provided"
    calls: list[tuple[str | None, Sequence[ChatMessage]]] = []

    async def runner(pin: str | None, messages: Sequence[ChatMessage]) -> str:
        calls.append((pin, messages))
        return "  raw-markdown  "

    invocation = LLMInvocation("same", "llm-model")
    client = FakeLLMClient("same", text_result="llm text")
    logger, stream = structured_test_logger("tests.agent.facade")
    stages = LLMStages(
        {"same": client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        agent_runners={"same": runner},
        logger=logger,
    )
    generate = stages.agent_paper_search_markdown if paper else stages.agent_search_markdown
    assert await generate(AgentInvocation("same", model), "prompt") == "raw-markdown"
    expected = llm_paper_search_messages("prompt") if paper else llm_search_messages("prompt")
    assert calls == [(model, expected)]
    assert not client.text_calls and not client.json_calls
    assert "llm_stage_" not in stream.getvalue()
    assert await stages.llm_search_markdown(invocation, "llm") == "llm text"
    assert len(calls) == 1


@pytest.mark.parametrize("paper", [False, True])
@pytest.mark.parametrize("mode", ["missing", "empty", "cancelled"])
async def test_agent_stage_missing_runner_empty_text_and_cancellation(
    paper: bool, mode: str
) -> None:
    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        if mode == "cancelled":
            raise asyncio.CancelledError
        return "  "

    inv = LLMInvocation("same", "llm")
    stages = LLMStages(
        {},
        judge=inv,
        safety=inv,
        content_clean=inv,
        focus_summary=inv,
        agent_runners={} if mode == "missing" else {"same": runner},
    )
    generate = stages.agent_paper_search_markdown if paper else stages.agent_search_markdown
    if mode == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await generate(AgentInvocation("same"), "prompt")
    else:
        with pytest.raises(ExecutionFailure) as caught:
            await generate(AgentInvocation("same"), "prompt")
        assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
