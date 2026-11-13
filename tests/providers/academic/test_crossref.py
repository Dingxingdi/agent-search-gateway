import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.crossref import CrossrefProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "crossref"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_crossref_maps_metadata_and_never_invents_missing_date() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await CrossrefProvider(executor).search("metadata")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "10.1000/crossref.example"
    assert hit.doi == "10.1000/crossref.example"
    assert hit.title == "Crossref Example"
    assert hit.authors == ("Alice Example", "Consortium")
    assert hit.abstract == "A structured abstract."
    assert hit.venue == "Example Journal"
    assert hit.published_date == date(2023, 11, 9)
    assert hit.citation_count == 42
    assert hit.url == "https://publisher.example/crossref"
    assert hit.pdf_url == "https://publisher.example/crossref.pdf"
    assert hits[1].published_date is None
    assert hits[1].url == "https://doi.org/10.1000/no.date"

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.crossref.org/works"
    assert request.params == {
        "query": "metadata",
        "rows": 10,
        "sort": "relevance",
        "order": "desc",
    }


def test_crossref_date_defaults_boolean_month_and_day() -> None:
    assert CrossrefProvider._date({"published": {"date-parts": [[2024, True, False]]}}) == date(
        2024,
        1,
        1,
    )


async def test_crossref_optional_contact_adds_mailto_only_when_configured() -> None:
    executor = RecordingJsonExecutor([{"message": {"items": []}}])
    provider = CrossrefProvider(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    await provider.search("query")
    assert executor.requests[0].params == {
        "query": "query",
        "rows": 10,
        "sort": "relevance",
        "order": "desc",
        "mailto": "[REDACTED_SECRET]",
    }


async def test_crossref_invalid_items_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await CrossrefProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("case", ["valid", "mixed", "all_invalid", "empty"])
async def test_paper_response_reports_candidate_summary(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    valid = {"DOI": "10.1000/example", "title": ["Paper"]}
    invalid: list[object] = [None, {}, {"DOI": "10.1000/example"}, {"title": ["Paper"]}]
    cases: dict[str, list[object]] = {
        "valid": [valid],
        "mixed": [valid, *invalid],
        "all_invalid": invalid,
        "empty": [],
    }
    items = cases[case]
    accepted = int(case in ("valid", "mixed"))
    rejected = len(items) - accepted
    payload = {"message": {"items": items}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        hits = await CrossrefProvider(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="crossref")
        ).search("query")
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(hits) == accepted
    if hits:
        assert hits[0].authors == ()
        assert hits[0].abstract == ""
    assert len(summaries) == 1
    assert summaries[0]["provider"] == "crossref"
    assert summaries[0]["stage"] == "paper_search"
    assert summaries[0]["phase"] == "adapter"
    assert summaries[0]["received"] == len(items)
    assert summaries[0]["accepted"] == accepted
    assert summaries[0]["rejected"] == rejected
    expected_reasons = {"invalid_record_shape": 1, "missing_required_field": 3} if rejected else {}
    assert summaries[0]["rejection_counts"] == expected_reasons
    rejects = [
        vars(record)["gateway_fields"]["reason"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "paper_candidate_rejected"
    ]
    assert len(rejects) == rejected
    assert {reason: rejects.count(reason) for reason in set(rejects)} == expected_reasons
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "success"
    assert diagnostics[0]["response_kind"] == "object"
    assert diagnostics[0]["response_bytes"] == len(httpx.Response(200, json=payload).content)


@pytest.mark.parametrize("case", ["success", "unknown", "array", "decode"])
async def test_response_classification_never_logs_paper_payload(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    candidate: dict[str, object] = {"DOI": "10.1000/example", "title": ["PRIVATE_TITLE"]}
    candidate.update(
        abstract="PRIVATE_ABSTRACT",
        error={"message": "PRIVATE_CANDIDATE_ERROR", "code": "PRIVATE_CANDIDATE_CODE"},
    )
    payload: object = {"message": {"items": [candidate]}}
    if case == "unknown":
        payload = {
            "error": {"message": "PRIVATE_NESTED_ERROR", "code": "PRIVATE_NESTED_CODE"},
            "body": "PRIVATE_BODY",
            "unexpected": {"error": "PRIVATE_RECURSIVE_ERROR"},
        }
    elif case == "array":
        payload = [{"message": "PRIVATE_ARRAY_MESSAGE"}]
    response = (
        httpx.Response(
            200, content=b"PRIVATE_INVALID_JSON", headers={"content-type": "application/json"}
        )
        if case == "decode"
        else httpx.Response(200, json=payload)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        provider = CrossrefProvider(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="crossref")
        )
        if case == "success":
            assert len(await provider.search("query")) == 1
        else:
            with pytest.raises(ProtocolFailure):
                await provider.search("query")
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    fields = diagnostics[0]
    assert fields["semantic_result"] == ("success" if case == "success" else "unknown")
    if case != "success":
        assert fields["category"] == ("decode" if case == "decode" else "invalid_envelope")
    assert not any(name.startswith("upstream_") for name in fields)
    assert fields["http_status"] == 200
    assert fields["response_bytes"] == len(response.content)
    assert (
        sum(
            getattr(record, "gateway_event", None) == "http_attempt_completed" for record in records
        )
        == 1
    )
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)
