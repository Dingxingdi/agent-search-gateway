from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.providers.academic.arxiv import ArxivProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "arxiv"


async def test_arxiv_maps_atom_feed_and_isolates_malformed_entry() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "search.xml").read_text()])
    provider = ArxivProvider(executor)

    hits = await provider.search("graph transformers")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "arxiv"
    assert hit.source_id == "2401.12345"
    assert hit.arxiv_id == "2401.12345"
    assert hit.title == "Example arXiv Paper"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Example abstract."
    assert hit.doi == "10.1000/arxiv.example"
    assert hit.published_date == date(2024, 1, 2)
    assert hit.updated_date == date(2024, 2, 3)
    assert hit.topics == ("cs.AI", "cs.LG")
    assert hit.url == "https://arxiv.org/abs/2401.12345v2"
    assert hit.pdf_url == "https://arxiv.org/pdf/2401.12345v2.pdf"

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.response_mode == "text"
    assert request.headers is None
    assert request.params == {
        "search_query": "all:graph transformers",
        "max_results": 10,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }


async def test_arxiv_malformed_xml_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "malformed.xml").read_text()])
    with pytest.raises(ProtocolFailure) as caught:
        await ArxivProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_arxiv_http_failure_propagates_without_adapter_retry() -> None:
    failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "transport")
    executor = RecordingJsonExecutor([failure])
    with pytest.raises(ExecutionFailure):
        await ArxivProvider(executor).search("query")
    assert len(executor.requests) == 1


@pytest.mark.parametrize(
    "case", ["valid", "mixed", "all_invalid", "empty", "decode", "wrong_root", "wrong_namespace"]
)
async def test_xml_provider_reports_response_and_candidate_diagnostics(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging
    import xml.etree.ElementTree as ET

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    body = (
        (FIXTURES / "search.xml").read_text().replace("Example abstract.", "PRIVATE_XML_BODY café")
    )
    root = ET.fromstring(body)
    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    if case == "valid":
        root.remove(entries[1])
    elif case == "all_invalid":
        root.remove(entries[0])
    elif case == "empty":
        for entry in entries:
            root.remove(entry)
    body = ET.tostring(root, encoding="unicode")
    if case == "decode":
        body = (FIXTURES / "malformed.xml").read_text() + "PRIVATE_XML_BODY"
    elif case == "wrong_root":
        body = "<html><body>PRIVATE_HTML_BODY captcha validation</body></html>"
    elif case == "wrong_namespace":
        body = '<feed xmlns="urn:wrong"><entry>PRIVATE_XML_BODY</entry></feed>'
    invalid_envelope = case in ("decode", "wrong_root", "wrong_namespace")
    received = {"valid": 1, "mixed": 2, "all_invalid": 1, "empty": 0}.get(case, 0)
    accepted = int(case in ("valid", "mixed"))
    rejected = received - accepted
    response = httpx.Response(
        200,
        content=body.encode("utf-16"),
        headers={"content-type": "application/atom+xml; charset=utf-16"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        provider = ArxivProvider(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="arxiv")
        )
        if invalid_envelope:
            with pytest.raises(ProtocolFailure) as caught:
                await provider.search("query")
            assert caught.value.code is ErrorCode.PROTOCOL_ERROR
            assert caught.value.message == (
                "arxiv/paper_search: response was not valid Atom XML"
                if case == "decode"
                else "arxiv/paper_search: response Atom envelope was invalid"
            )
        else:
            assert len(await provider.search("query")) == accepted

    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["response_kind"] == ("unknown" if case == "decode" else "xml")
    assert diagnostics[0]["semantic_result"] == ("unknown" if invalid_envelope else "success")
    if invalid_envelope:
        assert diagnostics[0]["category"] == ("decode" if case == "decode" else "invalid_envelope")
    assert diagnostics[0]["http_status"] == 200
    assert not any(name.startswith("upstream_") for name in diagnostics[0])
    assert (
        sum(
            getattr(record, "gateway_event", None) == "http_attempt_completed" for record in records
        )
        == 1
    )
    assert diagnostics[0]["response_bytes"] == len(response.content)
    assert diagnostics[0]["content_type"] == "application/atom+xml"
    summaries = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == (0 if invalid_envelope else 1)
    if summaries:
        assert summaries[0]["provider"] == "arxiv"
        assert summaries[0]["stage"] == "paper_search"
        assert summaries[0]["phase"] == "adapter"
        assert summaries[0]["received"] == received
        assert summaries[0]["accepted"] == accepted
        assert summaries[0]["rejected"] == rejected
        assert summaries[0]["rejection_counts"] == (
            {"missing_required_field": rejected} if rejected else {}
        )
    rejects = [
        vars(record)["gateway_fields"]["reason"]
        for record in records
        if getattr(record, "gateway_event", None) == "paper_candidate_rejected"
    ]
    assert rejects == ["missing_required_field"] * rejected
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)
