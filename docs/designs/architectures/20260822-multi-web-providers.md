## Architecture: Additional Web Search and Fetch Providers

### 1. Scope & Assumptions

#### In Scope

- Add nine built-in web-provider integrations with the smallest practical change to the existing provider architecture: Bright Data, Scrape.do, ZenRows, Decodo, ScrapingDog, ScrapeGraphAI, ScraperAPI, ScrapingAnt, and SerpApi.
- Explicitly exclude Apify from this design; it will be handled separately.
- Preserve the existing gateway provider contracts:
  - `KeywordSearchProvider.search(query) -> list[KeywordSearchHit]`
  - `URLFetchProvider.fetch(url) -> URLFetchCandidate`
- Preserve the existing `ProviderRegistry` registration model, common web-provider configuration fields, runtime assembly, quota model, keyword-search fan-out, fetch scheduler, URL store, CLI/socket protocol, and result-file contracts.
- Register each provider only for capabilities that map directly to a current, supported provider API without adding local SERP parsing, AI extraction schemas, or provider-specific orchestration.
- Expose only provider-specific configuration required for the selected basic integration. Optional provider tuning controls are intentionally omitted.
- Extend the existing shared HTTP transport with a narrow text-response operation because several selected fetch APIs return Markdown or HTML directly rather than JSON.
- Keep all provider authentication/request/response details inside adapters.

#### Provider Capability and Minimal Configuration Matrix

| Registry name | Provider | Search | Fetch | Provider-specific required config | Selected provider API |
|---|---|---:|---:|---|---|
| `brightdata` | Bright Data | yes | yes | `api_url`, `search_zone`, `fetch_zone` | SERP API + Web Unlocker `/request` |
| `scrape_do` | Scrape.do | yes | yes | `api_url` | Google Search plugin + scrape Markdown output |
| `zenrows` | ZenRows | no | yes | `api_url` | current Fetch API |
| `decodo` | Decodo | yes | yes | `api_url` | Web Scraping API Google Search template + Markdown response |
| `scrapingdog` | ScrapingDog | yes | yes | `api_url` | Google Search API + Web Scraping API |
| `scrapegraphai` | ScrapeGraphAI | yes | yes | `api_url` | v2 Search + Scrape APIs |
| `scraperapi` | ScraperAPI | yes | yes | `api_url` | synchronous Google SERP API + synchronous scrape API |
| `scrapingant` | ScrapingAnt | no | yes | `api_url` | v2 Markdown endpoint |
| `serpapi` | SerpApi | yes | no | `api_url` | Google Search API |

`api_key_env`, `enable_search`, `enable_fetch`, and `max_concurrency` remain the existing shared web-provider configuration fields and are not repeated in the provider-specific column.

#### Todo

- Apify integration and Actor selection/configuration.
- ZenRows keyword search through its current dedicated Google Search Results API. This batch keeps ZenRows fetch-only because the search API uses a separate `serp.api.zenrows.com` endpoint and a clean second-endpoint configuration is deferred rather than hard-coded into the adapter.
- ScrapingAnt keyword search. The current selected API exposes scraping/Markdown/AI extraction rather than a dedicated deterministic structured SERP contract; this design does not add a Google SERP parser or an AI extraction prompt/schema merely to synthesize `KeywordSearchHit` values.
- Optional provider controls such as country, language, locale, device, JavaScript rendering, premium/residential proxy selection, result count, pagination, freshness/cache controls, safe search, sessions, and provider-specific advanced modes.
- Generic provider option passthrough or a declarative HTTP-provider DSL.
- Provider-specific async-job APIs, batch APIs, crawl APIs, monitor APIs, browser sessions, screenshots, or webhook flows.
- Generalized support for multiple credentials per provider. The selected integrations fit the current one-opaque-secret model.
- Renaming `HttpJsonExecutor`; the existing class name is retained to avoid a broad compatibility-only refactor even though it gains text-response support.

#### Assumptions

