# Additional Web Search and Fetch Providers Implementation Plan

**Goal:** Add nine built-in web-provider integrations—Bright Data, Scrape.do, ZenRows, Decodo, ScrapingDog, ScrapeGraphAI, ScraperAPI, ScrapingAnt, and SerpApi—behind the existing gateway search/fetch contracts, with one shared text-response transport extension and no provider-specific branches in core orchestration, scheduling, storage, protocol, or quota code.

**Architecture:** `docs/designs/architectures/20260822-multi-web-providers.md`

**Error handling:** `docs/designs/error-handlings/20260822-multi-web-providers.md`

**Testing:** `docs/designs/testings/20260822-multi-web-providers.md`

---

## Baseline and implementation boundaries

Current worktree baseline before implementation:

```text
uv run pytest -q
301 passed, 4 skipped
```

This plan follows the design scope exactly. `Apify` is explicitly deferred and must not be partially implemented, registered, documented as supported, or added to configuration examples in this batch.

### Intended production-code footprint

Create:

```text
src/agent_search_gateway/providers/web/brightdata.py
src/agent_search_gateway/providers/web/scrape_do.py
src/agent_search_gateway/providers/web/zenrows.py
src/agent_search_gateway/providers/web/decodo.py
src/agent_search_gateway/providers/web/scrapingdog.py
src/agent_search_gateway/providers/web/scrapegraphai.py
src/agent_search_gateway/providers/web/scraperapi.py
src/agent_search_gateway/providers/web/scrapingant.py
src/agent_search_gateway/providers/web/serpapi.py
```

Modify:

```text
src/agent_search_gateway/providers/http.py
src/agent_search_gateway/providers/web/common.py
src/agent_search_gateway/providers/defaults.py
```

Tests/support expected to change:

```text
tests/support/http.py
tests/providers/test_http_executor.py
tests/providers/test_registry.py
tests/providers/web/test_brightdata.py
tests/providers/web/test_scrape_do.py
tests/providers/web/test_zenrows.py
tests/providers/web/test_decodo.py
tests/providers/web/test_scrapingdog.py
tests/providers/web/test_scrapegraphai.py
tests/providers/web/test_scraperapi.py
tests/providers/web/test_scrapingant.py
tests/providers/web/test_serpapi.py
tests/providers/web/test_search_result_tolerance.py
tests/unit/test_config_web_providers.py
tests/runtime/test_runtime_assembly.py
tests/docs/test_documented_config.py
```

Documentation/config example:

```text
config.example.toml
README.md
```

Inspect-only sources of truth unless a failing test proves the approved design cannot be implemented otherwise:

```text
src/agent_search_gateway/config.py:16-150
src/agent_search_gateway/runtime.py:130-182
src/agent_search_gateway/providers/contracts.py:13-45
src/agent_search_gateway/providers/registry.py
src/agent_search_gateway/orchestrators/
src/agent_search_gateway/scheduler/
src/agent_search_gateway/url_store.py
src/agent_search_gateway/result_writer.py
src/agent_search_gateway/protocol.py
src/agent_search_gateway/errors.py
```

Do not add provider-name branches to `config.py`, `Runtime._build_web_providers()`, search orchestration, fetch scheduling, URL storage, CLI, daemon/socket protocol, result writing, or quota management. Do not add new `ErrorCode` values, provider-specific retry loops, provider-specific scheduler order, generic vendor-option passthrough, async job state, browser sessions, local SERP parsing, AI extraction schemas, or vendor metadata fields on gateway domain objects.

### Locked gateway contracts

These remain unchanged:

```python
class KeywordSearchProvider(Protocol):
    name: str
    async def search(self, query: str) -> list[KeywordSearchHit]: ...

class URLFetchProvider(Protocol):
    name: str
    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...

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

### Locked transport contracts

Keep existing `JsonRequester` unchanged and add narrow text capability:

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

class HttpRequester(JsonRequester, TextRequester, Protocol):
    pass
```

`HttpRequester` is only a structural convenience for adapters whose Search uses JSON and Fetch uses text. Existing JSON-only adapters and their `RecordingJsonExecutor` tests must not require annotation or call-site changes.

`HttpJsonExecutor.request_json()` and new `request_text()` must share the same timeout, retry, HTTP-status classification, sanitized endpoint logging, cancellation, and HTTP-client lifecycle. JSON decode is the only response-mode-specific behavior owned by `request_json()`.

### Locked provider capability/configuration matrix

```text
brightdata    search=true  fetch=true  options={api_url, search_zone, fetch_zone}
scrape_do     search=true  fetch=true  options={api_url}
zenrows       search=false fetch=true  options={api_url}
decodo        search=true  fetch=true  options={api_url}
scrapingdog   search=true  fetch=true  options={api_url}
scrapegraphai search=true  fetch=true  options={api_url}
scraperapi    search=true  fetch=true  options={api_url}
scrapingant   search=false fetch=true  options={api_url}
serpapi       search=true  fetch=false options={api_url}
```

Shared provider configuration remains exactly `enable_search`, `enable_fetch`, `api_key_env`, and `max_concurrency`.

---

### Task 1: Add the shared text-response transport happy path

**Files:**
- Modify: `src/agent_search_gateway/providers/http.py:22-162`
- Modify: `src/agent_search_gateway/providers/web/common.py:10-19`
- Modify: `tests/providers/test_http_executor.py:18-113`
- Modify: `tests/support/http.py:7-34`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (Shared Text Transport)
- Reference: `docs/designs/error-handlings/20260822-multi-web-providers.md` (Shared HTTP Transport Failures)

- [ ] **Step 1: Write the failing successful-text transport test**

Add `test_http_executor_request_text_returns_exact_body_without_json_decode_or_payload_logging`.

Scenario:

```text
Use httpx.MockTransport to return HTTP 200 with sentinel Markdown/HTML text.
Construct HttpJsonExecutor with a structured test logger.
Call request_text("POST", sensitive URL, stage="fetch", headers=..., json_body=...).
Assert the exact decoded response.text is returned.
Assert http_attempt_started and http_attempt_completed are emitted once.
Assert endpoint logging strips userinfo/query/fragment exactly like request_json.
Assert response body, credential placeholder, query sentinel, target/search sentinel, request body, and request repr are absent from logs.
```

Keep the existing invalid-JSON test unchanged; it is the regression proof that JSON mode still decodes and classifies protocol failure separately.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/test_http_executor.py::test_http_executor_request_text_returns_exact_body_without_json_decode_or_payload_logging -v
```

Expected:

```text
FAIL because HttpJsonExecutor.request_text does not exist
```

- [ ] **Step 3: Refactor one shared HTTP-response execution path, then add `request_text()`**

Domain-specific pseudocode:

```text
private _request_response(method, url, stage, headers, json_body) -> httpx.Response:
  compute sanitized log endpoint once
  run existing retry_async loop
  preserve before_attempt / on_retry events
  perform client.request with existing timeout
  preserve retryable statuses 408 / 429 / >=500
  preserve transport retries
  preserve non-retryable >=400 ExecutionFailure
  return only successful httpx.Response

request_json(...):
  response = await _request_response(...)
  try response.json()
  except ValueError:
    preserve category=decode http_failed
    raise existing ProtocolFailure(PROTOCOL_ERROR, ...)

