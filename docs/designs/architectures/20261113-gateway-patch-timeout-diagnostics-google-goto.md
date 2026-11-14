## Architecture: Gateway Patch — Agent Timeout, Provider Diagnostics, Google Goto

**Status:** Draft for user review (not implemented)
**Date:** 2026-11-13
**Base:** d0ff1bc / v0.2.1
**Companion specs:** [Error handling](../error-handlings/20261113-gateway-patch-timeout-diagnostics-google-goto.md) and [Testing](../testings/20261113-gateway-patch-timeout-diagnostics-google-goto.md).

### 1. Scope & Assumptions

#### In Scope
- Accept any *finite positive* Agent timeout; remove the current 540-second maximum.
- Remove fixed 1,140-second Agent workflow and 1,200-second Agent socket client deadlines that can override a longer configured invocation timeout.
- Retain the configured per-process deadline, agy --print-timeout, Python hard timeout margin, bounded output and process-group cleanup.
- Unify DEBUG timing/meaning for per-attempt retries, provider terminal outcomes and per-result rejections; extract bounded and redacted response diagnostics on success and failure.
- Decode only the observed Google search wrapper shape, google.com/goto?url=..., once at the shared keyword-result ingestion boundary.
- Preserve CLI, NDJSON and JSONL contracts, HTTP retry policy, partial success, Fetch semantics and non-Agent timeouts.

#### Todo / Out of Scope
- No full response archive, webpage body logging, raw model/Agent answer or prompt logging, or raw Agent stderr.
- No arbitrary redirects, URL shortener resolution, network redirect requests, preemptive support for other Google/Bing formats, or URL rules framework.
- No new CLI/config flags, Agent harness, automatic whole-Agent retries, public error schema, or retroactive URLStore rewriting.
- No refactor of LLMStages, provider registry, RetryPolicy, academic aggregation or output schema.

#### Assumptions
- Issue #74 confirms Google /goto wrappers from ScraperAPI and Scrape.do, but its redacted query values do not establish direct versus opaque destinations. The [SerpApi primary report](https://serpapi.com/blog/googles-new-goto-redirect-urls-resolution-in-progress/) confirms the opaque `CAES...` form; the 2026-11-14 review correction preserves well-formed URL-safe CAES wrappers without decoding them. No specific live Bright Data wrapper shape was confirmed.
- The user chose unlimited *configuration range*, not an infinite process runtime: timeout_seconds must remain finite and positive, default 300.
- DEBUG should contain actionable response metadata and provider code/message snippets, not complete raw payloads. Default CLI stdout/stderr remain unchanged.
- The actual agy CLI parser is an external dependency; removal of Gateway's cap does not guarantee every extreme floating-point duration is accepted upstream.

---

### 2. Architecture Summary

Retain CLI → Unix socket → Daemon → orchestrators → providers and existing stores. A configured Agent timeout remains enforced in its subprocess adapter; the outer Agent workflow and socket reader no longer impose independent fixed caps. Shared HTTP/retry observability records attempts, adapters safely classify vendor response fields, and pipeline owners emit one terminal provider_completed or provider_failed after internal retries. Log only redacted, bounded diagnostic fields plus success/skip counters. A small pure URL helper handles one verified Google /goto shape before keyword result normalization, deduplication and URLStore admission, irrespective of originating Web provider.

---

### 3. Design Decisions

#### Runtime Model

##### Preserve finite Agent invocation deadline, remove unrelated ceilings
- **Description:** Remove the >540 validation in resolve_agent_config(), and the fixed Agent-only 1,140-second orchestrator timeout. Keep timeout_seconds >0 and finite, its --print-timeout argument, the Python timeout_seconds + 5s bound and all bounded child cleanup.
- **Rationale:** A 3,600-second configured call must not fail at 540 or 1,140 seconds.
- **Trade-offs:** Time waiting for quota and multiple scope branches may exceed one configured invocation timeout; there is no new end-to-end deadline.
- **Rejected alternative:** Compute a global limit from invocation counts and concurrency; it duplicates scheduling policy and risks cutting off queued legitimate jobs.

##### Keep process ownership intact
- **Description:** Keep the capacity lease, TemporaryDirectory, pipe byte limits, SIGTERM/SIGKILL, shielded subprocess spawn, bounded reap and cancellation propagation.
- **Rationale:** Removing an arbitrary configuration cap should not remove resource guards.
- **Trade-offs:** Existing documented OS-level blocked-spawn and escaped-descendant caveats still apply.
- **Rejected alternative:** Remove Python's hard timeout together with the configuration maximum.

#### Interface / Protocol

