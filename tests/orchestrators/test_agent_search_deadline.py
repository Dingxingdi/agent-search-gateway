import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import AgentInvocation, OAResolution, SearchScope
from agent_search_gateway.orchestrators import search as search_module
from agent_search_gateway.providers.contracts import ChatMessage
from tests.support.agent_search import agent_orchestrator, paper_markdown, web_markdown


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_agent_workflow_deadline_cancels_running_and_queued_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: SearchScope,
) -> None:
    monkeypatch.setattr(search_module, "_AGENT_WORKFLOW_TIMEOUT_SECONDS", 0.03, raising=False)
    gate = CapacityGate(1)
    entered: list[str | None] = []
    cleaned = asyncio.Event()

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        async with gate.lease():
            entered.append(model)
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return web_markdown()

    search = agent_orchestrator(
        tmp_path,
        [AgentInvocation("a"), AgentInvocation("a", "queued")],
        {"a": runner},
    )
    async with asyncio.timeout(1):
        with pytest.raises(ExecutionFailure) as caught:
            await search.agent_search("q", request_id="11111111", scope=scope)
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert caught.value.message == "Agent search workflow timed out"
    assert caught.value.reason == "timeout"
    assert entered == [None] and cleaned.is_set() and gate.in_use == 0
    assert not list(tmp_path.glob("*.jsonl"))


async def test_agent_workflow_deadline_does_not_write_completed_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "_AGENT_WORKFLOW_TIMEOUT_SECONDS", 0.03, raising=False)
    web_finished = asyncio.Event()
    paper_cleaned = asyncio.Event()

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        if "## Paper" not in messages[0]["content"]:
            web_finished.set()
            return web_markdown()
        try:
            await asyncio.Event().wait()
        finally:
            paper_cleaned.set()
        return paper_markdown()

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    with pytest.raises(ExecutionFailure, match="workflow timed out"):
        await asyncio.wait_for(
            search.agent_search("q", request_id="11111111", scope="all"),
            1,
        )
    assert web_finished.is_set() and paper_cleaned.is_set()
    assert not list(tmp_path.glob("*.jsonl"))


async def test_agent_workflow_deadline_includes_oa_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "_AGENT_WORKFLOW_TIMEOUT_SECONDS", 0.03, raising=False)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Resolver:
        name = "test"

        async def resolve(self, doi: str) -> OAResolution | None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return None

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        return paper_markdown()

    search = agent_orchestrator(
        tmp_path,
        [AgentInvocation("a")],
        {"a": runner},
        resolver=Resolver(),
    )
    with pytest.raises(ExecutionFailure, match="workflow timed out"):
        await asyncio.wait_for(
            search.agent_search("q", request_id="11111111", scope="paper"),
            1,
        )
    assert entered.is_set() and cleaned.is_set()
    assert not list(tmp_path.glob("*.jsonl"))


async def test_external_cancellation_remains_cancellation_and_awaits_cleanup(
    tmp_path: Path,
) -> None:
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
        return web_markdown()

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    task = asyncio.create_task(search.agent_search("q", request_id="11111111", scope="all"))
    async with asyncio.timeout(1):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cleaned.is_set() and not list(tmp_path.glob("*.jsonl"))


async def test_agent_deadline_does_not_apply_to_llm_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.orchestrators.test_scoped_llm_search import ScopedClient, make_orchestrator

    monkeypatch.setattr(search_module, "_AGENT_WORKFLOW_TIMEOUT_SECONDS", 0.0)
    client = ScopedClient("old", synchronize_branches=True)
    search = make_orchestrator(tmp_path, {"old": client})
    path = Path(await search.llm_search("q", request_id="11111111", scope="all"))
    assert path.exists() and client.calls == ["web", "paper"]


async def test_single_agent_timeout_keeps_partial_success_semantics(tmp_path: Path) -> None:
    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        if "## Paper" in messages[0]["content"]:
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED, "Agent invocation failed", reason="timeout"
            )
        return web_markdown()

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    path = Path(await search.agent_search("q", request_id="11111111", scope="all"))
    assert path.read_bytes() == (
        b'{"type":"web","url":"https://example.test/web","abstract":"Agent result"}\n'
    )
