import asyncio
import logging

import pytest

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.academic.enrichment import enrich_paper_records
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import OAResolution, PaperIdentifiers, PaperRecord
from agent_search_gateway.providers.contracts import PaperSearchHit
from agent_search_gateway.url_normalization import normalize_url


class RecordingResolver:
    name = "unpaywall"

    def __init__(
        self,
        response: OAResolution | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[str] = []

    async def resolve(self, doi: str) -> OAResolution | None:
        self.calls.append(doi)
        if self.failure is not None:
            raise self.failure
        return self.response


def _record(*, doi: str = "10.1000/example", pdf: str | None = None) -> PaperRecord:
    return PaperRecord(
        title="Paper",
        authors=("Author",),
        abstract="Abstract",
        identifiers=PaperIdentifiers(doi=doi),
        published_date=None,
        updated_date=None,
        url=normalize_url("https://publisher.example/paper"),
        pdf_url=normalize_url(pdf) if pdf is not None else None,
        venue="",
        topics=(),
        citation_counts={},
        is_open_access=None,
        oa_status="",
        license="",
        sources=("openalex",),
    )


async def test_enrichment_runs_once_after_six_source_hits_deduplicate() -> None:
    providers = ("arxiv", "semantic_scholar", "openalex", "dblp", "crossref", "core")
    hits = [
        PaperSearchHit(
            source=source,
            source_id=(
                "2401.12345"
                if source == "arxiv"
                else "10.1000/shared"
                if source == "crossref"
                else f"id-{index}"
            ),
            title="Shared Paper",
            authors=("Author",),
            doi="10.1000/shared",
            arxiv_id="2401.12345" if source == "arxiv" else "",
            url=f"https://example.test/{source}",
        )
        for index, source in enumerate(providers)
    ]
    records = PaperAggregator(providers).aggregate(hits)
    resolver = RecordingResolver(
        OAResolution(
            landing_url=normalize_url("https://repository.example/paper"),
            pdf_url=normalize_url("https://repository.example/paper.pdf"),
            is_open_access=True,
            oa_status="green",
            license="cc-by",
        )
    )

    enriched = await enrich_paper_records(records, resolver)

    assert len(records) == 1
    assert resolver.calls == ["10.1000/shared"]
    assert str(enriched[0].pdf_url) == "https://repository.example/paper.pdf"
    assert enriched[0].is_open_access is True
    assert enriched[0].oa_status == "green"
    assert enriched[0].license == "cc-by"


async def test_enrichment_skips_doi_less_and_keeps_stronger_direct_pdf() -> None:
    resolver = RecordingResolver(
        OAResolution(
            landing_url=normalize_url("https://repository.example/paper"),
            pdf_url=normalize_url("https://repository.example/weaker.pdf"),
            is_open_access=True,
            oa_status="green",
            license="cc-by",
        )
    )
    direct = _record(pdf="https://publisher.example/direct.pdf")
    doi_less = _record(doi="")

    enriched = await enrich_paper_records([direct, doi_less], resolver)

    assert resolver.calls == ["10.1000/example"]
    assert str(enriched[0].pdf_url) == "https://publisher.example/direct.pdf"
    assert enriched[1] == doi_less


async def test_enrichment_failure_logs_safe_event_and_retains_record(caplog: object) -> None:
    resolver = RecordingResolver(
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "resolver failed")
    )
    original = _record()
    with caplog.at_level(logging.DEBUG):  # type: ignore[attr-defined]
        enriched = await enrich_paper_records([original], resolver)
    assert enriched == [original]
    events = [getattr(record, "gateway_event", "") for record in caplog.records]  # type: ignore[attr-defined]
    assert "paper_enrichment_failed" in events
    rendered_fields = repr(
        [getattr(record, "gateway_fields", {}) for record in caplog.records]  # type: ignore[attr-defined]
    )
    assert "10.1000/example" not in rendered_fields


