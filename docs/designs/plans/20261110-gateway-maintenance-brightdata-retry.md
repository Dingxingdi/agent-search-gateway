# Gateway Maintenance and Bright Data Header-Aware Retry Implementation Plan

**Goal:** Document the observed dblp limitation, expose the installed CLI version, simplify the example configuration, and correctly classify and retry Bright Data SERP header failures through the existing HTTP/retry path.

**Architecture:** [docs/designs/architectures/20261109-gateway-maintenance-brightdata-retry.md](../architectures/20261109-gateway-maintenance-brightdata-retry.md)

**Error handling:** [docs/designs/error-handlings/20261109-gateway-maintenance-brightdata-retry.md](../error-handlings/20261109-gateway-maintenance-brightdata-retry.md)

**Testing:** [docs/designs/testings/20261109-gateway-maintenance-brightdata-retry.md](../testings/20261109-gateway-maintenance-brightdata-retry.md)

---

## Execution context and scope

- Worktree: `/home/agent/agent-search-gateway/.worktrees/brightdata-header-retry-maintenance`.
- Branch: `brightdata-header-retry-maintenance`.
- Inspected starting commit: `2a6a8b0`, containing the three approved design documents.
- Plan date: 2026-11-10, Asia/Tokyo.
- Every path below is relative to this worktree. Reuse it; do not create another worktree.
- This document plans implementation. Unchecked implementation steps and expected test outcomes are not claims that the feature has been implemented or tested.

The approved design resolves the original Search-command question: the existing Bright Data Search request is retained. The correction is response-header classification before JSON decoding, with recovery inside the existing attempt budget. Do not introduce `data_format="parsed"`, change zones, or reinterpret the working Web Unlocker request.

Keep changes within these boundaries:

1. Remove four example configuration blocks, not four provider implementations.
2. Change the example timeout to 180.0 seconds; preserve the runtime default of 30.0 seconds.
3. Use the existing package `__version__`; do not bump the version.
4. Keep one `retry_async` loop per HTTP request. Three configured attempts mean three sends total, including mixed HTTP and vendor failures.
5. Install response inspection on each Bright Data Search call, not on the executor instance. Search and Fetch currently share an executor.
6. Preserve quotas, orchestration, socket messages, result schemas, and aggregate CLI errors.
7. Keep ordinary tests offline. Live provider smoke testing is optional and does not gate implementation.
8. Complete the tasks sequentially. No separate agent or additional execution framework is necessary.

## Planning baseline

The following existing tests were run against this worktree's `src` using the already available development interpreter:

```bash
PYTHONPATH=src /home/agent/agent-search-gateway/.venv/bin/python -m pytest -q tests/unit/test_retry.py tests/providers/test_http_executor.py tests/providers/web/test_brightdata.py tests/cli/test_cli.py tests/docs/test_documented_config.py
```

Observed result on 2026-11-10: **139 passed in 0.64s**. This is the pre-change baseline, not evidence for the proposed new behavior.

For implementation, use a worktree-local development environment. If setup is necessary, restore the tool proxy only for the network command:

```bash
https_proxy="${CODEXPRO_TOOL_HTTPS_PROXY:-${CODEXPRO_TOOL_ALL_PROXY:?Missing CODEXPRO_TOOL_HTTPS_PROXY and CODEXPRO_TOOL_ALL_PROXY}}" uv sync --locked --all-groups
```

Report missing proxy variables rather than substituting a hardcoded address. Once dependencies are available, the commands below use `uv run --locked --offline` to avoid implicit network access. Do not change the lockfile, installed user configuration, or ngrok environment for this work.

## Current execution path and reuse points

The CLI parses arguments before resolving `RuntimePaths.default()` or entering `asyncio.run()`. The daemon and `SearchOrchestrator` select providers and acquire their existing web quota lease. `BrightDataAdapter.search()` constructs its Google SERP URL and calls `HttpJsonExecutor.request_json()`.

The executor already performs the HTTP send inside `retry_async`; JSON decoding happens after that loop returns. Therefore, the inspector belongs immediately after the existing `http_attempt_completed` event inside the HTTP operation, before generic status handling accepts a response and before JSON decoding. A typed failure raised there can use the same attempt counter, timeout, and retry hooks.

`BrightDataAdapter.fetch()` calls `request_text()`, which can continue without inspection. `retry_async` also has direct LLM callers, so an omitted delay-adjustment callback must retain its exact existing behavior.

Reuse:

- `HttpStatusFailure`, `ExecutionFailure`, and the existing `ErrorCode.ALL_PROVIDERS_FAILED` taxonomy.
- `httpx.MockTransport` for real adapter/executor tests without a network.
- `tests/support/http.py` for request-shape tests using predecoded responses.
- `tests/support/logging.py::structured_test_logger`, `KeyValueFormatter`, and `SecretRedactor`.
- Existing injected sleep/monotonic functions and existing retry hooks.
- Existing provider registry, config loader, orchestration tests, and `organic` mapping assertions.

## Confirmed file structure

No new production module or new test-helper module is required.

