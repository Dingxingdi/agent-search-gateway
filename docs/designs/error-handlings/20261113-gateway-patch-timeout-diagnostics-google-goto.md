## Error Handling: Gateway Patch — Provider Diagnostics, Retry Semantics, Agent Timeout, Google Goto

**Status:** Draft for user review (not implemented)
**Date:** 2026-11-13
**Base:** d0ff1bc / v0.2.1
**Companion specs:** [Architecture](../architectures/20261113-gateway-patch-timeout-diagnostics-google-goto.md) and [Testing](../testings/20261113-gateway-patch-timeout-diagnostics-google-goto.md).

### 1. Intent and boundaries

The current gateway records its own execution exceptions and selected transport metadata, but a real upstream failure may be flattened to a generic gateway message. Successful upstream calls also lack a consistent safe diagnostic summary. Certain malformed provider candidates are silently skipped. This patch makes the existing DEBUG diagnostic log useful for both success and failure without recording full webpages, raw model answers, prompts, or HTTP bodies.

This is an **observability/diagnosis patch**, not a change to public exception taxonomy, provider ordering, retry counts, result schema, or fallback policy. Distinguish three scopes:
1. **Attempt:** One network or model/CLI exchange; may retry.
2. **Provider branch:** One provider's participation in an orchestrated request; may succeed, fail, or be bypassed/fallen back.
3. **Workflow:** The entire requested gateway command; may succeed despite a provider failure.

The same HTTP terminal attempt may produce an executor-level `http_failed` and one orchestrator-level `provider_failed`: these refer to different scopes, not two declarations that the whole workflow failed.

### 2. Error information currently available and gaps

| Source | Current relevant signals | Gap to close | Owner after patch |
| --- | --- | --- | --- |
| Shared HTTP JSON executor | Attempt start, HTTP code, retry/final HTTP failure and JSON decode failure | Safe response size/type, structured upstream error details and consistent success/failure diagnostics | Executor for transport; adapter for business payload |
| Bright Data SERP | `x-brd-status-code`, `x-brd-error-code`, `x-brd-error`; response inspector already recognizes HTTP 200 with upstream error | Expose the existing bounded, safe header diagnostics uniformly at attempt/provider scope | Existing Bright Data inspector + shared logger |
| Other Web SERP providers | Parsed hits, some provider-specific status/error dictionaries | JSON `error`/message frequently flattened; malformed hits skipped via `except ExecutionFailure: continue` | Provider adapter using common diagnostic helper |
| Academic providers and OA | Candidate-rejection and enrichment-error events in some adapters | Partial counts/reasons and provider response diagnostics not consistently summarized | Academic adapters / OA wrapper + shared helper |
| LLM OpenAI-chat backend | HTTP failures, malformed response, protocol retry | Protocol retry can be misleadingly named `provider_failed`; safe terminal API error code/message missing | Existing LLM client + wrapper |
| Antigravity Agent | Process exit, timeout, stdout decode status, terminal status, captured stderr bytes | A terminal JSON error may be reduced to local category; raw stderr intentionally not logged | CLI runner + Agent adapter |
| URL Fetch backends | Transport/parsing failure, sequential fallback, judge failure | Safe upstream failure metadata and accepted/rejected content diagnostics vary by adapter | Fetch adapter and fetch orchestrator |
| Orchestrators | `provider_failed`, `provider_completed`, stage events, `workflow_failed` | Terminal outcomes are not consistently distinguishable from recoverable attempts | Orchestrator, once per provider branch |

### 3. Canonical event policy

Do not introduce a second event bus or logging backend. Reuse `log_event`, existing request-id context, configured rotating DEBUG file and `SecretRedactor`. Retain existing event names where their meaning is correct; add bounded fields and new structured events only where necessary.

