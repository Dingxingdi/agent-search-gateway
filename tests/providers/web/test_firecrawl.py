import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.config import resolve_web_provider_config
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import (
    DiagnosticText,
    KeyValueFormatter,
    SecretRedactor,
    SecretValue,
)
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor, HttpStatusFailure
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/firecrawl"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "options",
    [
        {},
        {
            "search_limit": 25,
            "search_highlights": False,
            "scrape_formats": ["rawHtml", "markdown"],
            "scrape_parsers": ["image", "pdf"],
        },
    ],
)
def test_firecrawl_registry_factory_accepts_resolved_options(options: dict[str, object]) -> None:
    registry = build_default_registry()
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "firecrawl": {
                    "enable_search": True,
                    "api_key_env": "TEST_KEY",
                    "api_url": "https://fire.example.test",
                    "use_proxy": False,
                    **options,
                }
            }
        },
        registry,
        {"TEST_KEY": "x"},
    ).providers
    adapter = registry.require(provider.name).factory(
        name=provider.name,
        secret=provider.secret,
        http_executor=RecordingJsonExecutor([]),
        **provider.options,
    )
    assert isinstance(adapter, FirecrawlAdapter)


@pytest.mark.parametrize("parsers", [[], ["image", "pdf"]])
async def test_firecrawl_resolved_options_snapshot_lists_before_factory(
    parsers: list[str],
) -> None:
    formats = ["rawHtml", "markdown"]
    original_parsers = tuple(parsers)
    table = {
        "enable_fetch": True,
        "api_key_env": "TEST_KEY",
        "api_url": "https://fire.example.test",
        "scrape_formats": formats,
        "scrape_parsers": parsers,
    }
    registry = build_default_registry()
    [config] = resolve_web_provider_config(
        {"web_providers": {"firecrawl": table}}, registry, {"TEST_KEY": "x"}
    ).providers
    assert table["scrape_formats"] == ["rawHtml", "markdown"]
    assert table["scrape_parsers"] == list(original_parsers)
    formats.clear()
    parsers[:] = ["pdf"] if not original_parsers else []
    executor = RecordingJsonExecutor([_fixture("scrape.json")])
    adapter = registry.require(config.name).factory(
        name=config.name,
        secret=config.secret,
        http_executor=executor,
        **config.options,
    )
    assert isinstance(adapter, FirecrawlAdapter)

    await adapter.fetch(normalize_url("https://example.com/fire"))

    [request] = executor.requests
    assert request.url.endswith("/v2/scrape")
    assert request.json_body == {
        "url": "https://example.com/fire",
        "formats": ["rawHtml", "markdown"],
        "parsers": list(original_parsers),
    }


@pytest.mark.parametrize("as_tuple", [False, True])
def test_firecrawl_constructor_copies_scrape_sequences(as_tuple: bool) -> None:
    formats = ["rawHtml", "markdown"]
    parsers = ["image", "pdf"]
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([]),
        search_limit=25,
        search_highlights=False,
        scrape_formats=tuple(formats) if as_tuple else formats,
        scrape_parsers=tuple(parsers) if as_tuple else parsers,
    )
    formats.clear()
    parsers.clear()
    assert adapter._scrape_formats == ("rawHtml", "markdown")
    assert adapter._scrape_parsers == ("image", "pdf")


async def test_firecrawl_adapter_maps_v2_search_and_scrape_payloads() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("scrape.json")])
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test/v2",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            "https://example.com/fire",
            "Firecrawl result",
            "Firecrawl snippet",
            "",
            "",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/fire"))
    assert candidate == URLFetchCandidate("<article>Fetched raw</article>", "Fetched markdown")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://fire.example.test/v2/search"
    assert fetch_request.url == "https://fire.example.test/v2/scrape"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {
        "query": "query",
        "sources": ["web"],
        "limit": 10,
        "highlights": True,
    }
    assert fetch_request.json_body == {
        "url": "https://example.com/fire",
        "formats": ["markdown"],
        "parsers": [],
    }

    markdown_only = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [{"success": True, "data": {"markdown": "only markdown"}}]
        ),
    )
    assert await markdown_only.fetch(
        normalize_url("https://example.com/fire")
    ) == URLFetchCandidate("only markdown", "only markdown")

    bad = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"success": False}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.search("query")


