from urllib.parse import parse_qs, urlparse

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import URLFetchCandidate
from agent_search_gateway.providers.web.zenrows import ZenRowsAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingTextExecutor


async def test_zenrows_fetch_uses_current_fetch_api_and_requests_markdown() -> None:
    executor = RecordingTextExecutor(["# page\nbody"])
    adapter = ZenRowsAdapter(
        name="zenrows",
        api_url="https://api.zenrows.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate("# page\nbody", "# page\nbody")
    request = executor.requests[0]
    assert request.method == "GET"
    assert request.stage == "fetch"
    parsed = urlparse(request.url)
    assert parsed._replace(query="").geturl() == "https://api.zenrows.test/v1"
    assert parse_qs(parsed.query) == {
        "apikey": ["secret"],
        "url": [str(target)],
        "response_type": ["markdown"],
    }
    assert request.headers is None
    assert request.json_body is None
    assert "google" not in parsed.path


@pytest.mark.parametrize("body", ["", " \n"])
async def test_zenrows_fetch_rejects_empty_body(body: str) -> None:
    adapter = ZenRowsAdapter(
        name="zenrows",
        api_url="https://api.zenrows.test",
        secret=SecretValue("secret"),
        http_executor=RecordingTextExecutor([body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_zenrows_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ZenRowsAdapter(
            name="zenrows",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingTextExecutor([]),
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
    from tests.support.web_diagnostics import assert_text_fetch_diagnostics

    await assert_text_fetch_diagnostics(
        caplog,
        "zenrows",
        lambda executor: ZenRowsAdapter(
            name="zenrows",
            api_url="https://zenrows.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=executor,
        ),
        body,
        status,
        semantic,
    )
