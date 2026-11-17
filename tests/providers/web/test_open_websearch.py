"""Offline daemon Search contracts with neutral synthetic fixtures."""

import asyncio
import json
import logging
from pathlib import Path
from typing import cast

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor, ProviderResponseFailure
from agent_search_gateway.providers.web.open_websearch import OpenWebSearchAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor
from tests.support.web_diagnostics import assert_safe_response_diagnostic

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "open_websearch"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


async def test_search_omits_engines_when_not_configured() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    await adapter.search("合成查询")
    assert len(http.requests) == 1
    request = http.requests[0]
    assert (request.method, request.url, request.stage) == (
        "POST",
        "http://127.0.0.1:3210/search",
        "search",
    )
    assert request.headers is None
    assert request.params is None
    assert request.json_body == {"query": "合成查询"}


async def test_search_sends_configured_engines_with_max_total_limit() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(
        name="open_websearch", http_executor=http, engines=["bing", "duckduckgo"]
    )
    await adapter.search("query")
    assert len(http.requests) == 1
    assert http.requests[0].json_body == {
        "query": "query",
        "engines": ["bing", "duckduckgo"],
        "limit": 50,
    }


@pytest.mark.parametrize(
    "engines", [[], "bing,duckduckgo", {}, 1, True, [""], ["   "], ["bing", 1], [None]]
)
def test_engines_rejects_invalid_structure(engines: object) -> None:
    http = RecordingJsonExecutor([])
    with pytest.raises(
        TypeError, match="^engines must be a non-empty list of non-blank strings or None$"
    ):
        OpenWebSearchAdapter(
            name="open_websearch", http_executor=http, engines=cast(list[str], engines)
        )
    assert http.requests == []


async def test_search_preserves_opaque_engine_names_order_and_duplicates() -> None:
    engines = ["custom_z", "custom_a", "custom_z", "all", " custom_b "]
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http, engines=engines)
    await adapter.search("query")
    assert http.requests[0].json_body == {"query": "query", "engines": engines, "limit": 50}


async def test_engines_none_preserves_default_request() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http, engines=None)
    await adapter.search("query")
    assert http.requests[0].json_body == {"query": "query"}


async def test_engines_snapshot_ignores_later_input_mutation() -> None:
    engines = ["bing", "duckduckgo"]
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http, engines=engines)
    engines[0] = "changed"
    engines.append("extra")
    await adapter.search("query")
    assert http.requests[0].json_body == {
        "query": "query",
        "engines": ["bing", "duckduckgo"],
        "limit": 50,
    }


async def test_search_builds_a_fresh_engines_list_each_time() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json"), _fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(
        name="open_websearch", http_executor=http, engines=["bing", "duckduckgo"]
    )
    await adapter.search("first")
    body = http.requests[0].json_body
    assert isinstance(body, dict)
    engines = body["engines"]
    assert isinstance(engines, list)
    engines[0] = "changed"
    engines.append("extra")
    await adapter.search("second")
    assert http.requests[1].json_body == {
        "query": "second",
        "engines": ["bing", "duckduckgo"],
        "limit": 50,
    }


async def test_search_maps_result_description() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    assert await adapter.search("query") == [
        KeywordSearchHit("https://EXAMPLE.com/one#section", "First page", "first"),
        KeywordSearchHit("https://example.com/two", "Second page", "second"),
    ]
    assert adapter.name == "open_websearch"


