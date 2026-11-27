# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Starting with 1.0.0, breaking changes to documented public interfaces require a major release.

The first public release is `v0.1.0`.

## [Unreleased]

## [3.1.0] - 2026-11-27

### Added

- Added the optional `agent-search-gateway-mcp` command for STDIO and Streamable HTTP. Its five tools use the existing daemon and return full search JSONL or literal Fetch text to clients.

### Fixed

- Include the optional MCP dependencies in release verification and documentation builds.

## [3.0.0] - 2026-11-26

### Changed

- **Breaking configuration change.** HTTP Web, academic, OA and LLM providers now require ordered `[[namespace.provider.credentials]]` arrays. Move existing endpoint, environment-variable authentication and limit fields into each member. Old provider-level fields and `default_max_concurrency` are rejected, including mixed configurations. User files are never converted automatically; write previous concurrency values explicitly to preserve them.
- Limits are independently optional, with no global/provider default cap. Capacity counts logical operations including retries; rate policies count physical HTTP attempts. HTTPX has unlimited active connections. Agent limits remain optional CLI-invocation limits on each alias.
- Credential pools select with `round_robin` (default) or `fill_first`. Existing retries finish on one credential before terminal execution failure tries another untried member, for one pass without failure memory or cooldown. Empty results and partial success do not switch credentials.
- `web_providers.fetch_order` orders enabled Fetch providers, appending unlisted enabled providers in TOML order and preferring currently ready pools. Credential fallback precedes provider fallback.
- Search retains naturally returned body candidates without running body validation stages or requesting full text. Fetch advances explicit URL states, rejects cached candidates without mixing body pairs, and reuses READY content and its Safety result. Focus summaries remain request-local; admission, unavailable responses and the absence of refresh/TTL remain unchanged.
- Doctor validates and labels every configured HTTP credential separately, reports its configured or unlimited concurrency and rate limits, and runs only existing applicable health checks without paid Search/Fetch probes.

### Fixed

- Discard natural search body fields that cannot be encoded as UTF-8 while preserving usable metadata and any valid body field from the same result, so later Fetch can proceed.
- Return `LLM_STAGE_FAILED` consistently when an LLM credential's rate wait times out, including during physical HTTP retries.

## [2.0.0] - 2026-11-24

### Changed

- **Breaking CLI waiting behavior.** Remove the 600-second default response deadline from `keyword-search`, `paper-search`, `llm-search`, `url-fetch`, and `stop`; these commands now wait indefinitely while the daemon keeps the connection open. Automation that requires bounded waits must enforce a deadline in its caller or job runner. Explicit internal numeric timeouts, Agent process deadlines and Provider HTTP timeouts remain effective; interrupting the CLI does not cancel background work.
- Firecrawl Search now requests metadata only; explicit Fetch sends configurable formats and parsers, defaulting to Markdown with no parsers. PDF parsing choices can affect supplier charges.
- arXiv queries combine title/abstract fields with OR and terms with AND, preserve quoted phrases, and support bounded result and sorting options. Unrepresentable literal queries fail only that provider without rewriting the user's meaning.
- HTTP Web, academic, OA and LLM providers support per-provider `use_proxy`, defaulting to environment proxies; direct mode retains TLS verification and configured CA sources. The Zhihu example uses direct mode.

## [1.2.0] - 2026-11-24

### Added

- Add the opt-in `baiduqianfan` keyword search provider. It queries Baidu Qianfan Web Search and Agentic Search concurrently and returns results from a successful branch when the other fails.
- Check open-webSearch daemon liveness in `doctor` when Search or Fetch is enabled. One credential-free `GET /health` checks the service, and a failed probe gives exit code 1.

### Changed

- Clarify Zhihu rate limits. Zhihu has no published uniform fixed QPS, so the gateway leaves the request window and minimum interval unset by default.

## [1.1.0] - 2026-11-24

### Added

