## Testing: Additional Web Search and Fetch Providers

### 1. Test Strategy

The implementation should be verified primarily at the adapter and shared-transport boundaries using the repository's existing deterministic pytest + `httpx.MockTransport` + recording-executor style. The feature intentionally preserves the core provider protocols, runtime assembly model, search orchestration, fetch scheduling, quota semantics, URL store, and public result formats, so tests should concentrate on the actual new behavior rather than duplicating existing orchestration coverage for each vendor.

Primary goals:

- Prove all nine built-in registrations have the exact capabilities and provider-specific option whitelists approved by the architecture.
- Prove the existing config resolver accepts those options, rejects unsupported stages, preserves the single `api_key_env -> SecretValue` model, and requires no central provider-specific branches.
- Prove the new shared text-response transport preserves existing timeout/retry/status/logging/redaction semantics and differs from `request_json()` only by returning `response.text` instead of decoding JSON.
- Prove each provider adapter constructs only the minimal selected provider request, maps the documented stable response fields into existing gateway domain objects, and ignores unmodeled metadata.
- Prove search adapters keep the existing per-result tolerance boundary while failing malformed top-level responses.
- Prove fetch adapters reject missing/empty bodies and leave semantic body acceptance/rejection to the existing `FetchScheduler`.
- Prove no adapter exposes optional vendor tuning fields that were intentionally deferred.
- Prove existing `SearchOrchestrator`, `FetchScheduler`, `ProviderQuotaManager`, and public result contracts need no vendor-specific branches.
- Keep the default suite completely offline and credential-free.

No new test framework, HTTP mocking framework, provider SDK, snapshot framework, or mandatory network dependency is introduced.

No new live integration test is required for this batch. Nine opt-in live tests would materially increase credential management, external cost, flakiness, and maintenance while providing little architectural coverage beyond deterministic adapter contract tests. A future provider-specific live smoke test may be added when an API proves unusually drift-prone or when real credentials are already available for CI/manual verification.

---

### 2. Test Layers

| Layer | Purpose | Real network? | Suggested location |
|---|---|---:|---|
| Shared HTTP transport | Verify `request_text()` reuses retry/status/logging/redaction behavior | No | `tests/providers/test_http_executor.py` |
| Test HTTP doubles | Record JSON/text requests without changing existing JSON-only tests | No | `tests/support/http.py` |
| Provider adapters | Verify request construction, auth, response mapping, malformed-response behavior | No | `tests/providers/web/test_<provider>.py` |
| Provider fixtures | Small representative provider response shapes | No | `tests/fixtures/providers/<provider>/` where JSON fixtures improve readability |
| Registry | Verify names, static capabilities, factories, allowed config keys | No | existing registry/runtime tests |
| Config | Verify capability rejection, option whitelist, disabled-provider semantics | No | `tests/unit/test_config_web_providers.py` |
| Runtime assembly | Verify generic factory/options plumbing still constructs adapters and shares one quota across dual stages | No | `tests/runtime/test_runtime_assembly.py` only where current generic coverage is insufficient |
| Orchestrator/scheduler regression | Verify no new vendor branch is necessary | No | existing suites; add focused regression only if implementation changes shared behavior |
| Documentation config | Verify examples remain parseable and provider names/options match registry | No | existing `tests/docs/` patterns |

The adapter tests are the primary provider correctness tests. Registry/config tests prove integration into the existing architecture. Core workflow tests should remain provider-agnostic.

---

### 3. Shared Test HTTP Doubles

The current `RecordingJsonExecutor` should remain usable unchanged by all existing tests.

Add the smallest sibling test support needed for new adapters:

```python
@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    url: str
    stage: str
    headers: Mapping[str, str] | None
    json_body: object | None

class RecordingTextExecutor:
    async def request_text(...) -> str: ...

class RecordingHttpExecutor:
    async def request_json(...) -> object: ...
    async def request_text(...) -> str: ...
```

Recommended use:

- JSON-only adapters: existing `RecordingJsonExecutor`.
- Text-only adapters: `RecordingTextExecutor`.
- Adapters whose Search uses JSON and Fetch uses text: `RecordingHttpExecutor`.

`RecordingHttpExecutor` should keep separate queued response types or one explicitly typed queue so a test fails immediately if the adapter invokes the wrong response mode. Do not silently coerce JSON to text or text to JSON in the fake.

The exact fake class names are not production contracts. The invariant is that existing JSON-only tests do not need mass edits merely because production transport gains `request_text()`.

