# Testing: MCP Server (STDIO and Streamable HTTP)

Date: 2026-11-27
Baseline: v3.0.0, commit `dabfbbc`
Companions: [Architecture](../architectures/20261127-mcp-server.md), [Error Handling](../error-handlings/20261127-mcp-server.md)
Status: proposed for review

## 1. Objectives and scope

Verify that the new MCP frontend behaves like a thin client of the existing daemon, rather than a second implementation of gateway business logic. Prove the public tool contract over both supported transports, safe result conversion, recoverable failures, and shared state with existing CLI users. Protect existing CLI-only installations and all current public interfaces.

The architecture and error-handling documents define the expected behavior. Test the externally observable contracts; do not enforce an arbitrary SDK internal class layout or introduce a second provider test suite. Existing tests already cover the core provider, credential, fetch-state, scheduling, URL admission, retry, and shutdown algorithms. Add focused cross-interface cases to show that MCP uses those same algorithms and state.

This is a design specification, not an execution report. All cases below are implementation acceptance criteria; no new implementation or test execution is part of this spec-writing task.

## 2. Test strategy and fixtures

Use the existing pytest, pytest-asyncio, Ruff and strict mypy tooling, with the optional MCP SDK installed for MCP tests. Select the SDK version in the implementation plan and verify its supported Python versions, client/server transport APIs, tool result and error serialization, Host/Origin validation, cancellation and resource lifecycle against that version.

Reuse existing fixture patterns from tests/acceptance/test_gateway_workflows.py, tests/acceptance/test_credential_pool_fetch_state.py, tests/daemon/test_daemon_dispatch.py, tests/support/acceptance.py and tests/support/fakes.py where appropriate. Use actual Request/Response classes, RuntimePaths, protocol.send_request, ForegroundDaemon and ResultWriter in tests intended to verify their integration. Do not modify core production code to make tests easier; inject paths and a typed socket-client callable at the new adapter boundary.

Use three levels of controlled execution:

| Level | Backend | What it proves |
|---|---|---|
| Adapter unit tests | Recording typed async socket client; real temporary files | Correct mapping, one-call behavior, safety and error translation |
| SDK and transport contract tests | Official MCP client and server with a controlled daemon or scripted Unix socket peer | Actual initialization, discovery, tool calls, protocol errors, transport parity |
| Cross-interface acceptance tests | Real ForegroundDaemon and existing Runtime/orchestrators with fake providers or HTTPX MockTransport | Shared admission, credentials, scheduling, persisted results and lifecycle with CLI and MCP |

All automated acceptance tests must work offline with no API keys or external network access. Local Unix and loopback TCP sockets are allowed. Existing live integration tests remain opt-in; do not change them or make paid providers required for MCP CI.

### Isolation and deterministic cleanup

- Allocate separate temporary config, result and socket locations per test or fixture. Never connect to the developer's actual running daemon or read/write their real cache or credentials. For executable subprocesses, use an isolated HOME (or equivalent test process configuration) consistent with RuntimePaths.default(); never require a new production configuration option solely for tests.
- Use short temporary Unix socket paths, including macOS compatibility with the existing tests/conftest.py behavior. Use an OS-allocated available loopback port and reliable readiness detection for HTTP; do not assume port 8765 is available in CI.
- Use bounded test waits, readiness events, controlled barriers and manual clocks where appropriate. Avoid arbitrary sleeps, timing-based flakiness, and treating the test deadline as a new product timeout.
- Always close MCP sessions, socket writers, temporary files, and test-owned server/subprocess resources in finally blocks or context managers, even after assertion failures. Only shut down test-owned daemon instances. Confirm that shutting down MCP alone never stops the daemon.
- Use synthetic URLs and synthetic error secrets/paths; never expose actual credentials in logs or assertions. Use deterministic request IDs or inspect emitted names rather than depending on randomness.

## 3. Expected test files