| File | Planned responsibility |
|---|---|
| `README.md` | dblp caveat and disablement example; version commands; brief explanation of template changes and safe Bright Data diagnostics. |
| `config.example.toml` | Remove exactly four provider blocks and change the example request timeout. |
| `docs/public-interface.md` | Document top-level version flags and clarify that omitted example blocks do not remove supported provider configuration. |
| `CHANGELOG.md` | Record the user-visible changes under `Unreleased`, without a release/version bump. |
| `src/agent_search_gateway/cli.py` | Add the argparse version action using the existing package version. |
| `src/agent_search_gateway/retry.py` | Add optional bounded delay adjustment at the existing scheduling point. |
| `src/agent_search_gateway/providers/http.py` | Define a narrow typed response-failure signal and inspector alias; run inspection within an attempt; integrate typed retry, opt-in jitter, and safe events. |
| `src/agent_search_gateway/providers/web/common.py` | Extend only `JsonRequester.request_json` with the optional inspector keyword. |
| `src/agent_search_gateway/providers/web/brightdata.py` | Own Bright Data header interpretation and safe diagnostic values; opt Search into inspection. |
| `tests/docs/test_documented_config.py` | Separate registry/capability assertions from selected example-provider assertions; verify the example timeout contract. |
| `tests/cli/test_cli.py` | Verify both version aliases exit successfully before runtime/config/daemon work. |
| `tests/unit/test_retry.py` | Verify delay adjustment, bounds, actual-delay hooks, and default/cancellation compatibility. |
| `tests/providers/test_http_executor.py` | Verify the optional HTTP contract, single attempt budget, timeout, and legacy behavior. |
| `tests/providers/web/test_brightdata.py` | Verify header classifications, real executor integration, safe diagnostics, and Search/Fetch isolation. |
| `tests/support/http.py` | Accept the optional inspector in the two JSON-capable recording fakes. |
| `tests/orchestrators/test_keyword_search_pipeline.py` | Exercise existing partial/all-failed behavior with the new typed failure as a small regression extension. |

Read but do not modify `src/agent_search_gateway/config.py`, `src/agent_search_gateway/models.py`, `src/agent_search_gateway/errors.py`, `src/agent_search_gateway/runtime.py`, `src/agent_search_gateway/observability.py`, `src/agent_search_gateway/providers/defaults.py`, `src/agent_search_gateway/providers/contracts.py`, `src/agent_search_gateway/providers/academic/common.py`, `src/agent_search_gateway/providers/llm/openai_chat.py`, `src/agent_search_gateway/orchestrators/search.py`, `src/agent_search_gateway/daemon.py`, or `src/agent_search_gateway/protocol.py`. The required extension fits existing files and does not need a new provider framework.

Line anchors below refer to the inspected starting commit; locate the named symbol after preceding edits move lines.

## Internal contracts to use consistently

These names are implementation choices for the narrow extension, not new public APIs.

| Name / location | Exact contract | Notes |
|---|---|---|
| `DelayAdjuster` in `retry.py` | `Callable[[BaseException, float], float]` | Receives the caught error and already capped exponential delay. |
| `retry_async(..., adjust_delay=None)` | New keyword-only `adjust_delay: DelayAdjuster \| None = None` | Identity behavior when absent; `RetryPolicy` stays unchanged. |
| `ProviderResponseFailure` in `providers/http.py` | Subclass of `ExecutionFailure`, using `ErrorCode.ALL_PROVIDERS_FAILED` | Keeps a safe message; never stores a response, headers, body, query, or credential. |
| Failure fields | `status_code: int`, `retryable: bool`, `safe_reason: str`, `diagnostics: Mapping[str, object]` | `status_code` is outer HTTP status. Copy only already safe diagnostics from the inspector. |
| `ResponseInspector` in `providers/http.py` | `Callable[[httpx.Response], ProviderResponseFailure \| None]` | Returns `None` for the normal path; returns a typed failure for explicit provider failure. |
| JSON request extension | `response_inspector: ResponseInspector \| None = None` | Same keyword/type in `HttpJsonExecutor.request_json`, `_request_response`, `JsonRequester`, and JSON recording fakes. |
| `BrightDataAdapter._inspect_search_response` | `(self, response: httpx.Response) -> ProviderResponseFailure \| None` | Contains all `x-brd-*` parsing and vendor allowlists. |
| `HttpJsonExecutor._adjust_retry_delay` | `(exc: BaseException, delay: float) -> float`, a static helper | Adjusts only retryable `ProviderResponseFailure`; returns the original delay for status/transport errors. |

Use one required-data constructor for `ProviderResponseFailure`: provider name, stage, outer `status_code`, `retryable`, `safe_reason`, and safe `diagnostics`. Its inherited `code` remains the gateway enum; do not overwrite it with a vendor string. The adapter supplies `brightdata_error_code` and optional integer `brightdata_status` inside `diagnostics`. Generic code accesses these as a mapping and never recognizes Bright Data header names or hardcodes Bright Data log keys.

The inspector imports the generic signal from `providers/http.py`. The HTTP module must not import `web/common.py` or `BrightDataAdapter`, avoiding a cycle. `JsonRequester` and the recording fakes import the alias from the HTTP module. The academic protocol need not change: an implementation accepting an additional optional keyword remains compatible with its narrower callers.

## TDD execution convention

For each independent new behavior listed in a task, perform its RED → GREEN → REFACTOR cycle before moving to the next behavior. Parameterize equivalent cases; do not combine unrelated expectations into one large scenario.