- The gateway continues to treat provider names as stable configuration identifiers. `scrape_do` is used instead of `scrape.do` because a dot in an unquoted TOML table key has nesting semantics.
- Search integrations use the provider's basic/default Google/web search behavior and do not force optional localization, pagination, device, or result-count settings.
- Fetch integrations request Markdown when the provider has a direct Markdown mode; otherwise they use the provider's normal page body. Returned Markdown is acceptable as both `raw_content` and `content` when no distinct raw representation is requested in the minimal integration.
- Provider query-string credentials are acceptable where required by the provider API because the existing transport logging path strips query strings from logged endpoints and does not intentionally log request bodies or credentials.
- Decodo's `api_key_env` value contains the opaque token expected after the Basic authentication scheme. The gateway does not split it into username/password fields.
- Bright Data requires distinct configured zone names for the selected SERP and Web Unlocker products even though both requests use the same `/request` HTTP endpoint and secret.
- `HttpJsonExecutor` remains responsible for request timeout, retry, HTTP-status classification, transport-failure mapping, and HTTP lifecycle logging for both JSON and text response modes.
- Existing orchestrators remain responsible for provider quota leasing, candidate type validation, URL normalization, duplicate handling, body semantic validation, URL-store mutation, fallback behavior, and result persistence.
- Provider APIs may return extra metadata. Adapters discard fields that do not map to existing gateway contracts.

---

### 2. Architecture Summary

The nine providers are added as ordinary built-in adapters behind the existing search/fetch protocols and registered through the existing `ProviderRegistry`. No provider-specific branch is added to central config resolution, runtime assembly, `SearchOrchestrator`, `FetchScheduler`, `URLStore`, CLI, or socket protocol. Each adapter owns only authentication, endpoint construction, minimal provider-specific request shape, response parsing, and mapping into `KeywordSearchHit` / `URLFetchCandidate`. The only shared runtime behavior change is a narrow `HttpJsonExecutor.request_text(...)` operation that reuses the executor's existing retry/status/logging boundary and returns a successful response body as text rather than decoding JSON. Providers are registered only for APIs that naturally satisfy current contracts: ZenRows and ScrapingAnt are fetch-only in this iteration, while SerpApi is search-only. This avoids deprecated endpoints, local SERP parsers, provider-specific AI extraction schemas, and new core abstractions.

---

### 3. Design Decisions

#### Runtime Model

##### Keep Every New Integration as a Normal Built-In Web Adapter

- Description: Add one adapter module per provider under `providers/web/`, then register it in `build_default_registry()` with static capabilities, factory, and explicit allowed configuration keys.
- Rationale: The repository already isolates third-party HTTP schemas behind thin adapters. All selected APIs can be represented by the existing search/fetch contracts, so a new provider framework or runtime path is unnecessary.
- Trade-offs: Nine adapters introduce some repetitive request/response mapping code, and vendor API changes must be maintained independently.
- Rejected Alternatives:
  - Generic declarative HTTP provider framework: rejected because it expands configuration/security/validation surface far beyond this feature.
  - Provider-specific orchestrators: rejected because they would duplicate quota, fallback, normalization, semantic validation, and persistence.
  - One combined adapter for similar scraping APIs: rejected because request similarity hides materially different response/error/auth contracts and would move vendor conditionals into a shared component.

##### Reuse One Existing Web Quota Per Provider Across Search and Fetch

- Description: Providers that implement both stages use the same adapter instance and the same `ProviderQuotaManager` entry, exactly like current dual-capability providers.
- Rationale: Runtime assembly and scheduler semantics already define one provider-level capacity budget across supported web stages.
- Trade-offs: A provider's search traffic can consume capacity otherwise available to its fetch traffic.
- Rejected Alternatives:
  - Separate search/fetch quotas: rejected because it would change a core concurrency rule only for selected vendors.

#### Interface / Protocol

##### Preserve Gateway Search and Fetch Contracts Without Vendor Fields

