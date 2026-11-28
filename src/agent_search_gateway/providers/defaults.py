"""Built-in first-version web provider registrations."""

from .contracts import ProviderCapabilities
from .registry import ProviderRegistry, WebProviderRegistration
from .web.anysearch import AnySearchAdapter
from .web.baiduqianfan import BaiduQianfanAdapter
from .web.brave import BraveAdapter
from .web.brightdata import BrightDataAdapter
from .web.decodo import DecodoAdapter
from .web.doubao import DoubaoAdapter
from .web.exa import ExaAdapter
from .web.firecrawl import FirecrawlAdapter
from .web.jina import JinaReaderAdapter
from .web.keenable import KeenableAdapter
from .web.langsearch import LangSearchAdapter
from .web.linkup import LinkupAdapter
from .web.open_websearch import OpenWebSearchAdapter
from .web.parallel import ParallelAdapter
from .web.scrape_do import ScrapeDoAdapter
from .web.scrapegraphai import ScrapeGraphAIAdapter
from .web.scraperapi import ScraperAPIAdapter
from .web.scrapingant import ScrapingAntAdapter
from .web.scrapingdog import ScrapingDogAdapter
from .web.serpapi import SerpApiAdapter
from .web.tavily import TavilyAdapter
from .web.tinyfish import TinyFishAdapter
from .web.youcom import YouComAdapter
from .web.yuanbao import YuanbaoAdapter
from .web.zenrows import ZenRowsAdapter
from .web.zhihu import ZhihuAdapter


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
            frozenset(
                {"api_url", "search_limit", "search_highlights", "scrape_formats", "scrape_parsers"}
            ),
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
            frozenset({"api_url", "mode", "search_fetch_policy", "extract_fetch_policy"}),
        ),
        WebProviderRegistration(
            "brightdata",
            ProviderCapabilities(search=True, fetch=True),
            BrightDataAdapter,
            frozenset({"api_url", "search_zone", "fetch_zone"}),
            credential_override_keys=frozenset({"search_zone", "fetch_zone"}),
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
        WebProviderRegistration(
            "jina",
            ProviderCapabilities(search=False, fetch=True),
            JinaReaderAdapter,
            frozenset({"api_url"}),
            requires_api_key=False,
        ),
        WebProviderRegistration(
            "yuanbao",
            ProviderCapabilities(search=True, fetch=False),
            YuanbaoAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "zhihu",
            ProviderCapabilities(search=True, fetch=False),
            ZhihuAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "doubao",
            ProviderCapabilities(search=True, fetch=True),
            DoubaoAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "youcom",
            ProviderCapabilities(search=True, fetch=True),
            YouComAdapter,
            frozenset({"api_url"}),
            requires_api_key=False,
            supports_optional_api_key=True,
            validate_config=YouComAdapter.validate_config,
        ),
        WebProviderRegistration(
            "keenable",
            ProviderCapabilities(search=True, fetch=True),
            KeenableAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "open_websearch",
            ProviderCapabilities(search=True, fetch=True),
            OpenWebSearchAdapter,
            frozenset({"api_url", "engines"}),
            requires_api_key=False,
        ),
        WebProviderRegistration(
            "baiduqianfan",
            ProviderCapabilities(search=True, fetch=False),
            BaiduQianfanAdapter,
            frozenset({"api_url"}),
        ),
        WebProviderRegistration(
            "langsearch",
            ProviderCapabilities(search=True, fetch=False),
            LangSearchAdapter,
            frozenset({"api_url"}),
        ),
    )
    for registration in registrations:
        registry.register(registration)
    return registry