| Proposed file | Scope |
|---|---|
| tests/mcp/test_adapter.py | Normalization, typed RPC, result text, file safety, cancellation, gateway errors |
| tests/mcp/test_server.py | SDK registration, input schema, successful text content, tool error mapping, adapter invocation count |
| tests/mcp/test_entrypoint.py | Command options, lazy optional dependency, launch routing, startup diagnostics |
| tests/mcp/test_stdio.py | End-to-end STDIO initialization, discovery, tool calls, stdout framing, recovery, cleanup |
| tests/mcp/test_streamable_http.py | End-to-end Streamable HTTP, session and Host/Origin behavior, recovery, concurrency, cleanup |

A small tests/mcp/conftest.py or shared helper is acceptable if it avoids duplicated transport setup. Prefer reuse of existing shared fixtures. Place cross-interface acceptance cases in the transport tests with shared fixtures or a focused acceptance test file if separation improves clarity; file count is not a contract. Do not create five independent copies of business logic tests.

## 4. Adapter unit cases

### 4.1 Request mapping and input validation

Parameterize the accepted and rejected inputs. Assert the exact existing request object sent to the socket client, the socket path from RuntimePaths, and call count.

| Case | Expected |
|---|---|
| keyword_search and paper_search with query alone | Correct KeywordSearchRequest or PaperSearchRequest, no provider filter |
| Both with provider selector | Trim outer whitespace, retain exact case-sensitive alias |
| llm_search and agent_search with prompt alone | Correct request type, scope web, provider/model unset |
| Both with each scope and selector combination | Scope web/paper/all, optional independent filters passed without rewriting |
| Null/omitted selectors | No selector; daemon chooses existing default behavior |
| url_fetch with URL and optional focus | Existing URL normalization; focus trimmed, absent/blank focus becomes None |
| Blank or whitespace-only query/prompt | Existing empty_query error, zero socket calls |
| Blank supplied selector | Existing bad_request, zero socket calls |
| Invalid scope, URL, or argument type | Existing semantic error or SDK schema rejection, zero socket calls |
| Unknown tool or unsupported Fetch filter | No unsupported request reaches adapter or daemon |

Provider/model selection validity and matching remain daemon-owned. Test that a valid syntactic but unmatched selector produces the existing daemon failure without falling back. Do not implement new provider lookup in adapter tests.

### 4.2 Search results and Fetch text

- For each of the four search kinds, create a result with the existing expected naming convention in a temporary results directory. Return its absolute path from the fake typed client. Assert returned text equals the complete original UTF-8 file content, including final newline, Unicode, multiple rows, mixed web/paper row order and whitespace; do not require reserialization.
- Exercise an empty successful result file and assert successful empty text, not an error, placeholder, null or an automatic second search.
- Exercise partial provider success through the real daemon acceptance case; adapter itself only sees a success response and does not infer failure from row count.
- Assert Fetch success is returned exactly as received, including empty text if supplied by the controlled peer, existing UNAVAILABLE_MESSAGE, and a string that resembles an absolute path. Verify Fetch never opens files.
- Assert search response contains no added server path, metadata wrapper, count, summary or reordering; files are unchanged after the call. Test that one call reads only its returned file, never lists or chooses a different result.

### 4.3 Result-file safety

| Rejected case | Assertion |
|---|---|
| Relative path or explicit traversal | Tool failure, no out-of-directory content |
| Absolute path outside root or sibling root with similar prefix | Tool failure |
| Wrong kind, extension or request ID | Tool failure even if a real file exists |
| Directory, FIFO or another nonregular file | Prompt failure without blocking on a FIFO |
| Symlink to external file or final-component symlink | Rejected; external data never returned |
| Missing file / disappearance between validation and read | Safe read failure |
| Permission denied or other read failure | Safe read failure without raw paths |
| Invalid UTF-8 or partial read followed by failure | No partial successful content |
| Path replacement race at opening | Validate the actual opened object; never follow an external symlink |

Use platform-supported controlled race/fault injection, not privileged operations, for permission and replacement cases. Check the POSIX no-follow and regular-file guarantees at the actual read boundary. A configured root with a legitimate resolved location must still accept genuine writer-produced files. The local same-user filesystem trust assumption does not imply protection against arbitrary malicious mutation by another process with equivalent OS privileges.

