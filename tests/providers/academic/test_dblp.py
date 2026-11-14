from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.providers.academic.dblp import DblpProvider
from tests.support.academic_diagnostics import (
    academic_response,
    assert_candidate_counts,
    assert_private_payload_absent,
    assert_response_classification,
)
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
    async with academic_response("dblp", response=response) as executor:
        provider = DblpProvider(executor)
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

    records = assert_response_classification(
        caplog,
        response,
        case,
        response_kind="unknown" if case == "decode" else "xml",
        invalid_envelope=invalid_envelope,
        content_type=media_type,
    )
    assert_candidate_counts(
        records,
        "dblp",
        received,
        accepted,
        {expected_reason: rejected} if rejected else {},
        summary_expected=not invalid_envelope,
    )
    assert_private_payload_absent(records)