##### Agent socket wait has no fixed default limit
- **Description:** For AgentSearchRequest only, send_request() uses an asyncio.wait_for timeout of None by default; explicit numeric overrides remain honored. Non-Agent response timeout stays 600 seconds.
- **Rationale:** The CLI does not know the daemon's configured duration, quota wait or two-branch scheduling.
- **Trade-offs:** An unresponsive but connected daemon may cause an Agent CLI to wait until interruption/disconnection; there is no independent client watchdog.
- **Rejected alternative:** Add config-reading logic to CLI or a new NDJSON progress/deadline handshake.

##### Preserve public interfaces
- **Description:** No new command switches, request/response frame fields, JSONL fields or result semantics. Only valid Agent TOML timeout range expands.
- **Rationale:** This is a maintenance patch, not a public API revision.
- **Trade-offs:** Rich vendor diagnostics appear only when DEBUG is enabled.
- **Rejected alternative:** Expose full vendor errors directly on CLI stderr.

#### Provider Integration

##### Extract diagnostics at the layer that understands the response
- **Description:** Use shared safe field/truncation helpers in existing observability/provider modules. HttpJsonExecutor retains transport status/attempt ownership; adapters recognize their own success/error JSON or XML envelopes, error codes and rejected candidate counts. The owner of a provider pipeline records its terminal outcome.
- **Rationale:** HTTP 200 may contain a business error and a non-2xx body may contain a useful safe vendor code.
- **Trade-offs:** A few small adapter edits are needed to capture vendor-specific envelope fields; unknown payloads remain classified generically.
- **Rejected alternatives:** Dump all response bodies; force every vendor through one JSON error schema; build a new provider logging framework.

##### URL unwrapping is provider-independent and deliberately narrow
- **Description:** Add unwrap_search_result_url() for exact allowed Google hostname + /goto path and one unambiguous url parameter; validate the extracted HTTP(S) target. Unknown URL shapes and well-formed opaque URL-safe `CAES...` wrappers pass unchanged; other recognized but invalid wrappers are rejected as individual candidates. Call it from SearchOrchestrator._stage_keyword_hit before normalize_url().
- **Rationale:** Keyword Web adapters share KeywordSearchHit; the rule belongs before URL identity and works for all such adapters.
- **Trade-offs:** It does not unwrap opaque links, other forms, LLM/Agent markdown URLs or paper URLs.
- **Rejected alternatives:** Per-provider code, modification of global normalize_url(), speculative rule tables, or network redirect following.

#### State Management

##### Keep URLStore and fallback behavior unchanged
- **Description:** Admit and deduplicate by the unwrapped target where one is safely obtained; otherwise retain existing store, quota, retry, partial success, semantic rejection and sequential Fetch fallback behavior.
- **Rationale:** The wrapper bug concerns incoming keyword URL identity; downstream fetch should receive the correct key.
- **Trade-offs:** Previously stored wrapper URLs are not migrated.
- **Rejected alternative:** Resolve wrapper links only at url-fetch time, which is too late for search deduplication.

#### Storage / Persistence

##### Reuse DEBUG sinks without archiving raw payloads
- **Description:** Keep the rotating file, stderr DEBUG sink, request ID and redaction filters. Log response byte count/type, upstream status/code/safe message, record counts, skip reasons and outcome; cap text (target maximum 512 characters).
- **Rationale:** Debugging needs cause and context, not potentially megabytes of HTML/model output.
- **Trade-offs:** A completely opaque vendor error may still need separate external reproduction.
- **Rejected alternative:** A new raw response storage subsystem with retention and leakage risks.

#### Concurrency / Scheduling

##### Do not change execution order, only terminal event ownership
- **Description:** HTTP retries remain local to HttpJsonExecutor/retry_async; Agent runs once. Keyword/Academic/LLM/Agent provider pipelines emit one terminal event per selected invocation/branch; Fetch keeps sequential provider fallback.
- **Rationale:** Retries and branch fan-out are not themselves terminal provider failures.
- **Trade-offs:** Several distinct diagnostic events may legitimately describe one incident.
- **Rejected alternative:** Suppress intermediate failures or mark every intermediate retry provider_failed.

#### Security

##### Upstream error text is untrusted
- **Description:** Allowlist scalar fields, normalize whitespace, apply a small length bound, sanitize sensitive URL query/header material, apply SecretRedactor and never log arbitrary exception repr(), full bodies, prompt, stdout or stderr.
- **Rationale:** Vendor payloads may include credentials, prompts, private content and log-control characters.
- **Trade-offs:** Messages may be shortened or omitted when safe extraction is uncertain.
- **Rejected alternative:** Arbitrarily truncate raw response bodies and assume redaction catches every secret.

#### Observability

