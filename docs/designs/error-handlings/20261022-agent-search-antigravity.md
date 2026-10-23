## Error Handling: Agent Search with Antigravity CLI

**Status:** Approved by user on 2026-10-22; model selection revised per user request on 2026-10-23; implementation not started.
**Architecture:** [20261022-agent-search-antigravity.md](../architectures/20261022-agent-search-antigravity.md).
**Testing:** [20261022-agent-search-antigravity.md](../testings/20261022-agent-search-antigravity.md).

### 1. Principles and Existing Contracts

- Preserve existing typed `GatewayError` classes, daemon exception sanitization, nonzero business-command exit behavior, and final-output-only stdout.
- Add only one domain-specific no-provider code: `ErrorCode.NO_AGENT_SEARCH_PROVIDERS`. Existing `BAD_REQUEST`, `CONFIG_ERROR`, `ALL_PROVIDERS_FAILED` and `PROTOCOL_ERROR` suffice for the other cases.
- Report bad CLI selectors before Socket / Harness work. Do not silently fall back to a different Agent, model, LLM search provider or keyword-search service.
- Treat the headless CLI as a **fallible external process**, not as a guaranteed successful model completion. Nonzero process exit or JSON `status != SUCCESS` cannot count as success.
- Never log raw prompt, raw response, Antigravity stderr, CLI error payloads, authentication values, API keys or unredacted exception messages. Emit a small allowlisted failure category.
- Each Agent invocation/branch can fail independently. Keep `llm-search`'s existing partial success semantics: one surviving provider/branch is enough; both `all` branches failing produces no result.
- Never leave orphaned child processes after a timeout, parent-task cancellation, daemon shutdown or stdout/stderr over-limit event.
- Preserve existing fetch LLM configuration/validation and all keyword/fetch error semantics.

### 2. Error Matrix

| Boundary / Condition | Classification | User-visible outcome | Side effects / cleanup |
|---|---|---|---|
| Empty/whitespace prompt | `InputFailure(EMPTY_QUERY)` | Input error | No Agent launched; no file |
| Invalid scope | `InputFailure(BAD_REQUEST)` / argparse rejection | Input error | No Agent launched |
| Explicit blank provider/model selector | `InputFailure(BAD_REQUEST)` | Explain blank selector | No Agent launched |
| No configured `search_agent` invocations | `ExecutionFailure(NO_AGENT_SEARCH_PROVIDERS)` | No Agent search providers configured | No Agent launched |
| Unknown Agent provider/model or missing pair | `InputFailure(BAD_REQUEST)` | Precise mismatch message | No Agent launched |
| Explicit model selector with only default-model Agent entries | `InputFailure(BAD_REQUEST)` | No configured model match | Zero Agent calls; do not discover or overwrite the default |
| Invalid Agent TOML / unknown harness | `ConfigFailure(CONFIG_ERROR)` | Daemon startup/doctor reports invalid config | No Agent launched |
| `agy` not installed / wrong executable | Provider `ExecutionFailure` | Generic selected-provider failure if all fail | No other selected backend fallback |
| Auth missing/expired or invalid model | Provider `ExecutionFailure` | Existing all-provider failure if all fail | Reap child, no file |
| CLI exits 0 but `status != SUCCESS` | Provider `ExecutionFailure` | Existing all-provider failure if all fail | Do not parse `response` |
| Exit 0 and `status=SUCCESS` but empty response | Provider `ExecutionFailure` | Existing all-provider failure if all fail | No result file |
| Missing or malformed JSON terminal envelope | `ProtocolFailure(PROTOCOL_ERROR)` inside provider, then aggregate | Existing all-provider failure if all fail | No raw output leakage |
| Markdown grammar invalid | Existing `ParserFailure` inside selected pipeline, then aggregate | Existing all-provider failure if all fail | No URL admitted from failing invocation |
| One provider fails, another succeeds | Provider isolation | Successful results only | Commit only survivors |
| One `all` branch fails and the other completes | Existing branch partial success | Mixed file with surviving rows | No result rows from failed branch |
| Both `all` branches fail | `ExecutionFailure(ALL_PROVIDERS_FAILED)` | Error | No new file |
| Per-process timeout / oversized output | Provider execution failure | Existing all-provider failure if all fail | Kill process group and reap |
| Whole Agent request exceeds workflow deadline | `ExecutionFailure(ALL_PROVIDERS_FAILED)` with safe timeout message | Error | Cancel all children, no file |
| External cancellation / daemon stop | Propagate `CancelledError` | Existing daemon cancellation behavior | Kill process group, cleanup temp directory |
| Filesystem write/collision/error | Existing writer/daemon failure path | Existing sanitized error | Writer rolls back newly created file |

The classification shown for a provider failure is internal; the existing `SearchOrchestrator` aggregation determines the final result. A `ProtocolFailure` from one invocation should not override an otherwise successful sibling.

### 3. Input, Selection and Configuration Errors

#### 3.1 Prompt / Scope

