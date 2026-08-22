from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.web.serpapi import SerpApiAdapter
from tests.support.http import RecordingJsonExecutor


async def test_serpapi_search_uses_google_engine_and_maps_organic_results() -> None:
    executor = RecordingJsonExecutor(
        [
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A", "snippet": "Snippet A"}
                ]
            }
        ]
    )
    adapter = SerpApiAdapter(
        name="serpapi",
        api_url="https://serpapi.test/",
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
    assert parsed._replace(query="").geturl() == "https://serpapi.test/search"
    assert parse_qs(parsed.query) == {
        "engine": ["google"],
        "q": ["hello world & docs"],
        "api_key": ["secret"],
    }
    assert request.headers is None
    assert request.json_body is None


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_serpapi_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        SerpApiAdapter(
            name="serpapi",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingJsonExecutor([]),
        )
