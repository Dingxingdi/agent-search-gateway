import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.exa import ExaAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/exa"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_exa_adapter_maps_search_and_contents_without_deprecated_fields() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("contents.json")])
    adapter = ExaAdapter(
        name="exa",
        api_url="https://exa.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            "https://example.com/exa",
            "Exa result",
            "First highlight",
            "Full Exa text",
            "Full Exa text",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/exa"))
    assert candidate == URLFetchCandidate("Fetched Exa text", "Fetched Exa text")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://exa.example.test/search"
    assert search_request.headers == {"x-api-key": "x"}
    assert search_request.json_body == {
        "query": "query",
        "contents": {"text": True, "highlights": True},
    }
    assert fetch_request.url == "https://exa.example.test/contents"
    assert fetch_request.json_body == {"urls": ["https://example.com/exa"], "text": True}
    for deprecated in ("context", "livecrawl", "tokensNum"):
        assert deprecated not in str(search_request.json_body)
        assert deprecated not in str(fetch_request.json_body)

    bad = ExaAdapter(
        name="exa",
        api_url="https://exa.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [{"url": "https://example.com/exa", "text": "body"}],
                    "statuses": [{"id": "https://example.com/exa", "status": "error"}],
                }
            ]
        ),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/exa"))


@pytest.mark.parametrize("state", ["error", "success"])
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, state: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import DiagnosticText, KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    target = "https://example.com/exa?key=PRIVATE_QUERY"
    payload = {
        "statuses": [
            {"id": "https://example.com/other", "status": "PRIVATE_UNMATCHED"},
            {
                "id": target,
                "status": state,
                "error": {
                    "code": "blocked",
                    "message": "target blocked",
                    "body": "PRIVATE_ERROR_BODY",
                },
            },
        ],
        "results": [{"url": target, "text": "PRIVATE_BODY", "error": "PRIVATE_CONTENT_ERROR"}],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = ExaAdapter(
            name="exa",
            api_url="https://exa.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="exa"),
        )
        if state == "success":
            assert (await adapter.fetch(normalize_url(target))).content == "PRIVATE_BODY"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(normalize_url(target))
            assert caught.value.message == "exa/fetch: provider reported per-URL failure"
            status = caught.value.diagnostics["upstream_status"]
            assert isinstance(status, DiagnosticText)
            assert status.text == "error"
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostic = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostic) == 1
    assert diagnostic[0]["semantic_result"] == ("success" if state == "success" else "error")
    assert diagnostic[0]["upstream_status"].text == state
    rendered = "\n".join(
        KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")])).format(record)
        for record in records
    )
    assert "PRIVATE_" not in rendered
    if state == "error":
        assert diagnostic[0]["category"] == "provider_response"
        assert "upstream_code=blocked" in rendered
        assert 'upstream_message="target blocked"' in rendered