- CLI trims the prompt as it does for `llm-search`. Reject empty input with `EMPTY_QUERY`.
- `--scope` accepts exactly `web`, `paper`, `all`. On direct/internal requests, revalidate scope before any subprocess is started.
- Preserve the ordering of current LLM checks: request ID, prompt, scope, then invocation selection.

#### 3.2 Provider and Model Selectors

Agent selector semantics mirror current LLM ones, but only against `search_agent.providers`. An omitted invocation model is valid and remains `None`. Without a model selector, default-model entries are eligible (including under a provider-only selector); with an explicit model selector they never match. Match only explicit configured strings, without asking Antigravity for its default or changing an invocation. Proposed messages:

- `No Agent search providers are configured` — empty Agent invocation list (`NO_AGENT_SEARCH_PROVIDERS`).
- `No Agent search invocation matches provider 'P'` — provider only unmatched.
- `No Agent search invocation matches model 'M'` — model only unmatched.
- `No Agent search invocation matches provider 'P' or model 'M'` — both absent.
- `No Agent search invocation matches provider 'P' with model 'M'` — both exist separately but not as one invocation.
- `Provider must not be empty` / `Model must not be empty` — explicitly blank selectors.

No Agent invocation means a no-provider error even when selectors are present, matching the current LLM no-provider priority. A provider defined under `agent_providers` but not referenced by `search_agent.providers` is **not** selectable.

#### 3.3 Agent Config at Startup

Reject malformed selected Agent configuration before the daemon listens:

- `search_agent.providers` not an array of tables; missing/blank/non-string `provider`.
- If a `model` key is present, its value must be a nonempty string; blank/whitespace-only/non-string values (including explicit `None` in the resolver input) are invalid. An omitted key is valid and resolves to `None`, without inheriting any LLM model.
- Referenced provider alias missing from `agent_providers`.
- Missing/unknown `harness` (only `antigravity` is supported).
- Invalid `command` (must be a nonempty executable name/path, not a shell pipeline or argument list).
- Nonpositive/nonfinite/not-an-integer `max_concurrency`; invalid `timeout_seconds` (positive bounded number, max 540).
- Invalid `pass_env` type or any blank/invalid environment variable name; values must be **names**, not credential contents.
- Unknown/incorrect types for recognized Agent configuration fields.

Do not change existing `llm_providers`, `search_llm`, `fetch_llm` validation. Do not contact the Harness or network at startup or in `doctor`. A missing executable can be diagnosed locally when the runner is actually invoked; do not break unrelated commands merely because `agy` is not installed.

### 4. Antigravity Process Errors

#### 4.1 Spawn and Invocation

The runner builds an argv array using `asyncio.create_subprocess_exec`; no `shell=True`. Launch with:

```text
agy -p <serialized system/user prompt> \
    --output-format json --print-timeout <bounded duration>
```

Append `--model <configured model>` only if the selected invocation explicitly pins a model. Otherwise omit both the flag and its value, and let Antigravity use the user's saved model. Do not pass empty strings, `None`, or an invented default slug. Gateway still requires JSON output and an explicit bounded CLI timeout for its parsing/deadline contract. If the Harness fails because its saved default is missing or unusable, report a provider failure rather than choosing an LLM model or retrying another model.

No `--continue`, `--conversation` or `--dangerously-skip-permissions`. Each invocation gets a temporary cwd and curated environment. It uses user-configured Harness tools/permissions rather than trying to control them.

- `FileNotFoundError`, `PermissionError` or other safe OS spawn failure -> classify `spawn_failure`; do not echo OS exception paths/secrets verbatim.
- Nonzero exit -> classify `nonzero_exit`. For example missing authentication or unknown model; do not parse or emit raw CLI diagnostics.
- Maintain compatibility with the official CLI's `status` vocabulary (`SUCCESS`, `ERROR`, `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING`, `RUNNING`). Only `SUCCESS` is valid for a completed result; all others become `terminal_status_failure`.
- A process that exits 0 after a tool was soft-denied may still emit `SUCCESS` with a response; that response is accepted **only as model text**, not as evidence that the tool ran. It still must pass the strict Markdown parser.

#### 4.2 Terminal JSON and Markdown Parsing

- Require UTF-8 terminal stdout containing a single JSON object; reject extra text, missing `status`, missing/empty/non-string `response`, invalid status, and invalid JSON.
- Ignore metadata such as `conversation_id`, `usage` and `duration_seconds` for the search contract; never persist session IDs.
- Preserve the original `parse_search_markdown` behavior, including rejection of invalid URLs/blocks and discarding empty abstracts. Do not add a fallback URL regex.
- Preserve strict `parse_paper_markdown` shape and existing aggregator validation. For an Agent call only, pass `source_kind="agent"`; existing LLM uses its default `"llm"`.
- If a provider produced valid but empty web rows under existing parser rules, count that invocation as completed successfully, consistent with current LLM behavior.
- Paper hits rejected by aggregation do not trigger a whole branch failure when parsing itself completed successfully, consistent with current `llm-search`.

#### 4.3 Timeout, Cancellation and Process Reaping

Use three independent bounds:

| Layer | Proposed default | Rule |
|---|---:|---|
| Per `agy` invocation | 300 seconds | Configurable to at most 540; explicitly send CLI `--print-timeout` and add a short Python hard deadline |
| Complete `agent-search` workflow | 1,140 seconds | Cancels queued and running jobs, including both `all` branches |
| CLI-to-daemon socket for `AgentSearchRequest` | 1,200 seconds | Leaves room for error delivery/process cleanup; all existing commands keep 300 seconds |

- Acquire Agent per-provider quota **before** launching the child and release after reaping.
- On `TimeoutError`, mark the invocation failed and terminate the child's process group; after bounded graceful termination use forced kill and await process reaping.
- On `asyncio.CancelledError`, perform the same cleanup and re-raise rather than treating cancellation as a normal failed provider.
- On daemon shutdown, the existing 10-second grace period applies; after daemon cancellation all Agent children must be terminated and reaped.
- On oversized stdout/stderr, terminate and reap before reporting `output_limit`. Do not use unbounded `communicate()` buffering.
- Place temporary working directory removal in `finally`; ensure it is not deleted until the child has terminated.
- A process timeout must never become a success because partial stdout happens to resemble a valid Markdown result.

#### 4.4 Bounded Output and Secret-safe Diagnostics

Proposed adapter limits: stdout up to **4 MiB**, stderr up to **256 KiB**. Read both streams concurrently to prevent pipe deadlock; reject overflow rather than silently truncating terminal JSON.

The safe `model` metadata is the configured string or `None` (rendered as `model=null` by the existing formatter). It does not claim to identify the Harness's effective default; do not read settings or call `agy models` to fill it.

Log only these categories (or equivalent fixed codes): `spawn_failure`, `nonzero_exit`, `terminal_status_failure`, `invalid_json`, `empty_response`, `invalid_markdown`, `timeout`, `output_limit`, `cancelled`. Do not log raw text even at DEBUG level.

**Trade-off:** `agy -p TEXT` necessarily places the research prompt in subprocess argv, which might be visible in local process inspection while the child runs. This is an explicit limitation of choosing minimal non-interactive `-p` integration; switching to streaming stdin is a separate future hardening effort. Do not use a shell to construct argv.

#### 4.5 Process Environment Boundary

- Preserve basic execution/auth-cache variables needed for installed `agy` (e.g. `PATH`, `HOME`, `XDG_CONFIG_HOME` / `XDG_CACHE_HOME`, temp and locale).
- Do **not** pass arbitrary inherited `OPENAI_API_KEY`, `TAVILY_API_KEY` or other Gateway provider secrets by default.
- Allow additional **explicitly named** `pass_env` variables when required by the user's Harness setup.
- If the required Harness authentication cannot be located with the curated environment, fail clearly as a provider execution error instead of silently expanding the environment to all daemon secrets.
- A private cwd and environment whitelist reduce accidental exposure but do not sandbox arbitrary tools or prevent an Agent whose own permissions allow absolute file access.

### 5. Aggregation, Results and Source Attribution

- Preserve configured invocation order even if completion order differs; duplicate configured provider/model entries, including repeated `(provider, None)` entries, remain separate executions as for LLM.
- Web: commit valid nonempty-abstract results in first-seen order; URL deduplication stays in `SearchOrchestrator` / `URLStore`.
- Paper: keep `finalize_paper_hits` shared. Agent source is `agent:<provider>`; extend generated-source precedence without changing existing `llm:` ordering or strong identifier matching.
- For a selected invocation's invalid Markdown, no records from that invocation enter the shared store. Other successful invocations may still commit.
- `--scope all` uses independent branch outcomes; if only one branch succeeded, write only that branch's rows. Both branches failing -> `ALL_PROVIDERS_FAILED` and no file.
- The writer must not create result files for invalid input, no providers or complete failure. Any partial write failure retains the existing unlink-on-failure behavior.
- Update request ID collision detection to include `agent` alongside `keyword`, `llm` and `paper`.

### 6. Compatibility / Non-regressions

- Do not change existing `llm-search` failure messages, `NO_LLM_SEARCH_PROVIDERS` code, fetch errors, selector matching, call ordering, or stdout.
- Do not replace the `LLMClient` protocol or make Agent run through `LLMClient.complete_json`.
- Do not modify current HTTP retry policies for LLM requests or assume those retries apply to an entire Agent process. **No automatic whole-Agent retry in v1**; repeated Agent calls can incur extra charges/side effects.
- No new network checks in `doctor`. Config shape errors remain deterministic and local.
- Keep the existing `url-fetch` unavailable/safety/content-clean logic as-is.

### 7. Review Checklist

The corresponding testing spec must cover input/config errors, soft-denied tools with exit code zero, envelope errors, parser failures, source attribution, partial success, timeout/kill/reap, stdout/stderr limits, secret filtering, empty/missing Agent config and exact LLM backwards compatibility.

This error design was approved together with the architecture and testing specs on 2026-10-22; the optional-model rules were revised with the plan at the user's request on 2026-10-23.
