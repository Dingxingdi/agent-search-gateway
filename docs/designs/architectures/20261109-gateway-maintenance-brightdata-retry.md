## Architecture: Gateway Maintenance and Bright Data Header-Aware Retry

### 1. Scope & Assumptions

#### In Scope

- Document that dblp can trigger bot verification even with infrequent API calls; provide an optional disablement example in README.
- Add top-level `agent-search-gateway --version` and `agent-search-gateway -v`, printing the installed CLI package's existing `__version__` and exiting without configuration or daemon access.
- Remove only the ZenRows, Decodo, ScrapingDog, and ScrapeGraphAI stanzas from `config.example.toml`; change its `[retry].request_timeout_seconds` from `30.0` to `180.0`.
- Detect Bright Data SERP search failures reported by `x-brd-error-code`, `x-brd-error`, and `x-brd-status-code`, even when outer HTTP status is 200.
- Retry only documented recoverable Bright Data header errors (`captcha`, `timeout`, `expect_element`, `expect_body`), using the **existing** retry policy's total attempt budget with bounded optional jitter.
- Record safe provider, stage, attempt, outer/inner status, code, retry decision, actual wait, and terminal classification in DEBUG logs.

#### Todo / Explicitly Out of Scope

- No changes to Bright Data Search request endpoint, authentication, zones, `format: "raw"`, `brd_json=1`, or response's existing `organic` mapping.
- No changes to Bright Data Web Unlocker Fetch request parameters or standard success path.
- No removal of the four providers from the adapter registry, normal configuration support, or the README capability table.
- No changes to runtime retry defaults, `RetryPolicy` fields, the socket protocol, orchestrators, or daemon's existing public error messages.
- No separate Bright Data retry loop or new retry configuration; no general provider format negotiation or CAPTCHA solving.
- No package version bump, release, or PR in this design phase.

#### Assumptions

- Current worktree starts from `v0.2.0`. `src/agent_search_gateway/__init__.py` already supplies canonical `__version__ = "0.2.0"`, in sync with `pyproject.toml`.
- The official Bright Data API supports the already-implemented Google SERP URL with `brd_json=1` and outer `format: "raw"`.
- `retry.max_attempts` means the **total** HTTP attempts, not additional retries; its configured default is 3.
- End users can inspect the local DEBUG log when they need vendor-specific failure details; retaining the CLI's existing summary error is intentional.
- New provider inspection and jitter behavior must be opt-in to preserve every other provider and existing HTTP retry test.

---

### 2. Architecture Summary

Keep the local daemon, per-provider quota, generic HTTP executor, and single generic retry loop. Bright Data owns the interpretation of its proprietary response headers and passes a narrow, optional response-inspection callback only from its `search()` call. The HTTP executor invokes that callback **inside** the same attempt that performs the HTTP request, so the generic retry loop handles both existing transport/status failures and typed vendor-header failures without multiplying attempts. Add an optional, bounded delay adjustment to the existing retry helper, used only for retryable Bright Data SERP header failures; the default remains exact exponential backoff. Log sanitized terminal and retry diagnostics at the HTTP boundary. Separately, add a top-level argparse version action and edit README and the example TOML without changing provider registrations or daemon configuration defaults.

---

### 3. Design Decisions

#### Runtime Model

##### Preserve daemon, quotas, and one retry layer

- **Description:** Leave `Runtime.build()`, `ProviderQuotaManager`, `SearchOrchestrator`, `FetchOrchestrator`, socket requests, and result sinks unchanged. Bright Data search continues to call the shared `HttpJsonExecutor` from inside the provider's existing quota lease.
- **Rationale:** The anomalous response is a transport-adjacent provider condition; correcting it before JSON decoding does not require redesigning search orchestration or command error propagation.
- **Trade-off:** If all providers fail, the business CLI still shows the existing generic all-providers-failed message. DEBUG logs carry the precise Bright Data diagnosis.
- **Rejected alternative:** An adapter-level outer `retry_async` would multiply request attempts and bypass unified per-attempt logging; changing the orchestrator's public error summary adds unrelated contract changes.

#### Interface / Protocol

