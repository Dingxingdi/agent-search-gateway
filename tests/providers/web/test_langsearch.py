import json
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import (
    KeywordSearchHit,
    KeywordSearchProvider,
    URLFetchProvider,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from tests.support.http import RecordingJsonExecutor

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/langsearch/search.json"


@pytest.mark.parametrize(
    ("api_url", "expected_url"),
    [
        ("https://api.langsearch.com", "https://api.langsearch.com/v1/web-search"),
        ("https://api.langsearch.com/", "https://api.langsearch.com/v1/web-search"),
        (
            "https://gateway.example.test/prefix/",
            "https://gateway.example.test/prefix/v1/web-search",
        ),
    ],
)
async def test_langsearch_posts_metadata_only_request(api_url: str, expected_url: str) -> None:
    registration = build_default_registry().get("langsearch")
    assert registration is not None
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    adapter = registration.factory(
        name="langsearch", api_url=api_url, secret=SecretValue("example"), http_executor=executor
    )
    assert isinstance(adapter, KeywordSearchProvider)
    assert not isinstance(adapter, URLFetchProvider)
    query = "  中文？  café!  "
    hits = await adapter.search(query)

    [request] = executor.requests
    assert (request.method, request.stage, request.url) == ("POST", "search", expected_url)
    assert request.headers == {"Authorization": "Bearer example"}
    assert request.params is None
    assert request.json_body == {"query": query, "count": 10}
    assert hits == [
        KeywordSearchHit("https://first.example.test/", "First title", "First snippet"),
        KeywordSearchHit("https://second.example.test/", "Second title", "Second snippet"),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)


async def test_langsearch_sends_json_content_type() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Content-Type"] == "application/json"
        return httpx.Response(200, json=json.loads(_FIXTURE.read_text(encoding="utf-8")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        registration = build_default_registry().require("langsearch")
        adapter = registration.factory(
            name="langsearch",
            api_url="https://api.langsearch.com",
            secret=SecretValue("example"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="langsearch"
            ),
        )
        assert isinstance(adapter, KeywordSearchProvider)
        assert len(await adapter.search("query")) == 2
