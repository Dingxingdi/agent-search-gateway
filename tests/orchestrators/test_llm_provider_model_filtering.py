import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, LLMSearchScope
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.orchestrators.test_llm_invocation_selection import MISMATCHES, _invocations
from tests.orchestrators.test_scoped_llm_search import PAPER_BLOCK
from tests.support.fakes import FakeLLMClient, FakeOAResolver
from tests.support.logging import structured_test_logger


class RecordingClient(FakeLLMClient):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.scoped_calls: list[tuple[str, LLMInvocation, tuple[ChatMessage, ...]]] = []
        self.fail_models: set[str] = set()
        self.fail_scopes: set[str] = set()
        self.empty = False
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.cancelled = 0

    async def complete_text(
        self, invocation: LLMInvocation, messages: Sequence[ChatMessage]
    ) -> str:
        scope = "paper" if "## Paper" in messages[0]["content"] else "web"
        self.text_calls.append((invocation, tuple(messages)))
        self.scoped_calls.append((scope, invocation, tuple(messages)))
        self.started.set()
        if self.release is not None:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if invocation.model in self.fail_models or scope in self.fail_scopes:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "selected invocation failed")
        identity = f"{invocation.provider}/{invocation.model}"
        if scope == "web":
            abstract = "" if self.empty else identity
            return f"## Result\nURL: https://example.com/web/{identity}\nAbstract: {abstract}"
        paper = (
            PAPER_BLOCK.replace("Shared Paper", identity)
            .replace("10.1000/shared", f"10.1000/{identity}")
            .replace("https://example.com/paper", f"https://example.com/paper/{identity}")
        )
        # Valid grammar can complete successfully while aggregation rejects every identifier.
        return paper.replace(f"DOI: 10.1000/{identity}", "DOI: invalid") if self.empty else paper


