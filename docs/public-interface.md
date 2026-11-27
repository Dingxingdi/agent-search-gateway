# Public interface and compatibility policy

`agent-search-gateway` retains its alpha development status. Starting with `1.0.0`, the project commits to compatibility for its documented user-facing interfaces.

## Supported user-facing interfaces

The `3.x` compatibility policy covers these public interfaces.

- the `agent-search-gateway` executable and the commands documented in the README and `--help` output;
- the optional `agent-search-gateway-mcp` command, documented tool arguments and MCP text/error contracts;
- the configuration path and documented TOML fields, including supported provider settings omitted from the selected examples in `config.example.toml`;
- the rule that credential-bearing configuration fields name environment variables instead of storing credential values;
- the successful business-command stdout contract described in the README;
- the documented JSONL result schemas for keyword, LLM-assisted, mixed, and academic-paper searches;
- the documented local runtime paths for the socket, result files, and debug log.

The top-level `--version` and `-v` flags print `agent-search-gateway <version>` using the installed CLI package version and exit successfully without reading configuration or contacting a daemon. They are not subcommand-local flags.

Changes to those interfaces require tests, documentation, and a changelog entry.

The request-scoped `--provider` flag on `keyword-search`, `paper-search`, and `llm-search`, and the independent `--model` flag on `llm-search`, are supported CLI interfaces. Selection is exact and case-sensitive within the enabled search-provider or configured search-invocation namespace; LLM providers are configuration aliases, not protocol names. Both LLM filters select their intersection. Omitted filters preserve default behavior, and invalid selections fail without falling back to unselected backends. Filters do not apply to `url-fetch` or change normal post-search processing.

### MCP server

The optional `agent-search-gateway-mcp` executable uses STDIO by default (`--transport stdio`). Its stdout carries only protocol messages, and diagnostics go to stderr. With `--transport streamable-http`, connect to `http://127.0.0.1:8765/mcp` by default. Only HTTP accepts `--host` and `--port` (1–65535). Start the daemon separately for business calls. Both transports expose the same five tools and no management tools.

| Tool and arguments | Operation |
|---|---|
| `keyword_search(query, provider=null)` | Search enabled web providers by keyword. |
| `paper_search(query, provider=null)` | Search enabled academic providers. |
| `llm_search(prompt, scope="web", provider=null, model=null)` | Search with configured LLM invocations. |
| `agent_search(prompt, scope="web", provider=null, model=null)` | Search with configured Agent invocations. |
| `url_fetch(url, focus=null)` | Fetch a URL already admitted by search in the current daemon, optionally focused on a topic. |

Arguments are named JSON fields with strict types. The required `query`, `prompt`, or `url` must be a nonblank string. The allowed `scope` strings are `web` (default), `paper`, and `all`. Omit `provider`, `model`, or `focus` or pass JSON `null` to use the default. A supplied provider or model must be a string and is matched exactly and case-sensitively after trimming. A blank selector is rejected. Supplying both filters selects their intersection. Focus is trimmed, and a blank focus is equivalent to null.

A successful call returns one `TextContent` with `isError` false. Each search returns the complete JSONL text, preserving row order, whitespace, and final newlines. An empty search returns the empty string. Fetch returns the daemon's literal text, including the successful unavailable message `URL unavailable: it may have failed content validation or been flagged as unsafe.` Fetch text is never interpreted as a filename.

