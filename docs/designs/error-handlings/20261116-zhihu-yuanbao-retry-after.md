## Error Handling: Retry-After, Zhihu and Yuanbao Search

**Status:** Draft for user review; no implementation  
**Date:** 2026-11-16  
**Base:** eeeff82 (v0.2.2)  
**Companion specs:** [Architecture](../architectures/20261116-zhihu-yuanbao-retry-after.md) | [Testing](../testings/20261116-zhihu-yuanbao-retry-after.md)

### 1. Intent and ownership

Extend the existing HTTP retry policy and add two Web adapters without changing public errors, CLI behavior or logging infrastructure. Preserve the project's attempt / adapter / provider-pipeline boundaries:

1. **HTTP attempt:** `HttpJsonExecutor` owns transport, HTTP status, retry scheduling, and safe response metadata.
2. **Adapter result:** Zhihu/Yuanbao own provider-declared errors, envelope validation and per-item rejection counts.
3. **Provider pipeline:** `SearchOrchestrator` owns one terminal `provider_completed` or `provider_failed` per logical provider, regardless of internal retries. A partial Zhihu success is still a completed provider pipeline.

Do not create custom HTTP retries in the adapters or convert one invalid search result into a failure of its valid siblings. No modification to `ProviderRegistry`, `SearchOrchestrator`, Bright Data code or academic adapters.

### 2. HTTP Retry-After behavior

Current behavior: HTTP 408, 429, and 5xx and transport/timeouts are retryable; `retry_async` computes capped exponential backoff. A response status is classified before JSON/XML decoding. Both Semantic Scholar and arXiv already use the shared HTTP executor.

| Case | Delay | Attempts | Outcome and log |
| --- | --- | --- | --- |
| Retryable HTTP code + valid integer `Retry-After` | Server's nonnegative seconds | Existing `max_attempts` | `http_retrying` with actual delay |
| Retryable HTTP code + valid HTTP-date `Retry-After` | Nonnegative time until that date | Existing `max_attempts` | Same retry event |
| Retryable status + absent/invalid/negative header | Existing capped exponential delay | Existing `max_attempts` | Existing event and delay |
| Header exceeds `max_delay_seconds` | Server wait, not clipped | Existing `max_attempts` | Actual wait visible in `delay_ms` |
| HTTP 401/403/other nonretryable status | No retry | N/A | `http_failed` with status |
| Retry attempts exhausted | No extra sleep | N/A | One HTTP-terminal `http_failed` and existing pipeline terminal failure |
| HTTP retry eventually succeeds | No terminal failure | N/A | `http_retrying` for failed attempts, then completion |
| Response decode/semantic parsing fails after HTTP 200 | No new retry | N/A | Existing `ProtocolFailure` / adapter failure |
| Cancellation | Immediate propagation | N/A | No new retry or swallowed cancellation |

- Parse only standard delta-seconds and HTTP-date, never execute or log raw header text.
- Invalid header values are best-effort fallbacks, not new exceptions. A date in the past translates to zero delay; no arbitrary hard upper bound is added.
- Preserve `retry_async` initial `min(base * 2^(attempt-1), max_delay)` calculation and unchanged provider-header jitter. The optional delay adjuster becomes authoritative for valid server waits, so its return value is not capped again. Non-finite adjustments still fall back safely.
- Extend `_RetryableStatus` with parsed duration metadata only, not raw HTTP response or response headers. Existing `_adjust_retry_delay` handles status and provider-header cases.
- Keep existing `http_retrying` event name/level and `delay_ms`; add a bounded, static `delay_source` (e.g. `retry_after` or `backoff`) if needed to disambiguate. Do not create new retry event types.
- Do not interpret Zhihu/Yuanbao business error codes embedded in HTTP 200 as HTTP 429. Those errors remain adapter-level business failures under current retry granularity.

### 3. Zhihu: two independent API branches

The adapter calls `zhihu_search` and `global_search`. Each branch invokes the shared HTTP executor and validates its own provider-level status/envelope. Both branches are awaited; a cancelled task or cancellation result must propagate, never count as a recoverable branch failure.

| Site search | Global search | Adapter result | Log ownership |
| --- | --- | --- | --- |
| Success with results | Success with results | Combine valid mapped hits | HTTP per-attempt; adapter candidate summaries; orchestrator `provider_completed` |
| Success with zero hits | Success with zero hits | Valid `[]` | Orchestrator `provider_completed` |
| Success (including empty) | Failure after retries / protocol failure | Return successful branch hits | Adapter `provider_partial_failure` once; orchestrator `provider_completed` |
| Failure | Success (including empty) | Same, other branch | Adapter `provider_partial_failure` once; orchestrator `provider_completed` |
| Failure | Failure | Raise one existing typed failure, preserve a representative safe cause | HTTP per-branch failure; orchestrator `provider_failed` once |
| Success envelope containing bad individual items | Any other successful branch | Skip bad items, retain valid hits | `provider_candidates_summary` with reason counts |

