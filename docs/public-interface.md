# Public interface and compatibility policy

`agent-search-gateway` retains its alpha development status. Starting with `1.0.0`, the project commits to compatibility for its documented user-facing interfaces.

## Supported user-facing interfaces

The `1.x` compatibility policy covers these public interfaces.

- the `agent-search-gateway` executable and the commands documented in the README and `--help` output;
- the configuration path and documented TOML fields, including supported provider settings omitted from the selected examples in `config.example.toml`;
- the rule that credential-bearing configuration fields name environment variables instead of storing credential values;
- the successful business-command stdout contract described in the README;
- the documented JSONL result schemas for keyword, LLM-assisted, mixed, and academic-paper searches;
- the documented local runtime paths for the socket, result files, and debug log.

The top-level `--version` and `-v` flags print `agent-search-gateway <version>` using the installed CLI package version and exit successfully without reading configuration or contacting a daemon. They are not subcommand-local flags.

Changes to those interfaces require tests, documentation, and a changelog entry.

The request-scoped `--provider` flag on `keyword-search`, `paper-search`, and `llm-search`, and the independent `--model` flag on `llm-search`, are supported CLI interfaces. Selection is exact and case-sensitive within the enabled search-provider or configured search-invocation namespace; LLM providers are configuration aliases, not protocol names. Both LLM filters select their intersection. Omitted filters preserve default behavior, and invalid selections fail without falling back to unselected backends. Filters do not apply to `url-fetch` or change normal post-search processing.

### Provider rate policies, credentials and input limits

Web, academic and OA provider tables accept `rate_limits`, an array of windows with a positive integer `requests` count and a positive `period_seconds` duration. `min_interval_seconds` independently sets minimum spacing between starts. Each physical request, including retries, counts against all windows for its provider in that daemon. Web Search and Fetch share them. `max_concurrency` governs in-flight work separately. Omission leaves the local rate policy empty. Existing configurations are not automatically rewritten. See the [provider rate reference](provider-rate-limits.md) for all 31 services, including unpublished or account-dependent values. Source coverage does not imply live verification.

`[retry].request_timeout_seconds` bounds local rate waiting per attempt. Fetch can select a different ready provider. The scheduler and the first send share the selected candidate's remaining wait budget. A rate-wait timeout allows fallback to other candidates. Waiting only for concurrency, running another candidate, and `Retry-After` sleeps do not use that budget.

**Breaking configuration change.** Enabled OpenAlex requires `api_key_env` naming a nonempty credential in the daemon environment (for example, `OPENALEX_API_KEY`). Supply the variable and restart, or disable OpenAlex. The gateway uses Bearer authentication with no anonymous fallback. `contact_email_env` is optional and cannot replace the key. This policy is stricter than the upstream basic anonymous mode.

Under `[input_limits]`, the defaults are `llm_request_body_bytes = 131072` for the complete serialized LLM JSON request and `agent_prompt_bytes = 98304` for the single serialized Agent prompt argument. Both count UTF-8 bytes independently of model tokenization. Oversized variable text retains roughly 80% of its kept bytes from the head and 20% from the tail. The gateway preserves instructions, template framing and protected user focus. If protected content and framing cannot fit, `input_budget_unfit` ends preparation before a call. A known OS single-argument limit or embedded NUL can also reject a fitted Agent prompt during preflight. The OS still enforces aggregate argv/environment limits at spawn and an `E2BIG` rejection maps to `argument_limit`.

### Agent search contract

`agent-search PROMPT --scope web|paper|all` is a supported CLI command with default
scope `web` and independent `--provider` and `--model` filters. Agent providers use
the `agent_providers` namespace and `search_agent.providers` invocation list.
`harness = "antigravity"`, `command`, `max_concurrency`, `timeout_seconds`, and
`pass_env` follow the documented types, defaults, and bounds in the README.
Proxy variables, like other extra environment variables, must exist in the daemon's
environment and be explicitly named in `pass_env`; the gateway does not assign their
values or load shell startup files. Configure the uppercase and/or lowercase
`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY` names required by the Harness
in this allowlist. If the launcher stores proxy addresses under different names,
map them to the standard names only in the daemon launch environment; keep addresses
and credentials out of TOML. Restart the daemon after changing its environment or
configuration.

Invocation `model` is optional. Omission uses the Harness's saved default by
omitting its execution `--model` flag; it never inherits an LLM model.
The gateway's `--model` only matches explicitly configured models and never
overrides an unpinned invocation. Exact case-sensitive selectors preserve order
and duplicate invocations; a mismatch performs no Agent work and has no fallback.

Successful stdout is only the absolute `agent-<request_id>.jsonl` path.
Web rows retain exactly `url` and `abstract`; paper rows retain the existing
paper schema with `agent:<alias>` provenance. Mixed results add a `type`
discriminator and put Web rows before paper rows. Empty successful branches and
partial success follow the documented search behavior; complete failure writes
no result file. Paper landing admission and PDF metadata rules are shared.

`fetch_llm` remains mandatory through existing stage inheritance, while
`search_llm` and `search_agent` are independently optional. Agent execution is a
fresh noninteractive process per invocation, without gateway retries. The
configured single-process deadline and output limits apply. `timeout_seconds` defaults
to 300 and accepts representable finite positive numbers without a Gateway-imposed
upper bound. Each agy invocation still receives that timeout and has Python protection
at the configured timeout plus 5 seconds, starting after quota acquisition. Quota
waiting, separate scope calls, and paper OA enrichment can make total workflow time
longer than one process budget; there is no fixed Gateway deadline for the whole
Agent workflow.

