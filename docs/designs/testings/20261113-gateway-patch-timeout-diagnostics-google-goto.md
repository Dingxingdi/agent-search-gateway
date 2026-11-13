## Testing: Gateway Patch — Agent Timeout, Provider Diagnostics, Google Goto

**Status:** Draft for user review (not implemented)
**Date:** 2026-11-13
**Base:** d0ff1bc / v0.2.1
**Companion specs:** [Architecture](../architectures/20261113-gateway-patch-timeout-diagnostics-google-goto.md) and [Error handling](../error-handlings/20261113-gateway-patch-timeout-diagnostics-google-goto.md).

### 1. Quality goals and scope

Prove three outcomes without changing public behavior unexpectedly:
1. A finite positive Agent `timeout_seconds` has **no Gateway-imposed upper bound**. The configured deadline is effective, not overridden by old orchestration (1140 seconds) or Agent socket (1200 seconds) limits.
2. Every provider operation produces **safe, structured, bounded diagnostics** on success or failure, separating a retry attempt from one terminal provider outcome and exposing partial candidate rejection.
3. The **only verified** redirect format, `google.com/goto?url=...`, is unwrapped at the shared keyword-hit stage, with unsafe matches rejected and unknown forms passed through.

All default tests are deterministic, offline and quick. Use fake transports, processes, clocks, provider payload fixtures and log sinks. No paid calls or real credentials are required. Retry counts/backoff, fallback ordering, CLI/NDJSON response fields, JSONL results and error codes are regression contracts.

### 2. Test locations and ownership

| Area | Existing suite to extend or nearby focused file | Main responsibility |
| --- | --- | --- |
| Agent config | `tests/unit/test_config_agent.py` | Numeric boundary validation |
| Agent orchestration | `tests/orchestrators/test_agent_search_deadline.py` | No 1140 s outer deadline; cancellation |
| Agent runner | `tests/providers/agent/test_antigravity_lifecycle.py`; `test_antigravity_cli.py` | Configured timeout, subprocess cleanup, terminal JSON |
| Socket CLI | `tests/unit/test_socket_client.py` | No default 1200 s Agent cap, non-Agent 600 s, explicit overrides |
| HTTP execution | `tests/providers/test_http_executor.py` | Attempt/status/retry/final outcome metadata |
| Web providers | `tests/providers/web/test_brightdata.py` and representative other adapters | Safe upstream fields, malformed hit counts |
| Keyword pipeline | `tests/orchestrators/test_keyword_search_pipeline.py` | URL rewrite before normalize/dedup and partial failure |
| Academic / OA | Existing `tests/providers/academic/` and paper pipeline tests | Candidate failures, Unpaywall 404, XML/JSON |
| LLM | `tests/orchestrators/test_llm_search.py` and LLM client tests | Protocol retry classification and stage failures |
| Agent logs | `tests/observability/test_agent_event_logging.py` | Safe process/terminal diagnostics |
| Shared logging | `tests/observability/test_provider_event_logging.py`, `test_redaction.py` | Consistency, truncation, confidentiality |
| Acceptance | `tests/acceptance/test_agent_search_workflows.py`, `test_gateway_workflows.py`, `test_debug_and_doctor.py` | End-to-end result/schema compatibility |
| Docs | `tests/docs/test_documented_config.py` | No stale timeout ceiling |

Prefer extending focused existing tests. A small dedicated pure URL helper test file is allowed; a duplicate integration suite per Web adapter is unnecessary.

### 3. Agent timeout cases

