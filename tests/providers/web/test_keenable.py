"""Offline Keenable keyed Search and Fetch contracts with synthetic fixtures."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor, ProviderResponseFailure
from agent_search_gateway.providers.web.keenable import KeenableAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor
from tests.support.web_diagnostics import assert_safe_response_diagnostic

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "keenable"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


async def test_search_maps_keyed_results() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == [
        KeywordSearchHit("https://EXAMPLE.com/one#section", "First page", "first"),
        KeywordSearchHit("https://example.com/two", "Second page", "second"),
    ]


async def test_search_sends_only_query_and_max_results() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    await adapter.search("合成查询")
    assert len(http.requests) == 1
    request = http.requests[0]
    assert (request.method, request.url, request.stage) == (
        "POST",
        "https://api.keenable.ai/v1/search",
        "search",
    )
    assert request.headers == {"X-API-Key": SecretValue("synthetic-key").reveal()}
    assert request.json_body == {"query": "合成查询", "max_results": 10}
    assert request.params is None


@pytest.mark.parametrize("snippet", [None, ""])
async def test_search_accepts_optional_snippet(snippet: object) -> None:
    http = RecordingJsonExecutor(
        [{"results": [{"url": "https://example.com/a", "title": "A", "snippet": snippet}]}]
    )
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == [KeywordSearchHit("https://example.com/a", "A")]


async def test_search_preserves_valid_results(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    http = RecordingJsonExecutor([_fixture("search_partial.json")])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == [
        KeywordSearchHit("https://EXAMPLE.com/good#x", "Good", "kept")
    ]
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0] == {
        "provider": "keenable",
        "stage": "search",
        "phase": "adapter",
        "received": 13,
        "accepted": 1,
        "rejected": 12,
        "rejection_counts": {"missing_url": 3, "invalid_record": 9},
        "processed": 13,
        "unprocessed": 0,
        "rewritten": 0,
    }


async def test_search_rejects_embedded_keyed_error(caplog: pytest.LogCaptureFixture) -> None:
    # Defensive synthetic placement of a documented flat error envelope at HTTP 200.
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_fixture("search_error.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="keenable"
            ),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.search("PRIVATE_QUERY")
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert caught.value.status_code == 200
    assert caught.value.safe_reason == "provider reported failure"
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    assert diagnostic["category"] == "provider_response"
    code = diagnostic["upstream_code"]
    assert isinstance(code, DiagnosticText)
    assert code.text == "invalid_api_key"


async def test_search_parser_rejects_explicit_error_without_inspector() -> None:
    http = RecordingJsonExecutor(
        [{"error": "PRIVATE_ERROR", "code": "invalid_api_key", "results": []}]
    )
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    with pytest.raises(ExecutionFailure, match="provider reported failure"):
        await adapter.search("合成查询")


@pytest.mark.parametrize("root", ["https://custom.example/api", "https://custom.example/api/"])
async def test_search_uses_configured_root(root: str) -> None:
    http = RecordingJsonExecutor([{"results": []}])
    adapter = KeenableAdapter(
        name="keenable", api_url=root, secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == []
    assert http.requests[0].url == "https://custom.example/api/v1/search"


async def test_search_returns_valid_empty_results(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    http = RecordingJsonExecutor([_fixture("search_empty.json")])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == []
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == summaries[0]["accepted"] == summaries[0]["rejected"] == 0


async def test_search_all_invalid_results(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    http = RecordingJsonExecutor([{"results": [None, {"url": "bad", "title": "A"}]}])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == []
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == summaries[0]["rejected"] == 2
    assert summaries[0]["accepted"] == 0
    assert summaries[0]["rejection_counts"] == {"invalid_record": 2}


@pytest.mark.parametrize(
    "payload",
    [None, [], "PRIVATE_BODY", {}, {"results": None}, {"results": {}}, {"results": "PRIVATE_BODY"}],
)
async def test_search_rejects_invalid_results_envelope(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("PRIVATE_QUERY")
    assert "PRIVATE_" not in caught.value.message


async def test_search_ignores_description_without_snippet() -> None:
    http = RecordingJsonExecutor(
        [{"results": [{"url": "https://example.com/a", "title": "", "description": "deprecated"}]}]
    )
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.search("合成查询") == [KeywordSearchHit("https://example.com/a")]


@pytest.mark.parametrize(
    "code",
    [
        "invalid_api_key",
        "insufficient_credits",
        "search_rate_limited",
        "search_failed_upstream",
        "PRIVATE_CODE",
        None,
        42,
    ],
)
async def test_search_business_errors_are_terminal_and_safe(
    caplog: pytest.LogCaptureFixture, code: object
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "error": "PRIVATE_ERROR",
                "message": "PRIVATE_MESSAGE",
                "code": code,
                "fix": "PRIVATE_FIX",
                "retryable": True,
                "results": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="keenable"
            ),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.search("PRIVATE_QUERY")
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    selected = diagnostic["upstream_code"]
    assert isinstance(selected, DiagnosticText)
    assert selected.text == (
        code if isinstance(code, str) and not code.startswith("PRIVATE_") else "unknown"
    )
    assert "upstream_message" not in diagnostic


@pytest.mark.parametrize("status", [401, 402])
async def test_search_never_falls_back_to_public_endpoint(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=_fixture("search_error.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("synthetic-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="keenable"
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("合成查询")
    assert not isinstance(caught.value, ProviderResponseFailure)
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert str(calls[0].url) == "https://api.keenable.ai/v1/search"
    assert calls[0].headers["X-API-Key"] == "synthetic-key"
    assert json.loads(calls[0].content) == {"query": "合成查询", "max_results": 10}


@pytest.mark.parametrize(
    ("fixture", "semantic", "category", "failure_type"),
    [
        ("search_success.json", "success", None, None),
        ("search_empty.json", "success", None, None),
        ("search_malformed.json", "unknown", "invalid_envelope", ExecutionFailure),
        ("invalid_json.txt", "unknown", "decode", ProtocolFailure),
    ],
)
async def test_search_response_diagnostics(
    caplog: pytest.LogCaptureFixture,
    fixture: str,
    semantic: str,
    category: str | None,
    failure_type: type[ExecutionFailure] | None,
) -> None:
    caplog.set_level(logging.DEBUG)
    response = httpx.Response(
        200,
        content=(_FIXTURES / fixture).read_bytes(),
        headers={"content-type": "application/json"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="keenable"
            ),
        )
        if failure_type is None:
            await adapter.search("PRIVATE_QUERY")
        else:
            with pytest.raises(failure_type) as caught:
                await adapter.search("PRIVATE_QUERY")
            assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    if category is not None:
        assert diagnostic["category"] == category
    assert "upstream_message" not in diagnostic


async def test_fetch_encodes_target_url_once() -> None:
    target = normalize_url("https://example.com/中文?q=a+b&next=中文#fragment")
    http = RecordingJsonExecutor([{"url": str(target), "content": "Body"}])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    await adapter.fetch(target)
    assert len(http.requests) == 1
    request = http.requests[0]
    parsed = httpx.URL(request.url)
    assert parsed.params["url"] == str(target)
    assert parsed.params.multi_items() == [("url", str(target))]
    assert parsed.path == "/v1/fetch"
    assert request.method == "GET"
    assert request.stage == "fetch"
    assert request.json_body is None
    assert request.params is None
    assert request.headers == {"X-API-Key": SecretValue("synthetic-key").reveal()}


async def test_fetch_maps_markdown_content() -> None:
    http = RecordingJsonExecutor([_fixture("fetch_success.json")])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    result = await adapter.fetch(normalize_url("https://example.com/final"))
    assert result.raw_content == result.content == "  # Synthetic page\n\nA neutral body.\n"


@pytest.mark.parametrize("content", [None, "", " \n\t "])
async def test_fetch_rejects_empty_content(content: object) -> None:
    payload = {"url": "https://example.com/final", "content": content}
    http = RecordingJsonExecutor([payload])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/a"))
    assert caught.value.diagnostics["semantic_result"] == "empty"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "PRIVATE_BODY",
        {"url": "https://example.com/a", "content": 42},
        {"url": "https://example.com/a", "content": {"message": "PRIVATE_BODY"}},
    ],
)
async def test_fetch_rejects_invalid_envelope(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/a"))
    assert caught.value.diagnostics["category"] == "invalid_envelope"
    assert "PRIVATE_" not in caught.value.message


@pytest.mark.parametrize(
    "returned_url", [None, "", " ", 42, "PRIVATE_URL", "ftp://example.com/file"]
)
async def test_fetch_requires_valid_returned_url(returned_url: object) -> None:
    http = RecordingJsonExecutor([{"url": returned_url, "content": "PRIVATE_BODY"}])
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/a"))
    assert caught.value.diagnostics["category"] == "invalid_envelope"
    assert "PRIVATE_" not in caught.value.message


async def test_fetch_parser_prioritizes_flat_error_without_inspector() -> None:
    http = RecordingJsonExecutor(
        [
            {
                "error": "PRIVATE_ERROR",
                "code": "invalid_api_key",
                "url": "https://example.com/a",
                "content": "PRIVATE_BODY",
            }
        ]
    )
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    with pytest.raises(ExecutionFailure, match="provider reported failure") as caught:
        await adapter.fetch(normalize_url("https://example.com/a"))
    assert caught.value.diagnostics["category"] == "provider_response"
    assert "PRIVATE_" not in caught.value.message


@pytest.mark.parametrize("code", ["content_not_extracted", "target_timeout", "PRIVATE_CODE"])
async def test_fetch_business_errors_are_terminal_and_safe(
    caplog: pytest.LogCaptureFixture, code: str
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "error": "PRIVATE_ERROR",
                "message": "PRIVATE_MESSAGE",
                "fix": "PRIVATE_FIX",
                "code": code,
                "retryable": True,
                "url": "https://example.com/a",
                "content": "PRIVATE_BODY",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="keenable"
            ),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/a?secret=PRIVATE_TARGET"))
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert caught.value.status_code == 200
    assert caught.value.safe_reason == "provider reported failure"
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    assert diagnostic["category"] == "provider_response"
    selected = diagnostic["upstream_code"]
    assert isinstance(selected, DiagnosticText)
    assert selected.text == ("unknown" if code == "PRIVATE_CODE" else code)
    assert "upstream_message" not in diagnostic


async def test_fetch_allows_documented_final_url() -> None:
    http = RecordingJsonExecutor(
        [{"url": "https://example.org/redirected", "content": "Plain text without a heading."}]
    )
    adapter = KeenableAdapter(
        name="keenable", secret=SecretValue("synthetic-key"), http_executor=http
    )
    result = await adapter.fetch(normalize_url("https://example.com/original"))
    assert result.raw_content == result.content == "Plain text without a heading."


@pytest.mark.parametrize("root", ["https://custom.example/api", "https://custom.example/api/"])
async def test_fetch_omits_live_and_uses_keyed_endpoint(root: str) -> None:
    http = RecordingJsonExecutor([_fixture("fetch_success.json")])
    adapter = KeenableAdapter(
        name="keenable", api_url=root, secret=SecretValue("synthetic-key"), http_executor=http
    )
    await adapter.fetch(normalize_url("https://example.com/a"))
    request = http.requests[0]
    assert request.url == "https://custom.example/api/v1/fetch?url=https%3A%2F%2Fexample.com%2Fa"
    assert request.json_body is None
    assert list(httpx.URL(request.url).params) == ["url"]


@pytest.mark.parametrize(
    "payload,semantic",
    [
        ({"url": "https://example.com/a"}, "empty"),
        (_fixture("fetch_empty.json"), "empty"),
        ({"content": "PRIVATE_BODY"}, "error"),
        (_fixture("fetch_error.json"), "error"),
    ],
)
async def test_fetch_fixture_failures_have_safe_diagnostics(
    caplog: pytest.LogCaptureFixture, payload: object, semantic: str
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="keenable"
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/a?secret=PRIVATE_TARGET"))
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    assert diagnostic["category"] == (
        "provider_response"
        if isinstance(payload, dict) and "error" in payload
        else "invalid_envelope"
    )


@pytest.mark.parametrize("status,attempts", [(402, 1), (422, 1), (429, 2), (500, 2)])
async def test_fetch_uses_shared_http_attempt_budget(status: int, attempts: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=_fixture("fetch_error.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KeenableAdapter(
            name="keenable",
            secret=SecretValue("synthetic-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="keenable"
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/a"))
    assert len(calls) == attempts
    assert not isinstance(caught.value, ProviderResponseFailure)
    assert all(request.method == "GET" and request.url.path == "/v1/fetch" for request in calls)
