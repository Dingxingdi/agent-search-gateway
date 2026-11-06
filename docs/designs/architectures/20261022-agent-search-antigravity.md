## Architecture: Agent Search with Antigravity CLI

**Status:** Implemented and offline-verified, including optional model selection. Live Web, Paper, and both-branch All succeeded with `claude-sonnet-4-6` on 2026-11-06. With `agy 1.5.2`, unpinned default-model Web (71.8 seconds) and pinned `gemini-3.8-flash-high` Web (51.7 seconds) also passed without Gateway HTTP calls. A separate Gemini Harness trace observed `search_web` and terminal `SUCCESS`. Gemini Paper/All, broad Paper latency, sandbox-outside denial, and live follow-up fetch remain unverified; per-result source access and general retrieval accuracy were not independently established. See the implementation plan's 2026-11-06 records.
**Date:** 2026-10-22
**Last revised:** 2026-11-06
**Base revision:** `fe72cc1` (`main`).
**Companion documents:** [Error handling](../error-handlings/20261022-agent-search-antigravity.md) and [Testing](../testings/20261022-agent-search-antigravity.md).

### 1. Scope & Assumptions

#### In Scope

- Add a separate `agent-search` CLI command supporting `--scope web|paper|all` (default `web`), `--provider` and `--model`.
- Support only the **Google Antigravity CLI** (`agy`) in non-interactive mode, using the user's saved Antigravity model by default; an invocation may explicitly pin a model. Every invocation starts a fresh Agent session.
- Reuse the existing `llm-search` search prompts, restricted Markdown grammars/parsers, selected-invocation fan-out, partial-failure policy, web URL admission/deduplication, paper aggregation/OA enrichment, and JSONL writers.
- Retain `LLMStages` for its six current methods and temporarily add two Agent search methods there, as explicitly agreed. Keep Antigravity subprocess code in a separate provider adapter, outside `LLMStages`.
- Create `providers/llm/` and `providers/agent/`; move the existing `providers/openai_chat.py` into `providers/llm/openai_chat.py` without changing API behavior.
- Keep `fetch_llm` mandatory. `search_llm.providers` may already be empty; independently allow `search_agent.providers` to be absent/empty.
- Add only the minimum new request, configuration, result-kind, source-attribution, timeout and process-management support.

#### Todo / Out of Scope

- Implementing OpenAI Responses, Anthropic Messages, other LLM protocols, Codex, Claude Code, other Harnesses or Agent SDK/remote APIs.
- A new `AgentSearchOrchestrator`, generic public plugin loader, LLM transport registry rebuild or restructuring `LLMStages` now.
- Calling Gateway web providers from the Harness; managing the Harness's MCP, browser, tools, permissions or conversation history.
- Replacing the strict Markdown grammar with JSON, fetching/verifying every Agent result URL, changing JSONL row schemas, or making the four `fetch_llm` stages optional.

#### Baseline Assumptions (Verified Against Pre-implementation Source)

- `LLMStages.llm_search_markdown` / `llm_paper_search_markdown` construct messages and call an `LLMClient`; parsers are invoked by `SearchOrchestrator`, not by `LLMStages`.
- `LLMStages` also implements `judge` / `safety` / `content_clean` / `focus_summary` for existing keyword/fetch workflows.
- `config.py::_search_invocations` accepts an empty search list; `_fetch_invocations` still resolves all four fetch stages.
- `SearchOrchestrator.llm_search` owns the web, paper and concurrent all-scope paths; the paper path already calls `finalize_paper_hits`.
- `paper_search_parser.py` sets `source="llm:<provider>"`. `PaperAggregator` gives `llm:` sources special generated-source precedence.
- `request_ids.py` and `ResultWriter` know only `keyword` / `llm` / `paper` file names. `protocol.py` defaults to 300 seconds for a socket response.
- Installed `agy --help` supports `-p`, `--model`, `--output-format json` and `--print-timeout`. Antigravity's [official Headless Mode documentation](https://www.antigravity.google/docs/cli/headless/) specifies terminal `status` and `response` fields and notes that a soft-denied tool may still produce exit code zero.
- Antigravity's [official CLI reference](https://www.antigravity.google/docs/cli/reference/) documents the persisted `/model` choice and the per-session `--model` flag. Gateway leaves an omitted invocation model unresolved and lets the child CLI load the user's saved choice; it does not read or modify Antigravity settings.
- Internal Python imports and socket frames are not stable public interfaces, but documented CLI/TOML/stdout/JSONL contracts are; see `docs/public-interface.md`.

