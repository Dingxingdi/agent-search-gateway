import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.models import OAResolution
from agent_search_gateway.orchestrators.paper import PaperSearchOrchestrator
from agent_search_gateway.providers.contracts import PaperSearchHit
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeAcademicSearchProvider, FakeOAResolver


def _providers() -> tuple[FakeAcademicSearchProvider, ...]:
    return tuple(
        FakeAcademicSearchProvider(
            name,
            [
                PaperSearchHit(
                    source=name,
                    source_id={
                        "arxiv": "2401.00001",
                        "openalex": "W1",
                        "crossref": "10.1000/crossref",
                    }[name],
                    title=name,
                    authors=("Author",),
                    abstract=f"About {name}",
                    doi=f"10.1000/{name}",
                    url=f"https://example.com/{name}",
                )
            ],
        )
        for name in ("arxiv", "openalex", "crossref")
    )


def _build(
    tmp_path: Path, providers: Sequence[FakeAcademicSearchProvider]
) -> tuple[PaperSearchOrchestrator, FakeOAResolver]:
    resolver = FakeOAResolver()
    orchestrator = PaperSearchOrchestrator(
        providers=providers,
        quotas=ProviderQuotaManager(
            web_limits={}, llm_limits={}, academic_limits={p.name: 1 for p in providers}
        ),
        aggregator=PaperAggregator(tuple(p.name for p in providers)),
        resolver=resolver,
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
    )
    return orchestrator, resolver


async def test_paper_filter_calls_only_selected_discovery_provider(tmp_path: Path) -> None:
    providers = _providers()
    orchestrator, resolver = _build(tmp_path, providers)
    original = orchestrator.providers
    path = Path(
        await orchestrator.paper_search("query", request_id="11111111", provider="openalex")
    )
    assert [p.calls for p in providers] == [[], ["query"], []]
    [record] = [json.loads(line) for line in path.read_text().splitlines()]
    assert record["title"] == "openalex"
    assert record["sources"] == ["openalex"]
    assert resolver.calls == ["10.1000/openalex"]
    assert [
        orchestrator.store.get(normalize_url(f"https://example.com/{p.name}")) is not None
        for p in providers
    ] == [False, True, False]
    assert [orchestrator.quotas.get_academic(p.name).max_observed_in_use for p in providers] == [
        0,
        1,
        0,
    ]
    assert orchestrator.providers is original


@pytest.mark.parametrize("selector", ["missing", "OPENALEX", " openalex ", "", " \t ", "unpaywall"])
async def test_paper_invalid_selection_does_not_resolve_or_admit(
    tmp_path: Path, selector: str
) -> None:
    providers = _providers()
    orchestrator, resolver = _build(tmp_path, providers)
    before = set((tmp_path / "results").glob("*"))
    with pytest.raises(InputFailure) as error:
        await orchestrator.paper_search("query", request_id="11111111", provider=selector)
    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == (
        "Provider must not be empty"
        if not selector.strip()
        else f"No enabled paper-search provider matches '{selector}'"
    )
    assert all(not p.calls for p in providers)
    assert resolver.calls == []
    assert all(orchestrator.quotas.get_academic(p.name).max_observed_in_use == 0 for p in providers)
    assert all(
        orchestrator.store.get(normalize_url(f"https://example.com/{p.name}")) is None
        for p in providers
    )
    assert set((tmp_path / "results").glob("*")) == before


@pytest.mark.parametrize("selector", [None, "missing", " "])
async def test_paper_empty_runtime_precedes_selection(tmp_path: Path, selector: str | None) -> None:
    orchestrator, _ = _build(tmp_path, ())
    with pytest.raises(ExecutionFailure) as error:
        await orchestrator.paper_search("query", request_id="11111111", provider=selector)
    assert error.value.code is ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS
    assert error.value.message == "No academic search providers are enabled"
    with pytest.raises(InputFailure) as empty:
        await orchestrator.paper_search(" ", request_id="11111111", provider=selector)
    assert empty.value.code is ErrorCode.EMPTY_QUERY


async def test_paper_selected_failure_does_not_fall_back(tmp_path: Path) -> None:
    providers = _providers()
    providers[1].failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "selected failed")
    orchestrator, resolver = _build(tmp_path, providers)
    with pytest.raises(ExecutionFailure) as error:
        await orchestrator.paper_search("query", request_id="11111111", provider="openalex")
    assert error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert error.value.message == "All academic search provider pipelines failed"
    assert [p.calls for p in providers] == [[], ["query"], []]
    assert not resolver.calls
    assert not list((tmp_path / "results").glob("*"))


async def test_paper_empty_selected_success(tmp_path: Path) -> None:
    providers = _providers()
    providers[1].result = []
    orchestrator, resolver = _build(tmp_path, providers)
    path = Path(
        await orchestrator.paper_search("query", request_id="11111111", provider="openalex")
    )
    assert path.read_text() == ""
    assert [p.calls for p in providers] == [[], ["query"], []]
    assert resolver.calls == []


async def test_paper_filter_is_request_local(tmp_path: Path) -> None:
    providers = _providers()
    orchestrator, _ = _build(tmp_path, providers)
    original = orchestrator.providers
    await orchestrator.paper_search("first", request_id="11111111", provider="openalex")
    path = Path(await orchestrator.paper_search("second", request_id="22222222"))
    assert [p.calls for p in providers] == [["second"], ["first", "second"], ["second"]]
    assert [json.loads(line)["title"] for line in path.read_text().splitlines()] == [
        p.name for p in providers
    ]
    assert orchestrator.providers is original


async def test_paper_valid_selection_keeps_oa_enrichment(tmp_path: Path) -> None:
    providers = _providers()
    orchestrator, resolver = _build(tmp_path, providers)
    resolver.result = OAResolution(
        landing_url=None,
        pdf_url=normalize_url("https://example.com/open.pdf"),
        is_open_access=True,
        oa_status="green",
    )
    path = Path(
        await orchestrator.paper_search("query", request_id="11111111", provider="openalex")
    )
    [record] = [json.loads(line) for line in path.read_text().splitlines()]
    assert resolver.calls == ["10.1000/openalex"]
    assert record["is_open_access"] is True
    assert record["pdf_url"] == "https://example.com/open.pdf"
    assert record["sources"] == ["openalex"]
    assert orchestrator.store.get(normalize_url(record["url"])) is not None
    assert orchestrator.store.get(normalize_url(record["pdf_url"])) is None
