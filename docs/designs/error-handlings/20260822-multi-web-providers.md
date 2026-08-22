## Error Handling: Additional Web Search and Fetch Providers

### 1. Error-Handling Principles

This feature preserves the gateway's existing error taxonomy, provider-isolation behavior, and fetch fallback semantics. Adding nine providers and a text-response transport mode must not introduce a new public error code, provider-specific exception hierarchy, retry subsystem, or scheduler path.

Primary rules:

- Invalid enabled-provider configuration fails daemon startup through the existing `ConfigFailure(ErrorCode.CONFIG_ERROR, ...)` path.
- Unsupported requested capabilities continue to fail during existing web-provider config resolution.
- HTTP timeout, transport failure, retryable status, non-retryable HTTP status, and lifecycle logging remain centralized in `HttpJsonExecutor` for both JSON and text response modes.
- Invalid JSON remains `ProtocolFailure(ErrorCode.PROTOCOL_ERROR, ...)` from the shared HTTP boundary.
- A successful HTTP text response is not parsed by the transport. Empty/whitespace-only page bodies are rejected by the provider adapter because the fetch contract requires usable body content.
- Provider JSON envelope/shape failures are translated at adapter boundaries using the existing `failure(...)`, `require_object(...)`, `require_list(...)`, `require_string(...)`, `non_empty_string(...)`, and related helpers where applicable.
- Search remains tolerant at the individual result-entry boundary: one malformed result is skipped when the adapter can isolate the problem to that result.
- A malformed required top-level search response fails only that provider pipeline; other keyword providers continue.
- A fetch adapter either returns one valid non-empty `URLFetchCandidate` or raises an existing execution/protocol failure. `FetchScheduler` owns fallback to another provider.
- Body semantic rejection (`cheap_check` / judge rejection) remains owned by `FetchScheduler`; adapters do not classify a valid-but-unacceptable page body as transport/protocol failure.
- `asyncio.CancelledError` is never converted into a provider failure.
- API credentials, query strings containing credentials, request bodies containing user search queries/target URLs, fetched page bodies, and raw vendor error payloads must not be added to exception or log messages.

No change is made to `ErrorCode`.

---

### 2. Configuration Failures

#### Unknown Provider-Specific Option

Condition:

- A configured provider table contains a field other than the existing shared web-provider fields or that provider's declared `allowed_config_keys`.

Handling:

- Preserve existing `_validate_options()` behavior in `resolve_web_provider_config()`.
- Raise `ConfigFailure(ErrorCode.CONFIG_ERROR, "unknown config key(s) for <provider>: ...")`.
- Fail startup before adapter construction or network access.

Rationale:

- Provider option whitelisting already exists and keeps provider-specific schema knowledge out of central config branches.

#### Unknown Enabled Provider

Condition:

- A provider not registered in `ProviderRegistry` is enabled for search or fetch.

Handling:

- Preserve existing `ConfigFailure(CONFIG_ERROR, "unknown enabled web provider: ...")` behavior.

#### Unsupported Requested Capability

Conditions:

- `zenrows.enable_search = true`.
- `scrapingant.enable_search = true`.
- `serpapi.enable_fetch = true`.
- Any future registration requests a stage for which its static capability is false.

Handling:

- Existing config resolution checks `registration.capabilities` and raises `ConfigFailure(CONFIG_ERROR, ...)` before runtime construction.
- Do not defer this error to a missing adapter method at request time.

#### Missing Credential Environment Variable

Condition:

- A provider is enabled for at least one supported stage and `api_key_env` is absent/empty or names an unset/empty environment variable.

Handling:

- Preserve existing web-provider config behavior.
- Raise `ConfigFailure(CONFIG_ERROR, ...)` during startup.
- The message may contain the environment-variable name but must never contain the credential value.

#### Invalid Common Concurrency Configuration

Condition:

- `max_concurrency` is non-integer, boolean, or non-positive.

Handling:

- Preserve existing `_positive_int(...)` validation and `CONFIG_ERROR` behavior.

#### Missing/Invalid Provider-Specific Required String

Conditions include:

- Empty/non-string `api_url` passed to an adapter whose constructor requires it.
- Bright Data `search_zone` or `fetch_zone` absent, empty, or non-string.

Handling:

- Validate at adapter construction rather than adding vendor branches to `config.py`.
- Raise `TypeError` from the constructor.
- Preserve `Runtime._build_web_providers()` behavior: constructor `TypeError` becomes `ConfigFailure(ErrorCode.CONFIG_ERROR, "Invalid configuration for web provider <name>")`.

Bright Data note:

- Both zones are required by the minimal adapter constructor even if only one gateway stage is enabled.
- This is an intentional minimal-core-change trade-off; do not add stage-dependent required-option semantics to registry/config solely to improve this case.

#### Disabled Provider

Condition:

- Both `enable_search=false` and `enable_fetch=false`.

Handling:

- Preserve current behavior: no credential is required and no adapter is constructed.
- Top-level option-name validation still applies before the disabled-provider early return.
- Provider constructor validation is not required because the adapter is not part of the runtime.

---

### 3. Shared HTTP Transport Failures

`request_json()` and the new `request_text()` must share one transport/status/retry implementation. The response-mode difference occurs only after a successful HTTP response has passed those checks.

#### Retryable HTTP Status

Condition:

- HTTP `408`, `429`, or any `5xx` response.

Handling:

- Use the existing resolved `RetryPolicy`.
- Emit the existing `http_retrying` events with sanitized endpoint, provider, stage, attempt, status, delay, and timing.
- After retry exhaustion, raise the same provider `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "<provider>/<stage>: HTTP status <status>")` behavior as today.

Stage consequences:

- Search: only that provider pipeline fails; completed provider pipelines remain usable.
- Fetch: that provider attempt is recorded as an execution failure and `FetchScheduler` may select another unattempted provider.

#### Timeout or Transport Failure

Condition:

- `httpx.TimeoutException` or `httpx.TransportError`.

Handling:

- Reuse existing retry behavior.
- After retry exhaustion raise existing provider `ExecutionFailure` with transport-failure semantics.
- Do not add retries inside individual adapters.

#### Non-Retryable HTTP Error

Condition:

- HTTP `4xx` other than `408` or `429`.

Handling:

- Do not retry.
- Raise existing provider `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, ...)`.
- This includes authentication, authorization, invalid-request, quota-plan, or target-rejection responses represented as non-retryable HTTP statuses.
- Do not introduce provider-specific public error codes for `401`, `403`, or other vendor statuses.

#### Invalid JSON in `request_json()`

Condition:

- HTTP response is successful but `response.json()` raises `ValueError`.

Handling:

- Preserve existing `ProtocolFailure(ErrorCode.PROTOCOL_ERROR, "<provider>/<stage>: response was not valid JSON")` behavior.
- Do not retry solely because JSON decoding failed; HTTP transport already succeeded.
- Adapters must not catch this and convert it to `[]` or an empty fetch candidate.

#### Text Response in `request_text()`

Condition:

- HTTP response is successful.

Handling:

- Return `response.text` without JSON decoding.
- Do not classify arbitrary text, HTML, or Markdown as a protocol error at the transport layer.
- Do not log the returned text.

#### Empty Text Response

Condition:

- `request_text()` returns `""` or whitespace-only text for a URL-fetch operation.

Handling:

- The transport returns it normally because HTTP succeeded.
- The adapter rejects it using a concise existing provider `ExecutionFailure`, e.g. `failure(name, "fetch", "page body is empty")`.
- `FetchScheduler` then follows normal execution-failure fallback.

Rationale:

- Empty-body validity is a provider/domain contract issue, not an HTTP transport error.

#### Text Decoding

Handling:

- Use `httpx.Response.text` and its normal decoding behavior.
- Do not add provider-specific charset detection or decoding exceptions in this feature.
- If a provider requires a future binary response contract, that is a separate transport capability and is out of scope.

#### Cancellation

Condition:

- Search/fetch is cancelled while waiting for quota, during retry sleep, HTTP I/O, parsing, or orchestration.