---

### 4. Shared `request_text()` Transport Tests

Extend `tests/providers/test_http_executor.py` with focused tests only for behavior not already proven by `request_json()`.

#### Successful Text Response

Use `httpx.MockTransport` returning HTTP 200 with a sentinel Markdown/HTML body.

Assert:

- `await executor.request_text(...)` returns the exact decoded `response.text`.
- It does not attempt JSON decoding.
- Existing `http_attempt_started` and `http_attempt_completed` events are emitted.
- Response body text is absent from logs.

#### Retryable Status Uses Existing Retry Policy

Return `500`, then `429`, then successful text.

Assert:

- Attempt count follows the existing retry policy.
- `http_retrying` events have the same status/delay categories as JSON mode.
- Final returned text is exact.
- Error response bodies are never logged.

This test can be parameterized/shared with current JSON expectations if that reduces duplication, but a broad refactor of current tests is not required.

#### Non-Retryable HTTP Error

Return `401` or `400` with sensitive text body.

Assert:

- `ExecutionFailure` with `ALL_PROVIDERS_FAILED` is raised exactly as JSON mode.
- No retry occurs.
- Sensitive response body is absent from exception message and logs.

#### Transport Failure

Raise an `httpx.TransportError`, then succeed or exhaust according to the selected test.

Assert the same retry/final failure semantics as existing JSON mode and no exception detail leakage.

#### Query/Userinfo/Fragment Redaction

Call `request_text()` with a URL containing userinfo, a query-string credential sentinel, target URL/search query sentinel, and fragment.

Assert logged endpoint contains only the sanitized scheme/host/path, matching current `http_endpoint_for_log()` behavior.

#### Empty Text Is Not a Transport Error

Return HTTP 200 with `""`.

Assert `request_text()` returns `""` normally.

Rationale: empty-body rejection belongs to fetch adapters, not the HTTP executor.

#### JSON Decode Behavior Is Unchanged

Keep the existing invalid-JSON test proving `request_json()` still raises `ProtocolFailure` without retry. The addition of a shared internal response-execution helper must not change this behavior.

---

### 5. Common Search Adapter Contract Tests

Every search-capable new adapter should prove the same minimum contract in its own provider test file:

1. Constructor accepts the approved minimal configuration.
2. `search("hello world")` sends the documented HTTP method, selected endpoint/path, authentication form, and only the required/basic request fields.
3. Query encoding is correct for spaces/reserved characters.
4. A representative provider response maps into `KeywordSearchHit(url, title, snippet)` with `raw_content == ""` and `content == ""` unless the selected provider API stably returns a genuine page body and the architecture explicitly maps it.
5. Extra provider metadata is ignored.
6. One malformed individual organic result is skipped while valid neighbors remain in order.
7. Malformed required top-level response fails with existing `ExecutionFailure`/`ProtocolFailure` semantics rather than returning `[]`.
8. A structurally valid empty result set returns `[]`.
9. Optional vendor controls deferred by the design are absent from generated requests.

Do not duplicate downstream tests for URL normalization, empty abstract rejection, deduplication, search fan-out, or result writing in every adapter file; these are already `SearchOrchestrator` responsibilities.

---

### 6. Common Fetch Adapter Contract Tests

Every fetch-capable new adapter should prove:

1. Constructor accepts the approved minimal configuration.
2. `fetch(normalize_url(...))` sends the documented method, endpoint/path, authentication, target URL mapping, and selected Markdown/content mode.
3. Successful non-empty body maps to `URLFetchCandidate` exactly as designed.
4. Empty/whitespace-only body fails with existing `ExecutionFailure`.
5. JSON-returning fetch APIs fail on malformed required body/envelope fields.
6. Provider-embedded failure envelopes fail without exposing raw diagnostic content.
7. Optional rendering/proxy/localization/session controls are absent unless they are mandatory for the basic API call.

Do not repeat `cheap_check`, judge, fallback-order, or URL-store mutation tests per adapter. Existing scheduler/orchestrator tests own those semantics.

---

### 7. Provider-Specific Adapter Tests

#### 7.1 Bright Data

Suggested file: `tests/providers/web/test_brightdata.py`.

##### Constructor / Required Zones

Assert valid `api_url`, `search_zone`, and `fetch_zone` construct successfully.

Parameterize invalid/empty/non-string zone values and assert constructor `TypeError`.

