# Public interface and compatibility policy

`agent-search-gateway` is pre-1.0 alpha software. This document distinguishes supported user-facing interfaces from implementation details so contributors and users can judge compatibility risk.

## Supported user-facing interfaces

The following interfaces are public for the current `0.x` line:

- the `agent-search-gateway` executable and the commands documented in the README and `--help` output;
- the configuration path and documented TOML fields, including supported provider settings omitted from the selected examples in `config.example.toml`;
- the rule that credential-bearing configuration fields name environment variables instead of storing credential values;
- the successful business-command stdout contract described in the README;
- the documented JSONL result schemas for keyword, LLM-assisted, mixed, and academic-paper searches;
- the documented local runtime paths for the socket, result files, and debug log.

The top-level `--version` and `-v` flags print `agent-search-gateway <version>` using the installed CLI package version and exit successfully without reading configuration or contacting a daemon. They are not subcommand-local flags.

Changes to those interfaces require tests, documentation, and a changelog entry. A patch release should not intentionally break them.

The request-scoped `--provider` flag on `keyword-search`, `paper-search`, and `llm-search`, and the independent `--model` flag on `llm-search`, are supported CLI interfaces. Selection is exact and case-sensitive within the enabled search-provider or configured search-invocation namespace; LLM providers are configuration aliases, not protocol names. Both LLM filters select their intersection. Omitted filters preserve default behavior, and invalid selections fail without falling back to unselected backends. Filters do not apply to `url-fetch` or change normal post-search processing.

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
subsequent `url-fetch`. A recognized wrapper with an invalid destination is discarded
as one candidate; unknown URL shapes, including unsupported opaque formats outside
this rule, pass through existing URL rules.
There is no network redirect following, recursive decoding, or migration of old
admitted links. The rule applies to every keyword provider, independently of its
adapter, and does not rewrite LLM, Agent, or paper-search URLs.

## Diagnostic interfaces

`doctor` messages and DEBUG events are intended for operators, but their exact wording, event set, ordering, and metadata fields may evolve during the `0.x` series. Automation should prefer result files and documented command outcomes over parsing diagnostic text.

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

Upstream diagnostic text is redacted before sanitizing and truncating; code/status/type
fields are limited to 128 characters and messages to 512. Logged URLs omit userinfo,
query, and fragment. Full payloads, prompts, page/model-response bodies, Agent stdout
or stderr content, and authentication values are not intentionally logged. Normal
CLI stdout/stderr contracts and public result/error schemas are unchanged.

Debug output is not a safe telemetry export by default. URL paths and operational
metadata can still be sensitive. Review and redact it before sharing.

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
