# Error Handling: MCP Server (STDIO and Streamable HTTP)

Date: 2026-11-27
Baseline: v3.0.0, commit `dabfbbc`
Companion: [Architecture](../architectures/20261127-mcp-server.md)
Status: proposed for review

## 1. Principles and ownership

- Preserve the existing daemon, socket protocol, error taxonomy, provider retries, rate limits, URL state and CLI behavior. Do not add a daemon error code, modify a core module, or introduce a new retry/cooldown/circuit-breaker mechanism.
- The SDK owns MCP initialization, JSON-RPC, schema validation, transport/session errors, and shutdown of its own resources. The adapter owns semantic argument normalization, typed socket calls, safe local result reading, and translation to safe frontend failure information. Only server.py depends on the SDK and constructs MCP-specific tool errors.
- Business execution errors are unsuccessful MCP tool results (`isError: true`) with safe text; they are not ordinary successful text results or HTTP 5xx responses for an otherwise valid tool call. Invalid protocol messages, unsupported methods, authentication at an external proxy, and schema/transport failures use the SDK/proxy mechanisms.
- Each accepted business call makes at most one socket request. Existing provider/credential retries within the daemon remain unchanged. No automatic re-execution after a socket, decoding, result-file, delivery, cancellation, or timeout failure, including ambiguous completion.
- STDIO and Streamable HTTP share the same adapter and business failure semantics. A single failed tool call normally leaves the MCP server usable. Process startup or transport-level failures are a separate boundary.
- No new business timeout, capacity gate, persistence, or state reconciliation. Never use a tool failure as justification to start, stop or restart the daemon.

## 2. Error boundaries and expected outcomes

| Failure / outcome | Owner | MCP behavior | Subsequent behavior |
|---|---|---|---|
| Invalid command option, transport, host/port syntax, or HTTP-only option used with STDIO | Entry point | Usage error on stderr, nonzero exit; no business work | Correct options and relaunch |
| Optional SDK missing | Entry point | Installation guidance on stderr, nonzero exit; help still works without extra | Install package with MCP extra |
| SDK initialization, HTTP bind, transport startup failure | Entry point / SDK | Safe diagnostic on stderr and nonzero exit when startup fails | Fix deployment, relaunch; daemon untouched |
| Invalid MCP frame, handshake, method, tool name or argument schema | SDK | SDK-defined protocol/validation error | No socket call for rejected inputs; preserve SDK session semantics |
| HTTP Host/Origin or session rejection | SDK | SDK-defined transport rejection; no tool execution | Correct client/proxy configuration, not disable protections |
| Blank query/prompt, blank supplied selector, invalid scope/URL | Adapter / SDK | SDK validation error where applicable; otherwise tool error with corresponding existing input code | No socket call after frontend rejection |
| Daemon socket absent or connection refused | Adapter | Tool error: daemon unavailable; instruct operator to run `agent-search-gateway start` | Next call attempts its own connection; server stays running |
| Connection reset, permission error, broken pipe, malformed or oversized response, unexpected EOF | Adapter / existing protocol client | Safe tool error; retain existing protocol_error when provided | No retry; outcome may be unknown |
| Valid daemon ErrorResponse | Adapter / server | Tool error retaining existing ErrorCode value and safe message | Existing daemon state and recovery rules apply |
| Valid search success with readable result file | Adapter | Exact JSONL text; success even if file is empty | No second search or file mutation |
| Search success with invalid/missing/unreadable result file | Adapter | Tool error: result unavailable or invalid | No repeat search, no deletion or repair |
| Valid Fetch success including existing unavailable message | Adapter | Exact daemon text as success | Existing URL state and cached result behavior preserved |
| Client cancels or disconnects before response | SDK / socket client | Propagate cancellation when delivered; no synthetic successful result | Close local socket if cancelled; daemon may still execute |
| Unexpected ordinary exception | Boundary where it occurs | Safe generic failure, stderr diagnostic without sensitive details | No retry; future independent calls can proceed |