For each failed read, assert the RPC call count remains one, no output file is modified or removed, no path or synthetic secret appears in client-visible errors, and no fallback file is opened. Where relevant, prove blocking reads are offloaded without serializing unrelated async calls and opened resources are released after failure/cancellation.

### 4.4 Gateway and transport failures

- Parameterize every existing ErrorCode from errors.py as ErrorResponse. Assert conversion to failure with that code, safe actionable text, no second RPC, and no successful business result. Verify separately that typed GatewayError raised locally retains its code.
- Test DaemonUnavailable independently, because it is not a GatewayError subclass. Report daemon startup guidance without a local socket path.
- Test ProtocolFailure, permission/reset/broken-pipe errors, unexpected EOF/malformed frame, and invalid response object. Use controlled socket fixtures for framing failures where the real send_request behavior is relevant. Preserve existing protocol frame limits, including oversized response rejection.
- Inject unexpected ordinary exceptions containing synthetic secret and local-path markers; confirm safe generic error information rather than raw repr or traceback.
- Verify cancellation propagates instead of becoming successful text or ordinary business failure. Use a recording/cancellable client and a real socket peer to verify connection cleanup without shutdown, replay, or new implicit deadline.
- Verify two independent blocked RPCs can enter concurrently with no adapter semaphore or serialization. Use barriers, not exact elapsed time assertions.

## 5. MCP server and entry-point tests

### 5.1 Tool registration and protocol contract

Use SDK-backed tool discovery and invocation, not merely mocks of Python functions. For both transports, assert exactly these public tool names:

```text
keyword_search
paper_search
llm_search
agent_search
url_fetch
```

Verify each advertised input schema has the documented required fields, optional nullable selectors/focus, scope choices and default web. Descriptions communicate the result format, filter behavior and URL admission prerequisite for Fetch. No daemon lifecycle, config, credentials or arbitrary file-read tool is exposed. Do not depend on irrelevant SDK-generated schema formatting or list order.

For valid calls verify the real MCP call result: successful text content containing the exact daemon-derived value, no path wrapper, no JSON string double-encoding or new structured result contract; empty results remain successful. For business failures verify the SDK result has `isError: true`, preserves applicable existing gateway error codes and does not return an ordinary success string. For SDK-level malformed inputs verify appropriate validation/protocol rejection and no socket request; the exact SDK-owned JSON-RPC error wording is not a gateway contract. Unsupported arguments must not alter behavior or trigger unexpected upstream work.

### 5.2 Executable and dependency boundaries

- Verify `agent-search-gateway-mcp --help` succeeds even without SDK and exposes STDIO/Streamable HTTP choices.
- Default transport is STDIO. Explicit STDIO selects only STDIO; explicit Streamable HTTP uses the default loopback host/8765 unless options override them. Check options with a stub server to avoid binding production defaults.
- Reject invalid transport, invalid port, empty host, and HTTP-only flags in STDIO with nonzero exit and usage on stderr. Test explicit non-loopback host warning and preservation of SDK protections.
- A missing optional extra emits installation guidance to stderr with nonzero exit; unrelated import/runtime bugs must not be misreported as missing extra. Use a real isolated CLI-only installation for final acceptance, not just sys.modules mutation.
- HTTP bind or SDK startup failure is reported safely and exits unsuccessfully. No daemon startup, stop or restart request occurs during any launch or failure.
- Verify existing `agent-search-gateway --help`, `--version`, and CLI command behavior with and without the optional extra; existing CLI imports/execution do not load MCP. Verify importing the new package marker alone has no runtime side effects.
- Verify the entry point invokes the SDK transport runner and supports shutdown of only its own resources. STDIO startup diagnostics do not contaminate stdout protocol frames.

## 6. Real transport acceptance

### 6.1 Common contract suite

Run the same representative valid, empty-success, daemon-business-error and invalid-input cases over STDIO and Streamable HTTP. Use the official MCP client for initialization, tools/list and tools/call, and actual transport IO. Compare semantic tool schemas and returned text/error flags across both; do not compare irrelevant SDK session identifiers or JSON frame formatting.