request_text(...):
  response = await _request_response(...)
  return response.text
```

Add `TextRequester` and, if needed for typing mixed adapters, `HttpRequester(JsonRequester, TextRequester, Protocol)` to `providers/web/common.py`. Do not add `request_text()` to `JsonRequester`.

Extend `tests/support/http.py` with the smallest sibling doubles needed later:

```text
RecordingTextExecutor:
  queued str responses
  request_text(...)

RecordingHttpExecutor:
  separate JSON and text response queues
  request_json(...)
  request_text(...)
```

Reuse `RecordedRequest`; do not test the fake itself and do not modify existing JSON-only adapter tests merely because the production executor gained a method.

- [ ] **Step 4: Verify GREEN and preserve JSON behavior**

```bash
uv run pytest tests/providers/test_http_executor.py -v
```

Expected: the new text happy-path test and all existing JSON executor tests pass.

- [ ] **Step 5: Refactor with tests green**

Keep exactly one implementation of retry/status/logging policy. If `request_json()` and `request_text()` each contain their own retry loop after this task, the refactor is incomplete.

```bash
uv run ruff check src/agent_search_gateway/providers/http.py src/agent_search_gateway/providers/web/common.py tests/providers/test_http_executor.py tests/support/http.py
uv run mypy src/agent_search_gateway/providers/http.py src/agent_search_gateway/providers/web/common.py tests/support/http.py
```

Expected: both pass.

---

### Task 2: Lock text transport retry, terminal failure, redaction, and empty-body semantics

**Files:**
- Modify: `tests/providers/test_http_executor.py:18-209`
- Modify: `src/agent_search_gateway/providers/http.py` only if the new tests expose a shared-policy regression
- Reference: `src/agent_search_gateway/observability.py` (`http_endpoint_for_log`)
- Reference: `docs/designs/testings/20260822-multi-web-providers.md` (Shared `request_text()` Transport Tests)

- [ ] **Step 1: Write focused text-mode failure/regression tests**

Add focused tests that prove only the response decoding mode changed:

```text
test_http_executor_request_text_retries_retryable_statuses
  500 -> 429 -> 200 text
  exact attempt count and http_retrying category/status/delay
  final text exact
  all response bodies absent from logs

test_http_executor_request_text_maps_non_retryable_status_without_retry
  401/400 sensitive body
  existing ExecutionFailure + ALL_PROVIDERS_FAILED
  body absent from exception/logs

test_http_executor_request_text_retries_transport_failure
  one TransportError then success, or exhaustion
  same retry/final failure semantics as JSON
  exception detail absent from logs

test_http_executor_request_text_redacts_userinfo_query_and_fragment
  credential/query/target sentinels never logged

test_http_executor_request_text_allows_empty_success_body
  HTTP 200 "" returns "" and emits no decode/protocol failure
```

Do not add a transport test that expects whitespace/empty text to fail; adapter fetch contracts own that rule.

- [ ] **Step 2: Run the focused tests and verify RED where policy sharing is incomplete**

```bash
uv run pytest tests/providers/test_http_executor.py -k "request_text" -v
```

Expected: any duplicated or incomplete text-mode transport behavior fails before adapter work begins.

- [ ] **Step 3: Make only transport-policy corrections**

If a test fails, fix the shared response execution helper rather than adding text-specific retry/status branches.

```text
retryable status => shared _RetryableStatus path
transport error => shared retry_async path
terminal >=400 => shared _execution_failure path
HTTP 200 empty string => normal response.text
logging => sanitized endpoint and fixed operational metadata only
```

Never log `response.text`, `response.content`, raw exception text, request body, credential header, or full request URL.

- [ ] **Step 4: Verify GREEN across all executor behavior**

```bash
uv run pytest tests/providers/test_http_executor.py -v
```

Expected: JSON and text modes share lifecycle behavior; invalid JSON still fails once without retry.

- [ ] **Step 5: Run nearby retry/observability regressions**

```bash
uv run pytest tests/unit/test_retry.py tests/unit/test_observability_logging.py tests/providers/test_http_executor.py -v
```

Expected: all pass.

---

### Task 3: Implement Bright Data structured Search

**Files:**
- Create: `src/agent_search_gateway/providers/web/brightdata.py`
- Create: `tests/providers/web/test_brightdata.py`
- Reference: `src/agent_search_gateway/providers/web/tinyfish.py:40-68`
- Reference: `src/agent_search_gateway/providers/web/common.py:22-69`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (Bright Data)

- [ ] **Step 1: Write the failing basic Bright Data Search contract test**

Add `test_brightdata_search_uses_serp_zone_and_maps_organic_results` using `RecordingHttpExecutor` JSON mode.

Scenario:

```text
Construct BrightDataAdapter with name, api_url, SecretValue, search_zone, fetch_zone.
Call search("hello world & docs").
Assert POST goes to configured /request endpoint with stage="search".
Assert Authorization uses Bearer [REDACTED_SECRET] from the SecretValue boundary.
Assert request uses search_zone and never fetch_zone.
Assert target is a correctly encoded Google Search URL derived from the one gateway query.
Assert only the selected structured-SERP response mode and required fields are sent.
Assert country/language/device/page/result-count/session controls are absent.
Return representative successful structured SERP JSON.
Map valid organic entries to KeywordSearchHit(url, title, snippet) only.
Assert raw_content == content == "" for ordinary SERP entries.
Assert extra provider metadata is ignored.
```

Use the field names from the selected Bright Data SERP `/request` contract captured by the architecture. If the vendor contract has changed since the design was written, update this adapter/test pair only after confirming the current API; do not widen core abstractions.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
uv run pytest tests/providers/web/test_brightdata.py::test_brightdata_search_uses_serp_zone_and_maps_organic_results -v
```

Expected: collection/import failure because `brightdata.py` does not exist.

- [ ] **Step 3: Implement only the Search path**

Pseudocode:

```text
validate/store constructor inputs needed by Search
build Bearer authorization header by revealing SecretValue only at request construction
google_url = "https://www.google.com/search?" + urlencode({"q": query})
request POST endpoint(api_url, "/request") through HttpRequester.request_json
body = selected minimal Bright Data SERP request using search_zone + google_url + structured JSON mode
validate required top-level successful SERP envelope
results = require documented organic collection
for item in results:
  inside per-result ExecutionFailure boundary:
    require non-empty result URL
    optional title/snippet strings
    append KeywordSearchHit
return hits
```

Do not fetch page bodies during Search and do not map SERP excerpts/snippets into `content`.

- [ ] **Step 4: Verify GREEN and nearby search adapter regressions**

```bash
uv run pytest tests/providers/web/test_brightdata.py -k "search" -v
uv run pytest tests/providers/web/test_tinyfish.py tests/providers/web/test_search_result_tolerance.py -v
```

Expected: Bright Data basic Search passes and existing Search adapters remain green.

- [ ] **Step 5: Refactor/check**

Keep Bright Data request/response fields local to `brightdata.py`. Do not create a generic SERP adapter base class.

```bash
uv run ruff check src/agent_search_gateway/providers/web/brightdata.py tests/providers/web/test_brightdata.py
uv run mypy src/agent_search_gateway/providers/web/brightdata.py tests/providers/web/test_brightdata.py
```