Handling:

- Preserve `asyncio.CancelledError` propagation.
- Never translate cancellation into `ALL_PROVIDERS_FAILED` or an empty result.
- Existing context-manager/quota cleanup remains responsible for releasing acquired capacity.

---

### 4. Search Response Failures

These rules apply to Bright Data, Scrape.do, Decodo, ScrapingDog, ScrapeGraphAI, ScraperAPI, and SerpApi.

#### Malformed Required Top-Level Search Response

Examples:

- Expected root object is not an object.
- Expected result collection is missing or not an array/object of the selected documented shape.
- Provider returns HTTP 200 with a success/error envelope indicating the request failed and no valid result payload is present.

Handling:

- Raise an existing provider execution/protocol failure from the adapter.
- Do not return `[]` for a malformed required envelope.
- `SearchOrchestrator` isolates the failure to this provider pipeline.
- If at least one other provider pipeline completes, keyword search succeeds with those results.
- If every provider pipeline fails, preserve existing `ALL_PROVIDERS_FAILED` behavior.

#### Provider-Embedded Failure in HTTP 200

Condition:

- Vendor returns HTTP 200 but its JSON body explicitly indicates failure, contains an error object instead of the required search data, or omits the documented success payload due to vendor-side rejection.

Handling:

- Adapter raises `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "<provider>/search: provider reported failure")` or another concise non-sensitive reason.
- Do not surface complete provider `message`, diagnostic, request payload, stack trace, or echoed query text.

Rationale:

- HTTP success does not imply provider operation success, but raw vendor diagnostics may contain request/user information.

#### Malformed Individual Search Result

Examples:

- Result item is not an object.
- URL/link is absent, non-string, or empty.
- Optional title/snippet field is present with an invalid type.
- A provider-specific nested organic result object has an invalid required field.

Handling:

- Parse each result inside the established per-entry tolerance boundary.
- Catch only the existing provider parse `ExecutionFailure` for that result.
- Skip that entry and continue with subsequent results.
- Preserve the original provider result order for valid entries.

Do not catch:

- `asyncio.CancelledError`;
- transport failures;
- invalid top-level JSON;
- required top-level response-shape failures.

#### Empty Valid Search Result Set

Condition:

- Provider returns a structurally valid successful response with zero organic/web results, or every isolated result entry is malformed and skipped.

Handling:

- Return `[]`.
- This counts as a completed provider pipeline under existing orchestration semantics, not provider execution failure.

#### Search Result Has Empty Abstract Material

Condition:

- Adapter returns a structurally valid hit with an empty snippet but possibly a title.

Handling:

- Preserve existing `SearchOrchestrator._stage_keyword_hit()` behavior: snippet is primary abstract material and title is fallback.
- If both are empty, the orchestrator rejects the candidate with existing `empty_abstract` semantics.
- Adapter must not invent page text or perform a second fetch merely to populate a search abstract.

#### Search Result URL Is Syntactically Invalid

Condition:

- Provider supplies a non-empty URL string that later fails gateway URL normalization.

Handling:

- Preserve existing orchestration behavior; do not add provider-specific URL normalization rules.
- The search pipeline handles invalid hit data through its existing invalid-provider-data boundary.

---

### 5. Fetch Response Failures

These rules apply to Bright Data, Scrape.do, ZenRows, Decodo, ScrapingDog, ScrapeGraphAI, ScraperAPI, and ScrapingAnt.

#### Successful HTTP but Missing Page Body

Conditions:

- Text-returning provider returns empty/whitespace-only text.
- JSON-returning provider lacks its selected Markdown/content field.
- Required body field is null/non-string/empty.
- Provider returns a successful envelope but no usable content for the requested target.

Handling:

- Raise an existing provider `ExecutionFailure` with a concise reason such as `page body is empty` or `page body was not returned`.
- Do not create `URLFetchCandidate(raw_content="")` because the scheduler contract rejects empty raw content.
- Do not mark the URL unavailable in the adapter.
- `FetchScheduler` records the execution failure and may try another provider.

#### Provider-Embedded Fetch Failure in HTTP 200