@pytest.mark.parametrize(
    "body_fields",
    [
        {},
        {"markdown": "Unexpected markdown", "rawHtml": "<main>Unexpected HTML</main>"},
        {"markdown": {"unexpected": "object"}},
        {"rawHtml": {"unexpected": "object"}},
        {"markdown": ["unexpected", "array"]},
        {"rawHtml": ["unexpected", "array"]},
    ],
    ids=[
        "metadata-only",
        "strings",
        "markdown-object",
        "html-object",
        "markdown-array",
        "html-array",
    ],
)
async def test_firecrawl_search_never_requests_or_maps_body(body_fields: dict[str, object]) -> None:
    executor = RecordingJsonExecutor(
        [
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://example.com/fire",
                            "title": "Firecrawl result",
                            "description": "Firecrawl snippet",
                            **body_fields,
                        }
                    ]
                },
            }
        ]
    )
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    assert await adapter.search("query") == [
        KeywordSearchHit(
            "https://example.com/fire", "Firecrawl result", "Firecrawl snippet", "", ""
        )
    ]

    [request] = executor.requests
    assert request.method == "POST"
    assert request.url == "https://fire.example.test/v2/search"
    assert request.stage == "search"
    assert request.json_body == {
        "query": "query",
        "sources": ["web"],
        "limit": 10,
        "highlights": True,
    }
    assert "scrapeOptions" not in request.json_body


@pytest.mark.parametrize(
    ("search_limit", "search_highlights"),
    [(25, True), (10, False), (25, False)],
    ids=["limit-only", "highlights-only", "both"],
)
async def test_firecrawl_search_uses_custom_metadata_parameters(
    search_limit: int, search_highlights: bool
) -> None:
    executor = RecordingJsonExecutor([{"success": True, "data": {"web": []}}])
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test/v2",
        secret=SecretValue("x"),
        http_executor=executor,
        search_limit=search_limit,
        search_highlights=search_highlights,
    )

    assert await adapter.search("query") == []

    [request] = executor.requests
    assert request.method == "POST"
    assert request.url == "https://fire.example.test/v2/search"
    assert request.json_body == {
        "query": "query",
        "sources": ["web"],
        "limit": search_limit,
        "highlights": search_highlights,
    }
    assert "scrapeOptions" not in request.json_body


async def test_firecrawl_search_failure_carries_selected_safe_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        "success": False,
        "error": {"code": "quota", "message": "quota exhausted", "body": "PRIVATE_ERROR_BODY"},
        "data": {"web": [{"markdown": "PRIVATE_MARKDOWN", "error": "PRIVATE_CONTENT_ERROR"}]},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = FirecrawlAdapter(
            name="firecrawl",
            api_url="https://fire.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="firecrawl"
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")
    assert caught.value.message == "firecrawl/search: provider reported failure"
    message = caught.value.diagnostics.get("upstream_message")
    assert isinstance(message, DiagnosticText)
    assert message.text == "quota exhausted"
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "error"
    formatter = KeyValueFormatter(SecretRedactor())
    rendered = "\n".join(formatter.format(record) for record in caplog.records)
    assert 'upstream_message="quota exhausted"' in rendered
    assert "upstream_code=quota" in rendered
    assert "PRIVATE_" not in rendered
    assert "event=provider_candidates_summary" not in rendered


@pytest.mark.parametrize(
    ("payload", "message", "semantic"),
    [
        (
            {
                "success": False,
                "error": {
                    "code": "blocked",
                    "message": "target blocked",
                    "body": "PRIVATE_ERROR_BODY",
                },
                "data": {"markdown": "PRIVATE_BODY"},
            },
            "provider reported failure",
            "error",
        ),
        (
            {
                "success": True,
                "data": {
                    "markdown": "PRIVATE_BODY",
                    "rawHtml": "PRIVATE_RAW",
                    "error": "PRIVATE_CONTENT_ERROR",
                },
            },
            None,
            "success",
        ),
        ({"success": True, "data": {}}, "page body is empty", "empty"),
        (
            {"success": True, "data": {"rawBase64": "PRIVATE_BASE64"}},
            "page body is empty",
            "empty",
        ),
    ],
)
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, payload: object, message: str | None, semantic: str
) -> None:
    from tests.support.web_diagnostics import assert_json_fetch_diagnostics

    await assert_json_fetch_diagnostics(
        caplog,
        "firecrawl",
        lambda executor: FirecrawlAdapter(
            name="firecrawl",
            api_url="https://firecrawl.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=executor,
        ),
        payload,
        message,
        semantic,
        check_provider_response_category=True,
    )
    if semantic == "empty":
        adapter = FirecrawlAdapter(
            name="firecrawl",
            api_url="https://fire.example.test",
            secret=SecretValue("x"),
            http_executor=RecordingJsonExecutor([payload]),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/fire"))
        assert caught.value.message == "firecrawl/fetch: page body is empty"
        assert caught.value.diagnostics == {
            "semantic_result": "empty",
            "response_kind": "object",
        }
        diagnostics = [
            vars(record)["gateway_fields"]
            for record in caplog.records
            if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
        ]
        assert diagnostics[0]["response_kind"] == "object"


@pytest.mark.parametrize(
    "formats",
    [("markdown",), ("rawHtml",), ("markdown", "rawHtml"), ("rawHtml", "markdown")],
)
@pytest.mark.parametrize("parsers", [(), ("pdf",), ("image",), ("pdf", "image")])
async def test_firecrawl_scrape_options_only_affect_fetch(
    formats: tuple[str, ...], parsers: tuple[str, ...]
) -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("scrape.json")])
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
        scrape_formats=formats,
        scrape_parsers=parsers,
    )

    hits = await adapter.search("query")
    assert all(hit.raw_content == hit.content == "" for hit in hits)
    assert await adapter.fetch(normalize_url("https://example.com/fire")) == URLFetchCandidate(
        "<article>Fetched raw</article>", "Fetched markdown"
    )

    search, fetch = executor.requests
    assert search.json_body == {
        "query": "query",
        "sources": ["web"],
        "limit": 10,
        "highlights": True,
    }
    assert fetch.json_body == {
        "url": "https://example.com/fire",
        "formats": list(formats),
        "parsers": list(parsers),
    }


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"markdown": "markdown"}, URLFetchCandidate("markdown", "markdown")),
        ({"rawHtml": "<main>raw</main>"}, URLFetchCandidate("<main>raw</main>", "")),
        (
            {"markdown": "markdown", "rawHtml": "<main>raw</main>"},
            URLFetchCandidate("<main>raw</main>", "markdown"),
        ),
    ],
    ids=["markdown-only", "rawHtml-only", "both"],
)
async def test_firecrawl_fetch_preserves_body_mapping(
    data: dict[str, object], expected: URLFetchCandidate
) -> None:
    executor = RecordingJsonExecutor([{"success": True, "data": data}])
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.fetch(normalize_url("https://example.com/fire")) == expected
    assert len(executor.requests) == 1


