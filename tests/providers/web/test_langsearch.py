import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import (
    KeywordSearchHit,
    KeywordSearchProvider,
    URLFetchProvider,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from tests.support.http import RecordingJsonExecutor

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