Expected: both pass.

---

### Task 4: Add Bright Data Fetch, independent zones, and required-zone validation

**Files:**
- Modify: `src/agent_search_gateway/providers/web/brightdata.py`
- Modify: `tests/providers/web/test_brightdata.py`
- Reference: `src/agent_search_gateway/providers/web/firecrawl.py:102-118`
- Reference: `src/agent_search_gateway/runtime.py:170-176`

- [ ] **Step 1: Write failing Fetch/zone-isolation tests**

Add:

```text
test_brightdata_fetch_uses_fetch_zone_and_returns_markdown_text
  call fetch(normalize_url(...))
  POST configured /request with stage="fetch"
  Bearer authentication through SecretValue
  request uses fetch_zone, not search_zone
  target URL equals normalized URL
  selected Web Unlocker Markdown/text mode only
  exact returned text => URLFetchCandidate(raw_content=text, content=text)

test_brightdata_search_and_fetch_keep_zones_independent
  invoke both methods on the same adapter
  assert Search request carries only search_zone and Fetch carries only fetch_zone

test_brightdata_requires_non_empty_zones
  parameterize missing/empty/whitespace/non-string search_zone and fetch_zone
  constructor raises TypeError before HTTP
```

Also cover whitespace-only Fetch text as an `ExecutionFailure("page body is empty")` at the adapter boundary.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/providers/web/test_brightdata.py -k "fetch or zone" -v
```

Expected: failures because Fetch/zone validation is incomplete.

- [ ] **Step 3: Implement minimal Fetch and validation**

```text
constructor:
  require api_url, search_zone, fetch_zone are non-empty strings
  store stripped API base and zone names

fetch(target):
  POST /request through request_text
  construct Bearer header from SecretValue only for the request
  body uses fetch_zone + str(target) + selected Markdown/text output mode
  text = returned response text
  if not text.strip(): raise failure(name, "fetch", "page body is empty")
  return URLFetchCandidate(raw_content=text, content=text)
```

Both zones remain required even when only one stage is enabled; do not add stage-dependent config semantics.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_brightdata.py -v
```

Expected: all Bright Data tests pass.

- [ ] **Step 5: Refactor/check**

No retry, semantic page-quality check, scheduler fallback, or URL-store mutation belongs in this adapter.

```bash
uv run ruff check src/agent_search_gateway/providers/web/brightdata.py tests/providers/web/test_brightdata.py
uv run mypy src/agent_search_gateway/providers/web/brightdata.py tests/providers/web/test_brightdata.py
```

Expected: both pass.

---

### Task 5: Implement Scrape.do Search and Markdown Fetch

**Files:**
- Create: `src/agent_search_gateway/providers/web/scrape_do.py`
- Create: `tests/providers/web/test_scrape_do.py`
- Reference: `src/agent_search_gateway/providers/web/tinyfish.py:40-68`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (Scrape.do)

- [ ] **Step 1: Write failing minimal request/mapping tests**

Add one Search test and one Fetch test:

```text
test_scrape_do_search_builds_minimal_google_plugin_request
  configured base + /plugin/google/search
  token field receives [REDACTED_SECRET] through URL construction only
  q encoded in query string
  query containing spaces/reserved characters round-trips correctly
  no location/device/page/result-count controls
  organic_results => KeywordSearchHit(url, title, snippet)
  raw_content/content remain empty

test_scrape_do_fetch_requests_markdown_and_maps_text
  normal scrape endpoint
  token field + normalized target URL encoded in query string
  output=markdown
  no rendering/proxy/session tuning
  exact text => URLFetchCandidate(text, text)
```

Use `RecordingHttpExecutor` so Search must consume JSON and Fetch must consume text.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_scrape_do.py -v
```

Expected: import failure because adapter does not exist.

- [ ] **Step 3: Implement only selected synchronous APIs**

```text
constructor requires non-empty api_url
search_url = endpoint(api_url, "/plugin/google/search") + urlencode({token: credential, q: query})
request_json selected documented method, stage="search"
validate successful root + organic_results list
map per result with isolated ExecutionFailure tolerance

fetch_url = configured normal scrape endpoint + urlencode({token: credential, url: target, output: "markdown"})
request_text selected documented method, stage="fetch"
reject whitespace-only body
return URLFetchCandidate(text, text)
```

Reveal `SecretValue` only while constructing the provider-required query parameter. Keep credential/query values out of adapter exceptions; transport logging will sanitize query strings.

- [ ] **Step 4: Verify GREEN and redaction boundary**

```bash
uv run pytest tests/providers/web/test_scrape_do.py tests/providers/test_http_executor.py -v
```

Expected: all pass.

- [ ] **Step 5: Refactor/check**

Do not add a query-parameter API to `HttpJsonExecutor` solely for Scrape.do.

```bash
uv run ruff check src/agent_search_gateway/providers/web/scrape_do.py tests/providers/web/test_scrape_do.py
uv run mypy src/agent_search_gateway/providers/web/scrape_do.py tests/providers/web/test_scrape_do.py
```

Expected: both pass.

---

### Task 6: Implement ZenRows as fetch-only in this batch

**Files:**
- Create: `src/agent_search_gateway/providers/web/zenrows.py`
- Create: `tests/providers/web/test_zenrows.py`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (ZenRows)

- [ ] **Step 1: Write the failing Fetch contract test**

Add `test_zenrows_fetch_uses_current_fetch_api_and_requests_markdown`.

Scenario:

```text
Construct fetch-only ZenRowsAdapter with api_url + SecretValue + TextRequester.
Fetch a normalized target URL.
Assert credential and target mapping follow the selected page-fetch API.
Assert Markdown output is explicitly selected.
Assert the fetch request does not target the separately hosted `serp.api.zenrows.com` Google Search Results API deferred by the architecture.
Assert JS rendering, premium proxy, country, session, and other optional controls are absent.
Assert returned non-empty Markdown => URLFetchCandidate(raw_content=text, content=text).
```

Add one whitespace-only response case that raises existing `ExecutionFailure`.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_zenrows.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement only `fetch()`**

```text
constructor requires non-empty api_url
build the selected page-fetch request using only credential + target + Markdown selector
call request_text(stage="fetch")
reject empty/whitespace text with failure(name, "fetch", "page body is empty")
return URLFetchCandidate(text, text)
```

Do not implement `search()` and do not parse a Google SERP locally.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_zenrows.py -v
```

Expected: all ZenRows adapter tests pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/zenrows.py tests/providers/web/test_zenrows.py
uv run mypy src/agent_search_gateway/providers/web/zenrows.py tests/providers/web/test_zenrows.py
```

Expected: both pass.

---

### Task 7: Implement Decodo Google Search template and Markdown Fetch

**Files:**
- Create: `src/agent_search_gateway/providers/web/decodo.py`
- Create: `tests/providers/web/test_decodo.py`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (Decodo)

- [ ] **Step 1: Write failing Search/Fetch request tests**

Cover:

```text
test_decodo_search_uses_google_search_template_and_opaque_basic_token
  POST current /v2/scrape endpoint
  Authorization header uses Basic [REDACTED_SECRET]
  body selects target="google_search", query=<gateway query>, parse=true
  no legacy plan endpoint
  parsed organic results => hits