| Event | Scope and timing | Level | Minimum relevant fields |
| --- | --- | --- | --- |
| `http_attempt_started` | Before each HTTP request | DEBUG | provider, stage, attempt |
| `http_attempt_completed` | After an HTTP response, including non-2xx and 200 with semantic error | DEBUG | provider, stage, attempt, http_status, response_bytes if available, content_type if available |
| `http_retrying` | Failed HTTP attempt, another attempt will be scheduled | DEBUG/WARNING according to existing policy | provider, stage, attempt, category, http_status, retry_delay if known |
| `http_failed` | Executor gives up / cannot decode response / transport fails without retry | WARNING/DEBUG according to existing policy | provider, stage, attempt, category, http_status if available |
| `provider_response_diagnostic` | After safely inspecting upstream payload/header or CLI terminal object, on **success or failure** | DEBUG | provider, stage, upstream_status/code/message if known, response type/size, semantic result |
| `provider_retrying` | Recoverable provider protocol failure outside the HTTP executor, with next attempt scheduled | DEBUG/WARNING | provider, stage, attempt, reason category, next attempt |
| `provider_candidates_summary` | After a candidate-producing response has been processed, even if zero accepted | DEBUG | provider, stage, received, accepted, rejected, rejection_counts |
| `provider_completed` | Provider branch reaches successful terminal outcome; **once** | Existing level | provider, stage, result_count if known |
| `provider_failed` | Provider branch exhausts alternatives/retries or definitively fails; **once** | Existing level | provider, stage, canonical failure category and available safe upstream code |
| `provider_fallback` | Recoverable provider failure leads to next configured fetch/resolver option | DEBUG | provider, stage, fallback category, next provider if known |
| `workflow_failed` | Request as a whole fails and daemon returns its existing error payload | Existing level | request-id, command, existing public error category |

One provider call can contain multiple `http_retrying` entries, one `provider_response_diagnostic` for each actual received response, and only one terminal `provider_completed` or `provider_failed` for that provider branch. Successful fallback branches do **not** turn an earlier failed attempt into a workflow failure.

**Example: three attempts**
- Tavily HTTP 429: log status and safe upstream code; `http_retrying(attempt=1)`. No provider terminal failure yet.
- Tavily HTTP 502: same for attempt 2.
- Tavily HTTP 200 valid: log response diagnostic and `provider_completed` once. No `provider_failed`.
- If attempt 3 instead fails: executor logs terminal `http_failed`, and orchestrator records one `provider_failed` for the Tavily branch.

### 4. Structured diagnostics and data minimization

Collect facts available **at the boundary where they are known**, without introspection of arbitrary exception `str(e)` or unconditional serialization of responses:

- Transport: elapsed milliseconds when available, status, safe `content-type`, response-byte count, retry index/delay, transport exception **type/category**, not its arbitrary message.
- API/business payload: allowlisted upstream code, status and short descriptive upstream message from known provider error fields; optional parsed structure kind and approximate hit count. For Bright Data preserve known `x-brd-*` header fields; do not dump all headers.
- CLI/model response: process exit code, timeout vs cancellation vs spawn error, terminal status, stdout/stderr byte lengths when available, parse/validation category, optional short allowlisted API error metadata; never raw model output or raw stderr.
- Candidate processing: number received/accepted/rejected and small dictionary of stable rejection categories such as `missing_url`, `invalid_url`, `missing_required_field`, `invalid_record`, `unsupported_scheme`, `recognized_invalid_redirect`, `decode_error`. Avoid per-item raw values.
- Sanitize strings before `log_event`: whitelist supported keys, strip control characters, apply existing secret redaction, cap upstream code at 128 characters, message at 512, error category at a small fixed width. Preserve total original length and a `truncated` boolean for long **safe messages**, not raw payload text. Logging must be best effort; diagnostics parser/logging failure cannot change a provider's business result.
- Exclude credentials, auth headers, cookies, full request/response bodies, signed URL query strings, full search queries when sensitive, page text, full JSON/XML payloads, prompts, complete LLM/Agent responses, stdout/stderr and tracebacks with arbitrary external content. If an upstream message cannot be classified or safely sanitized, emit code/type/count only.
- Successful responses record **metadata and counts**, not document text. There is no new archive, database, full-response artifact, CLI output field, or user-configured log dump flag.