---

### 2. Architecture Summary

Preserve the existing daemon, `SearchOrchestrator` and `LLMStages`. Add the independent `agent-search` command and Agent invocation/config namespace, then extract shared **text-search** orchestration from `SearchOrchestrator.llm_search`. The shared path builds the same Web/Paper prompts, executes each configured invocation through a supplied asynchronous generation function, parses Markdown, aggregates results and writes JSONL. The only backend-specific step differs: existing LLM methods use `LLMClient.complete_text`, while new Agent methods in `LLMStages` use an injected map of **Agent runner callables**, whose first implementation invokes one fresh `agy -p` process and returns the terminal JSON `response`. Agent code cannot access URLStore or paper aggregation. Existing fetch LLM stages, LLM transport behavior and result schemas remain untouched.

```text
llm-search CLI        agent-search CLI
       \                 /
        NDJSON -> daemon -> SearchOrchestrator
                           |
                 shared text-search pipeline
               /                          \
LLMStages.llm_*_markdown       LLMStages.agent_*_markdown
       |                                  |
existing LLMClient           configured Agent runner callable
       |                                  |
OpenAI Chat HTTP                   agy CLI process
               \                          /
                     Markdown text
                          |
           existing Web/Paper parsers
                          |
       URLStore / PaperAggregator / OA enrichment
                          |
          ResultWriter (llm-* or agent-*)
```

---

### 3. Design Decisions

#### Runtime Model

##### Reuse one SearchOrchestrator, separate its command entry points

- **Description:** Keep `llm_search()` and add `agent_search()`. Both delegate into a small internal shared search workflow; each uses its own configured invocation tuple and generation functions.
- **Rationale:** The bulk of the existing implementation is backend-independent.
- **Trade-offs:** `SearchOrchestrator` has two public methods and one generic helper; existing LLM error messages/order must be preserved.
- **Rejected alternatives:** A separate Agent orchestrator duplicates pipeline logic; disguising Agent as an LLM provider conflates configuration namespaces.

##### Keep fetch LLM requirements unchanged

- **Description:** Add optional `ResolvedAgentConfig` to `ResolvedConfig`. Do not relax `fetch_llm` requirements, alter existing `ResolvedLLMConfig`, or initialize unrelated protocols.
- **Rationale:** The user explicitly requires the LLM-assisted content-processing features to stay configured.
- **Trade-offs:** A daemon serving only Agent Search still needs valid `fetch_llm` configuration.
- **Rejected alternative:** Lazy/optional fetch stages unnecessarily change existing keyword/fetch behavior.

#### Interface / Protocol

##### Independent typed request and selectors

- **Description:** Add `AgentSearchRequest(prompt, scope="web", provider=None, model=None)`; extend CLI, codec and daemon dispatch. Reuse the existing exact, case-sensitive, provider/model intersection policy but only against `[[search_agent.providers]]`. A model selector matches only an explicitly configured model; an invocation with `model=None` remains eligible when no model selector is supplied. Selectors do not overwrite invocation models or discover the Harness default. `SearchScope` may be a shared alias for the same three literals while preserving existing `LLMSearchScope` compatibility.
- **Rationale:** Separate public commands and backend namespaces are explicit and testable.
- **Trade-offs:** Mechanical additions to several dispatch/type branches.
- **Rejected alternative:** Encode Agent intent in `LLMSearchRequest` or user prompt text.