- Description: Do not change `KeywordSearchProvider`, `URLFetchProvider`, `KeywordSearchHit`, `URLFetchCandidate`, CLI commands, NDJSON protocol, `SearchRecord`, or result JSONL shape.
- Rationale: The selected APIs have enough information to map into current domain objects; optional vendor metadata is not consumed by the gateway.
- Trade-offs: Search positions, dates, provider request IDs, usage data, SERP feature blocks, and other provider metadata are discarded.
- Rejected Alternatives:
  - Generic metadata dictionaries: rejected because no current core consumer needs them.
  - Provider-specific CLI/socket controls: rejected because vendor semantics must not leak into public protocol boundaries.

##### Add Text Responses at the Shared HTTP Boundary, Not Inside Adapters

- Description: Extend `HttpJsonExecutor` with `request_text(method, url, *, stage, headers=None, json_body=None) -> str`. It reuses the same HTTP execution/retry/status/logging path as `request_json()` but returns `response.text` after successful HTTP validation instead of calling `response.json()`.
- Rationale: Bright Data Web Unlocker Markdown, Scrape.do Markdown, ZenRows Fetch Markdown, Decodo Markdown, ScrapingDog page scraping, and ScraperAPI synchronous scraping can return non-JSON bodies. Allowing adapters to use `httpx` directly would duplicate the repository's transport policy and bypass central redaction-oriented logging.
- Trade-offs: `HttpJsonExecutor` becomes slightly broader than its name suggests. Retaining the name avoids broad compatibility-only churn.
- Rejected Alternatives:
  - Rename the executor to `HttpExecutor`: rejected as unrelated broad churn.
  - Use `httpx.AsyncClient` directly in text adapters: rejected because timeout/retry/status/error/logging behavior would be duplicated.
  - Force raw Markdown/HTML through JSON parsing: rejected because it is not a JSON protocol.

##### Keep Narrow Requester Protocols

- Description: Preserve existing `JsonRequester`. Add a text-capable protocol, and optionally a combined structural protocol for adapters using both JSON and text. Existing JSON-only adapters do not need annotation changes.
- Rationale: Structural protocols let adapters depend only on the transport behavior they use and keep deterministic test doubles narrow.
- Trade-offs: There are two small requester protocols instead of one.
- Rejected Alternatives:
  - Add `request_text()` to `JsonRequester`: rejected because it would unnecessarily break/change every existing JSON-only test double's structural contract.

##### Keep URL Query Construction in Adapters

- Description: Continue using `urllib.parse.urlencode` / quoting in adapters for APIs that put secrets, target URLs, or queries in the query string. Do not add generic `params` transport plumbing solely for these providers.
- Rationale: TinyFish already demonstrates this approach; the existing executor sanitizes logged endpoints by removing query strings.
- Trade-offs: URL-building boilerplate is repeated in several small adapters.
- Rejected Alternatives:
  - New generic query-param transport API: rejected because it is not necessary to satisfy the integration.

##### Keep Provider-Specific Configuration Explicit and Whitelisted

- Description: Register only the required provider-specific keys shown in the capability matrix. Existing common keys remain centrally parsed. Adapter constructors validate provider-specific required strings such as Bright Data zones.
- Rationale: This is the established configuration model and catches typos/unsupported controls at startup without vendor branches in `config.py`.
- Trade-offs: Adding an optional control later requires an explicit registry and adapter change.
- Rejected Alternatives:
  - Arbitrary vendor option passthrough: rejected because it weakens startup validation and compatibility reasoning.
  - Provider-specific branches in `config.py`: rejected because central config is provider-agnostic and adapter construction is already the provider-specific validation boundary.

#### State Management

##### Add No Provider Session or Job State

- Description: Use synchronous/request-response APIs only. Do not persist provider sessions, cookies, async task IDs, crawl IDs, or request IDs.
- Rationale: Current search/fetch contracts are one-call operations and the gateway has no provider-session model.
- Trade-offs: Async/batch products and cross-call session reuse are unavailable.
- Rejected Alternatives:
  - Store vendor session/job identifiers in `URLStore`: rejected because it introduces vendor-specific mutable state into a core URL/body store.

#### Storage / Persistence

##### Preserve Existing Search Admission and Fetch Persistence