A runtime-level focused test should prove constructor `TypeError` is still translated by generic runtime assembly into `CONFIG_ERROR`; do not add Bright Data logic to central config tests.

##### Search Request

Use `RecordingHttpExecutor` JSON mode.

Assert:

- request targets configured `/request` endpoint;
- Bearer authentication uses `SecretValue` only in the Authorization header;
- JSON request uses `search_zone`;
- target URL is a Google search URL derived from the gateway query;
- structured JSON response mode is requested as required by the selected SERP API;
- `fetch_zone` is not used by Search;
- no optional country/language/device/page fields are injected.

Return a compact representative SERP response and assert organic URL/title/snippet mapping.

##### Fetch Request

Use text mode.

Assert:

- same configured `/request` base endpoint;
- `fetch_zone` is used, not `search_zone`;
- target URL is the normalized requested URL;
- selected output mode requests Markdown/text;
- returned text becomes the designed `URLFetchCandidate`.

##### Zone Isolation Regression

One test using both methods on the same adapter should explicitly prove Search and Fetch use the correct independent zones. This is Bright Data's key provider-specific regression.

---

#### 7.2 Scrape.do

Suggested file: `tests/providers/web/test_scrape_do.py`.

##### Search

Assert:

- endpoint is `/plugin/google/search` under configured base;
- token and query are correctly encoded according to provider API;
- response organic entries map into gateway hits;
- optional location/device/page controls are absent.

##### Fetch

Assert:

- normal scrape endpoint is used;
- target URL and token are correctly encoded;
- `output=markdown` is present;
- exact text response maps to the candidate;
- empty text fails.

##### Sensitive Query URL Regression

Because token is in the query string, transport-level redaction coverage plus one adapter request-construction assertion is sufficient; do not add adapter logging infrastructure.

---

#### 7.3 ZenRows

Suggested file: `tests/providers/web/test_zenrows.py`.

ZenRows is fetch-only.

Assert:

- registry/config rejects `enable_search=true` separately in capability tests;
- adapter uses the page-fetch API selected by the architecture and does not call the separately hosted Google Search Results API;
- target URL and API credential are placed according to the selected fetch contract;
- Markdown output is requested;
- non-empty Markdown maps to the candidate;
- empty text fails;
- no JS render/premium proxy/country/session options are injected by default.

Add a negative regression assertion that the generated fetch request does not target the separately hosted `serp.api.zenrows.com` search API that is intentionally deferred in this batch.

---

#### 7.4 Decodo

Suggested file: `tests/providers/web/test_decodo.py`.

##### Authentication

Assert every request uses exactly:

```text
Authorization: Basic <opaque configured secret>
```

Do not test decoding/splitting the token because the adapter must not do so.

##### Search

Assert request uses current real-time scrape endpoint with the Google Search template, `parse=true`, and the gateway query; map parsed organic results to hits.

Assert no deprecated/legacy plan endpoint is used.

##### Fetch

Assert request targets normalized URL with Markdown mode and maps the successful body into a candidate.

Cover provider success envelope/body absence and empty body as fetch failures.

---

#### 7.5 ScrapingDog

Suggested file: `tests/providers/web/test_scrapingdog.py`.

##### Search

Assert `/google` request contains only the required API key + query in the minimal request and maps `organic_results[].link/title/snippet`.

Assert `deep_scrape` is not enabled.

##### Fetch

Assert `/scrape` receives API key + target URL and returned page text maps to `URLFetchCandidate`.

Assert optional `dynamic`, premium proxy, country, wait, and similar controls are absent.

---

#### 7.6 ScrapeGraphAI

Suggested file: `tests/providers/web/test_scrapegraphai.py`.

##### Version / Authentication Regression

Assert adapter uses v2 `/api/search` and `/api/scrape` paths and `SGAI-APIKEY` header.

Add a regression assertion that no v1 endpoint is constructed.

##### Search

Assert minimal request body contains only required/current search input selected by the architecture and maps stable result URL/title/snippet fields.

If stable search results include genuine page content and implementation maps it, add exact body-field assertions. Otherwise explicitly assert `raw_content/content` remain empty; do not infer body semantics from excerpts.

##### Fetch

Assert `/api/scrape` requests Markdown and maps the documented response field to both raw/clean content when appropriate.

Malformed success/error envelope and empty Markdown must fail.

Do not test Extract/Crawl/Monitor APIs because they are out of scope.

---

#### 7.7 ScraperAPI

Suggested file: `tests/providers/web/test_scraperapi.py`.