test_decodo_fetch_requests_markdown_with_same_opaque_basic_token
  POST current real-time endpoint
  normalized target URL + markdown=true only as required
  returned Markdown/text => URLFetchCandidate(text, text)
```

Never decode, split, validate, or log the Basic token material.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_decodo.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement selected current endpoint behavior**

```text
construct Authorization: Basic header from SecretValue at request time
search => request_json POST /v2/scrape with google_search template + parse=true
validate parsed search envelope and organic collection
map URL/title/snippet per entry

fetch => request_text selected current /v2/scrape Markdown mode
reject empty body
return URLFetchCandidate(text, text)
```

If the selected Decodo Fetch contract returns Markdown inside a JSON success envelope rather than raw text at implementation time, keep that adaptation inside `decodo.py`; do not add another core response abstraction unless the same requirement is proven across multiple providers.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_decodo.py -v
```

Expected: all Decodo adapter tests pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/decodo.py tests/providers/web/test_decodo.py
uv run mypy src/agent_search_gateway/providers/web/decodo.py tests/providers/web/test_decodo.py
```

Expected: both pass.

---

### Task 8: Implement ScrapingDog Google Search and Web Scraping APIs

**Files:**
- Create: `src/agent_search_gateway/providers/web/scrapingdog.py`
- Create: `tests/providers/web/test_scrapingdog.py`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (ScrapingDog)

- [ ] **Step 1: Write failing Search/Fetch tests**

```text
test_scrapingdog_search_uses_google_endpoint_and_minimal_query
  /google
  query string contains only required api_key credential field + query
  deep_scrape absent
  organic_results[].link/title/snippet => hits

test_scrapingdog_fetch_uses_scrape_endpoint_and_maps_page_text
  /scrape
  api_key credential field + normalized target URL
  dynamic/premium/country/wait controls absent
  non-empty text => URLFetchCandidate
```

Use reserved characters in the query/target to prove `urlencode` behavior.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_scrapingdog.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement minimal synchronous requests**

```text
search request_json against /google with encoded credential/query
fetch request_text against /scrape with encoded credential/url
per-entry search parsing uses existing common helpers
fetch rejects empty body
```

No `deep_scrape` or optional fetch tuning.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_scrapingdog.py -v
```

Expected: all ScrapingDog adapter tests pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/scrapingdog.py tests/providers/web/test_scrapingdog.py
uv run mypy src/agent_search_gateway/providers/web/scrapingdog.py tests/providers/web/test_scrapingdog.py
```

Expected: both pass.

---

### Task 9: Implement ScrapeGraphAI v2 Search and Scrape

**Files:**
- Create: `src/agent_search_gateway/providers/web/scrapegraphai.py`
- Create: `tests/providers/web/test_scrapegraphai.py`
- Reference: `src/agent_search_gateway/providers/web/firecrawl.py:56-118`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (ScrapeGraphAI)

- [ ] **Step 1: Write failing v2/authentication contract tests**

```text
test_scrapegraphai_search_uses_v2_search_and_sgai_apikey
  POST /api/search under configured v2 host
  SGAI-APIKEY header receives [REDACTED_SECRET] through SecretValue
  minimal current search input only
  no v1 path
  stable result URL/title/snippet => hits
  raw_content/content remain empty unless the documented stable Search result contains a genuine full-page body explicitly selected by the architecture

test_scrapegraphai_fetch_uses_v2_scrape_markdown
  POST /api/scrape
  same SGAI-APIKEY authentication form
  normalized target + Markdown selection
  documented successful Markdown/body field => candidate
  no Extract/Crawl/Monitor fields
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_scrapegraphai.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement only v2 synchronous JSON contracts**

```text
constructor requires non-empty api_url
construct SGAI-APIKEY header from SecretValue at request time
search => request_json POST /api/search, validate success/result collection, map hits
fetch => request_json POST /api/scrape, validate success envelope, require non-empty selected Markdown/body, return candidate
```

Do not use v1 or generic AI Extract.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_scrapegraphai.py -v
```

Expected: all ScrapeGraphAI adapter tests pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/scrapegraphai.py tests/providers/web/test_scrapegraphai.py
uv run mypy src/agent_search_gateway/providers/web/scrapegraphai.py tests/providers/web/test_scrapegraphai.py
```

Expected: both pass.

---

### Task 10: Implement ScraperAPI synchronous structured Search and synchronous Fetch

**Files:**
- Create: `src/agent_search_gateway/providers/web/scraperapi.py`
- Create: `tests/providers/web/test_scraperapi.py`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (ScraperAPI)

- [ ] **Step 1: Write failing synchronous endpoint tests**

```text
test_scraperapi_search_uses_synchronous_structured_google_endpoint
  /structured/google/search
  api_key credential field + query only
  query encoding correct
  organic results map to URL/title/snippet
  no async/batch task fields or optional localization/rendering controls

test_scraperapi_fetch_uses_synchronous_scrape_endpoint
  normal synchronous scrape endpoint
  api_key credential field + normalized target URL
  no render/premium/country/session flags
  returned text => candidate
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_scraperapi.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement only synchronous request-response APIs**

```text
search => request_json against /structured/google/search query URL
fetch => request_text against synchronous scrape query URL
search parses organic results with per-entry tolerance
fetch rejects empty text
```

Reveal the credential only while building the provider-required query parameter. Do not add polling, task IDs, batch APIs, or webhooks.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_scraperapi.py -v
```

Expected: all ScraperAPI adapter tests pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/scraperapi.py tests/providers/web/test_scraperapi.py
uv run mypy src/agent_search_gateway/providers/web/scraperapi.py tests/providers/web/test_scraperapi.py
```

Expected: both pass.

---

### Task 11: Implement ScrapingAnt as fetch-only `/v2/markdown`

**Files:**
- Create: `src/agent_search_gateway/providers/web/scrapingant.py`
- Create: `tests/providers/web/test_scrapingant.py`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (ScrapingAnt)

- [ ] **Step 1: Write the failing JSON Markdown Fetch contract test**

Add `test_scrapingant_fetch_uses_v2_markdown_and_maps_markdown_field`.

Scenario:

```text
GET/selected documented method against /v2/markdown.
Authentication uses x-api-key with [REDACTED_SECRET] from SecretValue.
Normalized target URL is sent according to current endpoint contract.
No AI Extract prompt/schema or optional browser tuning.
Successful JSON response markdown field maps to both raw_content and content.
```

Add malformed body cases for missing/non-string/empty `markdown` and a provider-error envelope; they must raise existing `ExecutionFailure` without raw vendor diagnostics in the message.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_scrapingant.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement only `fetch()` through JsonRequester**

```text
construct x-api-key header from SecretValue at request time
request_json current /v2/markdown with target URL
validate successful response object
markdown = non_empty_string(...)
return URLFetchCandidate(markdown, markdown)
```

Do not implement `search()` and do not add AI extraction to synthesize Search results.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_scrapingant.py -v
```