##### Preserve successful result shapes and stdout contract

- **Description:** `agent-search` prints only the absolute path to `agent-<request_id>.jsonl`. Web rows have exactly `url`/`abstract`; paper rows retain the existing paper schema; mixed rows retain the existing `type` discriminator, Web first.
- **Rationale:** Reuses consumers and makes all Agent URLs available to existing `url-fetch` after admission.
- **Trade-offs:** Web rows do not expose Harness provenance or tool traces, matching existing LLM rows.
- **Rejected alternative:** Store an Agent transcript or add novel fields to public result rows.

#### State Management

##### Start a fresh headless process per invocation

- **Description:** No `--continue`, `--conversation`, persistent session ID, or long-lived stdin streaming. `--scope all` launches independent Web/Paper tasks; provider quotas may serialize process execution.
- **Rationale:** No shared conversational state or recovery protocol is needed.
- **Trade-offs:** Duplicate startup time and possibly two Agent invocations per `--scope all` request.
- **Rejected alternative:** Session reuse adds lifecycle and state semantics absent from the current gateway.

#### Storage / Persistence

##### Extend existing writer and request-ID kind

- **Description:** Add `agent` to `ResultKind`, file-name collision checks and applicable paper/mixed writer kinds; give the existing mixed writer a `kind="llm"` default so old calls remain unchanged.
- **Rationale:** Reuses atomic `open("x")`, serialization and failure rollback.
- **Trade-offs:** A few type unions and tests expand.
- **Rejected alternative:** A dedicated Agent writer would duplicate core serialization code.

#### Provider Integration

##### Organize adapters by family

- **Description:** Move `providers/openai_chat.py` to `providers/llm/openai_chat.py` and adjust relative imports/tests. Add `providers/agent/__init__.py` and `providers/agent/antigravity_cli.py`; leave shared `providers/contracts.py` and `providers/http.py` where they are.
- **Rationale:** Mirrors `providers/web/` and `providers/academic/` and supports future protocols/Harnesses without building them now.
- **Trade-offs:** A small import-path migration; internal imports are explicitly not public.
- **Rejected alternative:** Reorganizing all provider code or adding compatibility shims for undocumented Python module paths.

##### Temporarily add Agent methods to LLMStages without coupling its transport

- **Description:** Introduce `agent_search_markdown()` and `agent_paper_search_markdown()`. Inject an optional mapping of Agent runner **callables** keyed by alias, separate from existing `Mapping[str, LLMClient]`. A runner accepts `(model: str | None, messages: Sequence[ChatMessage])` and returns `str`; `None` means defer model selection to the Harness. `LLMStages` builds the already-existing prompt messages and calls the selected runner. Do not require Agent backends to implement `LLMClient.complete_json` or expose CLI specifics inside `LLMStages`.
- **Rationale:** Honors the user's planned later `LLMStages` rebuild while retaining a narrow future extraction seam.
- **Trade-offs:** The class remains mixed-responsibility temporarily, and its Agent methods should not emit `llm_stage_*` logging names.
- **Rejected alternatives:** Add `AgentClient` classes / separate `SearchStages` now (premature structure); put subprocess code directly into `LLMStages` (future Harness coupling).

##### Keep parsing and paper aggregation shared

- **Description:** The Antigravity runner returns only the raw `response` Markdown. Reuse `parse_search_markdown` and `parse_paper_markdown`. Add a backwards-compatible `source_kind="llm"` argument to the paper parser; Agent calls use `source_kind="agent"`. Teach `PaperAggregator` to treat both `llm:` and `agent:` as generated sources and include configured Agent priorities.
- **Rationale:** Prevents misleading `llm:antigravity` provenance while retaining existing paper precedence and OA behavior.
- **Trade-offs:** Small parser/aggregator changes need focused compatibility tests.
- **Rejected alternatives:** Fake `llm:` source labels or duplicate the paper pipeline.

