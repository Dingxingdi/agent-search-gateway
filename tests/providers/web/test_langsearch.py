import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import (
    DiagnosticText,
    KeyValueFormatter,
    SecretRedactor,
    SecretValue,
)
from agent_search_gateway.providers.contracts import (
    KeywordSearchHit,
    KeywordSearchProvider,
    URLFetchProvider,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web import langsearch
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import without_debug_body_previews
from tests.support.web_diagnostics import assert_safe_response_diagnostic

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/langsearch/search.json"
_MISSING = object()


async def test_langsearch_accepts_all_rejected_candidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    results = [
        None,
        {},
        {"url": ""},
        {"url": 7},
        {"url": "https://bad-title.example.test/", "name": []},
        {"url": "https://bad-snippet.example.test/", "snippet": False},
    ]
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor(
            [{"code": 200, "data": {"webPages": {"value": results}}}]
        ),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    assert await adapter.search("query") == []

    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "langsearch",
            "stage": "search",
            "phase": "adapter",
            "received": 6,
            "accepted": 0,
            "rejected": 6,
            "rejection_counts": {"missing_url": 2, "invalid_record": 4},
            "processed": 6,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]


async def test_langsearch_reports_candidate_counts(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    results = [
        {"url": "https://first.example.test/", "name": "First", "snippet": "First snippet"},
        None,
        {},
        {"url": ""},
        {"url": 7},
        {"url": "https://bad-title.example.test/", "name": []},
        {"url": "https://second.example.test/", "name": "Second", "snippet": "Second snippet"},
        {"url": "https://bad-snippet.example.test/", "snippet": False},
    ]
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor(
            [{"code": 200, "data": {"webPages": {"value": results}}}]
        ),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    assert len(await adapter.search("query")) == 2

    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "langsearch",
            "stage": "search",
            "phase": "adapter",
            "received": 8,
            "accepted": 2,
            "rejected": 6,
            "rejection_counts": {"missing_url": 2, "invalid_record": 4},
            "processed": 8,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]


async def test_langsearch_keeps_valid_candidates_in_order() -> None:
    results = [
        {"url": "https://first.example.test/", "name": "First", "snippet": "First snippet"},
        None,
        {},
        {"url": ""},
        {"url": 7},
        {"url": "https://bad-title.example.test/", "name": []},
        {"url": "https://second.example.test/", "name": "Second", "snippet": "Second snippet"},
        {"url": "https://bad-snippet.example.test/", "snippet": False},
    ]
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor(
            [{"code": 200, "data": {"webPages": {"value": results}}}]
        ),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    assert await adapter.search("query") == [
        KeywordSearchHit("https://first.example.test/", "First", "First snippet"),
        KeywordSearchHit("https://second.example.test/", "Second", "Second snippet"),
    ]


async def test_langsearch_preserves_duplicate_urls() -> None:
    url = "HTTPS://Example.TEST/Page#Section"
    results = [
        {"url": url, "name": "First", "snippet": "First snippet"},
        {"url": url, "name": "Second", "snippet": "Second snippet"},
    ]
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor(
            [{"code": 200, "data": {"webPages": {"value": results}}}]
        ),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    assert await adapter.search("query") == [
        KeywordSearchHit(url, "First", "First snippet"),
        KeywordSearchHit(url, "Second", "Second snippet"),
    ]


async def test_langsearch_ignores_response_body_fields() -> None:
    result = {
        "url": "https://example.test/",
        "name": "Title",
        "text": "Text must not become a snippet",
        "raw_content": "Raw body",
        "content": "Body",
        "date": "2026-11-28",
        "datePublished": "2026-11-28",
        "extra": {"metadata": "ignored"},
    }
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor(
            [{"code": 200, "data": {"webPages": {"value": [result]}}}]
        ),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    hits = await adapter.search("query")

    assert hits == [KeywordSearchHit("https://example.test/", "Title", "")]
    assert hits[0].raw_content == hits[0].content == ""


