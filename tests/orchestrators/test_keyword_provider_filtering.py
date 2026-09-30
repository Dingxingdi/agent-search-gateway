import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeKeywordSearchProvider, FakeLLMClient


def _build(
    tmp_path: Path, providers: Sequence[FakeKeywordSearchProvider]
) -> tuple[SearchOrchestrator, URLStore, FakeLLMClient]:
    store = URLStore()
    judge = LLMInvocation("judge_only", "review-model")
    client = FakeLLMClient("judge_only", json_result={"ok": True})
    stages = LLMStages(
        {client.name: client}, judge=judge, safety=judge, content_clean=judge, focus_summary=judge
    )
    orchestrator = SearchOrchestrator(
        keyword_providers=providers,
        llm_invocations=(),
        quotas=ProviderQuotaManager(web_limits={p.name: 1 for p in providers}, llm_limits={}),
        stages=stages,
        store=store,
        result_writer=ResultWriter(tmp_path / "results"),
    )
    return orchestrator, store, client


def _providers() -> tuple[FakeKeywordSearchProvider, ...]:
    return tuple(
        FakeKeywordSearchProvider(
            name, [KeywordSearchHit(f"https://example.com/{name}", snippet=name)]
        )
        for name in ("tavily", "exa", "brave")
    )


async def test_keyword_filter_calls_only_selected_provider(tmp_path: Path) -> None:
    providers = _providers()
    orchestrator, store, _ = _build(tmp_path, providers)
    original = orchestrator._keyword_providers
    path = Path(await orchestrator.keyword_search("query", request_id="11111111", provider="exa"))
    assert [p.calls for p in providers] == [[], ["query"], []]
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"url": "https://example.com/exa", "abstract": "exa"}
    ]
    assert [
        store.get(normalize_url(f"https://example.com/{p.name}")) is not None for p in providers
    ] == [False, True, False]
    assert [orchestrator.quotas.get_web(p.name).max_observed_in_use for p in providers] == [0, 1, 0]
    assert orchestrator._keyword_providers is original


@pytest.mark.parametrize("selector", ["missing", "EXA", " exa ", "", " \t "])
async def test_keyword_bad_selector_has_no_side_effects(tmp_path: Path, selector: str) -> None:
    providers = _providers()
    orchestrator, store, client = _build(tmp_path, providers)
    before = set((tmp_path / "results").glob("*"))
    with pytest.raises(InputFailure) as error:
        await orchestrator.keyword_search("query", request_id="11111111", provider=selector)
    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == (
        "Provider must not be empty"
        if not selector.strip()
        else f"No enabled keyword-search provider matches '{selector}'"
    )
    assert all(not p.calls for p in providers)
    assert all(orchestrator.quotas.get_web(p.name).max_observed_in_use == 0 for p in providers)
    assert all(store.get(normalize_url(f"https://example.com/{p.name}")) is None for p in providers)
    assert not client.json_calls
    assert set((tmp_path / "results").glob("*")) == before


@pytest.mark.parametrize("selector", [None, "missing", " "])
async def test_keyword_empty_runtime_precedes_selection(
    tmp_path: Path, selector: str | None
) -> None:
    orchestrator, _, _ = _build(tmp_path, ())
    with pytest.raises(ExecutionFailure) as error:
        await orchestrator.keyword_search("query", request_id="11111111", provider=selector)
    assert error.value.code is ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS
    assert error.value.message == "No keyword search providers are enabled"
    with pytest.raises(InputFailure) as empty:
        await orchestrator.keyword_search(" ", request_id="11111111", provider=selector)
    assert empty.value.code is ErrorCode.EMPTY_QUERY


async def test_keyword_selected_failure_does_not_fall_back(tmp_path: Path) -> None:
    providers = _providers()
    providers[1].failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "selected failed")
    orchestrator, _, _ = _build(tmp_path, providers)
    with pytest.raises(ExecutionFailure) as error:
        await orchestrator.keyword_search("query", request_id="11111111", provider="exa")
    assert error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert error.value.message == "All keyword search provider pipelines failed"
    assert [p.calls for p in providers] == [[], ["query"], []]
    assert not list((tmp_path / "results").glob("*"))


async def test_keyword_selected_empty_success_writes_empty_file(tmp_path: Path) -> None:
    providers = _providers()
    providers[1].result = []
    orchestrator, _, _ = _build(tmp_path, providers)
    path = Path(await orchestrator.keyword_search("query", request_id="11111111", provider="exa"))
    assert path.read_text() == ""
    assert [p.calls for p in providers] == [[], ["query"], []]


async def test_keyword_next_request_without_filter_runs_all_providers(tmp_path: Path) -> None:
    providers = _providers()
    orchestrator, _, _ = _build(tmp_path, providers)
    original = orchestrator._keyword_providers
    await orchestrator.keyword_search("first", request_id="11111111", provider="exa")
    path = Path(await orchestrator.keyword_search("second", request_id="22222222"))
    assert [p.calls for p in providers] == [["second"], ["first", "second"], ["second"]]
    assert [json.loads(line)["url"] for line in path.read_text().splitlines()] == [
        f"https://example.com/{p.name}" for p in providers
    ]
    assert orchestrator._keyword_providers is original


async def test_keyword_filter_does_not_disable_other_alias_judge(tmp_path: Path) -> None:
    providers = _providers()
    body = "Useful page content with substantive information. " * 100
    providers[1].result = [
        KeywordSearchHit("https://example.com/exa", snippet="exa", raw_content=body)
    ]
    orchestrator, store, client = _build(tmp_path, providers)
    await orchestrator.keyword_search("query", request_id="11111111", provider="exa")
    assert [p.calls for p in providers] == [[], ["query"], []]
    assert len(client.json_calls) == 1
    assert client.json_calls[0][0].provider == "judge_only"
    record = store.get(normalize_url("https://example.com/exa"))
    assert record is not None and record.raw_content == body