| ID | Given | When | Expected |
| --- | --- | --- | --- |
| AT-01 | Timeout unset | Parse Agent config | Existing 300-second default retained |
| AT-02 | Timeout 540, 3600, 7200 separately | Parse config | Each accepted exactly as supplied |
| AT-03 | Timeout 0, -1, negative fractional | Parse config | Validation failure, process never started |
| AT-04 | NaN, positive/negative infinity, nonnumeric | Parse config | Invalid or non-finite rejected |
| AT-05 | Configured 7200, fake process reaches virtual 1141 | Orchestrator awaits | No old 1140-second workflow cancellation |
| AT-06 | Fake Agent daemon responds at virtual 1201 | Default client awaits | Response delivered; no hidden 1200-second socket timer |
| AT-07 | Configured 3600, fake process exceeds its deadline | Agent runner operates | Existing timeout failure; kill/reap; one provider terminal failure |
| AT-08 | Run completes after old maximum but before configured deadline | Agent stage returns | One successful result and one provider completed event |
| AT-09 | Request cancelled mid-run | Runner exits | Process/streams cleaned; cancellation categorized correctly |
| AT-10 | Caller explicitly supplies numeric socket timeout | Agent client invoked | Explicit override still honored |
| AT-11 | Non-Agent call exceeds existing 600-second client wait | Non-Agent request | Existing timeout behavior unchanged |
| AT-12 | Daemon disconnects with outstanding Agent request | Socket client awaits | Connection error, waiter released, no infinite busy loop |

Use injected/fake monotonic time, patched coroutine clocks or controlled futures. **Never sleep 1140 or 7200 real seconds.** Verify other resource limits and process cleanup remain in effect.

### 4. HTTP / API response diagnostics

| ID | Fixture | Expected structured evidence |
| --- | --- | --- |
| HT-01 | HTTP 200 valid JSON | Status, safe content type and byte count; provider response summary; final success |
| HT-02 | Other accepted 2xx from existing contract | Correct success path, no spurious failure |
| HT-03 | HTTP 429 → HTTP 502 → valid HTTP 200 | Two retry events, three response status summaries, zero terminal failed, one provider completed |
| HT-04 | 429/502 to exhaustion | One executor terminal failure and **one** provider terminal failed; no duplicate provider terminal events |
| HT-05 | Connect timeout/error before any response | Attempt + transport exception **category**, no fabricated status/response bytes |
| HT-06 | HTTP 200 invalid JSON | Decode category and size/type; no raw JSON/text |
| HT-07 | HTTP 200 valid JSON with provider-level `error` | Safe upstream error code/message retained; existing public exception semantics |
| HT-08 | Bright Data HTTP 200, `x-brd-status-code=502`, `x-brd-error-code=captcha`, empty body | Upstream 502/captcha represented, not misclassified as generic JSON decode |
| HT-09 | Very large JSON/HTML response | Only bounded diagnostics plus size/type and candidate counts; no full payload |
| HT-10 | Unknown vendor error shape | Safe structural category, no invented code or arbitrary body repr |
| HT-11 | 408/429/5xx supported by current retry policy | Existing retry/backoff unchanged, event timing correct |
| HT-12 | Successful response with no error fields | Useful metadata still logged without synthetic upstream error |

Assert event **fields and counts** through a capture fixture, not brittle human sentences. Transport metadata belongs to shared executor; provider-specific meaning belongs to adapters.

### 5. Cross-provider success and failure coverage

| ID | Provider family and command | Test cases | Expected result |
| --- | --- | --- | --- |
| PD-01 | Web SERP; `keyword-search` | Valid list, mixed valid/malformed list, zero valid hits | Received/accepted/rejected counts and grouped reasons; valid items survive |
| PD-02 | Bright Data SERP | Known header anomaly, valid `organic`, malformed `organic` | Vendor fields safely retained; no raw results; candidate counts |
| PD-03 | Other Web providers | Realistic adapter-specific 200 `error`, 429/5xx, missing URL | Safe code/message when supported, stable skip reasons, one branch final |
| PD-04 | Academic providers; `paper-search` | Valid record, malformed XML/JSON, missing required field | Existing `paper_candidate_rejected` events compatible; aggregate counts |
| PD-05 | OA / Unpaywall | 404 no match vs 500 outage | 404 remains normal absence in business layer; 500 distinguishable error |
| PD-06 | OpenAI-chat; `llm-search` | Protocol error then retry success, exhausted protocol retries | `provider_retrying` only with pending retry; exactly one terminal provider event |
| PD-07 | Antigravity; `agent-search` | Successful JSON, status ERROR JSON, malformed JSON, exit nonzero, timeout | Safe terminal status/code, duration/exit/byte counts; no stdout, stderr or prompt |
| PD-08 | Fetch providers; `url-fetch` | First fails, next succeeds; all fail; valid but empty page | Per-provider branch outcome/fallback, existing unavailable behavior preserved |
| PD-09 | LLM judge or post-processor | Invalid answer, judge failure | Safe stage/category, no raw completion text |

