# agent-search-gateway

`agent-search-gateway` is a local foreground daemon plus a thin CLI for aggregated keyword search, LLM-assisted search, and fetching URLs that were admitted by search.

Version 0.1 keeps URL admission and body state in daemon memory. Restarting the daemon clears that state. The daemon communicates over a local Unix-domain socket, so this version targets Unix-like environments. It does not provide persistent URL state, remote daemon access, automatic repairs, or live provider checks in `doctor`.

## End-user installation

Install the CLI from the current source checkout with `uv tool`. This creates an isolated runtime for the command and does not use the repository `uv.lock` as the installed tool environment.

```bash
uv tool install .
```

Create `~/.config/agent-search-gateway-cli/config.toml` from `config.example.toml`, adjust the enabled providers, and export the environment variables named by each provider's `api_key_env`. The configuration stores environment-variable names only; credential values remain in the daemon process environment.

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

Parallel accepts optional Search mode `turbo`, `fast`, `basic`, or `advanced`, plus independent Search and Extract fetch policies. Extract always requests full content internally, while Search result count is left to Parallel's provider default. When enabling Parallel, set `api_key_env` to the name of the environment variable that holds the credential.

Web search and fetch stages for the same provider share one concurrency quota. LLM provider transports have their own independent quotas.

## Commands

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

## Academic paper search

Direct paper discovery queries enabled academic providers concurrently and merges records by DOI, arXiv id, provider-native id, then a conservative bibliographic fallback. The built-in discovery order is arXiv, Semantic Scholar, OpenAlex, dblp, Crossref, then CORE. Unpaywall is not a discovery provider; it runs after deduplication and only for DOI-bearing papers.

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

`keyword-search` and `llm-search --scope web` print the absolute path of a newly created JSONL result file. Each line contains exactly two public fields:

```json
{"url":"https://example.com/article","abstract":"Short search abstract"}
```

A successful search result filename has the form `keyword-<request_id>.jsonl` or `llm-<request_id>.jsonl`. In DEBUG mode that same eight-hex-character request ID appears on the daemon's internal events, making the result file a direct correlation key for the trace.

A URL must first be admitted by keyword or LLM search before `url-fetch` can use it. Admission, cached body content, and unavailable state are in memory only. A daemon restart therefore requires searching again before fetching the same URL.

Business command stdout is final-output-only: successful search commands print only the absolute result path, and successful fetch commands print only their final text. DEBUG events are emitted by the daemon process to its stderr and debug log, not into a requesting business CLI's stdout.

## DEBUG tracing

`agent-search-gateway start --debug` enables DEBUG logging only for the `agent_search_gateway` logger namespace. It does not turn on low-level `httpx` or `httpcore` DEBUG logging.

The daemon writes the same project events to stderr and to:

```text
~/.cache/agent-search-gateway-cli/logs/debug.log
```

The current log rotates at 5 MiB and retains 3 backups (`debug.log.1`, `.2`, and `.3`). The file is append-oriented across daemon restarts and contains explicit `session_started` / `session_stopped` boundaries when orderly lifecycle events are available.

DEBUG events expose operational metadata such as request ID, provider, semantic stage, model, retry attempt, HTTP status, timing, result counts, scheduler/quota decisions, candidate URLs, and fixed acceptance/rejection reason codes. Target URL path/query/fragment values may be persisted, but URI userinfo is stripped first. HTTP transport endpoint fields additionally omit userinfo, query, and fragment so request-specific search/query content is not recorded as endpoint metadata. Treat the debug files as sensitive local artifacts because target URLs can themselves contain private or signed query values.

The implementation does not intentionally log query/prompt/page/model-response bodies or authentication values. Central secret redaction is also applied to final rendered messages and debug tracebacks as defense in depth. DEBUG mode is diagnostic rather than a raw payload/TRACE mode.

Expected provider/retry/semantic failures are logged as concise events without tracebacks. Unexpected daemon workflow failures include a traceback only in DEBUG mode. Logging sink failures after successful startup do not change business workflow results.

## Development and verification

Development and CI remain lockfile-driven. `uv sync --locked` is intentionally separate from the end-user `uv tool install .` flow because lint, type-check, and test dependencies belong to the repository development environment.

Run the default no-network development checks with:

```bash
uv sync --locked
uv run ruff check .
uv run mypy src tests
uv run pytest -v
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