##### Minimal first-party Harness factory

- **Description:** The first internal factory maps `harness="antigravity"` to a bound `run_antigravity` callable. Future Harnesses can extend this mapping/config validation without touching the search pipeline. Do not implement a generic public plugin registry now.
- **Rationale:** Clear extensibility for actual future Harness requirements with low present code cost.
- **Trade-offs:** Adding an adapter still requires a small composition-root/config change.
- **Rejected alternative:** Dynamic plugin loading and registry frameworks without a second implementation to validate the abstraction.

#### Configuration

##### Independent Agent namespace and invocation list

- **Description:** Add `[agent_providers.<alias>]` and `[[search_agent.providers]]`. The provider has `harness="antigravity"`, optional `command="agy"`, positive `max_concurrency` (default 1), optional `timeout_seconds` (default 300; max 540), and optional `pass_env` containing names of extra variables to pass. The invocation requires `provider` and optionally accepts `model`. Omission resolves to `AgentInvocation.model=None`; an explicitly present model must be a nonempty string (blank/non-string values are invalid, not a request for the default). No Agent config or empty `search_agent.providers` is valid.
- **Rationale:** Does not alter current `llm_providers`, `search_llm` or `fetch_llm` configuration; supports aliases and model filtering.
- **Trade-offs:** A separate small config parser/validation path is needed. With no pinned model, changes to the user's saved Antigravity model apply to later CLI invocations; Gateway does not snapshot or discover that model.
- **Rejected alternative:** Store executable and model in the LLM transport config, or accept arbitrary shell command strings.

```toml
# Existing fetch_llm remains mandatory and unchanged.
[agent_providers.antigravity_main]
harness = "antigravity"
command = "agy"
max_concurrency = 1
timeout_seconds = 300
# pass_env = ["OPTIONAL_HARNESS_ENV_NAME"]

[[search_agent.providers]]
provider = "antigravity_main"
# Omit model to use the model saved in Antigravity.
# model = "gemini-3.1-pro-high"  # Optional pin for this invocation.
```

In the implemented schema, `provider` must still be configured; omitting `model` does not auto-create an invocation or inherit `global_default_llm`. An explicit model identifier should be checked by the user against the installed `agy models`; neither `doctor` nor startup performs model discovery or reads Antigravity settings. Gateway passes an explicit model only for that invocation and never changes the saved default.

#### Concurrency / Scheduling

##### Separate Agent quotas and bounded execution

- **Description:** Add an optional Agent quota map to `ProviderQuotaManager` and lease it for each subprocess. Keep existing `asyncio.gather(..., return_exceptions=True)`, ordered results and partial success semantics. Default Agent execution timeout is 300s (per invocation, max config 540s); whole Agent Search request has an 1,140s deadline; Agent socket response deadline is 1,200s. Existing non-Agent socket calls retain 300s.
- **Rationale:** `--scope all` may run two processes sequentially when concurrency is 1; the existing 300s CLI socket limit would otherwise expire first. The total deadline also bounds a large fan-out.
- **Trade-offs:** Many slow selected invocations may time out rather than all complete; limits should be documented.
- **Rejected alternatives:** Share LLM quotas by name (not independent), or allow unlimited Agent processes and blocking requests.

#### Security

##### Opaque Harness with a controlled process boundary

