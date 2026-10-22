## Testing: Agent Search with Antigravity CLI

**Status:** Approved by user on 2026-10-22; specification only, no new code has been implemented.
**Architecture:** [20261022-agent-search-antigravity.md](../architectures/20261022-agent-search-antigravity.md).
**Errors:** [20261022-agent-search-antigravity.md](../error-handlings/20261022-agent-search-antigravity.md).

### 1. Objectives, Baseline and Strategy

Validate the feature as a **small extension of an already-tested search workflow**, not as a new independent search engine. The highest risks are accidental LLM/Agent provider confusion, loss of existing LLM output compatibility, wrong Agent paper provenance, process lifetime leaks and CLI timeouts.

Pre-change verification on `main` at `fe72cc1` (completed in discussion, not a test of the new feature):

- `uv run pytest -q`: **871 passed, 4 skipped**.
- `uv run ruff check .`: pass.
- `uv run mypy src tests scripts`: pass.

**Primary acceptance requirements:**

1. All preexisting `keyword-search`, `paper-search`, `llm-search` and `url-fetch` behavior remains intact.
2. `agent-search` is an independent CLI / socket request, with `--scope web|paper|all`, `--provider` and `--model`; no hidden LLM search fallback.
3. Both search backends use the *existing exact* Web/Paper prompt grammars, parsers, dedup/merge, URL admission and JSONL row shapes.
4. `fetch_llm` remains mandatory; `search_llm` and `search_agent` lists are independently optional.
5. Only Antigravity is supported in v1, and it uses one non-interactive process per invocation with no resumed session.
6. Agent process cancellation, timeout, error envelope, bounded output and environment restrictions are deterministic and safe.
7. Existing LLM API adapter moves directory without changing transport behavior.
8. All normal CI and unit/acceptance tests run **without real network, tokens or a real Gemini session**.

### 2. Test Layers and Ownership

| Layer | Suggested files | Core assertion | Real network? |
|---|---|---|---|
| CLI | `tests/cli/test_cli.py` | New verb/flags; exact request construction | No |
| Request model/codec | `tests/unit/test_protocol_codec.py`, `test_search_request_selectors.py` | Agent frames and strict validation; legacy frames unchanged | No |
| Configuration | new `tests/unit/test_config_agent.py`, existing `test_config_llm.py` | Optional Agent namespace, required fetch LLM, strict checks | No |
| Runtime composition | `tests/runtime/test_runtime_assembly.py`, new targeted Agent runtime tests | Agent runners injection and independent quotas | No |
| LLMStages | `tests/unit/test_llm_stages.py` + focused Agent tests | Two new Agent methods reuse prompt builders; six existing methods unaffected | No |
| Shared orchestrator | new `tests/orchestrators/test_agent_search.py`, existing LLM tests | Web/Paper/All parity; selectors; no duplicated business rules | No |
| Parsers/aggregation | `tests/unit/test_paper_search_parser.py`, `tests/academic/test_paper_aggregator_*.py` | Agent provenance and old LLM ranking unchanged | No |
| Results/IDs | `tests/unit/test_result_writer.py`, `test_request_ids.py` | Agent file kinds and collision rollback | No |
| Agent adapter | new `tests/providers/agent/test_antigravity_cli.py` | Fake CLI spawn, status parsing, cancel/reap, env/limits | No |
| Daemon/Socket | `tests/daemon/test_daemon_dispatch.py`, `test_daemon_shutdown.py` | Agent route, request deadlines, shutdown | No |
| Acceptance | `tests/acceptance/test_gateway_workflows.py` or focused new test | CLI -> real Unix socket -> daemon -> fake Agent -> JSONL -> url-fetch | No |
| Diagnostics | `tests/doctor/test_config.py` / `tests/observability/test_redaction.py` | Local-only checks and no raw payload logs | No |
| Opt-in real smoke | manual command or opt-in integration module | Verify user's `agy` auth/search/tools/version | Yes, manual only |

Keep existing tests as direct regressions. Do not replace or weaken current strict assertions merely because a shared search helper was extracted.

