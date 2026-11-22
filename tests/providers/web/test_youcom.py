"""Offline You.com Search and Contents contracts using neutral synthetic fixtures."""

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import (
    HttpJsonExecutor,
    HttpStatusFailure,
    ProviderResponseFailure,
)
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor
from tests.support.web_diagnostics import assert_safe_response_diagnostic

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "youcom"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


@pytest.mark.parametrize(
    "api_url",
    ["https://api.you.com/mcp/free", "HTTPS://API.YOU.COM:443/mcp/free/"],
)
def test_youcom_accepts_exact_keyless_mcp_endpoint(api_url: str) -> None:
    http = RecordingJsonExecutor([])
    adapter = YouComAdapter(name="youcom", api_url=api_url, http_executor=http)
    assert adapter.name == "youcom"
    assert http.requests == []


@pytest.mark.parametrize("api_url", [123, None, "https://[api.you.com/mcp/free"])
def test_youcom_rejects_invalid_api_url(api_url: str) -> None:
    http = RecordingJsonExecutor([])
    with pytest.raises(ConfigFailure) as caught:
        YouComAdapter(
            name="youcom", api_url=api_url, secret=SecretValue("PRIVATE_KEY"), http_executor=http
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert http.requests == []


@pytest.mark.parametrize(
    ("api_url", "secret"),
    [
        ("https://ydc-index.io", None),
        ("https://proxy.example.test/api", None),
        ("https://api.you.com/mcp/free", SecretValue("PRIVATE_KEY")),
    ],
)
def test_youcom_rejects_conflicting_protocol_credentials(
    api_url: str, secret: SecretValue | None
) -> None:
    http = RecordingJsonExecutor([])
    with pytest.raises(ConfigFailure) as caught:
        YouComAdapter(name="youcom", api_url=api_url, secret=secret, http_executor=http)
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "PRIVATE_" not in str(caught.value)
    assert http.requests == []


@pytest.mark.parametrize(
    "api_url",
    [
        str(httpx.URL(scheme="https", host="api.you.com", path="/mcp")),
        str(httpx.URL(scheme="https", host="api.you.com", path="/mcp", query=b"profile=free")),
        "https://api.you.com/mcp/paid",
        "https://api.you.com/mcp-other",
        "http://api.you.com/mcp/free",
        str(httpx.URL("https://api.you.com/mcp/free").copy_with(username="PRIVATE_USER")),
        "https://" + "@api.you.com/mcp/free",
        "https://api.you.com:8443/mcp/free",
        "https://api.you.com:invalid/mcp/free",
        "https://api.you.com/mcp/free?PRIVATE_QUERY",
        "https://api.you.com/mcp/free?",
        "https://api.you.com/mcp/free#PRIVATE_FRAGMENT",
        "https://api.you.com/mcp/free#",
        "https://api.you.com/mcp/free//",
        "https://api.you.com/mcp/free/extra",
        "https://api.you.com/mcp/%66ree",
        "https://api.you.com/%6dcp/free",
        "https://api.you.com/old/../mcp/free",
        "https://api.you.com/%2e/mcp/free",
        "https://api.you.com./mcp/free",
    ],
)
@pytest.mark.parametrize("secret", [None, SecretValue("PRIVATE_KEY")])
def test_youcom_rejects_unsupported_official_mcp_urls(
    api_url: str, secret: SecretValue | None
) -> None:
    http = RecordingJsonExecutor([])
    with pytest.raises(ConfigFailure) as caught:
        YouComAdapter(name="youcom", api_url=api_url, secret=secret, http_executor=http)
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "PRIVATE_" not in str(caught.value)
    assert http.requests == []


@pytest.mark.parametrize("separator", ["\u3002", "\uff0e", "\uff61"])
@pytest.mark.parametrize("trailing", [False, True], ids=["between-labels", "trailing"])
def test_youcom_rejects_idna_dot_in_official_mcp_host(separator: str, trailing: bool) -> None:
    host = "api.you.com" + separator if trailing else separator.join(("api", "you", "com"))
    api_url = f"https://{host}/mcp/free"
    assert httpx.URL(api_url).raw_host == (b"api.you.com." if trailing else b"api.you.com")
    http = RecordingJsonExecutor([])
    with pytest.raises(ConfigFailure, match="Unsupported You.com MCP endpoint"):
        YouComAdapter(
            name="youcom", api_url=api_url, secret=SecretValue("test"), http_executor=http
        )
    assert http.requests == []


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


@pytest.mark.parametrize(
    "root",
    [
        "https://example.com/api",
        "https://example.com/api/",
        "https://example.com/api.you.com/mcp/free",
    ],
)
async def test_search_uses_custom_root(root: str) -> None:
    http = RecordingJsonExecutor([_fixture("search_empty.json")])
    adapter = YouComAdapter(
        name="custom",
        api_url=root,
        secret=SecretValue("synthetic-key"),
        http_executor=http,
    )
    assert await adapter.search("合成查询") == []
    assert http.requests[0].url == f"{root.rstrip('/')}/v1/search"


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


@pytest.mark.parametrize(
    "root",
    [
        "https://example.com/api",
        "https://example.com/api/",
        "https://example.com/api.you.com/mcp/free",
    ],
)
async def test_fetch_uses_custom_root_and_normalized_match(root: str) -> None:
    http = RecordingJsonExecutor([[{"url": " https://EXAMPLE.com/page ", "markdown": "body"}]])
    adapter = YouComAdapter(
        name="custom", api_url=root, secret=SecretValue("synthetic-key"), http_executor=http
    )
    assert await adapter.fetch(normalize_url("https://example.com/page")) == URLFetchCandidate(
        "body", "body"
    )
    assert http.requests[0].url == f"{root.rstrip('/')}/v1/contents"


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


def _mcp_adapter(
    client: httpx.AsyncClient, api_url: str = "https://api.you.com/mcp/free", *, attempts: int = 1
) -> YouComAdapter:
    return YouComAdapter(
        name="youcom",
        api_url=api_url,
        http_executor=HttpJsonExecutor(
            client, RetryPolicy(attempts, 0, 0, 5), provider_name="youcom"
        ),
    )


def _mcp_fixture_reply(request: httpx.Request, fixture: str) -> dict[str, object]:
    envelope = _fixture(fixture)
    assert isinstance(envelope, dict)
    envelope["id"] = json.loads(request.content)["id"]
    return envelope


@pytest.mark.parametrize(
    "api_url",
    [
        "https://api.you.com/mcp/free",
        "HTTPS://API.YOU.COM:443/mcp/free/",
    ],
)
async def test_mcp_search_posts_only_you_search(api_url: str) -> None:
    from agent_search_gateway import __version__

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.you.com/mcp/free"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["mcp-protocol-version"] == "2026-07-28"
        assert request.headers["mcp-method"] == "tools/call"
        assert request.headers["mcp-name"] == "you-search"
        assert not any(
            key in request.headers
            for key in (
                "authorization",
                "x-api-key",
                "payment",
                "payment-signature",
                "mcp-session-id",
            )
        )
        body = json.loads(request.content)
        assert isinstance(body["id"], str) and body["id"]
        assert body == {
            "jsonrpc": "2.0",
            "id": body["id"],
            "method": "tools/call",
            "params": {
                "name": "you-search",
                "arguments": {"query": "neutral query", "count": 10},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "agent-search-gateway",
                        "version": __version__,
                    },
                },
            },
        }
        return httpx.Response(200, json=_mcp_fixture_reply(request, "mcp_search_structured.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await _mcp_adapter(client, api_url).search("neutral query")
    assert hits == [
        KeywordSearchHit(
            f"https://example.test/{i}", f"Example page {i}", f"Synthetic excerpt {i}."
        )
        for i in range(1, 11)
    ]
    assert len(calls) == 1


async def test_mcp_requests_isolate_shared_client_defaults() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        tool = request.headers.get("mcp-name")
        if tool == "you-search":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "edge=synthetic; Path=/; Secure"},
                json=_mcp_fixture_reply(request, "mcp_search_structured.json"),
            )
        if tool == "you-extract":
            return httpx.Response(
                200, json=_mcp_fixture_reply(request, "mcp_fetch_structured.json")
            )
        return httpx.Response(200, json=_fixture("search_success.json"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"X-Private-Default": "synthetic", "X-API-Key": "synthetic"},
        auth=("synthetic", "synthetic"),
        cookies={"existing": "synthetic"},
        params={"tenant": "synthetic"},
    ) as client:
        adapter = _mcp_adapter(client)
        await adapter.search("neutral query")
        await adapter.fetch(normalize_url("https://example.test/page"))
        rest = YouComAdapter(
            name="youcom",
            api_url="https://api.you.com",
            secret=SecretValue("rest-key"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="youcom"),
        )
        await rest.search("neutral query")

    search, extract, ordinary = calls
    for request in (search, extract):
        assert str(request.url) == "https://api.you.com/mcp/free"
        assert {"authorization", "x-private-default", "x-api-key", "cookie"}.isdisjoint(
            request.headers
        )
    assert ordinary.url.params["tenant"] == "synthetic"
    assert ordinary.headers["authorization"].startswith("Basic ")
    assert ordinary.headers["x-private-default"] == "synthetic"
    assert ordinary.headers["x-api-key"] == "rest-key"
    assert "existing=synthetic" in ordinary.headers["cookie"]