- **Description:** The adapter uses `asyncio.create_subprocess_exec` without a shell, a fresh private temporary cwd, a controlled environment (basic `PATH`/`HOME`/`XDG_*`/locale plus explicitly named `pass_env` values) and an isolated process group/session for cancellation cleanup. Do not pass `--dangerously-skip-permissions`; do not modify user Agent/MCP/tool settings. Process environment filtering and a temp cwd are **not** a security sandbox. Cleanup retains the quota through a 2-second termination grace, a 2-second combined pipe drain/reap attempt, and (if needed after transport closure) a final 2-second wait. Signal errors or escaped descendants must not block the request indefinitely; a cleanup failure preserves an existing invocation error/cancellation and otherwise prevents success. See the error design's process-reaping contract for OS-permission and escaped-process limits.
- **Rationale:** Avoid accidental API-key exposure from the Gateway daemon and accidental repository writes while leaving the Harness a black box.
- **Trade-offs:** Some Harness setups require explicit extra environment variable names.
- **Rejected alternatives:** Inherit every Gateway API key, execute a shell string, or silently elevate tool permissions.

##### Parse CLI envelope and require success

- **Description:** Use `agy -p TEXT --output-format json --print-timeout <duration>`; append `--model MODEL` only when `AgentInvocation.model` is not `None`. JSON output and the explicit bounded timeout remain Gateway adapter requirements even though both flags are optional in the CLI. Accept only exit status 0, JSON `status="SUCCESS"` and a nonempty string `response`; then apply the existing strict Markdown parser. Bound captured stdout/stderr and never log prompt or raw CLI output.
- **Rationale:** Antigravity headless tool soft-denial can still yield exit code 0; status and parser checks are mandatory.
- **Trade-offs:** Correct output grammar cannot prove sources were genuinely accessed; source verification is outside the request.

#### Observability

##### Keep existing event semantics

- **Description:** Leave all existing `llm_stage_*` and LLM provider logs unchanged; Agent search records `provider_started/completed/failed` under `stage=agent_search` / `agent_paper_search`, with provider, the configured model (or `model=null` when delegated to Antigravity), elapsed time and outcome category, not raw prompts/CLI stderr. The model field records an optional pin, not a discovered effective model.
- **Rationale:** Regression tests remain meaningful; Agent execution can be diagnosed without leaking sensitive content.
- **Trade-offs:** No full internal tool-call trace in Gateway logs.
- **Rejected alternative:** Store raw Agent transcripts inside Gateway diagnostics.

#### Future Migration

##### Defer LLMStages split, keep seams small

- **Description:** Agent logic in `LLMStages` depends only on the callable signature. In a later project, search methods can move to `SearchStages` and content-processing stages to `FetchLLMStages` with no Harness rewrite. Existing OpenAI transport remains independent of future Responses/Messages.
- **Rationale:** Avoids doing the planned separate refactor before it is necessary.
- **Trade-offs:** Temporary class cohesion issue is explicitly accepted by the user.
- **Rejected alternative:** Build extensible class hierarchies or implement additional protocols to justify an abstraction now.

---

### 4. Component Catalog

