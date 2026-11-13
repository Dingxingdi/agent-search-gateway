from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import URLFetchCandidate
from agent_search_gateway.providers.web.scrapingant import ScrapingAntAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor


async def test_scrapingant_fetch_uses_v2_markdown_and_maps_markdown_field() -> None:
    executor = RecordingJsonExecutor(
        [{"url": "https://example.com/path", "markdown": "# page\nbody"}]
    )
    adapter = ScrapingAntAdapter(
        name="scrapingant",
        api_url="https://api.scrapingant.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate("# page\nbody", "# page\nbody")
    request = executor.requests[0]
    parsed = urlparse(request.url)
    assert request.method == "GET"
    assert request.stage == "fetch"
    assert parsed._replace(query="").geturl() == "https://api.scrapingant.test/v2/markdown"
    assert parse_qs(parsed.query) == {"url": [str(target)], "x-api-key": ["secret"]}
    assert request.headers is None
    assert request.json_body is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"markdown": None},
        {"markdown": 1},
        {"markdown": ""},
        {"markdown": "   "},
        {"error": "RAW_VENDOR_DIAGNOSTIC"},
    ],
)
async def test_scrapingant_fetch_rejects_invalid_markdown_without_vendor_diagnostics(
    payload: object,
) -> None:
    adapter = ScrapingAntAdapter(
        name="scrapingant",
        api_url="https://api.scrapingant.test",
        secret=SecretValue("secret"),
        http_executor=RecordingJsonExecutor([payload]),
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com"))
    assert "RAW_VENDOR_DIAGNOSTIC" not in caught.value.message


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_scrapingant_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ScrapingAntAdapter(
            name="scrapingant",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingJsonExecutor([]),
        )


@pytest.mark.parametrize(
    ("payload", "message", "semantic"),
    [
        (
            {
                "error": {
                    "code": "blocked",
                    "message": "target blocked",
                    "body": "PRIVATE_ERROR_BODY",
                },
                "markdown": "PRIVATE_BODY",
            },
            "provider reported failure",
            "error",
        ),
        (
            {"markdown": "PRIVATE_BODY", "metadata": {"error": "PRIVATE_CONTENT_ERROR"}},
            None,
            "success",
        ),
        ({"markdown": "  "}, "markdown must be non-empty", "empty"),
    ],
)
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, payload: object, message: str | None, semantic: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import DiagnosticText, KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = ScrapingAntAdapter(
            name="scrapingant",
            api_url="https://scrapingant.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="scrapingant"
            ),
        )
        if message is None:
            assert (
                await adapter.fetch(normalize_url("https://example.com/page?key=PRIVATE_QUERY"))
            ).content == "PRIVATE_BODY"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(normalize_url("https://example.com/page?key=PRIVATE_QUERY"))
            assert caught.value.message == f"scrapingant/fetch: {message}"
            if semantic == "error":
                selected = caught.value.diagnostics["upstream_message"]
                assert isinstance(selected, DiagnosticText)
                assert selected.text == "target blocked"
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == semantic
    rendered = "\n".join(
        KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")])).format(record)
        for record in records
    )
    assert "PRIVATE_" not in rendered
    if semantic == "error":
        assert 'upstream_message="target blocked"' in rendered
    else:
        assert "target blocked" not in rendered
