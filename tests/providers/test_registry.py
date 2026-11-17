import pytest

from agent_search_gateway.providers.contracts import (
    KeywordSearchHit,
    ProviderCapabilities,
    URLFetchCandidate,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import ProviderRegistry, WebProviderRegistration
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.decodo import DecodoAdapter
from agent_search_gateway.providers.web.doubao import DoubaoAdapter
from agent_search_gateway.providers.web.jina import JinaReaderAdapter
from agent_search_gateway.providers.web.keenable import KeenableAdapter
from agent_search_gateway.providers.web.open_websearch import OpenWebSearchAdapter
from agent_search_gateway.providers.web.scrape_do import ScrapeDoAdapter
from agent_search_gateway.providers.web.scrapegraphai import ScrapeGraphAIAdapter
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.providers.web.scrapingant import ScrapingAntAdapter
from agent_search_gateway.providers.web.scrapingdog import ScrapingDogAdapter
from agent_search_gateway.providers.web.serpapi import SerpApiAdapter
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.providers.web.yuanbao import YuanbaoAdapter
from agent_search_gateway.providers.web.zenrows import ZenRowsAdapter
from agent_search_gateway.providers.web.zhihu import ZhihuAdapter
from tests.support.fakes import FakeKeywordSearchProvider, FakeURLFetchProvider


def _factory() -> object:
    return object()


def test_web_provider_registration_requires_api_key_by_default() -> None:
    registration = WebProviderRegistration(
        "custom",
        ProviderCapabilities(search=True, fetch=False),
        _factory,
        frozenset({"api_url"}),
    )

    assert registration.requires_api_key is True


def test_registry_exposes_exact_capabilities_and_contract_types() -> None:
    registry = ProviderRegistry()
    search_only = WebProviderRegistration(
        name="search",
        capabilities=ProviderCapabilities(search=True, fetch=False),
        factory=_factory,
        allowed_config_keys=frozenset({"api_url"}),
    )
    fetch_only = WebProviderRegistration(
        name="fetch",
        capabilities=ProviderCapabilities(search=False, fetch=True),
        factory=_factory,
        allowed_config_keys=frozenset(),
    )
    dual = WebProviderRegistration(
        name="dual",
        capabilities=ProviderCapabilities(search=True, fetch=True),
        factory=_factory,
        allowed_config_keys=frozenset({"api_url"}),
    )
    for registration in (search_only, fetch_only, dual):
        registry.register(registration)

    assert registry.capabilities("dual") == ProviderCapabilities(search=True, fetch=True)
    assert [item.name for item in registry.for_stage("search")] == ["search", "dual"]
    assert [item.name for item in registry.for_stage("fetch")] == ["fetch", "dual"]
    assert [item.name for item in registry.list_in_registration_order()] == [
        "search",
        "fetch",
        "dual",
    ]

    hit = KeywordSearchHit("https://example.com", "title", "snippet", "raw", "clean")
    candidate = URLFetchCandidate("raw", "clean")
    assert hit.url == "https://example.com"
    assert candidate.raw_content == "raw"

    search_fake = FakeKeywordSearchProvider("fake-search", [hit])
    fetch_fake = FakeURLFetchProvider("fake-fetch", candidate)
    assert search_fake.calls == []
    assert fetch_fake.calls == []

    assert "URLStore" not in str(search_fake.search.__annotations__)
    assert "URLStore" not in str(fetch_fake.fetch.__annotations__)


def test_default_registry_preserves_existing_provider_contracts() -> None:
    registry = build_default_registry()
    assert [item.name for item in registry.list_in_registration_order()[:20]] == [
        "tavily",
        "firecrawl",
        "exa",
        "linkup",
        "brave",
        "anysearch",
        "tinyfish",
        "parallel",
        "brightdata",
        "scrape_do",
        "zenrows",
        "decodo",
        "scrapingdog",
        "scrapegraphai",
        "scraperapi",
        "scrapingant",
        "serpapi",
        "jina",
        "yuanbao",
        "zhihu",
    ]
    registrations = registry.list_in_registration_order()[8:20]

    expected = [
        (
            "brightdata",
            ProviderCapabilities(True, True),
            BrightDataAdapter,
            frozenset({"api_url", "search_zone", "fetch_zone"}),
        ),
        ("scrape_do", ProviderCapabilities(True, True), ScrapeDoAdapter, frozenset({"api_url"})),
        ("zenrows", ProviderCapabilities(False, True), ZenRowsAdapter, frozenset({"api_url"})),
        ("decodo", ProviderCapabilities(True, True), DecodoAdapter, frozenset({"api_url"})),
        (
            "scrapingdog",
            ProviderCapabilities(True, True),
            ScrapingDogAdapter,
            frozenset({"api_url"}),
        ),
        (
            "scrapegraphai",
            ProviderCapabilities(True, True),
            ScrapeGraphAIAdapter,
            frozenset({"api_url"}),
        ),
        ("scraperapi", ProviderCapabilities(True, True), ScraperAPIAdapter, frozenset({"api_url"})),
        (
            "scrapingant",
            ProviderCapabilities(False, True),
            ScrapingAntAdapter,
            frozenset({"api_url"}),
        ),
        ("serpapi", ProviderCapabilities(True, False), SerpApiAdapter, frozenset({"api_url"})),
        ("jina", ProviderCapabilities(False, True), JinaReaderAdapter, frozenset({"api_url"})),
        ("yuanbao", ProviderCapabilities(True, False), YuanbaoAdapter, frozenset({"api_url"})),
        ("zhihu", ProviderCapabilities(True, False), ZhihuAdapter, frozenset({"api_url"})),
    ]

    assert [
        (item.name, item.capabilities, item.factory, item.allowed_config_keys)
        for item in registrations
    ] == expected


@pytest.mark.parametrize(
    ("provider_name", "adapter_type"),
    [("yuanbao", YuanbaoAdapter), ("zhihu", ZhihuAdapter)],
)
def test_service_key_search_registration_contract(
    provider_name: str,
    adapter_type: type[YuanbaoAdapter] | type[ZhihuAdapter],
) -> None:
    registration = build_default_registry().require(provider_name)

    assert registration.factory is adapter_type
    assert registration.capabilities == ProviderCapabilities(search=True, fetch=False)
    assert registration.requires_api_key is True
    assert registration.allowed_config_keys == frozenset({"api_url"})


def test_default_registry_marks_jina_and_open_websearch_credentialless() -> None:
    registry = build_default_registry()

    jina = registry.require("jina")
    assert jina.name == "jina"
    assert jina.capabilities == ProviderCapabilities(search=False, fetch=True)
    assert jina.factory is JinaReaderAdapter
    assert jina.allowed_config_keys == frozenset({"api_url"})
    assert jina.requires_api_key is False
    assert {
        item.name for item in registry.list_in_registration_order() if not item.requires_api_key
    } == {"jina", "open_websearch"}


def test_default_registry_adds_four_dual_capability_providers() -> None:
    registrations = build_default_registry().list_in_registration_order()
    expected = [
        ("doubao", DoubaoAdapter, frozenset({"api_url"}), True),
        ("youcom", YouComAdapter, frozenset({"api_url"}), True),
        ("keenable", KeenableAdapter, frozenset({"api_url"}), True),
        ("open_websearch", OpenWebSearchAdapter, frozenset({"api_url", "engines"}), False),
    ]
    for name, factory, allowed_keys, requires_key in expected:
        matches = [item for item in registrations if item.name == name]
        assert len(matches) == 1, f"{name} must be registered exactly once"
        [registration] = matches
        assert registration.factory is factory
        assert registration.capabilities == ProviderCapabilities(search=True, fetch=True)
        assert registration.allowed_config_keys == allowed_keys
        assert registration.requires_api_key is requires_key
    assert [item.name for item in registrations[20:]] == [name for name, _, _, _ in expected]
