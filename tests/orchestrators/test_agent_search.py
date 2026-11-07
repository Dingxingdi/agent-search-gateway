import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import AgentInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.agent_search import RecordingRunner, agent_orchestrator, web_markdown


async def test_agent_web_search_writes_existing_web_schema(tmp_path: Path) -> None:
    assert hasattr(SearchOrchestrator, "agent_search"), "Agent search workflow must be present"
    runner, store = RecordingRunner(), URLStore()
    search = agent_orchestrator(
        tmp_path, [AgentInvocation("agent")], {"agent": runner}, store=store
    )
    path = Path(await search.agent_search(" query ", request_id="11111111"))
    assert path.is_absolute() and path.name == "agent-11111111.jsonl"
    assert path.read_bytes() == (b'{"url":"https://example.test/web","abstract":"Agent result"}\n')
    assert store.get(normalize_url("https://example.test/web")) is not None
    explicit = Path(await search.agent_search("query", request_id="22222222", scope="web"))
    assert explicit.read_bytes() == path.read_bytes()
    assert [call[0] for call in runner.calls] == [None, None]
    assert runner.calls[0][1][-1]["content"] == "query"


async def test_agent_web_preserves_order_and_admits_only_complete_parsed_results(
    tmp_path: Path,
) -> None:
    later_finished = asyncio.Event()

    async def first(model: str | None, messages: Sequence[ChatMessage]) -> str:
        await later_finished.wait()
        return web_markdown("https://EXAMPLE.test/first", "first")

    async def later(model: str | None, messages: Sequence[ChatMessage]) -> str:
        later_finished.set()
        return web_markdown("https://example.test/first", "duplicate") + web_markdown(
            "https://example.test/second",
            "second",
        )

    bad = RecordingRunner(
        web_markdown("https://example.test/not-admitted") + "## Result\nURL: missing-abstract"
    )
    empty = RecordingRunner(web_markdown("https://example.test/empty", ""))
    store = URLStore()
    search = agent_orchestrator(
        tmp_path,
        [AgentInvocation(name) for name in ("first", "bad", "later", "empty")],
        {"first": first, "bad": bad, "later": later, "empty": empty},
        store=store,
    )
    path = Path(await search.agent_search("query", request_id="11111111"))
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"url": "https://example.test/first", "abstract": "first"},
        {"url": "https://example.test/second", "abstract": "second"},
    ]
    assert store.get(normalize_url("https://example.test/not-admitted")) is None
    assert store.get(normalize_url("https://example.test/empty")) is None


@pytest.mark.parametrize("successful_empty", [False, True])
async def test_agent_web_all_failure_vs_successful_empty(
    tmp_path: Path,
    successful_empty: bool,
) -> None:
    runners = {
        "a": RecordingRunner("invalid"),
        "b": RecordingRunner(
            web_markdown(abstract="") if successful_empty else "invalid",
        ),
    }
    search = agent_orchestrator(tmp_path, [AgentInvocation("a"), AgentInvocation("b")], runners)
    if successful_empty:
        path = Path(await search.agent_search("query", request_id="11111111"))
        assert path.read_bytes() == b""
    else:
        with pytest.raises(ExecutionFailure) as caught:
            await search.agent_search("query", request_id="11111111")
        assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
        assert not list(tmp_path.glob("*.jsonl"))