- Use [You.com's keyless free MCP Search and Extract endpoint](docs/youcom-free-mcp.md) through the existing `youcom` provider by replacing its configuration table and omitting `api_key_env`. The default keyed REST configuration remains available; free MCP failures follow existing provider fallback behavior without switching to paid You.com REST.

## [1.0.0] - 2026-11-20

### Added

- Configure provider-wide rolling request windows and minimum spacing for Web, academic and OA requests. Fetch can select another ready provider when local rate waiting would block. The [rate reference](docs/provider-rate-limits.md) documents account conditions and examples for all 31 services.
- Bound serialized LLM requests and Agent prompt arguments in UTF-8 bytes through `[input_limits]`. Oversized variable input retains its head and tail while protecting instructions and user focus.

### Changed

- **Breaking configuration change for enabled OpenAlex.** Add `api_key_env` to the existing table, set it to the name of a daemon environment variable such as `OPENALEX_API_KEY`, supply the credential, and restart. To skip OpenAlex, disable it. The gateway sends Bearer credentials with no anonymous fallback. Contact email remains optional.
- Log records include UTC timestamps with milliseconds. Opt-in DEBUG tracing adds one bounded, redacted request/response preview per HTTP send or Agent spawn attempt, including retries and failed calls. Previews can contain private text. Ordinary runs emit no body previews.
- Apply semantic-versioning compatibility guarantees to the documented public interfaces from 1.0.0. Internal Python modules and the local socket protocol remain implementation details.

### Security

- Bound DEBUG preview inspection and redact credential fields, private-key blocks, and sensitive URL components before rendering. Preserve byte counts when a safe preview cannot be produced.

## [0.4.0] - 2026-11-17

### Added

- Add independent Search and Fetch providers for Doubao, You.com, Keenable, and an existing open-webSearch HTTP daemon. Include disabled configuration examples, credential and permission guidance, optional open-webSearch engine selection with a shared 50-result budget, safe partial-failure diagnostics, and opt-in live checks.

## [0.3.0] - 2026-11-17

### Added

- Added search-only Zhihu (site and global search) and Tencent Yuanbao SearchPro providers through the existing Web provider configuration. Zhihu retains usable results if one branch fails and refreshes its authentication timestamp for each HTTP attempt; Yuanbao summaries remain search snippets, not fetched page bodies.

### Fixed

- Respect valid server `Retry-After` waits on retryable HTTP statuses in the shared request executor, including Semantic Scholar and arXiv requests. The configured attempt budget stays unchanged, and a server-directed wait may exceed the local backoff cap.

### Known limitations

- Bright Data can still occasionally return HTTP 200 followed by a JSON parsing failure; the cause remains unconfirmed and this change does not add retries for malformed successful responses.

## [0.2.2] - 2026-11-14

### Fixed

- Remove fixed Gateway limits on Agent timeout configuration, the whole Agent workflow, and the default Agent socket response wait. `timeout_seconds` still defaults to 300 and requires a representable finite positive number; each agy call retains its configured timeout, Python protection at timeout plus 5 seconds, cancellation, and process cleanup. Quota waits, separate scope calls, and OA enrichment may extend total time. The CLI timeout argument preserves the configured value without six-digit rounding or exponent notation. External agy duration limits still apply; non-Agent socket waits remain 600 seconds.
- Add bounded, redacted DEBUG diagnostics for successful and failed Web search/Fetch, academic/OA, HTTP, LLM, and Agent operations. Separate HTTP attempts/responses/retries, LLM protocol/stage work, per-invocation provider terminal outcomes, and whole workflows; retain safe upstream error details, actual status/byte metadata, and independent adapter/staging candidate counts without logging full content. Redact quoted credential fields and replace entire matched URLs in upstream messages, including literal, escaped, and percent-encoded forms, so unregistered credentials in their paths or hosts are not retained. Correct success diagnostics on failing HTTP statuses and retain safe metadata for malformed model JSON through protocol retries and final stage failures. Unpaywall 404 remains a completed lookup with no match; normal CLI output and result/error schemas are unchanged.
- Unwrap supported Google `/goto?url=...` links once at the shared keyword-result entry point before URL normalization, deduplication, admission, and cached-body merging, so later `url-fetch` uses the destination identity. Only HTTP(S) links on exact `google.com` or `www.google.com` hosts with exact `/goto` paths and one valid HTTP(S) destination are supported. Preserve well-formed opaque URL-safe `CAES...` wrappers under their existing identity instead of dropping results; their underlying destinations remain unresolved. Discard other recognized invalid wrappers individually and preserve unknown URL shapes without network redirects, recursive decoding, historical migration, or changes to LLM/Agent/paper URLs (#74).

## [0.2.1] - 2026-11-10

### Added

- Added top-level `--version` and `-v` flags to print the installed CLI package version without requiring configuration or a running daemon.

### Fixed

- Preserve ordinary Bright Data HTTP retries when transient outer and inner statuses are the only failure metadata, retaining the shared attempt budget and exact HTTP backoff.
- Increased the non-Agent CLI daemon-response wait from 300 to 600 seconds so the example's three 180-second HTTP attempts and backoff can finish before the client deadline.
- Recognize Bright Data SERP failures reported through response headers before JSON decoding, including HTTP 200 with an empty body. Recoverable errors share the existing HTTP attempt budget with bounded retry jitter and safe DEBUG diagnostics for retry decisions and terminal failures.

### Changed

- Simplified `config.example.toml` by omitting the ZenRows, Decodo, ScrapingDog, and ScrapeGraphAI examples while retaining their built-in support. The example request timeout is now 180.0 seconds; the runtime fallback remains 30.0 seconds and existing user configurations are unchanged.
- Documented observed dblp bot verification even at low API request frequency, with an optional configuration change to disable dblp.

## [0.2.0] - 2026-11-07

### Added

- Added Antigravity-backed `agent-search` with `--scope web|paper|all`, exact `--provider` / `--model` selection, independent `agent_providers` and `search_agent` configuration, and optional Harness-default models. Results use `agent-<request_id>.jsonl`, shared Web/Paper schemas, and `agent:<alias>` paper provenance. Fresh subprocess invocations have bounded output, per-alias concurrency, process-group cleanup, workflow deadlines, and safe diagnostics without whole-Agent retries. `fetch_llm` still must resolve; LLM and Agent search are independently optional.
- Added request-scoped `--provider` filtering to `keyword-search`, `paper-search`, and `llm-search`, plus independent `--model` filtering for `llm-search`. Filters select configured search backends before execution, preserve unfiltered defaults, and report distinct provider/model mismatch errors without falling back to other backends.

### Fixed

- Added concrete `transport_type` exception names to HTTP transport retry and terminal failure events without logging exception messages or changing retry behavior (#77).
- Corrected the TinyFish Search and Fetch endpoints in the example configuration. Existing installations must update their configuration and restart the daemon; see the README migration guidance (#73).

### Changed

- Documented the known Google `goto` wrapper URL limitation in ScraperAPI and Scrape.do search results, including how to disable their search independently of fetch. Provider behavior is unchanged; the gateway does not resolve these wrappers (#74).

### Security

- Raised the pytest floor to 9.0.3, upgraded the locked test stack, and added locked dependency auditing to CI and release verification to remediate CVE-2025-71176.

## [0.1.0] - 2026-09-02

### Added

- Open-source contribution, security, support, conduct, interface-stability, and release documentation.
- Hardened continuous integration with enforced Ruff formatting, least-privilege permissions, immutable action references, Linux/Python and macOS compatibility coverage, packaging checks, documentation builds, and layered secret scanning.
- A GitHub Pages documentation site with a generated pdoc reference and safe deployment gating until Pages is enabled.
- Automated dependency update configuration and a tag-driven, privilege-separated GitHub Release workflow with build provenance attestations.

### Security

- Audited the complete Git history, fully paginated issue and pull-request text, review comments, Actions logs, artifacts, releases, and Wiki state before the open-source readiness change.