Expected: all ScrapingAnt tests pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/scrapingant.py tests/providers/web/test_scrapingant.py
uv run mypy src/agent_search_gateway/providers/web/scrapingant.py tests/providers/web/test_scrapingant.py
```

Expected: both pass.

---

### Task 12: Implement SerpApi as search-only Google Search

**Files:**
- Create: `src/agent_search_gateway/providers/web/serpapi.py`
- Create: `tests/providers/web/test_serpapi.py`
- Reference: `src/agent_search_gateway/providers/web/brave.py:32-63`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (SerpApi)

- [ ] **Step 1: Write the failing SerpApi Search contract test**

Add `test_serpapi_search_uses_google_engine_and_maps_organic_results`.

Scenario:

```text
Construct SerpApiAdapter with configured Search base, SecretValue, JsonRequester.
Call search with reserved characters.
Request uses configured Search endpoint and fixed engine=google.
Encoded query q and api_key credential field are present.
location/language/device/result-count/pagination controls are absent.
organic_results[].link/title/snippet map to hits.
raw_content/content remain empty.
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/providers/web/test_serpapi.py -v
```

Expected: adapter missing.

- [ ] **Step 3: Implement only `search()`**

```text
request_url = configured Search endpoint + urlencode({engine: "google", q: query, api_key: credential})
payload = request_json(..., stage="search")
validate root + organic_results
parse valid entries inside per-entry ExecutionFailure boundary
return hits
```

Reveal `SecretValue` only for query construction. Do not implement arbitrary page fetch.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web/test_serpapi.py -v
```

Expected: SerpApi basic Search passes.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/web/serpapi.py tests/providers/web/test_serpapi.py
uv run mypy src/agent_search_gateway/providers/web/serpapi.py tests/providers/web/test_serpapi.py
```

Expected: both pass.

---

### Task 13: Lock malformed-result tolerance and top-level failure semantics for every new Search adapter

**Files:**
- Modify: `tests/providers/web/test_brightdata.py`
- Modify: `tests/providers/web/test_scrape_do.py`
- Modify: `tests/providers/web/test_decodo.py`
- Modify: `tests/providers/web/test_scrapingdog.py`
- Modify: `tests/providers/web/test_scrapegraphai.py`
- Modify: `tests/providers/web/test_scraperapi.py`
- Modify: `tests/providers/web/test_serpapi.py`
- Modify: `tests/providers/web/test_search_result_tolerance.py:1-170` only if parameterization reduces duplication cleanly
- Modify: corresponding adapter modules only when tests expose a gap
- Reference: `docs/designs/error-handlings/20260822-multi-web-providers.md` (Search Response Failures)

- [ ] **Step 1: Add the common Search failure matrix**

For all seven Search-capable new adapters, prove:

```text
one malformed result surrounded by valid neighbors:
  non-object result OR missing/empty/non-string URL
  malformed optional title/snippet field
  only malformed entry is skipped
  valid order is preserved

malformed required top-level response:
  root not object
  required organic/result collection missing
  required collection wrong type
  provider-embedded failure in HTTP 200 when the selected API exposes one
  => existing ExecutionFailure/ProtocolFailure, never []

valid successful empty result collection:
  => []
```

Use provider-specific tests where envelope shape matters. Extend `test_search_result_tolerance.py` only for truly common per-entry behavior; do not force heterogeneous vendor envelopes into a generic fixture abstraction.

Assert raw vendor error diagnostics are not copied into gateway exceptions.

- [ ] **Step 2: Run all new Search adapter tests and verify RED where tolerance is missing**

```bash
uv run pytest \
  tests/providers/web/test_brightdata.py \
  tests/providers/web/test_scrape_do.py \
  tests/providers/web/test_decodo.py \
  tests/providers/web/test_scrapingdog.py \
  tests/providers/web/test_scrapegraphai.py \
  tests/providers/web/test_scraperapi.py \
  tests/providers/web/test_serpapi.py \
  tests/providers/web/test_search_result_tolerance.py -v
```

Expected: any adapter that catches too broadly, returns `[]` for malformed top-level data, or fails the whole provider on one bad result is exposed.

- [ ] **Step 3: Make exception boundaries exact**

```text
top-level request_json + root/envelope/list validation remain outside per-result try/except
for each result:
  catch only existing provider parse ExecutionFailure
  skip malformed result
transport failure / invalid JSON / top-level failure / cancellation propagate normally
valid results=[] or all isolated entries skipped => []
```

Do not catch `BaseException`, `Exception`, `ProtocolFailure`, or `asyncio.CancelledError` just to keep searching.

- [ ] **Step 4: Verify GREEN with existing orchestrator semantics**

```bash
uv run pytest tests/providers/web tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py -v
```

Expected: adapter tolerance and provider-isolation semantics pass without provider-name branches in orchestration.

- [ ] **Step 5: Refactor/check**

Prefer existing `require_object`, `require_list`, `non_empty_string`, `optional_string`, and `failure` helpers. Keep vendor-specific nested response helpers local to their adapter.

```bash
uv run ruff check src/agent_search_gateway/providers/web tests/providers/web
uv run mypy src/agent_search_gateway/providers/web tests/providers/web
```

Expected: both pass.

---

### Task 14: Lock Fetch missing-body, provider-failure, and semantic-boundary behavior

**Files:**
- Modify: `tests/providers/web/test_brightdata.py`
- Modify: `tests/providers/web/test_scrape_do.py`
- Modify: `tests/providers/web/test_zenrows.py`
- Modify: `tests/providers/web/test_decodo.py`
- Modify: `tests/providers/web/test_scrapingdog.py`
- Modify: `tests/providers/web/test_scrapegraphai.py`
- Modify: `tests/providers/web/test_scraperapi.py`
- Modify: `tests/providers/web/test_scrapingant.py`
- Modify: corresponding adapter files only if tests expose a gap
- Reference: `tests/scheduler/test_fetch_outcomes.py`
- Reference: `docs/designs/error-handlings/20260822-multi-web-providers.md` (Fetch Response Failures)

- [ ] **Step 1: Add the common Fetch failure matrix**

For all eight Fetch-capable providers, cover the applicable cases:

```text
raw text API returns "" or whitespace => ExecutionFailure, no empty candidate
JSON API body field missing/null/non-string/empty => ExecutionFailure
provider HTTP-200 error envelope => ExecutionFailure with concise fixed reason
raw vendor diagnostic string/object absent from exception
single synchronous response does not require final URL equality unless response shape is multi-result/batch
no adapter calls cheap_check, judge, scheduler fallback, URLStore, or result writer
```

For provider APIs with one-request/one-body synchronous semantics, do not invent redirect/final-URL validation. If a selected provider response is multi-result and must match a target, reuse `normalized_match()`.

- [ ] **Step 2: Run focused Fetch tests and verify RED**

```bash
uv run pytest tests/providers/web -k "fetch or scrape or markdown" -v
```

Expected: any adapter that returns an empty candidate or leaks vendor diagnostics fails.

- [ ] **Step 3: Implement only adapter-domain body validation**

```text
text mode:
  if not text.strip(): raise failure(name, "fetch", "page body is empty")

JSON mode:
  validate provider success envelope
  require selected body/markdown field as non-empty string