Construct fixtures from actual known response formats; do not assume a universal JSON error schema across vendors. Representative tests exercise each shared diagnostic adapter path plus specific exceptional formats.

### 6. Event counts, partial rejections, and fallback

One scripted keyword request should produce these facts:

- Provider A fails attempt 1 with 429, attempt 2 with 502, succeeds attempt 3: two `http_retrying`, one `provider_completed`, zero `provider_failed`.
- Provider B exhausts retries: one executor `http_failed` and one branch `provider_failed`. These events refer to distinct layers.
- Provider C receives 10 items: accepts 5, drops 3 for `missing_url` and 2 for `invalid_record`. The summary must report received=10, accepted=5, rejected=5, with exact grouped reasons; must not log rejected records.
- The overall keyword workflow succeeds with A/C results; no `workflow_failed` just because B failed.

Also verify:
- Each provider branch has exactly one terminal `provider_completed` or `provider_failed`.
- The LLM client does not emit terminal failure while retrying.
- Agent has no new automatic retry; an Agent terminal error produces one final failure event.
- Academic enrichment/fetch fallback records branch-level failure without falsely marking whole workflow failed.
- Request ID, provider and stage attribution remain correct under concurrent requests.
- The same status=200 but vendor error state is visible as a **semantic** failure.
- Malformed candidates that previously triggered silent `continue` now appear in aggregate counts, including all-invalid responses.

### 7. Privacy, volume and log integrity

| ID | Injected condition | Assertion |
| --- | --- | --- |
| LG-01 | Configured secret embedded in a known vendor error message | No secret appears after existing `SecretRedactor` |
| LG-02 | Error message over 512 chars, code over 128 | Bounded fields, truncation marker when relevant; original byte length retained |
| LG-03 | Unique sentinel in HTML page, raw JSON, article text, Agent stdout/stderr, model answer | Sentinel absent from diagnostics; only allowlisted error metadata present |
| LG-04 | Signed URL and `token=` query in an exception string | URL query/token not emitted |
| LG-05 | Auth/cookie/unrecognized custom HTTP headers | No header dump, only allowlisted known values |
| LG-06 | Error text containing CRLF/control characters or log injection | Sanitized single structured event, not forged log line |
| LG-07 | Unknown vendor payload or diagnostic helper error | Logging best-effort; provider control flow/result unaffected |
| LG-08 | DEBUG on/off, rotating log file reaches threshold | Existing level/rotation/stdout behavior maintained |
| LG-09 | Concurrent branches | Correct request-id/provider/stage correlation and no cross-branch counts |
| LG-10 | Empty body, or no HTTP response at all | Missing metadata represented as absent/unknown |
| LG-11 | Secret near truncation boundary | Redacted before final output |
| LG-12 | Ordinary success | Diagnostics include safe status/size/count, never full webpage or model answer |

Assert no secrets in serialized event stream, not merely fields read by the primary test. Existing log file path/rotation policy unchanged.

### 8. Google goto URL cases

All helper tests are pure local parsing; no network requests. Table-drive input, expected result and recognized-invalid state.