@pytest.mark.parametrize("field", ["name", "snippet"])
@pytest.mark.parametrize("value", [_MISSING, None, ""], ids=["missing", "null", "empty"])
async def test_langsearch_defaults_missing_metadata(field: str, value: object) -> None:
    result: dict[str, object] = {
        "url": "https://example.test/",
        "name": "Title",
        "snippet": "Snippet",
    }
    if value is _MISSING:
        del result[field]
    else:
        result[field] = value
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor(
            [{"code": 200, "data": {"webPages": {"value": [result]}}}]
        ),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    assert await adapter.search("query") == [
        KeywordSearchHit(
            "https://example.test/",
            "" if field == "name" else "Title",
            "" if field == "snippet" else "Snippet",
        )
    ]


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        pytest.param((), None, "response must be an object", id="root-null"),
        pytest.param((), [], "response must be an object", id="root-list"),
        pytest.param((), "response", "response must be an object", id="root-string"),
        pytest.param((), 200, "response must be an object", id="root-number"),
        pytest.param(("code",), _MISSING, "code must be the integer 200", id="code-missing"),
        pytest.param(("code",), None, "code must be the integer 200", id="code-null"),
        pytest.param(("code",), True, "code must be the integer 200", id="code-true"),
        pytest.param(("code",), False, "code must be the integer 200", id="code-false"),
        pytest.param(("code",), "200", "code must be the integer 200", id="code-success-string"),
        pytest.param(("code",), 200.0, "code must be the integer 200", id="code-float"),
        pytest.param(("code",), {}, "code must be the integer 200", id="code-object"),
        pytest.param(("code",), 429, "code must be the integer 200", id="code-error-integer"),
        pytest.param(("code",), "429", "code must be the integer 200", id="code-error-string"),
        *[
            pytest.param(path, value, f"{label} must be an object", id=f"{label}-{kind}")
            for path, label in [
                (("data",), "data"),
                (("data", "webPages"), "data.webPages"),
            ]
            for kind, value in [
                ("missing", _MISSING),
                ("null", None),
                ("list", []),
                ("string", "invalid"),
                ("number", 200),
                ("boolean", True),
            ]
        ],
        *[
            pytest.param(
                ("data", "webPages", "value"),
                value,
                "data.webPages.value must be an array",
                id=f"value-{kind}",
            )
            for kind, value in [
                ("missing", _MISSING),
                ("null", None),
                ("object", {}),
                ("string", "invalid"),
                ("number", 200),
                ("boolean", True),
            ]
        ],
    ],
)
async def test_langsearch_rejects_invalid_success_envelope(
    path: tuple[str, ...], value: object, reason: str
) -> None:
    payload: object = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    if path:
        parent = payload
        for key in path[:-1]:
            assert isinstance(parent, dict)
            parent = parent[key]
        assert isinstance(parent, dict)
        if value is _MISSING:
            del parent[path[-1]]
        else:
            parent[path[-1]] = value
    else:
        payload = value
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor([payload]),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("query")

    assert caught.value.code == ErrorCode.ALL_PROVIDERS_FAILED
    assert str(caught.value) == f"langsearch/search: {reason}"


async def test_langsearch_accepts_empty_results(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    payload["data"]["webPages"]["value"] = []
    registration = build_default_registry().require("langsearch")
    adapter = registration.factory(
        name="langsearch",
        api_url="https://api.langsearch.com",
        secret=SecretValue("example"),
        http_executor=RecordingJsonExecutor([payload]),
    )
    assert isinstance(adapter, KeywordSearchProvider)

    assert await adapter.search("query") == []

    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "langsearch",
            "stage": "search",
            "phase": "adapter",
            "received": 0,
            "accepted": 0,
            "rejected": 0,
            "rejection_counts": {},
            "processed": 0,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]


