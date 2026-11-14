# agent-search-gateway

[![CI](https://github.com/Dingxingdi/agent-search-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Dingxingdi/agent-search-gateway/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`agent-search-gateway` is a local foreground daemon plus a thin CLI for aggregated keyword search, LLM and Agent search, academic discovery, and fetching URLs admitted by search.

Version 0.1 keeps URL admission and body state in daemon memory. Restarting the daemon clears that state. The daemon communicates over a local Unix-domain socket, so this version targets Unix-like environments. It does not provide persistent URL state, remote daemon access, automatic repairs, or live provider checks in `doctor`.

## Installation

The project requires Python 3.11 or newer on a Unix-like system. Install [uv](https://docs.astral.sh/uv/), clone a tagged release, and install the CLI into an isolated tool environment:

```bash
git clone https://github.com/Dingxingdi/agent-search-gateway.git
cd agent-search-gateway
uv tool install .
```

For a reproducible installation, check out a specific release tag before running `uv tool install .`. Tagged GitHub Releases also contain a wheel and source distribution built by CI. The repository `uv.lock` controls development and CI; the installed tool environment is resolved independently. PyPI publishing is not configured yet.

Create `~/.config/agent-search-gateway-cli/config.toml` from `config.example.toml` and adjust the enabled providers. Only providers that require credentials use `api_key_env`; export the environment variables named by those entries. The configuration stores environment-variable names only; credential values remain in the daemon process environment.

After configuration, run the local health check before starting the foreground daemon:

```bash
agent-search-gateway doctor
agent-search-gateway start
```

For detailed daemon tracing, start it in DEBUG mode instead:

```bash
agent-search-gateway start --debug
```

`doctor` is local and makes no network requests. It validates configuration/environment resolution, local filesystem usability, and daemon socket state. A missing socket is reported as `[info] daemon not running`; that state is informational and does not make an otherwise healthy doctor run fail. `doctor` does not repair files or sockets and does not contact configured providers.

## Configuration and runtime paths

`config.example.toml` contains a selection of supported providers. ZenRows, Decodo, ScrapingDog, and ScrapeGraphAI remain supported but are omitted from this template. Its request timeout is 180.0 seconds; the runtime fallback when that setting is omitted remains 30.0 seconds. Changes to the template do not rewrite an existing user configuration.

The daemon reads:

```text
~/.config/agent-search-gateway-cli/config.toml
```

Runtime files are stored under:

```text
~/.cache/agent-search-gateway-cli/daemon.sock
~/.cache/agent-search-gateway-cli/results/
~/.cache/agent-search-gateway-cli/logs/debug.log
```

The results directory is created by the daemon as needed. The debug log is created only by `agent-search-gateway start --debug`; ordinary `start` does not create or append it.

### Web provider capabilities

| Provider | Keyword search | URL fetch |
|---|---:|---:|
| Tavily | yes | yes |
| Firecrawl | yes | yes |
| Exa | yes | yes |
| Linkup | yes | yes |
| Brave | yes | no |
| AnySearch | yes | no |
| TinyFish | yes | yes |
| Parallel | yes | yes |
| Bright Data | yes | yes |
| Scrape.do | yes | yes |
| ZenRows | no | yes |
| Decodo | yes | yes |
| ScrapingDog | yes | yes |
| ScrapeGraphAI | yes | yes |
| ScraperAPI | yes | yes |
| ScrapingAnt | no | yes |
| SerpApi | yes | no |
| Jina Reader | no | yes |

Parallel accepts optional Search mode `turbo`, `fast`, `basic`, or `advanced`, plus independent Search and Extract fetch policies. Extract always requests full content internally, while Search result count is left to Parallel's provider default. When enabling Parallel, set `api_key_env` to the name of the environment variable that holds the credential.

Jina Reader is a credential-free, fetch-only provider, so its configuration must omit `api_key_env`. Each actual Jina Reader request sends `X-No-Cache: true` to refresh Reader-side content. This does not bypass content already prepared by the gateway: an admitted URL whose body is already prepared still short-circuits before provider dispatch, and the gateway exposes no force-refresh option. Configuration order remains fetch-provider priority, so place Jina earlier when free-first behavior is desired.

Web search and fetch stages for the same provider share one concurrency quota. LLM provider transports have their own independent quotas.

#### TinyFish endpoint migration

TinyFish uses separate public Search and Fetch services. In an existing `~/.config/agent-search-gateway-cli/config.toml`, replace the legacy `https://api.tinyfish.ai/search` and `https://api.tinyfish.ai/fetch` values with the following endpoints:

```toml
[web_providers.tinyfish]
search_api_url = "https://api.search.tinyfish.ai"
fetch_api_url = "https://api.fetch.tinyfish.ai"
```

Edit these two keys in the existing provider block; keep its credential reference and enable flags unchanged. Upgrading the package or changing `config.example.toml` does not update an existing user configuration. Restart the daemon after updating its configuration; restarting clears in-memory URL admission and cached content, so search again before using `url-fetch`. Deliberately configured custom endpoints remain supported and are not automatically rewritten.

#### Known provider limitations

ScraperAPI (`scraperapi`) and Scrape.do (`scrape_do`) can return Google `google.com/goto` wrapper URLs in organic search results; [issue #74](https://github.com/Dingxingdi/agent-search-gateway/issues/74) documents the observed limitation. At the shared keyword-result entry point, the gateway unwraps absolute HTTP(S) URLs whose hostname is exactly `google.com` or `www.google.com`, path is exactly `/goto`, and query contains exactly one `url` parameter with a valid HTTP(S) destination. It decodes that query value once, before normal URL normalization, deduplication, admission, and cached-body merging, so later `url-fetch` uses the destination identity.

A recognized wrapper with a missing, duplicate, empty, malformed, or unsafe destination is discarded individually. Unknown URL shapes, including unsupported opaque formats outside this rule, pass through unchanged to the existing URL rules. The gateway does not follow network redirects, recursively unwrap destinations, or decode opaque tokens. This shared rule covers matching links from every keyword provider; it does not rewrite LLM, Agent, or paper-search URLs, migrate previously admitted URLs, or establish that a particular provider has returned this shape in a live test.

If unsupported wrapper formats affect your results, use another search provider or set `enable_search = false` in the `[web_providers.scraperapi]` and `[web_providers.scrape_do]` blocks. Their `enable_fetch` flags are independent and may remain enabled. Restarting clears previously admitted links and requires searching again before fetching URLs.

## Commands

Print the installed CLI package version without configuration or a running daemon:

```bash
agent-search-gateway --version
agent-search-gateway -v
```

Run the daemon in the foreground:

```bash
agent-search-gateway start
```

Stop the daemon:

```bash
agent-search-gateway stop
```

Run the local diagnostic command:

```bash
agent-search-gateway doctor
```

Run keyword search:

```bash
agent-search-gateway keyword-search "query text"
```

Run direct academic paper search:

```bash
agent-search-gateway paper-search "research topic"
```

Run LLM-assisted search. The default scope remains `web`, preserving the pre-academic-search output contract:

```bash
agent-search-gateway llm-search "research prompt"
```

Use the paper-only LLM grammar or run web and paper branches concurrently:

```bash
agent-search-gateway llm-search "research prompt" --scope paper
agent-search-gateway llm-search "research prompt" --scope all
```

### Request-scoped provider and model filters

Use `--provider` to select an enabled search provider for one request. `llm-search` also accepts `--model`; either flag can be used alone or together:

```bash
agent-search-gateway keyword-search "query" --provider tavily
agent-search-gateway paper-search "topic" --provider arxiv
agent-search-gateway llm-search "prompt" --provider openai_main
agent-search-gateway llm-search "prompt" --model gpt-5
agent-search-gateway llm-search "prompt" --provider openai_main --model gpt-5
```

These example provider aliases and models must exist in your enabled search configuration; they are not guaranteed defaults. Selector values are exact and case-sensitive after the CLI trims surrounding whitespace. Each flag takes one name; comma-separated lists, wildcards, and partial matching are not supported.

For keyword and paper search, `--provider` matches the enabled search provider's name, such as `tavily` or `arxiv`. Fetch-only or disabled providers do not match. For LLM search, it matches the configured provider alias in `[[search_llm.providers]]`, not the transport protocol name `openai` or an unrelated `llm_providers` entry.

LLM selection preserves configured order and every matching invocation, including duplicate provider/model pairs:

- Provider only runs all search models configured for that alias.
- Model only runs that model across every matching search provider alias.
- Both flags run their intersection: only invocations with that provider and model.
- No flags run all configured search invocations, as before.

The same selection applies to each LLM scope. With `--scope all`, both branches use the selected invocations:

```bash
agent-search-gateway llm-search "prompt" --provider openai_main --model gpt-5 --scope paper
agent-search-gateway llm-search "prompt" --provider openai_main --model gpt-5 --scope all
```

An explicitly empty or whitespace-only selector is an input error. Unmatched selectors fail before provider work begins and never fall back to unselected backends. When both LLM flags are supplied, the error distinguishes a missing provider, a missing model, both names being absent, and both names existing separately without a matching provider/model pair. A runtime with no search providers retains its existing no-provider error; if every selected execution fails, the existing all-providers-failed error still applies.

Filters affect search fan-out only. Keyword result judging configured through `fetch_llm` and normal paper OA enrichment can still run. `url-fetch`, `start`, `stop`, and `doctor` do not accept these flags. The daemon still validates and assembles all enabled providers at startup, so selecting one provider does not bypass startup validation for other enabled providers.

Selection lasts for one request; the next command without flags uses the full configured set. After installing a version with these flags, stop and restart an already-running older daemon once to load the new code. Subsequent selector changes do not require restarting. Restarting clears in-memory URL admission and cached content, so search again before fetching previously admitted URLs.

For a smoke test, start the daemon with `--debug` and correlate the result filename's request ID with the search-stage provider/model events. Only the selected search pipelines should execute; legitimate judge or OA enrichment events may name other services. Successful search stdout remains only the absolute result-file path.

## Agent search

Agent search uses the installed and authenticated Antigravity CLI (`agy`).
Add this configuration alongside the required `fetch_llm` configuration:

```toml
[agent_providers.antigravity_main]
harness = "antigravity"
command = "agy"
max_concurrency = 1
timeout_seconds = 300
pass_env = []

[[search_agent.providers]]
provider = "antigravity_main"
# Omit model to use the Harness default.
# model = "MODEL"
```

```bash
agent-search-gateway agent-search "Find sources about agent evaluation"
agent-search-gateway agent-search "Find papers about agent evaluation" --scope paper
agent-search-gateway agent-search "Find sources and papers about agent evaluation" --scope all
```

The default scope is `web`. Each invocation starts a fresh Agent process.
`--provider` selects an exact configured alias; `--model` only filters explicitly
configured model entries. It does not override or discover the Harness default.
Search results are written to `agent-<request_id>.jsonl`; stdout contains only
the absolute result path. Agent and LLM search configuration are independent.


### Agent configuration and selection

The provider key `antigravity_main` is your configuration alias; `harness = "antigravity"`
selects the supported executable adapter. Agent aliases and quotas are independent of
LLM provider aliases, even when their names match.

| Setting | Type and default | Constraint |
|---|---|---|
| `harness` | Required string | Only `"antigravity"` is supported |
| `command` | String, `"agy"` | One executable name or path; relative paths are anchored to the daemon's startup cwd, bare names use PATH; no shell command, embedded flags, or expansion |
| `max_concurrency` | Integer, `1` | Positive; shared by every invocation of that alias |
| `timeout_seconds` | Number, `300` | Representable finite positive number; no Gateway-imposed upper bound |
| `pass_env` | Array of strings, `[]` | Additional environment-variable names, never values |
| Invocation `provider` | Required string | Exact key in `agent_providers` |
| Invocation `model` | Optional string | If present, nonempty; omission delegates to the Harness default |

The four `fetch_llm` stages (judge, safety, content_clean, focus_summary) must still
resolve through the existing explicit or inherited LLM configuration.
`search_llm` and `search_agent` are independently optional. Omit `search_agent`, or
replace its entries with `[search_agent]` and `providers = []`, to disable Agent
search. An Agent-only search setup still needs valid fetch LLM configuration.

Install, authenticate, and choose a saved default model in Antigravity before use.
The gateway does not install it, log in, discover models, or inherit a model from
`global_default_llm`. `doctor` validates Agent configuration locally without
checking executable availability, authentication, or model access.

To pin a model, obtain its exact slug from your installed version's `agy models`
and set `model` on the same invocation. For example, after configuring
`model = "MODEL"` (replace `MODEL` with an available slug), select it with:

```bash
agent-search-gateway agent-search "Find sources about agent evaluation" --provider antigravity_main --model MODEL
```

Gateway selectors are exact and case-sensitive after CLI whitespace trimming.
Provider-only selection retains both default and pinned entries for that alias;
model-only selection matches pinned entries across aliases; both flags select their
intersection. A default entry never matches an explicit model selector, even when
the Harness currently uses that model. No match fails before any Agent starts.
Every matching entry executes independently, including duplicates, in configured
result order. A later request without selectors uses the full configuration.

### Agent results and execution limits

Web rows keep exactly `url` and `abstract`. Paper rows use the existing paper schema,
with `agent:<alias>` in `sources` and source-specific citation counts. Academic
provider metadata retains precedence over generated Agent and LLM metadata.
`--scope all` emits Web rows first with `"type":"web"`, then paper rows with
`"type":"paper"`. It schedules separate fresh calls for both branches; a quota of
one makes them queue. Each call has its own temporary working directory and does
not resume a previous conversation.

A successful branch may return no admissible rows and still create an empty file.
Individual invocation or branch failures do not discard successful siblings.
If all selected invocations or both branches fail, no result file is created.
Only parsed and admitted landing URLs can be fetched; PDF metadata is not admitted.

| Budget | Limit |
|---|---|
| Harness print timeout | Configured `timeout_seconds`, explicitly passed to agy on every call |
| Python process deadline | Print timeout plus 5 seconds, starting after quota acquisition |
| Process termination | SIGTERM to the process group, then SIGKILL after up to 2 seconds |
| Whole Agent workflow | No fixed Gateway deadline; quota queues, separate scope calls, and paper OA enrichment add to total time |
| Agent socket response wait | No fixed default deadline; cancellation or disconnection ends the wait |
| Other socket response wait | 600 seconds |
| Captured output | stdout 4 MiB; stderr 256 KiB; overflow fails without accepting truncated output |

The default single-process timeout remains 300 seconds. A configured deadline still
ends an overlong call and triggers process cleanup; request cancellation and client
disconnection also clean up active Agent work. Quota waiting happens before that
process deadline begins. Two scope branches and any subsequent OA enrichment are
separate work, so total workflow duration can exceed a single process budget.
An unresponsive but connected daemon can leave the Agent client waiting until
interrupted or disconnected. Explicit numeric socket timeouts supplied by internal
callers remain honored.

The gateway accepts representable finite positive timeout values without a fixed
maximum, but the external agy duration parser may reject extreme values; the gateway
does not guarantee that agy supports every such duration.

These asynchronous deadlines cannot interrupt synchronous OS process creation if
the operating system stalls before Python receives the process handle. Process-group
cleanup also cannot guarantee termination of descendants that escape the group.

Agent calls are not retried by the gateway; the HTTP retry configuration does not
apply. Success requires exit code zero and one valid JSON terminal object with
`status = "SUCCESS"` and a nonempty response, followed by normal Markdown parsing.
A soft denial, successful status, or well-formed result does not prove that the
Agent used tools or accessed the cited sources.

The gateway requests JSON output and uses stdin from `DEVNULL`. It does not add
automatic approval flags or manage Harness tools, MCP servers, or permissions.
The serialized prompt is passed as the `-p` argument and may be visible to local
process inspection. OS limits apply to individual arguments and the combined
arguments/environment; an `E2BIG` rejection is reported with the safe diagnostic
`argument_limit`, not `spawn_failure`. The protocol's 1 MiB request-frame ceiling
does not guarantee that an external Harness can accept an equally large prompt.
A private temporary cwd and a reduced environment are not a sandbox: Harness
settings, auth caches, home files, and permitted tools remain under the Harness's
control.

The child environment keeps `PATH`, `HOME`, standard XDG config/cache/data/state/runtime
paths, temporary-directory variables, and locale variables, plus explicitly named
`pass_env` entries that exist in the daemon environment. It does not copy arbitrary
gateway credentials. Use `pass_env` only for variables your Harness needs; configuration
contains names, not secret values. Debug events record alias, configured model
(`null` when omitted), stage, timing, counts, and fixed failure classifications;
they do not record prompts, responses, raw stderr, or environment values.

## Academic paper search

Direct paper discovery queries enabled academic providers concurrently and merges records by DOI, arXiv id, provider-native id, then a conservative bibliographic fallback. The built-in discovery order is arXiv, Semantic Scholar, OpenAlex, dblp, Crossref, then CORE. Unpaywall is not a discovery provider; it runs after deduplication and only for DOI-bearing papers.

**dblp availability warning:** dblp has been observed to trigger bot verification even with infrequent API calls. Reducing request frequency does not guarantee availability. If this affects your searches, you can optionally disable dblp by editing `enabled` in the existing `[academic_providers.dblp]` table (do not append a duplicate table):

```toml
[academic_providers.dblp]
enabled = false
```

Academic providers use a separate configuration namespace and concurrency budget:

```toml
[academic_providers]
default_max_concurrency = 3

[academic_providers.arxiv]
enabled = true
api_url = "https://export.arxiv.org/api/query"

[academic_providers.semantic_scholar]
enabled = true
api_url = "https://api.semanticscholar.org/graph/v1"

[academic_providers.openalex]
enabled = true
api_url = "https://api.openalex.org"

[academic_providers.dblp]
enabled = true
api_url = "https://dblp.org/search/publ/api"

[academic_providers.crossref]
enabled = true
api_url = "https://api.crossref.org"

[academic_providers.core]
enabled = true
api_url = "https://api.core.ac.uk/v3"
api_key_env = "CORE_API_KEY"

[oa_resolvers.unpaywall]
enabled = false
api_url = "https://api.unpaywall.org/v2"
```

arXiv and dblp require no credentials. Semantic Scholar may set `api_key_env`; omitting it selects unauthenticated mode and there is no authentication fallback after a configured credential is rejected. CORE requires `api_key_env`, and the named environment variable must exist. OpenAlex and Crossref may set `contact_email_env`. Unpaywall is disabled by default; enabling it requires `contact_email_env`, and the named environment variable must exist. Configuration values reference environment-variable names rather than embedding API keys or contact values in TOML.

`paper-search` writes `paper-<request-id>.jsonl`. Each row uses the paper schema directly and has no `type` discriminator. Fields are `title`, `authors`, `abstract`, `identifiers`, `published_date`, `updated_date`, `url`, `pdf_url`, `venue`, `topics`, `citation_counts`, `is_open_access`, `oa_status`, `license`, and `sources`. `identifiers` contains `doi`, `arxiv_id`, `semantic_scholar_id`, `openalex_id`, `dblp_key`, and `core_id`.

`llm-search --scope paper` writes the same paper rows to `llm-<request-id>.jsonl`. `llm-search --scope all` uses the mixed sink: web rows are emitted first with `"type":"web"`, followed by paper rows with `"type":"paper"`. The `type` field is added only by the mixed writer. Plain `llm-search` remains the legacy web-only path and its JSONL bytes are unchanged.

Paper landing URLs are admitted to the shared URL store using the academic abstract, or the title when no abstract exists, so a later `url-fetch` may retrieve them. Paper PDF URLs are output metadata only and are not admitted automatically. Unpaywall enrichment never replaces a stronger direct PDF and resolver failures do not discard an already discovered paper.

Search commands create result files only after a successful branch has completed and all rows validate. Input/configuration errors, no-provider failures, and all-provider/all-branch failures do not create result files. A successful empty search may create an empty JSONL file.

Fetch a URL that has already been admitted by a successful search. The final positional focus is optional:

```bash
agent-search-gateway url-fetch "https://example.com/article" "pricing details"
```

`keyword-search`, `llm-search --scope web`, and `agent-search --scope web` print the absolute path of a newly created JSONL result file. Each line contains exactly two public fields:

```json
{"url":"https://example.com/article","abstract":"Short search abstract"}
```

Successful search files use `keyword-<request_id>.jsonl`, `llm-<request_id>.jsonl`, `paper-<request_id>.jsonl`, or `agent-<request_id>.jsonl`. In DEBUG mode that same eight-hex-character request ID appears on the daemon's internal events, making the result file a direct correlation key for the trace.

A URL must first be admitted by keyword, LLM, Agent, or academic paper search before `url-fetch` can use it. Admission, cached body content, and unavailable state are in memory only. A daemon restart therefore requires searching again before fetching the same URL.

Business command stdout is final-output-only: successful search commands print only the absolute result path, and successful fetch commands print only their final text. DEBUG events are emitted by the daemon process to its stderr and debug log, not into a requesting business CLI's stdout.

## DEBUG tracing

`agent-search-gateway start --debug` enables DEBUG logging only for the `agent_search_gateway` logger namespace. It does not turn on low-level `httpx` or `httpcore` DEBUG logging.

The daemon writes the same project events to stderr and to:

```text
~/.cache/agent-search-gateway-cli/logs/debug.log
```

The current log rotates at 5 MiB and retains 3 backups (`debug.log.1`, `.2`, and `.3`). The file is append-oriented across daemon restarts and contains explicit `session_started` / `session_stopped` boundaries when orderly lifecycle events are available.

DEBUG diagnostics cover Web search and Fetch providers, JSON/XML academic providers and OA resolvers, HTTP execution, LLM calls, and Agent processes. Success events retain useful metadata and counts; failures add allowlisted upstream code, status, type, and short messages where the provider's response format supports safe extraction. HTTP status remains separate from an upstream status inside a response, including errors carried by HTTP 200. Response byte counts describe the content actually read, rather than trusting Content-Length.

| Event layer | Meaning |
|---|---|
| HTTP attempts and responses | `http_attempt_started`, `http_attempt_completed`, and `provider_response_diagnostic` describe individual attempts and received responses; a transport failure without a response does not invent response metadata |
| HTTP retry and failure | `http_retrying` records another scheduled attempt; `http_failed` marks terminal HTTP failure |
| LLM protocol and stages | `llm_protocol_attempt_started`, `provider_retrying`, and `llm_protocol_completed` describe protocol work; `llm_stage_*` events describe judge, safety, cleaning, and summary stages |
| Provider terminal outcome | The owning search, Fetch, or OA pipeline emits one `provider_completed` or `provider_failed` for each invocation and scope; retries and different scopes are separate from that outcome |
| Workflow outcome | The daemon's `workflow_completed` or `workflow_failed` describes the whole command; one provider failure need not fail successful sibling work |

Candidate summaries keep adapter parsing (`phase=adapter`) separate from keyword admission preparation (`phase=staging`). Each phase reports received, accepted, rejected, grouped rejection reasons, and rewritten counts where relevant. Early termination distinguishes processed from unprocessed candidates rather than calling untouched candidates rejected. Unpaywall HTTP 404 is a completed OA lookup with no match, not a provider failure. Agent diagnostics include safe process classifications, exit status, and stdout/stderr byte counts after cleanup.

Events retain request ID, provider, stage, model, timing, scheduler/quota decisions, and safe URL components. Target URL path/query/fragment values are filtered before logging: paths may remain, while query and fragment are omitted. URI userinfo is stripped from logged URLs; HTTP transport endpoint fields follow the same rule. Upstream error text is redacted before sanitizing and truncating: code/status/type fields are capped at 128 characters and messages at 512. Message length and truncation metadata can explain omitted detail.

The implementation does not intentionally log full response payloads, query/prompt/page/model-response bodies, Agent stdout/stderr content, or authentication values. Central secret redaction is also applied to final rendered messages and debug tracebacks as defense in depth. DEBUG mode is diagnostic rather than a raw payload/TRACE mode. Treat debug files as sensitive local artifacts and review them before sharing; URL paths and operational metadata can still be private. Normal business-command stdout/stderr contracts are unchanged.

Transport retry (`http_retrying`) and terminal failure (`http_failed`) events include a `transport_type` field such as `ReadTimeout`, `ConnectTimeout`, or `ConnectError`. Only the concrete exception class name is recorded, not its message. HTTP status and JSON decode failures do not carry this field.

Bright Data SERP Search can report a provider failure through response headers even when the HTTP status is 200. The gateway checks those headers before decoding JSON and retries validated `captcha`, `timeout`, `expect_element`, and `expect_body` errors within the configured total attempt budget, using bounded jitter.

For these failures, DEBUG `http_retrying` and `http_failed` events include safe outer/inner statuses, error classifications, the actual retry wait, and whether another retry was scheduled or the budget was exhausted. Unknown errors and authentication failures are terminal. Normal search command output continues to use the documented result-path and aggregate-error contracts.

Expected provider/retry/semantic failures are logged as concise events without tracebacks. Unexpected daemon workflow failures include a traceback only in DEBUG mode. Logging sink failures after successful startup do not change business workflow results.

## Public interface and compatibility

The CLI commands, documented configuration, stdout contracts, result-file schemas, and runtime paths are the supported interfaces for the current `0.x` line. Python module imports and the local socket wire protocol remain internal implementation details. See [the public interface and compatibility policy](docs/public-interface.md) before building automation against the project.

A static documentation site and generated internal Python reference are built in CI and published from the default branch at the [project documentation address](https://dingxingdi.github.io/agent-search-gateway/).

## Development and verification

Development and CI remain lockfile-driven. `uv sync --locked` is intentionally separate from the end-user `uv tool install .` flow because lint, type-check, documentation, and test dependencies belong to the repository development environment.

Run the default no-network development checks with:

```bash
uv sync --locked --all-groups
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run pytest -v
uv run python scripts/build_docs.py
```

The default test suite uses fakes, local Unix sockets, and mock transports and does not require provider credentials.

## Opt-in live integration checks

Live connectivity tests are disabled by default. To opt in, set:

```text
WEB_SEARCH_RUN_INTEGRATION=1
TAVILY_API_KEY=...
OPENAI_API_KEY=...
PARALLEL_API_KEY=...
```

`OPENAI_MODEL` may optionally select the chat-completions model used by the OpenAI-compatible connectivity check. Parallel Search and Extract use `PARALLEL_API_KEY` when their opt-in live checks are enabled. These integration checks validate connectivity and basic response shape only; normal CI does not enable them.

## Contributing and support

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and pull-request expectations, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards. Support is community-provided on a best-effort basis with no guaranteed response time; see [SUPPORT.md](SUPPORT.md).

## Security

Do not report vulnerabilities in public issues or pull requests. Follow [SECURITY.md](SECURITY.md) and use the repository's private vulnerability-reporting flow. Configuration should contain environment-variable names rather than credential values, and debug logs must be reviewed as sensitive local artifacts before sharing.

Security tooling for this project is provided by [Snyk](https://snyk.io/).

## Releases

The project follows Semantic Versioning for tagged releases and records user-visible changes in [CHANGELOG.md](CHANGELOG.md). GitHub Release `v0.1.0` is the initial public release; tagged releases are built, attested, and published by CI. PyPI publishing is not configured yet. Maintainers should follow [RELEASING.md](RELEASING.md).

## License

This project is available under the [MIT License](LICENSE).