For each transport, cover all five tools with correct mapping to the existing requests. Confirm at least one real daemon Search writes a JSONL file which is read by the MCP process and returned to the client. Check that Fetch returns final text and errors do not disrupt subsequent valid calls. SDK calls must exercise the actual registration/serialization code, not a fake MCP server.

### 6.2 STDIO

- Launch the installed entry point or production module entry in a test-owned child process with an isolated HOME and separate stderr capture. The client sends real protocol messages over stdin/stdout and initializes successfully.
- Discover and call tools with daemon absent. Verify an explicit tool error (startup guidance), then start a controlled daemon and call again successfully without restarting the STDIO process.
- Verify stdout is exclusively SDK protocol traffic. Unexpected logging, startup banners, path output or raw traceback on stdout are failures.
- Close the client and check child process cleanup within the test bound while the independent daemon remains responsive to CLI.

### 6.3 Streamable HTTP

- Start the actual HTTP transport on a test-owned loopback endpoint at /mcp. Initialize the official Streamable HTTP client, exercise discovery and tools/call. Assert expected endpoint and protocol behavior; do not treat a successful TCP connection alone as a passing MCP smoke test.
- Verify independent/concurrent client calls, session cleanup and later calls after individual tool failures. Session state is SDK-owned and cannot change business state ownership.
- With daemon absent, discovery still works and business call fails safely. Start daemon and verify later call succeeds within the same running HTTP server, with no implicit daemon management.
- Exercise disallowed Host/Origin requests against the SDK-protected endpoint and verify rejection before daemon work. Exercise legitimate loopback requests and the documented trusted proxy host configuration if applicable to the selected SDK. Explicit non-loopback binding must not silently disable protections. Authentication and TLS termination are external deployment responsibilities and not implemented or asserted as native MCP features.
- Verify a controlled port-binding conflict is safely reported. Stop only the HTTP server/session and verify the daemon remains reachable to another client.
- Exercise transport disconnect/cancellation where the SDK supports it; never assert that HTTP disconnect must always cancel daemon execution. Assert no automatic replay or ShutdownRequest and no dependency on the cancelled session for future calls.

## 7. Cross-interface acceptance with the real daemon

Reuse production ForegroundDaemon, runtime/orchestrators, ResultWriter and Unix socket with controlled providers; keep all tests offline. A stub returning predetermined text is insufficient for the shared-state cases below.

| Scenario | Required evidence |
|---|---|
| CLI keyword Search -> STDIO Fetch | Search admits URL; existing Fetch workflow succeeds using same daemon |
| HTTP keyword Search -> CLI Fetch | MCP result text matches written JSONL; CLI Fetch uses admitted URL |
| STDIO and HTTP Fetch same already-ready URL | Existing URLStore reuse and no repeated Fetch/Safety work |
| Search through any interface with a naturally returned body | Existing deferred processing semantics; no Judge/Clean/Safety/Focus from search itself |
| Paper Search -> Fetch | Landing URL is admitted; PDF metadata URL is not independently admitted merely by being returned |
| LLM/Agent scope and selectors | At least representative all/web/paper and matched/unmatched selector cases use same daemon semantics and existing schemas; exhaustive core parsing remains covered by existing tests |
| One provider succeeds, another fails | Return existing partial-success JSONL; do not add a provider retry or postprocessing step |
| All selected providers fail or no provider selected by valid filter | Existing ErrorResponse becomes tool error; no fallback outside daemon rules |
| Shared credential pool / scheduling | Interleave requests from CLI, STDIO and HTTP to the same logical provider; recording fake upstream proves one shared credential selection and rate/capacity gate, not separate quota state |
| Omitted limits | Multiple independent calls reach controlled provider concurrently; no additional adapter global cap |
| Daemon restart while MCP is running | In-flight outcome follows existing daemon contract; future calls reconnect; stale admission may be lost; a new Search permits subsequent Fetch |
| Stop/close one MCP frontend | Other frontend and CLI continue to work; no ShutdownRequest or daemon termination |
| Client cancellation after request send | Local request/transport cleanup; no auto-resubmission; do not require daemon cancellation or forbid existing result-file completion |