return URLFetchCandidate only after provider-contract body exists
```

Semantic usability remains scheduler-owned; do not retry with alternate vendor flags after a valid body is rejected downstream.

- [ ] **Step 4: Verify GREEN with scheduler fallback regressions**

```bash
uv run pytest tests/providers/web tests/scheduler/test_fetch_capacity.py tests/scheduler/test_fetch_outcomes.py tests/orchestrators/test_url_fetch_flow.py -v
```

Expected: all pass and scheduler behavior remains generic.

- [ ] **Step 5: Refactor/check**

Inspect adapter imports. No new provider adapter should import scheduler, orchestrator, URL store, result writer, CLI, daemon, or protocol modules.

```bash
uv run ruff check src/agent_search_gateway/providers/web tests/providers/web
uv run mypy src/agent_search_gateway/providers/web tests/providers/web
```

Expected: both pass.

---

### Task 15: Lock constructor validation for provider-specific required strings

**Files:**
- Modify: all nine new adapter test files
- Modify: all nine new adapter modules only as required
- Reference: `src/agent_search_gateway/runtime.py:170-176`
- Reference: `docs/designs/error-handlings/20260822-multi-web-providers.md` (Missing/Invalid Provider-Specific Required String)

- [ ] **Step 1: Write constructor validation tests**

Each adapter's basic test already proves approved minimal construction succeeds. Add parameterized invalid required-string coverage without over-testing unrelated syntax:

```text
api_url invalid values for each new adapter:
  ""
  "   "
  non-string representative
  => TypeError before any HTTP request

Bright Data additionally:
  search_zone invalid => TypeError
  fetch_zone invalid => TypeError
```

Do not add URL-format/network reachability validation; only require the configured value be a non-empty string as specified.

- [ ] **Step 2: Run constructor tests and verify RED where adapters currently accept invalid values**

```bash
uv run pytest tests/providers/web -k "constructor or required or api_url or zone" -v
```

Expected: failures identify missing provider-local validation.

- [ ] **Step 3: Add minimal constructor validation**

For each adapter:

```text
if api_url is not a string or not api_url.strip(): raise TypeError
store normalized base using rstrip("/") where endpoint composition benefits
```

Bright Data similarly validates and stores both zones.

Do not add a new configuration exception hierarchy. Runtime will translate constructor `TypeError` generically.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/web -v
```

Expected: all adapter tests pass.

- [ ] **Step 5: Refactor/check consistency**

Keep labels/messages local and concise. DRY only if a tiny pure helper clearly improves all new adapters; do not retrofit every existing adapter merely for style consistency.

```bash
uv run ruff check src/agent_search_gateway/providers/web tests/providers/web
uv run mypy src/agent_search_gateway/providers/web tests/providers/web
```

Expected: both pass.

---

### Task 16: Register all nine built-ins with exact capabilities and config whitelists

**Files:**
- Modify: `src/agent_search_gateway/providers/defaults.py:3-71`
- Modify: `tests/providers/test_registry.py:14-58`
- Modify: `tests/runtime/test_runtime_assembly.py:112-133` because it currently asserts complete built-in registration order
- Inspect only: `src/agent_search_gateway/providers/registry.py`

- [ ] **Step 1: Write failing exact built-in registry assertions**

Extend registry coverage to assert the existing providers remain in their current order and append the nine new registrations without reordering old entries.

Expected new records:

```text
brightdata    ProviderCapabilities(True, True)   {api_url, search_zone, fetch_zone}
scrape_do     ProviderCapabilities(True, True)   {api_url}
zenrows       ProviderCapabilities(False, True)  {api_url}
decodo        ProviderCapabilities(True, True)   {api_url}
scrapingdog   ProviderCapabilities(True, True)   {api_url}
scrapegraphai ProviderCapabilities(True, True)   {api_url}
scraperapi    ProviderCapabilities(True, True)   {api_url}
scrapingant   ProviderCapabilities(False, True)  {api_url}
serpapi       ProviderCapabilities(True, False)  {api_url}
```

Assert factories are the corresponding adapter classes. Do not add registry metadata for auth style, endpoint type, response mode, or required-option semantics.

- [ ] **Step 2: Run registry/runtime registration tests and verify RED**

```bash
uv run pytest tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py -k "registry or assembly" -v
```

Expected: complete built-in expectations fail because registrations are absent.

- [ ] **Step 3: Add imports and append registrations in `build_default_registry()`**

```text
import each new adapter
append one WebProviderRegistration per matrix row
keep exact registry names, especially scrape_do
set exact capability booleans
set only approved allowed_config_keys
```

Do not modify `ProviderRegistry`.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py -v
```

Expected: registry order, capability, and option-whitelist assertions pass.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check src/agent_search_gateway/providers/defaults.py tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py
uv run mypy src/agent_search_gateway/providers/defaults.py tests/providers/test_registry.py
```

Expected: both pass.

---

### Task 17: Prove generic config resolution accepts the new providers and rejects unsupported capability/optional fields

**Files:**
- Modify: `tests/unit/test_config_web_providers.py:93-164`
- Inspect only: `src/agent_search_gateway/config.py:16-150`
- Reference: `docs/designs/error-handlings/20260822-multi-web-providers.md` (Configuration Failures)

- [ ] **Step 1: Add failing real-default-registry config tests**

Add focused tests rather than one case per identical `api_url` provider.

Capability rejection parameterization:

```text
zenrows + enable_search=true
scrapingant + enable_search=true
serpapi + enable_fetch=true
=> ConfigFailure with CONFIG_ERROR before runtime construction
```

Allowed option plumbing:

```text
Bright Data enabled with api_url/search_zone/fetch_zone + api_key_env
resolve with stub credential value
ResolvedWebProviderConfig.options preserves exactly those three provider-specific fields
shared fields absent from options
credential stays wrapped in SecretValue

Scrape.do representative {api_url} case
options == {api_url: ...}
```

Unknown option:

```text
representative new provider with country=true
=> existing unknown config key(s) ConfigFailure
```

Disabled semantics:

```text
representative new provider disabled for both stages, no api_key_env
=> resolves with secret=None and no constructor validation
whitelisted options are preserved
```

- [ ] **Step 2: Run config tests and verify RED**

```bash
uv run pytest tests/unit/test_config_web_providers.py -v
```

Expected: tests fail until registrations exist; after Task 16, they should exercise only existing generic resolver behavior.

- [ ] **Step 3: Make no production config change unless a test proves a defect**

The expected implementation is zero edits to `config.py`:

```text
_validate_options uses registration.allowed_config_keys
capability checks use registration.capabilities
api_key_env resolves one SecretValue
max_concurrency stays common
provider-specific options flow through MappingProxyType
```