A RED run must fail because the desired behavior or optional interface is absent. Fix syntax/import/collection errors before treating a run as evidence. Previously implemented behaviors are regression guards and should remain green; do not deliberately break them to manufacture a RED result.

Use minimal transport fakes instead of mocking the adapter, retry loop, or classification function. Documentation wording is checked by review; do not add brittle tests that merely duplicate a prose sentence. Config loading and CLI grammar are executable contracts and should be tested.

### Task 1: Update the selected configuration examples and dblp guidance

**Files:**

- Modify: `config.example.toml:80-102` and `config.example.toml:223-227`.
- Test: `tests/docs/test_documented_config.py:206-302`.
- Docs: `README.md`, sections “Configuration and runtime paths” and “Academic paper search”.
- Docs: `docs/public-interface.md`, supported configuration scope.
- Docs: `CHANGELOG.md`, `Unreleased`.
- Reference: `src/agent_search_gateway/config.py::resolve_retry_policy` and `providers/defaults.py`.

- [ ] **Step 1: Write the failing test**

Add `test_example_provider_selection_omits_only_requested_templates` using `load_toml` and real `resolve_config` with the existing `_stub_environment`.

Scenario: the example is a selected provider template while built-in registration remains complete.

Setup: load the checked-in TOML; extract provider tables rather than counting the scalar `default_max_concurrency` as a provider.

Action: resolve the example and compare its provider tables with the intended retained set.

Expected: absent `zenrows`, `decodo`, `scrapingdog`, `scrapegraphai`; retained `tavily`, `firecrawl`, `exa`, `linkup`, `brave`, `anysearch`, `tinyfish`, `parallel`, `brightdata`, `scrape_do`, `scraperapi`, `scrapingant`, `serpapi`, and `jina`.

Adjust the existing provider-contract test without weakening its capability or README-table assertions:

- Keep its entire `expected_capabilities` mapping and all existing README provider rows.
- Define the registered provider expectations separately from the configured example-provider expectations.
- Resolve registry entries/capabilities for every supported provider under test, including the omitted four.
- Index `by_name[name]`, `web[name]`, and assert example credentials/enable flags only for providers actually expected in the example.
- Compute the credentialed-example set from the selected example set, excluding Jina.
- Retain Bright Data's independent zone assertions and all other remaining example options.

In its own RED cycle, add `test_example_timeout_does_not_change_runtime_default`: `resolve_retry_policy(example).request_timeout_seconds == 180.0`, while `resolve_retry_policy({}).request_timeout_seconds == 30.0`.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --locked --offline pytest -q tests/docs/test_documented_config.py::test_example_provider_selection_omits_only_requested_templates
uv run --locked --offline pytest -q tests/docs/test_documented_config.py::test_example_timeout_does_not_change_runtime_default
```

Expected: the first fails because the four tables still exist; the second fails because the example resolves to 30.0. Registry and runtime-default assertions should already pass.

- [ ] **Step 3: Describe the minimal implementation**

```text
Delete the four entire web-provider example tables.
Set only the example [retry].request_timeout_seconds to 180.0.
Leave all remaining example settings and provider registrations intact.

Near README's academic-provider explanation:
  state that dblp has been observed to trigger bot verification even
  with infrequent API calls;
  explain that reducing frequency does not guarantee availability;
  offer editing enabled=false in the existing academic_providers.dblp table.

Explain that the example is a selection of supported providers and that
template edits do not automatically rewrite an installed user's config.
Clarify the supported-configuration paragraph in docs/public-interface.md
so omission of an example does not imply removal of built-in support.
Record template changes and the dblp limitation under Unreleased.
```

Use this small optional mitigation example:

```toml
[academic_providers.dblp]
enabled = false
```

Tell readers to edit the existing table rather than append a duplicate TOML table. Keep the example configuration's existing dblp enablement and runtime behavior unchanged.

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run --locked --offline pytest -q tests/docs/test_documented_config.py tests/providers/test_registry.py tests/unit/test_config_web_providers.py
uv run --locked --offline pytest -q
```

Expected: selected example tables resolve, all supported provider capability assertions pass, example timeout is 180.0, fallback remains 30.0, and the offline suite is green.

Review the README note for all three points: low-frequency verification is an observed limitation, slowing requests is not a guaranteed remedy, and disabling dblp is optional. Confirm the provider capability table still contains all four omitted examples.

- [ ] **Step 5: Refactor (keep tests green)**

Only deduplicate local test sets if that improves the distinction between registry support and example selection. Avoid introducing a second source of production provider capabilities. If refactoring changes files after Step 4, rerun the targeted docs/config tests and `uv run --locked --offline pytest -q`; otherwise retain the Step 4 evidence.

### Task 2: Expose top-level CLI version flags

**Files:**

- Modify: `src/agent_search_gateway/cli.py:12-30` and `cli.py::build_parser` (starting at line 48).
- Test: `tests/cli/test_cli.py`.
- Docs: `README.md`, Commands.
- Docs: `docs/public-interface.md`, supported CLI interfaces.
- Docs: `CHANGELOG.md`, `Unreleased/Added`.
- Reference: `src/agent_search_gateway/__init__.py`, `pyproject.toml`, `tests/unit/test_package_metadata.py`.

- [ ] **Step 1: Write the failing test**

Add a parameterized `test_cli_version_flags_exit_before_runtime` for `"--version"` and `"-v"`.