The Agent socket response wait has no fixed default deadline. Cancellation and
disconnection still end the wait and trigger active process cleanup as applicable;
an unresponsive but connected daemon can leave the client waiting until interrupted.
Internal callers' explicit numeric socket timeouts remain honored. Non-Agent socket
response waits retain their 600-second default. External agy duration parsing may
reject extreme values; accepting a configuration does not guarantee that agy supports
every representable duration. Process-group cleanup retains its OS-blocked-spawn and
escaped-descendant limitations.

The gateway does not promise tool execution or source access based solely on terminal
success. These additions do not turn Python imports or the socket protocol into a
stable SDK.

### Keyword result URL identity

At the shared keyword-result entry point, before normal URL normalization and
admission, the gateway unwraps absolute HTTP(S) links with hostname exactly
`google.com` or `www.google.com`, path exactly `/goto`, and exactly one `url` query
parameter containing a valid HTTP(S) destination. That value is decoded once, and
the target becomes the identity used for deduplication, cached-body merging, and
subsequent `url-fetch`. Well-formed wrappers carrying opaque URL-safe `CAES...`
tokens remain unchanged, so their search results retain the wrapper URL identity.
Their underlying destination is not resolved or deduplicated with direct links.
Other recognized wrappers with an invalid destination are discarded as individual
candidates; unknown URL shapes outside this rule pass through existing URL rules.
There is no network redirect following, recursive decoding, or migration of old
admitted links. The rule applies to every keyword provider, independently of its
adapter, and does not rewrite LLM, Agent, or paper-search URLs.

## Diagnostic interfaces

`doctor` makes no network requests when `open_websearch` is absent or both Search and Fetch are disabled. With valid configuration and either capability enabled, it sends one credential-free `GET /health` to the configured open-webSearch daemon. A healthy response confirms daemon liveness only. A failed probe produces a `[fail]` check and exit code 1. A missing gateway Unix socket is independently reported as `[info] daemon not running`. A report containing only `[ok]` and `[info]` checks exits with code 0.

`doctor` messages and DEBUG events are intended for operators, but their exact wording, event set, ordering, and metadata fields may evolve within the `1.x` series. Automation should prefer result files and documented command outcomes over parsing diagnostic text.

DEBUG diagnostics cover Web search/Fetch, academic/OA, HTTP, LLM, and Agent operations.
HTTP attempts, received responses, scheduled retries, and terminal HTTP failures are
distinct events. Actual HTTP status is separate from an upstream status carried inside
the response; byte counts describe content actually read. LLM protocol retries and
stage events are separate from the owning pipeline's provider terminal outcome.
Search, Fetch, and OA owners record one terminal outcome for each invocation and
scope; the daemon records the whole workflow outcome. An individual provider failure
need not fail a partially successful workflow. Unpaywall 404 remains a completed
lookup with no match.

Success diagnostics retain metadata and counts. Failure diagnostics add allowlisted
upstream code/status/type and short messages when safely extractable. Adapter and
keyword staging candidate counts have separate phases, grouped rejection reasons,
and processed/unprocessed counts when processing stops early. Agent diagnostics
include classifications, exit status, and output byte counts after cleanup.

Upstream error metadata is redacted and sanitized. Code/status/type fields are limited to 128 characters and messages to 512. Logged URLs omit userinfo, query and fragment. Every formatted log record includes UTC wall-clock `time` with millisecond precision and a trailing `Z`. `elapsed_ms` describes elapsed duration.

Ordinary mode emits no body previews. With opt-in `start --debug`, each actual HTTP send and each attempted Agent spawn produces one `upstream_body_preview` event. It pairs the prepared request with the HTTP response body or captured Agent stdout. An absent response has a null preview. Preflight failures and rate waits that never reach send or spawn produce no preview. For each side, both the redacted preview text and its final JSON-escaped field value fit within 8192 UTF-8 bytes, including any omission marker. `request_input_truncated` records shortening of the outbound input. `request_preview_truncated` and `response_preview_truncated` record output clipping, incomplete capture, or omission caused by the preview inspection budget. A complete body that cannot be decoded or safely scrubbed instead has a null preview with its actual byte count; safety omission alone does not set a truncation flag. When the body is absent, the preview is null, the byte count is zero, and the flag is false. Events exclude header contents, raw stderr, environment values, full argv, credential values and discarded input middles.

Preview preparation inspects at most 262144 UTF-8 bytes in each raw body or individual text being redacted, including original text used by input-truncation previews. When this work limit is exceeded, the affected preview is null and its `request_preview_truncated` or `response_preview_truncated` flag is true. The event retains the actual byte counts, and request processing continues with its normal result.

DEBUG output can contain private text and URL paths. Review it before sharing. The opt-in `debug.log` rotates at 5 MiB with 3 backups. Business-command stdout and public result and error schemas retain their documented contracts.

## Internal interfaces

The following implementation details may change without deprecation unless explicitly documented otherwise.

- imports from `agent_search_gateway` Python modules other than `__version__`;
- provider adapter classes and registries;
- orchestrator, scheduler, store, parser, and model internals;
- the local Unix-domain socket wire protocol;
- in-memory admission, body, and unavailable-state representation;
- test helpers and design-document code samples.

The package is currently distributed as a CLI application, not as a stable Python SDK. Consumers should invoke the CLI rather than importing internal modules.

## Versioning

Tagged releases follow Semantic Versioning for the supported public interfaces.

- Patch releases contain backward-compatible fixes.
- Minor releases add backward-compatible functionality.
- Breaking changes require a major release and must be called out in its release notes.

Security fixes may remove unsafe behavior without a full deprecation cycle.

See [CHANGELOG.md](../CHANGELOG.md) for changes and [RELEASING.md](../RELEASING.md) for the release process.