async def test_search_accepts_empty_results() -> None:
    http = RecordingJsonExecutor([_fixture("search_empty.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    assert await adapter.search("query") == []


async def test_search_preserves_valid_records_and_counts_rejections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    http = RecordingJsonExecutor([_fixture("search_partial.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    assert await adapter.search("query") == [
        KeywordSearchHit("https://EXAMPLE.com/good#x", "Good", "kept")
    ]
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0] == {
        "provider": "open_websearch",
        "stage": "search",
        "phase": "adapter",
        "received": 9,
        "accepted": 1,
        "rejected": 8,
        "rejection_counts": {"missing_url": 3, "invalid_record": 5},
        "processed": 9,
        "unprocessed": 0,
        "rewritten": 0,
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "invalid",
        {},
        {"status": "ok"},
        {"status": "ok", "data": []},
        {"status": "ok", "data": {"results": None}},
        {"status": "ok", "data": {"results": "invalid"}},
    ],
)
async def test_search_rejects_invalid_containers(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("query")
    assert caught.value.diagnostics["category"] == "invalid_envelope"


async def test_search_rejects_malformed_fixture() -> None:
    http = RecordingJsonExecutor([_fixture("search_malformed.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure, match="data.results must be an array"):
        await adapter.search("query")


async def _search_response(
    caplog: pytest.LogCaptureFixture, payload: object
) -> tuple[list[KeywordSearchHit], dict[str, object]]:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="open_websearch"
            ),
            engines=["bing", "duckduckgo"],
        )
        hits = await adapter.search("PRIVATE_QUERY")
    assert len(calls) == 1
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "success")
    events = [getattr(record, "gateway_event", None) for record in caplog.records]
    assert "http_retry" not in events
    assert "http_failed" not in events
    return hits, diagnostic


async def test_search_keeps_results_when_some_engines_fail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hits, diagnostic = await _search_response(caplog, _fixture("search_partial_engines.json"))
    assert hits == [KeywordSearchHit("https://example.com/kept", "Kept", "kept")]
    assert diagnostic["category"] == "provider_response"
    assert diagnostic["response_kind"] == "object"
    code = diagnostic["upstream_code"]
    assert isinstance(code, DiagnosticText)
    assert code.text == "partial_engine_failures"
    message = diagnostic["upstream_message"]
    assert isinstance(message, DiagnosticText)
    assert message.text == "partial_engine_failures count=1"


async def test_search_marks_engine_failures_even_when_results_are_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hits, diagnostic = await _search_response(caplog, _fixture("search_all_engines_failed.json"))
    assert hits == []
    code = diagnostic["upstream_code"]
    assert isinstance(code, DiagnosticText)
    assert code.text == "partial_engine_failures"
    message = diagnostic["upstream_message"]
    assert isinstance(message, DiagnosticText)
    assert message.text == "partial_engine_failures count=2"


@pytest.mark.parametrize(
    "data",
    [
        {"results": []},
        {"results": [], "partialFailures": []},
        {"results": [], "partialFailures": "PRIVATE_FAILURE"},
        {"results": [], "partialFailures": {"engine": "PRIVATE_ENGINE"}},
    ],
)
async def test_search_omits_engine_failure_marker_without_failures(
    caplog: pytest.LogCaptureFixture,
    data: dict[str, object],
) -> None:
    hits, diagnostic = await _search_response(caplog, {"status": "ok", "data": data})
    assert hits == []
    assert "upstream_code" not in diagnostic
    assert "upstream_message" not in diagnostic


async def test_search_redacts_engine_failure_details(caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "status": "ok",
        "data": {
            "query": "PRIVATE_QUERY",
            "engines": ["PRIVATE_ENGINE"],
            "results": [
                {
                    "url": "https://example.com/?key=PRIVATE_URL",
                    "title": "PRIVATE_TITLE",
                    "description": "PRIVATE_BODY",
                },
                {"title": "PRIVATE_REJECTED"},
            ],
            "partialFailures": [
                {
                    "engine": "PRIVATE_ENGINE",
                    "code": "PRIVATE_CODE",
                    "message": "PRIVATE_MESSAGE",
                    "hint": "PRIVATE_HINT",
                    "fix": "PRIVATE_FIX",
                },
                "PRIVATE_INNER",
                None,
            ],
        },
    }
    hits, diagnostic = await _search_response(caplog, payload)
    assert len(hits) == 1
    message = diagnostic["upstream_message"]
    assert isinstance(message, DiagnosticText)
    assert message.text == "partial_engine_failures count=3"
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == 2
    assert summaries[0]["accepted"] == 1
    assert summaries[0]["rejected"] == 1
    assert summaries[0]["rejection_counts"] == {"missing_url": 1}


async def test_search_rejects_error_status_even_with_results() -> None:
    http = RecordingJsonExecutor(
        [
            {
                "status": "error",
                "error": {"code": "engine_error", "message": "PRIVATE_MESSAGE"},
                "data": {"results": [{"url": "https://example.com/good", "title": "Good"}]},
            }
        ]
    )
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure, match="provider reported failure") as caught:
        await adapter.search("PRIVATE_QUERY")
    assert caught.value.diagnostics["semantic_result"] == "error"
    assert "PRIVATE_" not in str(caught.value)


@pytest.mark.parametrize("status", [None, "unknown", "OK", True, 1, [], {}])
async def test_search_rejects_unknown_status(status: object) -> None:
    payload: dict[str, object] = {"data": {"results": []}}
    if status is not None:
        payload["status"] = status
    http = RecordingJsonExecutor([payload])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("PRIVATE_QUERY")
    assert caught.value.diagnostics["category"] == "invalid_envelope"
    assert "PRIVATE_" not in str(caught.value)


@pytest.mark.parametrize(
    "code",
    [
        "PRIVATE_UNKNOWN",
        "browser_unavailable",
        "invalid_request",
        "engine_error",
        "validation_failed",
        "fetch_failed",
        None,
        [],
        42,
    ],
)
async def test_search_does_not_retry_unknown_daemon_error(
    caplog: pytest.LogCaptureFixture,
    code: object,
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []
    payload = {
        "status": "error",
        "error": {
            "code": code,
            "message": "PRIVATE_MESSAGE",
            "hint": "PRIVATE_HINT",
            "fix": "PRIVATE_FIX",
            "retryable": True,
        },
        "data": {"results": [{"url": "https://example.com/PRIVATE_URL", "title": "PRIVATE_BODY"}]},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="open_websearch"
            ),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.search("PRIVATE_QUERY")
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert "PRIVATE_" not in str(caught.value)
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    assert diagnostic["category"] == "provider_response"
    selected = diagnostic["upstream_code"]
    assert isinstance(selected, DiagnosticText)
    expected = code if isinstance(code, str) and code != "PRIVATE_UNKNOWN" else "unknown"
    assert selected.text == expected
    assert "upstream_message" not in diagnostic
    events = [getattr(record, "gateway_event", None) for record in caplog.records]
    assert events.count("http_failed") == 1
    assert "http_retry" not in events


@pytest.mark.parametrize("api_url", ["", "   ", None, 1, True, [], {}])
def test_api_url_rejects_invalid_configuration(api_url: object) -> None:
    http = RecordingJsonExecutor([])
    with pytest.raises(TypeError, match="^api_url must be a non-empty string$"):
        OpenWebSearchAdapter(name="open_websearch", api_url=cast(str, api_url), http_executor=http)
    assert http.requests == []


async def test_search_uses_configured_base_url() -> None:
    http = RecordingJsonExecutor([_fixture("search_empty.json")])
    adapter = OpenWebSearchAdapter(
        name="open_websearch", api_url=" https://daemon.example.test/base/ ", http_executor=http
    )
    await adapter.search("query")
    assert http.requests[0].url == "https://daemon.example.test/base/search"


@pytest.mark.parametrize(
    ("fixture", "failure_type", "semantic"),
    [
        ("search_error.json", ProviderResponseFailure, "error"),
        ("invalid_json.txt", ProtocolFailure, "unknown"),
    ],
)
async def test_search_failure_fixtures_use_executor(
    caplog: pytest.LogCaptureFixture,
    fixture: str,
    failure_type: type[ExecutionFailure],
    semantic: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            text=(_FIXTURES / fixture).read_text(),
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="open_websearch"
            ),
        )
        with pytest.raises(failure_type):
            await adapter.search("PRIVATE_QUERY")
    assert len(calls) == 1
    assert_safe_response_diagnostic(caplog, semantic)


async def test_search_leaves_non_200_error_to_http_executor() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json=_fixture("search_error.json"))
        return httpx.Response(200, json=_fixture("search_empty.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="open_websearch"
            ),
            engines=["bing", "duckduckgo"],
        )
        assert await adapter.search("query") == []
    assert len(calls) == 2
    assert [json.loads(request.content) for request in calls] == [
        {"query": "query", "engines": ["bing", "duckduckgo"], "limit": 50},
        {"query": "query", "engines": ["bing", "duckduckgo"], "limit": 50},
    ]


async def test_fetch_maps_daemon_content() -> None:
    http = RecordingJsonExecutor([_fixture("fetch_success.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    target = normalize_url("https://example.com/page?key=value#section")
    candidate = await adapter.fetch(target)
    content = "  # Synthetic page\n\nNeutral content.\n"
    assert candidate == URLFetchCandidate(content, content)
    assert len(http.requests) == 1
    request = http.requests[0]
    assert (request.method, request.url, request.stage) == (
        "POST",
        "http://127.0.0.1:3210/fetch-web",
        "fetch",
    )
    assert request.json_body == {"url": str(target)}
    assert request.headers is None
    assert request.params is None


async def test_fetch_omits_search_options_when_engines_are_configured() -> None:
    http = RecordingJsonExecutor([_fixture("fetch_success.json")])
    adapter = OpenWebSearchAdapter(
        name="open_websearch",
        api_url="https://daemon.example.test/base/",
        http_executor=http,
        engines=["bing", "duckduckgo"],
    )
    target = normalize_url("https://example.com/page")
    await adapter.fetch(target)
    assert len(http.requests) == 1
    assert http.requests[0].url == "https://daemon.example.test/base/fetch-web"
    assert http.requests[0].json_body == {"url": str(target)}
    assert http.requests[0].headers is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"content": None},
        {"content": ""},
        {"content": " \n\t"},
        {"title": "Synthetic", "finalUrl": "https://example.com/final"},
    ],
)
async def test_fetch_rejects_empty_content(data: dict[str, object]) -> None:
    http = RecordingJsonExecutor([{"status": "ok", "data": data}])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/page"))
    assert caught.value.diagnostics["semantic_result"] == "empty"


@pytest.mark.parametrize("content", [1, True, [], {}])
async def test_fetch_rejects_non_string_content(content: object) -> None:
    http = RecordingJsonExecutor([{"status": "ok", "data": {"content": content}}])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/page"))
    assert caught.value.diagnostics["category"] == "invalid_envelope"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"status": "unknown", "data": {"content": "body"}},
        {"status": "ok"},
        {"status": "ok", "data": None},
        {"status": "ok", "data": []},
        {"status": True, "data": {"content": "body"}},
    ],
)
async def test_fetch_rejects_invalid_envelopes(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/page"))
    assert caught.value.diagnostics["category"] == "invalid_envelope"


@pytest.mark.parametrize(
    "code",
    [
        "browser_unavailable",
        "invalid_request",
        "engine_error",
        "validation_failed",
        "fetch_failed",
        "PRIVATE_UNKNOWN",
        None,
        [],
        42,
    ],
)
async def test_fetch_browser_unavailable_is_terminal_and_redacts_details(
    caplog: pytest.LogCaptureFixture, code: object
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = cast(dict[str, object], _fixture("fetch_error.json"))
    payload["error"] = {
        "code": code,
        "message": "PRIVATE_MESSAGE",
        "hint": "PRIVATE_HINT",
        "details": "PRIVATE_DETAILS",
        "retryable": True,
    }
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="open_websearch"
            ),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/PRIVATE_URL"))
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert "PRIVATE_" not in str(caught.value)
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    selected = diagnostic["upstream_code"]
    assert isinstance(selected, DiagnosticText)
    expected = code if isinstance(code, str) and code != "PRIVATE_UNKNOWN" else "unknown"
    assert selected.text == expected
    assert "upstream_message" not in diagnostic
    events = [getattr(record, "gateway_event", None) for record in caplog.records]
    assert events.count("http_failed") == 1
    assert "http_retrying" not in events


@pytest.mark.parametrize(
    ("fixture", "semantic"), [("fetch_success.json", "success"), ("fetch_empty.json", "empty")]
)
async def test_fetch_reports_response_diagnostics(
    caplog: pytest.LogCaptureFixture, fixture: str, semantic: str
) -> None:
    caplog.set_level(logging.DEBUG)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture(fixture))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="open_websearch"
            ),
        )
        if semantic == "empty":
            with pytest.raises(ExecutionFailure):
                await adapter.fetch(normalize_url("https://example.com/page"))
        else:
            await adapter.fetch(normalize_url("https://example.com/page"))
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    assert diagnostic["stage"] == "fetch"
    if semantic == "success":
        assert diagnostic["response_kind"] == "object"