async def test_agent_paper_search_uses_shared_aggregation_and_admission(tmp_path: Path) -> None:
    from agent_search_gateway.models import OAResolution
    from tests.support.agent_search import paper_markdown
    from tests.support.fakes import FakeOAResolver

    runners = {
        "z-first": RecordingRunner(paper_markdown()),
        "a-second": RecordingRunner(paper_markdown("https://example.test/duplicate")),
        "broken": RecordingRunner("invalid paper"),
    }
    store = URLStore()
    resolver = FakeOAResolver(
        OAResolution(
            landing_url=normalize_url("https://example.test/open"),
            pdf_url=normalize_url("https://example.test/paper.pdf"),
            is_open_access=True,
            oa_status="gold",
        )
    )
    search = agent_orchestrator(
        tmp_path,
        [AgentInvocation(name) for name in runners],
        runners,
        store=store,
        resolver=resolver,
    )
    path = Path(await search.agent_search("papers", request_id="11111111", scope="paper"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert path.name == "agent-11111111.jsonl"
    assert len(rows) == 1 and "type" not in rows[0]
    assert rows[0]["sources"] == ["agent:z-first", "agent:a-second"]
    assert rows[0]["identifiers"]["doi"] == "10.1000/shared"
    assert rows[0]["is_open_access"] is True
    assert resolver.calls == ["10.1000/shared"]
    assert store.get(normalize_url(rows[0]["url"])) is not None
    assert store.get(normalize_url(rows[0]["pdf_url"])) is None
    assert all(len(runner.calls) == 1 for runner in runners.values())


async def test_agent_paper_aggregation_can_succeed_with_no_valid_records(tmp_path: Path) -> None:
    from tests.support.agent_search import paper_markdown

    runner = RecordingRunner(paper_markdown(doi="invalid-doi"))
    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    path = Path(await search.agent_search("q", request_id="11111111", scope="paper"))
    assert path.read_bytes() == b""


async def test_agent_all_schedules_both_branches_and_writes_web_then_paper(tmp_path: Path) -> None:
    from tests.support.agent_search import paper_markdown

    started: set[str] = set()
    both = asyncio.Event()

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        branch = "paper" if "## Paper" in messages[0]["content"] else "web"
        started.add(branch)
        if len(started) == 2:
            both.set()
        await both.wait()
        return paper_markdown() if branch == "paper" else web_markdown()

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    async with asyncio.timeout(1):
        path = Path(await search.agent_search("q", request_id="11111111", scope="all"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert started == {"web", "paper"}
    assert [row["type"] for row in rows] == ["web", "paper"]
    assert rows[0] == {
        "type": "web",
        "url": "https://example.test/web",
        "abstract": "Agent result",
    }
    assert rows[1]["sources"] == ["agent:a"]


@pytest.mark.parametrize(
    ("web", "paper", "types"),
    [
        ("ok", "ok", ["web", "paper"]),
        ("ok", "bad", ["web"]),
        ("bad", "ok", ["paper"]),
        ("bad", "bad", None),
        ("empty", "bad", []),
        ("bad", "empty", []),
    ],
)
async def test_agent_all_partial_success_matrix(
    tmp_path: Path,
    web: str,
    paper: str,
    types: list[str] | None,
) -> None:
    from tests.support.agent_search import paper_markdown

    calls: list[str] = []

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        branch = "paper" if "## Paper" in messages[0]["content"] else "web"
        calls.append(branch)
        mode = paper if branch == "paper" else web
        if mode == "bad":
            return "invalid Markdown"
        if branch == "paper":
            return paper_markdown(doi="invalid-doi" if mode == "empty" else "10.1000/shared")
        return web_markdown(abstract="" if mode == "empty" else "Agent result")

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    if types is None:
        with pytest.raises(ExecutionFailure) as caught:
            await search.agent_search("q", request_id="11111111", scope="all")
        assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
        assert caught.value.message == "All Agent search branches failed"
        assert not list(tmp_path.glob("*.jsonl"))
    else:
        path = Path(await search.agent_search("q", request_id="11111111", scope="all"))
        assert [json.loads(line)["type"] for line in path.read_text().splitlines()] == types
    assert sorted(calls) == ["paper", "web"]


@pytest.mark.parametrize("limit", [1, 2])
async def test_agent_all_independent_calls_share_the_provider_gate(
    tmp_path: Path, limit: int
) -> None:
    from agent_search_gateway.concurrency import CapacityGate
    from tests.support.agent_search import paper_markdown

    gate = CapacityGate(limit)
    first = asyncio.Event()
    second = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        async with gate.lease():
            branch = "paper" if "## Paper" in messages[0]["content"] else "web"
            calls.append(branch)
            (first if len(calls) == 1 else second).set()
            await release.wait()
            return paper_markdown() if branch == "paper" else web_markdown()

    search = agent_orchestrator(tmp_path, [AgentInvocation("a")], {"a": runner})
    task = asyncio.create_task(search.agent_search("q", request_id="11111111", scope="all"))
    try:
        async with asyncio.timeout(1):
            await first.wait()
            if limit == 2:
                await second.wait()
            else:
                await asyncio.sleep(0)
                assert not second.is_set()
            release.set()
            await task
        assert sorted(calls) == ["paper", "web"]
        assert gate.max_observed_in_use == limit and gate.in_use == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
