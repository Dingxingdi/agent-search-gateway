import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.semantic_scholar import SemanticScholarProvider
from tests.support.academic_diagnostics import (
    academic_response,
    assert_json_candidates,
    assert_private_payload_absent,
    assert_response_classification,
    candidate_case,
    private_json_response,
)
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "semantic_scholar"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_semantic_scholar_maps_graph_search_without_auth_when_omitted() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await SemanticScholarProvider(executor).search("transformers")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "649DEF34F8BE52C8B66281AF98AE884C09AEF38B"
    assert hit.doi == "10.5555/3295222.3295349"
    assert hit.arxiv_id == "1706.03762v5"
    assert hit.authors == ("Ashish Vaswani", "Noam Shazeer")
    assert hit.published_date == date(2017, 6, 12)
    assert hit.citation_count == 12345
    assert hit.topics == ("Computer Science", "Mathematics")
    assert hit.pdf_url == "https://example.test/attention.pdf"
    assert hit.is_open_access is True
    assert hits[1].abstract == ""
    assert hits[1].pdf_url == ""

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url.endswith("/paper/search")
    assert request.headers is None
    assert request.params == {
        "query": "transformers",
        "limit": 10,
        "fields": (
            "title,abstract,citationCount,authors,url,publicationDate,"
            "externalIds,fieldsOfStudy,openAccessPdf"
        ),
    }


async def test_semantic_scholar_optional_auth_header_is_sent_once() -> None:
    executor = RecordingJsonExecutor([{"data": []}])
    await SemanticScholarProvider(
        executor,
        api_key=SecretValue("[REDACTED_SECRET]"),
    ).search("query")
    assert executor.requests[0].headers == {"x-api-key": "[REDACTED_SECRET]"}


async def test_semantic_scholar_invalid_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await SemanticScholarProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert caught.value.reason == "invalid_data_envelope"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "missing_data_envelope"),
        ({"error": "RAW_PROVIDER_DETAIL_SENTINEL"}, "missing_data_envelope"),
        ({"data": None}, "invalid_data_envelope"),
        ([], "invalid_data_envelope"),
    ],
)
async def test_semantic_scholar_classifies_unexpected_success_envelopes(
    payload: object,
    reason: str,
) -> None:
    with pytest.raises(ProtocolFailure) as caught:
        await SemanticScholarProvider(RecordingJsonExecutor([payload])).search("query")

    assert caught.value.reason == reason
    assert reason in caught.value.message
    assert "RAW_PROVIDER_DETAIL_SENTINEL" not in caught.value.message


async def test_semantic_scholar_auth_failure_has_no_unauthenticated_fallback() -> None:
    executor = RecordingJsonExecutor(
        [ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "authentication rejected")]
    )
    provider = SemanticScholarProvider(
        executor,
        api_key=SecretValue("[REDACTED_SECRET]"),
    )
    with pytest.raises(ExecutionFailure):
        await provider.search("query")
    assert len(executor.requests) == 1


@pytest.mark.parametrize("case", ["valid", "mixed", "all_invalid", "empty"])
async def test_paper_response_reports_candidate_summary(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    valid = {"paperId": "paper-1", "title": "Paper", "url": "https://example.test/paper"}
    invalid: list[object] = [None, {}, {"paperId": "paper-1"}, {"title": "Paper"}]
    items, accepted, rejected = candidate_case(case, valid, invalid)
    payload = {"data": items}
    async with academic_response("semantic_scholar", payload=payload) as executor:
        hits = await SemanticScholarProvider(executor).search("query")
    assert_json_candidates(
        caplog, "semantic_scholar", hits, payload, len(items), accepted, rejected
    )


@pytest.mark.parametrize("status", [200, 429])
async def test_scalar_error_diagnostics_preserve_public_envelope_failure(
    caplog: pytest.LogCaptureFixture, status: int
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import DiagnosticText, KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    secret = SecretValue("PRIVATE_AUTH")
    detail = "quota blocked PRIVATE_AUTH https://example.test/path?q=PRIVATE_QUERY " + "x" * 600
    payload = {"error": detail, "body": "PRIVATE_BODY"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    ) as client:
        with pytest.raises(ExecutionFailure) as caught:
            await SemanticScholarProvider(
                HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="semantic_scholar"),
                api_key=secret,
            ).search("query")
    if status == 200:
        assert isinstance(caught.value, ProtocolFailure)
        assert caught.value.reason == "missing_data_envelope"
        assert caught.value.message == "semantic_scholar/paper_search: missing_data_envelope"
    assert "quota blocked" not in caught.value.message
    message = caught.value.diagnostics.get("upstream_message")
    assert isinstance(message, DiagnosticText)
    assert message.text == detail
    assert "upstream_code" not in caught.value.diagnostics
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["http_status"] == status
    assert diagnostics[0]["category"] == "provider_response"
    assert diagnostics[0]["semantic_result"] == "error"
    assert diagnostics[0]["upstream_message"].text == detail
    formatter = KeyValueFormatter(SecretRedactor([secret]))
    rendered = "\n".join(formatter.format(record) for record in records)
    assert "quota blocked" in rendered
    assert "PRIVATE_" not in rendered
    assert "upstream_message_truncated=true" in rendered


@pytest.mark.parametrize("case", ["success", "unknown", "array", "decode"])
async def test_response_classification_never_logs_paper_payload(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    candidate: dict[str, object] = {
        "paperId": "paper-1",
        "title": "PRIVATE_TITLE",
        "url": "https://example.test/paper",
    }
    candidate.update(
        abstract="PRIVATE_ABSTRACT",
        error={"message": "PRIVATE_CANDIDATE_ERROR", "code": "PRIVATE_CANDIDATE_CODE"},
    )
    payload: object = {"data": [candidate]}
    response = private_json_response(case, payload)
    async with academic_response("semantic_scholar", response=response) as executor:
        provider = SemanticScholarProvider(executor)
        if case == "success":
            assert len(await provider.search("query")) == 1
        else:
            with pytest.raises(ProtocolFailure):
                await provider.search("query")
    records = assert_response_classification(caplog, response, case)
    assert_private_payload_absent(records)