@pytest.mark.parametrize("operation", ["search", "fetch"])
@pytest.mark.parametrize("max_attempts", [1, 3])
async def test_connection_refusal_uses_shared_retry_budget(
    operation: str, max_attempts: int
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("Synthetic connection refusal", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(max_attempts, 0, 0, 5), provider_name="open_websearch"
            ),
        )
        with pytest.raises(ExecutionFailure):
            if operation == "search":
                await adapter.search("Synthetic query")
            else:
                await adapter.fetch(normalize_url("https://example.com/page"))
    assert len(calls) == max_attempts
    suffix = "/search" if operation == "search" else "/fetch-web"
    assert {request.url.path for request in calls} == {suffix}
    assert {request.method for request in calls} == {"POST"}


async def test_fetch_cancellation_propagates() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.CancelledError("Synthetic cancellation")
    calls: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        entered.set()
        await release.wait()
        raise cancelled

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="open_websearch"
            ),
        )
        task = asyncio.create_task(adapter.fetch(normalize_url("https://example.com/page")))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value is cancelled
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 1
    assert calls[0].url.path == "/fetch-web"


async def test_fetch_rejects_error_status_even_with_content() -> None:
    http = RecordingJsonExecutor([_fixture("fetch_error.json")])
    adapter = OpenWebSearchAdapter(name="open_websearch", http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/page"))
    assert caught.value.diagnostics["semantic_result"] == "error"


async def test_fetch_leaves_non_200_error_to_http_executor() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            500 if len(calls) == 1 else 200,
            json=_fixture("fetch_error.json" if len(calls) == 1 else "fetch_success.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenWebSearchAdapter(
            name="open_websearch",
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="open_websearch"
            ),
            engines=["bing", "duckduckgo"],
        )
        candidate = await adapter.fetch(normalize_url("https://example.com/page"))
    assert candidate.raw_content == "  # Synthetic page\n\nNeutral content.\n"
    assert len(calls) == 2
    assert [json.loads(request.content) for request in calls] == [
        {"url": "https://example.com/page"}
    ] * 2
