from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor


async def test_brightdata_search_uses_serp_zone_and_maps_organic_results() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            {
                "organic": [
                    {
                        "link": "https://example.com/a",
                        "title": "A",
                        "description": "Snippet A",
                        "rank": 1,
                    }
                ],
                "ignored": "metadata",
            }
        ]
    )
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test/",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    assert await adapter.search("hello world & docs") == [
        KeywordSearchHit("https://example.com/a", "A", "Snippet A")
    ]

    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://api.brightdata.test/request"
    assert request.stage == "search"
    assert request.headers == {"Authorization": "Bearer secret"}
    assert isinstance(request.json_body, dict)
    assert request.json_body["zone"] == "serp-zone"
    assert "fetch-zone" not in str(request.json_body)
    assert request.json_body["format"] == "raw"
    assert set(request.json_body) == {"zone", "url", "format"}
    google_url = str(request.json_body["url"])
    params = parse_qs(urlparse(google_url).query)
    assert urlparse(google_url)._replace(query="").geturl() == "https://www.google.com/search"
    assert params == {"q": ["hello world & docs"], "brd_json": ["1"]}


async def test_brightdata_fetch_uses_fetch_zone_and_returns_markdown_text() -> None:
    executor = RecordingHttpExecutor(text_responses=["# fetched\nbody"])
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    target = normalize_url("https://example.com/path?a=1&b=2")
    assert await adapter.fetch(target) == URLFetchCandidate("# fetched\nbody", "# fetched\nbody")

    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://api.brightdata.test/request"
    assert request.stage == "fetch"
    assert request.headers == {"Authorization": "Bearer secret"}
    assert request.json_body == {
        "zone": "fetch-zone",
        "url": str(target),
        "format": "raw",
        "data_format": "markdown",
    }
    assert "serp-zone" not in str(request.json_body)


async def test_brightdata_search_and_fetch_keep_zones_independent() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[{"organic": []}],
        text_responses=["body"],
    )
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    await adapter.search("query")
    await adapter.fetch(normalize_url("https://example.com"))

    search_request, fetch_request = executor.requests
    assert isinstance(search_request.json_body, dict)
    assert isinstance(fetch_request.json_body, dict)
    assert search_request.json_body["zone"] == "serp-zone"
    assert fetch_request.json_body["zone"] == "fetch-zone"


@pytest.mark.parametrize("field", ["api_url", "search_zone", "fetch_zone"])
@pytest.mark.parametrize("value", [None, "", "   ", 1])
def test_brightdata_requires_non_empty_configuration(field: str, value: object) -> None:
    values: dict[str, object] = {
        "api_url": "https://api.brightdata.test",
        "search_zone": "serp-zone",
        "fetch_zone": "fetch-zone",
    }
    values[field] = value
    with pytest.raises(TypeError):
        BrightDataAdapter(
            name="brightdata",
            api_url=values["api_url"],  # type: ignore[arg-type]
            search_zone=values["search_zone"],  # type: ignore[arg-type]
            fetch_zone=values["fetch_zone"],  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingHttpExecutor(),
        )


@pytest.mark.parametrize("body", ["", " \n\t"])
async def test_brightdata_fetch_rejects_empty_body(body: str) -> None:
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))
