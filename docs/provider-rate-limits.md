# Provider rate limits

Use this reference when selecting settings for your account. It covers the 24 Web providers, six academic providers and one open-access resolver registered by the gateway. All 31 have an official source. Some sources publish only account-specific limits or courtesy guidance. Source coverage does not establish that a live call succeeded.

## Reading the settings

`N/T` means `{ requests = N, period_seconds = T }` in the provider's `rate_limits` array. Each window counts physical HTTP attempts over the preceding T seconds, including Search, Fetch and retries that use the same provider. All windows apply together. `min_interval_seconds` sets a separate minimum time between starts. `max_concurrency` limits simultaneous work independently. A provider with no rate settings has no local frequency limit. The example settings live in [config.example.toml](../config.example.toml).

Limits apply within one running gateway. Other processes, keys and applications can consume the same upstream allowance. Choose settings for the product, account and endpoints you actually use. Monthly credits, dollar budgets and included usage describe billing and do not supply a request frequency.

## Provider reference

| Config name | Current endpoint or product | Authentication and plan | Published technical limit | Suggested local settings | Official sources | Verified |
|---|---|---|---|---|---|---|
| `tavily` | Search `/search`, Extract `/extract` | Development key | 100 requests/min | 100/60 | [Rate Limits](https://help.tavily.com/articles/3240802908-rate-limits) | 2026-11-19 |
| `firecrawl` | v2 Search and Scrape | Authenticated Free | 10/min for each endpoint. Two browser workers process pages and queue excess work. | Share 10/60. Browser workers do not determine HTTP concurrency. | [Rate limits](https://github.com/firecrawl/firecrawl-docs/blob/main/rate-limits.mdx) | 2026-11-19 |
| `exa` | Ordinary `/search` and `/contents` | Free API key | 10 queries/s for search, 100/s for contents. Deep search has a separate 5/s limit. | Share 10/1 for the two active endpoints. | [Billing](https://exa.ai/docs/admin/billing) | 2026-11-19 |
| `linkup` | `/v1/search` and `/v1/fetch` | Organization API key | 10 queries/s per organization across Search and Fetch. x402 is counted by IP. | Share 10/1 across both operations. | [Rate Limits](https://docs.linkup.so/pages/documentation/platform/rate-limits.md) | 2026-11-19 |
| `brave` | Web Search `/res/v1/web/search` | Search Prepaid, including its free credits | 50 requests/s for this product. Answers and legacy plans have separate terms. | 50/1 on Search Prepaid. | [Pricing](https://api-dashboard.search.brave.com/documentation/pricing) | 2026-11-19 |
| `anysearch` | `/v1/search` | Registered Free with API key | Up to 10 queries/s on Free and up to 20/s on Pro. A key alone does not identify a Pro account. | 10/1 for Free. Exclude the consumable daily allowance. | [Pricing](https://www.anysearch.com/pricing), [Auth](https://www.anysearch.com/docs/auth) | 2026-11-19 |
| `tinyfish` | TinySearch and TinyFetch, one URL per Fetch | Free retrieval services outside the Wallet | Search 30/min and 500/hour. Fetch 150 URLs/min and 1,000 URLs/day. The daily reset algorithm is unconfirmed. | Share 30/60, 500/3600 and 1000/86400. Count a rolling 24 hours locally. | [Pricing](https://www.tinyfish.ai/pricing) | 2026-11-19 |
| `parallel` | Build API `/v1/search` and `/v1/extract` | Build API key, including free usage | 600/min for each endpoint. Launch MCP has variable limits. | Share 600/60 for Build API. | [Pricing](https://parallel.ai/pricing) | 2026-11-19 |
| `brightdata` | `/request` with SERP Search or Web Unlocker Fetch zone | Free Tier account with no funds added, using applicable zones | Default shared 1,000/min for SERP, Web Unlocker and Proxy. Funding removes this Free Tier limit. | Share 1000/60 under these account and zone conditions. | [Free Tier](https://docs.brightdata.com/general/account/billing-and-pricing/free-tier) | 2026-11-19 |
| `scrape_do` | Synchronous Google Search and scraping API | Free token, main synchronous API | 5 concurrent requests. The Async queue has its own limit. Fixed request frequency unknown. | Concurrency 5. Leave windows empty. | [Pricing](https://scrape.do/pricing/) | 2026-11-19 |
| `zenrows` | Universal Scraper API `/v1` Fetch | Free API key | 5 concurrent requests | Concurrency 5. | [Pricing](https://www.zenrows.com/pricing) | 2026-11-19 |
| `decodo` | Legacy Web Scraping API `/v2/scrape` | Free account with legacy Basic access | 10 requests/s under the listed free product terms. Confirm eligibility for the legacy interface. | 10/1 for matching accounts. | [Product](https://decodo.com/scraping/web), [Legacy endpoint](https://decodo.com/scraping/web/rss-scraper-api), [Legacy access](https://help.decodo.com/docs/web-scraping-api-introduction) | 2026-11-19 |
| `scrapingdog` | Google `/google` and `/scrape` | Free API key | 1 concurrent request | Concurrency 1. | [Pricing](https://www.scrapingdog.com/pricing/) | 2026-11-19 |
| `scrapegraphai` | `/api/search` and `/api/scrape` | Free API key | 10 requests/min. Crawl concurrency belongs to a different operation. | Share 10/60. | [Pricing](https://scrapegraphai.com/pricing) | 2026-11-19 |
| `scraperapi` | Structured Google Search and synchronous Fetch | Free API key | 5 concurrent connections | Concurrency 5. | [FAQ](https://docs.scraperapi.com/resources/faq/plans-and-billing) | 2026-11-19 |
| `scrapingant` | v2 Markdown Fetch | Free API key | 1 active request | Concurrency 1. | [v2 Errors](https://docs.scrapingant.com/errors) | 2026-11-19 |
| `serpapi` | Google `/search` | Free API key | Throughput of 50 searches/hour. Success-based billing does not establish how the throughput window counts failures. | 50/3600, counting every local attempt. | [Pricing](https://serpapi.com/pricing) | 2026-11-19 |
| `jina` | Reader Fetch | Anonymous Reader, as used by the adapter | 20 requests/min without a key. Authenticated tiers differ. | 20/60. | [Reader](https://jina.ai/reader/) | 2026-11-19 |
| `yuanbao` | Tencent SearchPro `/SearchPro` | Service key for the account's provisioned edition | Fast and Standard editions allow 20 queries/s, Flagship allows 5/s. Request Mode describes the response type. | 5/1 when the edition is unknown. Use 20/1 after confirming Fast or Standard. | [Product FAQ](https://cloud.tencent.com/document/product/1806/121804) | 2026-11-19 |
| `zhihu` | Site search and global search | Developer account with access secret | Account quotas and over-frequency errors are documented. A common fixed frequency is unknown. | Leave windows empty. Exclude trial usage allowances. | [Quota](https://developer.zhihu.com/docs?key=quota) | 2026-11-19 |
| `doubao` | Custom Search `/web_search` and `/fetch` | Subscription keys support Search alone. Enabling Search and Fetch together requires a pay-as-you-go key with Fetch access. | Default Search is 10 queries/s. A fixed free-only rate is unpublished. Fetch limits depend on the console and subscription Search keys cannot call Fetch. | 10/1 only as a shared starting point for default Search. Apply any lower account or Fetch limit. | [Custom](https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh), [Fetch](https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh), [Errors](https://docs.volcengine.com/docs/networked-search/error-description?lang=zh) | 2026-11-19 |
| `youcom` | `/v1/search` and `/v1/contents` | Self-serve API key | 10 requests/s for each endpoint | Share 10/1. | [Rate limits](https://you.com/docs/rate-limits) | 2026-11-19 |
| `keenable` | `/v1/search` and `/v1/fetch` | Authenticated organization | Default 10 requests/s across all organization keys and both operations. Authentication removes the anonymous 1,000/hour/IP cap. | Share 10/1. | [Rate limits](https://docs.keenable.ai/rate-limits) | 2026-11-19 |
| `open_websearch` | Self-hosted `/search` and `/fetch-web` | Operator-managed daemon and selected engines | A common fixed frequency is unknown. Individual engines set their own policies. | Leave windows empty or configure your deployment's limit. | [Project](https://github.com/Aas-ee/open-webSearch) | 2026-11-19 |
| `arxiv` | Legacy Atom query API | Public access | One connection and at least 3 seconds between requests | Concurrency 1 and interval 3 s. | [API ToU](https://info.arxiv.org/help/api/tou.html) | 2026-11-19 |
| `semantic_scholar` | Academic Graph paper search | Optional API key. Unauthenticated requests share a pool. | Initial keyed rate 1 request/s. Anonymous callers have no dedicated rate guarantee. | 1/1. In anonymous mode this is only a local limit. | [API](https://webflow.semanticscholar.org/product/api) | 2026-11-19 |
| `openalex` | Works search `/works` | Free API key required by this gateway | Ordinary requests 30/s. Searches with more than 5 Boolean operators allow 2/s without bursts. Certain broad topic/concept OR filters and semantic search allow 1/s. This adapter sends neither. | 30/1 plus interval 0.5 s across all queries, since user queries can contain complex Boolean expressions. | [Authentication](https://help.openalex.org/api/authentication/) | 2026-11-19 |
| `dblp` | Publication search `/search/publ/api` | Public access | Hard frequency unknown. The operator recommends spacing requests 1–2 seconds apart and respecting Retry-After. | Leave windows empty. Use a courtesy interval of 2 s. | [Search API](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html), [Crawling guidance](https://dblp.org/faq/Am%2BI%2Ballowed%2Bto%2Bcrawl%2Bthe%2Bdblp%2Bwebsite.html) | 2026-11-19 |
| `crossref` | REST `/works` list | Public pool, or polite pool with contact email | Public 1 request/s and 1 concurrent request. Polite 3/s and 3 concurrent requests. | 1/1 and concurrency 1 work for either pool. | [REST limits](https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/) | 2026-11-19 |
| `core` | API v3 `/search/works` | API key | 5 single requests or 1 batch request per 10 s. The published categories do not identify this endpoint unambiguously. | 1/10 is a conservative local choice. | [API](https://core.ac.uk/services/api) | 2026-11-19 |
| `unpaywall` | DOI lookup `/v2/:doi` | Contact email required | 100,000 calls/day technical usage limit | 100000/86400 if enabled. The example leaves it disabled. | [API](https://unpaywall.org/api), [API overview](https://data.unpaywall.org/products/api) | 2026-11-19 |

## Optional Web provider configurations

The example file omits these four providers. To enable one, copy its table into your configuration and supply the named environment variable to the daemon. If the table already exists, edit its fields in place. The Decodo variable holds the credential for the legacy Basic authorization header.

```toml
[web_providers.zenrows]
enable_search = false
enable_fetch = true
api_url = "https://api.zenrows.com"
api_key_env = "ZENROWS_API_KEY"
max_concurrency = 5

[web_providers.decodo]
enable_search = true
enable_fetch = true
api_url = "https://scraper-api.decodo.com"
api_key_env = "DECODO_BASIC_CREDENTIAL"
rate_limits = [{ requests = 10, period_seconds = 1 }]

[web_providers.scrapingdog]
enable_search = true
enable_fetch = true
api_url = "https://api.scrapingdog.com"
api_key_env = "SCRAPINGDOG_API_KEY"
max_concurrency = 1

[web_providers.scrapegraphai]
enable_search = true
enable_fetch = true
api_url = "https://v2-api.scrapegraphai.com"
api_key_env = "SCRAPEGRAPHAI_API_KEY"
rate_limits = [{ requests = 10, period_seconds = 60 }]
```