- Description: Adapters only return domain candidates. `SearchOrchestrator` remains responsible for URL normalization/deduplication/search admission and optional search-body admission; fetch orchestration remains responsible for semantic body acceptance and final URL-store mutation.
- Rationale: This preserves the current ownership boundary and keeps vendor adapters side-effect free outside HTTP mapping.
- Trade-offs: Provider-specific response metadata is not persisted.
- Rejected Alternatives:
  - Persist raw provider responses: rejected because it expands storage/privacy exposure and conflicts with the no-payload observability model.

#### Provider Integration

##### Bright Data Uses SERP API for Search and Web Unlocker for Fetch

- Description: `BrightDataAdapter` uses configured `api_url` and `/request`. Search sends a Google Search URL with `search_zone`, requests structured JSON, and maps organic entries to URL/title/snippet. Fetch sends the normalized URL with `fetch_zone` and requests Markdown/raw text suitable for `URLFetchCandidate`.
- Rationale: Bright Data exposes structured SERP data and general page unlocking as separate products sharing one HTTP endpoint but using distinct zones.
- Trade-offs: Adapter construction requires both zone names whenever Bright Data is enabled, even if only one gateway stage is enabled. This follows the existing simple option/constructor plumbing rather than adding stage-dependent validation machinery.
- Rejected Alternatives:
  - Infer/hard-code zone names: rejected because zones are account-defined.
  - Parse Google HTML through Web Unlocker: rejected because native structured SERP exists.

##### Scrape.do Uses Structured Google Search and Markdown Scrape Output

- Description: Search calls `/plugin/google/search` with token + `q`, maps `organic_results`, and leaves optional controls at provider defaults. Fetch calls the normal scrape endpoint with target URL and `output=markdown`, returning text as both raw and cleaned content.
- Rationale: Both operations are synchronous and directly match gateway contracts.
- Trade-offs: The token is placed in the provider-required query parameter.
- Rejected Alternatives:
  - Expose localization/device/page controls now: rejected as optional tuning.

##### ZenRows Is Fetch-Only in This Batch

- Description: Register `zenrows` with `search=False, fetch=True`. `ZenRowsAdapter.fetch()` uses the selected ZenRows page-fetch path and requests Markdown for the normalized URL.
- Rationale: ZenRows currently exposes a dedicated structured Google Search Results API on the separate `serp.api.zenrows.com` host. This batch intentionally keeps the existing one-`api_url` ZenRows configuration focused on page fetch; adding search cleanly would require selecting and whitelisting a second endpoint rather than silently hard-coding or deriving a host that the configured `api_url` does not control.
- Trade-offs: ZenRows cannot participate in `keyword-search` in this version even though the vendor now offers a native structured SERP API.
- Rejected Alternatives:
  - Hard-code a second SERP host while leaving only `api_url` configurable: rejected because endpoint configuration would become inconsistent and difficult to test or override.
  - Fetch Google and parse locally: rejected as brittle SERP parser coupling.
  - AI Extract to synthesize results: rejected due prompt/schema semantics, cost, and nondeterminism.

##### Decodo Uses Current Google Search Template and Markdown Response

- Description: Search sends `POST /v2/scrape` with `target="google_search"`, the query, and `parse=true`, then maps parsed organic results. Fetch sends the target URL with `markdown=true` to the same real-time endpoint and treats successful Markdown text as the candidate. Authentication uses `Authorization: Basic <opaque secret>`.
- Rationale: Decodo's current all-in-one Web Scraping API supplies both a dedicated parsed Google Search template and direct Markdown output without local parsing.
- Trade-offs: The configured secret is an opaque Basic token rather than a semantically named API key, but the current one-secret model is sufficient.
- Rejected Alternatives:
  - Username/password core config: rejected because the selected API accepts one token and current secret plumbing is sufficient.
  - Legacy plan-specific endpoints: rejected because the current API provides required capabilities.

##### ScrapingDog Uses Google Search API and Web Scraping API

- Description: Search calls `/google` with `api_key` + `query` and maps `organic_results[].link/title/snippet`. Fetch calls `/scrape` with `api_key` + target `url` and returns page text.
- Rationale: Separate synchronous APIs directly match both gateway stages.
- Trade-offs: Credentials are included in query parameters per the documented minimal API form.
- Rejected Alternatives:
  - `deep_scrape` during search: rejected because it changes cost/behavior and duplicates the gateway fetch stage.