| Component | Purpose | Responsibilities | Interface | Dependencies | Owns State? | Role |
|---|---|---|---|---|---|---|
| CLI (`cli.py`) | New public command | Scope/selector parsing | `agent-search` | Socket client | No | Entry |
| `models.py` / `protocol.py` | Typed internal request | Encode/decode `agent_search` | `AgentSearchRequest` | stdlib | No | Boundary |
| `daemon.py` | Dispatch and shutdown | Route, reserve IDs, cancel tasks | `_invoke_workflow` | Runtime | Active tasks | Coordinator |
| `config.py` | Resolve Agent configuration | Validate aliases, model/limits | `ResolvedAgentConfig` | env names | No | Validator |
| `runtime.py` | Compose runners and quotas | Bind Antigravity factory, pass to stages/orchestrator | `Runtime.build` | Config | Clients/runners | Composition root |
| `providers/llm/openai_chat.py` | Preserve existing HTTP backend | Chat Completions calls/retry/JSON | `LLMClient` | HTTP executor | HTTP client | Adapter |
| `providers/agent/antigravity_cli.py` | Opaque Agent invocation | Serialize messages, subprocess, terminal envelope, cleanup | async runner `(model, messages) -> str` | stdlib subprocess | Per-call process | Adapter |
| `LLMStages` | Existing stages + temporary Agent facade | Existing LLM methods, new two Agent search methods | `llm_*_markdown` / `agent_*_markdown` | Prompt builders, client/runner maps | Maps | Facade |
| `SearchOrchestrator` | Shared search | Select, gather, parse, merge, write | `llm_search` / `agent_search` | Stages, store, writer | Invocation tuples | Coordinator |
| Prompt builders | Web/Paper text format | Build role messages | `llm_search_messages` / `llm_paper_search_messages` | None | No | Transformer |
| Markdown parsers | Validate search output | Records/hits + source tagging | `parse_search_markdown` / `parse_paper_markdown` | URL normalizer | No | Validator |
| `PaperAggregator` + OA resolver | Paper dedup/enrichment | Generated-source precedence, OA, URL admission | `finalize_paper_hits` | URLStore | No | Transformer |
| `URLStore` | Existing admitted URLs | Admit Agent Web/Paper URLs for `url-fetch` | `admit` | NormalizedURL | Daemon memory | Store |
| `ResultWriter` / `RequestIdRegistry` | Save results | Agent filenames, collision/atomicity | Existing writers + `kind` | Filesystem | Files/IDs | Sink |
| `ProviderQuotaManager` | Protect concurrency | Agent quota namespace | `get_agent` | CapacityGate | Lease state | Scheduler |

The Agent adapter must not import `URLStore`, `PaperAggregator`, `SearchOrchestrator` or `ResultWriter`. LLM transports must not depend on Agent modules.

---

### 5. Data Flow

#### 5.1 Daemon Startup (Existing `start` Command, Extended Config)

```python
raw = load_toml(config_file)
cfg = resolve_config(raw, ...)             # existing fetch_llm still required
if invalid_agent_reference_or_harness_or_limits:
    raise ConfigFailure(CONFIG_ERROR)      # no network / "agy models" call
runtime = Runtime.build(cfg, paths)
llm_clients = build_existing_llm_clients(cfg.llm)
agent_runners = build_agent_runner_callables(cfg.agent)  # empty if no config
stages = LLMStages(
    llm_clients, existing_fetch_stages, agent_runners=agent_runners
)
search = SearchOrchestrator(
    ..., llm_invocations=cfg.llm.search_invocations,
    agent_invocations=cfg.agent.search_invocations,
    stages=stages, store=shared_store, result_writer=shared_writer
)
# Absent agent config does not affect old commands.
```

#### 5.2 `agent-search` Web (Default)

```python
# CLI -> Socket
request = AgentSearchRequest(prompt, scope="web", provider=selector, model=selector)
response = await send_request(socket, request)  # Agent-specific bounded deadline

# Daemon
with reserve_request_id(may_write_search_result=True) as request_id:
    path = await runtime.search_orchestrator.agent_search(
        request.prompt, scope=request.scope, provider=request.provider,
        model=request.model, request_id=request_id
    )

# SearchOrchestrator shared path
if not prompt.strip(): raise InputFailure(EMPTY_QUERY)
if scope not in ("web", "paper", "all"): raise InputFailure(BAD_REQUEST)
selected = select_only_agent_search_invocations(provider, model)
# helper: absent config -> NO_AGENT_SEARCH_PROVIDERS; bad selectors -> BAD_REQUEST
outcomes = await gather(
    run_web_generation_and_parse(inv, stages.agent_search_markdown)
    for inv in selected
, return_exceptions=True)
if no successful list outcomes: raise ExecutionFailure(ALL_PROVIDERS_FAILED)
for hit in successful_outcomes_in_original_invocation_order:
    store.admit(hit.url, hit.abstract)
    append_first_occurrence(hit.url)
path = writer.write_results("agent", canonical_records, request_id)
return str(path)

# Generator branch
messages = llm_search_messages(prompt)
markdown = await configured_runner(inv.model, messages)
records = parse_search_markdown(markdown)
```

