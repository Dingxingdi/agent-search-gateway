from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.providers.academic.dblp import DblpProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "dblp"


async def test_dblp_maps_publication_xml_and_uses_stable_key() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "search.xml").read_text()])
    hits = await DblpProvider(executor).search("structured search")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_id == "conf/example/Author24"
    assert hit.title == "A Structured DBLP Paper"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.venue == "ExampleConf"
    assert hit.published_date == date(2024, 1, 1)
    assert hit.url == "https://dblp.org/rec/conf/example/Author24"
    assert hit.doi == "10.1000/dblp.example"
    assert hit.abstract == ""

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://dblp.org/search/publ/api"
    assert request.response_mode == "text"
    assert request.params == {"q": "structured search", "format": "xml", "h": 10}


async def test_dblp_malformed_xml_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "malformed.xml").read_text()])
    with pytest.raises(ProtocolFailure) as caught:
        await DblpProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_dblp_failure_does_not_trigger_html_fallback() -> None:
    executor = RecordingJsonExecutor([ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "failed")])
    with pytest.raises(ExecutionFailure):
        await DblpProvider(executor).search("query")
    assert len(executor.requests) == 1


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "mixed",
        "all_invalid",
        "empty",
        "decode",
        "wrong_root",
        "wrong_namespace",
        "missing_hits",
        "missing_info",
    ],
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
    body = (FIXTURES / "search.xml").read_text().replace("ExampleConf", "PRIVATE_XML_BODY café")
    root = ET.fromstring(body)
    container = root.find("hits")
    assert container is not None
    entries = container.findall("hit")
    if case == "valid":
        container.remove(entries[1])
    elif case == "all_invalid":
        container.remove(entries[0])
    elif case == "empty":
        for entry in entries:
            container.remove(entry)
    if case == "missing_info":
        container.remove(entries[0])
        entries[1].clear()
    body = ET.tostring(root, encoding="unicode")
    if case == "decode":
        body = (FIXTURES / "malformed.xml").read_text() + "PRIVATE_XML_BODY"
    elif case == "wrong_root":
        body = "<html><body>PRIVATE_HTML_BODY captcha validation</body></html>"
    elif case == "wrong_namespace":
        body = '<result xmlns="urn:wrong"><hits>PRIVATE_XML_BODY</hits></result>'
    elif case == "missing_hits":
        body = "<result>PRIVATE_XML_BODY</result>"
    invalid_envelope = case in ("decode", "wrong_root", "wrong_namespace", "missing_hits")
    received = {"valid": 1, "mixed": 2, "all_invalid": 1, "missing_info": 1, "empty": 0}.get(
        case, 0
    )
    accepted = int(case in ("valid", "mixed"))
    rejected = received - accepted
    expected_reason = "invalid_record_shape" if case == "missing_info" else "missing_required_field"
    media_type = "text/html" if case == "wrong_root" else "application/xml"
    response = httpx.Response(
        200,
        content=body.encode("utf-16"),
        headers={"content-type": f"{media_type}; charset=utf-16"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        provider = DblpProvider(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="dblp")
        )
        if invalid_envelope:
            with pytest.raises(ProtocolFailure) as caught:
                await provider.search("query")
            assert caught.value.code is ErrorCode.PROTOCOL_ERROR
            assert caught.value.message == (
                "dblp/paper_search: response was not valid XML"
                if case == "decode"
                else "dblp/paper_search: response XML envelope was invalid"
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
    assert diagnostics[0]["content_type"] == media_type
    summaries = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == (0 if invalid_envelope else 1)
    if summaries:
        assert summaries[0]["provider"] == "dblp"
        assert summaries[0]["stage"] == "paper_search"
        assert summaries[0]["phase"] == "adapter"
        assert summaries[0]["received"] == received
        assert summaries[0]["accepted"] == accepted
        assert summaries[0]["rejected"] == rejected
        assert summaries[0]["rejection_counts"] == ({expected_reason: rejected} if rejected else {})
    rejects = [
        vars(record)["gateway_fields"]["reason"]
        for record in records
        if getattr(record, "gateway_event", None) == "paper_candidate_rejected"
    ]
    assert rejects == [expected_reason] * rejected
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)