##### ScrapeGraphAI Uses Only v2 Search and Scrape

- Description: Authenticate with `SGAI-APIKEY`, call `POST /api/search` for search and `POST /api/scrape` for fetch, requesting Markdown for fetched content. Search maps only stable fields needed by the gateway; inline full page content is mapped to body fields only if present in the documented stable result shape.
- Rationale: v2 explicitly exposes synchronous JSON Search and Scrape on one host; v1 is deprecated.
- Trade-offs: Advanced Extract/Crawl/Monitor/History capabilities are unused.
- Rejected Alternatives:
  - v1 endpoints: rejected as deprecated.
  - Extract for fetch: rejected because gateway fetch needs page body, not a custom extraction schema.

##### ScraperAPI Uses Synchronous Structured Google SERP and Synchronous Scraping

- Description: Search calls `/structured/google/search` with only `api_key` + `query` and parses JSON organic results. Fetch calls the synchronous scrape API with `api_key` + target URL and returns text.
- Rationale: Synchronous endpoints fit existing one-call protocols and avoid polling/task state.
- Trade-offs: Basic fetch is not guaranteed to be provider-cleaned Markdown; existing downstream semantic/clean stages remain responsible for usability.
- Rejected Alternatives:
  - Async structured/batch endpoints: rejected because they require task IDs/polling/webhooks.
  - Optional render/premium/localization controls: rejected as out of scope.

##### ScrapingAnt Is Fetch-Only Using the Markdown Endpoint

- Description: Register `scrapingant` with `search=False, fetch=True`. Fetch calls `/v2/markdown` with target URL + `x-api-key`, parses JSON `markdown`, and returns it as both raw and cleaned content.
- Rationale: The Markdown endpoint deterministically satisfies `URLFetchProvider`; current general/Markdown/AI Extract APIs do not provide a dedicated structured search result contract.
- Trade-offs: ScrapingAnt does not participate in keyword search.
- Rejected Alternatives:
  - Scrape Google and parse HTML: rejected because it adds a SERP parser.
  - AI Extract organic results: rejected because it adds nondeterministic prompt/schema semantics to emulate native search.

##### SerpApi Is Search-Only Using Google Search API

- Description: Register `serpapi` with `search=True, fetch=False`. Search calls the configured Search API with fixed `engine=google`, `q=<query>`, and the secret, then maps `organic_results[].link/title/snippet`.
- Rationale: SerpApi directly satisfies structured keyword search and is not a general arbitrary-page fetch service.
- Trade-offs: Later content fetch for a SerpApi-admitted URL must use another fetch-capable provider.
- Rejected Alternatives:
  - Treat cached/raw SERP artifacts as URL fetch: rejected because they are search-result-page artifacts, not arbitrary admitted target pages.

##### Preserve Existing Per-Result Search Tolerance

- Description: Malformed individual organic/result entries are skipped when isolated to one entry. Malformed required top-level response structure remains a provider pipeline failure.
- Rationale: Matches current adapter behavior and prevents one malformed result from discarding otherwise valid search hits.
- Trade-offs: Partial provider schema issues can yield fewer hits without failing the whole provider.
- Rejected Alternatives:
  - Fail on first malformed entry: rejected as more brittle than current convention.
  - Return empty list for malformed top-level data: rejected because it hides provider protocol drift.

##### Use Existing Search Body Semantics Conservatively

- Description: Structured SERP providers normally populate URL/title/snippet only. Stable full page content returned inline may populate body fields, but excerpts/SERP snippets are never represented as a full body merely to avoid later fetches.
- Rationale: Search/body semantics already exist centrally and should not be weakened by provider-specific shortcuts.
- Trade-offs: Some deep-search products are underused in the minimal integration.
- Rejected Alternatives:
  - Map any search text into `content`: rejected because snippets are not full page bodies.

#### Concurrency / Scheduling

##### Reuse Existing Search Fan-Out and Fetch Fallback