Scenario: a user asks for the installed CLI version without a subcommand or daemon/config access.

Setup: import the existing package `__version__`; capture stdout/stderr with `capsys`; replace `RuntimePaths.default` with a fail-fast callable so entering runtime setup cannot silently pass.

Action: call the real `main([flag])`, expecting argparse's `SystemExit`.

Expected assertions:

```python
assert caught.value.code == 0
assert captured.out == f"agent-search-gateway {__version__}\n"
assert captured.err == ""
```

Keep existing subcommand parsing and command-output tests. Add or retain a parser guard that no arguments still exit 2 when neither version flag is present, and that the version aliases are not installed on child parsers.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --locked --offline pytest -q tests/cli/test_cli.py::test_cli_version_flags_exit_before_runtime
```

Expected: both cases fail because the current parser exits 2 instead of printing the version and exiting 0. Failure must not arise from the fail-fast runtime sentinel.

- [ ] **Step 3: Describe the minimal implementation**

Import `__version__` from the package and add this action immediately after creating the top-level parser, before declaring required subparsers:

```python
parser.add_argument(
    "-v",
    "--version",
    action="version",
    version=f"%(prog)s {__version__}",
)
```

Keep `main()`, `run_command()`, runtime paths, and daemon dispatch unchanged. Document both invocations and that the output is the installed CLI package version and does not require a running daemon or configuration. Add the public-interface and changelog entries. Do not add a `version` subcommand, child-parser flag, manual pre-parser, or a second hardcoded version string.

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run --locked --offline pytest -q tests/cli/test_cli.py tests/unit/test_package_metadata.py tests/docs/test_documented_config.py tests/docs/test_installation_contract.py
uv run --locked --offline agent-search-gateway --version
uv run --locked --offline agent-search-gateway -v
uv run --locked --offline pytest -q
```

Expected: tests pass; both commands print `agent-search-gateway 0.2.0` once with exit 0 on this branch. Tests derive their expectation from `__version__`, so a later release does not require updating a duplicated literal.

- [ ] **Step 5: Refactor (keep tests green)**

Keep both aliases on one argparse action. No extraction is needed for this small change. If cleanup is performed, rerun the CLI tests and the full offline suite.

### Task 3: Add optional bounded delay adjustment to the existing retry loop

**Files:**

- Modify: `src/agent_search_gateway/retry.py:10-42`.
- Test: `tests/unit/test_retry.py`.
- Reference: `src/agent_search_gateway/models.py::RetryPolicy`.
- Regression: `tests/providers/llm/test_openai_chat.py` through the provider suite.

- [ ] **Step 1: Write the failing test**

Add `test_retry_delay_adjuster_updates_sleep_and_retry_hook`.

Scenario: an opted-in caller adjusts a capped backoff, and observability sees exactly the same wait as the sleeper.

Setup: use the real `retry_async`, a three-attempt policy, an operation failing twice, a collecting async sleeper, collecting `on_retry`, and a deterministic `adjust_delay(exc, delay)` returning `delay * 0.9`.

Action: run the operation.

Expected: three calls, successful return, and both hook delays and sleep delays equal approximately `[0.225, 0.45]` under the default policy. The callback receives the actual caught exception and capped base delay.

Then cover one behavior per test:

- `test_retry_delay_adjuster_clamps_invalid_delays`: finite values below zero clamp to zero and above the configured cap clamp to the cap; NaN/infinity fall back to the original finite capped delay.
- `test_retry_delay_adjuster_failure_preserves_retry_execution`: an ordinary adjustment callback exception falls back to the original capped delay.
- `test_retry_delay_adjuster_is_skipped_when_no_retry_is_scheduled`: nonretryable failure, exhausted/max-attempts-one failure, and cancellation do not invoke adjustment or sleep.
- Extend the cancellation guard to prove cancellation during the wait also propagates without another operation call.

Retain the existing exact `[0.25, 0.5]` assertion with no callback and existing observer-hook exception-isolation tests.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --locked --offline pytest -q tests/unit/test_retry.py::test_retry_delay_adjuster_updates_sleep_and_retry_hook
```

Expected: failure because the optional adjustment interface is not implemented. After adding the signature during GREEN, the behavioral assertions must still distinguish adjusted delays from the old exact delays. Do not count a collection/import error as RED.

- [ ] **Step 3: Describe the minimal implementation**

```text
On a caught failure:
  propagate cancellation immediately;
  if terminal or out of attempts, raise without adjustment or sleep;
  calculate the existing capped exponential delay;
  if adjust_delay exists:
    call it with the failure and capped delay;
    on ordinary callback failure or a nonfinite result, keep the capped delay;
    otherwise clamp its numeric result into [0, max_delay_seconds];
  pass that final delay to the existing on_retry hook;
  await the existing sleep callable with that same delay.
```

Use `math.isfinite` for the new adjustment result. Do not catch `BaseException` around the callback, since cancellation must remain cancellation. Preserve the existing observer-hook isolation and operation return/exception identity. Do not add randomness to `retry.py`, alter `RetryPolicy`, or change direct LLM callers.

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run --locked --offline pytest -q tests/unit/test_retry.py tests/providers/test_http_executor.py tests/providers/llm
uv run --locked --offline pytest -q
```

Expected: adjusted waits and hooks agree, bounds/cancellation tests pass, and unchanged callers retain their old exact delays.

