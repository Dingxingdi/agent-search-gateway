import logging
from collections.abc import Callable
from typing import Protocol

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.anysearch import AnySearchAdapter
from agent_search_gateway.providers.web.brave import BraveAdapter
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.common import HttpRequester
from agent_search_gateway.providers.web.decodo import DecodoAdapter
from agent_search_gateway.providers.web.exa import ExaAdapter
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.providers.web.linkup import LinkupAdapter
from agent_search_gateway.providers.web.scrape_do import ScrapeDoAdapter
from agent_search_gateway.providers.web.scrapegraphai import ScrapeGraphAIAdapter
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.providers.web.scrapingdog import ScrapingDogAdapter
from agent_search_gateway.providers.web.serpapi import SerpApiAdapter
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.providers.web.tinyfish import TinyFishAdapter
from tests.support.http import RecordingHttpExecutor, RecordingJsonExecutor
from tests.support.logging import structured_test_logger


class _SearchAdapter(Protocol):
    async def search(self, query: str) -> list[KeywordSearchHit]: ...


AdapterFactory = Callable[[RecordingJsonExecutor], _SearchAdapter]
NewAdapterFactory = Callable[[RecordingHttpExecutor], _SearchAdapter]


def _firecrawl(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _tavily(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _exa(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return ExaAdapter(
        name="exa",
        api_url="https://exa.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _brave(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return BraveAdapter(
        name="brave",
        api_url="https://brave.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _anysearch(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _tinyfish(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return TinyFishAdapter(
        name="tinyfish",
        search_api_url="https://search.tiny.example.test",
        fetch_api_url="https://fetch.tiny.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


@pytest.mark.parametrize(
    ("factory", "payload", "expected_snippet"),
    [
        (
            _firecrawl,
            {
                "success": True,
                "data": {
                    "web": [
                        {"url": "https://example.com/bad", "title": 1},
                        {"url": "https://example.com/good"},
                    ]
                },
            },
            "",
        ),
        (
            _tavily,
            {
                "results": [
                    {"url": "https://example.com/bad", "content": {}},
                    {"url": "https://example.com/good"},
                ]
            },
            "",
        ),
        (
            _exa,
            {
                "results": [
                    {"url": "https://example.com/bad", "title": {}},
                    {
                        "url": "https://example.com/good",
                        "highlights": [""],
                        "summary": "fallback summary",
                    },
                ]
            },
            "fallback summary",
        ),
        (
            _brave,
            {
                "web": {
                    "results": [
                        {"url": "https://example.com/bad", "description": []},
                        {"url": "https://example.com/good"},
                    ]
                }
            },
            "",
        ),
        (
            _anysearch,
            {
                "code": 0,
                "data": {
                    "results": [
                        {"url": "https://example.com/bad", "snippet": []},
                        {"url": "https://example.com/good"},
                    ]
                },
            },
            "",
        ),
        (
            _tinyfish,
            {
                "results": [
                    {"url": "https://example.com/bad", "title": []},
                    {"url": "https://example.com/good"},
                ]
            },
            "",
        ),
    ],
    ids=("firecrawl", "tavily", "exa", "brave", "anysearch", "tinyfish"),
)
async def test_search_adapters_keep_valid_hits_when_presentation_fields_are_malformed(
    factory: AdapterFactory,
    payload: object,
    expected_snippet: str,
) -> None:
    adapter = factory(RecordingJsonExecutor([payload]))

    hits = await adapter.search("query")

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/good"
    assert hits[0].title == ""
    assert hits[0].snippet == expected_snippet


async def test_linkup_skips_only_isolated_malformed_search_results() -> None:
    adapter = LinkupAdapter(
        name="linkup",
        api_url="https://linkup.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [
                        None,
                        {
                            "url": {},
                            "name": "Bad URL",
                            "content": "Bad URL",
                        },
                        {
                            "url": "https://example.com/bad-name",
                            "name": 1,
                            "content": "Bad name",
                        },
                        {
                            "url": "https://example.com/bad-content",
                            "name": "Bad content",
                            "content": [],
                        },
                        {
                            "url": "https://example.com/good",
                            "name": "Good",
                            "content": "Useful snippet",
                        },
                    ]
                }
            ]
        ),
    )

    hits = await adapter.search("query")

    assert hits == [
        KeywordSearchHit(
            url="https://example.com/good",
            title="Good",
            snippet="Useful snippet",
        )
    ]