### 3. CLI, Request Model and Protocol Tests

#### 3.1 Parser / Command Surface

Assert these construct `AgentSearchRequest` with normalized selector values:

```text
agent-search "query"
agent-search "query" --scope web
agent-search "query" --scope paper
agent-search "query" --scope all
agent-search "query" --provider antigravity_main
agent-search "query" --model gemini-3.1-pro-high
agent-search "query" --provider antigravity_main --model gemini-3.1-pro-high
```

- Default scope is `web`.
- Trim leading/trailing whitespace from selectors; explicitly blank values fail before Socket call.
- Reject unsupported `--scope` values and unsupported flags on `url-fetch` / `keyword-search` / `start` / `doctor`.
- Existing `llm-search` CLI selectors remain independent of the Agent namespace.
- Successful stdout is one result-file path, no status text/progress/stdout transcript.

#### 3.2 NDJSON Frame and Typed Round Trip

```json
{"type":"agent_search","prompt":"topic"}
{"type":"agent_search","prompt":"topic","scope":"paper"}
{"type":"agent_search","prompt":"topic","provider":"antigravity_main","model":"gemini-3.1-pro-high"}
{"type":"agent_search","prompt":"topic","scope":"all","provider":"antigravity_main"}
```

Check `encode_request` and `decode_request_frame` produce identical values, omit default scope/unset optional fields, and reject:

- Non-string prompt/provider/model; unknown extra fields; invalid scope.
- Missing required fields; wrong request type.
- Whitespace-only provider/model through the normal orchestration/CLI validation path.
- All existing legacy frames and response frames remain byte-compatible.

Verify `send_request` chooses the longer Agent deadline only for an `AgentSearchRequest` without explicit timeout; existing commands still default to 300 seconds and caller-specified timeouts override the default.

### 4. Configuration / Runtime Tests

#### 4.1 Minimal Config Matrix

| Config | Expected startup | `llm-search` | `agent-search` |
|---|---|---|---|
| Valid mandatory `fetch_llm`; no `search_llm` or `search_agent` | Success | `NO_LLM_SEARCH_PROVIDERS` | `NO_AGENT_SEARCH_PROVIDERS` |
| Valid fetch; LLM search only | Success | Works | `NO_AGENT_SEARCH_PROVIDERS` |
| Valid fetch; Agent search only | Success | `NO_LLM_SEARCH_PROVIDERS` | Works |
| Valid fetch; both search lists | Success | Works | Works |
| Missing/invalid mandatory fetch stages | Existing startup failure | N/A | N/A |
| Invalid referenced Agent harness/config | `CONFIG_ERROR` | N/A | N/A |

This locks down the agreed separation between *optional search backends* and *required fetch LLM processing*.

#### 4.2 Agent Configuration Validation

Parameterize:

- `search_agent.providers` absent, empty and invalid type.
- Referenced alias absent from `agent_providers`; extra unreferenced provider.
- Missing/empty/unknown `harness`, wrong `command` type, shell-looking command text disallowed.
- Missing/empty model, duplicated provider/model entries (valid and **both must run**).
- `max_concurrency` zero, negative, bool, fractional; valid 1 or 2.
- `timeout_seconds` invalid/nonfinite, zero, too high (more than 540), valid default and override.
- `pass_env` invalid array/non-string/empty/invalid environment variable names, valid explicit name list.
- Existing LLM provider config/`extra_body` behavior stays unchanged.

#### 4.3 Runtime Assembly, Provider Independence and Registry Seam

- No Agent config leaves existing runtime construction, HTTP clients and `LLMStages` call behavior intact.
- Agent runtime builds a bound runner for each referenced `harness="antigravity"` alias, with its own quota. No `LLMClient.complete_json` is needed for an Agent.
- Provider alias collision across LLM and Agent namespaces is allowed but selects the correct backend based on command.
- Agent runner is not constructed as `OpenAIChatCompletionsClient` and does not modify its `_build_llm_clients()` behavior.
- Test configured but unselected Agent aliases are not executed; `--provider` is request-local.
- Existing `Runtime.aclose()` closes HTTP resources. Active Agent subprocess cleanup is owned by the running adapter/task; stopping the daemon cancels/reaps it.
- `doctor` validates config without invoking `agy models`, prompting login or using the network.