A client-side transport failure after a request was sent cannot establish that the daemon did not execute it. Do not label such an error as safe to retry automatically. Tool responses are not a new stable machine-readable exception schema; use MCP's error flag and preserve the existing daemon code when applicable.

## 3. Request and daemon error mapping

### 3.1 Input errors

Mirror the existing CLI boundary:
- Trim query and prompt; reject empty input using `empty_query`.
- Optional provider/model selectors: omitted or null means no selection; trim supplied strings, reject empty strings using `bad_request`. Exact case-sensitive matching and selection validity remain daemon-owned.
- Scope must be web, paper or all; default web. Invalid scope is rejected by schema/semantic validation without a socket request.
- Reuse `normalize_url` and its existing `invalid_url` behavior. Trim focus; blank focus becomes None. Do not implement separate admission or arbitrary fetch behavior.
- Unknown tools, fields/types rejected by SDK schema and malformed protocol messages must not enter business execution. No new request type is sent to daemon.

### 3.2 Gateway errors

Existing `errors.py` defines these codes; the MCP layer does not change them.

| Existing code | Handling / safe guidance |
|---|---|
| bad_request | Report invalid request, selector or scope; preserve safe validation information |
| empty_query | Query or prompt must not be empty |
| invalid_url | Supply a valid supported URL |
| url_not_admitted | Search must admit the URL before Fetch; do not auto-search |
| no_keyword_search_providers | No keyword search providers available for this request |
| no_llm_search_providers | No LLM search providers available for this request |
| no_agent_search_providers | No Agent search providers available for this request |
| no_url_fetch_providers | No Fetch providers available for this request |
| no_academic_search_providers | No academic search providers available for this request |
| all_providers_failed | All relevant providers failed; consult daemon diagnostics |
| llm_stage_failed | Existing LLM stage failed; consult daemon diagnostics |
| protocol_error | Daemon or local protocol failure; consult daemon diagnostics |
| config_error | Gateway configuration error; operator should check daemon configuration |
| daemon_shutting_down | Daemon shutting down; later independent calls can reconnect |

Handle both typed GatewayError from local validation/protocol and ErrorResponse from the daemon. Retain the existing code as text in a tool error. Reuse messages when known safe; otherwise substitute concise code-appropriate guidance rather than reflect arbitrary upstream/provider text, local paths or exception strings. Adapter-only failures (e.g. DaemonUnavailable or result-file failure) need no invented core ErrorCode. DaemonUnavailable is a separate exception, not a subclass of GatewayError. Ordinary unexpected exceptions must not escape with raw traceback or repr into tool results.

Partial provider success and successful empty search already have a defined daemon outcome; do not infer failure from number of rows. The existing `UNAVAILABLE_MESSAGE` is returned by Fetch as a successful text response for an unavailable URL, not an ErrorResponse. Preserve that behavior, distinct from `url_not_admitted` and an execution failure. Never inspect result prose to decide whether it is an error.

## 4. Result-file error handling and security

Only the four search tools interpret SuccessResponse.text as a file path. Fetch returns SuccessResponse.text literally, including strings resembling paths. The caller cannot choose a file path, filename prefix or result directory.