@pytest.mark.parametrize(
    ("api_url", "expected_url"),
    [
        ("https://api.langsearch.com", "https://api.langsearch.com/v1/web-search"),
        ("https://api.langsearch.com/", "https://api.langsearch.com/v1/web-search"),
        (
            "https://gateway.example.test/prefix/",
            "https://gateway.example.test/prefix/v1/web-search",
        ),
    ],
)
async def test_langsearch_posts_metadata_only_request(api_url: str, expected_url: str) -> None:
    registration = build_default_registry().get("langsearch")
    assert registration is not None
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    adapter = registration.factory(
        name="langsearch", api_url=api_url, secret=SecretValue("example"), http_executor=executor
    )
    assert isinstance(adapter, KeywordSearchProvider)
    assert not isinstance(adapter, URLFetchProvider)
    query = "  中文？  café!  "
    hits = await adapter.search(query)

    [request] = executor.requests
    assert (request.method, request.stage, request.url) == ("POST", "search", expected_url)
    assert request.headers == {"Authorization": "Bearer example"}
    assert request.params is None
    assert request.json_body == {"query": query, "count": 10}
    assert hits == [
        KeywordSearchHit("https://first.example.test/", "First title", "First snippet"),
        KeywordSearchHit("https://second.example.test/", "Second title", "Second snippet"),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)


