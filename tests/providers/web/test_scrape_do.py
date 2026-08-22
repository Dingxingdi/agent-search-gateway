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