##### Use existing package version for top-level CLI flags

- **Description:** Add `parser.add_argument("-v", "--version", action="version", version="%(prog)s <package __version__>")` on the top-level argparse parser.
- **Rationale:** Argparse's version action exits before required subcommand checks, `RuntimePaths.default()`, `asyncio.run()`, daemon contact, or configuration loading. `__version__` already exists.
- **Trade-off:** `-v` is globally reserved for version output; no subcommand-level version flag is introduced.
- **Rejected alternative:** A `version` subcommand or manual preparse increases command-branch logic and is not the requested grammar.

##### Make vendor response inspection optional on JSON calls

- **Description:** Add one optional response-inspection argument to the shared JSON request boundary (and its narrow protocol/fake, if necessary). The callback receives the `httpx.Response`, checks vendor headers, and reports a typed provider-response failure including safe code, optional inner status, retry eligibility, and sanitized reason.
- **Rationale:** The adapter knows Bright Data's headers; only the HTTP executor can apply an error classification before `response.json()` and *within* the existing retry loop.
- **Trade-off:** The generic request boundary gains a small optional extension contract and affected test doubles must accept it.
- **Rejected alternatives:**
  - Import `BrightDataAdapter` or recognize `x-brd-*` by name in `providers/http.py`: couples shared transport to one vendor.
  - Handle `ProtocolFailure` and retry from `BrightDataAdapter.search()`: too late to inspect headers via the existing JSON executor; would introduce nested retries.
  - Redesign the entire HTTP client hierarchy: unnecessary for a single vendor-header case.

#### Provider Integration

##### Bright Data Search only interprets header failures

- **Description:** Bright Data supplies the callback only for `search()`. On each HTTP response, inspect `x-brd-error-code`, `x-brd-error` and `x-brd-status-code` before any JSON decode. A reported error or failure-status header takes precedence over outer 2xx success; `captcha`, `timeout`, `expect_element` and `expect_body` are retryable. Unknown/invalid/auth/config errors are terminal. A normal response proceeds unchanged to `organic` mapping.
- **Rationale:** Preserves the working SERP request contract and isolates vendor-specific header semantics.
- **Trade-off:** Provider-specific header behavior is not automatically available to other adapters, which is intentional.
- **Rejected alternative:** Change `format`/`data_format` or the SERP zone. Real successful responses confirm the existing request format works.

##### Keep Fetch unaffected

- **Description:** `fetch()` continues using `request_text()` and the existing zone, `format: "raw"`, and `data_format: "markdown"`. No Bright Data header callback is installed on the shared executor globally.
- **Rationale:** Search and Fetch share an executor but have independent request contracts; per-call opt-in is essential.
- **Trade-off:** A future independently diagnosed Fetch header failure will need an explicit follow-up design.

#### Concurrency / Scheduling

##### Reuse `retry_async` and inject jitter only where required

- **Description:** Extend `retry_async` with an **optional** delay-adjustment function defaulting to identity. Compute capped exponential delay as today, then optionally apply a bounded jitter factor (e.g. `uniform(0.8, 1.0)`) only for retryable Bright Data header errors. Log and sleep for the *same actual adjusted delay*. Keep cancellation semantics, total `max_attempts` budget, per-attempt request timeout, and existing hooks.
- **Rationale:** This is a small additive enhancement at the existing retry scheduling point. Other providers retain their exact delays and test outputs.
- **Trade-off:** A tiny amount of optional callback plumbing and deterministic randomness injection is added; jitter is intentionally not a new user-facing config knob.
- **Rejected alternatives:** Global jitter by default (behavior change for all providers), second vendor loop (amplified attempts), wrapping HTTP sleep without adjusting retry-log delay (misleading logs), or hardcoded extra attempts.

#### State Management / Storage

##### Retain current in-memory state and file contracts

- **Description:** No new persistent data, retry counters outside the existing attempt loop, socket fields, provider cache, or result schema. Attempts are local to each shared HTTP request.
- **Rationale:** Failure-header recovery is a transient execution detail.
- **Trade-off:** Detailed diagnostics remain available only when DEBUG tracing is enabled.

#### Security