- Description: Search-capable adapters automatically participate in existing `asyncio.gather` fan-out under quotas; fetch-capable adapters participate in existing capacity-aware `FetchScheduler` fallback.
- Rationale: Capabilities and adapter protocols already contain all core orchestration information.
- Trade-offs: This change does not optimize provider ordering by latency, price, or vendor class.
- Rejected Alternatives:
  - Special scraping-provider tier/order: rejected as a separate policy/behavior change.

#### Security

##### Keep One Opaque Secret Per Provider

- Description: Continue resolving only `api_key_env` into `SecretValue`. Adapters reveal it only while constructing required Bearer, Basic, header, or query credentials.
- Rationale: Every selected minimal integration can authenticate with one secret/token.
- Trade-offs: Future modes requiring multiple independent credentials need their own design.
- Rejected Alternatives:
  - Generic username/password/token fields now: rejected as unnecessary core expansion.

##### Do Not Log Query Credentials or Response Bodies

- Description: Reuse `http_endpoint_for_log()` so query strings are excluded from transport endpoint logs. `request_text()` emits only operational metadata and never fetched Markdown/HTML.
- Rationale: Several providers put secrets in query parameters, while fetched pages may contain sensitive content.
- Trade-offs: Debug logs cannot show exact query parameters or raw bodies.
- Rejected Alternatives:
  - Full request URLs/payload logs: rejected because they risk secret/content leakage.

#### Observability

##### Reuse Existing HTTP and Provider Lifecycle Events

- Description: `request_text()` emits the existing HTTP attempt/retry/completion/failure event family. Orchestrators continue existing provider/candidate/fallback events with the new provider names.
- Rationale: Current events already have provider + stage dimensions needed for diagnostics.
- Trade-offs: Vendor-native usage/request IDs/warnings are not promoted into telemetry.
- Rejected Alternatives:
  - Per-provider logging schemas: rejected as inconsistent and higher-risk for payload leakage.

#### Future Migration

##### Generalize Only After Repeated Requirements Are Proven

- Description: Keep auth, response paths, optional features, and request shapes local to adapters. Generalize only the repeated capability clearly shared by several providers in this batch: successful raw text responses at the HTTP transport boundary.
- Rationale: This preserves YAGNI while preventing duplicated transport policy.
- Trade-offs: Some adapter code remains repetitive.
- Rejected Alternatives:
  - Generic SERP/scrape base classes now: rejected because response envelopes, auth placement, URL matching, output formats, and error semantics differ materially.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| Built-in `ProviderRegistry` registrations | Advertise support/config | Register nine names, capabilities, factories, allowed option keys | Existing registry APIs | capabilities + adapters | Registration map | Registry |
| `resolve_web_provider_config` | Resolve common provider config | Existing shared keys, capability checks, option whitelist, one secret | Existing function | Registry, env, `SecretValue` | No | Validator / transformer |
| `Runtime._build_web_providers` | Instantiate adapters | Existing executor creation + reserved args + options | Existing assembly | Config, registry | Adapter/executor refs | Coordinator / factory |
| `HttpJsonExecutor` | Shared transport policy | Existing JSON requests plus text requests; timeout/retry/status/logging | `request_json`, new `request_text` | `httpx`, retry, observability | HTTP client | Transport boundary |
| `JsonRequester` | JSON adapter transport contract | Existing JSON method | `request_json` | None | No | Interface |
| New text requester protocol | Text adapter transport contract | `request_text`; optionally compose with JSON protocol | `request_text` | None | No | Interface |
| Nine new adapters | Isolate vendor APIs | Auth, endpoints, minimal requests, response mapping | `search` and/or `fetch` | requester protocols + helpers | Immutable config | Adapter / transformer |
| `SearchOrchestrator` | Coordinate searches | Fan-out, quotas, validation, normalization/dedup, admission/results | Existing `keyword_search` | providers, quotas, store | Workflow aggregation | Coordinator |
| `FetchScheduler` | Coordinate fetch fallback | Capacity selection, candidate/body checks, semantic validation/fallback | Existing `fetch_until_accepted` | providers, quotas, stages | Per-call attempts | Scheduler / validator |
| `URLStore` / `ResultWriter` | Preserve state/output | Existing URL/body state and unchanged result files | Existing APIs | Orchestrators | Yes | Store / sink |

