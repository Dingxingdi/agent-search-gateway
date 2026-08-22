from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor


async def test_scraperapi_search_uses_synchronous_structured_google_endpoint() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A", "snippet": "Snippet A"}
                ]
            }
        ]
    )
    adapter = ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test/",
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
    assert parsed._replace(query="").geturl() == "https://api.scraperapi.test/structured/google/search"
    assert parse_qs(parsed.query) == {"api_key": ["secret"], "query": ["hello world & docs"]}


async def test_scraperapi_fetch_uses_synchronous_scrape_endpoint() -> None:
    executor = RecordingHttpExecutor(text_responses=["<html>page</html>"])
    adapter = ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test/",
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
    assert parsed._replace(query="").geturl() == "https://api.scraperapi.test"
    assert parse_qs(parsed.query) == {"api_key": ["secret"], "url": [str(target)]}


@pytest.mark.parametrize("body", ["", " \n"])
async def test_scraperapi_fetch_rejects_empty_body(body: str) -> None:
    adapter = ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test",
        secret=SecretValue("secret"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_scraperapi_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ScraperAPIAdapter(
            name="scraperapi",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingHttpExecutor(),
        )
