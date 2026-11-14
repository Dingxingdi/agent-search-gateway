import logging

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import (
    DiagnosticText,
    KeyValueFormatter,
    SecretRedactor,
    SecretValue,
)
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor
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


@pytest.mark.parametrize("status", [200, 503])
async def test_decodo_search_preserves_bounded_selected_error_diagnostics(
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        "results": [
            {
                "status_code": status,
                "content": {
                    "errors": [
                        {
                            "code": "parse_error",
                            "message": "parser unavailable",
                            "body": "PRIVATE_ERROR_BODY",
                        },
                        {"code": "PRIVATE_SECOND_CODE", "message": "PRIVATE_SECOND_ERROR"},
                        {"message": {"nested": "PRIVATE_NESTED"}},
                    ],
                    "results": {"results": {"organic": [{"message": "PRIVATE_CANDIDATE"}]}},
                },
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = DecodoAdapter(
            name="decodo",
            api_url="https://decodo.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="decodo"),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("PRIVATE_QUERY")
    assert caught.value.message == "decodo/search: provider reported failure"
    assert caught.value.diagnostics["upstream_status"] == DiagnosticText(str(status), 128)
    assert caught.value.diagnostics["upstream_code"] == DiagnosticText("parse_error", 128)
    assert caught.value.diagnostics["upstream_message"] == DiagnosticText("parser unavailable", 512)
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["http_status"] == 200
    assert diagnostics[0]["semantic_result"] == "error"
    formatter = KeyValueFormatter(SecretRedactor())
    rendered = "\n".join(formatter.format(record) for record in caplog.records)
    assert "parse_error" in rendered
    assert "parser unavailable" in rendered
    assert "PRIVATE_" not in rendered
    assert "provider_candidates_summary" not in rendered


@pytest.mark.parametrize("status", [12000, 503, "200"])
async def test_decodo_search_keeps_inner_status_failure_policy(
    status: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        "results": [
            {
                "status_code": status,
                "content": {
                    "errors": [],
                    "results": {"results": {"organic": []}},
                },
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = DecodoAdapter(
            name="decodo",
            api_url="https://decodo.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="decodo"),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")
    assert caught.value.message == "decodo/search: provider reported failure"
    assert caught.value.diagnostics["upstream_status"] == DiagnosticText(str(status), 128)
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["http_status"] == 200
    assert diagnostics[0]["upstream_status"].text == str(status)
    assert diagnostics[0]["category"] == "provider_response"
    assert diagnostics[0]["semantic_result"] == "error"
    assert not any(
        getattr(record, "gateway_event", None) == "provider_candidates_summary"
        for record in caplog.records
    )


async def test_decodo_search_keeps_known_status_on_malformed_organic_envelope() -> None:
    payload = {
        "results": [
            {
                "status_code": 200,
                "content": {
                    "errors": [],
                    "results": {"results": {"organic": {"body": "PRIVATE_ORGANIC"}}},
                },
            }
        ]
    }
    adapter = DecodoAdapter(
        name="decodo",
        api_url="https://decodo.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingHttpExecutor(json_responses=[payload]),
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("query")
    assert caught.value.message == "decodo/search: organic must be an array"
    assert caught.value.diagnostics["upstream_status"] == DiagnosticText("200", 128)
    assert "upstream_message" not in caught.value.diagnostics


@pytest.mark.parametrize(
    "errors",
    [
        [{"code": {"nested": "PRIVATE_CODE"}, "message": ["PRIVATE_MESSAGE"]}],
        [["PRIVATE_ARRAY"]],
        [{"body": {"message": "PRIVATE_UNKNOWN_BODY"}}],
    ],
)
async def test_decodo_search_never_expands_unknown_error_objects(
    errors: list[object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {"results": [{"status_code": 200, "content": {"errors": errors}}]}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = DecodoAdapter(
            name="decodo",
            api_url="https://decodo.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="decodo"),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")
    assert caught.value.message == "decodo/search: provider reported failure"
    assert caught.value.diagnostics["upstream_status"] == DiagnosticText("200", 128)
    assert "upstream_code" not in caught.value.diagnostics
    assert "upstream_message" not in caught.value.diagnostics
    formatter = KeyValueFormatter(SecretRedactor())
    rendered = "\n".join(formatter.format(record) for record in caplog.records)
    assert "semantic_result=error" in rendered
    assert "PRIVATE_" not in rendered


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
        "decodo",
        lambda executor: DecodoAdapter(
            name="decodo",
            api_url="https://decodo.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=executor,
        ),
        body,
        status,
        semantic,
    )