A `status=200` HTTP response can still carry `upstream_status=502`, `upstream_code=captcha`, `semantic_result=error`. It must remain recognizable as an upstream error, rather than incorrectly becoming “invalid JSON.”

### 5. Workflow-specific failure points

#### 5.1 Keyword search (Web providers)

```text
for each enabled provider in keyword-search pipeline:
    try adapter.search(query):
        executor logs each HTTP attempt/received response
        adapter safely extracts known provider JSON/header error fields
        if business payload signals error:
            raise existing appropriate ExecutionFailure with safe diagnostics
        for candidate in provider response:
            if candidate cannot form a valid SearchHit:
                accumulate rejection category, continue (preserve current policy)
            else:
                accept candidate
        log provider_candidates_summary(received, accepted, rejected, reason counts)
        record provider_completed once
    except ExecutionFailure:
        record provider_failed once with safe category and upstream metadata
        preserve existing orchestration policy
for each accepted hit at shared _stage_keyword_hit:
    try unwrap_search_result_url(hit.url):
        if verified Google /goto with valid target: use target
        if recognized but invalid target:
            log candidate rejection category; skip this hit
        otherwise: preserve URL unchanged
    continue existing normalize_url -> URLStore -> dedupe pipeline
```

Neither a rejected hit nor one provider branch failure automatically fails the complete keyword workflow. Raw URLs are not logged.

#### 5.2 Paper search (academic sources and Open Access)

- HTTP/API failures: transport event + safe response diagnostic, followed by branch terminal failure or existing source fallback.
- XML/JSON malformed or absent required fields: log parser error category (not raw XML), preserve adapter's established strict/lenient behavior.
- Where `paper_candidate_rejected` already exists, retain it and add aggregate `provider_candidates_summary` rather than recording one unbounded event per malformed record.
- OA metadata/enrichment failures retain `paper_enrichment_failed` and continue/fail exactly as before.
- Unpaywall 404 means “no OA match” at the business level; keep that behavior and label its normal absence distinctly from a retryable upstream outage in higher-level logs. The underlying executor may still record HTTP 404; do not fabricate a provider failure.

#### 5.3 LLM search

- Each OpenAI-chat HTTP attempt is logged by the executor.
- A valid 200 HTTP response with an `error` object or unusable protocol response yields a safe `provider_response_diagnostic` with upstream code/message or parse category.
- When protocol retry is scheduled, use `provider_retrying` (not `provider_failed`); after retries exhaust, one provider terminal failure; if eventually successful, one `provider_completed`.
- Failed downstream answer parsing/judging keeps `llm_stage_failed` with a safe stage/category, no raw completion.

#### 5.4 Agent search

- No automatic Agent retry is added.
- Log spawn/timeout/cancel/nonzero exit/terminal JSON parse/terminal status with stage, safe failure code, duration and byte counts. Preserve user-configured positive finite `timeout_seconds` as the only intentional Agent runtime deadline; clean up subprocess on timeout/cancel.
- A terminal response such as `{"status":"ERROR","error":...}` should be inspected for allowlisted code/status/message and logged as safe diagnostics before producing the existing public failure category. Do not dump terminal JSON or stderr.
- Missing JSON, malformed JSON, success status without required result fields and premature process exit keep their current external behavior with improved categories/metadata.
- Agent CLI client waits for its matching daemon response without an additional fixed maximum. A daemon disconnect remains an error and is logged by existing workflow/transport machinery; no busy loop.

#### 5.5 URL fetch and content processing

- Each URL Fetch provider produces attempt metadata and safe semantic-content metadata (e.g. extraction success, recognized empty output, response bytes) when available.
- If a configured fetch provider fails, emit its branch `provider_failed` and existing fallback event/choice; after all candidates fail, preserve existing unavailable-result behavior.
- Semantic rejection/empty content should carry stable reason/counts. Judge/model failure retains established terminal-or-fallback logic; log its own safe stage/category.
- Never log fetched HTML, markdown body, extracted page text or an entire URL with token-bearing query.