##### Make attempt, response and terminal events distinguishable
- **Description:** Retain http_attempt_started/completed, http_retrying and http_failed for HTTP-level outcomes. Add safe provider response metadata on both success and error. Use provider_retrying (not provider_failed) for an LLM protocol retry; provider_completed/provider_failed represent one terminal pipeline outcome. Keep candidate_rejected, paper_candidate_rejected, provider_partial_failure and workflow_failed distinct.
- **Rationale:** Logs should answer whether an attempt, a provider pipeline or a whole command failed.
- **Trade-offs:** Tests that previously asserted provider_failed for protocol retries need updating.
- **Rejected alternative:** Preserve the ambiguous prior naming.

#### Future Migration

##### Add new patterns only with evidence
- **Description:** When another real wrapper is observed, add a small branch and tests to unwrap_search_result_url(). Extract a rule table only if repeated formats justify it.
- **Rationale:** Avoid coupling new providers to a guessed family of redirects.
- **Trade-offs:** Each new shape requires a code/test update.
- **Rejected alternative:** Premature generic redirect framework.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| config.resolve_agent_config | Agent config boundary | Positive finite validation, no 540 cap | Internal config resolver | TOML/config helpers | No | Validator |
| run_antigravity | Agent process boundary | Configured deadline, output bounds, cleanup, safe terminal data | Internal AgentTextRunner | asyncio, quota | Per-call process/tasks | Executor |
| protocol.send_request | Client socket boundary | Agent wait None, other waits 600s | Migration-stable NDJSON client | asyncio | Connection | Transport |
| Daemon | Command dispatch | Workflow result/log lifecycle | Internal request dispatcher | Runtime | Active tasks | Coordinator |
| HttpJsonExecutor | Shared HTTP boundary | Attempts, retries, status/decode diagnostics | request_json/request_text | httpx, retry_async | Client | Executor |
| Web/Academic adapters | Vendor parsing | Safe business error and rejection classification | Existing search/fetch/resolve | HTTP executor | No | Adapter |
| SearchOrchestrator | Keyword/LLM/Agent coordination | Terminal events, keyword URL staging, result writing | Existing search methods | Adapters, stages, URLStore | Per-workflow | Coordinator |
| PaperSearchOrchestrator | Paper search | Terminal events and aggregation | paper_search | Academic adapters | Per-workflow | Coordinator |
| FetchScheduler | URL provider fallback | Per-provider attempt/fallback outcome | fetch_until_accepted | Web adapters, stages | Local remaining set | Scheduler |
| LLMStages/OpenAI Chat | LLM boundary | Protocol validation and retry context | Existing stage methods | HTTP executor | Per-call | Executor |
| unwrap_search_result_url | Correct keyword URL identity | One pure verified Google goto decoding rule | New internal helper | urllib.parse | No | Transformer |
| URLStore/ResultWriter | Persist results | Existing admission, dedup, JSONL | Existing internal interfaces | Filesystem | Yes | Sink |
| observability | Diagnostics | Safe fields, log_event, redaction, sinks | Existing logging helpers | logging | Logger session | Sink |

The URL helper must not call HTTP or depend on provider names; the HTTP executor must not know academic aggregation, URLStore or Agent stdout. Logging must never determine whether business work succeeds.

---

### 5. Data Flow

#### 5.1 Start / configuration

~~~text
CLI.start(debug):
    install existing DEBUG handlers when requested
    Daemon.start -> resolve_config -> resolve_agent_config
        if timeout is not finite positive: ConfigFailure
        else: keep duration even if >540
    Runtime._build_agent_runners binds timeout to run_antigravity
    if startup fails: return existing concise error
~~~

#### 5.2 keyword-search

~~~text
CLI -> send_request(KeywordSearchRequest) -> Daemon
SearchOrchestrator.keyword_search:
    if invalid query/provider: InputFailure
    for each selected provider concurrently:
        provider_started
        HttpJsonExecutor: attempts -> response/retry/terminal HTTP events
        adapter:
            if vendor business error: extract safe code/message; ExecutionFailure
            parse items; count/log invalid items without changing filtering
        for each KeywordSearchHit:
            if verified Google /goto: extract target
                if well-formed opaque CAES token: retain wrapper identity
                elif invalid target: candidate_rejected; continue
            normalize URL; keep existing other invalid URL handling
            if abstract empty: candidate_rejected; continue
            if body provided: existing LLM Judge; reject/fail as before
        provider_completed or provider_failed (once per pipeline)
    if all pipelines failed: ALL_PROVIDERS_FAILED
    else: URLStore.admit -> existing ResultWriter JSONL
Daemon: workflow_completed or workflow_failed -> CLI
~~~

#### 5.3 paper-search