##### Search

Assert synchronous `/structured/google/search` is used with only API key + query in the minimal request.

Assert organic response mapping and malformed/empty response behavior.

Add a negative assertion that async/batch endpoint fields/task IDs are absent.

##### Fetch

Assert synchronous scrape endpoint receives API key + normalized target URL and returned text maps to candidate.

Assert rendering/premium/country/session flags are absent by default.

---

#### 7.8 ScrapingAnt

Suggested file: `tests/providers/web/test_scrapingant.py`.

ScrapingAnt is fetch-only.

Assert:

- `/v2/markdown` is used;
- `x-api-key` authentication is used;
- normalized target URL is sent according to the provider contract;
- JSON `markdown` field maps to both raw/clean candidate content;
- missing/non-string/empty Markdown fails;
- provider error envelope fails;
- no AI Extract prompt/schema is sent.

Registry/config tests separately prove `enable_search=true` is rejected.

---

#### 7.9 SerpApi

Suggested file: `tests/providers/web/test_serpapi.py`.

SerpApi is search-only.

Assert:

- request uses configured Search endpoint;
- fixed `engine=google` is present;
- gateway query and API secret are correctly encoded;
- no optional location/language/device/result-count settings are injected;
- `organic_results[].link/title/snippet` map to hits;
- malformed individual organic result is skipped;
- malformed top-level response/provider error fails;
- valid empty organic results return `[]`.

Registry/config tests separately prove `enable_fetch=true` is rejected.

---

### 8. Registry Tests

Extend built-in registry coverage with exact assertions for all nine registrations.

Expected registrations:

```text
brightdata    search=true  fetch=true  keys={api_url, search_zone, fetch_zone}
scrape_do     search=true  fetch=true  keys={api_url}
zenrows       search=false fetch=true  keys={api_url}
decodo        search=true  fetch=true  keys={api_url}
scrapingdog   search=true  fetch=true  keys={api_url}
scrapegraphai search=true  fetch=true  keys={api_url}
scraperapi    search=true  fetch=true  keys={api_url}
scrapingant   search=false fetch=true  keys={api_url}
serpapi       search=true  fetch=false keys={api_url}
```

Also preserve registration-order assertions if the current test suite treats built-in order as intentional. Append the new registrations rather than reordering existing providers unless implementation has a documented reason.

Do not add registry concepts for endpoint type, auth type, JSON/text response mode, or required options. Those remain adapter details.

---

### 9. Configuration Tests

Extend `tests/unit/test_config_web_providers.py` only where the current generic tests do not already prove behavior.

#### Capability Rejection

Parameterize the three explicit unsupported combinations:

```text
zenrows + enable_search=true
scrapingant + enable_search=true
serpapi + enable_fetch=true
```

Assert existing `ConfigFailure` with `CONFIG_ERROR`.

#### Allowed Option Plumbing

Use the real default registry to prove representative provider options survive into `ResolvedWebProviderConfig.options` unchanged:

- Bright Data: `api_url`, `search_zone`, `fetch_zone`.
- One normal `{api_url}` provider such as Scrape.do.

There is no need to repeat identical `api_url` plumbing assertions for all eight remaining providers after registry tests prove their whitelist.

#### Unknown Option Rejection

Use one representative new provider and assert an optional vendor field deliberately not exposed, e.g. `country`, is rejected as an unknown config key.

This proves the chosen minimal surface without nine duplicate tests.

#### Disabled Provider Semantics

Use one representative new provider with both stages disabled and no credential to prove existing no-secret behavior still applies. Provider constructor-only validation need not run when disabled.

#### Bright Data Constructor Validation

Do not special-case zone validation in `config.py`. Test invalid zones directly on `BrightDataAdapter`; add one runtime construction test proving the existing constructor-`TypeError` conversion to `CONFIG_ERROR` if current generic runtime tests do not already establish this strongly enough.

---

### 10. Runtime Assembly Tests

The existing runtime assembly is generic and should stay generic.

Add only focused assertions required by new shared behavior:

- A dual-capability new adapter is constructed once and the same object participates in both search and fetch tuples.
- Its one configured web quota remains shared across both stages; do not introduce search/fetch-specific quotas.
- `provider_config.options` are passed through by the existing `kwargs.update(...)` path.
- Existing reserved keys `name`, `http_executor`, and `secret` remain protected.
- The executor object injected into a mixed JSON/text adapter provides both `request_json()` and `request_text()` at runtime.

