import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.openalex import (
    OpenAlexProvider,
    reconstruct_abstract,
)
from tests.support.academic_diagnostics import (
    academic_response,
    assert_json_candidates,
    assert_private_payload_absent,
    assert_response_classification,
    candidate_case,
    private_json_response,
)
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "openalex"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_reconstruct_abstract_sorts_positions_and_accepts_empty_index() -> None:
    assert reconstruct_abstract({"world": [1], "Hello": [0], "again": [2]}) == "Hello world again"
    assert reconstruct_abstract({}) == ""
    assert reconstruct_abstract(None) == ""


async def test_openalex_maps_works_and_falls_back_to_canonical_landing() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await OpenAlexProvider(executor).search("open science")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "W2741809807"
    assert hit.doi == "https://doi.org/10.1000/OPENALEX.EXAMPLE"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Hello world again"
    assert hit.published_date == date(2024, 3, 4)
    assert hit.url == "https://publisher.example/paper"
    assert hit.pdf_url == "https://publisher.example/paper.pdf"
    assert hit.citation_count == 77
    assert hit.is_open_access is True
    assert hit.oa_status == "gold"
    assert hit.license == "cc-by"
    assert hit.topics == ("Machine Learning", "Artificial Intelligence")
    assert hits[1].url == "https://openalex.org/W999"
    assert hits[1].is_open_access is False

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.openalex.org/works"
    assert request.params == {"search": "open science", "per_page": 10}


async def test_openalex_optional_contact_is_request_identity_only() -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    provider = OpenAlexProvider(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    await provider.search("query")
    assert executor.requests[0].params == {
        "search": "query",
        "per_page": 10,
        "mailto": "[REDACTED_SECRET]",
    }


async def test_openalex_invalid_results_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await OpenAlexProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("case", ["valid", "mixed", "all_invalid", "empty"])
async def test_paper_response_reports_candidate_summary(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    valid = {"id": "https://openalex.org/W123", "title": "Paper"}
    invalid: list[object] = [None, {}, {"id": "W123"}, {"title": "Paper"}]
    items, accepted, rejected = candidate_case(case, valid, invalid)
    payload = {"results": items}
    async with academic_response("openalex", payload=payload) as executor:
        hits = await OpenAlexProvider(executor).search("query")
    assert_json_candidates(caplog, "openalex", hits, payload, len(items), accepted, rejected)


@pytest.mark.parametrize("case", ["success", "unknown", "array", "decode"])
async def test_response_classification_never_logs_paper_payload(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    candidate: dict[str, object] = {
        "id": "W123",
        "title": "PRIVATE_TITLE",
        "abstract_inverted_index": {"PRIVATE_ABSTRACT_INDEX": [0]},
    }
    candidate.update(
        abstract="PRIVATE_ABSTRACT",
        error={"message": "PRIVATE_CANDIDATE_ERROR", "code": "PRIVATE_CANDIDATE_CODE"},
    )
    payload: object = {"results": [candidate]}
    response = private_json_response(case, payload)
    async with academic_response("openalex", response=response) as executor:
        provider = OpenAlexProvider(executor)
        if case == "success":
            assert len(await provider.search("query")) == 1
        else:
            with pytest.raises(ProtocolFailure):
                await provider.search("query")
    records = assert_response_classification(caplog, response, case)
    assert_private_payload_absent(records)