| ID | Input | Expected |
| --- | --- | --- |
| GU-01 | `https://www.google.com/goto?url=https%3A%2F%2Fexample.org%2Fpaper%3Fx%3D1` | `https://example.org/paper?x=1` |
| GU-02 | `https://google.com/goto?url=http%3A%2F%2Fexample.org%2Fa` | Valid HTTP target |
| GU-03 | `https://example.org/a` | Unchanged |
| GU-04 | `https://www.google.com/url?q=...` | Unchanged: unobserved format |
| GU-05 | Google `/goto` missing `url` | Recognized-invalid; skip candidate |
| GU-06 | Google `/goto` with empty or duplicate `url` | Recognized-invalid; skip candidate |
| GU-07 | Destination `javascript:`, `file:` or relative path | Recognized-invalid; do not expose unsafe URL |
| GU-08 | Host `google.com.evil.test` or `google.com@evil.test` | Not identified as Google host |
| GU-09 | Nonmatching host or path | Unchanged |
| GU-10 | Encoded Unicode/query delimiters in legitimate target | Decode once; preserve target meaning for normalizer |
| GU-11 | Google goto and direct hit point to same target from different providers | Existing canonical URL dedup returns one URL |
| GU-12 | Two valid goto links, one invalid goto, one direct hit | Invalid only skipped; aggregate rejection reason logged |
| GU-13 | Control chars/newline in extracted destination | Reject safely; never log raw URL |
| GU-14 | Malformed/ambiguous percent encoding | Reject if recognized-invalid; do not guess |
| GU-15 | Signed destination URL with query token | No query leak in log; normalizer maintains existing semantics |

Integration check: unwrapping occurs inside `SearchOrchestrator._stage_keyword_hit` **before** existing `normalize_url` and `URLStore`, independent of Bright Data, ScraperAPI or Scrape.do adapter. No rewrites in global `normalize_url` or `url-fetch`. A mocked network resolver must show **zero** requests during unwrapping.

### 9. Regression, review and verification sequence

1. Demonstrate failing focused tests on current code: timeout 3600 rejected; old 1140/1200 deadlines; missing success/upstream-error diagnostics; misleading LLM retry; unchanged goto link.
2. Implement each smallest coherent change and run corresponding focused test group.
3. Run existing HTTP executor, Web/Academic/LLM/Agent/Fetch regression tests, then all gateway acceptance tests, including `--provider` filtering and output framing.
4. Run full repository pytest, lint/typecheck and documented-config tests using the project's configured commands, recording exact commands and outcomes; distinguish pre-existing failures.
5. Inspect representative DEBUG records manually for **actual** safe field coverage, no duplicate branch terminal failures, redaction and truncation.
6. Only if useful, perform a few **real** low-cost smoke tests with configured environment and a narrowly chosen `--provider`. No 7200-second live run is required. Antigravity `agy -p` may legitimately take minutes for first output; avoid misclassifying this as broken connectivity.

No source implementation or tests are added during brainstorming. Specifications are reviewed first; commit only after the user approves them.

### 10. Definition of done

- [ ] Agent 3600/7200 accepted and truly supervised by that configured deadline; no hidden 540/1140/1200 upper cap.
- [ ] Invalid/non-finite timeout rejected; cancellation cleans processes; non-Agent timeout unchanged.
- [ ] Every provider response yields meaningful safe metadata on success and failure where a response exists.
- [ ] Known provider status/error code/message preserved for diagnosis rather than replaced only by local gateway categories.
- [ ] Attempt retries and terminal provider outcomes are separate; exactly one terminal outcome per provider branch.
- [ ] Partial bad records produce accurate counts/reasons without logging records themselves.
- [ ] DEBUG logging is bounded, redacted, safe under malformed values and does not dump bodies, credentials, prompts or signed URLs.
- [ ] Verified Google goto URLs unwrap once centrally; invalid recognized links drop only that item; unknown formats unchanged.
- [ ] No external redirect requests or changes to global normalization/fetch behavior.
- [ ] Public protocol, CLI/results and retry/fallback behaviors remain compatible; focused and full offline tests pass.