async def test_langsearch_sends_json_content_type() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Content-Type"] == "application/json"
        return httpx.Response(200, json=json.loads(_FIXTURE.read_text(encoding="utf-8")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        registration = build_default_registry().require("langsearch")
        adapter = registration.factory(
            name="langsearch",
            api_url="https://api.langsearch.com",
            secret=SecretValue("example"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="langsearch"
            ),
        )
        assert isinstance(adapter, KeywordSearchProvider)
        assert len(await adapter.search("query")) == 2


def _http_adapter(
    client: httpx.AsyncClient, *, max_attempts: int = 1
) -> langsearch.LangSearchAdapter:
    return langsearch.LangSearchAdapter(
        name="langsearch",
        api_url="https://langsearch.example.test",
        secret=SecretValue("PRIVATE_AUTH"),
        http_executor=HttpJsonExecutor(
            client, RetryPolicy(max_attempts, 0, 0, 5), provider_name="langsearch"
        ),
    )


@pytest.mark.parametrize(
    ("body", "category", "semantic"),
    [
        (b"{", "decode", "unknown"),
        (b"[]", "invalid_envelope", "unknown"),
        (b"{}", "invalid_envelope", "unknown"),
        (b'{"code":true}', "invalid_envelope", "unknown"),
        (b'{"code":200.0}', "invalid_envelope", "unknown"),
        (b'{"code":"200","data":{"webPages":{"value":[]}}}', "invalid_envelope", "unknown"),
        (b'{"code":200,"data":[]}', "invalid_envelope", "unknown"),
        (b'{"code":200,"data":{"webPages":null}}', "invalid_envelope", "unknown"),
        (b'{"code":200,"data":{"webPages":{"value":{}}}}', "invalid_envelope", "unknown"),
        (b'{"code":403}', "provider_response", "error"),
        (b'{"code":"quota_exceeded"}', "provider_response", "error"),
        (b'{"code":200,"data":{"webPages":{"value":[]}}}', None, "success"),
        (
            b'{"code":200,"data":{"webPages":{"value":[{"url":"https://example.test"}]}}}',
            None,
            "success",
        ),
    ],
)
async def test_langsearch_classifies_response_diagnostics(
    caplog: pytest.LogCaptureFixture, body: bytes, category: str | None, semantic: str
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = _http_adapter(client)
        if semantic == "success":
            await adapter.search("query")
        else:
            with pytest.raises(ExecutionFailure):
                await adapter.search("query")

    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    if category is None:
        assert diagnostic["response_kind"] == "object"
    else:
        assert diagnostic["category"] == category


@pytest.mark.parametrize(
    ("code", "message", "msg", "selected"),
    [
        (403, "denied", "PRIVATE_FALLBACK", "denied"),
        ("QUOTA", "", "PRIVATE_FALLBACK", ""),
        (429, {"text": "PRIVATE_OBJECT"}, "try later", "try later"),
        ("E_LIMIT", ["PRIVATE_LIST"], {"text": "PRIVATE_OBJECT"}, None),
        (
            403,
            _MISSING,
            "PRIVATE_AUTH " + "界" * 550 + "PRIVATE_TAIL",
            "PRIVATE_AUTH " + "界" * 550 + "PRIVATE_TAIL",
        ),
    ],
    ids=["prefer-message", "empty-message", "fallback-msg", "invalid-messages", "bounded-redacted"],
)
async def test_langsearch_selects_safe_error_message(
    caplog: pytest.LogCaptureFixture,
    code: int | str,
    message: object,
    msg: object,
    selected: str | None,
) -> None:
    payload = {
        "code": code,
        "msg": msg,
        "usage": {"cost": "PRIVATE_USAGE"},
        "log_id": "PRIVATE_LOG_ID",
        "error": {"message": "PRIVATE_NESTED"},
    }
    if message is not _MISSING:
        payload["message"] = message
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ExecutionFailure) as caught:
            await _http_adapter(client).search("query")

    assert caught.value.message == "langsearch/search: code must be the integer 200"
    expected = {"upstream_code": DiagnosticText(str(code), 128)}
    if selected is not None:
        expected["upstream_message"] = DiagnosticText(selected, 512)
    assert caught.value.diagnostics == expected
    [diagnostic] = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert {k: v for k, v in diagnostic.items() if k.startswith("upstream_")} == expected
    assert {"usage", "log_id", "error", "msg", "message"}.isdisjoint(diagnostic)
    formatter = KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")]))
    rendered = without_debug_body_previews("\n".join(formatter.format(r) for r in caplog.records))
    assert "PRIVATE_" not in rendered
    if selected and len(selected) > 512:
        assert "<redacted>" in rendered
        assert "…" in rendered
        assert "upstream_message_truncated=true" in rendered


@pytest.mark.parametrize(
    ("status", "body", "code", "reason"),
    [
        (200, b"{", ErrorCode.PROTOCOL_ERROR, "response was not valid JSON"),
        (
            401,
            b'{"code":200,"data":{"webPages":{"value":[]}}}',
            ErrorCode.ALL_PROVIDERS_FAILED,
            "HTTP status 401",
        ),
        (200, b'{"code":429}', ErrorCode.ALL_PROVIDERS_FAILED, "code must be the integer 200"),
    ],
)
async def test_langsearch_http_and_business_failures_send_once(
    status: int, body: bytes, code: ErrorCode, reason: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ExecutionFailure) as caught:
            await _http_adapter(client, max_attempts=2).search("query")
    assert len(requests) == 1
    assert caught.value.code == code
    assert caught.value.message == f"langsearch/search: {reason}"
    if status == 401:
        assert caught.value.diagnostics["category"] == "http_status"
        assert caught.value.diagnostics["semantic_result"] == "error"


async def test_langsearch_executor_retries_http_429() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"code": 429})
        return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await _http_adapter(client, max_attempts=2).search("query") == []
    assert len(requests) == 2


async def test_langsearch_diagnostic_error_preserves_business_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(root: object) -> dict[str, object]:
        raise RuntimeError("unavailable diagnostics")

    monkeypatch.setattr(langsearch, "_error_fields", unavailable)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 403}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ExecutionFailure) as caught:
            await _http_adapter(client).search("query")

    assert caught.value.code == ErrorCode.ALL_PROVIDERS_FAILED
    assert caught.value.message == "langsearch/search: code must be the integer 200"
    assert caught.value.diagnostics == {}


async def test_langsearch_response_diagnostic_error_preserves_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(response: httpx.Response) -> dict[str, object]:
        raise RuntimeError("unavailable diagnostics")

    monkeypatch.setattr(langsearch, "_search_response_diagnostics", unavailable)
    payload = {"code": 200, "data": {"webPages": {"value": [{"url": "https://example.test/"}]}}}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await _http_adapter(client).search("query") == [
            KeywordSearchHit("https://example.test/", "", "")
        ]
