## Error Handling: Gateway Maintenance and Bright Data Header-Aware Retry

### 1. Principles

- Preserve existing `ErrorCode` values, CLI aggregate error messages, search orchestration, concurrency quotas, transport retries, and provider isolation.
- Bright Data Search alone interprets `x-brd-error-code`, `x-brd-error`, and `x-brd-status-code` **before** JSON decoding, even with outer HTTP 200.
- Use the **same** `retry_async` invocation and `RetryPolicy.max_attempts` for vendor-header, HTTP-status, and transport failures. Never add a nested adapter retry loop.
- Use a per-search optional response inspector and optional jitter. The default shared executor behavior and Bright Data Web Unlocker Fetch remain unchanged.
- Log specific failure classifications only as safe DEBUG metadata, while the CLI keeps the preexisting aggregate summary.

---

### 2. Response Classification and Precedence

| Response | Classification | Handling |
|---|---|---|
| HTTP 200; `x-brd-error-code: captcha`; `x-brd-status-code: 502`; empty body | Recoverable vendor failure | Retry; do **not** decode body |
| Code `timeout`, `expect_element`, or `expect_body` | Recoverable vendor failure | Retry in same budget |
| Nonempty unknown error code or `x-brd-error` without documented recoverable code | Terminal vendor failure | No vendor-specific retry; safe diagnostic |
| Outer HTTP 200; no code/explanation; valid inner `x-brd-status-code >= 400` | Terminal vendor failure | No vendor-specific retry without known code |
| Outer and inner HTTP statuses are both retryable; no code/explanation or malformed metadata | Existing HTTP retry status | Defer to the shared HTTP-status predicate, budget, and unjittered backoff |
| Header with only inner 200, no error, valid JSON | Success | Parse existing `organic` |
| No provider error; body empty/invalid JSON | Existing decode failure | Preserve `ProtocolFailure`; do not guess captcha |
| No provider error; HTTP 408, 429, or >=500 | Existing HTTP retry status | Preserve current retries and logging |
| HTTP 401/403 or explicit nonrecoverable authorization failure | Terminal | No retry |
| Malformed or ambiguous header values | Untrusted metadata | Do not retry on malformed or unknown code |

Priority:

1. After sending the request, classify any **explicit** Bright Data provider failure inside the shared HTTP attempt, before `response.json()` and before outer 2xx success can be accepted.
2. If no explicit vendor failure exists, retain existing generic HTTP status / transport classification.
3. Decode JSON and validate `organic` only for normal successful responses.

Only these exact error codes are vendor-retryable: `captcha`, `timeout`, `expect_element`, `expect_body`. A valid non-2xx provider-reported status without a documented code does not gain vendor-specific retry eligibility. If both outer and inner statuses meet the shared ordinary HTTP retry policy and no explicit error or malformed metadata is present, inspection defers to that existing HTTP retry path. Masked successful outer responses and terminal inner statuses remain terminal. Unknown or invalid codes never silently become retryable.

A small typed `ExecutionFailure`-compatible internal signal can contain safe provider code, optional inner status, outer status, sanitized explanation, and a retryability flag; use existing `ErrorCode.ALL_PROVIDERS_FAILED` rather than introducing a new public error code or socket payload.

---

### 3. Retry Budget, Backoff, and Jitter

#### Total attempts and coexistence with legacy failures

- `RetryPolicy.max_attempts` counts the entire request's HTTP sends, not “retries after the first”. The configured fallback is 3.
- `base_delay_seconds`, `max_delay_seconds`, `request_timeout_seconds`, and the existing HTTP retry eligibility remain in force.
- Mixed failures such as `captcha → HTTP 500 → captcha` still use **at most 3** sends with the default policy.
- When the last attempt fails, log its safe vendor classification and propagate the typed internal failure to the existing orchestrator, which may aggregate it into the generic user-facing message.
- A 200/`captcha`/empty-body response must never proceed to JSON decode.

#### Opt-in jitter

- Add an optional (default identity) delay transformation in the existing `retry_async` scheduling path.
- Only when the caught failure is one of the four retryable Bright Data header classifications should the capped exponential delay be adjusted by bounded randomness (e.g. factor `uniform(0.8, 1.0)`). Normal HTTP-status/transport retries and unrelated providers retain their exact previous delays.
- Cap actual delay at `max_delay_seconds`; it must remain finite and nonnegative.
- The `on_retry` callback must receive the **actual** delay later used by `sleep`, not an unjittered estimate.
- The random source must be injectible/fakeable in tests. Do not add a new global configuration parameter or mutate `RetryPolicy`.

