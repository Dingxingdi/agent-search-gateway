from collections.abc import Callable
from typing import Protocol

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.web.anysearch import AnySearchAdapter
from agent_search_gateway.providers.web.brave import BraveAdapter
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.decodo import DecodoAdapter
from agent_search_gateway.providers.web.exa import ExaAdapter
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.providers.web.scrape_do import ScrapeDoAdapter
from agent_search_gateway.providers.web.scrapegraphai import ScrapeGraphAIAdapter
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.providers.web.scrapingdog import ScrapingDogAdapter
from agent_search_gateway.providers.web.serpapi import SerpApiAdapter
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.providers.web.tinyfish import TinyFishAdapter
from tests.support.http import RecordingHttpExecutor, RecordingJsonExecutor


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


def _brightdata(executor: RecordingHttpExecutor) -> _SearchAdapter:
    return BrightDataAdapter(
        name="brightdata",
        api_url="https://bright.example.test",
        search_zone="search-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _scrape_do(executor: RecordingHttpExecutor) -> _SearchAdapter:
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


def _scrapingdog(executor: RecordingHttpExecutor) -> _SearchAdapter:
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


def _serpapi(executor: RecordingHttpExecutor) -> _SearchAdapter:
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
