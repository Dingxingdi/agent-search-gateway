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

### Request-scoped provider and model filtering

Use `--provider` to debug one configured search backend without changing the daemon configuration:

```bash
agent-search-gateway keyword-search "query" --provider tavily
agent-search-gateway paper-search "topic" --provider arxiv
agent-search-gateway llm-search "prompt" --provider openai_main
agent-search-gateway llm-search "prompt" --model gpt-5
agent-search-gateway llm-search "prompt" --provider openai_main --model gpt-5
```

For `keyword-search`, the provider must be enabled for keyword search. For `paper-search`, it must be an enabled academic discovery provider; Unpaywall is an OA resolver, not a discovery provider. Fetch-only or disabled providers cannot be selected for search.

For `llm-search`, provider names are configured aliases referenced by `[[search_llm.providers]]`, not transport protocol names such as `openai`. Only resolved search invocations count: an alias or model configured solely for a global default or fetch stage is not a valid search selector. The example aliases and models above must exist in your own search configuration; these example values are not guaranteed defaults.

`--provider` alone selects every search invocation for that alias, possibly across several models. `--model` alone selects every invocation with that exact model name, possibly across several aliases. Supplying both selects their intersection. Matching invocations keep their configured order and duplicates; multiple matches are not an ambiguity error. These selectors also work with every LLM search scope:

```bash
agent-search-gateway llm-search "prompt" --scope paper --provider openai_main
agent-search-gateway llm-search "prompt" --scope all --provider openai_main --model gpt-5
```

In `--scope all`, both the web and paper branches use the same selected invocations. Matching is exact and case-sensitive. The CLI trims surrounding whitespace, but does not split comma-separated names or interpret wildcards. Explicitly empty or whitespace-only values are rejected before connecting to the daemon. Unknown selectors never fall back to all providers.

When both LLM selectors are supplied, errors distinguish four cases: neither provider nor model exists among search invocations; only the provider is missing; only the model is missing; or both exist individually but not as a configured pair. Invalid selectors use `BAD_REQUEST` internally; the CLI prints the explanatory message to stderr and exits unsuccessfully. An empty configured search collection retains its existing no-provider error. If all selected providers fail, an unselected healthy provider is not called as a fallback.

Filters apply only to the current request. Omitting them on the next request restores the normal full search fan-out. The daemon still assembles all enabled providers and validates their configuration and credentials at startup; these flags do not bypass another provider's startup checks. They do not filter `fetch_llm` stages such as judging, safety, cleaning, or focus summaries, and normal OA enrichment can still run after selected paper discovery. `url-fetch`, `start`, `stop`, and `doctor` do not accept these flags; `--model` is available only on `llm-search`.

After upgrading to code that supports these flags, stop and restart an already-running daemon once to load the new code. Changing selectors after that requires no restart. Restarting clears in-memory URL admission and cached content, so search again before fetching a URL.

To verify routing, start the daemon with `--debug`, run a filtered search, and correlate its result filename's request ID with the DEBUG search-stage events. Only selected search providers or invocations should appear in those stages; legitimate downstream judge or OA events may still name other providers. Successful search stdout remains only the absolute result-file path, with no added selection diagnostics.

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