#### 5.3 `agent-search --scope paper`

```python
selected = select_only_agent_search_invocations(provider, model)
outcomes = await gather(
    run_paper_generation_and_parse(inv, stages.agent_paper_search_markdown)
    for inv in selected
, return_exceptions=True)
if no successful list outcomes: raise ExecutionFailure(ALL_PROVIDERS_FAILED)
hits = []
for result in outcomes_in_original_invocation_order:
    if isinstance(result, list):
        hits.extend(result)

# Each runner executes exactly once, then parses exactly once:
async def run_paper_generation_and_parse(inv, generate):
    markdown = await generate(inv, prompt)
    return parse_paper_markdown(
        markdown, provider=inv.provider, source_kind="agent"
    )

records = await finalize_paper_hits(
    hits, aggregator=shared_aggregator,
    resolver=existing_oa_resolver, store=shared_URLStore
)
path = writer.write_paper_results("agent", records, request_id)
return str(path)
```

#### 5.4 `agent-search --scope all`

```python
try:
    web_outcome, paper_outcome = await gather(
        web_branch(), paper_branch(), return_exceptions=True
    )
except CancelledError:
    terminate_and_reap_outstanding_children()
    raise
if both outcomes are exceptions:
    raise ExecutionFailure(ALL_PROVIDERS_FAILED)
web_rows = [] if web_outcome failed else web_outcome
paper_rows = [] if paper_outcome failed else paper_outcome
path = writer.write_mixed_results(
    web_rows, paper_rows, request_id=request_id, kind="agent"
)
return str(path)
# Branches are scheduled concurrently but per-provider quota 1 may serialize them.
# Successful empty branch remains successful, as with llm-search.
```

#### 5.5 Antigravity Adapter Per Selected Invocation

```python
async def run_antigravity(model: str | None, messages, *, command, timeout, pass_env):
    prompt = encode_chat_roles_as_single_cli_text(messages)
    cwd = private_temporary_workdir()
    argv = [
        command, "-p", prompt,
        "--output-format", "json", "--print-timeout", f"{timeout}s"
    ]
    if model is not None:
        argv.extend(["--model", model])
    # None is never stringified or replaced with a guessed default.
    try:
        process = await create_subprocess_exec(
            *argv, cwd=cwd, env=curated_env(pass_env), start_new_session=True,
            stdout=PIPE, stderr=PIPE
        )
        stdout, stderr = await bounded_communicate(process, timeout=timeout + margin)
        if process.returncode != 0: raise ExecutionFailure(ALL_PROVIDERS_FAILED)
        envelope = strict_json_object(stdout)
        if envelope.get("status") != "SUCCESS":
            raise ExecutionFailure(ALL_PROVIDERS_FAILED)
        text = envelope.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ExecutionFailure(ALL_PROVIDERS_FAILED)
        return text
    except (TimeoutError, CancelledError):
        await terminate_process_group_and_reap(process)  # graceful then kill
        raise
    finally:
        remove_temporary_workdir()
```

#### 5.6 Existing `llm-search` and `url-fetch` Compatibility

```python
await orchestrator.llm_search(prompt, scope, provider, model)
# Shared pipeline invokes existing methods, not an Agent runner:
await stages.llm_search_markdown(invocation, prompt)
await stages.llm_paper_search_markdown(invocation, prompt)
# Result: llm-*.jsonl; paper source: "llm:<provider>".

# No changes to existing keyword or URL fetch stage calls:
await stages.judge(...)
await stages.safety(...)
await stages.content_clean(...)
await stages.focus_summary(...)
```

---

### 6. Interfaces & Contracts

#### Public CLI

```bash
agent-search-gateway agent-search "query"
agent-search-gateway agent-search "query" --scope paper
agent-search-gateway agent-search "query" --scope all
agent-search-gateway agent-search "query" --provider antigravity_main
# Requires a search_agent entry explicitly configured with this model.
agent-search-gateway agent-search "query" --provider antigravity_main --model gemini-3.1-pro-high
```