def _build(
    tmp_path: Path, invocations: tuple[LLMInvocation, ...] | None = None
) -> tuple[SearchOrchestrator, dict[str, RecordingClient], FakeOAResolver]:
    selected_invocations = _invocations() if invocations is None else invocations
    clients = {
        name: RecordingClient(name)
        for name in ("openai_main", "deepseek_main", "azure_main", "fetch_only")
    }
    fallback = LLMInvocation("fetch_only", "global-model")
    stages = LLMStages(
        clients, judge=fallback, safety=fallback, content_clean=fallback, focus_summary=fallback
    )
    resolver = FakeOAResolver()
    orchestrator = SearchOrchestrator(
        keyword_providers=(),
        llm_invocations=selected_invocations,
        quotas=ProviderQuotaManager(web_limits={}, llm_limits={name: 2 for name in clients}),
        stages=stages,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
        paper_resolver=resolver,
    )
    return orchestrator, clients, resolver


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
@pytest.mark.parametrize(
    ("provider", "model", "indexes"),
    [
        (None, None, [0, 1, 2, 3]),
        ("openai_main", None, [0, 1]),
        (None, "gpt-5", [0, 3]),
        ("openai_main", "gpt-5", [0]),
    ],
)
async def test_llm_all_scopes_use_only_selected_invocations(
    tmp_path: Path,
    scope: LLMSearchScope,
    provider: str | None,
    model: str | None,
    indexes: list[int],
) -> None:
    invocations = _invocations()
    orchestrator, clients, resolver = _build(tmp_path, invocations)
    path = Path(
        await orchestrator.llm_search(
            "user content", request_id="11111111", scope=scope, provider=provider, model=model
        )
    )
    selected = [invocations[index] for index in indexes]
    branches = ["web", "paper"] if scope == "all" else [scope]
    for name, client in clients.items():
        expected = [
            (branch, item) for branch in branches for item in selected if item.provider == name
        ]
        assert [(branch, item) for branch, item, _ in client.scoped_calls] == expected
        for _, item, messages in client.scoped_calls:
            assert any(item is original for original in selected)
            assert messages[-1]["content"] == "user content"
            assert item.provider not in str(messages) and item.model not in str(messages)
    payloads = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(payloads) == len(selected) * len(branches)
    expected_urls = [
        f"https://example.com/{branch}/{item.provider}/{item.model}"
        for branch in branches
        for item in selected
    ]
    assert [record["url"] for record in payloads] == expected_urls
    if scope == "all":
        assert [record["type"] for record in payloads] == [
            branch for branch in branches for _ in selected
        ]
    else:
        assert all("type" not in record for record in payloads)
    assert len(resolver.calls) == (len(selected) if "paper" in branches else 0)
    for branch in ("web", "paper"):
        for item in invocations:
            admitted = orchestrator._store.get(
                normalize_url(f"https://example.com/{branch}/{item.provider}/{item.model}")
            )
            assert (admitted is not None) is (branch in branches and item in selected)
    assert orchestrator._llm_invocations is invocations


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        *MISMATCHES,
        ("fetch_only", None, "No LLM search invocation matches provider 'fetch_only'"),
        (None, "global-model", "No LLM search invocation matches model 'global-model'"),
        ("openai", None, "No LLM search invocation matches provider 'openai'"),
        ("", None, "Provider must not be empty"),
        (None, " \t ", "Model must not be empty"),
    ],
)
async def test_llm_invalid_selection_never_starts_work(
    tmp_path: Path, scope: LLMSearchScope, provider: str | None, model: str | None, message: str
) -> None:
    orchestrator, clients, resolver = _build(tmp_path)
    with pytest.raises(InputFailure) as error:
        await orchestrator.llm_search(
            "prompt", request_id="11111111", scope=scope, provider=provider, model=model
        )
    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == message
    assert all(not client.text_calls and not client.json_calls for client in clients.values())
    assert not resolver.calls
    assert not list((tmp_path / "results").glob("*"))
    assert all(
        orchestrator._store.get(
            normalize_url(f"https://example.com/{branch}/{item.provider}/{item.model}")
        )
        is None
        for branch in ("web", "paper")
        for item in _invocations()
    )


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_llm_empty_runtime_and_empty_prompt_priority(
    tmp_path: Path, scope: LLMSearchScope
) -> None:
    orchestrator, _, _ = _build(tmp_path, ())
    with pytest.raises(ExecutionFailure) as error:
        await orchestrator.llm_search(
            "prompt", request_id="11111111", scope=scope, provider="", model=""
        )
    assert error.value.code is ErrorCode.NO_LLM_SEARCH_PROVIDERS
    with pytest.raises(InputFailure) as empty:
        await orchestrator.llm_search(" ", request_id="11111111", scope=scope, provider="missing")
    assert empty.value.code is ErrorCode.EMPTY_QUERY


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ("web", "All LLM web search provider pipelines failed"),
        ("paper", "All LLM paper search provider pipelines failed"),
        ("all", "All LLM search branches failed"),
    ],
)
async def test_llm_selected_failure_does_not_fall_back(
    tmp_path: Path, scope: LLMSearchScope, message: str
) -> None:
    orchestrator, clients, resolver = _build(tmp_path)
    clients["openai_main"].fail_models.add("gpt-5")
    with pytest.raises(ExecutionFailure) as error:
        await orchestrator.llm_search(
            "prompt", request_id="11111111", scope=scope, provider="openai_main", model="gpt-5"
        )
    assert error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert error.value.message == message
    assert len(clients["openai_main"].text_calls) == (2 if scope == "all" else 1)
    assert all(not client.text_calls for name, client in clients.items() if name != "openai_main")
    assert not resolver.calls
    assert not list((tmp_path / "results").glob("*"))


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_llm_partial_selected_success_and_duplicate_execution(
    tmp_path: Path, scope: LLMSearchScope
) -> None:
    first = _invocations()[0]
    duplicate = LLMInvocation(first.provider, first.model, {"temperature": 0.9})
    invocations = (*_invocations(), duplicate)
    orchestrator, clients, _ = _build(tmp_path, invocations)
    clients["openai_main"].fail_models.add("gpt-5-mini")
    path = Path(
        await orchestrator.llm_search(
            "prompt", request_id="11111111", scope=scope, provider="openai_main"
        )
    )
    assert len(path.read_text().splitlines()) == (2 if scope == "all" else 1)
    calls = clients["openai_main"].text_calls
    assert len(calls) == (6 if scope == "all" else 3)
    assert sum(item is duplicate for item, _ in calls) == (2 if scope == "all" else 1)
    assert duplicate.extra_body == {"temperature": 0.9}
    assert all(not client.text_calls for name, client in clients.items() if name != "openai_main")


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_llm_selected_empty_success_still_writes_file(
    tmp_path: Path, scope: LLMSearchScope
) -> None:
    orchestrator, clients, _ = _build(tmp_path)
    clients["openai_main"].empty = True
    path = Path(
        await orchestrator.llm_search(
            "prompt", request_id="11111111", scope=scope, provider="openai_main"
        )
    )
    assert path.read_text() == ""
    assert all(not client.text_calls for name, client in clients.items() if name != "openai_main")


