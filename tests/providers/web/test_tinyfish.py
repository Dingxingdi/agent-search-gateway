import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.tinyfish import TinyFishAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/tinyfish"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_tinyfish_adapter_uses_separate_search_and_fetch_base_urls() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("fetch.json")])
    adapter = TinyFishAdapter(
        name="tinyfish",
        search_api_url="https://search.tiny.example.test/",
        fetch_api_url="https://fetch.tiny.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("hello world") == [
        KeywordSearchHit("https://example.com/tiny", "TinyFish result", "TinyFish snippet")
    ]
    assert await adapter.fetch(normalize_url("https://example.com/tiny")) == URLFetchCandidate(
        "TinyFish fetched markdown", "TinyFish fetched markdown"
    )

    search_request, fetch_request = executor.requests
    assert search_request.method == "GET"
    assert search_request.url == "https://search.tiny.example.test?query=hello+world"
    assert search_request.headers == {"X-API-Key": "x"}
    assert fetch_request.url == "https://fetch.tiny.example.test"
    assert fetch_request.headers == {"X-API-Key": "x"}
    assert fetch_request.json_body == {
        "urls": ["https://example.com/tiny"],
        "format": "markdown",
        "links": False,
        "image_links": False,
    }

    bad = TinyFishAdapter(
        name="tinyfish",
        search_api_url="https://search.tiny.example.test",
        fetch_api_url="https://fetch.tiny.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [],
                    "errors": [{"url": "https://example.com/tiny", "error": "timeout"}],
                }
            ]
        ),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/tiny"))


@pytest.mark.parametrize("case", ["error", "success", "strict"])
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import DiagnosticText, KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    target = "https://example.com/page?key=PRIVATE_QUERY"
    results: list[object] = (
        []
        if case == "error"
        else [{"url": target, "text": "PRIVATE_BODY", "error": "PRIVATE_CONTENT_ERROR"}]
    )
    if case == "strict":
        results.insert(0, None)
    payload = {
        "results": results,
        "errors": [
            {"url": "https://example.com/other", "error": "PRIVATE_UNMATCHED"},
            {
                "url": target,
                "error": {
                    "code": "blocked",
                    "message": "target blocked",
                    "body": "PRIVATE_ERROR_BODY",
                },
            },
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = TinyFishAdapter(
            name="tinyfish",
            search_api_url="https://search.tiny.example.test",
            fetch_api_url="https://fetch.tiny.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="tinyfish"
            ),
        )
        if case == "success":
            assert (await adapter.fetch(normalize_url(target))).content == "PRIVATE_BODY"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(normalize_url(target))
            if case == "error":
                assert caught.value.message == "tinyfish/fetch: provider reported per-URL failure"
                message = caught.value.diagnostics["upstream_message"]
                assert isinstance(message, DiagnosticText)
                assert message.text == "target blocked"
            else:
                assert caught.value.message == "tinyfish/fetch: result must be an object"
                assert caught.value.diagnostics["semantic_result"] == "error"
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostic = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostic) == 1
    assert diagnostic[0]["semantic_result"] == ("success" if case == "success" else "error")
    rendered = "\n".join(
        KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")])).format(record)
        for record in records
    )
    assert "PRIVATE_" not in rendered
    if case == "error":
        assert "upstream_code=blocked" in rendered
        assert 'upstream_message="target blocked"' in rendered
    else:
        assert "target blocked" not in rendered