#### Cancellation and failures outside the allowlist

- `asyncio.CancelledError` exits immediately with no additional retry.
- Non-recoverable authorization, permission, invalid zone, malformed configuration, and unknown provider errors are terminal.
- Successful HTTP responses with bad `organic` structure remain adapter parsing failures; do not retry every JSON/semantic error.
- The existing web-provider quota lease continues to cover all attempts; every HTTP send continues to use the existing per-attempt timeout.

---

### 4. User-Facing Behavior and Diagnostics

#### Internal terminal example

```text
provider=brightdata
stage=search
attempt=3
http_status=200
brightdata_status=502
brightdata_error_code=captcha
reason="redirect location was rejected"
result=terminal (retry budget exhausted)
```

This is an internal diagnosis, not a new public CLI output format.

#### Preserve aggregate result and error behavior

`SearchOrchestrator.keyword_search()` already gathers provider results and returns a generic `ALL_PROVIDERS_FAILED` error if all selected pipelines fail, including a single `--provider brightdata`. Keep that behavior; no edits to orchestrator, daemon, or socket contract.

If another provider succeeds, preserve its results. Bright Data failure diagnostics remain visible in DEBUG events without turning a partial search success into a failure.

---

### 5. Safe Structured Logging

Reuse the existing `http_attempt_started`, `http_attempt_completed`, `http_retrying`, and `http_failed` events:

- An attempt event records provider, stage, attempt, and sanitized endpoint.
- Completion records the outer HTTP status and timing; 200 at this layer may still be a provider-header failure.
- On a retryable Bright Data error, record `category=provider_header`, outer `status`, parsed `brightdata_status`, normalized allowlisted `brightdata_error_code`, attempt, and **actual** `delay_ms`.
- On terminal failure, record the same safe classification, last attempt, elapsed time, and a bounded safe reason. Existing summary `provider_failed` events need no changes.

Do **not** log raw response bodies, arbitrary `x-brd-error` header contents, full target URLs/query strings, authorization headers, API keys, or arbitrary exception text.

Header values are untrusted: bound field lengths, discard control characters, normalize simple classifications, and redact or drop URLs, token-like strings, credentials, and unsafe explanation fragments. If sanitization cannot establish a safe explanation, use a fixed marker such as `provider_reported_failure`. The existing centralized `SecretRedactor` is defense in depth, not the only safeguard.

DEBUG-only operational logs provide the specific reason; normal CLI search stdout/stderr remains unchanged.

---

### 6. Other Requested Changes and Error Compatibility

- **Top-level CLI version:** Both `--version` and `-v` print the canonical `__version__` once and exit 0 before config/daemon access. Existing subcommand validation remains unchanged when the flag is absent.
- **Example configuration:** Remove only the four requested provider example blocks, leaving their registry support intact. Set template `request_timeout_seconds=180.0`; leave the code's default of 30.0 unchanged and do not mutate existing user files.
- **dblp README note:** Characterize bot verification even at low API rates as an observed limitation and recommend optionally setting `[academic_providers.dblp] enabled = false`. No automatic verification bypass or new dblp retry behavior.
- **Bright Data request:** Keep `search_zone`, `fetch_zone`, bearer auth, endpoint, `format="raw"`, Google `brd_json=1`, and Fetch `data_format="markdown"` exactly as before.

---

### 7. Acceptance Criteria

1. HTTP 200 + empty body + `captcha` header is classified as a Bright Data header failure, not a JSON decoding failure.
2. Four recoverable codes retry and respect `max_attempts`; other codes, auth and config errors do not gain vendor retries.
3. An exhaustion event preserves safe error code/status/reason and records the final attempt.
4. Jitter is bounded, testable, used only on recoverable Bright Data header failures, and its actual wait matches `delay_ms` in logs.
5. Standard HTTP retry/timeout/cancellation behavior and every other provider's delays remain unchanged.
6. Web Unlocker Fetch request and successful text response remain identical to the current implementation.
7. No credentials, sensitive URLs, arbitrary provider headers, or raw bodies leak into logs.
8. CLI and orchestrator keep existing result and error contracts.
