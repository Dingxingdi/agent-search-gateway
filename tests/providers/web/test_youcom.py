"""Offline You.com Search and Contents contracts using neutral synthetic fixtures."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor, ProviderResponseFailure
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor
from tests.support.web_diagnostics import assert_safe_response_diagnostic

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "youcom"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


async def test_search_joins_top_level_result_snippets() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.search("合成查询") == [
        KeywordSearchHit("https://EXAMPLE.com/one#section", "First page", "first\n\nsecond"),
        KeywordSearchHit("https://example.com/two", "Second page", "third"),
    ]


async def test_search_omits_optional_extraction() -> None:
    adapter_key = "synthetic-key"
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    await adapter.search("合成查询")
    assert len(http.requests) == 1
    request = http.requests[0]
    assert (request.method, request.url, request.stage) == (
        "POST",
        "https://ydc-index.io/v1/search",
        "search",
    )
    assert request.headers == {"X-API-Key": adapter_key}
    assert request.json_body == {"query": "合成查询", "count": 10}
    assert request.params is None


@pytest.mark.parametrize("extra", [{}, {"snippets": None}, {"snippets": []}])
async def test_search_accepts_optional_snippets(extra: dict[str, object]) -> None:
    http = RecordingJsonExecutor(
        [
            {
                "results": {
                    "web": [
                        {
                            "url": "https://example.com/optional",
                            "description": "not a snippet",
                            "contents": {"snippets": ["not a snippet"], "markdown": "not content"},
                            **extra,
                        }
                    ]
                }
            }
        ]
    )
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.search("合成查询") == [KeywordSearchHit("https://example.com/optional")]


async def test_search_rejects_only_invalid_records(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    http = RecordingJsonExecutor([_fixture("search_partial.json")])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.search("合成查询") == [
        KeywordSearchHit("https://example.com/good", "Good", "kept")
    ]
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "youcom",
            "stage": "search",
            "phase": "adapter",
            "received": 14,
            "accepted": 1,
            "rejected": 13,
            "rejection_counts": {"missing_url": 3, "invalid_record": 10},
            "processed": 14,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]


async def test_search_rejects_defensive_200_error(caplog: pytest.LogCaptureFixture) -> None:
    # A documented non-2xx error shape moved to HTTP 200 as a defensive synthetic case.
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_fixture("search_error.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="youcom"),
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
    selected_code = diagnostic["upstream_code"]
    assert isinstance(selected_code, DiagnosticText)
    assert selected_code.text == "unauthorized"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"detail": "PRIVATE_DETAIL"}, "unknown"),
        ({"error": "unauthorized", "message": "PRIVATE_MESSAGE"}, "unauthorized"),
        ({"error": "payment_required"}, "payment_required"),
        ({"error": "forbidden"}, "forbidden"),
        ({"error": "invalid_request"}, "invalid_request"),
        ({"error": "spend_cap_exceeded"}, "spend_cap_exceeded"),
        ({"error": "PRIVATE_CODE", "retryable": True}, "unknown"),
        (
            {"error": {"code": "PRIVATE_CODE", "message": "PRIVATE_MESSAGE"}, "retryable": True},
            "unknown",
        ),
        ({"error": {"message": "PRIVATE_MESSAGE"}}, "unknown"),
    ],
)
async def test_search_errors_are_terminal_and_safe(
    caplog: pytest.LogCaptureFixture,
    payload: object,
    code: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="youcom"),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.search("PRIVATE_QUERY")
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    selected_code = diagnostic["upstream_code"]
    assert isinstance(selected_code, DiagnosticText)
    assert selected_code.text == code


@pytest.mark.parametrize(
    "error",
    [
        "unauthorized",
        {"code": "forbidden"},
        {"message": "PRIVATE_MESSAGE"},
    ],
)
async def test_search_parser_rejects_explicit_error_with_results(error: object) -> None:
    http = RecordingJsonExecutor([{"error": error, "results": {"web": []}}])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("PRIVATE_QUERY")
    assert caught.value.diagnostics["semantic_result"] == "error"
    assert "PRIVATE_" not in caught.value.message


@pytest.mark.parametrize("root", ["https://example.com/api", "https://example.com/api/"])
async def test_search_uses_custom_root(root: str) -> None:
    http = RecordingJsonExecutor([_fixture("search_empty.json")])
    adapter = YouComAdapter(
        name="custom",
        api_url=root,
        secret=SecretValue("synthetic-key"),
        http_executor=http,
    )
    assert await adapter.search("合成查询") == []
    assert http.requests[0].url == "https://example.com/api/v1/search"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "invalid",
        {},
        {"results": None},
        {"results": []},
        {"results": {}},
        {"results": {"web": None}},
        {"results": {"web": {}}},
    ],
)
async def test_search_rejects_invalid_envelopes(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure):
        await adapter.search("合成查询")


@pytest.mark.parametrize(
    "metadata",
    [
        {"detail": "PRIVATE_METADATA"},
        {"message": "PRIVATE_METADATA", "code": "unauthorized"},
        {"error": {"metrics": 1}},
        {"error": None},
    ],
)
async def test_search_does_not_misclassify_normal_metadata(
    caplog: pytest.LogCaptureFixture,
    metadata: dict[str, object],
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": {"web": []}, **metadata})
        )
    ) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="youcom"),
        )
        assert await adapter.search("PRIVATE_QUERY") == []
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "success")
    assert diagnostic["response_kind"] == "object"
    assert "upstream_code" not in diagnostic


@pytest.mark.parametrize(
    ("fixture", "semantic", "category"),
    [
        ("search_success.json", "success", None),
        ("search_empty.json", "success", None),
        ("search_malformed.json", "unknown", "invalid_envelope"),
        ("invalid_json.txt", "unknown", "decode"),
    ],
)
async def test_search_classifies_response_diagnostics(
    caplog: pytest.LogCaptureFixture,
    fixture: str,
    semantic: str,
    category: str | None,
) -> None:
    caplog.set_level(logging.DEBUG)
    body = (_FIXTURES / fixture).read_text()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text=body, headers={"content-type": "application/json"}
            )
        )
    ) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="youcom"),
        )
        if semantic == "success":
            await adapter.search("PRIVATE_QUERY")
        else:
            expected = ProtocolFailure if category == "decode" else ExecutionFailure
            with pytest.raises(expected):
                await adapter.search("PRIVATE_QUERY")
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    if category is not None:
        assert diagnostic["category"] == category


async def test_search_leaves_non_200_status_to_executor() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("synthetic-key"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="youcom"),
        )
        with pytest.raises(ExecutionFailure, match="HTTP status 401") as caught:
            await adapter.search("合成查询")
    assert not isinstance(caught.value, ProviderResponseFailure)
    assert len(calls) == 1


async def test_fetch_selects_matching_markdown_from_root_array() -> None:
    adapter_key = "synthetic-key"
    http = RecordingJsonExecutor([_fixture("fetch_success.json")])
    adapter = YouComAdapter(name="youcom", secret=SecretValue(adapter_key), http_executor=http)
    target = normalize_url("https://example.com/page")
    markdown = "  # Page\n\nSelected body.\n"
    assert await adapter.fetch(target) == URLFetchCandidate(markdown, markdown)
    assert len(http.requests) == 1
    request = http.requests[0]
    assert (request.method, request.url, request.stage) == (
        "POST",
        "https://ydc-index.io/v1/contents",
        "fetch",
    )
    assert request.headers == {"X-API-Key": adapter_key}
    assert request.json_body == {"urls": [str(target)], "formats": ["markdown"]}
    assert request.params is None


@pytest.mark.parametrize("root", ["https://example.com/api", "https://example.com/api/"])
async def test_fetch_uses_custom_root_and_normalized_match(root: str) -> None:
    http = RecordingJsonExecutor([[{"url": " https://EXAMPLE.com/page ", "markdown": "body"}]])
    adapter = YouComAdapter(
        name="custom", api_url=root, secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.fetch(normalize_url("https://example.com/page")) == URLFetchCandidate(
        "body", "body"
    )
    assert http.requests[0].url == "https://example.com/api/v1/contents"


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"markdown": None},
        {"markdown": ""},
        {"markdown": " \n\t "},
        {"html": "<p>not markdown</p>"},
        {"markdown": 12},
        {"markdown": {"text": "not markdown"}},
    ],
)
async def test_fetch_rejects_unusable_markdown(record: dict[str, object]) -> None:
    http = RecordingJsonExecutor([[{"url": "https://example.com/page", **record}]])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure):
        await adapter.fetch(normalize_url("https://example.com/page"))


@pytest.mark.parametrize(
    "item",
    [None, "not a record", 7, [], {}, {"url": ""}, {"url": "not a URL"}, {"url": 7}],
)
async def test_fetch_skips_invalid_records_before_valid_match(item: object) -> None:
    target = normalize_url("https://example.com/page")
    http = RecordingJsonExecutor([[item, {"url": str(target), "markdown": "body"}]])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.fetch(target) == URLFetchCandidate("body", "body")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        "not an array",
        {"url": "https://example.com/page", "markdown": "body"},
        [],
        [None, "not a record", {"url": "not a URL", "markdown": "body"}],
        [{"url": "https://example.com/other", "markdown": "body"}],
        [
            {
                "url": "https://example.com/other",
                "final_url": "https://example.com/page",
                "markdown": "body",
            }
        ],
    ],
)
async def test_fetch_rejects_payload_without_usable_matching_record(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure):
        await adapter.fetch(normalize_url("https://example.com/page"))


async def test_fetch_accepts_later_nonempty_matching_record() -> None:
    target = normalize_url("https://example.com/page")
    http = RecordingJsonExecutor(
        [[{"url": str(target), "markdown": "  "}, {"url": str(target), "markdown": "body"}]]
    )
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.fetch(target) == URLFetchCandidate("body", "body")


async def test_fetch_reports_array_success_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    target = normalize_url("https://example.com/page?key=PRIVATE_QUERY")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=[{"url": str(target), "markdown": "PRIVATE_BODY"}]
            )
        )
    ) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="youcom"),
        )
        assert await adapter.fetch(target) == URLFetchCandidate("PRIVATE_BODY", "PRIVATE_BODY")
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "success")
    assert diagnostic["response_kind"] == "array"
    assert diagnostic["stage"] == "fetch"


@pytest.mark.parametrize(
    ("payload", "semantic", "kind"),
    [
        (_fixture("fetch_empty.json"), "empty", "array"),
        ([{"url": "https://example.com/page"}], "empty", "array"),
        ([{"url": "https://example.com/page", "markdown": 7}], "empty", "array"),
        ([], "no_match", "array"),
        ([{"url": "https://example.com/other", "markdown": "PRIVATE_BODY"}], "no_match", "array"),
        ([None, {"url": "not a URL"}], "no_match", "array"),
        ({}, "error", None),
        ("PRIVATE_CONTAINER", "error", None),
    ],
)
async def test_fetch_classifies_unsuccessful_payload_diagnostics(
    caplog: pytest.LogCaptureFixture, payload: object, semantic: str, kind: str | None
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="youcom"),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/page"))
    assert caught.value.diagnostics["semantic_result"] == semantic
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    assert diagnostic["category"] == "invalid_envelope"
    if kind is not None:
        assert diagnostic["response_kind"] == kind


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (_fixture("fetch_error.json"), "invalid_request"),
        ({"detail": "PRIVATE_DETAIL"}, "unknown"),
        ({"error": "PRIVATE_ERROR", "code": "invalid_request"}, "invalid_request"),
        ({"error": "PRIVATE_ERROR", "code": "PRIVATE_CODE", "retryable": True}, "unknown"),
        ({"error": {"code": "PRIVATE_CODE", "message": "PRIVATE_MESSAGE"}}, "unknown"),
    ],
)
async def test_fetch_rejects_defensive_200_error(
    caplog: pytest.LogCaptureFixture, payload: object, code: str
) -> None:
    # A documented non-2xx error shape moved to HTTP 200 as a defensive synthetic case.
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = YouComAdapter(
            name="youcom",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="youcom"),
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/page?key=PRIVATE_QUERY"))
    assert len(calls) == 1
    assert caught.value.retryable is False
    assert caught.value.status_code == 200
    assert caught.value.safe_reason == "provider reported failure"
    assert "PRIVATE_" not in caught.value.message
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    assert diagnostic["stage"] == "fetch"
    assert diagnostic["category"] == "provider_response"
    selected_code = diagnostic["upstream_code"]
    assert isinstance(selected_code, DiagnosticText)
    assert selected_code.text == code


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"detail": "PRIVATE_DETAIL"}, "unknown"),
        ({"error": "PRIVATE_ERROR", "code": "invalid_request"}, "invalid_request"),
        ({"error": {"code": "PRIVATE_CODE", "message": "PRIVATE_MESSAGE"}}, "unknown"),
        ({"error": "forbidden", "results": {"web": []}}, "forbidden"),
    ],
)
async def test_fetch_parser_rejects_safe_business_errors(payload: object, code: str) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = YouComAdapter(name="youcom", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/page?key=PRIVATE_QUERY"))
    assert caught.value.diagnostics["category"] == "provider_response"
    assert caught.value.diagnostics["semantic_result"] == "error"
    selected_code = caught.value.diagnostics["upstream_code"]
    assert isinstance(selected_code, DiagnosticText)
    assert selected_code.text == code
    assert "PRIVATE_" not in caught.value.message
