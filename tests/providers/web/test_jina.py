import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.providers.contracts import URLFetchCandidate
from agent_search_gateway.providers.web.jina import JinaReaderAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingTextExecutor


async def test_jina_fetch_posts_normalized_target_with_no_cache_and_maps_text() -> None:
    sentinel = "# Title\n\nBody\n"
    executor = RecordingTextExecutor([sentinel])
    adapter = JinaReaderAdapter(
        name="jina",
        api_url="https://reader.example.test/",
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path/to/page?q=one&second=two#section")

    assert await adapter.fetch(target) == URLFetchCandidate(sentinel, sentinel)
    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://reader.example.test"
    assert request.stage == "fetch"
    assert request.headers == {"X-No-Cache": "true"}
    assert request.params is None
    assert request.json_body == {"url": str(target)}
    assert request.response_mode == "text"
    assert str(target) not in request.url


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_jina_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        JinaReaderAdapter(
            name="jina",
            api_url=api_url,  # type: ignore[arg-type]
            http_executor=RecordingTextExecutor([]),
        )


@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
async def test_jina_fetch_rejects_empty_page_body(body: str) -> None:
    adapter = JinaReaderAdapter(
        name="jina",
        api_url="https://reader.example.test",
        http_executor=RecordingTextExecutor([body]),
    )

    with pytest.raises(ExecutionFailure) as exc_info:
        await adapter.fetch(normalize_url("https://example.com/page"))

    assert exc_info.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert "jina/fetch: page body is empty" in str(exc_info.value)


@pytest.mark.parametrize(
    "api_url",
    [
        "https://jina.example.test",
        "https://jina.example.test/https://example.com/PRIVATE_PATH?key=PRIVATE_QUERY#PRIVATE_FRAGMENT",
    ],
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
    caplog: pytest.LogCaptureFixture, body: str, status: int, semantic: str, api_url: str
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
        adapter = JinaReaderAdapter(
            name="jina",
            api_url=api_url,
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="jina"),
        )
        target = normalize_url("https://example.com/page?key=PRIVATE_QUERY")
        if semantic == "success":
            assert await adapter.fetch(target) == URLFetchCandidate(body, body)
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(target)
            expected = "page body is empty" if semantic == "empty" else "HTTP status 403"
            assert caught.value.message == f"jina/fetch: {expected}"
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