### 5. LLMStages and Shared Search Tests

#### 5.1 Prompt Equality and Facade

Use fake LLM clients and injected async Agent runners that record passed `(model, messages)`. Assert:

- `LLMStages.agent_search_markdown` passes the **same message sequence** as `llm_search_messages(prompt)`.
- `LLMStages.agent_paper_search_markdown` passes the **same message sequence** as `llm_paper_search_messages(prompt)`.
- Model and provider alias are resolved from the Agent invocation, not the existing fetch/LLM invocation.
- Agent facade returns runner Markdown unchanged except for existing whitespace/nonempty validation contract; it does not parse output itself.
- Agent methods never call `LLMClient`; existing LLM methods never call an Agent runner.
- Existing six `LLMStages` tests and logs stay unchanged.
- Agent-specific logs do not emit `llm_stage_*` events or sensitive prompts.

#### 5.2 Web Search Shared Orchestration

Construct two or more fake Agent invocations returning valid `## Result` blocks. Assert:

- Default and explicit `web` produce the same Web JSONL bytes.
- Duplicate normalized URLs are admitted once and preserve first-seen configured invocation order, **not task completion order**.
- Missing/empty abstracts still use existing parser behavior; no regex-only URL path is introduced.
- Result file is `agent-<id>.jsonl`; each Web row still has exactly `url` and `abstract`.
- The Agent result's URL is admitted to the same `URLStore` and is accessible through unchanged `url-fetch`.
- A malformed result only removes that invocation's records; surviving providers can still return success.
- A valid but empty list is a **successful** invocation under the existing LLM search rules.

#### 5.3 Paper Search Shared Orchestration

Return strict existing `## Paper` grammar from fake Agent backends:

- `parse_paper_markdown(text, provider="X")` still yields `source="llm:X"` by default.
- `parse_paper_markdown(text, provider="X", source_kind="agent")` yields `source="agent:X"` with unchanged metadata validation.
- `PaperAggregator` groups duplicate DOI/arXiv/strong native IDs and preserves the historical LLM precedence; Agent receives generated-source precedence as designed.
- Mixed academic/LLM/Agent candidates retain deterministic priority/order with consistent `sources` and citation counts.
- OA enrichment still runs once on aggregated DOI-bearing papers, and landing pages enter URLStore; PDF URLs are metadata only.
- `agent-search --scope paper` uses `agent-*.jsonl` with the existing paper fields and **no** `type` field.

#### 5.4 All Scope and Partial-Failure Matrix

| Web branch | Paper branch | Expected result |
|---|---|---|
| Success nonempty | Success nonempty | Mixed `web` rows then `paper` rows |
| Success | Failure | Mixed file with Web only |
| Failure | Success | Mixed file with Paper only |
| Failure | Failure | `ALL_PROVIDERS_FAILED`, no file |
| Success empty | Failure | Successful **empty** mixed file |
| Invalid parser | Valid sibling | Sibling survives |
| Task externally cancelled | Any | Cancellation propagates; no child leaks |

Assert both branches are **scheduled concurrently**. With Agent provider quota 2, they can overlap. With default quota 1, one is queued and then executed; both sessions remain independent. No `--continue`/`--conversation` flags and distinct process invocations for the branches.

#### 5.5 Selector Semantics

Mirror the existing `test_llm_provider_model_filtering.py` matrix but for `AgentInvocation`, covering all three scopes:

- No selectors -> all configured Agent invocation entries.
- Provider only -> all configured models for that alias.
- Model only -> all configured aliases using that exact model string.
- Both -> exact intersection.
- Duplicate matching entries -> both executed, in configured order.
- Missing provider, missing model, both absent, provider/model mismatch -> exact `BAD_REQUEST`, **zero** runner calls, no file.
- Empty Agent namespace -> `NO_AGENT_SEARCH_PROVIDERS` (even with invalid selector, same no-provider priority as existing LLM).
- Selecting an LLM-only alias/model with `agent-search` -> no Agent match, **not** an LLM fallback.
- LLM `--provider` and `--model` selection tests remain byte- and behavior-identical.