`--provider` and `--model` perform exact, case-sensitive selection over configured `search_agent` invocations only; missing entries do not trigger fallback. Without selectors, select every configured entry; provider-only selects that alias's default-model and pinned-model entries. An explicit model selector matches only non-`None` configured model strings and excludes default-model entries, even if Antigravity's saved model happens to have the same name. The selector never fills in an omitted invocation model. Duplicate entries, including two default-model entries for the same alias, remain independent calls; no magic `default` model string is introduced.

#### Internal Socket Request

```json
{"type":"agent_search","prompt":"research topic","scope":"paper","provider":"antigravity_main","model":"gemini-3.1-pro-high"}
```

Omit default scope and unset selectors in serialized frames. Successful socket response remains `{"ok":true,"text":"/absolute/path/agent-<id>.jsonl"}`; failure remains the existing `ErrorResponse` shape.

#### Typed Invocation / Runner Boundary

```python
@dataclass(frozen=True, slots=True)
class AgentInvocation:
    provider: str
    model: str | None = None

AgentTextRunner = Callable[
    [str | None, Sequence[ChatMessage]], Awaitable[str]
]
# Inputs: optional pinned model + Web/Paper role messages.
# None delegates to the CLI's saved model; it remains None through runtime/stages.
# Output: nonempty Markdown text satisfying the selected prompt grammar.
# No raw subprocess protocol, Agent session or Gateway store escapes this boundary.
```

Existing `LLMInvocation` and `LLMClient` retain their required string model and remain unchanged. No new concrete `AgentClient` class is required.

#### JSONL Contracts

- Web: `{"url":"https://example.com","abstract":"Nonempty description"}` — exactly two keys.
- Paper: existing `PaperRecord` schema; generated source becomes `agent:<provider>`.
- All: existing mixed schema, Web rows followed by paper rows, each with `type`.
- Result filename: `agent-<eight lowercase hex request id>.jsonl`; collision detection covers all four kinds.
- Agent-reported URLs are admitted via the existing `URLStore`; a subsequent `url-fetch` still uses existing LLM processing.

#### Compatibility / Public Policy

- New documented CLI and TOML keys require README, `config.example.toml`, `docs/public-interface.md` and CHANGELOG updates on implementation.
- Existing CLI output, old config, old `llm`/`keyword`/`paper` result names and exact row bytes stay unchanged.
- `doctor` remains local/no-network; it may validate agent TOML fields but must not authenticate with `agy` or call remote model discovery.
- This spec establishes first-party internal extension seams only, not a public plugin API.

---

### 7. Expected Change Footprint

**Add/move provider files:** `providers/llm/__init__.py`, moved `providers/llm/openai_chat.py`, `providers/agent/__init__.py`, `providers/agent/antigravity_cli.py`. Update the few imports that refer to the old OpenAI module; its HTTP logic is unchanged.

**Mechanical entry-point changes:** `cli.py`, `models.py`, `protocol.py`, `daemon.py`, `config.py`, `runtime.py`, `request_ids.py`, `result_writer.py`, `errors.py`, `concurrency.py`.

**Narrow shared-flow changes:** `orchestrators/search.py`, `llm/stages.py`, `paper_search_parser.py`, `academic/aggregator.py`.

**Business behavior to leave alone:** `orchestrators/fetch.py`, `scheduler/fetch.py`, `URLStore` methods, existing Web/Academic adapters, existing Prompt text, existing OpenAI Chat API behavior.

The 2026-10-23 revision makes only Agent invocation model selection optional and retains the JSON envelope, explicit CLI timeout, selector filtering policy and existing LLM requirements. This approved design governs implementation scope alongside the companion error and test specs; it is not authorization to modify unrelated components.
