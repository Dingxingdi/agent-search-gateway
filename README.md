# agent-search-gateway

[![CI](https://github.com/Dingxingdi/agent-search-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Dingxingdi/agent-search-gateway/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

`agent-search-gateway` is a local foreground daemon plus a thin CLI for aggregated keyword search, LLM and Agent search, academic discovery, and fetching URLs admitted by search.

The gateway keeps URL admission and body state in daemon memory. Restarting the daemon clears that state. The daemon communicates over a local Unix-domain socket, so this version targets Unix-like environments. Persistent URL state, remote daemon access, and automatic repairs are unsupported. `doctor` checks the local installation and, when enabled, the configured open-webSearch daemon.

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

`doctor` validates the configuration and required environment variables, checks directory writability, and inspects the gateway Unix socket. It makes no network requests when `open_websearch` is absent or both Search and Fetch are disabled. With valid configuration and either capability enabled, it sends one credential-free `GET /health` to the configured open-webSearch daemon. A failed probe gives exit code 1. A missing gateway socket is informational (`[info] daemon not running`). A report with only `[ok]` and `[info]` checks gives exit code 0.

A healthy response confirms daemon liveness only. Search engine and browser readiness, cloud API key validity, permissions, quotas and service availability, and Agent executable availability and login remain unverified. Some Web provider option values, including their types, are validated when the runtime is built.

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

### Provider rate limits and input sizes

For each Web provider, academic provider or OA resolver, set `rate_limits` to an array of `{ requests = N, period_seconds = T }` windows. All configured windows and any `min_interval_seconds` between starts must permit the attempt. Search, Fetch and their retries share the same provider's allowance within the daemon. `max_concurrency` independently limits in-flight work. Omitted windows and spacing impose no local frequency policy.

Local rate waiting is bounded by `[retry].request_timeout_seconds` per attempt. Fetch can use another provider with both rate and concurrency available. If a candidate exhausts its rate-wait budget, Fetch can fall back to the remaining candidates. Waiting solely for concurrency, running another candidate, and server-directed `Retry-After` sleeps are outside that local budget.

The [provider rate reference](docs/provider-rate-limits.md) gives account and endpoint conditions, sources and optional configurations for all 32 registered Web, academic and OA services. Some limits are unpublished. The count describes source coverage and does not certify live success. Edit your existing provider tables to adopt the settings that fit your account.

Under `[input_limits]`, `llm_request_body_bytes` defaults to **131072** for the complete serialized LLM JSON body, and `agent_prompt_bytes` defaults to **98304** for the single serialized Agent prompt argument. Both measure UTF-8 bytes. Token counts depend on the model. For oversized input the gateway keeps approximately 80% of the retained variable text from its head and 20% from its tail, with an omission marker. It preserves the system instructions, template and protected user focus. If the protected content and required framing cannot fit, preparation fails with `input_budget_unfit` before any send or spawn.

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
| Doubao | yes | yes |
| You.com | yes | yes |
| Keenable | yes | yes |
| open-webSearch | yes | yes |
| Yuanbao | yes | no |
| Zhihu | yes | no |
| Baidu Qianfan | yes | no |

Parallel accepts optional Search mode `turbo`, `fast`, `basic`, or `advanced`, plus independent Search and Extract fetch policies. Extract always requests full content internally, while Search result count is left to Parallel's provider default. When enabling Parallel, set `api_key_env` to the name of the environment variable that holds the credential.

Jina Reader is a credential-free, fetch-only provider, so its configuration must omit `api_key_env`. Each actual Jina Reader request sends `X-No-Cache: true` to refresh Reader-side content. This does not bypass content already prepared by the gateway: an admitted URL whose body is already prepared still short-circuits before provider dispatch, and the gateway exposes no force-refresh option. Configuration order remains fetch-provider priority, so place Jina earlier when free-first behavior is desired.

Web search and fetch stages for the same provider share one concurrency quota. LLM provider transports have their own independent quotas.

#### Doubao, You.com, Keenable, and open-webSearch

These four examples are disabled by default. Set `enable_search = true` and/or
`enable_fetch = true` in the corresponding `[web_providers.NAME]` table to enable
each capability independently. The REST configurations below use an API root;
their adapters add the Search or Fetch path. You.com's free MCP alternative
uses the complete endpoint shown below, without appending a REST path. Existing
timeout and retry settings in `[retry]` and provider `max_concurrency` continue
to apply.

| Provider name | API root | Credential or service prerequisite |
|---|---|---|
| `doubao` | `https://open.feedcoopapi.com/search_api` | `api_key_env = "DOUBAO_API_KEY"`; Fetch requires a pay-as-you-go Key with Fetch permission. |
| `youcom` | `https://ydc-index.io` | `api_key_env = "YDC_API_KEY"`; the official Contents endpoint is a separate API call with separate billing and the appropriate Contents scope. |
| `keenable` | `https://api.keenable.ai` | `api_key_env = "KEENABLE_API_KEY"`; only the keyed API is used. Omitting `live` does not guarantee the upstream avoids real-time charges. |
| `open_websearch` | `http://127.0.0.1:3210` | Omit `api_key_env`; run your own open-webSearch HTTP daemon before enabling it. |

For You.com, choose either the existing keyed REST configuration or free MCP.
Replace the entire existing `[web_providers.youcom]` table with this free
alternative; do not append a duplicate table:

```toml
[web_providers.youcom]
enable_search = true
enable_fetch = true
api_url = "https://api.you.com/mcp/free"
```

Delete its `api_key_env` line. An existing `YDC_API_KEY` environment variable may
remain; it is not automatically used without `api_key_env`. Explicit credentials
on the free endpoint are rejected before HTTP. REST still uses its configured root
and key; Fetch calls the separate `/v1/contents` API with its existing billing and
permission requirements. The free endpoint uses the same `youcom` provider and
independent capability switches.

As of 2026-11-21, [You.com's MCP documentation](https://you.com/docs/agents/mcp-server)
publishes 100 free queries per day; this external quota may change. Local
`rate_limits` are not a remaining-balance meter or a daily quota ledger shared
across processes. The existing REST 10 QPS example is not an official MCP limit.
Free MCP failures follow existing Search fan-out and Fetch fallback through other
configured providers, without automatically switching to paid You.com REST.
See the [free MCP guide](docs/youcom-free-mcp.md) and
[replacement fragment](config.youcom-free.fragment.toml) for configuration and checks.
No new provider name, mode, SDK, or MCP service deployment is required.

Only `open_websearch` accepts the provider-specific `engines` option. The full
user-editable list in `config.example.toml` is an example, not a production default
or a supported-engine enumeration. For a smaller selection, edit the existing block:

```toml
[web_providers.open_websearch]
enable_search = false
enable_fetch = false
api_url = "http://127.0.0.1:3210"
engines = ["bing", "duckduckgo"]
```

The gateway passes the configured list to one daemon Search request, with a fixed
`limit=50` total result budget shared across the selected engines. This is not 50
results per engine and does not guarantee a hit from every engine. There are no
`limit` or `searchMode` configuration keys. Omit `engines` to send only the query
and use the daemon's single default engine, without sending a limit.

The daemon's `ALLOWED_SEARCH_ENGINES` must allow the selected names. Filtering can
remove requested engines; if all are filtered, the daemon may fall back to its
default. `DEFAULT_SEARCH_ENGINE` selects one default engine, not a list. Actual
availability depends on daemon version, network access, and its allowlist.
Duplicate entries consume the shared budget; the example has no duplicates.
`linuxdo` is currently unavailable and is omitted from the example. The gateway
does not enumerate, discover, or schedule child engines, or manage daemon lifecycle.

A valid daemon response can contain `partialFailures`; diagnostics retain only a
safe marker and count. A provider-level PASS does not prove every engine succeeded.
The response's `data.engines` is the parsed/filtered request list, not a success
list. Matching that list with no partial failures still does not prove that every
engine succeeded; `results[*].engine` confirms only the engines with observed hits.

Use `keyword-search --provider NAME` to isolate an enabled Search provider.
`url-fetch` has no `--provider` flag: isolate Fetch using a temporary configuration
or the opt-in adapter checks below. Normal CLI Fetch still requires a URL admitted
by search in the same gateway runtime.

#### Baidu Qianfan

The single `baiduqianfan` provider calls both Web Search and Agentic Search for
every logical search. To enable Search, add this block to your configuration if
it is absent, or edit its existing block:

```toml
[web_providers.baiduqianfan]
enable_search = true
enable_fetch = false
api_url = "https://qianfan.baidubce.com"
api_key_env = "BAIDUQIANFAN_API_KEY"
rate_limits = [{ requests = 3, period_seconds = 1 }]
```

Export `BAIDUQIANFAN_API_KEY` in the daemon environment and restart the daemon.
One logical search normally sends two physical HTTP requests, one per API.
Retries add physical attempts, and both APIs share the configured 3/1s local
window. Other clients can consume the same upstream allowance, so the local
limit cannot guarantee free billing quotas or prevent charges.

Web Search supplies summaries only. Agentic Search web-page `raw_content` passes through the existing `cheap_check` and `judge` checks during Search. Retained raw content later passes through `content_clean` and `safety` during Fetch. Media results retain an HTTP(S) link, title, and snippet; the gateway does not
download or transcribe media.

#### Enabling Zhihu and Yuanbao

Existing user configurations are not automatically updated when the package or template changes. To enable the new search providers, add the following blocks to `~/.config/agent-search-gateway-cli/config.toml` if they are absent, or edit their existing blocks:

```toml
[web_providers.yuanbao]
enable_search = true
enable_fetch = false
api_url = "https://api.wsa.cloud.tencent.com"
api_key_env = "YUANBAO_API_KEY"

[web_providers.zhihu]
enable_search = true
enable_fetch = false
api_url = "https://developer.zhihu.com"
api_key_env = "ZHIHU_ACCESS_SECRET"
```

`api_key_env` names the environment variable holding each credential in the daemon environment. `YUANBAO_API_KEY` and `ZHIHU_ACCESS_SECRET` are example names selected by this configuration; the adapters do not hardcode them. Export the named variables and restart the daemon after updating the configuration. Restarting clears in-memory URL admission and cached content, so search again before fetching URLs.

Zhihu is one logical provider that concurrently calls the site-search and global-search APIs at `/api/v1/content/zhihu_search` and `/api/v1/content/global_search`. One logical search may consume two API quotas; retries can add physical requests. An ordinary failure in either branch preserves the successful sibling's results, including a successful empty result. Both branches failing makes the logical provider fail.

Yuanbao sends a direct `POST /SearchPro` request authenticated with a Tencent service API key; it does not use a Tencent SDK or request signing. Its `content` and `passage` fields supply search summaries, not fetched page bodies. URLs admitted from these results remain eligible for a later `url-fetch` through an enabled Fetch provider. Keep both `api_url` values as base URLs; the adapters append the API paths.

After enabling the providers, select either one for an individual request:

```bash
agent-search-gateway keyword-search "query" --provider zhihu
agent-search-gateway keyword-search "query" --provider yuanbao
```

#### TinyFish endpoint migration

TinyFish uses separate public Search and Fetch services. In an existing `~/.config/agent-search-gateway-cli/config.toml`, replace the legacy `https://api.tinyfish.ai/search` and `https://api.tinyfish.ai/fetch` values with the following endpoints:

```toml
[web_providers.tinyfish]
search_api_url = "https://api.search.tinyfish.ai"
fetch_api_url = "https://api.fetch.tinyfish.ai"
```

Edit these two keys in the existing provider block; keep its credential reference and enable flags unchanged. Upgrading the package or changing `config.example.toml` does not update an existing user configuration. Restart the daemon after updating its configuration; restarting clears in-memory URL admission and cached content, so search again before using `url-fetch`. Deliberately configured custom endpoints remain supported and are not automatically rewritten.

#### Known provider limitations

Bright Data SERP Search has also occasionally returned HTTP 200 responses followed by JSON parsing failures. The cause remains unconfirmed. If this occurs, retry the search or choose another provider. This release documents the observation without fixing it or adding JSON-parse retries. This is a separate known limitation from the response-header checks described under DEBUG tracing.

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
process inspection. The configured byte cap applies first. If the fitted prompt exceeds a known Linux single-argument limit or contains a NUL byte, preflight fails with `input_budget_unfit`. OS limits also apply to individual arguments and the combined
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
contains names, not secret values. DEBUG events record alias, configured model
(`null` when omitted), stage, timing, counts and failure classifications. A spawn
attempt can also record bounded previews of the prepared prompt and captured stdout.
See [DEBUG tracing](#debug-tracing) for the content and redaction rules.

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
api_key_env = "OPENALEX_API_KEY"
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

**OpenAlex migration.** Enabled OpenAlex now requires an API key. In your existing table add the environment-variable name shown above, then supply that variable from your credential store to the daemon and restart it. Alternatively, set `enabled = false` to skip OpenAlex. An optional contact email cannot satisfy the key requirement. The gateway sends the key as a Bearer credential and never falls back to anonymous access. For an offline configuration check, a dummy value such as `OPENALEX_API_KEY=example-for-doctor-only` can satisfy the presence check. Live calls require a valid key. This is a gateway requirement even though OpenAlex still permits some basic anonymous queries.

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

## HTTP retry waits

For retryable HTTP statuses, a valid `Retry-After` header takes precedence over the local backoff. The gateway accepts a nonnegative integer number of seconds or an HTTP-date; zero is honored, and a past date means no wait. The server-directed wait can exceed `[retry].max_delay_seconds`, which caps only the locally calculated exponential backoff. A missing or invalid header falls back to that local backoff.

`max_attempts` remains the total attempt budget, including the first request; honoring `Retry-After` does not add attempts or guarantee that every HTTP 429 will recover. The existing retry configuration needs no additional keys.

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

Ordinary mode emits no body previews. In DEBUG mode each actual HTTP send (including each retry) and each Agent spawn attempt produces one `upstream_body_preview` with request and response sides. The response side shows the HTTP body or captured Agent stdout. A missing response has a null preview. A preparation failure or rate wait that prevents the call produces no preview event.

For each side, both the redacted preview text and its final JSON-escaped field value fit within 8192 UTF-8 bytes, including any omission marker. The complete event contains both fields and metadata. `request_input_truncated` means the outbound input was shortened to fit its configured cap. `request_preview_truncated` and `response_preview_truncated` describe incomplete log views. The event excludes headers, stderr content, environment values, full argument lists, authentication values and the discarded input middle. `time` is UTC wall-clock time with milliseconds and a trailing `Z`. `elapsed_ms` measures duration.

Treat DEBUG logs as sensitive local artifacts. A preview can contain private text even after redaction. Review it before sharing. The CLI keeps its final-output-only stdout and existing result and error schemas.

Transport retry (`http_retrying`) and terminal failure (`http_failed`) events include a `transport_type` field such as `ReadTimeout`, `ConnectTimeout`, or `ConnectError`. Only the concrete exception class name is recorded, not its message. HTTP status and JSON decode failures do not carry this field.

Bright Data SERP Search can report a provider failure through response headers even when the HTTP status is 200. The gateway checks those headers before decoding JSON and retries validated `captcha`, `timeout`, `expect_element`, and `expect_body` errors within the configured total attempt budget, using bounded jitter.

For these failures, DEBUG `http_retrying` and `http_failed` events include safe outer/inner statuses, error classifications, the actual retry wait, and whether another retry was scheduled or the budget was exhausted. Unknown errors and authentication failures are terminal. Normal search command output continues to use the documented result-path and aggregate-error contracts.

Expected provider/retry/semantic failures are logged as concise events without tracebacks. Unexpected daemon workflow failures include a traceback only in DEBUG mode. Logging sink failures after successful startup do not change business workflow results.

## Public interface and compatibility

The CLI commands, documented configuration, stdout contracts, result-file schemas, and runtime paths are the supported interfaces for the current `1.x` line. Python module imports and the local socket wire protocol remain internal implementation details. See [the public interface and compatibility policy](docs/public-interface.md) before building automation against the project.

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

The original REST adapter checks for Doubao, You.com, and Keenable use `DOUBAO_API_KEY`,
`YDC_API_KEY`, and `KEENABLE_API_KEY`, respectively. The open-webSearch check
requires an explicit `OPEN_WEBSEARCH_API_URL` pointing to your already-running HTTP
daemon; this variable selects only the test endpoint and is not a production
configuration option. Search uses only `["bing", "duckduckgo"]` with total
`limit=50`, rather than the full example list. Ensure both engines are allowed.

Run one original provider at a time, replacing `doubao` with `keenable` or
`open_websearch` as appropriate. For only keyed You.com REST, use the selector
`-k "youcom and not youcom_mcp"` instead:

```bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k doubao -v -rs -rP
```

Each provider has one Search and one Fetch case. Fetch reads the fixed public
`https://www.python.org/about/` page directly through the adapter; no gateway
service is started. Requests share a bounded three-attempt retry policy with a
30-second request timeout. Missing credentials or the explicit daemon address
produce SKIP; configured connection, permission, quota, or schema failures produce
FAIL. Ordinary CI leaves these eight original cases skipped.

Captured output contains safe JSON metrics for provider/stage, actual HTTP
attempts, last observed HTTP status, fixed failure category/known code, hit count
or content length, elapsed time, and response-contract outcome. `-rP` displays
metrics for successful cases; failed cases retain their safe captured output.
open-webSearch also reports whether the effective request list matched, the
partial-failure count, and only requested engine names with observed hits. A
filtered/defaulted list fails multi-engine coverage. A matching list with no
partial failures still does not prove both engines succeeded; engines without
observed hits remain unverified. The tests do not print raw requests, responses,
queries, URL parameters, page text, or credentials.

Run the Baidu Qianfan check with `BAIDUQIANFAN_API_KEY` set:

```bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_baiduqianfan.py -v -rs -rP
```

This command calls only Baidu Qianfan. One logical search normally sends two HTTP
requests, one to Web Search and one to Agentic Search. Retries consume additional
requests. The two APIs may share a free allowance, and calls may incur charges.
Both branches must successfully parse their results. A verified empty result is
valid for either branch.

For the keyless You.com free MCP check, select its dedicated case:

```bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k youcom_mcp -v -rs -rP
```

This case does not require or use `YDC_API_KEY`. It sends at most one Search
request and, after usable hits, one Extract request for the first hit, with a
30-second timeout per request and no retries. A Search failure reports Extract
as SKIP. Stage output reports safe PASS/FAIL/SKIP categories, request counts,
elapsed time, and hit/content counts. Default offline runs skip this case unless
`WEB_SEARCH_RUN_INTEGRATION=1` is set. Use `-k youcom_mcp`; `-k youcom` would
also select the original keyed REST checks. See the
[free MCP guide](docs/youcom-free-mcp.md) for details.

## Contributing and support

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and pull-request expectations, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards. Support is community-provided on a best-effort basis with no guaranteed response time; see [SUPPORT.md](SUPPORT.md).

## Security

Do not report vulnerabilities in public issues or pull requests. Follow [SECURITY.md](SECURITY.md) and use the repository's private vulnerability-reporting flow. Configuration should contain environment-variable names rather than credential values, and debug logs must be reviewed as sensitive local artifacts before sharing.

Security tooling for this project is provided by [Snyk](https://snyk.io/).

## Releases

The project follows Semantic Versioning for tagged releases and records user-visible changes in [CHANGELOG.md](CHANGELOG.md). GitHub Release `v0.1.0` is the initial public release; tagged releases are built, attested, and published by CI. PyPI publishing is not configured yet. Maintainers should follow [RELEASING.md](RELEASING.md).

## License

This project is available under the [MIT License](LICENSE).