async def _invoke(adapter: FirecrawlAdapter, stage: str) -> object:
    if stage == "search":
        return await adapter.search("query")
    return await adapter.fetch(normalize_url("https://example.com/fire"))


@pytest.mark.parametrize("stage", ["search", "fetch"])
@pytest.mark.parametrize(
    ("case", "status", "attempts", "failure_type", "code"),
    [
        ("payment-required", 402, 1, HttpStatusFailure, ErrorCode.ALL_PROVIDERS_FAILED),
        ("server-error", 503, 3, HttpStatusFailure, ErrorCode.ALL_PROVIDERS_FAILED),
        ("invalid-json", 200, 1, ProtocolFailure, ErrorCode.PROTOCOL_ERROR),
        ("provider-failure", 200, 1, ExecutionFailure, ErrorCode.ALL_PROVIDERS_FAILED),
    ],
)
async def test_firecrawl_preserves_executor_failure_contracts(
    stage: str,
    case: str,
    status: int,
    attempts: int,
    failure_type: type[ExecutionFailure],
    code: ErrorCode,
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if case == "invalid-json":
            return httpx.Response(status, content=b"{")
        return httpx.Response(status, json={"success": False})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = FirecrawlAdapter(
            name="firecrawl",
            api_url="https://fire.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="firecrawl", sleep=sleep
            ),
        )
        with pytest.raises(failure_type) as caught:
            await _invoke(adapter, stage)

    assert type(caught.value) is failure_type
    assert caught.value.code is code
    assert len(requests) == attempts
    assert len(sleeps) == attempts - 1
    assert all(
        request.url.path == f"/v2/{'search' if stage == 'search' else 'scrape'}"
        for request in requests
    )
    if isinstance(caught.value, HttpStatusFailure):
        assert caught.value.status_code == status
    elif case == "provider-failure":
        assert caught.value.message == f"firecrawl/{stage}: provider reported failure"


@pytest.mark.parametrize("stage", ["search", "fetch"])
async def test_firecrawl_retry_after_is_handled_only_by_executor(stage: str) -> None:
    sends: list[float] = []
    sleeps: list[float] = []
    now = 0.0

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(now)
        if len(sends) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"success": False})
        return httpx.Response(
            200, json=_fixture("search.json" if stage == "search" else "scrape.json")
        )

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = FirecrawlAdapter(
            name="firecrawl",
            api_url="https://fire.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(
                client,
                RetryPolicy(3, 0, 0, 5),
                provider_name="firecrawl",
                sleep=sleep,
                monotonic=lambda: now,
            ),
        )
        result = await _invoke(adapter, stage)

    expected: object = (
        [
            KeywordSearchHit(
                "https://example.com/fire", "Firecrawl result", "Firecrawl snippet", "", ""
            )
        ]
        if stage == "search"
        else URLFetchCandidate("<article>Fetched raw</article>", "Fetched markdown")
    )
    assert result == expected
    assert sends == [0, 2]
    assert sleeps == [2]
