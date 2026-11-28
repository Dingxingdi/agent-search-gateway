# Error Handling: LangSearch Web Search Provider

- Date: 2026-11-28
- Status: Draft for review
- Base: v3.1.0 / 5f70c12
- Architecture: ../architectures/20261128-langsearch.md
- Testing: ../testings/20261128-langsearch.md
- Official source: https://docs.langsearch.com/api/errors

## 1. Ownership and invariants

LangSearchAdapter owns request construction, business status/envelope validation, candidate mapping and best-effort diagnostic extraction. Use the existing JsonRequester, common failure/validation helpers, CandidateCounts and safe diagnostic conventions. Transport/retry belong to the HTTP executor; credential selection/fallback and concurrency belong to the pool; aggregation, URL validity and admission belong to the orchestrator.

No new public error codes, exception classes, retry policies, cooldown, account tracking, implicit Fetch or core changes. Preserve cancellation behavior.

## 2. Configuration and admission

| Condition | Existing behavior to preserve |
|---|---|
| Provider absent or disabled | No LangSearch search dispatch |
| Missing required credential for enabled provider | Existing configuration/credential failure |
| Fetch explicitly enabled | Capability validation rejects unsupported Fetch |
| Invalid endpoint, strategy or rate limit | Existing validation |
| Explicit selection of unavailable provider | Existing provider selection behavior |
| All members unavailable or wait budget exhausted | Existing member admission/wait failure |
| Cancellation | Propagate under existing policy; do not convert to retry/fallback |

Doctor remains an existing configuration check; no LangSearch online probe is introduced.

## 3. Transport, retry and fallback

HTTP status is the primary success boundary. Reuse the HTTP executor rather than making requests or implementing retries directly.

| HTTP/transport outcome | Policy |
|---|---|
| 400 | Invalid request; existing non-retryable status policy |
| 401 | Invalid/inactive credential; non-retryable at HTTP-attempt level |
| 403 | Access denied; non-retryable at HTTP-attempt level |
| 408 | Existing bounded retry |
| 429 | Existing bounded exponential retry including Retry-After handling when applicable |
| 500, 502, 503, 504 and other 5xx | Existing bounded retry |
| Retryable network errors/timeouts | Existing transport retry classification |
| Non-retryable transport/HTTP failures | Existing ExecutionFailure |
| Invalid JSON | Existing executor decode failure policy |

All physical attempts, including retries, consume the existing selected member's rate permits. Preserve configured retry policy and timeouts; do not add special jitter, another retry loop or supplier-specific exceptions. On terminal member execution failure, the logical credential wrapper may try another unattempted eligible member according to its normal policy. Successful zero/partial results do not trigger fallback.

LangSearch may return 429 for request/token rate or daily token exhaustion. Keys and MCP calls associated with one account share allowances. A different key may therefore fail identically. The local example of 5 RPS per credential does not enforce account-wide RPS/TPM/TPD. Do not assume short backoff always restores allowance. Operators can check upstream usage and reset time and configure suitable limits.

## 4. Business status and envelope

Use the documented integer code 200 success contract, not a boolean, string or absent code. Error codes can be strings or integers; error messages can occur in message or msg. A successful HTTP status carrying an explicit failed business status must not be mistaken for a valid empty result.

| Payload condition | Result |
|---|---|
| Non-object JSON root | Existing failure helper / ExecutionFailure |
| Missing, wrong-type or non-200 code | ExecutionFailure with safe diagnostics where available |
| Missing/non-object data or webPages | ExecutionFailure |
| Missing/null/non-list data.webPages.value | ExecutionFailure |
| Valid empty result list | Successful [] |
| Valid list with some/all invalid records | Return surviving list (possibly empty); report candidate counts |
| Extra metadata or unexpected text/full body | Ignore; no body cache or cleaning triggered by this adapter |

Response diagnostics are non-authoritative and best effort. Distinguish decode, invalid_envelope, provider_response and valid success using the existing classification vocabulary. Extract only known code and message/msg scalars via the shared safe diagnostics path; use a valid string message first, otherwise a valid string msg. Preserve truncation and redaction. Do not add log_id, usage, arbitrary nested payloads, raw headers or unbounded text as custom diagnostic fields. Use the existing failure helper for semantic errors. Semantic validation follows executor JSON parsing; do not introduce a separate retry loop.

## 5. Candidate handling

The response envelope is strict; individual records follow the current tolerant Search convention.

- Reject a non-object record, missing/blank URL, URL of wrong type or optional metadata of wrong non-null type. Use require_object, non_empty_string, optional_string, candidate_rejection_reason and CandidateCounts.
- Missing/null name or snippet maps to an empty string. Do not fabricate metadata; existing staging rules continue to decide admission.
- Preserve accepted record order; existing orchestration owns URL normalization, validity, deduplication and staging.
- Ignore text and other body fields. KeywordSearchHit.raw_content and content remain empty.
- Emit the existing provider_candidates_summary once per parsed result array, using existing counts and rejection reasons.

## 6. Request-level outcomes and observability

Keep existing provider selection, partial success, all-provider failure, output writing and URL admission semantics. The adapter cannot participate in explicit Fetch. A valid empty result is a successful adapter response and is handed to the orchestrator unchanged.

Use existing request/response preview limits, retry events, terminal failure logging, provider and member attribution and secret redaction. Diagnostics must not cause a valid search to fail. Do not emit credentials in fixtures, exceptions, diagnostic fields or public results.

## 7. Acceptance

Error handling requires no production changes beyond the new adapter and additive registry entry. Verify the cases in the companion testing spec. Core error behavior must remain unchanged.