### 6. Antigravity Adapter Tests Without Real Antigravity

Use a temporary executable fake `agy` program (or injected process factory), invoked with `create_subprocess_exec` on POSIX. Do not require any credentials or network.

#### 6.1 Happy Path

Fake CLI writes terminal JSON:

```json
{"conversation_id":"unused","status":"SUCCESS","response":"## Result\nURL: https://example.com/web\nAbstract: Agent result","usage":{"total_tokens":10}}
```

Assert:

- Arguments include exactly one `-p` prompt, configured `--model`, `--output-format json` and an explicit bounded `--print-timeout`.
- Arguments contain **no** `--continue`, `--conversation`, `--dangerously-skip-permissions` or shell pipeline.
- Prompt contains both the existing system instructions and the user research request, not provider-selector metadata.
- Adapter returns the `response` field rather than JSON envelope or CLI stderr.
- Two invocations use independent temporary cwd and independent processes; directories are removed afterward.

#### 6.2 Terminal Error Matrix

Parameterize subprocess outcomes:

| Fake exit | JSON/output | Expect |
|---:|---|---|
| 0 | `SUCCESS`, valid Markdown | Success |
| 0 | `ERROR`, even with nonempty response | Failure |
| 0 | `WAITING` / `CANCELED` / `INTERRUPTED` / `RUNNING` | Failure |
| 1 | `SUCCESS`, valid response | Failure (exit code also required) |
| 1 | `ERROR`, error field | Failure without leaking raw error |
| 0 | invalid JSON / array / missing status | Failure |
| 0 | empty, null or numeric response | Failure |
| 0 | `SUCCESS` + malformed Markdown | Adapter returns text; shared parser rejects |
| 0 | `SUCCESS` + valid Markdown + permission-denial note on stderr | No claim that tool ran; parse result normally |
| None | missing executable, permission-denied executable | Safe provider failure |

Check that when every selected Agent fails, `agent-*.jsonl` is not produced; when one succeeds, only its results are emitted.

#### 6.3 Environment and Process Safety

- Pass `PATH`, `HOME` / explicit supported XDG and locale variables; do not pass mock `OPENAI_API_KEY` / `TAVILY_API_KEY` by default.
- Explicit `pass_env=["TEST_HARNESS_SETTING"]` passes only the named variable, not unrelated secrets.
- Spawn with a fresh temporary cwd; no mutation of current repo files.
- Prompt containing spaces, quotes, newlines, or shell metacharacters stays a **single argv element** and cannot trigger shell expansion.
- stdout over 4 MiB / stderr over 256 KiB terminates the child and rejects the invocation; no truncated success.
- Fake script hangs or spawns child/grandchild; per-run timeout kills and reaps its process group, leaves no temporary working directory.
- Cancel an in-flight asyncio task and simulate the daemon 10-second shutdown cancellation; verify the process group is reaped and quota lease released.
- Ensure status/error handling never logs raw stderr, JSON, prompt, tokens, secret names' values, or full exception text.

The real CLI binary on the local developer machine is **not used** by ordinary tests.

### 7. Request IDs, Results and Daemon/Socket Tests

#### 7.1 Filename and Persistence

Extend `tests/unit/test_request_ids.py` and `test_result_writer.py`:

- `result_filename("agent", "a1b2c3d4")` -> `agent-a1b2c3d4.jsonl`.
- RequestIdRegistry retries a generated ID already present as any of the four kinds.
- Agent Web, Paper and All writers select `agent` file names; old invocations continue to write `llm`, `paper` or `keyword` unchanged.
- File creation remains exclusive and atomic; a simulated partial serializer or write failure leaves no half-result.
- Default `write_mixed_results(web, paper, request_id=id)` still writes the original `llm` filename and identical bytes.

