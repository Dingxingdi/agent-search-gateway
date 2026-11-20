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


@pytest.mark.parametrize("with_contact", [False, True])
async def test_openalex_sends_bearer_header_without_key_in_url(
    with_contact: bool, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    secret = SecretValue("[REDACTED_SECRET]")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_fixture("search.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAlexProvider(
            HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="openalex"),
            api_key=secret,
            contact_email=SecretValue("research@example.test") if with_contact else None,
        )
        hits = await provider.search("open science")
    assert len(hits) == 2
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.headers.get("Authorization") == f"Bearer {secret.reveal()}"
    assert str(request.url).startswith("https://api.openalex.org/works?")
    assert secret.reveal() not in str(request.url)
    assert "api_key" not in request.url.params
    expected_params = {"search": "open science", "per_page": "10"}
    if with_contact:
        expected_params["mailto"] = "research@example.test"
    assert dict(request.url.params) == expected_params
    assert secret.reveal() not in repr(provider)
    assert secret.reveal() not in repr(secret)
    formatter = KeyValueFormatter(SecretRedactor([secret]))
    assert secret.reveal() not in "\n".join(formatter.format(record) for record in caplog.records)


@pytest.mark.parametrize("status", [401, 403])
async def test_openalex_auth_failure_has_no_anonymous_fallback(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import httpx

    from agent_search_gateway.errors import ExecutionFailure
    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    secret = SecretValue("[REDACTED_SECRET]")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": secret.reveal()}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAlexProvider(
            HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="openalex"),
            api_key=secret,
        )
        with pytest.raises(ExecutionFailure) as caught:
            await provider.search("query")
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {secret.reveal()}"
    assert secret.reveal() not in str(caught.value)
    assert secret.reveal() not in repr(caught.value)
    assert secret.reveal() not in repr(provider)
    formatter = KeyValueFormatter(SecretRedactor([secret]))
    assert secret.reveal() not in "\n".join(formatter.format(record) for record in caplog.records)


async def test_openalex_honors_shared_retry_after_with_bearer_on_every_attempt() -> None:
    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.providers.http import HttpJsonExecutor

    secret = SecretValue("[REDACTED_SECRET]")
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json=_fixture("search.json"))

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAlexProvider(
            HttpJsonExecutor(
                client, RetryPolicy(2, 0.25, 2, 5), provider_name="openalex", sleep=sleep
            ),
            api_key=secret,
        )
        hits = await provider.search("open science")
    assert len(requests) == 2
    assert requests[0].url == requests[1].url
    assert all(
        request.headers["Authorization"] == f"Bearer {secret.reveal()}" for request in requests
    )
    assert delays == [5.0]
    assert len(hits) == 2


def test_reconstruct_abstract_sorts_positions_and_accepts_empty_index() -> None:
    assert reconstruct_abstract({"world": [1], "Hello": [0], "again": [2]}) == "Hello world again"
    assert reconstruct_abstract({}) == ""
    assert reconstruct_abstract(None) == ""


async def test_openalex_maps_works_and_falls_back_to_canonical_landing() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await OpenAlexProvider(executor, api_key=SecretValue("[REDACTED_SECRET]")).search(
        "open science"
    )

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
        api_key=SecretValue("[REDACTED_SECRET]"),
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
        await OpenAlexProvider(executor, api_key=SecretValue("[REDACTED_SECRET]")).search("query")
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
        hits = await OpenAlexProvider(executor, api_key=SecretValue("[REDACTED_SECRET]")).search(
            "query"
        )
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
        provider = OpenAlexProvider(executor, api_key=SecretValue("[REDACTED_SECRET]"))
        if case == "success":
            assert len(await provider.search("query")) == 1
        else:
            with pytest.raises(ProtocolFailure):
                await provider.search("query")
    records = assert_response_classification(caplog, response, case)
    assert_private_payload_absent(records)
