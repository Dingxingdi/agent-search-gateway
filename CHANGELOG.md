# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Because the project is pre-1.0, minor releases may contain breaking changes when they are called out in the release notes.

The first public release is `v0.1.0`.

## [Unreleased]

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
