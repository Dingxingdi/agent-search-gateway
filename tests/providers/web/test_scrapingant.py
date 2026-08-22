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
