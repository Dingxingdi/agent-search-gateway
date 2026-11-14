import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.core import CoreProvider
from tests.support.academic_diagnostics import (
    academic_response,
    assert_json_candidates,
    assert_private_payload_absent,
    assert_response_classification,
    candidate_case,
    private_json_response,
)
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "core"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_core_maps_works_with_required_bearer_auth() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await CoreProvider(
        executor,
        api_key=SecretValue("[REDACTED_SECRET]"),
    ).search("repositories")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "123456"
    assert hit.title == "CORE Example"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Repository abstract."
    assert hit.doi == "10.1000/CORE.EXAMPLE"
    assert hit.published_date == date(2022, 5, 6)
    assert hit.url == "https://core.ac.uk/works/123456"
    assert hit.pdf_url == "https://core.ac.uk/download/pdf/123456.pdf"
    assert hit.venue == "Example Repository"
    assert hit.topics == ("Machine Learning", "Open Access")
    assert hit.citation_count == 8
    assert hits[1].authors == ("String Author",)
    assert hits[1].pdf_url == "https://repository.example/fallback.pdf"

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.core.ac.uk/v3/search/works/"
    assert request.headers == {"Authorization": "Bearer [REDACTED_SECRET]"}
    assert request.params == {"q": "repositories", "limit": 10, "offset": 0}


async def test_core_bearer_is_sent_without_authentication_fallback() -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    provider = CoreProvider(executor, api_key=SecretValue("[REDACTED_SECRET]"))
    await provider.search("query")
    assert executor.requests[0].headers == {"Authorization": "Bearer [REDACTED_SECRET]"}

    failed = RecordingJsonExecutor(
        [ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "authentication rejected")]
    )
    with pytest.raises(ExecutionFailure):
        await CoreProvider(failed, api_key=SecretValue("[REDACTED_SECRET]")).search("query")
    assert len(failed.requests) == 1


async def test_core_invalid_results_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await CoreProvider(executor, api_key=SecretValue("[REDACTED_SECRET]")).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("case", ["valid", "mixed", "all_invalid", "empty"])
async def test_paper_response_reports_candidate_summary(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    valid = {"id": 123, "title": "Paper"}
    invalid: list[object] = [None, {}, {"id": 123}, {"title": "Paper"}]
    items, accepted, rejected = candidate_case(case, valid, invalid)
    payload = {"results": items}
    async with academic_response("core", payload=payload) as executor:
        hits = await CoreProvider(
            executor,
            api_key=SecretValue("[REDACTED_SECRET]"),
        ).search("query")
    assert_json_candidates(caplog, "core", hits, payload, len(items), accepted, rejected)


@pytest.mark.parametrize("case", ["success", "unknown", "array", "decode"])
async def test_response_classification_never_logs_paper_payload(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    candidate: dict[str, object] = {"id": 123, "title": "PRIVATE_TITLE"}
    candidate.update(
        abstract="PRIVATE_ABSTRACT",
        error={"message": "PRIVATE_CANDIDATE_ERROR", "code": "PRIVATE_CANDIDATE_CODE"},
    )
    payload: object = {"results": [candidate]}
    response = private_json_response(case, payload)
    async with academic_response("core", response=response) as executor:
        provider = CoreProvider(
            executor,
            api_key=SecretValue("[REDACTED_SECRET]"),
        )
        if case == "success":
            assert len(await provider.search("query")) == 1
        else:
            with pytest.raises(ProtocolFailure):
                await provider.search("query")
    records = assert_response_classification(caplog, response, case)
    assert_private_payload_absent(records)