def _brightdata(executor: RecordingHttpExecutor) -> _SearchAdapter:
    return BrightDataAdapter(
        name="brightdata",
        api_url="https://bright.example.test",
        search_zone="search-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _scrape_do(executor: HttpRequester) -> _SearchAdapter:
    return ScrapeDoAdapter(
        name="scrape_do",
        api_url="https://scrape-do.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _decodo(executor: RecordingHttpExecutor) -> _SearchAdapter:
    return DecodoAdapter(
        name="decodo",
        api_url="https://decodo.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _scrapingdog(executor: HttpRequester) -> _SearchAdapter:
    return ScrapingDogAdapter(
        name="scrapingdog",
        api_url="https://scrapingdog.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _scrapegraphai(executor: RecordingHttpExecutor) -> _SearchAdapter:
    return ScrapeGraphAIAdapter(
        name="scrapegraphai",
        api_url="https://scrapegraph.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _scraperapi(executor: RecordingHttpExecutor) -> _SearchAdapter:
    return ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://scraperapi.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _serpapi(executor: HttpRequester) -> _SearchAdapter:
    return SerpApiAdapter(
        name="serpapi",
        api_url="https://serpapi.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _decodo_payload(results: list[object]) -> object:
    return {
        "results": [
            {
                "status_code": 200,
                "content": {
                    "errors": [],
                    "results": {"results": {"organic": results}},
                },
            }
        ]
    }


@pytest.mark.parametrize(
    ("factory", "payload"),
    [
        (
            _brightdata,
            {
                "organic": [
                    {"link": "https://example.com/a", "title": "A"},
                    None,
                    {"link": 1},
                    {"link": "https://example.com/bad", "title": []},
                    {"link": "https://example.com/b", "title": "B"},
                ]
            },
        ),
        (
            _scrape_do,
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A"},
                    None,
                    {"link": 1},
                    {"link": "https://example.com/bad", "snippet": []},
                    {"link": "https://example.com/b", "title": "B"},
                ]
            },
        ),
        (
            _decodo,
            _decodo_payload(
                [
                    {"url": "https://example.com/a", "title": "A"},
                    None,
                    {"url": 1},
                    {"url": "https://example.com/bad", "desc": []},
                    {"url": "https://example.com/b", "title": "B"},
                ]
            ),
        ),
        (
            _scrapingdog,
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A"},
                    None,
                    {"link": 1},
                    {"link": "https://example.com/bad", "title": []},
                    {"link": "https://example.com/b", "title": "B"},
                ]
            },
        ),
        (
            _scrapegraphai,
            {
                "results": [
                    {"url": "https://example.com/a", "title": "A"},
                    None,
                    {"url": 1},
                    {"url": "https://example.com/bad", "content": []},
                    {"url": "https://example.com/b", "title": "B"},
                ]
            },
        ),
        (
            _scraperapi,
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A"},
                    None,
                    {"link": 1},
                    {"link": "https://example.com/bad", "snippet": []},
                    {"link": "https://example.com/b", "title": "B"},
                ]
            },
        ),
        (
            _serpapi,
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A"},
                    None,
                    {"link": 1},
                    {"link": "https://example.com/bad", "title": []},
                    {"link": "https://example.com/b", "title": "B"},
                ]
            },
        ),
    ],
    ids=(
        "brightdata",
        "scrape_do",
        "decodo",
        "scrapingdog",
        "scrapegraphai",
        "scraperapi",
        "serpapi",
    ),
)
async def test_new_search_adapters_skip_only_isolated_malformed_results(
    factory: NewAdapterFactory,
    payload: object,
) -> None:
    executor = RecordingHttpExecutor(json_responses=[payload])
    hits = await factory(executor).search("query")

    assert [hit.url for hit in hits] == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.parametrize(
    ("factory", "bad_payloads", "empty_payload"),
    [
        (
            _brightdata,
            [[], {}, {"organic": {}}, {"error": "RAW_VENDOR_DIAGNOSTIC"}],
            {"organic": []},
        ),
        (
            _scrape_do,
            [[], {}, {"organic_results": {}}, {"error": "RAW_VENDOR_DIAGNOSTIC"}],
            {"organic_results": []},
        ),
        (
            _decodo,
            [
                [],
                {},
                {"results": {}},
                {"results": []},
                {
                    "results": [
                        {
                            "status_code": 200,
                            "content": {
                                "errors": ["RAW_VENDOR_DIAGNOSTIC"],
                                "results": {"results": {"organic": []}},
                            },
                        }
                    ]
                },
            ],
            _decodo_payload([]),
        ),
        (
            _scrapingdog,
            [[], {}, {"organic_results": {}}, {"error": "RAW_VENDOR_DIAGNOSTIC"}],
            {"organic_results": []},
        ),
        (
            _scrapegraphai,
            [[], {}, {"results": {}}, {"detail": "RAW_VENDOR_DIAGNOSTIC"}],
            {"results": []},
        ),
        (
            _scraperapi,
            [[], {}, {"organic_results": {}}, {"error": "RAW_VENDOR_DIAGNOSTIC"}],
            {"organic_results": []},
        ),
        (
            _serpapi,
            [[], {}, {"organic_results": {}}, {"error": "RAW_VENDOR_DIAGNOSTIC"}],
            {"organic_results": []},
        ),
    ],
    ids=(
        "brightdata",
        "scrape_do",
        "decodo",
        "scrapingdog",
        "scrapegraphai",
        "scraperapi",
        "serpapi",
    ),
)
async def test_new_search_adapters_fail_malformed_top_level_but_allow_empty_results(
    factory: NewAdapterFactory,
    bad_payloads: list[object],
    empty_payload: object,
) -> None:
    for payload in bad_payloads:
        adapter = factory(RecordingHttpExecutor(json_responses=[payload]))
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")
        assert "RAW_VENDOR_DIAGNOSTIC" not in caught.value.message

    adapter = factory(RecordingHttpExecutor(json_responses=[empty_payload]))
    assert await adapter.search("query") == []