If tempted to add `if name == "brightdata"` or any other vendor name, stop and re-check the architecture.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest tests/unit/test_config_web_providers.py -v
```

Expected: all pass with no provider-specific config branch.

- [ ] **Step 5: Run config/documentation regressions**

```bash
uv run pytest tests/unit/test_config_web_providers.py tests/doctor/test_config.py -v
uv run ruff check tests/unit/test_config_web_providers.py
uv run mypy tests/unit/test_config_web_providers.py
```

Expected: all pass.

---

### Task 18: Prove runtime assembly remains generic for mixed JSON/text adapters and constructor failures

**Files:**
- Modify: `tests/runtime/test_runtime_assembly.py:29-231`
- Inspect only: `src/agent_search_gateway/runtime.py:130-182`
- Reference: `docs/designs/testings/20260822-multi-web-providers.md` (Runtime Assembly Tests)

- [ ] **Step 1: Write focused runtime assertions for one new dual-capability adapter**

Use Bright Data as the representative because it has provider-specific required options and mixed JSON/text transport.

Add a valid enabled Bright Data entry to the runtime test config with:

```text
enable_search=true
enable_fetch=true
api_key_env=<test env name>
api_url=<test URL>
search_zone=<test zone>
fetch_zone=<test zone>
max_concurrency=<distinct limit>
```

Assert:

```text
exactly one BrightDataAdapter object is constructed
same object identity appears in web_search_providers and web_fetch_providers
one web quota named brightdata is shared across both stages
quota limit equals configured value
one HttpJsonExecutor/client backs the adapter and is closed exactly once
injected executor provides request_json and request_text at runtime
provider options arrived through existing kwargs.update(...) plumbing
credential value and env-var name are absent from repr/log output
```

Add `test_runtime_maps_invalid_brightdata_zone_to_config_failure`: syntactically whitelisted config resolves, constructor receives an invalid zone, `Runtime.build()` raises existing `ConfigFailure` matching `Invalid configuration for web provider brightdata`.

- [ ] **Step 2: Run focused runtime tests and verify RED**

```bash
uv run pytest tests/runtime/test_runtime_assembly.py -k "brightdata or assembly" -v
```

Expected: failures until registration/adapter construction is wired.

- [ ] **Step 3: Keep runtime production code unchanged**

Existing path should remain sufficient:

```text
create HttpJsonExecutor once per enabled provider
kwargs = {name, http_executor, secret}
kwargs.update(provider_config.options)
adapter = registration.factory(**kwargs)
constructor TypeError => ConfigFailure(CONFIG_ERROR, "Invalid configuration for web provider <name>")
append same adapter object to enabled stage tuples
```

Do not branch on response mode; the one executor implements both requester protocols.

- [ ] **Step 4: Verify GREEN and shared quota/client lifecycle**

```bash
uv run pytest tests/runtime/test_runtime_assembly.py tests/runtime/test_quota_manager.py -v
```

Expected: all pass with no production runtime change.

- [ ] **Step 5: Refactor/check**

```bash
uv run ruff check tests/runtime/test_runtime_assembly.py
uv run mypy tests/runtime/test_runtime_assembly.py
```

Expected: both pass.

---

### Task 19: Document the nine providers and keep example configuration executable

**Files:**
- Modify: `config.example.toml:4-63`
- Modify: `README.md:50-65`
- Modify: `tests/docs/test_documented_config.py:31-63`
- Reference: `docs/designs/architectures/20260822-multi-web-providers.md` (Minimal Configuration Examples)

- [ ] **Step 1: Write failing documented-config/capability assertions**

Extend `test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help` or add focused sibling tests that inspect resolved example config and registry.

Assert:

```text
all nine registry names are present in config example
scrape_do is used; no dotted scrape.do table
Bright Data has api_url + search_zone + fetch_zone
ZenRows: enable_search=false, enable_fetch=true
ScrapingAnt: enable_search=false, enable_fetch=true
SerpApi: enable_search=true, enable_fetch=false
all other new dual providers enable both stages in the example
new providers expose only approved provider-specific option names
api_key_env values are environment-variable names/placeholders, never credentials
README capability table exactly matches registry capability booleans
no Apify row is added in this batch
```

Prefer parsed/resolved assertions over brittle full README snapshots.

- [ ] **Step 2: Run docs tests and verify RED**

```bash
uv run pytest tests/docs/test_documented_config.py -v
```

Expected: failure because example/capability documentation lacks the nine providers.

- [ ] **Step 3: Add minimal safe example config and capability documentation**

Append sections using the architecture's selected defaults:

```text
brightdata    https://api.brightdata.com      + both zones
scrape_do     https://api.scrape.do
zenrows       https://api.zenrows.com          fetch only
decodo        https://scraper-api.decodo.com
scrapingdog   https://api.scrapingdog.com
scrapegraphai https://v2-api.scrapegraphai.com
scraperapi    https://api.scraperapi.com
scrapingant   https://api.scrapingant.com      fetch only
serpapi       https://serpapi.com              search only
```

Use environment-variable names for credentials. Do not expose country, locale, device, render, premium proxy, result count, pagination, cache/freshness, sessions, AI extraction, crawl, monitor, or async controls.

Update README capability table with the nine provider rows. Add only concise explanatory prose needed for capability limitations; do not document unsupported tuning.

- [ ] **Step 4: Verify GREEN through the real resolver**

```bash
uv run pytest tests/docs/test_documented_config.py tests/unit/test_config_web_providers.py -v
```

Expected: example TOML parses/resolves offline with stub credentials and capability assertions match registry.

- [ ] **Step 5: Refactor/check documentation consistency**

Review names and capabilities against the matrix once more, especially `scrape_do`, ZenRows, ScrapingAnt, and SerpApi.

```bash
uv run ruff check tests/docs/test_documented_config.py
uv run mypy tests/docs/test_documented_config.py
```

Expected: both pass.

---

### Task 20: Run the complete offline verification gate and review architectural leakage

**Files:**
- Verify: all files changed by Tasks 1-19
- Inspect only: core orchestration/scheduler/config/runtime/storage/protocol modules for unintended vendor branches
- Reference: `docs/designs/testings/20260822-multi-web-providers.md` (Verification Sequence)

The approved testing design explicitly requires no new live integration tests for this batch. Do not add credentials, mandatory network access, provider SDKs, or opt-in live smoke files merely for symmetry with older integrations.

- [ ] **Step 1: Run all deterministic feature tests**

```bash
uv run pytest \
  tests/providers/test_http_executor.py \
  tests/providers/test_registry.py \
  tests/providers/web \
  tests/unit/test_config_web_providers.py \
  tests/runtime/test_runtime_assembly.py \
  tests/docs/test_documented_config.py -v