- [ ] **Step 5: Refactor (keep tests green)**

Keep one scheduling location and one attempt counter. Prefer a short local calculation over a new retry abstraction. If the calculation or tests are cleaned up, rerun the retry/provider tests and full offline suite.

### Task 4: Inspect JSON responses inside a single HTTP attempt

**Files:**

- Modify: `src/agent_search_gateway/providers/http.py`, beside `HttpStatusFailure`, in `request_json`, `_request_response`, `_is_retryable`, and `_log_failed`.
- Modify: `src/agent_search_gateway/providers/web/common.py::JsonRequester`.
- Modify: `tests/support/http.py::RecordingJsonExecutor.request_json` and `RecordingHttpExecutor.request_json`.
- Test: `tests/providers/test_http_executor.py`.

- [ ] **Step 1: Write the failing test**

Start with `test_json_response_inspector_runs_before_decode`.

Scenario: a caller can inspect a real response before the executor accepts its JSON body.

Setup: `httpx.MockTransport`, the real `HttpJsonExecutor`, and a local inspector that records the received response and returns `None`.

Action: call `request_json(..., response_inspector=inspector)`.

Expected: the inspector sees the response exactly once and normal JSON is returned. The first RED is the missing optional interface.

After that contract is green, add focused tests using the new typed failure:

- `test_json_response_failure_preempts_invalid_json`: the inspector returns a nonretryable typed failure for HTTP 200/empty body; catch that exact failure, not `ProtocolFailure`, with one send.
- `test_json_response_failures_share_http_attempt_budget`: inspector failure → plain HTTP 500 → inspector failure under `max_attempts=3`; exactly three sends and the final typed failure is retained.
- `test_json_response_failure_retries_only_when_marked_retryable`: retryable failure then success retries; terminal failure does not.
- `test_json_response_inspection_is_per_call`: an inspected JSON call followed by an ordinary JSON call and a text call cannot reuse the first call's inspector.
- Inspect `request.extensions["timeout"]` in the transport handler to verify every send still receives the configured per-attempt timeout.

Use generic safe fixture metadata in executor tests; vendor header interpretation belongs in Task 5. Reuse existing cancellation, transport subtype, status, redirect, and decode tests.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --locked --offline pytest -q tests/providers/test_http_executor.py::test_json_response_inspector_runs_before_decode
```

Expected: the current JSON boundary rejects the inspector keyword or does not invoke it. After the first cycle, each subsequent new behavior must fail for its missing typed retry/classification path before implementing that path.

- [ ] **Step 3: Describe the minimal implementation**

```text
Define ProviderResponseFailure and ResponseInspector next to HttpStatusFailure.
Add response_inspector=None to request_json and the internal _request_response.
Forward the JSON call's optional inspector into _request_response.
Leave request_text's public signature and call contract unchanged.

Inside the existing operation, after HTTP completion logging:
  if an inspector was supplied:
    failure = inspector(response);
    if failure is present, raise it;
  then run the unchanged generic retryable-status check.

In _is_retryable:
  return failure.retryable for ProviderResponseFailure;
  retain the existing status/timeout/transport predicate for all other errors.

Catch an exhausted or terminal ProviderResponseFailure around the existing
retry_async call, log category=provider_header, and re-raise the same object.
Keep it out of the transport_type fallback in on_retry.
Keep JSON decoding after the retry loop.
```

Provide a small generic path for safe provider-failure details in `_log_failed`; do not copy whole headers into it or change unrelated event fields. Task 6 completes the new detailed diagnostics and jitter. The basic provider retry branch should already use `category=provider_header`, never `category=transport`.

Update both JSON-capable recording fakes with the same explicitly typed optional keyword. They return already-decoded test objects and need not simulate HTTP header inspection. Keep `RecordedRequest`, text-fake signatures, and recorded request shapes unchanged. Actual inspection is verified with the real executor and `MockTransport`.

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run --locked --offline pytest -q tests/providers/test_http_executor.py tests/providers/web tests/providers/academic tests/providers/llm
uv run --locked --offline mypy src tests scripts
uv run --locked --offline pytest -q
```

Expected: the inspector works before decoding, mixed errors share one budget, no-inspector JSON/text paths retain existing behavior, and protocol/fake signatures type-check.

- [ ] **Step 5: Refactor (keep tests green)**

Share only the short safe-field preparation needed by retry and terminal logging. Do not split the executor or build a new HTTP hierarchy. If cleanup changes the implementation, rerun executor/provider tests, mypy, and the full offline suite.

### Task 5: Classify Bright Data Search headers and preserve Fetch

**Files:**

- Modify: `src/agent_search_gateway/providers/web/brightdata.py::BrightDataAdapter.search` and new private `_inspect_search_response`.
- Test: `tests/providers/web/test_brightdata.py`.
- Regression: `tests/providers/web/test_search_result_tolerance.py`.
- Reference: the response classification table in the error-handling spec.

- [ ] **Step 1: Write the failing test**

Add `test_brightdata_search_classifies_masked_captcha_before_json_decode`.

Scenario: the observed HTTP 200/empty-body failure has `x-brd-error-code=captcha`, `x-brd-error=redirect location was rejected`, and `x-brd-status-code=502`.

Setup: the real adapter and real HTTP executor over `MockTransport`; use `max_attempts=1` and a collecting sleeper.