Condition:

- JSON response indicates provider operation failure despite successful HTTP status.

Handling:

- Adapter raises existing `ExecutionFailure`.
- Do not copy raw provider diagnostics into the gateway exception message.
- Scheduler may fall back normally.

#### Redirected/Final URL Metadata

Condition:

- Provider returns final/redirect URL metadata that differs from the requested normalized URL.

Handling:

- Do not require final URL equality unless the selected provider API is a multi-URL/batch response where matching is necessary to identify the returned body.
- For one-request/one-body synchronous scrape APIs, the body is treated as the response to the requested target and the gateway's existing fetch contract does not gain a redirect metadata field.
- If a batch-shaped API must select a matching result, use existing `normalized_match()` semantics; malformed matching metadata causes provider failure rather than accepting an ambiguous body.

#### Valid Body Fails Gateway Semantic Validation

Condition:

- Adapter returns a non-empty valid `URLFetchCandidate`, but `cheap_check()` fails or LLM judge returns `ok=false`.

Handling:

- This is not an adapter error.
- Preserve `FetchScheduler` semantic-failure behavior and provider fallback.
- Adapter must not retry with different vendor options, rendering modes, proxies, or output formats in this iteration.

#### All Fetch Providers Fail

Handling:

- Preserve current scheduler/orchestrator behavior and accumulated `ExecutionFailure` information.
- Do not add a special aggregate error for scraping vendors.

---

### 6. Provider-Specific Error Boundaries

The provider-specific rules below define only where remote response errors are detected. They do not add new exception types.

| Provider | Search failure boundary | Fetch failure boundary |
|---|---|---|
| Bright Data | Structured SERP response missing expected successful organic result envelope or provider reports operation failure | `/request` HTTP failure or returned page/Markdown text empty |
| Scrape.do | Google plugin JSON malformed/missing expected organic result collection or reports failure | Scrape HTTP failure or Markdown text empty |
| ZenRows | Not search-capable | Fetch HTTP failure or Markdown/page body empty |
| Decodo | Google Search template response malformed, parsing envelope absent/invalid, or provider reports scrape failure | Scrape response lacks usable Markdown/body or reports provider failure |
| ScrapingDog | Google API JSON malformed/missing `organic_results` success shape | Scrape HTTP failure or page text empty |
| ScrapeGraphAI | v2 Search response malformed or provider success/error envelope indicates failure | v2 Scrape response malformed, provider reports failure, or selected Markdown/body field empty |
| ScraperAPI | Structured Google JSON malformed/missing organic results success shape | Synchronous scrape HTTP failure or response body empty |
| ScrapingAnt | Not search-capable | v2 Markdown JSON malformed, provider reports failure, or `markdown` missing/empty |
| SerpApi | Google Search JSON malformed/missing successful organic result shape or contains provider error instead of result data | Not fetch-capable |

Implementation rule:

- Prefer shared parsing helpers when response shapes fit them.
- Provider-specific parsing helpers may be local to one adapter when needed.
- Do not build a shared vendor-error abstraction merely because several providers have an `error`/`success` field; their envelopes and semantics are not stable enough to justify a core abstraction.

---

### 7. Authentication and Sensitive-Data Failures

#### Invalid Credential

Condition:

- Bearer, Basic, header, or query credential is rejected by the provider.

Handling:

- Usually arrives as HTTP `401`/`403` or another non-retryable 4xx and follows existing HTTP `ExecutionFailure` behavior.
- Do not distinguish invalid key from insufficient account/product entitlement in public gateway errors unless the provider's status code semantics are both stable and already represented by an existing gateway code; no such new distinction is introduced here.

#### Query-String Credentials

Applies to provider APIs whose minimal documented request places the credential in the URL query string.

Handling:

- Continue using `http_endpoint_for_log(url)` before emitting transport events.
- Logged endpoint must omit query and fragment.
- Adapter/provider error messages must identify only provider + stage + concise reason; they must not include the full request URL.

#### Header Credentials

Handling:

- Reveal `SecretValue` only at request construction.
- Never copy request headers into new logs/errors.