Unknown tools and schema violations (including extra fields, wrong types, or an invalid scope) produce a JSON-RPC `INVALID_PARAMS` error. Business failures return one `TextContent` with `isError` true, safe guidance, and the existing gateway error code when applicable. For example, fetching a URL that search has not admitted returns `url_not_admitted`. For client configuration, lifecycle and the HTTP trust boundary, see [MCP client setup](../README.md#mcp-client-setup).

### HTTP provider configuration

**Breaking configuration change:** every enabled or referenced HTTP Web, academic, OA and LLM provider requires a nonempty ordered `credentials` array, declared with `[[namespace.provider.credentials]]`. Put existing endpoint fields, environment-variable authentication references and optional limits in each member. Endpoint names remain `api_url` or the provider's existing dedicated fields; TinyFish retains both `search_api_url` and `fetch_api_url`. Provider tables retain enable flags, `use_proxy`, business options and LLM protocol settings. Model and invocation tables keep their existing locations. Write provider fields before its first credentials table and use full paths for subsequent business subtables.

Old provider-level endpoint/authentication/limit fields and `default_max_concurrency`, including mixed old/new configurations, are rejected with migration errors. Move them manually into each member. To retain previous concurrency, explicitly set its former value on each applicable member; omission now means no capacity gate. Upgrades do not convert user configuration.

`credential_strategy` is `round_robin` by default, or `fill_first`. Round Robin advances stable array positions when selecting; Fill First selects the first currently available member on every call, so an unrestricted first member can handle every call. Existing HTTP and LLM protocol retries finish within the selected credential. Only terminal provider execution failure tries another untried member, for one pass through the pool. Empty results, OA no-match and accepted partial success complete successfully. Failures create no memory, cooldown or persistent disablement.

`[web_providers].fetch_order` orders logical Fetch providers. Enabled providers omitted from the list append in TOML declaration order; this is ordering, not filtering. A disabled registered Fetch provider may appear; unknown, duplicate and search-only names are errors. Scheduling chooses a currently ready pool without waiting for a higher-priority limited pool. Credential fallback completes before trying another provider. A rejected body moves to the next logical provider.

For example, two independently configured Jina endpoints can omit all limits:

```toml
[web_providers.jina]
enable_fetch = true
credential_strategy = "round_robin"

[[web_providers.jina.credentials]]
api_url = "https://r.jina.ai"

[[web_providers.jina.credentials]]
api_url = "https://r.jina.ai"
```

This demonstrates array ordering and absent gates; duplicate endpoints do not create independent upstream allowances. Recommended settings in the rate reference must be configured explicitly.

Even authentication-free endpoints require credential members. arXiv, anonymous Jina and open-webSearch need no invented key; official You.com free MCP forbids key configuration, while its REST endpoint requires it. Academic and OA key/email references belong to the member. Bright Data members may override the provider's common zone options.


Web, academic, OA resolver and LLM provider tables accept `use_proxy` as a strict boolean, defaulting to `true`. The default preserves HTTPX environment proxies and `NO_PROXY`. Setting it to `false` connects that provider directly while preserving TLS certificate and hostname verification and CA environment settings: `SSL_CERT_FILE` takes precedence over `SSL_CERT_DIR`. This field applies only to HTTP providers and does not affect external Agent subprocess networking or its `pass_env` rules. Edit the existing configuration and restart the daemon normally to apply changes; upgrades do not rewrite user configuration.

Firecrawl Search returns metadata without requesting page scraping; page bodies require explicit Fetch. Its documented options are `search_limit` (integer 1–100, default 10), `search_highlights` (boolean, default true), `scrape_formats` (nonempty unique list of `markdown`/`rawHtml`, default `["markdown"]`) and `scrape_parsers` (unique list of `pdf`/`image`, default `[]`). Parser choices affect supplier costs; base64-only PDF responses are not usable text and can use existing Fetch fallback. See the [README](../README.md#firecrawl-search-and-explicit-fetch) for current supplier rules and billing caveats.

arXiv supports `query_fields` (nonempty ordered unique list of `ti`/`abs`, default `["ti", "abs"]`), `max_results` (integer 1–100, default 10), `sort_by` (`relevance`/`submittedDate`/`lastUpdatedDate`, default `relevance`) and `sort_order` (`ascending`/`descending`, default `descending`). Ordinary terms are AND-connected, fields within each term are OR-connected, and quoted phrases stay grouped. There is one logical query with existing retries, without LLM rewriting or a second retrieval pass. Literal inputs that cannot preserve their meaning in upstream syntax fail the arXiv provider with `PROTOCOL_ERROR` before HTTP; successful sibling providers remain usable. Long natural-language AND queries may reduce recall.

### Provider rate policies, credentials and input limits

Each HTTP credential accepts independently optional `rate_limits`, `min_interval_seconds` and `max_concurrency`. Windows contain a positive integer `requests` count and positive `period_seconds`; minimum intervals space physical attempts. Every physical HTTP attempt counts, including parallel branches and retries. Search and Fetch share the same member's policy. Capacity counts a logical operation including internal retries. Omitted limits create no gates and inherit no global or provider cap. HTTPX has unlimited active connections; its 20 idle keepalive connections are not an active-work limit. Agent limits remain at the CLI alias and count invocations; Agent has no HTTP credentials or multi-account fallback. See the [provider rate reference](provider-rate-limits.md) for all 32 services and explicit example policies; these examples are not runtime defaults or live verification.

`[retry].request_timeout_seconds` bounds credential selection waiting across rate and capacity checks. Rechecks consume the same budget. The scheduler and first send share the selected candidate's remaining wait budget. Existing per-attempt HTTP timeouts and Retry-After handling remain in effect; no new whole-operation deadline is added.

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

All business CLI commands (`keyword-search`, `paper-search`, `llm-search`,
`agent-search` and `url-fetch`) wait for the daemon response without a default
deadline. Version 2.0 removed the 600-second default for non-Agent CLI requests.
Automation that relied on that limit must set a deadline in its caller or job runner.
Interrupting or disconnecting the CLI ends the client wait. The daemon can continue
the request and write results.
`stop` also waits without a default response deadline: if the daemon accepts the
connection but does not acknowledge shutdown, it waits until the connection closes
or the operator interrupts it.
Internal callers' explicit numeric socket timeouts remain honored. Agent process
deadlines and Provider HTTP timeouts remain in effect. External agy duration parsing
may reject extreme values; accepting a configuration does not guarantee that agy
supports every representable duration. Process-group cleanup retains its
OS-blocked-spawn and escaped-descendant limitations.

The gateway does not promise tool execution or source access based solely on terminal
success. These additions do not turn Python imports or the socket protocol into a
stable SDK.

### Deferred URL body processing

Search does not request full page text and performs no cheap check, Judge, Clean, Safety or Focus stage. Naturally returned bodies are retained as candidates. Tavily Search omits `include_raw_content` and Exa Search omits `contents.text`; their Fetch behavior is unchanged. Exa summaries retain the first highlight, then summary/title fallback; Firecrawl uses description metadata.

URL records hold exactly URL, raw content, content, abstract and status. The six statuses are `need_fetch`, `need_judge`, `need_clean`, `need_safety`, `ready` and `unavailable`. Metadata-only Search admission starts at `need_fetch`; natural body candidates start at `need_judge`.

Fetch requires an admitted URL. Under the existing per-URL lock it rereads state, judges cached candidates, cleans when required and runs Safety once for the final body. Cached-body rejection clears both body fields and continues actual Fetch. Pure execution failure then leaves `need_fetch`; stage errors retain the last completed state for a later retry. A ready body reuses its Safety result, without TTL or refresh. Focus summaries are request-specific and never cached. Unavailable records retain the existing unavailable response.

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

`doctor` makes no network requests when `open_websearch` is absent or both Search and Fetch are disabled. With valid configuration and either capability enabled, it sends one credential-free `GET /health` per configured open-webSearch member. A healthy response confirms daemon liveness only. A failed probe produces a `[fail]` check and exit code 1. A missing gateway Unix socket is independently reported as `[info] daemon not running`. A report containing only `[ok]` and `[info]` checks exits with code 0.

Doctor validates every enabled or referenced credential separately, including later members, and reports namespace/provider/index labels. A successful member cannot hide another member's configuration or health failure. Existing applicable health checks do not perform paid Search or Fetch or measure balances.

`doctor` messages and DEBUG events are intended for operators, but their exact wording, event set, ordering, and metadata fields may evolve within the `3.x` series. Automation should prefer result files and documented command outcomes over parsing diagnostic text.

DEBUG diagnostics cover Web search/Fetch, academic/OA, HTTP, LLM, and Agent operations.
HTTP attempts, received responses, scheduled retries, and terminal HTTP failures are
distinct events. Actual HTTP status is separate from an upstream status carried inside
the response; byte counts describe content actually read. LLM protocol retries and
stage events are separate from the owning pipeline's provider terminal outcome.
Search, Fetch, and OA owners record one terminal outcome for each invocation and
scope; the daemon records the whole workflow outcome. An individual provider failure
need not fail a partially successful workflow. Unpaywall 404 remains a completed
lookup with no match.

HTTP credential operations, capacity and rate gates use the logical provider name,
a namespace (web, academic, oa, or llm), and a zero-based `credential_index`.
Selection, fallback, exhaustion, waiting and cancellation events omit the index when
no member is known. These fields do not expose authentication values, and are not
part of public result rows. Agent alias gates have no HTTP credential index.
`url_state_committed` records only completed, atomic URL-state changes, never an
uncommitted transition or the page body.

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

Consumers can automate the gateway through its CLI or the documented MCP tools.

## Versioning

Tagged releases follow Semantic Versioning for the supported public interfaces.

- Patch releases contain backward-compatible fixes.
- Minor releases add backward-compatible functionality.
- Breaking changes require a major release and must be called out in its release notes.

Security fixes may remove unsafe behavior without a full deprecation cycle.

See [CHANGELOG.md](../CHANGELOG.md) for changes and [RELEASING.md](../RELEASING.md) for the release process.