Action: call `adapter.search("query")`.

Expected: `ProviderResponseFailure` with outer status 200, safe `captcha` code, inner status 502, the bounded benign reason, one send, and no sleep. It is not a JSON protocol failure.

Then add separate parameterized or sequential tests for this classification matrix:

| Input | Expected |
|---|---|
| Each of `captcha`, `timeout`, `expect_element`, `expect_body` | Retryable typed failure. |
| Two masked failures, then valid `organic` JSON | Three sends and the existing expected `KeywordSearchHit` values. |
| Nonempty unknown code, explicit auth/config code, or explanation only | Terminal typed failure; one send. |
| Outer 401/403, including an otherwise recoverable header code | No retry; retain existing HTTP status failure when no vendor failure is present. |
| Valid inner 401/403 plus an otherwise recoverable code | Terminal vendor failure. |
| Status-only inner 502, or other valid non-2xx inner status | Terminal vendor failure; do not infer retry eligibility from status. |
| Only inner 200 and valid `organic` | Normal success. |
| Explicit recoverable error plus inner 200 and valid JSON | Error wins; retry according to the code. |
| No vendor headers and HTTP 500 followed by success | Existing generic HTTP retry. |
| No vendor error and empty/invalid JSON | Existing `ProtocolFailure`, one send. |
| No vendor error and malformed `organic` | Existing adapter failure, no vendor retry. |
| Malformed/out-of-range status or ambiguous duplicate code/status | Safe terminal classification, no guessed retry. |
| Duplicate/combined status values containing both 200 and 502 | Failure is not concealed by the benign value; no body accepted as success. |
| Oversized/unsafe error explanation with an otherwise valid recoverable code | Replace the explanation with a safe fixed reason; eligibility still depends on validated code/status. |

Preserve existing Search request-shape assertions exactly: POST to `/request`, Bearer header, Search zone, encoded Google `q` plus `brd_json=1`, and exactly the body keys `zone`, `url`, `format`.

Retain existing Fetch Markdown, independent-zone, and empty-body tests. Add `test_brightdata_fetch_does_not_inherit_search_header_inspection` using the same real adapter/executor: complete a Search, then return nonempty Fetch text with SERP-like headers. Fetch returns the text without special SERP retries.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --locked --offline pytest -q tests/providers/web/test_brightdata.py::test_brightdata_search_classifies_masked_captcha_before_json_decode
```

Expected: the current adapter does not supply an inspector, so the executor raises `ProtocolFailure: response was not valid JSON`. This reproduces the actual bug without a live API call.

- [ ] **Step 3: Describe the minimal implementation**

```text
Pass response_inspector=self._inspect_search_response only from search().
Leave its outgoing request and organic mapping unchanged.

In the private inspector:
  read x-brd-error-code, x-brd-error, and x-brd-status-code case-insensitively;
  distinguish absent/benign metadata from explicit failure or invalid metadata;
  validate code/status before deciding retryability;
  if no explicit failure and no malformed classification metadata, return None;
  otherwise build only safe diagnostic scalars;
  mark retryable only for a validated exact allowlisted code,
    provided outer/inner status does not indicate 401/403 and metadata is unambiguous;
  return ProviderResponseFailure with the original outer HTTP status.
```

Concrete conservative parsing choices:

- Accept only the four specified lowercase code tokens for recovery. Do not lowercase an unknown token, truncate it into a recognized code, or search for a recoverable substring.
- Bound classification inputs before processing. An error code longer than 64 characters, control-containing code, comma-joined code, or duplicate code fields is not a validated retry code.
- Parse inner status only from one bounded three-ASCII-digit value in 100–599. Invalid, out-of-range, or duplicated status metadata cannot authorize a retry. A valid status outside 200–299 without a known code is terminal.
- A nonempty error explanation is evidence of failure, not evidence of retryability.
- Log only known safe codes or fixed `unknown`/`invalid`/`missing` markers. Do not emit arbitrary unknown strings that might actually be tokens.
- Preserve the exact known benign explanation `redirect location was rejected` when safely bounded and free of controls. For all other untrusted explanations, use `provider_reported_failure`. Use a 160-character input bound for this decision; do not try to make arbitrary URLs/token-bearing prose safe by truncation alone.
- Keep outer/inner authentication statuses terminal even when a recoverable code is also present.
- When conflicting metadata exists, failure wins over an apparently successful body, while retry requires unambiguous validated metadata.

Only the adapter contains those vendor-specific rules. Reuse the existing final-render `SecretRedactor` as defense in depth; do not build a new logging/redaction subsystem. The failure object's string/message/diagnostics contain only the safe projection, never raw headers or the response.

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run --locked --offline pytest -q tests/providers/web/test_brightdata.py tests/providers/web/test_search_result_tolerance.py tests/providers/test_http_executor.py
uv run --locked --offline pytest -q
```

Expected: all header classifications, recovery, request-payload guards, and Search/Fetch isolation pass. Old body-only malformed-response tests still behave as before.

- [ ] **Step 5: Refactor (keep tests green)**

Keep any bounded header parsing helpers private to `brightdata.py`. Do not move vendor knowledge into `http.py` or add an adapter retry loop. Extract only clearly repeated validation within this file. After any cleanup, rerun the targeted tests and full offline suite.

### Task 6: Add header-only jitter, safe detailed events, and integration guards

**Files:**