##### Treat diagnostic headers as untrusted provider data

- **Description:** Classify retryability using a fixed known-code allowlist. Bound and sanitize printable error-code/reason material; never blindly forward raw `x-brd-error` headers, target query strings, request/response bodies, Authorization values, or API keys to logs. Reuse central secret redaction as defense in depth. Invalid or unsafe diagnostic content falls back to a fixed safe reason, while preserving safe code/status.
- **Rationale:** Provider headers can contain user-dependent URLs or arbitrary strings even when the outer HTTP status is 200.
- **Trade-off:** Some verbose vendor explanations may be truncated/redacted for security.

#### Observability

##### Enrich existing HTTP events, keep CLI output stable

- **Description:** Keep `http_attempt_started`/`http_attempt_completed`/`http_retrying`/`http_failed` and existing provider summary events. For inspected Bright Data failures, include structured safe `brightdata_error_code`, `brightdata_status`, outer `status`, `attempt`, retry decision and actual `delay_ms`, and terminal error category. No raw payload logging.
- **Rationale:** Debuggers can identify masked HTTP-200 failures without altering other providers or business command stdout/stderr contracts.
- **Trade-off:** Without DEBUG mode, the user sees the existing generic search failure.

#### Documentation / Future Migration

##### Keep edits narrow and accurately scoped

- **Description:** In README, add the dblp low-frequency anti-bot warning (and optional `enabled = false` mitigation) near academic search, and document both version flags. In `config.example.toml`, remove only four optional provider example blocks and set `request_timeout_seconds = 180.0`. Adapt `tests/docs/test_documented_config.py` to distinguish built-in registered capabilities from the intentionally reduced example entries. Do not change `config.py`'s default timeout (30 seconds) or its supported provider list.
- **Rationale:** An example template is not a provider deprecation, and the user explicitly requested the timeout change in the example only.
- **Trade-off:** Existing user configurations are unaffected until users edit them; README can state this explicitly.

---

### 4. Component Catalog

| Component | Purpose | Responsibilities | Interface / Output | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| `cli.build_parser` | Parse top-level version flags | Argparse version action using `__version__` | CLI stdout + exit 0 | package version | No | Entry/validator |
| `BrightDataAdapter.search` | Perform SERP requests | Existing request; optional vendor-header inspection; map `organic` | `search(query) -> list[KeywordSearchHit]` | `HttpRequester` | Configured zones, secret | Vendor adapter |
| `HttpJsonExecutor` | Shared request boundary | HTTP calls, optional inspection, retries, standard status/JSON handling, safe events | `request_json(..., optional inspector)` | `httpx`, `retry_async` | Client, retry policy | Transport coordinator |
| `retry_async` | Shared retry scheduling | Fixed attempt budget, capped backoff, opt-in bounded delay adjustment, hooks | `retry_async(policy, operation, ...)` | `RetryPolicy` | Per-call counters | Scheduler |
| `SearchOrchestrator` | Keyword fan-out and aggregation | Provider quotas, result admission, aggregate failures | `keyword_search` | Provider adapters, URL store | Existing URL state | Coordinator (unchanged) |
| `README.md` | Public operation guide | dblp caveat and CLI version syntax | Markdown docs | Public CLI/config contracts | No | Documentation |
| `config.example.toml` | Copyable configuration template | Selected provider examples, example timeout 180.0 | TOML | Existing config parser | No | Configuration sample |
| Existing test suites | Regression evidence | Cover parser, config, HTTP, retry and provider isolation | pytest | Mock transports/fakes | No | Verification |

`BrightDataAdapter` must not own retry attempts. `retry_async` and `HttpJsonExecutor` must not hardcode Bright Data header names. The version command must not know about daemon runtime state.

---

### 5. Data Flow

#### 5.1 Top-level version entry point

```text
CLI main(argv)
  -> cli.build_parser() loads the package's existing __version__
  -> argparse parses global --version / -v
  if version action selected:
      print "agent-search-gateway 0.2.0" on stdout
      exit 0
  else:
      enforce existing required subcommand and input grammar
      enter existing asyncio/run_command path
```