async def test_enrichment_unexpected_resolver_failure_retains_record(caplog: object) -> None:
    resolver = RecordingResolver(failure=RuntimeError("unexpected resolver failure"))
    original = _record()
    with caplog.at_level(logging.DEBUG):  # type: ignore[attr-defined]
        enriched = await enrich_paper_records([original], resolver)
    assert enriched == [original]
    events = [getattr(record, "gateway_event", "") for record in caplog.records]  # type: ignore[attr-defined]
    assert "paper_enrichment_failed" in events


async def test_enrichment_preserves_cancellation(caplog: pytest.LogCaptureFixture) -> None:
    resolver = RecordingResolver(failure=asyncio.CancelledError())
    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await enrich_paper_records([_record()], resolver)
    events = [
        (vars(record)["gateway_event"], vars(record)["gateway_fields"])
        for record in caplog.records
        if getattr(record, "gateway_event", "")
    ]
    assert [event for event, _ in events] == ["provider_started", "provider_failed"]
    assert events[1][1]["category"] == "cancelled"
    assert events[1][1]["reason"] == "cancelled"


@pytest.mark.parametrize("case", ["no_match", "resolved", "outage", "bad_envelope"])
async def test_oa_no_match_and_outage_have_distinct_terminal_diagnostics(
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
    from agent_search_gateway.providers.academic.unpaywall import UnpaywallResolver
    from agent_search_gateway.providers.http import HttpJsonExecutor

    response = (
        httpx.Response(404, json={"error": "PRIVATE_NO_MATCH"})
        if case == "no_match"
        else httpx.Response(
            200,
            json={
                "is_oa": True,
                "oa_status": "gold",
                "best_oa_location": {"url_for_pdf": "https://repository.test/paper.pdf"},
                "oa_locations": [],
            },
        )
    )
    if case == "outage":
        response = httpx.Response(500, json={"error": "PRIVATE_OUTAGE"})
    if case == "bad_envelope":
        response = httpx.Response(200, json={"error": "PRIVATE_BAD_ENVELOPE"})
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    original = _record()
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            resolver = UnpaywallResolver(
                HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="unpaywall"),
                contact_email=SecretValue("PRIVATE_EMAIL@example.test"),
            )
            enriched = await enrich_paper_records([original, original], resolver)

    assert len(enriched) == 2
    if case != "resolved":
        assert enriched == [original, original]
    else:
        assert all(
            str(record.pdf_url) == "https://repository.test/paper.pdf" for record in enriched
        )
    terminals = [
        (vars(record)["gateway_event"], vars(record)["gateway_fields"])
        for record in caplog.records
        if getattr(record, "gateway_event", "") in {"provider_completed", "provider_failed"}
    ]
    assert len(terminals) == 1
    assert terminals[0][0] == (
        "provider_failed" if case in {"outage", "bad_envelope"} else "provider_completed"
    )
    if case not in {"outage", "bad_envelope"}:
        assert terminals[0][1]["outcome"] == case
    else:
        assert terminals[0][1]["category"] == (
            "http_status" if case == "outage" else "invalid_envelope"
        )
    assert calls == (2 if case == "outage" else 1)
    assert (
        sum(getattr(record, "gateway_event", "") == "provider_started" for record in caplog.records)
        == 1
    )
    assert sum(
        getattr(record, "gateway_event", "") == "paper_enrichment_failed"
        for record in caplog.records
    ) == (1 if case in {"outage", "bad_envelope"} else 0)
    assert terminals[0][1]["provider"] == "unpaywall"
    assert terminals[0][1]["stage"] == "oa_resolve"
    gateway_records = [record for record in caplog.records if getattr(record, "gateway_event", "")]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in gateway_records
        if vars(record)["gateway_event"] == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == calls
    assert all(fields["response_bytes"] == len(response.content) for fields in diagnostics)
    assert (
        sum(vars(record)["gateway_event"] == "http_attempt_completed" for record in gateway_records)
        == calls
    )
    assert (
        diagnostics[0]["semantic_result"]
        == {
            "no_match": "no_match",
            "resolved": "success",
            "outage": "error",
            "bad_envelope": "unknown",
        }[case]
    )
    formatter = KeyValueFormatter(SecretRedactor([]))
    rendered = "\n".join(formatter.format(record) for record in gateway_records)
    assert "PRIVATE_" not in rendered
    assert "email=" not in rendered
