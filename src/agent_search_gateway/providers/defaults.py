"""Built-in first-version web provider registrations."""

from .contracts import ProviderCapabilities
from .registry import ProviderRegistry, WebProviderRegistration
from .web.anysearch import AnySearchAdapter
from .web.brave import BraveAdapter
from .web.brightdata import BrightDataAdapter
from .web.decodo import DecodoAdapter
from .web.exa import ExaAdapter
from .web.firecrawl import FirecrawlAdapter
from .web.linkup import LinkupAdapter
from .web.parallel import ParallelAdapter
from .web.scrape_do import ScrapeDoAdapter
from .web.scrapegraphai import ScrapeGraphAIAdapter
from .web.scraperapi import ScraperAPIAdapter
from .web.scrapingant import ScrapingAntAdapter
from .web.scrapingdog import ScrapingDogAdapter
from .web.serpapi import SerpApiAdapter
from .web.tavily import TavilyAdapter
from .web.tinyfish import TinyFishAdapter
from .web.zenrows import ZenRowsAdapter


def build_default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registrations = (
        WebProviderRegistration(
            "tavily",
            ProviderCapabilities(search=True, fetch=True),
            TavilyAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "firecrawl",
            ProviderCapabilities(search=True, fetch=True),
            FirecrawlAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "exa",
            ProviderCapabilities(search=True, fetch=True),
            ExaAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "linkup",
            ProviderCapabilities(search=True, fetch=True),
            LinkupAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "brave",
            ProviderCapabilities(search=True, fetch=False),
            BraveAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "anysearch",
            ProviderCapabilities(search=True, fetch=False),
            AnySearchAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "tinyfish",
            ProviderCapabilities(search=True, fetch=True),
            TinyFishAdapter,
            frozenset({"search_api_url", "fetch_api_url"}),
        ),
        WebProviderRegistration(
            "parallel",
            ProviderCapabilities(search=True, fetch=True),
            ParallelAdapter,
            frozenset(
                {"api_url", "mode", "search_fetch_policy", "extract_fetch_policy"}
            ),
        ),
        WebProviderRegistration(
            "brightdata",
            ProviderCapabilities(search=True, fetch=True),
            BrightDataAdapter,
            frozenset({"api_url", "search_zone", "fetch_zone"}),
        ),
        WebProviderRegistration(
            "scrape_do",
            ProviderCapabilities(search=True, fetch=True),
            ScrapeDoAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "zenrows",
            ProviderCapabilities(search=False, fetch=True),
            ZenRowsAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "decodo",
            ProviderCapabilities(search=True, fetch=True),
            DecodoAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "scrapingdog",
            ProviderCapabilities(search=True, fetch=True),
            ScrapingDogAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "scrapegraphai",
            ProviderCapabilities(search=True, fetch=True),
            ScrapeGraphAIAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "scraperapi",
            ProviderCapabilities(search=True, fetch=True),
            ScraperAPIAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "scrapingant",
            ProviderCapabilities(search=False, fetch=True),
            ScrapingAntAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "serpapi",
            ProviderCapabilities(search=True, fetch=False),
            SerpApiAdapter,
            frozenset({"api_url"}),
        ),
    )
    for registration in registrations:
        registry.register(registration)
    return registry