#### Decodo Basic Token

Handling:

- Treat configured secret as opaque token material for `Authorization: Basic <secret>`.
- Do not decode, split, validate, or log token contents.
- Authentication rejection follows normal HTTP 4xx handling.

---

### 8. Exception-Boundary Rules

#### Adapter Constructor

```text
provider adapter __init__
  -> validate only provider-specific required configuration
  -> invalid value => TypeError
Runtime._build_web_providers
  -> catches TypeError
  -> ConfigFailure(CONFIG_ERROR, "Invalid configuration for web provider <name>")
```

No provider constructor exception hierarchy is added.

#### JSON Search Adapter

```text
adapter.search
  -> request_json
       transport/status failure => ExecutionFailure
       invalid JSON => ProtocolFailure
  -> validate top-level provider envelope
       malformed => ExecutionFailure/ProtocolFailure by existing convention
  -> for each result
       isolated parse ExecutionFailure => skip result
  -> return valid hits
```

#### JSON Fetch Adapter

```text
adapter.fetch
  -> request_json
  -> validate provider success envelope/body field
  -> missing/empty body => ExecutionFailure
  -> return URLFetchCandidate
```

#### Text Fetch Adapter

```text
adapter.fetch
  -> request_text
       transport/status failure => ExecutionFailure
       success => str
  -> if not text.strip(): ExecutionFailure
  -> return URLFetchCandidate
```

#### Orchestrator/Scheduler

- Search catches provider execution failures at the existing pipeline boundary and allows other provider pipelines to finish.
- Fetch scheduler catches provider execution failures and attempts another provider according to current capacity/order semantics.
- Unexpected adapter exceptions remain converted by existing orchestrator/scheduler invalid-provider-data boundaries rather than introducing vendor exception wrapping.

---

### 9. Logging Rules

No new provider-specific log event family is introduced.

`request_text()` uses the same events as `request_json()`:

- `http_attempt_started`
- `http_attempt_completed`
- `http_retrying`
- `http_failed`

Existing orchestration events remain unchanged:

- provider started/completed/failed;
- candidate accepted/rejected;
- body accepted/rejected/skipped;
- scheduler provider selection/fallback.

Never add these fields to transport/provider logs in this feature:

- API-key/token/header value;
- query-string parameter values;
- full request URL when it contains query parameters;
- search query text;
- target page request body if it contains user data;
- fetched HTML/Markdown;
- full raw provider error object.

Safe operational metadata remains provider name, stage, sanitized endpoint, status code, retry attempt, elapsed time, counts, and existing fixed reason categories.

---

### 10. Failure Outcome Matrix

| Failure | Search provider outcome | Fetch provider outcome | Public/core change? |
|---|---|---|---|
| Invalid enabled config | startup `CONFIG_ERROR` | startup `CONFIG_ERROR` | no |
| Unsupported capability requested | startup `CONFIG_ERROR` | startup `CONFIG_ERROR` | no |
| Missing secret env | startup `CONFIG_ERROR` | startup `CONFIG_ERROR` | no |
| HTTP 408/429/5xx exhausted | provider pipeline failure | provider execution failure + fallback | no |
| HTTP timeout/transport exhausted | provider pipeline failure | provider execution failure + fallback | no |
| HTTP non-retryable 4xx | provider pipeline failure | provider execution failure + fallback | no |
| Invalid JSON | provider `ProtocolFailure` | provider `ProtocolFailure` + fallback boundary | no |
| Malformed top-level provider JSON | provider pipeline failure | provider execution failure + fallback | no |
| Malformed individual search result | skip entry | n/a | no |
| Valid zero search results | successful empty provider pipeline | n/a | no |
| Successful text HTTP with empty body | n/a | provider execution failure + fallback | only new text transport method |
| Non-empty body rejected by cheap check/judge | n/a | semantic failure + fallback | no |
| Cancellation | propagate | propagate | no |

The feature therefore preserves the existing external behavior: one bad provider does not fail a multi-provider search when another provider completes, and one bad fetch provider does not prevent fallback to another configured fetch provider.
