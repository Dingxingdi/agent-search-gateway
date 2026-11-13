import asyncio
import json
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
from tests.support.logging import structured_test_logger

PRIORITY = ("arxiv", "semantic_scholar", "openalex", "dblp", "crossref", "core")


def _hit(
    source: str,
    source_id: str,
    *,
    title: str = "Paper",
    abstract: str = "",
    doi: str = "",
    url: str | None = None,
    pdf_url: str = "",
) -> PaperSearchHit:
    return PaperSearchHit(
        source=source,
        source_id=source_id,
        title=title,
        authors=("A. Author",),
        abstract=abstract,
        doi=doi,
        url=url or f"https://example.test/{source}/{source_id}",
        pdf_url=pdf_url,
    )


def _orchestrator(
    tmp_path: Path,
    providers: list[object],
    *,
    resolver: object | None = None,
    store: URLStore | None = None,
    logger: object | None = None,
) -> PaperSearchOrchestrator:
    names = [
        provider.name  # type: ignore[attr-defined]
        for provider in providers
    ]
    return PaperSearchOrchestrator(
        providers=providers,  # type: ignore[arg-type]
        quotas=ProviderQuotaManager(
            web_limits={},
            llm_limits={},
            academic_limits={name: 1 for name in names},
        ),
        aggregator=PaperAggregator(PRIORITY),
        resolver=resolver,  # type: ignore[arg-type]
        store=store or URLStore(),
        result_writer=ResultWriter(tmp_path),
        logger=logger,  # type: ignore[arg-type]
    )


async def test_paper_search_rejects_empty_query_and_missing_providers(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path, [])
    with pytest.raises(InputFailure) as empty:
        await orchestrator.paper_search("   ", request_id="11111111")
    assert empty.value.code is ErrorCode.EMPTY_QUERY

    with pytest.raises(ExecutionFailure) as missing:
        await orchestrator.paper_search("query", request_id="22222222")
    assert missing.value.code is ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS
    assert not list(tmp_path.glob("paper-*.jsonl"))


async def test_partial_success_and_completed_empty_are_success_but_all_fail_is_error(
    tmp_path: Path,
) -> None:
    failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "failed")
    good = FakeAcademicSearchProvider("openalex", [_hit("openalex", "W1", title="Good")])
    failed = FakeAcademicSearchProvider("core", failure=failure)
    target = await _orchestrator(tmp_path, [failed, good]).paper_search(
        "query", request_id="11111111"
    )
    assert Path(target).read_text(encoding="utf-8").count("\n") == 1

    empty_dir = tmp_path / "empty"
    empty = FakeAcademicSearchProvider("openalex", [])
    empty_target = await _orchestrator(empty_dir, [failed, empty]).paper_search(
        "query", request_id="22222222"
    )
    assert Path(empty_target).read_text(encoding="utf-8") == ""

    all_failed_dir = tmp_path / "failed"
    with pytest.raises(ExecutionFailure) as caught:
        await _orchestrator(all_failed_dir, [failed]).paper_search("query", request_id="33333333")
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert not list(all_failed_dir.glob("paper-*.jsonl"))


class ControlledProvider:
    def __init__(self, name: str, hit: PaperSearchHit) -> None:
        self.name = name
        self.hit = hit
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, query: str) -> list[PaperSearchHit]:
        self.started.set()
        await self.release.wait()
        return [self.hit]


async def test_providers_run_concurrently_but_output_uses_configured_order(tmp_path: Path) -> None:
    first = ControlledProvider("openalex", _hit("openalex", "W2", title="First"))
    second = ControlledProvider("core", _hit("core", "core-2", title="Second"))
    orchestrator = _orchestrator(tmp_path, [first, second])

    task = asyncio.create_task(orchestrator.paper_search("query", request_id="44444444"))
    await asyncio.wait_for(first.started.wait(), timeout=1)
    await asyncio.wait_for(second.started.wait(), timeout=1)
    second.release.set()
    await asyncio.sleep(0)
    first.release.set()
    target = Path(await task)

    titles = [json.loads(line)["title"] for line in target.read_text().splitlines()]
    assert titles == ["First", "Second"]
    assert orchestrator.quotas.get_academic("openalex").max_observed_in_use == 1
    assert orchestrator.quotas.get_academic("core").max_observed_in_use == 1


