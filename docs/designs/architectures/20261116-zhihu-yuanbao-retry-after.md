## Architecture: Retry-After and Zhihu/Yuanbao Keyword Providers

**Status:** Draft for user review; no implementation  
**Date:** 2026-11-16  
**Base:** eeeff82 (v0.2.2)  
**Companion specs:** [Error handling](../error-handlings/20261116-zhihu-yuanbao-retry-after.md) | [Testing](../testings/20261116-zhihu-yuanbao-retry-after.md)

### 1. Scope & Assumptions

#### In Scope
- Recognize valid HTTP `Retry-After` in the shared executor for existing retryable HTTP statuses. This directly benefits Semantic Scholar and arXiv without editing their adapters.
- Add two independent, search-only Web providers: `zhihu` and `yuanbao`.
- One Zhihu adapter queries both the official site-search and global-search endpoints per keyword, returns the usable combined results, and tolerates one failed branch.
- One Yuanbao adapter uses the official Tencent Cloud SearchPro **service API-key** transport and maps only links/titles/summaries.
- Register the adapters via existing `WebProviderRegistration`, use existing provider configuration and diagnostics, update README and sample configuration, and add tests.
- Document the occasional Bright Data HTTP-200/unparseable-response limitation in README without changing its implementation.

#### Todo / Out of Scope
- No DeepSeek Search/Fetch implementation (deferred to [issue #98](https://github.com/Dingxingdi/agent-search-gateway/issues/98)).
- No generic per-provider API option configuration (deferred to [issue #24](https://github.com/Dingxingdi/agent-search-gateway/issues/24)).
- No new `backend` selector, retry granularity, provider-specific retry policy, Fetch adapter, SDK dependency, generic parser, full-response log archive or business-error auto-retry.
- No modification to SearchOrchestrator, FetchOrchestrator, URLStore, provider contracts, config resolver, runtime composition, academic adapters, Bright Data adapter, CLI/daemon protocol or JSONL result format.
- No version/tag/release action in the design-only phase.

#### Assumptions and source contracts
- Existing provider credentials are supplied as environment variables selected by the user's own `api_key_env` setting; the docs must not contain credential values.
- Zhihu endpoints: `GET https://developer.zhihu.com/api/v1/content/zhihu_search` and `GET https://developer.zhihu.com/api/v1/content/global_search`; `Query` parameter, Bearer Access Secret, second-resolution `X-Request-Timestamp`. The two upstream calls may consume two API quotas for one logical search.
- Zhihu successful responses carry provider success status and a list of hits; verify the exact `Data.Items` and hit-field contract against official docs/sanitized live fixture before finalizing the mapping; do not guess undocumented variants.
- Yuanbao service-key endpoint: `POST https://api.wsa.cloud.tencent.com/SearchPro`, JSON body with only `Query`; response `Response.Pages` is an array of JSON-encoded strings.
- Yuanbao `passage` is a standard summary and `content` a dynamic summary; neither is full fetched page content.
- The user approved valid server `Retry-After` over the locally configured backoff, with no arbitrary server-header wait maximum. The configured `max_delay_seconds` remains the local exponential-backoff cap.
- No new public configuration options are needed. Fixed/default vendor options follow existing Brave/AnySearch conventions.

Official references: [Zhihu developer platform](https://developer.zhihu.com/), [Zhihu API IDs](https://developer.zhihu.com/docs?key=quota), [Tencent service API-key usage](https://cloud.tencent.com/document/product/1806/130615), [Tencent SearchPro result fields](https://cloud.tencent.com/document/product/1806/121811).

---

### 2. Architecture Summary

Preserve the CLI → daemon → SearchOrchestrator → keyword adapter → HttpJsonExecutor → retry_async path. Register Zhihu and Yuanbao exactly like existing Web search-only providers with capabilities `search=True` and `fetch=False`, optional `api_url` and existing environment-based credential resolution. New adapters only implement `search(query) -> list[KeywordSearchHit]`. Existing orchestration continues to own quotas, URL canonicalization, redirect unwrapping, deduplication, URLStore admission and result writing. Retry-After is implemented only at the shared HTTP retry boundary, with no redesign of academic or Web search flows.

---

### 3. Design Decisions

#### Provider Integration

##### One adapter per new logical provider
- **Description:** Add `providers/web/zhihu.py` and `providers/web/yuanbao.py`. Both use existing `JsonRequester`, `SecretValue`, response helpers, `CandidateCounts` and `KeywordSearchHit`. Add two declarations to `providers/defaults.py` (search-only; allowed vendor option `api_url`).
- **Rationale:** `Runtime._build_web_providers` already injects `name`, `http_executor`, `secret` and allowed config options; no registration or runtime refactor is needed.
- **Trade-offs:** Two small vendor-specific parsers require separate tests.
- **Rejected alternatives:** A shared configurable search-provider framework, separate new registry or changes to common hit contracts.

##### Zhihu site + global search under one name
- **Description:** One `search(query)` starts the two independent HTTP GET operations, each with its own stage for diagnostic correlation. Await both; retain valid results if one branch fails. Use existing orchestration to deduplicate URLs; do not add an adapter-local URL set.
- **Rationale:** The user selected both APIs under one selectable provider (`--provider zhihu`).
- **Trade-offs:** Two remote calls and potential concurrent API consumption per one keyword search; both remain within one existing per-provider lease.
- **Rejected alternatives:** `zhihu_site` and `zhihu_global` as two registrations, orchestrator changes or bespoke URL dedup.

##### Yuanbao service-key SearchPro adapter
- **Description:** POST `{"Query": query}` with the existing HTTP client. Check top-level `Response.Error` before treating HTTP 200 as success. Decode each JSON-encoded `Pages` string. Map `url`, `title` and `snippet=(nonempty content or passage)`; always leave `raw_content` and `content` in `KeywordSearchHit` empty.
- **Rationale:** Tencent's documented service-key mode needs no SDK or signing client; snippets must not be mistaken for fetched body content.
- **Trade-offs:** Per-result JSON parsing is necessary; advanced Tencent filters remain unavailable until a future issue.
- **Rejected alternatives:** Tencent Cloud Python SDK, assuming JSON objects within Pages, writing summaries into cached page bodies.

#### Runtime Model and Concurrency

##### Reuse pipeline ownership
- **Description:** All quota leasing, retry invocation, provider-terminal outcome events, URL normalization/redirect logic, deduplication, URLStore updates and file output stay in their current components. Zhihu only performs a two-way local fan-out.
- **Rationale:** Core components already provide everything these adapters need.
- **Trade-offs:** Valid duplicates still pass through initial candidate staging before generic deduplication.
- **Rejected alternative:** Change SearchOrchestrator to understand Zhihu's two APIs or Yuanbao's JSON-string envelopes.

#### Retry Policy

##### Server-directed waiting on existing retryable HTTP statuses
- **Description:** For status 408, 429 or 5xx, parse `Retry-After` delta-seconds or HTTP-date. Attach only the validated nonnegative duration to the internal retryable-status exception. Pass the existing delay adjuster into `retry_async` for normal HTTP-status errors as well as provider-header errors. A valid server wait is authoritative; invalid/absent headers fall back to the existing capped exponential delay. The generic adjustment hook currently clamps to `max_delay_seconds`; make its adjusted result authoritative while retaining the cap when the local delay is initially computed. Existing provider-header jitter continues to operate within the original cap.
- **Rationale:** Semantic Scholar calls the executor's JSON method and arXiv its text method; both already use the same transport retry path.
- **Trade-offs:** Server-directed waits may exceed locally configured maximum backoff; attempt count, request timeout and cancellation remain unchanged.
- **Rejected alternatives:** Separate Semantic Scholar/arXiv retry loops, a new hard Retry-After limit, additional provider config or retrying XML/JSON parsing failures.

#### Interface / Protocol and State Management

##### Keep existing public contracts
- **Description:** No new CLI flags, config schema, result fields, Web/Academic protocol or storage format. Each sample config uses existing `enable_search`, `enable_fetch`, `api_url` and `api_key_env` fields; the last names an environment variable chosen by the user. Disallow unsupported Fetch and unknown vendor options using existing config validation.
- **Rationale:** This feature is additive.
- **Trade-offs:** Advanced search options are intentionally deferred.
- **Rejected alternative:** Add `extra_body` or `backend` fields now.

#### Storage / Persistence and Security

##### Keep search snippets separate from fetched bodies
- **Description:** Return link/title/snippet only. Existing URLStore and Fetch scheduler retain responsibility for admissible URLs and webpage body retrieval. Secrets are accessed only to form request headers. Log bounded allowlisted status/error fields and counts, never raw payloads or authentication headers.
- **Rationale:** Secure, low-coupling integration and correct subsequent URL Fetch.
- **Trade-offs:** Full vendor responses cannot be reconstructed from debug logs alone.
- **Rejected alternative:** Persist provider JSON blobs or pretend SearchPro summaries are page content.

#### Observability

##### Reuse existing attempt, candidate and provider events
- **Description:** Executor owns `http_attempt_started/completed`, `provider_response_diagnostic`, `http_retrying`, `http_failed`. Adapters classify business envelopes and emit `provider_candidates_summary`. Zhihu reuses `provider_partial_failure` for one failed internal branch. Orchestrators remain the only owners of terminal `provider_completed/provider_failed`. Add a small non-sensitive retry-delay-source field to existing retry events.
- **Rationale:** Attempts, branches and final provider outcomes are different scopes.
- **Trade-offs:** A single incident can legitimately produce multiple scoped log records.
- **Rejected alternative:** Duplicated terminal events, a new logging subsystem, or silent skipped failures.

#### Future Migration

##### Do not prebuild features from deferred issues
- **Description:** DeepSeek (#98) and provider-specific parameter overrides (#24) remain independent follow-up changes.
- **Rationale:** Preserve the project's intentionally low coupling and keep this release reviewable.
- **Trade-offs:** Future optional vendor parameters will require another change.
- **Rejected alternative:** Generalize before actual use cases demand it.

---

### 4. Component Catalog

| Component | Purpose | Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
| --- | --- | --- | --- | --- | --- | --- |
| `ZhihuAdapter` (new) | Zhihu keyword search | Two API calls, auth/timestamp, partial success, hit mapping | Existing `search(query)` | JsonRequester, common diagnostics | No persistent state | Source/transformer |
| `YuanbaoAdapter` (new) | Tencent keyword search | SearchPro, business-error check, JSON-string pages, hit mapping | Existing `search(query)` | JsonRequester, common diagnostics | No persistent state | Source/transformer |
| `build_default_registry` | Register providers | Add two search-only entries | Existing factory | Adapter constructors, ProviderCapabilities | Registry entries | Registry |
| `resolve_web_provider_config` (unchanged) | Config validation | Existing allowlist/capability/credential validation | Existing resolver | ProviderRegistry | No | Validator |
| `Runtime._build_web_providers` (unchanged) | Adapter composition | Construct HttpJsonExecutor and adapter | Existing runtime builder | Config and registry | HTTP clients | Composer |
| `HttpJsonExecutor` (small edit) | HTTP attempts | Retryable statuses, Retry-After, HTTP diagnostics | `request_json` / `request_text` | httpx, retry_async | Per-instance client | Executor |
| `retry_async` (small edit) | Retry scheduling | Attempt count, local backoff, authoritative adjusted delay | Existing function | RetryPolicy, sleep | Per-call attempts | Scheduler |
| `SearchOrchestrator` (unchanged) | Search coordinator | Quota, normalization, dedup, terminal logs and output | `keyword_search` | Providers, URLStore | Per-call results | Coordinator |
| `URLStore` / `ResultWriter` (unchanged) | Persist accepted links | Existing result record and JSONL | Existing interfaces | Filesystem | Yes | Sink |
| `observability` / `CandidateCounts` (unchanged) | Safe logs | Error field allowlists, counters, redaction | Existing helper functions | logging | Event-local | Telemetry |

The HTTP executor must not understand vendor result objects; the new adapters must not know URLStore, academic aggregation or the Fetch scheduler.

---

### 5. Data Flow

#### 5.1 Keyword search

~~~text
CLI keyword-search(query, --provider) -> daemon -> SearchOrchestrator
    if query/provider invalid: reject using existing errors
    for selected Web provider:
        acquire existing provider lease
        if zhihu:
            start official zhihu_search and global_search HTTP GETs
            each HTTP call retries/logs independently
            for each response:
                if cancelled: propagate cancellation
                if branch failed: retain its typed failure
                else: check business Code and Data.Items; map usable hits
                      skip malformed hits and count reasons
            if both branches failed: raise existing ExecutionFailure
            if one branch failed: log one provider_partial_failure
            return combined successful hits (possibly [])
        else if yuanbao:
            POST SearchPro with Query
            if HTTP/transport fails: existing executor retries/fails
            if Response.Error exists: raise existing typed failure with safe diagnostics
            if overall Response/Pages invalid: raise existing protocol failure
            for page string in Pages:
                if invalid JSON/record/URL: count rejection; continue
                else: return KeywordSearchHit(url,title,snippet), no fetched body
            log provider_candidates_summary
        if provider raised: orchestrator records one provider_failed
        else: orchestrator stages, normalizes, deduplicates, admits and completes
    if all providers failed: existing ALL_PROVIDERS_FAILED
    else: ResultWriter creates the existing keyword JSONL
~~~

#### 5.2 Academic paper search

~~~text
CLI paper-search(query, --provider semantic_scholar|arxiv)
    existing academic adapter calls HttpJsonExecutor.request_json/request_text
    for each request attempt:
        log response metadata
        if existing retryable HTTP status:
            if attempts remain:
                choose valid Retry-After delay; otherwise local exponential backoff
                log http_retrying with chosen wait; sleep; retry
            else: log http_failed and raise existing HTTP status failure
        else if nonretryable HTTP status: raise existing failure
        else: give JSON/text to unchanged paper parser
    existing Academic orchestrator handles terminal result/failure
~~~

#### 5.3 URL Fetch and configuration (unchanged)

~~~text
Runtime config resolver reads existing per-provider keys and environment secrets
    if provider requires key and key missing: existing ConfigFailure
    if Fetch enabled for Zhihu or Yuanbao: existing capability validation rejects it

CLI url-fetch(admitted URL) -> existing Fetch scheduler
    neither new adapter is Fetch-capable
    existing body/fallback logic runs unchanged
~~~

---

### 6. Interfaces & Contracts

**Existing internal search result (unchanged):**

~~~python
KeywordSearchHit(url: str, title: str = "", snippet: str = "",
                 raw_content: str = "", content: str = "")
~~~

Both new adapters return `list[KeywordSearchHit]`; an empty list is a valid successful empty search. A URL with no usable snippet may fall back to the title at the existing orchestrator boundary.

**Existing config (no new keys):** Each `[web_providers.zhihu]` and `[web_providers.yuanbao]` table uses `enable_search=true`, `enable_fetch=false`, its official `api_url` and the existing `api_key_env` field pointing to a user-supplied environment-variable name. The concrete environment-variable name is not hardcoded by the adapter.

**Vendor boundaries:** Zhihu checks provider success Code and the actual documented result list before mapping explicit URL/title/summary fields. Yuanbao checks `Response.Error` before `Response.Pages` and parses each page string individually. Both leave `raw_content`/`content` empty and emit only existing-safe diagnostics.

**Retry boundary:** `RetryPolicy.max_attempts` still determines attempts; `max_delay_seconds` caps only computed backoff. Valid server HTTP `Retry-After` is a nonnegative override; missing/invalid header preserves old behavior.

**Expected source change budget:** two new Web adapter files; small changes to `providers/defaults.py`, `providers/http.py`, `retry.py`; README/config example updates and focused tests. Everything else listed above remains unchanged.

This is a reviewed-design candidate, not an implementation authorization.