@pytest.mark.parametrize(
    "factory", [_scrape_do, _scrapingdog, _serpapi], ids=["scrape_do", "scrapingdog", "serpapi"]
)
@pytest.mark.parametrize(
    ("candidates", "expected_urls", "rejections"),
    [
        (
            [
                {"link": "https://example.com/good", "title": "Good"},
                None,
                {},
                {"link": None},
                {"link": "  "},
                {"link": 1},
                {"link": "https://example.com/bad", "title": []},
                {"link": "https://example.com/bad-snippet", "snippet": {}},
            ],
            ["https://example.com/good"],
            {"missing_url": 3, "invalid_record": 4},
        ),
        ([], [], {}),
        ([None, {}, {"link": ""}, {"link": 1}], [], {"missing_url": 2, "invalid_record": 2}),
    ],
    ids=["mixed", "empty", "all-invalid"],
)
async def test_serp_adapters_report_rejected_candidate_counts(
    factory: Callable[[HttpRequester], _SearchAdapter],
    candidates: list[object],
    expected_urls: list[str],
    rejections: dict[str, int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"organic_results": candidates})
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="serp")
        adapter = factory(executor)
        hits = await adapter.search("query")
    assert [hit.url for hit in hits] == expected_urls
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0] == {
        "provider": factory.__name__.removeprefix("_"),
        "stage": "search",
        "phase": "adapter",
        "received": len(candidates),
        "accepted": len(expected_urls),
        "rejected": sum(rejections.values()),
        "rejection_counts": rejections,
        "rewritten": 0,
        "processed": len(candidates),
        "unprocessed": 0,
    }