- Require an absolute path identifying an expected result file in the configured `RuntimePaths.results_dir`. The existing writer uses direct-child names `{kind}-{request_id}.jsonl` where kind is keyword, paper, llm or agent and request ID is eight lowercase hexadecimal characters. Enforce the correct kind for the originating request and reuse the existing naming/validation convention (see request_ids.py); do not independently define a new result naming scheme.
- Reject relative paths, traversal, wrong kind/extension/ID, external paths (including sibling directories with similar names), directories, special files and symlink escapes. Prefer rejecting a final-component symlink altogether because the existing writer creates regular result files. Resolve the configured root and enforce path containment, not string prefix matching.
- Safely open only the expected regular file; on POSIX, a directory-relative open with no-follow and a regular-file check on the opened descriptor is one implementation option. Avoid opening/blocking on special files (e.g. FIFO); handle races, disappearance, and permission errors as failures, not fallbacks to another path. The local same-user filesystem is a trusted deployment assumption, not an adversarial isolation guarantee.
- Read UTF-8 preserving text/line endings, off the event loop if using blocking I/O, without parsing, trimming, sanitizing the successful business content, summarizing, clipping or reordering. Invalid UTF-8 is a read failure. Empty file is valid.
- A missing or invalid file after daemon success is a tool error: report that the search result is unavailable or invalid. Never include its path in client error text; never retry the billable search, substitute another result, unlink, rename or rewrite files. Do not return partial file content after read/decode error.
- A result may exceed client/transport capacity. This version introduces no arbitrary result length cap or silent truncation; if the response cannot be delivered, use the SDK/transport failure behavior and do not re-run the workflow. Operators must understand that large searches can consume memory and context.

## 5. Cancellation, timeouts and lifecycle

The existing `send_request` uses a fresh socket for each request and closes it in a finally block. With its default response timeout of None there is no fixed overall client-side business deadline. Preserve the default and existing wire frame limits. Provider and credential waiting/attempt policies remain daemon-owned.

- If MCP Client or SDK delivers cancellation to the awaiting task, propagate cancellation (do not catch it as an ordinary business error). Existing socket cleanup closes the connection. Any local file read must close its file descriptor; if offloaded to a thread, cancellation may stop waiting before the read itself finishes, so the worker is responsible for closing it.
- HTTP disconnect does not necessarily deliver cancellation to the tool handler; do not promise that disconnect always interrupts a tool. Once a socket request is sent, daemon execution may continue and may write result files after the client stops waiting. This is the existing CLI behavior, not a new guarantee of remote cancellation.
- On daemon shutdown/restart, in-flight work follows the existing daemon behavior. MCP does not reconnect/replay in-flight requests. A later independent invocation opens a new socket and can succeed when the daemon is available; previously admitted URLs may need a new search after restart.
- Closing one MCP transport/session must never issue ShutdownRequest or terminate the daemon. SDK startup, session and process cleanup are independent of daemon lifecycle.

## 6. Error reporting and observability

Use safe, actionable messages, e.g.:
- `daemon unavailable: start the daemon with agent-search-gateway start`
- `url_not_admitted: search must admit the URL before fetching it`
- `protocol_error: invalid daemon response; consult daemon logs`
- `search result unavailable or invalid`

For these examples the exact wording is illustrative; error/success distinction and existing gateway code when available are contractual. No new public JSON error shape, no second debug log, no raw provider response/credential/config/headers/argv/environment values, prompt bodies, raw exception messages containing paths, or exception traces in tool errors. Keep protocol frames exclusively on STDIO stdout; launch/transport diagnostics go to stderr. Use existing daemon workflow logging and error details for operator investigation; do not create another request-ID scheme.

The SDK-specific behavior of validation, error serialization and transport cancellation must be verified in the implementation plan using the selected official SDK version; do not assume an ordinary string return represents an MCP tool error.

## 7. Acceptance criteria for this design

- Both transports expose equivalent error semantics for the same valid tool call.
- Invalid inputs produce no daemon request; one accepted call produces at most one socket request, including all failure paths.
- MCP discovery works with daemon absent; a failing call is recoverable by a new call after daemon starts, without restarting MCP.
- All daemon error codes remain meaningful, empty/partial success is preserved, unavailable Fetch remains existing success text, and no error is disguised as success.
- Files outside the expected result directory, nonregular files, invalid names, unreadable/invalid UTF-8 files and missing files cannot become successful tool content; no local path leaks or file mutation and no retry.
- No new adapter retries, global limits, implicit daemon management or cancellation guarantees. No existing core file needs modification.
- SDK/entrypoint errors do not corrupt STDIO protocol output. Existing CLI and daemon behavior remain compatible.

Detailed executable cases and fixtures belong in the companion Testing Spec, after this error handling section is approved.