@pytest.mark.parametrize("failed_scope", ["web", "paper"])
async def test_llm_all_keeps_selected_other_branch_success(
    tmp_path: Path, failed_scope: str
) -> None:
    orchestrator, clients, _ = _build(tmp_path)
    clients["openai_main"].fail_scopes.add(failed_scope)
    path = Path(
        await orchestrator.llm_search(
            "prompt", request_id="11111111", scope="all", provider="openai_main", model="gpt-5"
        )
    )
    [record] = [json.loads(line) for line in path.read_text().splitlines()]
    assert record["type"] == ("paper" if failed_scope == "web" else "web")
    assert all(not client.text_calls for name, client in clients.items() if name != "openai_main")


async def test_llm_concurrent_selectors_and_next_default_request_are_isolated(
    tmp_path: Path,
) -> None:
    orchestrator, clients, _ = _build(tmp_path)
    original = orchestrator._llm_invocations
    release = asyncio.Event()
    clients["openai_main"].release = release
    clients["azure_main"].release = release
    first = asyncio.create_task(
        orchestrator.llm_search(
            "first", request_id="11111111", scope="all", provider="openai_main", model="gpt-5"
        )
    )
    second = asyncio.create_task(
        orchestrator.llm_search("second", request_id="22222222", scope="all", provider="azure_main")
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(
                clients["openai_main"].started.wait(), clients["azure_main"].started.wait()
            ),
            timeout=1,
        )
        release.set()
        paths = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    finally:
        release.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    for path, name in zip(paths, ("openai_main", "azure_main"), strict=True):
        records = [json.loads(line) for line in Path(path).read_text().splitlines()]
        assert len(records) == 2 and all(name in record["url"] for record in records)
    for name, client in clients.items():
        assert all(
            messages[-1]["content"] == ("first" if name == "openai_main" else "second")
            for _, messages in client.text_calls
        )
        client.text_calls.clear()
    await orchestrator.llm_search("unfiltered", request_id="33333333")
    assert [len(clients[name].text_calls) for name in clients] == [2, 1, 1, 0]
    assert orchestrator._llm_invocations is original


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_llm_filtered_request_cancellation_propagates(
    tmp_path: Path, scope: LLMSearchScope
) -> None:
    orchestrator, clients, _ = _build(tmp_path)
    selected = clients["openai_main"]
    selected.release = asyncio.Event()
    task = asyncio.create_task(
        orchestrator.llm_search(
            "prompt", request_id="11111111", scope=scope, provider="openai_main", model="gpt-5"
        )
    )
    try:
        await asyncio.wait_for(selected.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert selected.cancelled == (2 if scope == "all" else 1)
    assert all(not client.text_calls for name, client in clients.items() if name != "openai_main")
    assert not list((tmp_path / "results").glob("*"))


async def test_llm_debug_events_name_only_selected_search_invocation(tmp_path: Path) -> None:
    logger, stream = structured_test_logger("filtering-test")
    orchestrator, _, _ = _build(tmp_path)
    orchestrator._logger = logger
    await orchestrator.llm_search(
        "prompt", request_id="11111111", scope="all", provider="openai_main", model="gpt-5"
    )
    started = [line for line in stream.getvalue().splitlines() if "event=provider_started" in line]
    assert len(started) == 2
    assert all("provider=openai_main" in line and "model=gpt-5" in line for line in started)
    assert not any(
        "azure_main" in line or "deepseek_main" in line or "gpt-5-mini" in line for line in started
    )