Ownership boundary: vendor adapters must not know about `URLStore`, result files, CLI/socket protocol, provider quotas, LLM body judgment, or cross-provider fallback. Core components must not know vendor endpoint fields, auth header names, SERP response paths, or page-format controls.

---

### 5. Data Flow

#### 5.1 Daemon Startup

```text
startup
  -> build_default_registry
       add nine registrations with static capabilities and option whitelists
  -> resolve_web_provider_config
       parse existing common keys
       validate registration + requested capabilities
       reject unknown provider options
       resolve existing single SecretValue
  -> Runtime._build_web_providers
       create existing HttpJsonExecutor per enabled provider
       construct adapter(name, secret, http_executor, **options)
       adapter validates required vendor strings (e.g. Bright Data zones)
       constructor TypeError -> existing ConfigFailure path
       append same adapter to enabled search/fetch tuples
  -> existing quota manager + SearchOrchestrator + FetchScheduler
```

#### 5.2 Structured Keyword Search

```text
SearchOrchestrator.keyword_search(query)
  -> existing validation
  -> gather every enabled KeywordSearchProvider pipeline

provider pipeline
  -> existing provider quota
  -> adapter.search(query)
       build provider-native minimal request
       -> HttpJsonExecutor.request_json
            existing retry/status/logging
            invalid JSON -> existing ProtocolFailure
       validate required top-level provider shape
       for each result:
         try map URL/title/snippet
         if isolated malformed result: skip it
       return list[KeywordSearchHit]
  -> existing hit validation
  -> existing abstract = snippet or title
  -> existing URL normalization/dedup/admission

one provider fails -> keep other completed pipelines
all fail -> existing ALL_PROVIDERS_FAILED
```

ZenRows and ScrapingAnt cannot enter this flow because their static registrations declare `search=False`; attempts to enable search fail during existing config capability validation.

#### 5.3 JSON-Returning URL Fetch

```text
FetchScheduler.fetch_until_accepted(url)
  -> select provider under existing capacity rules
  -> adapter.fetch(url)
       -> HttpJsonExecutor.request_json
       -> parse provider JSON
       -> require non-empty page/Markdown body
       -> URLFetchCandidate
  -> existing candidate type validation
  -> existing cheap_check + judge
  -> accepted => return
  -> failure/rejection => existing fallback
```

#### 5.4 Text-Returning URL Fetch

```text
FetchScheduler.fetch_until_accepted(url)
  -> select provider under existing capacity rules
  -> adapter.fetch(url)
       -> HttpJsonExecutor.request_text
            use same shared retry/status/logging path as request_json
            success => response.text
            no JSON decoding
            never log response body
       -> reject empty body at adapter boundary
       -> URLFetchCandidate(raw_content=text, content=<text when cleaned/Markdown>)
  -> existing candidate validation + semantic validation + fallback
```

#### 5.5 Shared Text Transport

```text
request_text(...)
  -> shared/private execute_response(...)
       retry 408 / 429 / 5xx exactly as existing behavior
       retry timeout/transport exactly as existing behavior
       non-retryable >=400 -> existing ExecutionFailure
       log sanitized endpoint only
       return successful httpx.Response
  -> response.text

request_json(...)
  -> same shared/private execute_response(...)
  -> response.json()
       decode failure -> existing ProtocolFailure
```

The private helper name is not a contract. The requirement is one transport-policy implementation for both response modes.

---

### 6. Interfaces & Contracts

#### Existing Provider Contracts — Unchanged

```python
class KeywordSearchProvider(Protocol):
    name: str
    async def search(self, query: str) -> list[KeywordSearchHit]: ...

class URLFetchProvider(Protocol):
    name: str
    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...
```

Classification: internal but architecturally stable provider contracts. No vendor-specific fields are added.

#### Existing Domain Objects — Unchanged