@pytest.mark.parametrize(
    "factory", [_scrape_do, _scrapingdog, _serpapi], ids=["scrape_do", "scrapingdog", "serpapi"]
)
@pytest.mark.parametrize(
    "error",
    [
        "Vendor quota exhausted",
        {"code": 429, "message": "Vendor quota exhausted", "body": "PRIVATE_ERROR_BODY"},
    ],
    ids=["string", "explicit-children"],
)
async def test_serp_adapters_report_only_selected_top_level_error(
    factory: Callable[[HttpRequester], _SearchAdapter], error: object
) -> None:
    logger, stream = structured_test_logger("test.serp.selected_error")
    payload = {
        "error": error,
        "organic_results": [{"message": "PRIVATE_CANDIDATE_BODY"}],
        "body": {"message": "PRIVATE_RESPONSE_BODY"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="serp", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await factory(executor).search("query")
    assert caught.value.message == (
        factory.__name__.removeprefix("_") + "/search: provider reported failure"
    )
    assert "upstream_message" in caught.value.diagnostics
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 1
    assert "semantic_result=error" in rendered
    assert "Vendor quota exhausted" in rendered
    assert "PRIVATE_" not in rendered


@pytest.mark.parametrize(
    "factory", [_scrape_do, _scrapingdog, _serpapi], ids=["scrape_do", "scrapingdog", "serpapi"]
)
@pytest.mark.parametrize(
    ("payload", "expected_category", "expected_semantic"),
    [
        ([], "invalid_envelope", "unknown"),
        ({}, "invalid_envelope", "unknown"),
        ({"organic_results": {"message": "PRIVATE_RESULTS"}}, "invalid_envelope", "unknown"),
    ],
    ids=["root", "missing-results", "invalid-results"],
)
async def test_serp_adapters_classify_malformed_envelopes(
    factory: Callable[[HttpRequester], _SearchAdapter],
    payload: object,
    expected_category: str,
    expected_semantic: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="serp")
        with pytest.raises(ExecutionFailure):
            await factory(executor).search("query")
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == expected_category
    assert diagnostics[0]["semantic_result"] == expected_semantic
    assert "upstream_message" not in diagnostics[0]
    assert not any(
        getattr(record, "gateway_event", None) == "provider_candidates_summary"
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "factory", [_scrape_do, _scrapingdog, _serpapi], ids=["scrape_do", "scrapingdog", "serpapi"]
)
async def test_serp_adapters_keep_urls_and_ignore_candidate_error_text(
    factory: Callable[[HttpRequester], _SearchAdapter],
) -> None:
    logger, stream = structured_test_logger("test.serp.success")
    url = "https://www.google.com/goto?url=https%3A%2F%2Fexample.com%2Fpage"
    payload = {
        "error": None,
        "organic_results": [
            {
                "link": url,
                "title": "Title",
                "snippet": "Snippet",
                "error": "PRIVATE_CANDIDATE_ERROR",
                "message": "PRIVATE_CANDIDATE_MESSAGE",
            }
        ],
        "body": {"error": "PRIVATE_RESPONSE_BODY"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="serp", logger=logger
        )
        assert await factory(executor).search("query") == [
            KeywordSearchHit(url, "Title", "Snippet")
        ]
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 1
    assert "semantic_result=success" in rendered
    assert "PRIVATE_" not in rendered
    assert "upstream_message" not in rendered


@pytest.mark.parametrize(
    "factory", [_scrape_do, _scrapingdog, _serpapi], ids=["scrape_do", "scrapingdog", "serpapi"]
)
async def test_serp_adapters_classify_decode_without_logging_body(
    factory: Callable[[HttpRequester], _SearchAdapter],
) -> None:
    logger, stream = structured_test_logger("test.serp.decode")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="PRIVATE_INVALID_JSON")
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="serp", logger=logger
        )
        with pytest.raises(ExecutionFailure, match="response was not valid JSON"):
            await factory(executor).search("query")
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 1
    assert "category=decode" in rendered
    assert "PRIVATE_" not in rendered