- A successful HTTP response with a non-success provider `Code` is an upstream business error, even if `Data` exists or HTTP status is 200. Use existing `failure` and allowlisted `upstream_code` / `upstream_message` diagnostics, not arbitrary exception text.
- Validate the actual official search envelope and `Data.Items` schema; malformed/missing required envelope is a branch failure, not a successful empty result. Valid empty `Items=[]` means zero results.
- For individual hits, require a usable URL; absent URL is `missing_url` and other invalid shapes use existing known rejection codes. Map optional title/summary conservatively; no new global result schema.
- Use `CandidateCounts` and existing `provider_candidates_summary`. The branch identifier is a fixed value (`zhihu_search` / `global_search`), not untrusted payload text.
- With exactly one failed branch, emit one adapter-local `provider_partial_failure` containing fixed branch, safe exception type/reason and allowed upstream diagnostics. No second terminal `provider_failed`.
- For two failures, local HTTP `http_failed` events are allowed for each physical request; `SearchOrchestrator` still emits only one terminal provider failure. Avoid duplicating that terminal event inside the adapter.
- Do not retry entire `ZhihuAdapter.search` merely because one branch failed; each HTTP operation uses shared retry once.

### 4. Yuanbao: response envelope versus per-result data

Tencent's documented service-key mode uses `POST /SearchPro`. HTTP 200 can contain a top-level `Response.Error`; never treat that as a successful empty search.

| Condition | Handling | Diagnostics |
| --- | --- | --- |
| HTTP 408/429/5xx / timeout / transport error | Existing shared HTTP retry and terminal failures | Existing HTTP events |
| JSON decoding fails | Existing `ProtocolFailure` | `decode`, response status/type/size |
| `Response.Error` exists, with Code/Message | Raise existing typed adapter failure | Bounded `upstream_code` and `upstream_message` |
| Missing/wrong-type `Response` or `Pages` | Fail whole provider with existing protocol/adapter error | `invalid_envelope`, no raw payload |
| Valid `Response.Pages=[]` | Return an empty list successfully | Candidate count zero |
| One Pages entry not JSON / not an object | Reject only this entry | `decode_error` or `invalid_record_shape` |
| One Pages entry without valid URL | Reject only this entry | `missing_url` / existing reason |
| Valid item with only `passage` | Use `passage` as snippet | `accepted` |
| Valid item with `content` | Prefer nonempty `content` for snippet | `accepted` |

- Inspect known vendor business error fields before checking `Pages`. Do not log full Tencent response, query contents, complete page JSON, or unaudited `RequestId` data.
- Per-result decode is a second JSON operation after the common response decode. Its errors are item-level, not automatic retries.
- Existing `provider_candidates_summary` reports total received, accepted, rejected and allowlisted rejection counts. Keep all `raw_content`/`content` body fields empty to preserve later Fetch eligibility.
- Leave business-error retry policy unchanged; Tencent codes such as `RequestLimitExceeded` are recorded as explicit upstream errors, not silently conflated with transport-level HTTP 429.

### 5. Diagnostics, safety and failure timing

| Event | Owner and meaning | Minimum fields |
| --- | --- | --- |
| `http_attempt_started` / `http_attempt_completed` | Executor; one physical attempt | provider, stage, attempt, sanitized endpoint, status when available, elapsed |
| `provider_response_diagnostic` | Executor + supplied adapter extractor; each received response | status, response_bytes, content_type, safe category/upstream error when known |
| `http_retrying` | Executor; one retry scheduled | provider, stage, attempt, actual delay_ms, category, optional delay source |
| `http_failed` | Executor; transport/status/decoder terminal failure | provider, stage, attempt, category, safe metadata |
| `provider_candidates_summary` | Adapter; successful candidate processing | provider, stage, phase=adapter, received, accepted, rejected, rejection_counts |
| `provider_partial_failure` | Zhihu adapter; one API branch failed, one succeeded | provider, stage, fixed branch, error type, safe reason/diagnostics |
| `provider_completed` / `provider_failed` | Existing orchestrator; logical provider terminal outcome | provider, stage, result counts / sanitized cause |

Safety invariants:
- Never emit credentials, Authorization headers, raw `Retry-After` strings, query contents, raw URLs with tokens, vendor response bodies or arbitrary exception representations.
- Reuse `safe_diagnostic_fields`, `best_effort_diagnostics`, `selected_error_fields` and active `SecretRedactor`. Known upstream scalar messages are bounded and sanitized, not written as raw JSON.
- Do not make diagnostic extraction capable of changing a successful response into a failure.
- Count and classify malformed items, do not print them. All failure/event fields must be stable under malformed/untrusted vendor data.

### 6. Bright Data and documentation

The user observed occasional HTTP 200 with an unparseable response but cannot reliably reproduce or inspect the payload. A prior, different HTTP-200/header-reported upstream failure was addressed already. This release **does not** infer that the present incident has the same cause or assert confirmed upstream fault.

README known-limitation note: occasional HTTP-200 responses may still fail JSON parsing; cause not established; users may retry or switch providers. No Bright Data code change or new parse retry.

### 7. Review / definition of done

- [ ] HTTP Retry-After controls delay only for existing retryable status responses; normal backoff and attempt limits remain unchanged.
- [ ] Valid header wait can exceed local backoff cap; invalid header does not throw.
- [ ] No duplicate provider-terminal events after retry success/failure.
- [ ] Zhihu partial success returns usable hits and visibly logs which branch failed.
- [ ] Yuanbao HTTP-200 `Response.Error` is always treated as error; individual bad pages do not discard siblings.
- [ ] Logged vendor diagnostics are bounded, redacted and useful; neither adapter stores search summaries as fetched page bodies.
- [ ] No changes to existing Bright Data, Academic adapters or orchestrators.

This specification requires user review before any implementation or commit.