Do not add one runtime test per provider. Adapter constructor/request tests plus registry metadata fully cover vendor-specific differences.

---

### 11. Orchestrator and Scheduler Regression Tests

No provider-name-specific test should be added to `SearchOrchestrator` or `FetchScheduler` merely because nine providers are registered.

Existing tests should remain authoritative for:

- concurrent keyword provider fan-out;
- one provider failure while others complete;
- all-provider search failure;
- URL normalization/deduplication;
- search body admission;
- per-provider quota acquisition;
- capacity-aware fetch selection;
- execution-failure fallback;
- semantic-failure fallback;
- empty/invalid candidate rejection;
- accepted candidate behavior.

Add a shared regression test only if `request_text()` implementation forces a change to one of these core components. Under the approved architecture, it should not.

A useful implementation review criterion is: if a test in these core directories needs to mention `brightdata`, `scrape_do`, `zenrows`, `decodo`, `scrapingdog`, `scrapegraphai`, `scraperapi`, `scrapingant`, or `serpapi`, re-check whether provider-specific logic has leaked into the core.

---

### 12. Documentation / Example Configuration Tests

Update the existing documented-config tests so the example TOML containing the nine providers remains parseable.

Assertions should cover:

- exact provider registry names, especially `scrape_do` rather than dotted `scrape.do`;
- ZenRows and ScrapingAnt examples enable only Fetch;
- SerpApi example enables only Search;
- Bright Data example contains both required zone fields;
- all provider credentials remain environment-variable references/placeholders, never literal credentials;
- no optional tuning controls appear in the minimal examples unless later explicitly approved.

README capability table should match registry capabilities.

Do not make documentation tests perform real network requests or require any provider environment variable to be set when examples are only parsed/inspected.

---

### 13. Fixture Strategy

Use JSON fixtures only when they make a provider response shape easier to understand or reuse. Do not create a large schema fixture corpus for every vendor.

Recommended approach:

- Small one-off response objects may stay inline in adapter tests.
- Use `tests/fixtures/providers/<provider>/search.json` when a nested organic-result envelope is sufficiently verbose to obscure the test.
- Use `fetch.json` only for JSON-returning fetch providers where the envelope matters.
- Text-returning fetch providers should generally use literal short Markdown/HTML strings in the test rather than fixture files.

Fixtures should contain only fields needed to prove stable mapping plus one or two ignored metadata fields. Do not copy complete vendor API examples or schemas into the repository.

---

### 14. Minimum Provider Test Matrix

| Provider | Basic search | Search malformed/empty | Basic fetch | Fetch empty/malformed | Auth/request-specific regression |
|---|---:|---:|---:|---:|---:|
| Bright Data | yes | yes | yes | yes | zones + Bearer |
| Scrape.do | yes | yes | yes | yes | query token + Markdown |
| ZenRows | n/a | capability rejection | yes | yes | fetch-only; dedicated SERP endpoint deferred |
| Decodo | yes | yes | yes | yes | opaque Basic token |
| ScrapingDog | yes | yes | yes | yes | `/google` vs `/scrape` |
| ScrapeGraphAI | yes | yes | yes | yes | v2 + `SGAI-APIKEY` |
| ScraperAPI | yes | yes | yes | yes | synchronous endpoints |
| ScrapingAnt | n/a | capability rejection | yes | yes | `/v2/markdown` + `x-api-key` |
| SerpApi | yes | yes | n/a | capability rejection | `engine=google` |

"Search malformed/empty" does not require a separate test for every malformed field if a common per-result tolerance test plus one top-level-malformed test demonstrates the boundary. Use parameterization where it keeps the test readable.

---

### 15. Verification Sequence

Implementation verification should run the repository's existing no-network gates without introducing special provider commands:

```text
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

Provider adapter tests must pass with no provider credentials and no network access.

Before considering the implementation complete, inspect the diff for these architectural regression signals:

- provider names or vendor field names added to `SearchOrchestrator`, `FetchScheduler`, `URLStore`, CLI, or protocol code;
- provider-specific branches added to `config.py`;
- direct `httpx` calls inside new provider adapters instead of the shared executor;
- retry loops inside adapters;
- optional vendor tuning fields added beyond the approved minimal configuration;
- raw request/response payload logging;
- new public error codes or result fields;
- duplicated core workflow tests once per vendor.

If none of those appear and the transport/adapter/registry/config tests pass, the implementation satisfies the minimal-intrusion design intent.
