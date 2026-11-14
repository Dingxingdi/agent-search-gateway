import asyncio
import json
from collections.abc import Awaitable, Sequence
from pathlib import Path
from time import monotonic

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import AgentInvocation, OAResolution, SearchScope
from agent_search_gateway.providers.contracts import ChatMessage
from tests.support.agent_search import agent_orchestrator, paper_markdown, web_markdown


async def _with_real_watchdog(work: Awaitable[str]) -> str:
    """Bound test hangs using real elapsed time, even while loop.time advances."""

    async def watchdog() -> None:
        deadline = monotonic() + 2
        while monotonic() < deadline:
            await asyncio.sleep(0.01)
        pytest.fail("Agent workflow did not finish within the real-time watchdog")

    task = asyncio.ensure_future(work)
    guard = asyncio.create_task(watchdog())
    try:
        done, _ = await asyncio.wait((task, guard), return_when=asyncio.FIRST_COMPLETED)
        if guard in done:
            await guard
        return await task
    finally:
        task.cancel()
        guard.cancel()
        await asyncio.gather(task, guard, return_exceptions=True)


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_agent_workflow_can_complete_after_old_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: SearchScope,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock_offset = 0.0

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        branch = "paper" if "## Paper" in messages[0]["content"] else "web"
        calls.append(branch)
        if len(calls) == (2 if scope == "all" else 1):
            entered.set()
        await release.wait()
        return paper_markdown() if branch == "paper" else web_markdown()

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})

    async def exercise() -> str:
        nonlocal clock_offset
        task = asyncio.create_task(search.agent_search("q", request_id="11111111", scope=scope))
        try:
            await entered.wait()
            clock_offset = 1141.0
            # Run actual asyncio timer callbacks before allowing the runner to finish.
            await asyncio.sleep(0.001)
            release.set()
            return await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(loop, "time", lambda: real_time() + clock_offset)
        path = Path(await _with_real_watchdog(exercise()))

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    expected_branches = ["web", "paper"] if scope == "all" else [scope]
    assert sorted(calls) == sorted(expected_branches)
    assert path.name == "agent-11111111.jsonl"
    assert len(rows) == len(expected_branches)
    if scope == "all":
        assert [row["type"] for row in rows] == expected_branches
    else:
        assert "type" not in rows[0]
    for branch, row in zip(expected_branches, rows, strict=True):
        assert row["url"] == f"https://example.test/{branch}"
        if branch == "web":
            assert row["abstract"] == "Agent result"
        else:
            assert row["title"] == "Shared Paper"
            assert row["identifiers"]["doi"] == "10.1000/shared"
            assert row["sources"] == ["agent:a"]


async def test_queued_invocation_gets_its_own_budget_after_old_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = CapacityGate(1)
    first_entered, second_queued, second_entered = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    release_first, release_second = asyncio.Event(), asyncio.Event()
    starts: list[float] = []
    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock_offset = 0.0

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        if model == "second":
            second_queued.set()
        async with gate.lease():
            starts.append(loop.time())
            # A controlled process budget starts only after the provider quota.
            async with asyncio.timeout(1200):
                if model == "first":
                    first_entered.set()
                    await release_first.wait()
                else:
                    second_entered.set()
                    await release_second.wait()
            return web_markdown(f"https://example.test/{model}", str(model))

    search = agent_orchestrator(
        tmp_path,
        [AgentInvocation("a", "first"), AgentInvocation("a", "second")],
        {"a": runner},
    )

    async def exercise() -> str:
        nonlocal clock_offset
        task = asyncio.create_task(search.agent_search("q", request_id="11111111"))
        try:
            await first_entered.wait()
            await second_queued.wait()
            assert gate.in_use == 1 and not second_entered.is_set()
            clock_offset = 1141.0
            await asyncio.sleep(0.001)
            release_first.set()
            await second_entered.wait()
            clock_offset = 2282.0
            await asyncio.sleep(0.001)
            release_second.set()
            return await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(loop, "time", lambda: real_time() + clock_offset)
        path = Path(await _with_real_watchdog(exercise()))

    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"url": "https://example.test/first", "abstract": "first"},
        {"url": "https://example.test/second", "abstract": "second"},
    ]
    assert starts[1] - starts[0] >= 1141
    assert gate.in_use == 0 and gate.max_observed_in_use == 1


@pytest.mark.parametrize("slow_phase", ["paper", "oa"])
async def test_completed_web_waits_for_paper_finalization_after_old_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow_phase: str,
) -> None:
    web_finished, phase_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock_offset = 0.0

    class Resolver:
        name = "test"

        async def resolve(self, doi: str) -> OAResolution | None:
            assert doi == "10.1000/shared"
            if slow_phase == "oa":
                phase_entered.set()
                await release.wait()
            return OAResolution(
                landing_url=None, pdf_url=None, is_open_access=True, oa_status="gold"
            )

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        if "## Paper" not in messages[0]["content"]:
            web_finished.set()
            return web_markdown()
        if slow_phase == "paper":
            phase_entered.set()
            await release.wait()
        return paper_markdown()

    search = agent_orchestrator(
        tmp_path, [AgentInvocation("a")], {"a": runner}, resolver=Resolver()
    )

    async def exercise() -> str:
        nonlocal clock_offset
        task = asyncio.create_task(search.agent_search("q", request_id="11111111", scope="all"))
        try:
            await web_finished.wait()
            await phase_entered.wait()
            clock_offset = 1141.0
            await asyncio.sleep(0.001)
            assert not task.done()
            assert not list(tmp_path.glob("*.jsonl"))
            release.set()
            return await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(loop, "time", lambda: real_time() + clock_offset)
        path = Path(await _with_real_watchdog(exercise()))

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["type"] for row in rows] == ["web", "paper"]
    assert rows[0] == {"type": "web", "url": "https://example.test/web", "abstract": "Agent result"}
    assert rows[1]["url"] == "https://example.test/paper"
    assert rows[1]["sources"] == ["agent:a"]
    assert rows[1]["is_open_access"] is True
    assert rows[1]["oa_status"] == "gold"


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


async def test_llm_search_still_completes_both_scopes(tmp_path: Path) -> None:
    from tests.orchestrators.test_scoped_llm_search import ScopedClient, make_orchestrator

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
