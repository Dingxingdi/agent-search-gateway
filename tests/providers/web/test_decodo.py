import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.decodo import DecodoAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor


def _search_payload(results: list[object]) -> object:
    return {
        "results": [
            {
                "content": {
                    "results": {
                        "parse_status_code": 12000,
                        "results": {"organic": results},
                    },
                    "errors": [],
                    "status_code": 12000,
                },
                "status_code": 200,
            }
        ]
    }


async def test_decodo_search_uses_google_search_template_and_opaque_basic_token() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            _search_payload(
                [
                    {
                        "url": "https://example.com/a",
                        "title": "A",
                        "desc": "Snippet A",
                    }
                ]
            )
        ]
    )
    adapter = DecodoAdapter(
        name="decodo",
        api_url="https://scraper.decodo.test/",
        secret=SecretValue("opaque-token"),
        http_executor=executor,
    )

    assert await adapter.search("hello world") == [
        KeywordSearchHit("https://example.com/a", "A", "Snippet A")
    ]
    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://scraper.decodo.test/v2/scrape"
    assert request.stage == "search"
    assert request.headers == {"Authorization": "Basic opaque-token"}
    assert request.json_body == {"target": "google_search", "query": "hello world", "parse": True}


async def test_decodo_fetch_requests_markdown_with_same_opaque_basic_token() -> None:
    executor = RecordingHttpExecutor(text_responses=["# page\nbody"])
    adapter = DecodoAdapter(
        name="decodo",
        api_url="https://scraper.decodo.test/",
        secret=SecretValue("opaque-token"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate("# page\nbody", "# page\nbody")
    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://scraper.decodo.test/v2/scrape"
    assert request.stage == "fetch"
    assert request.headers == {"Authorization": "Basic opaque-token"}
    assert request.json_body == {"url": str(target), "markdown": True}


@pytest.mark.parametrize("body", ["", " \n"])
async def test_decodo_fetch_rejects_empty_body(body: str) -> None:
    adapter = DecodoAdapter(
        name="decodo",
        api_url="https://scraper.decodo.test",
        secret=SecretValue("opaque-token"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_decodo_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        DecodoAdapter(
            name="decodo",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("opaque-token"),
            http_executor=RecordingHttpExecutor(),
        )