async def test_duplicate_hits_merge_then_resolve_doi_once_and_admit_only_landing(
    tmp_path: Path,
) -> None:
    direct_pdf = "https://publisher.example/direct.pdf"
    first = FakeAcademicSearchProvider(
        "openalex",
        [
            _hit(
                "openalex",
                "W3",
                title="Merged",
                abstract="Academic abstract",
                doi="10.1000/shared",
                url="https://publisher.example/landing",
                pdf_url=direct_pdf,
            )
        ],
    )
    second = FakeAcademicSearchProvider(
        "core",
        [_hit("core", "core-3", title="Merged", doi="10.1000/shared")],
    )
    resolver = FakeOAResolver(
        OAResolution(
            landing_url=normalize_url("https://repository.example/landing"),
            pdf_url=normalize_url("https://repository.example/resolved.pdf"),
            is_open_access=True,
            oa_status="green",
            license="cc-by",
        )
    )
    store = URLStore()
    target = Path(
        await _orchestrator(
            tmp_path,
            [first, second],
            resolver=resolver,
            store=store,
        ).paper_search("query", request_id="55555555")
    )
    payloads = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(payloads) == 1
    assert resolver.calls == ["10.1000/shared"]
    assert payloads[0]["pdf_url"] == direct_pdf
    landing = normalize_url("https://publisher.example/landing")
    assert store.get(landing) is not None
    assert store.get(landing).abstract == "Academic abstract"  # type: ignore[union-attr]
    assert store.get(normalize_url(direct_pdf)) is None


async def test_title_fallback_is_used_for_landing_admission_and_logs_are_payload_safe(
    tmp_path: Path,
) -> None:
    logger, stream = structured_test_logger("tests.paper.pipeline")
    title = "TITLE_PAYLOAD_SENTINEL"
    doi = "10.1000/doi-payload-sentinel"
    query = "QUERY_PAYLOAD_SENTINEL"
    provider = FakeAcademicSearchProvider(
        "openalex",
        [_hit("openalex", "W4", title=title, doi=doi, url="https://example.test/paper")],
    )
    store = URLStore()
    await _orchestrator(tmp_path, [provider], store=store, logger=logger).paper_search(
        query, request_id="66666666"
    )
    admitted = store.get(normalize_url("https://example.test/paper"))
    assert admitted is not None and admitted.abstract == title
    logs = stream.getvalue()
    assert "event=provider_started" in logs
    assert "stage=paper_search" in logs
    assert "event=provider_completed" in logs
    assert query not in logs
    assert title not in logs
    assert doi not in logs


async def test_json_adapter_summaries_stay_local_before_paper_aggregation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
    from agent_search_gateway.providers.academic.core import CoreProvider
    from agent_search_gateway.providers.academic.crossref import CrossrefProvider
    from agent_search_gateway.providers.academic.openalex import OpenAlexProvider
    from agent_search_gateway.providers.academic.semantic_scholar import SemanticScholarProvider
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "crossref.test":
            items = [
                {"DOI": "10.1000/shared", "title": ["PRIVATE_CROSSREF_TITLE"]},
                {},
            ]
            payload: object = {"message": {"items": items}}
        elif request.url.host == "openalex.test":
            results = [
                {
                    "id": "W123",
                    "title": "PRIVATE_OPENALEX_TITLE",
                    "doi": "10.1000/shared",
                    "abstract_inverted_index": {"PRIVATE_ABSTRACT": [0]},
                }
            ]
            payload = {"results": results}
        elif request.url.host == "core.test":
            payload = {"results": [None, {}]}
        else:
            payload = {"error": "quota blocked"}
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

        def executor(name: str) -> HttpJsonExecutor:
            return HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name=name)

        providers: list[object] = [
            CrossrefProvider(executor("crossref"), api_url="https://crossref.test"),
            OpenAlexProvider(executor("openalex"), api_url="https://openalex.test"),
            CoreProvider(
                executor("core"),
                api_url="https://core.test",
                api_key=SecretValue("[REDACTED_SECRET]"),
            ),
            SemanticScholarProvider(executor("semantic_scholar"), api_url="https://semantic.test"),
        ]
        orchestrator = _orchestrator(tmp_path, providers)
        target = await orchestrator.paper_search("mixed", request_id="77777777")
        rows = [json.loads(line) for line in Path(target).read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["title"] == "PRIVATE_OPENALEX_TITLE"
        assert rows[0]["identifiers"]["doi"] == "10.1000/shared"
        assert "diagnostics" not in rows[0]

    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    summaries = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 3
    partitions = [
        (fields["provider"], fields["received"], fields["accepted"], fields["rejected"])
        for fields in summaries
    ]
    assert sorted(partitions) == [("core", 2, 0, 2), ("crossref", 2, 1, 1), ("openalex", 1, 1, 0)]
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)