#### 7.2 Daemon Dispatch and Socket Timing

- `ForegroundDaemon._dispatch` treats Agent as a search request eligible for request-ID reservation.
- `_command_name` is `agent-search`, not `llm-search`; DEBUG events are distinct.
- `_invoke_workflow` forwards prompt, scope, provider, model and request ID to the same `search_orchestrator.agent_search`.
- Real local Unix-socket acceptance test: CLI -> `agent_search` request -> daemon -> fake runner -> real ResultWriter -> path in CLI stdout -> follow-up `url-fetch` from the same URLStore.
- Socket deadlines: old request default 300s; Agent default 1200s; explicit test overrides still honored. Use stub stream/clock to avoid waiting.
- Multiple concurrent Agent requests and a later selector-free request do not share selection or session state.
- `stop` cancels outstanding Agent invocation(s), releases quotas, reaps subprocesses and removes socket as existing daemon tests require.

### 8. Compatibility / CI / Documentation

#### 8.1 Moving the Existing OpenAI Adapter

- Move module `src/agent_search_gateway/providers/openai_chat.py` into `providers/llm/`, adjusting relative imports.
- Move its unit test under `tests/providers/llm/` if consistent with existing directory tests; update integration test import.
- The old `OpenAIChatCompletionsClient` methods, HTTP headers, retry behavior, `extra_body` checks, quotas, and `aclose` semantics must pass the same tests.
- Do not create a new public module/API compatibility promise beyond the documented internal import policy.

#### 8.2 Regression Commands

```bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run pytest -q
uv run python scripts/build_docs.py
```

Also run targeted tests for new Agent code and existing LLM scopes, adapter HTTP, source precedence and daemon shutdown. Default CI remains offline.

#### 8.3 Public Documentation Checks

Update implementation-time README, `config.example.toml`, `CHANGELOG.md` and `docs/public-interface.md` to describe:

- Independent `agent-search` command and flags.
- Mandatory fetch LLM versus optional search LLM / optional search Agent.
- Agent provider alias vs Harness type and model selection.
- Antigravity local `agy` install/login/config responsibility and `agy models` discovery.
- Headless status/permission soft-denial, bounded execution, retained authentication caches/environment, and cwd/process constraints.
- `agent-*.jsonl`, source `agent:<provider>`, and scope-all semantics.
- No promise of guaranteed source verification, no automatic permission approval, no Agent session resume.

#### 8.4 Opt-in Live Smoke (Not Default CI)

Only after explicit operator opt-in and with an authenticated local `agy`:

```bash
agent-search-gateway start --debug
agent-search-gateway agent-search "Find official documentation on Python asyncio cancellation" \
  --provider antigravity_main --model <slug-from-agy-models> --scope web
agent-search-gateway agent-search "Find academic papers on retrieval augmented generation" \
  --provider antigravity_main --model <slug-from-agy-models> --scope paper
agent-search-gateway agent-search "Find papers and reference sites for agent tool safety" \
  --provider antigravity_main --model <slug-from-agy-models> --scope all
```

Replace placeholder slugs before running; these are examples, not verified commands against a live account. Inspect only the newly returned file paths via ordinary shell tooling because direct access to `~/.cache/agent-search-gateway-cli/` may be permission-restricted in this environment. Check valid outputs, source labels and lack of background/orphan processes. Do not automatically run live API tests as part of the design phase.

### 9. Acceptance Gate

The feature is ready for implementation review only after all of the following are true:

- Both old and new search commands can be exercised through real local socket acceptance tests.
- Existing output bytes and error contracts remain unchanged.
- Agent Web/Paper/All produce the documented JSONL rows with correct source attribution.
- No Agent calls occur for invalid/empty selectors or missing search Agent configuration.
- No subprocess remains after timeout, cancellation or daemon stop.
- No unrelated LLM/Web API credentials enter Agent subprocesses by default or appear in logs.
- No real provider API or live Agent credentials are needed for the ordinary test suite.
- Documentation and changelog capture the new public command and configuration surface.

**The user reviewed and approved the three specs on 2026-10-22.**