#### 5.2 Keyword Search with Bright Data

```text
CLI keyword-search(query, optional --provider brightdata)
  -> existing socket/daemon dispatch
  -> SearchOrchestrator selects provider(s)
  -> acquire Bright Data web quota lease
  -> BrightDataAdapter.search(query)
       build same Google URL: q=<encoded-query>&brd_json=1
       POST /request, bearer auth, configured search_zone, format="raw"
       -> HttpJsonExecutor.request_json(..., optional Bright Data inspector)
            -> retry_async(configured RetryPolicy, existing operation)
                for attempt in 1..max_attempts:
                    send HTTP request with existing per-attempt timeout
                    log attempt + outer status
                    if Bright Data inspector detects vendor failure:
                        create typed provider-response failure with safe metadata
                        if code in fixed recoverable list and attempts remain:
                            compute capped exponential delay + bounded jitter
                            log retry with actual delay, sleep, continue
                        else:
                            log final failure and raise typed failure
                    else if existing HTTP retryable status/transport failure:
                        reuse unchanged existing retry behavior
                    else if existing non-2xx terminal status:
                        raise existing HTTP status failure
                    else:
                        return HTTP response
            if response received:
                parse JSON, then adapter validates root and maps organic[]
  -> SearchOrchestrator stages/admit/dedupe results
  if at least one selected provider succeeds:
      write current keyword JSONL result and print its path
  else:
      retain existing generic all-providers-failed CLI response
      inspect DEBUG log for Bright Data code, attempts and terminal diagnosis
```

#### 5.3 Existing URL Fetch entry point

```text
CLI url-fetch(admitted URL)
  -> existing daemon + FetchOrchestrator + FetchScheduler
  -> BrightDataAdapter.fetch(url), if selected as existing fetch provider
       POST /request using existing fetch_zone, format="raw",
         data_format="markdown"
       -> HttpJsonExecutor.request_text() WITHOUT Bright Data inspector
       -> existing generic status/transport retry and plain-text handling
  -> existing LLM content processing and final fetch output
```

#### 5.4 Copying and loading the example configuration

```text
Operator copies config.example.toml into user config
  -> only enumerated provider blocks are present in the example
  -> [retry].request_timeout_seconds = 180.0
  -> daemon startup resolves the user config through existing parser
  if other valid provider names are configured manually:
      keep normal provider registry and support
  if installed user config is unchanged:
      keep its existing timeout/provider behavior
```

---

### 6. Interfaces & Contracts

#### Public CLI version contract

```text
$ agent-search-gateway --version
agent-search-gateway 0.2.0
# exit status 0; no daemon/config dependency

$ agent-search-gateway -v
agent-search-gateway 0.2.0
# same output and exit status
```

#### Bright Data Search request contract (unchanged)

```json
{
  "zone": "<configured search_zone>",
  "url": "https://www.google.com/search?q=<encoded-query>&brd_json=1",
  "format": "raw"
}
```

#### Internal provider-header error signal

```json
{
  "http_status": 200,
  "provider_status": 502,
  "provider_error_code": "captcha",
  "safe_reason": "redirect location was rejected",
  "retryable": true
}
```

This is an illustrative **internal** classification, not a new network message or public error schema. Only fixed recoverable codes set `retryable=true`. The internal type may differ if a smaller implementation achieves the same contract.

#### DEBUG event contract (illustrative)

```text
event=http_retrying provider=brightdata stage=search attempt=1 status=200 brightdata_status=502 brightdata_error_code=captcha delay_ms=<actual-jittered-delay>
event=http_failed provider=brightdata stage=search attempt=3 status=200 brightdata_status=502 brightdata_error_code=captcha category=provider_header reason=<safe-summary>
```

Existing unrelated HTTP event field meanings stay unchanged. Raw provider diagnostic values are never automatically trusted for logging.

#### Public compatibility

- Successful keyword search still prints only the result-file path.
- All-selected-providers-failed still returns the existing aggregate CLI message.
- No changed socket request schema, result row fields, provider configuration schema, or runtime retry defaults.
- Version flags are a new top-level CLI interface; omitted flags follow the exact existing parser flow.
