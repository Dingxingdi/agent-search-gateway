from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.scrape_do import ScrapeDoAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor


async def test_scrape_do_search_builds_minimal_google_plugin_request() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A", "snippet": "Snippet A"}
                ]
            }
        ]
    )
    adapter = ScrapeDoAdapter(
        name="scrape_do",
        api_url="https://api.scrape.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    assert await adapter.search("hello world & docs") == [
        KeywordSearchHit("https://example.com/a", "A", "Snippet A")
    ]
    request = executor.requests[0]
    assert request.method == "GET"
    assert request.stage == "search"
    parsed = urlparse(request.url)
    assert parsed._replace(query="").geturl() == "https://api.scrape.test/plugin/google/search"
    assert parse_qs(parsed.query) == {"token": ["secret"], "q": ["hello world & docs"]}
    assert request.headers is None
    assert request.json_body is None


async def test_scrape_do_fetch_requests_markdown_and_maps_text() -> None:
    executor = RecordingHttpExecutor(text_responses=["# page\nbody"])
    adapter = ScrapeDoAdapter(
        name="scrape_do",
        api_url="https://api.scrape.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate("# page\nbody", "# page\nbody")
    request = executor.requests[0]
    assert request.method == "GET"
    assert request.stage == "fetch"
    parsed = urlparse(request.url)
    assert parsed._replace(query="").geturl() == "https://api.scrape.test"
    assert parse_qs(parsed.query) == {
        "token": ["secret"],
        "url": [str(target)],
        "output": ["markdown"],
    }
    assert request.headers is None
    assert request.json_body is None


@pytest.mark.parametrize("body", ["", " \n"])
async def test_scrape_do_fetch_rejects_empty_body(body: str) -> None:
    adapter = ScrapeDoAdapter(
        name="scrape_do",
        api_url="https://api.scrape.test",
        secret=SecretValue("secret"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_scrape_do_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ScrapeDoAdapter(
            name="scrape_do",
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
        adapter = ScrapeDoAdapter(
            name="scrape_do",
            api_url="https://scrape_do.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="scrape_do"
            ),
        )
        target = normalize_url("https://example.com/page?key=PRIVATE_QUERY")
        if semantic == "success":
            assert await adapter.fetch(target) == URLFetchCandidate(body, body)
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(target)
            expected = "page body is empty" if semantic == "empty" else "HTTP status 403"
            assert caught.value.message == f"scrape_do/fetch: {expected}"
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
