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
