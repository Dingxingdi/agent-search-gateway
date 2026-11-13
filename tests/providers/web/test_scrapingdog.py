from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.scrapingdog import ScrapingDogAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor


async def test_scrapingdog_search_uses_google_endpoint_and_minimal_query() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A", "snippet": "Snippet A"}
                ]
            }
        ]
    )
    adapter = ScrapingDogAdapter(
        name="scrapingdog",
        api_url="https://api.scrapingdog.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    assert await adapter.search("hello world & docs") == [
        KeywordSearchHit("https://example.com/a", "A", "Snippet A")
    ]
    request = executor.requests[0]
    parsed = urlparse(request.url)
    assert request.method == "GET"
    assert request.stage == "search"
    assert parsed._replace(query="").geturl() == "https://api.scrapingdog.test/google"
    assert parse_qs(parsed.query) == {"api_key": ["secret"], "query": ["hello world & docs"]}


async def test_scrapingdog_fetch_uses_scrape_endpoint_and_maps_page_text() -> None:
    executor = RecordingHttpExecutor(text_responses=["<html>page</html>"])
    adapter = ScrapingDogAdapter(
        name="scrapingdog",
        api_url="https://api.scrapingdog.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate(
        "<html>page</html>", "<html>page</html>"
    )
    request = executor.requests[0]
    parsed = urlparse(request.url)
    assert request.method == "GET"
    assert request.stage == "fetch"
    assert parsed._replace(query="").geturl() == "https://api.scrapingdog.test/scrape"
    assert parse_qs(parsed.query) == {"api_key": ["secret"], "url": [str(target)]}


@pytest.mark.parametrize("body", ["", " \n"])
async def test_scrapingdog_fetch_rejects_empty_body(body: str) -> None:
    adapter = ScrapingDogAdapter(
        name="scrapingdog",
        api_url="https://api.scrapingdog.test",
        secret=SecretValue("secret"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_scrapingdog_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ScrapingDogAdapter(
            name="scrapingdog",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingHttpExecutor(),
        )


@pytest.mark.parametrize(
    ("body", "status", "semantic"),
    [
        ("# PRIVATE_BODY\n<html>PRIVATE_HTML</html>", 200, "success"),
        (" \n\t", 200, "empty"),
        ("<html>PRIVATE_HTTP_ERROR</html>", 403, "error"),
    ],
)
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, body: str, status: int, semantic: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                text=body,
                headers={
                    "content-type": "text/markdown",
                    "x-brd-status-code": "502",
                    "x-brd-error-code": "captcha",
                },
            )
        )
    ) as client:
        adapter = ScrapingDogAdapter(
            name="scrapingdog",
            api_url="https://scrapingdog.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="scrapingdog"
            ),
        )
        target = normalize_url("https://example.com/page?key=PRIVATE_QUERY")
        if semantic == "success":
            assert await adapter.fetch(target) == URLFetchCandidate(body, body)
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(target)
            expected = "page body is empty" if semantic == "empty" else "HTTP status 403"
            assert caught.value.message == f"scrapingdog/fetch: {expected}"
            assert caught.value.diagnostics["semantic_result"] == semantic
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == semantic
    assert diagnostics[0]["response_kind"] == "text"
    assert diagnostics[0]["http_status"] == status
    assert diagnostics[0]["response_bytes"] == len(body.encode())
    assert diagnostics[0]["content_type"] == "text/markdown"
    assert "upstream_message" not in diagnostics[0]
    assert "upstream_code" not in diagnostics[0]
    rendered = "\n".join(
        KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")])).format(record)
        for record in records
    )
    assert "PRIVATE_" not in rendered
