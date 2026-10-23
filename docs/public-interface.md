# Public interface and compatibility policy

`agent-search-gateway` is pre-1.0 alpha software. This document distinguishes supported user-facing interfaces from implementation details so contributors and users can judge compatibility risk.

## Supported user-facing interfaces

The following interfaces are public for the current `0.x` line:

- the `agent-search-gateway` executable and the commands documented in the README and `--help` output;
- the configuration path and TOML fields represented by `config.example.toml`;
- the rule that credential-bearing configuration fields name environment variables instead of storing credential values;
- the successful business-command stdout contract described in the README;
- the documented JSONL result schemas for keyword, LLM-assisted, mixed, and academic-paper searches;
- the documented local runtime paths for the socket, result files, and debug log.

Changes to those interfaces require tests, documentation, and a changelog entry. A patch release should not intentionally break them.

The request-scoped `--provider` flag on `keyword-search`, `paper-search`, and `llm-search`, and the independent `--model` flag on `llm-search`, are supported CLI interfaces. Selection is exact and case-sensitive within the enabled search-provider or configured search-invocation namespace; LLM providers are configuration aliases, not protocol names. Both LLM filters select their intersection. Omitted filters preserve default behavior, and invalid selections fail without falling back to unselected backends. Filters do not apply to `url-fetch` or change normal post-search processing.

### Agent search contract

`agent-search PROMPT --scope web|paper|all` is a supported CLI command with default
scope `web` and independent `--provider` and `--model` filters. Agent providers use
the `agent_providers` namespace and `search_agent.providers` invocation list.
`harness = "antigravity"`, `command`, `max_concurrency`, `timeout_seconds`, and
`pass_env` follow the documented types, defaults, and bounds in the README.

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
documented process, workflow, socket, and output limits apply. The gateway does
not promise tool execution or source access based solely on terminal success.
These additions do not turn Python imports or the socket protocol into a stable SDK.

## Diagnostic interfaces

`doctor` messages and DEBUG events are intended for operators, but their exact wording, event set, ordering, and metadata fields may evolve during the `0.x` series. Automation should prefer result files and documented command outcomes over parsing diagnostic text.

Debug output is not a safe telemetry export by default. It can contain target URL components and operational metadata. Review and redact it before sharing.

## Internal interfaces

Unless explicitly documented otherwise, the following are implementation details and may change without deprecation during the `0.x` series:

- imports from `agent_search_gateway` Python modules other than `__version__`;
- provider adapter classes and registries;
- orchestrator, scheduler, store, parser, and model internals;
- the local Unix-domain socket wire protocol;
- in-memory admission, body, and unavailable-state representation;
- test helpers and design-document code samples.

The package is currently distributed as a CLI application, not as a stable Python SDK. Consumers should invoke the CLI rather than importing internal modules.

## Versioning

The project follows Semantic Versioning once tagged releases begin:

- patch releases contain backward-compatible fixes;
- minor `0.x` releases may contain breaking changes, which must be called out in release notes;
- a stable compatibility commitment begins at `1.0.0`.

Deprecation periods before `1.0.0` are best effort. Security fixes may remove unsafe behavior without a full deprecation cycle.

See [CHANGELOG.md](../CHANGELOG.md) for changes and [RELEASING.md](../RELEASING.md) for the release process.