@pytest.mark.parametrize("fixture", ["mcp_search_structured.json", "mcp_search_text.json"])
async def test_mcp_search_maps_verified_tool_content(fixture: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_mcp_fixture_reply(request, fixture))
        )
    ) as client:
        assert await _mcp_adapter(client).search("neutral query") == [
            KeywordSearchHit(
                f"https://example.test/{i}", f"Example page {i}", f"Synthetic excerpt {i}."
            )
            for i in range(1, 11)
        ]


@pytest.mark.parametrize(
    ("fields", "snippet"),
    [
        ({}, ""),
        ({"snippets": None}, ""),
        ({"snippets": []}, ""),
        ({"snippets": ["one", "two"]}, "one\n\ntwo"),
        ({"contents": {"highlights": ["first", "second"]}}, "first\n\nsecond"),
        (
            {"snippets": ["secondary"], "contents": {"highlights": ["primary"]}},
            "primary",
        ),
        ({"snippets": ["secondary"], "contents": {"highlights": []}}, ""),
        ({"snippets": ["secondary"], "contents": {"highlights": None}}, "secondary"),
        ({"contents": None, "snippets": ["secondary"]}, "secondary"),
        (
            {
                "description": "not a snippet",
                "contents": {"snippets": ["not a snippet"], "markdown": "not a body"},
            },
            "",
        ),
    ],
)
async def test_mcp_search_uses_verified_snippet_fields(
    fields: dict[str, object], snippet: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {
            "results": {"web": [{"url": " https://EXAMPLE.test/page#part ", **fields}]}
        }
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _mcp_adapter(client).search("neutral query") == [
            KeywordSearchHit(" https://EXAMPLE.test/page#part ", "", snippet)
        ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "PRIVATE_PAYLOAD",
        {},
        {"results": None},
        {"results": []},
        {"results": {}},
        {"results": {"web": None}},
        {"results": {"web": {}}},
    ],
)
async def test_mcp_search_requires_result_list_before_success_diagnostic(
    payload: object, caplog: pytest.LogCaptureFixture
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = payload
        return httpx.Response(200, json=envelope)

    await _assert_mcp_failure("search", caplog, reply, ExecutionFailure, "invalid_envelope")


async def test_mcp_search_accepts_valid_empty_result_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {"results": {"web": []}}
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _mcp_adapter(client).search("neutral query") == []


async def test_mcp_search_rejects_bad_candidates_without_losing_good_hits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rows: list[object] = [
        None,
        {},
        {"url": " "},
        {"url": "not a URL"},
        {"url": 7},
        {"url": "https://example.test/bad-title", "title": []},
        {"url": "https://example.test/bad-snippets", "snippets": "PRIVATE_SNIPPET"},
        {"url": "https://example.test/bad-snippet", "snippets": [7]},
        {"url": "https://example.test/bad-contents", "contents": []},
        {"url": "https://example.test/bad-highlights", "contents": {"highlights": "bad"}},
        {"url": "https://example.test/bad-highlight", "contents": {"highlights": [7]}},
        {"url": "https://example.test/\ud800"},
        {"url": "https://example.test/title", "title": "\udfff"},
        {"url": "https://example.test/snippet", "snippets": ["\ud800"]},
        {"url": "https://example.test/highlight", "contents": {"highlights": ["\udfff"]}},
        {"url": "https://EXAMPLE.test/good#part", "title": None, "snippets": ["kept 🌍"]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {"results": {"web": rows}}
        # The wire JSON is valid UTF-8 even when an escaped surrogate has no matching pair.
        return httpx.Response(
            200,
            content=json.dumps(envelope, ensure_ascii=True),
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await _mcp_adapter(client).search("PRIVATE_QUERY")
    for hit in hits:
        json.dumps([hit.url, hit.title, hit.snippet], ensure_ascii=False).encode("utf-8")
    assert hits == [KeywordSearchHit("https://EXAMPLE.test/good#part", "", "kept 🌍")]
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
            "received": 16,
            "accepted": 1,
            "rejected": 15,
            "rejection_counts": {"invalid_record": 13, "missing_url": 2},
            "processed": 16,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]
    assert_safe_response_diagnostic(caplog, "success")


@pytest.mark.parametrize(
    "representation",
    [
        "missing_id",
        "numeric_id",
        "wrong_id",
        "wrong_version",
        "missing_result",
        "natural_text",
        "structured_null_with_valid_text",
    ],
)
async def test_mcp_search_rejects_invalid_basic_envelopes(representation: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_text.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        if representation == "missing_id":
            del envelope["id"]
        elif representation == "numeric_id":
            envelope["id"] = 1
        elif representation == "wrong_id":
            envelope["id"] = "different-id"
        elif representation == "wrong_version":
            envelope["jsonrpc"] = "1.0"
        elif representation == "missing_result":
            del envelope["result"]
        elif representation == "natural_text":
            result["content"] = [{"type": "text", "text": "PRIVATE_NATURAL_LANGUAGE"}]
        elif representation == "structured_null_with_valid_text":
            result["structuredContent"] = None
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExecutionFailure) as caught:
            await _mcp_adapter(client).search("PRIVATE_QUERY")
    assert "PRIVATE_" not in caught.value.message


async def test_concurrent_mcp_calls_match_their_own_ids() -> None:
    import asyncio

    first_seen = asyncio.Event()
    second_completed = asyncio.Event()
    ids: list[str] = []
    completion_order: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rpc_id = body["id"]
        assert isinstance(rpc_id, str)
        ids.append(rpc_id)
        query = body["params"]["arguments"]["query"]
        if query == "first":
            first_seen.set()
            await second_completed.wait()
        else:
            await first_seen.wait()
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {
            "results": {"web": [{"url": f"https://example.test/{query}"}]}
        }
        completion_order.append(query)
        if query == "second":
            second_completed.set()
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _mcp_adapter(client)
        first, second = await asyncio.wait_for(
            asyncio.gather(adapter.search("first"), adapter.search("second")), timeout=5
        )
    assert first == [KeywordSearchHit("https://example.test/first")]
    assert second == [KeywordSearchHit("https://example.test/second")]
    assert len(set(ids)) == 2
    assert completion_order == ["second", "first"]


async def test_mcp_search_suppresses_raw_request_and_response_previews(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {
            "results": {
                "web": [
                    {
                        "url": "https://example.test/PRIVATE_PAGE",
                        "title": "PRIVATE_TITLE",
                        "contents": {"highlights": ["PRIVATE_SNIPPET"], "markdown": "PRIVATE_BODY"},
                    }
                ]
            },
            "metadata": {"query": "PRIVATE_QUERY", "unknown": "PRIVATE_METADATA"},
        }
        result["_meta"] = {"extra": "PRIVATE_METADATA"}
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _mcp_adapter(client).search("PRIVATE_QUERY") == [
            KeywordSearchHit(
                "https://example.test/PRIVATE_PAGE", "PRIVATE_TITLE", "PRIVATE_SNIPPET"
            )
        ]
    assert_safe_response_diagnostic(caplog, "success")


async def test_mcp_search_enforces_private_eight_mib_response_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        envelope["unused"] = "x" * (8 * 1024 * 1024)
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolFailure, match="response exceeded byte limit"):
            await _mcp_adapter(client).search("neutral query")


@pytest.mark.parametrize("line_end", ["\n", "\r\n", "\r"])
async def test_mcp_sse_search_event_boundaries(line_end: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rpc_id = json.loads(request.content)["id"]
        stream = (_FIXTURES / "mcp_search.sse").read_text().replace("REQUEST_ID", rpc_id)
        return httpx.Response(
            200,
            content=("\ufeff" + stream.replace("\n", line_end)).encode(),
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _mcp_adapter(client).search("neutral query") == [
            KeywordSearchHit("https://example.test/1", "Example page 1", "Synthetic excerpt 1.")
        ]


@pytest.fixture(params=["search", "fetch"])
def mcp_stage(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def _assert_mcp_failure(
    stage: str,
    caplog: pytest.LogCaptureFixture,
    reply: Callable[[httpx.Request], httpx.Response],
    failure_type: type[ExecutionFailure],
    category: str,
    code: str | None = None,
) -> None:
    caplog.set_level(logging.DEBUG)
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.you.com/mcp/free"
        assert json.loads(request.content)["method"] == "tools/call"
        response = reply(request)
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(failure_type) as caught:
            adapter = _mcp_adapter(client, attempts=3)
            if stage == "search":
                await adapter.search("PRIVATE_QUERY")
            else:
                await adapter.fetch(normalize_url("https://example.test/PRIVATE_PAGE"))
    assert len(responses) == 1
    assert f"youcom/{stage}" in caught.value.message
    assert "PRIVATE_" not in repr((caught.value, caught.value.diagnostics))
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "error")
    assert diagnostic["provider"] == "youcom"
    assert diagnostic["stage"] == stage
    assert diagnostic["http_status"] == responses[0].status_code
    expected_bytes = len(responses[0].content) if responses[0].is_success else 0
    assert diagnostic["response_bytes"] == expected_bytes
    assert diagnostic["category"] == category
    failures = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "http_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["category"] == ("status" if category == "http_status" else category)
    assert failures[0]["attempt"] == 1
    assert "upstream_message" not in diagnostic
    if code is None:
        assert "upstream_code" not in diagnostic
    else:
        upstream_code = diagnostic["upstream_code"]
        assert isinstance(upstream_code, DiagnosticText) and upstream_code.text == code


@pytest.mark.parametrize(
    "stream",
    [
        ": heartbeat\n\n",
        'data: {"jsonrpc":"2.0","method":"hello"}\n\n',
        "data: PRIVATE_NOT_JSON\n\n",
        "data: FINAL\n",
        "data: FINAL\n\ndata: {}\n",
        "data: FOREIGN\n\ndata: FINAL\n\n",
        "data: FINAL\n\ndata: FOREIGN\n\n",
        "data: CONFLICT\n\ndata: FINAL\n\n",
        "data: false\n\ndata: FINAL\n\n",
        'data: {"jsonrpc":"2.0","method":7}\n\ndata: FINAL\n\n',
        'data: {"jsonrpc":"1.0","method":"m"}\n\ndata: FINAL\n\n',
        'data: {"jsonrpc":"2.0","method":"m","params":[]}\n\ndata: FINAL\n\n',
        'data: {"jsonrpc":"2.0","method":"m","result":{}}\n\ndata: FINAL\n\n',
        'data: {"jsonrpc":"2.0","id":"server","method":"m"}\n\ndata: FINAL\n\n',
        'data: FINAL\n\ndata: {"jsonrpc":"2.0","method":"m"}\n\n',
    ],
)
async def test_mcp_sse_requires_one_consistent_final(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, stream: str
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        final = _mcp_fixture_reply(request, "mcp_search_structured.json")
        body = (
            stream.replace("FINAL", json.dumps(final))
            .replace("FOREIGN", json.dumps({**final, "id": "PRIVATE_FOREIGN"}))
            .replace("CONFLICT", json.dumps({**final, "result": {}}))
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


async def test_mcp_sse_accepts_identical_repeated_final() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        final = _mcp_fixture_reply(request, "mcp_search_structured.json")
        block = "data: " + json.dumps(final) + "\n\n"
        return httpx.Response(
            200, content=(block * 2).encode(), headers={"content-type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        assert len(await _mcp_adapter(client).search("neutral query")) == 10


@pytest.mark.parametrize(
    ("number", "code"),
    [
        (-32700, "parse_error"),
        (-32600, "invalid_request"),
        (-32601, "method_not_found"),
        (-32602, "invalid_params"),
        (-32603, "internal_error"),
        (-32020, "header_mismatch"),
        (-32021, "missing_required_client_capability"),
        (-32022, "unsupported_protocol_version"),
        (984321, "unknown"),
    ],
)
async def test_mcp_rpc_error_returns_only_allowlisted_code(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, number: int, code: str
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        error = {"code": number, "message": "PRIVATE_MESSAGE", "data": {"key": "PRIVATE_DATA"}}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "error": error}
        )

    await _assert_mcp_failure(
        mcp_stage, caplog, reply, ProviderResponseFailure, "provider_response", code
    )


@pytest.mark.parametrize(
    "error",
    [
        None,
        [],
        "PRIVATE_ERROR",
        {},
        {"code": -32601},
        {"message": "PRIVATE_MESSAGE"},
        {"code": False, "message": "PRIVATE_MESSAGE"},
        {"code": -32601.0, "message": "PRIVATE_MESSAGE"},
        {"code": "PRIVATE_CODE", "message": "PRIVATE_MESSAGE"},
        {"code": -32601, "message": 7},
    ],
)
async def test_mcp_rpc_error_rejects_invalid_shape(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, error: object
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "error": error}
        )

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


@pytest.mark.parametrize(
    "envelope",
    [
        [],
        {"jsonrpc": "2.0", "result": {}},
        {"jsonrpc": "1.0", "id": "REQUEST_ID", "result": {}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": "PRIVATE_ID", "result": {}},
        {"jsonrpc": "2.0", "id": "REQUEST_ID"},
        {"jsonrpc": "2.0", "id": "REQUEST_ID", "result": None},
        {
            "jsonrpc": "2.0",
            "id": "REQUEST_ID",
            "result": {},
            "error": {"code": -32601, "message": "PRIVATE_MESSAGE"},
        },
        {
            "jsonrpc": "2.0",
            "id": "REQUEST_ID",
            "method": "PRIVATE_METHOD",
            "result": {"structuredContent": {"results": {"web": []}}},
        },
    ],
)
async def test_mcp_envelope_rejects_invalid_responses(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, envelope: object
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        payload = envelope
        if isinstance(envelope, dict) and envelope.get("id") == "REQUEST_ID":
            payload = {**envelope, "id": json.loads(request.content)["id"]}
        return httpx.Response(200, json=payload)

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


@pytest.mark.parametrize("result_type", ["complete", None])
async def test_mcp_result_type_supports_complete_and_omitted(result_type: str | None) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_text.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        if result_type is None:
            result.pop("resultType", None)
        else:
            result["resultType"] = result_type
        return httpx.Response(
            200, json=envelope, headers={"content-type": "application/json; charset=UTF-8"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        assert len(await _mcp_adapter(client).search("neutral query")) == 10


@pytest.mark.parametrize(
    "fields",
    [
        {"resultType": "input_required"},
        {"resultType": "PRIVATE_RESULT_TYPE"},
        {"resultType": None},
        {"resultType": 7},
        {"isError": 0},
        {"isError": "PRIVATE_FLAG"},
        {"isError": None},
        {"content": "PRIVATE_INVALID"},
        {"content": [None]},
        {"content": [{"type": "text", "text": 7}]},
    ],
)
async def test_mcp_result_type_and_fields_are_validated(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, fields: dict[str, object]
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result.update(fields)
        return httpx.Response(200, json=envelope)

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


async def test_mcp_tool_error_is_terminal_even_with_usable_structured_results(
    mcp_stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        fixture = (
            "mcp_fetch_structured.json" if mcp_stage == "fetch" else "mcp_search_structured.json"
        )
        envelope = _mcp_fixture_reply(request, fixture)
        result = envelope["result"]
        assert isinstance(result, dict)
        result.update(isError=True, content=[{"type": "text", "text": "PRIVATE_TOOL_ERROR"}])
        return httpx.Response(
            200,
            content=("data: " + json.dumps(envelope) + "\n\n").encode(),
            headers={"content-type": "text/event-stream"},
        )

    await _assert_mcp_failure(
        mcp_stage, caplog, reply, ProviderResponseFailure, "provider_response", "tool_error"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"content": []},
        {"content": [{"type": "text", "text": ""}]},
        {"content": [{"type": "text", "text": "PRIVATE_NATURAL_LANGUAGE"}]},
        {"content": [{"type": "text", "text": "[]"}]},
        {
            "content": [
                {"type": "text", "text": '{"results":{"web":[]}}'},
                {"type": "text", "text": "PRIVATE_CONFLICT"},
            ]
        },
        {"structuredContent": [], "content": [{"type": "text", "text": '{"results":{"web":[]}}'}]},
    ],
)
async def test_mcp_text_rejects_unconfirmed_payloads(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, fields: dict[str, object]
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": fields}
        )

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


async def test_mcp_envelope_rejects_unnegotiated_media(
    mcp_stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_mcp_fixture_reply(request, "mcp_search_structured.json"),
            headers={"content-type": "text/plain"},
        )

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


@pytest.mark.parametrize(
    "encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]
)
async def test_mcp_json_requires_utf8(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, encoding: str
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = (
            {"results": {"web": []}}
            if mcp_stage == "search"
            else {"output": {"contents": {"full_page": {"markdown": "page"}}}}
        )
        envelope = {
            "jsonrpc": "2.0",
            "id": json.loads(request.content)["id"],
            "result": {"structuredContent": payload},
        }
        return httpx.Response(
            200,
            content=json.dumps(envelope).encode(encoding),
            headers={"content-type": "application/json"},
        )

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "decode")


async def test_mcp_json_rejects_invalid_syntax_as_decode(
    mcp_stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        return httpx.Response(
            200, content=json.dumps(envelope)[:-1], headers={"content-type": "application/json"}
        )

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "decode")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
async def test_mcp_json_preserves_utf8_text(encoding: str) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = {
            "jsonrpc": "2.0",
            "id": json.loads(request.content)["id"],
            "result": {
                "structuredContent": {"output": {"contents": {"full_page": {"markdown": "café"}}}}
            },
        }
        return httpx.Response(
            200,
            content=json.dumps(envelope, ensure_ascii=False).encode(encoding),
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result = await _mcp_adapter(client).fetch(normalize_url("https://example.test/page"))
    assert result == URLFetchCandidate("café", "café")


async def test_mcp_404_ends_call_without_handshake_or_fallback(
    mcp_stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _assert_mcp_failure(
        mcp_stage,
        caplog,
        lambda request: httpx.Response(404, text="PRIVATE_NOT_FOUND"),
        HttpStatusFailure,
        "http_status",
    )


@pytest.mark.parametrize("media_type", ["application/json", "text/event-stream"])
async def test_mcp_envelope_rejects_non_json_numbers(
    mcp_stage: str, caplog: pytest.LogCaptureFixture, media_type: str
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_search_structured.json")
        wire = json.dumps({**envelope, "_meta": {"value": float("nan")}})
        if media_type == "text/event-stream":
            wire = f"data: {wire}\n\n"
        return httpx.Response(200, content=wire.encode(), headers={"content-type": media_type})

    await _assert_mcp_failure(mcp_stage, caplog, reply, ProtocolFailure, "invalid_envelope")


async def test_mcp_fetch_calls_you_extract_with_url_only() -> None:
    target = normalize_url(" https://EXAMPLE.test/page ")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.you.com/mcp/free"
        assert request.headers["mcp-name"] == "you-extract"
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "you-extract"
        assert body["params"]["arguments"] == {"url": str(target)}
        return httpx.Response(200, json=_mcp_fixture_reply(request, "mcp_fetch_structured.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        candidate = await _mcp_adapter(client).fetch(target)
    assert candidate == URLFetchCandidate(
        "  # Synthetic page\n\nSynthetic full page body.\n ",
        "  # Synthetic page\n\nSynthetic full page body.\n ",
    )
    assert len(calls) == 1


@pytest.mark.parametrize("fixture", ["mcp_fetch_structured.json", "mcp_fetch_text.json"])
@pytest.mark.parametrize("media_type", ["application/json", "text/event-stream"])
async def test_mcp_fetch_returns_verified_nonempty_body(
    fixture: str, media_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, fixture)
        wire = json.dumps(envelope)
        if media_type == "text/event-stream":
            wire = ": heartbeat\n\nevent: message\ndata: " + wire + "\n\n"
        return httpx.Response(200, content=wire.encode(), headers={"content-type": media_type})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _mcp_adapter(client).fetch(normalize_url("https://example.test/page"))
    body = "  # Synthetic page\n\nSynthetic full page body.\n "
    assert result == URLFetchCandidate(raw_content=body, content=body)
    diagnostic, _ = assert_safe_response_diagnostic(caplog, "success")
    assert diagnostic["stage"] == "fetch"
    assert diagnostic["response_kind"] == "object"


@pytest.mark.parametrize("url_fields", [{}, {"url": " https://EXAMPLE.test/page "}])
async def test_mcp_fetch_checks_returned_url_when_present(
    url_fields: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": json.loads(request.content)["id"],
                "result": {
                    "structuredContent": {
                        "output": {
                            **url_fields,
                            "contents": {"full_page": {"markdown": "body"}},
                        }
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _mcp_adapter(client).fetch(
            normalize_url("https://example.test/page")
        ) == URLFetchCandidate("body", "body")


@pytest.mark.parametrize(
    ("output", "semantic"),
    [
        (
            {
                "url": "https://example.test/other",
                "contents": {"full_page": {"markdown": "PRIVATE_BODY"}},
            },
            "no_match",
        ),
        *[
            ({"url": url, "contents": {"full_page": {"markdown": "PRIVATE_BODY"}}}, "error")
            for url in [None, "", " ", 7, list[object](), "not a URL"]
        ],
        *[
            ({"contents": {"full_page": {"markdown": body}}}, "empty")
            for body in [None, "", " \n\t ", 7, dict[str, object](), list[object]()]
        ],
        ({}, "error"),
        ({"contents": None}, "error"),
        ({"contents": {"full_page": None}}, "error"),
        ({"contents": {"highlights": ["PRIVATE_SNIPPET"]}}, "error"),
        ({"markdown": "PRIVATE_BODY"}, "error"),
    ],
)
async def test_mcp_fetch_rejects_empty_or_nonbody_output(
    caplog: pytest.LogCaptureFixture, output: object, semantic: str
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://api.you.com/mcp/free"
        assert json.loads(request.content)["params"]["arguments"] == {
            "url": "https://example.test/page"
        }
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": json.loads(request.content)["id"],
                "result": {"structuredContent": {"output": output}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExecutionFailure) as caught:
            await _mcp_adapter(client, attempts=3).fetch(normalize_url("https://example.test/page"))
    assert len(calls) == 1
    assert caught.value.diagnostics["semantic_result"] == semantic
    assert "PRIVATE_" not in repr((caught.value, caught.value.diagnostics))
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    assert diagnostic["stage"] == "fetch"
    assert diagnostic["category"] == "invalid_envelope"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "results": {
                "web": [{"url": "https://example.test/page", "snippets": ["PRIVATE_SNIPPET"]}]
            }
        },
        {"error": "PRIVATE_ERROR"},
    ],
)
async def test_mcp_fetch_rejects_nonbody_payload(
    caplog: pytest.LogCaptureFixture, payload: object
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": json.loads(request.content)["id"],
                "result": {"structuredContent": payload},
            },
        )

    await _assert_mcp_failure("fetch", caplog, reply, ExecutionFailure, "invalid_envelope")


async def test_mcp_fetch_suppresses_page_and_target_previews(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    target = normalize_url("https://example.test/PRIVATE_TARGET")

    def handler(request: httpx.Request) -> httpx.Response:
        envelope = _mcp_fixture_reply(request, "mcp_fetch_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {
            "output": {
                "url": str(target),
                "contents": {"full_page": {"markdown": "PRIVATE_BODY"}},
                "title": "PRIVATE_TITLE",
                "metadata": {"extra": "PRIVATE_METADATA"},
            }
        }
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _mcp_adapter(client).fetch(target) == URLFetchCandidate(
            "PRIVATE_BODY", "PRIVATE_BODY"
        )
    assert_safe_response_diagnostic(caplog, "success")


async def test_mcp_fetch_concurrent_calls_keep_ids_and_diagnostics_independent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    caplog.set_level(logging.DEBUG)
    second_completed = asyncio.Event()
    ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ids.append(body["id"])
        target = body["params"]["arguments"]["url"]
        if target.endswith("/first"):
            await second_completed.wait()
        envelope = _mcp_fixture_reply(request, "mcp_fetch_structured.json")
        result = envelope["result"]
        assert isinstance(result, dict)
        result["structuredContent"] = {
            "output": {"url": target, "contents": {"full_page": {"markdown": target}}}
        }
        if target.endswith("/second"):
            second_completed.set()
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _mcp_adapter(client)
        targets = [normalize_url("https://example.test/" + name) for name in ("first", "second")]
        candidates = await asyncio.wait_for(
            asyncio.gather(*(adapter.fetch(target) for target in targets)), timeout=5
        )
    assert candidates == [URLFetchCandidate(str(target), str(target)) for target in targets]
    assert len(set(ids)) == 2
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 2
    assert all(
        row["stage"] == "fetch" and row["semantic_result"] == "success" for row in diagnostics
    )
