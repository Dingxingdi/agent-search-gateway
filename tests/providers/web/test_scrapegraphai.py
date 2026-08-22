import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.scrapegraphai import ScrapeGraphAIAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor


async def test_scrapegraphai_search_uses_v2_search_and_sgai_apikey() -> None:
    executor = RecordingJsonExecutor(
        [
            {
                "id": "search-id",
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "A",
                        "content": "# Full page\nbody",
                    }
                ],
            }
        ]
    )
    adapter = ScrapeGraphAIAdapter(
        name="scrapegraphai",
        api_url="https://v2-api.scrapegraph.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    assert await adapter.search("hello world") == [
        KeywordSearchHit(
            "https://example.com/a",
            "A",
            "",
            "# Full page\nbody",
            "# Full page\nbody",
        )
    ]
    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://v2-api.scrapegraph.test/api/search"
    assert request.stage == "search"
    assert request.headers == {"SGAI-APIKEY": "secret"}
    assert request.json_body == {"query": "hello world"}


async def test_scrapegraphai_fetch_uses_v2_scrape_markdown() -> None:
    executor = RecordingJsonExecutor(
        [{"id": "scrape-id", "results": {"markdown": {"data": ["# page\nbody"]}}}]
    )
    adapter = ScrapeGraphAIAdapter(
        name="scrapegraphai",
        api_url="https://v2-api.scrapegraph.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate("# page\nbody", "# page\nbody")
    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://v2-api.scrapegraph.test/api/scrape"
    assert request.stage == "fetch"
    assert request.headers == {"SGAI-APIKEY": "secret"}
    assert request.json_body == {"url": str(target), "formats": [{"type": "markdown"}]}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": {}},
        {"results": {"markdown": {}}},
        {"results": {"markdown": {"data": []}}},
        {"results": {"markdown": {"data": ["  "]}}},
        {"detail": "RAW_VENDOR_DIAGNOSTIC"},
    ],
)
async def test_scrapegraphai_fetch_rejects_missing_or_empty_markdown(payload: object) -> None:
    adapter = ScrapeGraphAIAdapter(
        name="scrapegraphai",
        api_url="https://v2-api.scrapegraph.test",
        secret=SecretValue("secret"),
        http_executor=RecordingJsonExecutor([payload]),
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com"))
    assert "RAW_VENDOR_DIAGNOSTIC" not in caught.value.message


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_scrapegraphai_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ScrapeGraphAIAdapter(
            name="scrapegraphai",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingJsonExecutor([]),
        )