- Modify: `src/agent_search_gateway/providers/http.py`, retry delay adjustment and provider-failure event details.
- Test: `tests/providers/web/test_brightdata.py`.
- Test: `tests/providers/test_http_executor.py`.
- Test: `tests/orchestrators/test_keyword_search_pipeline.py`.
- Docs: `README.md`, DEBUG tracing.
- Docs: `CHANGELOG.md`, `Unreleased/Fixed`.
- Reuse unchanged: `tests/support/logging.py`, `src/agent_search_gateway/observability.py`.

- [ ] **Step 1: Write the failing test**

Add `test_brightdata_header_retry_uses_adjusted_delay`.

Scenario: recoverable Bright Data headers use bounded jitter, and ordinary status failures do not.

Setup: real adapter/executor; fake sleeper; deterministic random factor 0.9; default retry policy. Make the random source fakeable by monkeypatching the module-level `random.uniform` used in `providers.http`, without adding a runtime constructor/configuration option.

Action and expected outcomes:

- `captcha → captcha → success`: waits approximately `[0.225, 0.45]`.
- In a separate test, `captcha → HTTP 500 → captcha`: exactly three sends; waits approximately `[0.225, 0.5]`; the final error is the last typed provider failure. The final attempt does not draw randomness or sleep.
- Deterministic factors 0.8 and 1.0 exercise the jitter bounds after exponential capping.
- `max_attempts=1` has one send and no wait.
- An inspector-enabled Search receiving only generic status/transport failures retains its exact original waits. Other providers do too.

Add `test_brightdata_header_events_expose_safe_metadata`, parameterized over recovery, exhaustion, and terminal nonrecoverable failure.

Use `structured_test_logger` with the credential registered for redaction. Put distinguishable sentinels in a query/target path, response body, credential, and an explanation containing a URL, token-like value, and control characters. Include a hostile oversized/unknown error-code case.

Assert the rendered events and exception messages never include those sentinels or raw vendor values. Assert positive structured diagnostics, not just the absence of secrets:

- `provider=brightdata`, `stage=search`, attempt, outer `status`, elapsed time;
- `category=provider_header`, safe `brightdata_error_code`, parsed `brightdata_status` when valid;
- safe `reason`, retry eligibility and whether a retry was actually scheduled;
- each retry's `delay_ms == max(0, int(recorded_sleep_delay * 1000))`;
- the terminal event's final attempt and terminal classification;
- no `transport_type` or traceback for provider-header failures.

As regression evidence, extend the existing orchestration test minimally with `ProviderResponseFailure` as a provider-failure variant. Confirm a successful sibling still produces the same JSONL; all failed branches still raise `ALL_PROVIDERS_FAILED` with `All keyword search provider pipelines failed`, without leaking vendor details or writing a result file. Exercise single-provider selection as well. This unchanged aggregate behavior may already be green; it does not require production orchestrator edits.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --locked --offline pytest -q tests/providers/web/test_brightdata.py::test_brightdata_header_retry_uses_adjusted_delay
uv run --locked --offline pytest -q tests/providers/web/test_brightdata.py::test_brightdata_header_events_expose_safe_metadata
```

Expected: the current typed-header path still uses unadjusted exponential waits and lacks the complete safe provider diagnostic fields. Any baseline failure unrelated to those differences must be fixed before proceeding.

- [ ] **Step 3: Describe the minimal implementation**

```text
Provide HttpJsonExecutor._adjust_retry_delay:
  if the error is a retryable ProviderResponseFailure:
    return capped_delay * random.uniform(0.8, 1.0);
  return capped_delay unchanged.

Supply that optional adjust_delay callback to the existing retry_async call
only when response inspection was opted into for this JSON request.
The retry helper remains responsible for final bounds and the same
delay reaching both on_retry and sleep.

On provider-header retries:
  emit http_retrying at DEBUG with generic outer status/attempt/timing,
  safe typed diagnostics, reason, retryable, retry_scheduled=true,
  and the actual delay_ms.

On terminal provider-header failure:
  emit http_failed at DEBUG with the same safe metadata,
  retry_scheduled=false, and terminal_reason set to
  exhausted or non_retryable;
  re-raise the typed failure.

Leave existing HTTP-status and transport retry logging at its existing
WARNING level with its existing field meanings and exact delays.
```

Use the failure's safe `diagnostics` mapping for vendor keys. Prepare generic fields once and merge only the inspector's controlled safe keys; do not allow diagnostics to overwrite `provider`, `stage`, `attempt`, `status`, `category`, or other executor-owned fields. No raw response/exception serialization.

Document that a Bright Data outer HTTP 200 may be a provider failure, that detailed safe classifications and retry waits are available in DEBUG, and that normal business-command output remains unchanged. Record the fix under `Unreleased`. Keep the existing warning about handling local debug files carefully.

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run --locked --offline pytest -q tests/unit/test_retry.py tests/providers/test_http_executor.py tests/providers/web/test_brightdata.py tests/orchestrators/test_keyword_search_pipeline.py
uv run --locked --offline pytest -q tests/providers tests/runtime tests/orchestrators tests/observability tests/cli tests/docs
uv run --locked --offline pytest -q
```

Expected: recovery/exhaustion share one attempt budget; only validated provider-header retries are jittered; logged/slept waits agree; safe metadata survives failure; sibling results, selected-provider errors, default delays, Fetch, and other providers remain compatible.