Use request counters, stable synthetic credential identifiers, barriers or manual clock to prove the shared-limit cases. Do not infer shared scheduling merely from concurrent success or repeat the complete credential pool algorithm test suite. Compare existing result file content and the transport result; assert exact equality without introducing new public result schemas. Both MCP transports must use the same adapter and daemon; no separate Runtime is constructed by either frontend.

## 8. Regression, packaging and CI

The current .github/workflows/ci.yml has a quality job on Linux Python 3.12, tests on Linux Python 3.11–3.14 plus macOS Python 3.12, docs build, wheel smoke, secret scan and final verify. Extend existing jobs minimally rather than add a separate CI architecture.

- Lock: add the official SDK as the optional `mcp` extra in pyproject.toml and update uv.lock. Verify the selected constraint resolves across the supported Python matrix and preserves CLI-only installation.
- Quality: sync the locked dev environment with the MCP extra so Ruff and strict mypy cover the new modules and tests without missing imports. Preserve existing audit and lint/type-check behavior; do not suppress checks across the repository.
- Matrix tests: install the extra in the current test matrix and execute the full offline suite including both transport tests. A test skip caused by an absent optional dependency is not passing MCP acceptance in this matrix. Keep existing live-test opt-in disabled in CI.
- Package smoke: build the wheel and verify two isolated installation modes from the artifact: without extras (existing CLI works, MCP help works and execution gives missing-extra guidance; SDK not installed), and with the MCP extra (both commands installed; real MCP initialization and tool discovery or equivalent executable smoke verified). Use temporary tool environments; do not overwrite a developer installation. Do not conflate a successful --help call with a functioning MCP server.
- Docs: keep existing documentation build and artifact validation passing. Check that API documentation generation remains compatible with lazy/optional MCP imports. If the docs generator legitimately needs the extra to document the new module, adjust only its environment or the new module boundary; do not make the SDK a required runtime dependency. Verify README installation examples, entry point, transport defaults, endpoint, shared-daemon prerequisite, security warnings, result format and optional dependency instructions.
- Retain existing secret scan and final CI gate. Record a changelog entry and public interface documentation without changing existing CLI contracts or manually bumping the version as part of implementation unless separately required by release process.

Proposed verification commands (exact environment options can be finalized by the implementation plan):

```bash
uv sync --locked --group dev --extra mcp
uv run --extra mcp ruff format --check src tests scripts
uv run --extra mcp ruff check .
uv run --extra mcp mypy src tests scripts
uv run --extra mcp pytest -v tests/mcp
WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest -v
uv build
```

Also run the existing locked dependency audit and documentation verification as configured in CI. Verify the wheel's base and extra installations in separate disposable environments. The commands above are intended implementation verification, not commands executed during design.

## 9. Acceptance gate and reporting

The implementation is acceptable when:

1. The full existing offline test suite passes and MCP-specific tests pass with no missing-dependency skips across the existing supported CI matrix.
2. Both actual transport clients discover the same five tools and verify the public success/error contracts, including empty searches and precise argument forwarding.
3. Cross-interface tests demonstrate shared URL admission, cached Fetch state, credential pool and rate/capacity behavior, plus independent MCP/daemon lifecycles.
4. File path, nonregular-file, UTF-8, failure disclosure and Host/Origin cases demonstrate the new boundary is safe under the documented same-user trust model.
5. No automatic replay, provider invocation, new overall timeout, global limit or implicit daemon management is introduced by the new layer; cancellation limitations are explicitly documented.
6. Both wheel install modes, formatting, lint, typing, dependency audit, docs and existing compatibility checks pass.
7. Review the implementation diff against the architecture's allowed file scope; no changes to existing core Python modules or config.example.toml. If a genuine SDK/platform compatibility blocker or core-boundary deviation is found, report it and obtain a design decision rather than weakening the contract silently.

Report actual commands/results, any skipped tests with reasons, dependency/platform compatibility limitations and deviations. No real provider calls are necessary for deterministic acceptance. Optional live smoke tests, if separately performed by the operator, must be reported separately and cannot substitute for the offline tests.