~~~text
CLI -> send_request(PaperSearchRequest) -> Daemon
PaperSearchOrchestrator.paper_search:
    validate query/provider
    for Academic provider concurrently:
        provider_started
        HTTP executor attempts and safe response metadata
        parse JSON, XML or Atom
            if invalid envelope: ProtocolFailure
            if invalid paper item: paper_candidate_rejected; continue
        provider_completed or provider_failed once
    if all failed: ALL_PROVIDERS_FAILED
    else:
        PaperAggregator.aggregate and log rejected/merged records
        OA enrichment: if resolver error, paper_enrichment_failed; keep paper
        URLStore.admit -> write existing JSONL
Daemon -> CLI
~~~

#### 5.4 llm-search

~~~text
CLI -> send_request(LLMSearchRequest) -> Daemon
SearchOrchestrator.llm_search:
    validate prompt/scope/provider/model
    for each Web/Paper branch invocation:
        provider_started
        LLMStages -> OpenAIChatCompletionsClient -> HttpJsonExecutor
        if protocol failure and retry remains: provider_retrying
        if LLM stage exhausted: llm_stage_failed; propagate
        parse strict search or paper Markdown
            if parser error: ParserFailure
        provider_completed or provider_failed once
    if both branches failed: aggregate failure
    else: existing partial success, aggregation and writer
Daemon -> CLI
~~~

#### 5.5 agent-search

~~~text
CLI -> send_request(AgentSearchRequest):
    default socket wait has no fixed 1200s limit
Daemon -> SearchOrchestrator.agent_search:
    validate prompt/scope/provider/model
    no fixed 1140s workflow timeout
    for each selected Agent/Web/Paper branch invocation:
        provider_started
        run_antigravity:
            await quota (outside process timer)
            spawn agy --print-timeout=configured finite seconds
            read bounded streams and wait under configured+margin deadline
            if nonzero/timeout/invalid JSON/status/empty/output limit:
                retain safe classification, bounded cleanup, raise
            else: cleanup and return response
        strict Markdown parse; provider_completed or provider_failed
    if every branch failed: aggregate failure
    else: existing partial-success writer
Daemon -> CLI
~~~

#### 5.6 url-fetch

~~~text
CLI -> send_request(URLFetchRequest) -> Daemon
FetchOrchestrator.url_fetch:
    normalize URL
    if not admitted: InputFailure
    if stored unavailable: return existing unavailable text
    if body cached: skip provider calls
    else FetchScheduler:
        for available Fetch provider sequentially:
            provider_started
            adapter.fetch through shared HTTP and safe diagnostics
            if execution error: provider_failed; provider_fallback; continue
            if valid response but semantic/Judge rejection:
                body_rejected; provider_completed(semantic_failure)
                provider_fallback; continue
            if Judge execution error: log dependency and propagate
            provider_completed(accepted); stop fallback
        if all execution failures: ALL_PROVIDERS_FAILED
        if final semantic failure: mark unavailable, return existing text
    optional content_clean, safety and focus_summary LLM stages
    return existing content/unavailable result or propagate existing failure
Daemon -> CLI
~~~

---

### 6. Interfaces & Contracts

#### Public contracts
- Existing CLI commands, selectors, stdout/stderr behavior, NDJSON request/response fields, error codes and JSONL result schemas are unchanged.
- Agent timeout_seconds is still numeric, finite and positive with a 300-second default; no Gateway upper bound.
- send_request(response_timeout_seconds=numeric) explicit overrides still work; Agent's default wait is None, non-Agent default remains 600 seconds.

#### Internal diagnostic contract

~~~json
{
  "provider": "brightdata",
  "stage": "search",
  "attempt": 2,
  "status": 200,
  "response_bytes": 1280,
  "category": "provider_response",
  "upstream_code": "captcha",
  "upstream_message": "redirect location was rejected",
  "retry_scheduled": false
}
~~~

All fields beyond provider/stage/event are optional when unknown. This object illustrates internal structured log fields, not a new public JSON response. Upstream message text is sanitized, length-limited and redacted. Normal DEBUG logs keep the existing request-id correlation.

#### Internal URL helper contract

~~~text
unwrap_search_result_url(raw: str):
    if host/path does not match known Google /goto: return unchanged
    extract exactly one unambiguous url query value
    if target matches CAES[A-Za-z0-9_-]+={0,2}: return original wrapper unchanged
    if not a valid HTTP(S) target: report recognized-invalid
    else: return target for existing normalize_url and URLStore
~~~

A small tagged result or a local exception may represent recognized-invalid without changing public models. No network calls or unrelated URL rewrites.

#### Operational invariants
- One provider terminal outcome per pipeline/branch; retry events are not terminal.
- Every HTTP response gets safe status/size diagnostics; adapters add safely understood vendor fields and candidate counts.
- Logging is best effort and must not change the business result.
- No raw pages, complete model output, prompts, unchecked exception messages, credentials or sensitive URL queries are written to logs.