### 6. Agent timeout error semantics

| Condition | Expected outcome | Diagnostic |
| --- | --- | --- |
| `timeout_seconds` finite positive (including 3600/7200) | Config accepted; Agent process supervised by that per-run deadline | Effective configured deadline, safe numeric value |
| `timeout_seconds <= 0`, NaN or infinity | Existing config validation error | Safe invalid-field category |
| Agent exceeds configured deadline | Existing timeout exception/failure, subprocess terminated and reaped | `provider_failed` once with `timeout` and configured value |
| Agent completes after 1140 or 1200 s but before its configured deadline | Success, provided daemon and socket remain alive | `provider_completed`, elapsed duration |
| Client disconnects / request is cancelled | Cancellation/connection failure and subprocess cleanup as applicable | Cancel/disconnect category, no raw payload |
| Non-Agent request exceeds its existing client timeout | Current non-Agent behavior unchanged | Existing transport timeout diagnostics |

No design here promises tolerance of arbitrary dead daemon/socket hangs: removal of a global Agent response deadline intentionally trades bounded client waiting for respecting the configured Agent runtime.

### 7. Google goto error semantics

Recognize only an absolute Google `/goto` URL with **exact** trusted hostname/path and exactly one unambiguous `url` parameter. Decode the query value using normal URL parsing and require a well-formed HTTP(S) destination for unwrapping. A structurally valid wrapper containing an opaque URL-safe `CAES...` token instead retains its original wrapper identity; it is not locally decoded or resolved. Support the verified `google.com` and `www.google.com` host spellings; reject spoofed or userinfo hosts.

| Input case | Result | Logging |
| --- | --- | --- |
| `https://www.google.com/goto?url=https%3A%2F%2Fexample.org%2Fa` | Use `https://example.org/a` before existing URL normalization/storage | Aggregate rewritten count in DEBUG |
| Well-formed Google wrapper with token matching `CAES[A-Za-z0-9_-]+={0,2}` | Preserve wrapper identity and candidate; do not infer its destination | Not a rewrite; never log the token |
| Already direct ordinary URL | Leave unchanged | No per-URL noise |
| Other redirect style (not observed) | Leave unchanged for future rule | No invented rejection |
| Matching `/goto` with missing, duplicate, empty, malformed or unsafe destination | Reject that single result | Increment `recognized_invalid_redirect` reason count |
| `google.com.evil.test/goto`, another path/host, other non-HTTP(S) target | Spoofed hosts unchanged then existing URL validity rules; for recognized Google /goto unsafe target reject | No full link or token dump |

Do not resolve redirects on the network, dereference arbitrary targets, change global `normalize_url`, or alter `url-fetch`. Another observed redirect format later requires only an isolated helper branch and table-driven tests.

### 8. Compatibility, rollout, and acceptance

- All public command names, protocol error shape, JSONL result shape, configured provider list, retry/fallback ordering, and current error categories remain compatible.
- Existing log events remain in place where semantically correct. New diagnostics stay DEBUG and are additive; event consumers should tolerate extra fields. Rename the misleading LLM recoverable `provider_failed` event to `provider_retrying` only while a retry is actually scheduled.
- Never guarantee external vendor error messages are trustworthy or complete. Empty bodies, network exceptions and failures before a response have `null`/absent fields, not invented metadata.
- For any provider success and failure, a developer can identify which upstream operation ran, its status/error code if exposed, its response size/type, how many candidates were accepted/rejected when applicable, whether retries/fallback happened, and the one terminal branch outcome.
- Verify no duplicate terminal provider outcomes, no previously silent malformed-item skip, and no sensitive/body content leakage. Tests and test doubles must not require live paid providers.

### 9. Open implementation notes for the planning phase

The smallest common helper/interface should reuse current structured events and existing provider diagnostics objects. Its exact module location and whether typed mapping or a tiny dataclass is best should be chosen while writing the implementation plan, after examining import dependencies. Avoid a heavyweight new logging subsystem or a provider-wide interface refactor in this patch.