```python
@dataclass(frozen=True, slots=True)
class KeywordSearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    raw_content: str = ""
    content: str = ""

@dataclass(frozen=True, slots=True)
class URLFetchCandidate:
    raw_content: str
    content: str = ""
```

Classification: migration-stable internal domain contracts used by orchestrators/scheduler.

#### New Text Transport Contract

```python
class TextRequester(Protocol):
    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> str: ...
```

`HttpJsonExecutor` implements this in addition to existing `request_json()`. Existing `JsonRequester` remains unchanged.

Classification: internal adapter/transport contract only.

#### Provider Registration Contract

```text
brightdata:    search+fetch; options api_url, search_zone, fetch_zone
scrape_do:     search+fetch; options api_url
zenrows:       fetch;        options api_url
decodo:        search+fetch; options api_url
scrapingdog:   search+fetch; options api_url
scrapegraphai: search+fetch; options api_url
scraperapi:    search+fetch; options api_url
scrapingant:   fetch;        options api_url
serpapi:       search;       options api_url
```

Existing shared keys remain unchanged: `enable_search`, `enable_fetch`, `api_key_env`, `max_concurrency`.

#### Minimal Configuration Examples

```toml
[web_providers.brightdata]
enable_search = true
enable_fetch = true
api_url = "https://api.brightdata.com"
api_key_env = "[REDACTED_SECRET]"
search_zone = "example_serp_zone"
fetch_zone = "example_unlocker_zone"

[web_providers.scrape_do]
enable_search = true
enable_fetch = true
api_url = "https://api.scrape.do"
api_key_env = "[REDACTED_SECRET]"

[web_providers.zenrows]
enable_search = false
enable_fetch = true
api_url = "https://api.zenrows.com"
api_key_env = "[REDACTED_SECRET]"

[web_providers.decodo]
enable_search = true
enable_fetch = true
api_url = "https://scraper-api.decodo.com"
api_key_env = "[REDACTED_SECRET]"

[web_providers.scrapingdog]
enable_search = true
enable_fetch = true
api_url = "https://api.scrapingdog.com"
api_key_env = "[REDACTED_SECRET]"

[web_providers.scrapegraphai]
enable_search = true
enable_fetch = true
api_url = "https://v2-api.scrapegraphai.com"
api_key_env = "[REDACTED_SECRET]"

[web_providers.scraperapi]
enable_search = true
enable_fetch = true
api_url = "https://api.scraperapi.com"
api_key_env = "[REDACTED_SECRET]"

[web_providers.scrapingant]
enable_search = false
enable_fetch = true
api_url = "https://api.scrapingant.com"
api_key_env = "[REDACTED_SECRET]"

[web_providers.serpapi]
enable_search = true
enable_fetch = false
api_url = "https://serpapi.com"
api_key_env = "[REDACTED_SECRET]"
```

These examples intentionally expose no optional vendor tuning fields.

#### External API Assumptions

- Bright Data: SERP API and Web Unlocker share `/request`, use Bearer auth and configured zones; SERP supplies structured search and Web Unlocker supports page/Markdown output.
- Scrape.do: `/plugin/google/search` supplies structured JSON; normal scrape supports `output=markdown`.
- ZenRows: a current dedicated Google Search Results API exists on `serp.api.zenrows.com`, but this batch selects only the page-fetch Markdown path and intentionally defers a second configurable search endpoint.
- Decodo: current Web Scraping API supports `google_search` with `parse=true` and Markdown response mode using Basic token auth.
- ScrapingDog: `/google` supplies structured organic results and `/scrape` supplies target content.
- ScrapeGraphAI: v2 exposes JSON `POST /api/search` and `POST /api/scrape` with `SGAI-APIKEY`; v1 is not selected.
- ScraperAPI: synchronous `/structured/google/search` supplies SERP JSON and synchronous scrape supplies target content.
- ScrapingAnt: `/v2/markdown` returns JSON containing final URL and Markdown.
- SerpApi: Google Search API returns structured `organic_results` suitable for URL/title/snippet mapping.

If one of these external contracts changes before implementation, update only that adapter mapping unless repeated evidence justifies a core abstraction.