```

Expected: all new adapter/transport/registration/config/runtime/docs tests pass without network or real credentials.

- [ ] **Step 2: Run static analysis**

```bash
uv run ruff check .
uv run mypy src tests
```

Expected: both pass.

- [ ] **Step 3: Run the complete default test suite**

```bash
uv run pytest -v
```

Expected:

```text
all default tests pass
existing live integration tests remain skipped unless their existing opt-in gates are enabled
no new provider credential is required
no network is required by the new tests
```

Pre-feature baseline is `301 passed, 4 skipped`; the passing count should increase only by deterministic tests added in this plan, while the skip count should not increase for these nine providers because no new live tests are required.

- [ ] **Step 4: Review the final diff for architectural regression signals**

Reject or remove any of the following:

```text
brightdata/scrape_do/zenrows/decodo/scrapingdog/scrapegraphai/scraperapi/scrapingant/serpapi names in SearchOrchestrator or FetchScheduler branches
provider-specific branch in config.py
provider-specific branch in Runtime._build_web_providers
provider-specific quota type or separate Search/Fetch quota
adapter direct httpx.AsyncClient usage
adapter retry loop
adapter cheap_check/judge/fallback/URLStore/result-writing logic
new public ErrorCode
new CLI/socket/result fields
raw request/response payload logging
full query-string logging
provider SDK dependency
local SERP parser
AI extraction prompt/schema to emulate Search
async job/task/session persistence
optional vendor controls outside the approved whitelist
Apify registration or implementation
```

Expected production changes remain limited to shared text transport, adapter-local logic, and default registration.

- [ ] **Step 5: Re-run the full gate after cleanup**

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Expected: all green.

---

## Self-Review

### Spec coverage

| Design requirement | Plan task(s) |
|---|---|
| Add nine providers; explicitly defer Apify | Boundaries, 3-19, 20 |
| Preserve `KeywordSearchProvider` / `URLFetchProvider` contracts | Boundaries, 3-14 |
| Preserve domain/result/protocol/storage shapes | Boundaries, 20 |
| Keep ordinary built-in adapter/registry model | 3-12, 16 |
| Add shared text response at HTTP boundary | 1, 2 |
| `JsonRequester` remains unchanged; add narrow text protocol | 1 |
| JSON/text share retry/status/logging lifecycle | 1, 2 |
| 408/429/5xx and transport retries unchanged | 2 |
| Non-retryable 4xx unchanged | 2 |
| Invalid JSON still `ProtocolFailure` without retry | 1, 2 |
| Text 200 returns arbitrary text without protocol decode | 1, 2 |
| Empty text rejected by adapter, not transport | 2, 4-10, 14 |
| Query/userinfo/fragment redaction and no body logging | 1, 2, 5, 8, 10, 20 |
| Bright Data Search + Fetch with independent zones | 3, 4 |
| Scrape.do Search + Markdown Fetch | 5 |
| ZenRows fetch-only in this batch; dedicated SERP endpoint deferred | 6, 16, 17 |
| Decodo Search + Fetch with opaque Basic token | 7 |
| ScrapingDog `/google` + `/scrape` | 8 |
| ScrapeGraphAI v2 only with `SGAI-APIKEY` | 9 |
| ScraperAPI synchronous Search + Fetch only | 10 |
| ScrapingAnt fetch-only `/v2/markdown` | 11, 16, 17 |
| SerpApi search-only Google engine | 12, 16, 17 |
| Per-result Search tolerance, top-level failures propagate | 3, 5, 7-10, 12, 13 |
| Valid empty Search results return `[]` | 13 |
| Fetch missing/empty body is provider execution failure | 4-11, 14 |
| Provider-embedded HTTP-200 failures use existing exceptions without raw diagnostics | 13, 14 |
| Cancellation remains unwrapped | 1, 2, 13, 14, 20 |
| Adapter constructors validate provider-specific required strings with `TypeError` | 4, 15 |
| Runtime converts constructor `TypeError` to `CONFIG_ERROR` | 18 |
| Exact provider option whitelists | 16, 17 |
| Unsupported capability rejected at config resolution | 17 |
| Disabled provider requires no secret/constructor validation | 17 |
| One adapter instance and quota shared across Search/Fetch | 18 |
| No provider-specific config/runtime/orchestrator/scheduler branch | 17, 18, 20 |
| No optional tuning controls | 3-12, 17, 19, 20 |
| Example config/README match registry capabilities | 19 |
| Default tests remain offline/credential-free; no new live tests | 20 |

### File-structure review

Expected final feature footprint:

```text
src/agent_search_gateway/providers/http.py
src/agent_search_gateway/providers/defaults.py
src/agent_search_gateway/providers/web/common.py
src/agent_search_gateway/providers/web/brightdata.py
src/agent_search_gateway/providers/web/scrape_do.py
src/agent_search_gateway/providers/web/zenrows.py
src/agent_search_gateway/providers/web/decodo.py
src/agent_search_gateway/providers/web/scrapingdog.py
src/agent_search_gateway/providers/web/scrapegraphai.py
src/agent_search_gateway/providers/web/scraperapi.py
src/agent_search_gateway/providers/web/scrapingant.py
src/agent_search_gateway/providers/web/serpapi.py

tests/support/http.py
tests/providers/test_http_executor.py
tests/providers/test_registry.py
tests/providers/web/test_brightdata.py
tests/providers/web/test_scrape_do.py
tests/providers/web/test_zenrows.py
tests/providers/web/test_decodo.py
tests/providers/web/test_scrapingdog.py
tests/providers/web/test_scrapegraphai.py
tests/providers/web/test_scraperapi.py
tests/providers/web/test_scrapingant.py
tests/providers/web/test_serpapi.py
tests/providers/web/test_search_result_tolerance.py   # only if shared parameterization is clean
tests/unit/test_config_web_providers.py
tests/runtime/test_runtime_assembly.py
tests/docs/test_documented_config.py

config.example.toml
README.md
```

No new orchestrator, scheduler, state/store, protocol, daemon, CLI, acceptance, or live-integration production path is expected. `config.py` and `runtime.py` are expected to remain production-code unchanged; their existing generic behavior is tested by Tasks 17-18.

### Ordering and dependency review

- Shared text transport is implemented and verified before any text-returning adapter depends on it.
- Bright Data exercises mixed JSON/text typing first, so the requester/test-double shape is proven before the other mixed adapters are added.
- Each provider's basic request/mapping exists before the cross-provider malformed-response and empty-body matrices lock error boundaries.
- Constructor validation is locked before runtime assembly deliberately relies on constructor `TypeError` conversion.
- Adapter behavior is complete before default registration exposes the providers.
- Registration exists before real-default-registry config capability/whitelist tests.
- Config/adapter plumbing exists before runtime object-identity/quota/client-lifecycle assertions.
- Runtime/config wiring exists before example config and README advertise support.
- Full static/default-suite verification is last.

### Type and contract consistency

- Every Search method is consistently `async def search(self, query: str) -> list[KeywordSearchHit]`.
- Every Fetch method is consistently `async def fetch(self, url: NormalizedURL) -> URLFetchCandidate`.
- Search snippets remain snippets; they are not promoted to full page `content` merely to avoid Fetch.
- Text-returning Markdown Fetch integrations use the exact returned text as `raw_content` and `content` when the selected API has no distinct raw representation.
- JSON-returning Markdown Fetch integrations validate the selected provider body field before constructing a candidate.
- Existing `JsonRequester.request_json(...) -> object` remains unchanged.
- New `TextRequester.request_text(...) -> str` uses the same keyword arguments and stage names as JSON transport.
- Mixed adapters depend structurally on both requester capabilities without changing runtime assembly.
- Provider names remain exactly `brightdata`, `scrape_do`, `zenrows`, `decodo`, `scrapingdog`, `scrapegraphai`, `scraperapi`, `scrapingant`, and `serpapi` from registry through config/docs/tests.
- Bright Data option names remain exactly `api_url`, `search_zone`, `fetch_zone`.
- All other new providers expose only `api_url` beyond the shared web-provider keys.
- No new model, `ErrorCode`, quota type, scheduler outcome, protocol field, URL-store field, or result-file field is introduced.

### Final implementation gate

Implementation is ready only after:

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

The final default gate must remain network-free for these nine integrations and must not expose credentials, query-string values, request bodies, fetched HTML/Markdown, or raw provider error payloads in logs or exception messages.
