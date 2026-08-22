from agent_search_gateway.providers.contracts import (
    KeywordSearchHit,
    ProviderCapabilities,
    URLFetchCandidate,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import ProviderRegistry, WebProviderRegistration
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.decodo import DecodoAdapter
from agent_search_gateway.providers.web.scrape_do import ScrapeDoAdapter
from agent_search_gateway.providers.web.scrapegraphai import ScrapeGraphAIAdapter
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.providers.web.scrapingant import ScrapingAntAdapter
from agent_search_gateway.providers.web.scrapingdog import ScrapingDogAdapter
from agent_search_gateway.providers.web.serpapi import SerpApiAdapter
from agent_search_gateway.providers.web.zenrows import ZenRowsAdapter
from tests.support.fakes import FakeKeywordSearchProvider, FakeURLFetchProvider


def _factory() -> object:
    return object()


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


def test_default_registry_appends_new_providers_with_exact_contracts() -> None:
    registry = build_default_registry()
    registrations = registry.list_in_registration_order()[-9:]

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
    ]

    assert [
        (item.name, item.capabilities, item.factory, item.allowed_config_keys)
        for item in registrations
    ] == expected
