import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore

PAPER_BLOCK = """## Paper
Title: Shared Paper
Authors: Alice Example
Abstract: Paper abstract
DOI: 10.1000/shared
arXiv:
Published: 2024-01-02
Updated:
URL: https://example.com/paper
PDF:
Venue: ExampleConf
Topics: AI
Citations: 7
Open Access: unknown
OA Status:
License:"""
WEB_BLOCK = "## Result\nURL: https://example.com/web\nAbstract: Web abstract"
EMPTY_WEB_BLOCK = "## Result\nURL: https://example.com/empty\nAbstract:"


class ScopedClient:
    def __init__(
        self,
        name: str,
        *,
        web: str = WEB_BLOCK,
        paper: str = PAPER_BLOCK,
        fail_web: bool = False,
        fail_paper: bool = False,
        synchronize_branches: bool = False,
    ) -> None:
        self.name = name
        self.web = web
        self.paper = paper
        self.fail_web = fail_web
        self.fail_paper = fail_paper
        self.web_started = asyncio.Event()
        self.paper_started = asyncio.Event()
        self.synchronize_branches = synchronize_branches
        self.calls: list[str] = []

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        system = messages[0]["content"]
        is_paper = "## Paper" in system
        scope = "paper" if is_paper else "web"
        self.calls.append(scope)
        started = self.paper_started if is_paper else self.web_started
        peer = self.web_started if is_paper else self.paper_started
        started.set()
        if self.synchronize_branches:
            await peer.wait()
        if (is_paper and self.fail_paper) or (not is_paper and self.fail_web):
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "branch failed")
        return self.paper if is_paper else self.web

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        return {"ok": True}

    async def aclose(self) -> None:
        return None


def make_orchestrator(
    tmp_path: Path,
    clients: dict[str, ScopedClient],
) -> SearchOrchestrator:
    invocations = tuple(LLMInvocation(name, f"{name}-model", {}) for name in clients)
    fallback = invocations[0]
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
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path),
        paper_aggregator=PaperAggregator(tuple(f"llm:{name}" for name in clients)),
        paper_resolver=None,
    )


async def test_default_web_scope_is_byte_compatible_with_explicit_web(tmp_path: Path) -> None:
    client = ScopedClient("one")
    orchestrator = make_orchestrator(tmp_path, {"one": client})
    default_path = Path(await orchestrator.llm_search("prompt", request_id="11111111"))
    explicit_path = Path(
        await orchestrator.llm_search("prompt", request_id="22222222", scope="web")
    )
    assert default_path.read_bytes() == explicit_path.read_bytes()
    assert default_path.read_bytes() == (
        b'{"url":"https://example.com/web","abstract":"Web abstract"}\n'
    )
    assert client.calls == ["web", "web"]


async def test_paper_scope_aggregates_invocations_isolates_parser_failure_and_has_no_type(
    tmp_path: Path,
) -> None:
    good = ScopedClient("good")
    duplicate = ScopedClient("duplicate")
    malformed = ScopedClient("malformed", paper="not paper grammar")
    orchestrator = make_orchestrator(
        tmp_path,
        {"good": good, "malformed": malformed, "duplicate": duplicate},
    )

    path = Path(await orchestrator.llm_search("prompt", request_id="33333333", scope="paper"))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["title"] == "Shared Paper"
    assert "type" not in lines[0]
    assert lines[0]["sources"] == ["llm:good", "llm:duplicate"]

    all_bad = make_orchestrator(
        tmp_path / "all-bad",
        {"bad": ScopedClient("bad", paper="malformed")},
    )
    with pytest.raises(ExecutionFailure) as caught:
        await all_bad.llm_search("prompt", request_id="44444444", scope="paper")
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert not list((tmp_path / "all-bad").glob("llm-*.jsonl"))


async def test_all_scope_schedules_branches_concurrently_and_writes_web_then_paper(
    tmp_path: Path,
) -> None:
    client = ScopedClient("one", synchronize_branches=True)
    orchestrator = make_orchestrator(tmp_path, {"one": client})
    path = Path(
        await asyncio.wait_for(
            orchestrator.llm_search("prompt", request_id="55555555", scope="all"),
            timeout=1,
        )
    )
    payloads = [json.loads(line) for line in path.read_text().splitlines()]
    assert [payload["type"] for payload in payloads] == ["web", "paper"]
    assert client.web_started.is_set() and client.paper_started.is_set()


@pytest.mark.parametrize(
    ("web_ok", "paper_ok", "expected_types"),
    [
        (True, True, ["web", "paper"]),
        (True, False, ["web"]),
        (False, True, ["paper"]),
    ],
)
async def test_all_scope_partial_success_matrix(
    tmp_path: Path,
    web_ok: bool,
    paper_ok: bool,
    expected_types: list[str],
) -> None:
    client = ScopedClient("one", fail_web=not web_ok, fail_paper=not paper_ok)
    path = Path(
        await make_orchestrator(tmp_path, {"one": client}).llm_search(
            "prompt", request_id="66666666", scope="all"
        )
    )
    payloads = [json.loads(line) for line in path.read_text().splitlines()]
    assert [payload["type"] for payload in payloads] == expected_types


async def test_all_scope_counts_successful_empty_branch_and_both_fail_has_no_file(
    tmp_path: Path,
) -> None:
    empty_web = ScopedClient("one", web=EMPTY_WEB_BLOCK, fail_paper=True)
    path = Path(
        await make_orchestrator(tmp_path / "empty", {"one": empty_web}).llm_search(
            "prompt", request_id="77777777", scope="all"
        )
    )
    assert path.read_text() == ""

    failed = ScopedClient("one", fail_web=True, fail_paper=True)
    root = tmp_path / "failed"
    with pytest.raises(ExecutionFailure) as caught:
        await make_orchestrator(root, {"one": failed}).llm_search(
            "prompt", request_id="88888888", scope="all"
        )
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert not list(root.glob("llm-*.jsonl"))