- [ ] **Step 5: Refactor (keep tests green)**

Reuse the existing event formatter, redactor, and elapsed-time calculation. If retry/terminal formatting duplicates a few safe fields, consolidate that narrow mapping only. Do not add public failure codes, protocol fields, jitter configuration, or a vendor-specific HTTP subclass. After any cleanup, rerun the targeted integration tests and full offline suite.

## Final verification and handoff gate

These are implementation acceptance checks, not additional feature work.

- [ ] Confirm the worktree diff contains only the planned implementation, tests, and documentation; original design documents remain intact.
- [ ] Confirm neither `__version__` nor `pyproject.toml` was bumped, and no user config, registry, quota, daemon, or socket code was altered.
- [ ] Confirm README contains both version invocations and the reviewed dblp low-frequency caveat/optional disablement; all provider capability rows remain.
- [ ] Confirm `CHANGELOG.md` has the version feature, template changes, dblp documentation, and Bright Data fix under `Unreleased`.
- [ ] Run the project's required static and documentation gates:

```bash
uv run --locked --offline ruff format --check src tests scripts
uv run --locked --offline ruff check .
uv run --locked --offline mypy src tests scripts
uv run --locked --offline pytest -v
uv run --locked --offline python scripts/build_docs.py
```

Expected: format/lint/type checks exit 0; the ordinary test suite passes with existing live checks skipped unless explicitly enabled; documentation builds successfully. A full-suite run on the same final revision from Task 6 may be reused rather than repeated solely to change verbosity. If a required check leads to another change, rerun the affected checks on that final revision. Packaging is unchanged, so no additional release or packaging task is introduced.

Optional smoke evidence, only when chosen for the implementation session and an appropriately configured daemon is already available:

```bash
agent-search-gateway keyword-search "Bright Data SERP header retry smoke" --provider brightdata
```

A successful CLI call prints a result path; an exhausted call may retain the generic all-providers-failed error. Judge the header fix using the DEBUG attempt count, classifications, and actual waits rather than expecting every upstream request to succeed. This command can still use the gateway's normal downstream LLM stages. Do not automatically restart an existing daemon or alter user configuration for this optional check. Inspect any cache logs via an allowed LocalHost bash command, and do not copy sensitive raw log contents into the plan or shared output.

The implementation handoff should report changed areas, final test results, and any real remaining failure with its evidence. Publishing, releasing, and opening a PR are outside this plan.

## Spec coverage and type-consistency review

| Spec requirement | Planned proof / task |
|---|---|
| dblp low-frequency warning and optional disablement | Task 1 README review; existing config namespace retained. |
| Top-level `--version` and `-v`, canonical version, no runtime access | Task 2 real `main` test, fail-fast runtime sentinel, CLI smoke. |
| Remove only four examples, keep registration/capabilities | Task 1 parsed/loaded template tests and preserved registry/README assertions. |
| Example 180.0, runtime fallback 30.0 | Task 1 explicit dual-resolution assertion; existing retry baseline. |
| Recognize HTTP-200/empty-body vendor failure before decode | Tasks 4–5 real adapter/executor transport tests. |
| Four known recoverable codes; terminal unknown/auth/config/status-only cases | Task 5 classification matrix, including auth precedence. |
| Malformed/ambiguous headers and safe explanation fallback | Task 5 validation rules; Task 6 hostile diagnostic cases. |
| One total attempt budget, including mixed failures and max-attempts-one | Tasks 4 and 6 send counts, final-failure identity, and no extra waits. |
| Bounded optional jitter; actual log delay equals sleep | Task 3 callback/bounds tests; Task 6 deterministic randomness and events. |
| Default status/transport/LLM behavior, timeout, hook isolation, cancellation | Tasks 3–4 regression suites plus Task 6 inspector-enabled generic-error guards. |
| Search payload, `organic` mapping, Fetch payload/text/empty handling unchanged | Task 5 existing contracts plus same-executor Fetch isolation. |
| No credential/query/body/header leakage; safe terminal metadata | Task 6 formatter/redactor integration with positive fields and negative sentinels. |
| Quotas, partial success, all-failed public errors, result files | Task 6 existing orchestration scenarios with typed failure; runtime/acceptance suites. |
| No unrelated schemas, defaults, registries, version/release changes | File boundaries and final diff/verification gate. |

Type review checklist for the plan author and implementer:

- `ResponseInspector` returns `ProviderResponseFailure | None` everywhere; HTTP raises the returned failure.
- `response_inspector` is the same optional keyword in the JSON protocol, executor, and both JSON recording fakes; text interfaces remain unchanged.
- `DelayAdjuster` receives `(BaseException, float)`; `_adjust_retry_delay` uses the same argument order.
- `GatewayError.code` is always an `ErrorCode`; Bright Data's vendor string lives only in safe diagnostics.
- `status_code` is outer HTTP status; `brightdata_status` is a validated integer inner status, omitted when unavailable.
- `on_retry` continues receiving the final `float` delay; `delay_ms` uses the same existing integer conversion.
- `retryable` describes eligibility; `retry_scheduled` distinguishes exhaustion from an actual next attempt.
- The HTTP/retry layer never imports the adapter or recognizes `x-brd-*`; its only provider extension is the optional inspector and typed safe signal.
