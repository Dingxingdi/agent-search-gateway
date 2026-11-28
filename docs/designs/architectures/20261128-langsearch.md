# Architecture: LangSearch Web Search Provider

- Date: 2026-11-28
- Status: Implemented; see the [implementation and verification record](../plans/20261128-langsearch.md).
- Base commit: 5f70c12 (v3.1.0)
- Branch: design-langsearch-20261128
- Companion specifications: ../error-handlings/20261128-langsearch.md and ../testings/20261128-langsearch.md
- Constraint: minimum intrusion, low coupling, reuse existing provider infrastructure.

## 1. Scope & Assumptions

### In Scope
- Register one optional built-in Web provider, langsearch, with Search support and no Fetch support. It is the 26th Web provider.
- Integrate the official REST Web Search endpoint through a small independent adapter. Use the existing credential-array configuration, HTTP executor, provider registry, runtime, orchestration and logging.
- Request URL-bearing metadata only, with a fixed result count of 10. Preserve the query received from the orchestrator; do not request full text.
- Update example configuration, documented capabilities, rate-limit reference, provider counts and affected tests.

### Todo / Out of Scope
- No LangSearch URL Fetch, MCP client, SDK, reranker, additional search parameters or separate provider for each credential.
- No local TPM/TPD tracking, account-wide gates, retry/cooldown policy or billing management.
- No public protocol, CLI, MCP server, daemon, config schema, credential pool, URL store, orchestration or shared helper refactoring. No other adapter or dependency changes.

### Assumptions / Sources
- Official REST endpoint: POST https://api.langsearch.com/v1/web-search, HTTP Bearer authentication. Count defaults to 10 and is capped at 50. Omit contents for snippets and freshness for no date restriction.
- Success uses integer code 200 and ordered results in data.webPages.value. Result items may contain name, url, snippet and other metadata. An empty result array is valid.
- The Free Plan includes all Web Search features subject to account limits. New accounts start at 5 RPS; existing account rates may differ. TPM is a rolling minute limit; TPD resets at 00:00 UTC. API keys and MCP share account allowances. The example is a per-member request window, not an account-wide guarantee.
- Existing v3.1.0 behavior: omitted member limits are unrestricted; successful partial/empty responses do not trigger credential fallback.
- Official references checked 2026-11-28:
  - https://langsearch.com/overview
  - https://langsearch.com/pricing
  - https://docs.langsearch.com/reference/search-api-guide
  - https://docs.langsearch.com/api/web-search-api
  - https://docs.langsearch.com/api/errors
- Existing references under src/agent_search_gateway: providers/web/anysearch.py, providers/web/brave.py, providers/web/common.py, providers/defaults.py, providers/registry.py, runtime.py, orchestrators/search.py.

## 2. Architecture Summary

CLI and MCP remain thin clients of the existing daemon. The search orchestrator selects providers, dispatches search through their credential pools, stages returned KeywordSearchHit values and admits eligible URLs into the URL store. LangSearchAdapter implements only the keyword search contract, using an injected JsonRequester to send one REST search per physical attempt and parse the vendor envelope. Configuration and construction are registry-driven. Existing components handle HTTP retry, proxy, rate, concurrency, credential fallback and diagnostics. LangSearch does not participate in Fetch.

## 3. Design Decisions

### Provider Integration

#### D1 — Independent Search-only adapter
- Description: Add providers/web/langsearch.py with LangSearchAdapter, matching AnySearchAdapter constructor parameters name, api_url, secret and http_executor. Append a registration in providers/defaults.py named langsearch, with ProviderCapabilities(search=True, fetch=False), allowed_config_keys containing only api_url, and default required credential behavior. Existing registration order remains unchanged.
- Rationale: Registry-driven construction already supports this capability shape.
- Trade-off: A dedicated vendor parser adds a small module, but other providers remain untouched.
- Rejected alternatives: Vendor conditionals inside another adapter, a new envelope abstraction, and a second MCP transport for the same quota.

#### D2 — Fixed metadata-only REST request
- Description: Use endpoint(api_url, "/v1/web-search"), POST, stage search, Bearer authentication using the injected SecretValue, JSON body containing query and count (10). Omit contents, freshness and domain filters. No additional request options.
- Rationale: Consistent with existing keyword search and avoids consuming extra output tokens on full-page text.
- Trade-off: No exposed count/date/domain tuning; introduce such options separately only if needed.
- Rejected alternatives: Request full text, expose arbitrary vendor options, or emulate URL Fetch through search.

#### D3 — Vendor envelope and mapping stay local
- Description: Require integer code 200, object data and webPages, and list value. Map url to url, name to title, snippet to snippet; leave raw_content and content empty. Reuse common validation functions, CandidateCounts, candidate_rejection_reason and the existing candidate summary event. Do not modify keyword_hits_from_results merely to support a different title key. Preserve result ordering at the adapter boundary; leave URL normalization, deduplication and staging to the orchestrator.
- Rationale: The rest of the gateway sees only standard KeywordSearchHit values.
- Trade-off: Publication metadata, usage and trace identifier are not exposed; existing staging decides whether metadata is sufficient for admission.
- Rejected alternatives: Extend the public result model, synthesize metadata, store upstream text as a body, duplicate staging logic.

### Runtime Model / Interface

#### D4 — Existing registry-driven construction and clients
- Description: Use generic config parsing, runtime factory, per-member executor and logical search dispatch. When enabled, LangSearch participates in default fan-out or explicit provider selection. CLI and both MCP transports retain existing daemon routes and result formats.
- Rationale: No new entry point or lifetime owner is needed.
- Trade-off: Existing gateway lifecycle and error conventions apply, not native vendor response passthrough.
- Rejected alternatives: Add LangSearch-specific branches to runtime, daemon, CLI, MCP or doctor.

### State Management / Storage

#### D5 — Explicit Fetch remains separate
- Description: Search admits eligible URLs through the existing orchestrator; both body fields remain empty even if upstream sends unexpected text. Explicit Fetch uses another existing Fetch-capable provider. LangSearch owns no URL state or persistent storage.
- Rationale: Prevent automatic body cleaning or bypassing explicit Fetch.
- Trade-off: Full content requires a separate provider.
- Rejected alternatives: Cache search text as body or add a LangSearch fetch implementation.

### Concurrency / Scheduling

#### D6 — Existing member controls
- Description: Use existing credentials array, round_robin default or fill_first. Each physical attempt including retries consumes the selected member's configured permit. On terminal execution failure the pool may try another unattempted member. Successful empty/partial results do not trigger fallback. Example sets 5 requests per second per member; no additional concurrency cap or hidden rate limit.
- Rationale: Current pool, retry and gate contracts suffice.
- Trade-off: Independent member limits do not enforce account-wide token/request limits; keys on the same account share upstream allowance. Operators adjust member limits to account conditions.
- Rejected alternatives: Account quota tracking, global caps, per-key rotation in the adapter, or new cooldown behavior.

### Security / Observability

#### D7 — Existing credential and diagnostic boundaries
- Description: Resolve the required environment variable through existing secret handling. Send credentials only through the existing HTTP authentication boundary. Reuse proxy/TLS handling, redaction and body preview limits. Add a local best-effort diagnostic extractor and candidate summary; see companion error handling spec.
- Rationale: Preserve existing security and observability contracts.
- Trade-off: No separate native usage or log_id fields are retained.
- Rejected alternatives: Store credentials in source/config, log unbounded payloads, or add new public diagnostics and online doctor probes.

### Future Migration

#### D8 — Additive opt-in
- Description: Add a disabled-by-default provider example. Existing configs remain valid and are not rewritten. Users add the provider and restart the daemon to enable it.
- Rationale: Upgrades should not cause extra network calls.
- Trade-off: Explicit enablement and credential configuration are required.
- Rejected alternatives: Enable by default or introduce config migration.

## 4. Component Catalog

| Component | Purpose / responsibility | Interface | Dependencies | Owns state? | Role |
|---|---|---|---|---|---|
| CLI / MCP | Submit existing requests | Existing search/fetch contract | Daemon socket | No new state | Entry / delivery |
| Search orchestrator | Fan out, stage and admit results | Existing keyword search | Runtime providers, URL store | Existing request state | Coordinator |
| Registry | Declare capability and factory | WebProviderRegistration | LangSearchAdapter | Registration metadata | Factory / validator |
| Config / runtime | Validate and construct members | Existing Web config | Registry, environment, executor | Existing resources | Assembly |
| Credential pool | Select member, fallback, concurrency | Existing search wrapper | Member adapters, gates | Existing member state | Coordinator |
| LangSearchAdapter | Build vendor request, validate and map results | async search(query: str) -> list[KeywordSearchHit] | JsonRequester, SecretValue, common helpers | No mutable operation state | Transformer |
| HTTP executor | Transport, retry, admission, logging | request_json | HTTPX, retry policy, gate | Existing resources | Transport |
| URL store / Fetch scheduler | Admit URLs, fetch body when requested | Existing URL admission and Fetch contracts | Existing Fetch providers | Existing daemon memory | Sink / coordinator |

## 5. Data Flow

### 5.1 Configuration / startup / doctor
```text
Config loader reads Web provider tables
Registry resolves langsearch and validates Search-only capabilities
if absent or disabled:
    do not expose an enabled LangSearch provider
else:
    validate credentials, options and capability
    if invalid: return existing configuration failure
    doctor checks existing configuration rules; no LangSearch network call
    runtime constructs the existing pool
    for each member:
        construct member gate, HTTP executor and LangSearchAdapter
    expose one logical Search provider named langsearch
```

### 5.2 Keyword search (CLI / MCP)
```text
Daemon receives existing request
Search orchestrator validates and selects providers
if langsearch is enabled and selected:
    existing pool selects available credential member
    try:
        adapter sends POST through HTTP executor
        executor retries eligible failures within existing policy
        if HTTP/JSON execution fails terminally: raise existing failure
        adapter validates business code and envelope
        if invalid: raise ExecutionFailure with safe diagnostics
        for each item in data.webPages.value:
            if malformed or missing URL: count rejection and continue
            map standard hit with empty body fields
        log candidate summary; return hits (including valid [])
    except terminal member execution failure:
        pool tries another eligible unattempted member if available
        otherwise propagate existing provider failure
    orchestrator stages, normalizes and admits eligible hits
    retain existing partial-success / aggregate failure semantics
else:
    retain existing selection behavior
Daemon returns existing result format
```

### 5.3 Explicit URL Fetch
```text
Fetch orchestrator checks admitted URL/body state
if not admitted or already prepared:
    use existing rejection or short-circuit
else:
    schedule configured Fetch-capable providers only
    LangSearch is never a Fetch candidate
    use existing fetch/judge/clean/storage rules and return result or error
```

## 6. Interfaces & Contracts

### Internal adapter
Constructor: same keyword-only arguments as AnySearchAdapter. Method: async search(query: str) -> list[KeywordSearchHit]. No fetch method.

### Vendor request
POST /v1/web-search; standard Bearer authentication and JSON content type.
```json
{"query": "example query", "count": 10}
```
Illustrative synthetic response:
```json
{"code": 200, "msg": null, "data": {"webPages": {"value": [
  {"name": "Example source", "url": "https://example.org/", "snippet": "A short result."}
]}}, "usage": {"input_tokens": 2, "output_tokens": 4}}
```
Mapped hit: URL https://example.org/, title Example source, snippet A short result.; both body fields empty. No new public result fields.

### Configuration contract
Use the existing Web provider table and credential array:
```toml
[web_providers.langsearch]
enable_search = false
enable_fetch = false

[[web_providers.langsearch.credentials]]
api_url = "https://api.langsearch.com"
rate_limits = [{ requests = 5, period_seconds = 1 }]
```
In the actual example template, set the credential's api_key_env field to the environment variable name LANGSEARCH_API_KEY. Never store the credential value. This field is required when enabling the provider. Existing credential_strategy, use_proxy and optional per-member limits remain supported; omitted limits are unrestricted. No vendor-specific config fields.

## 7. Expected change surface

- New production module: src/agent_search_gateway/providers/web/langsearch.py.
- Existing production module: src/agent_search_gateway/providers/defaults.py, import and registration only.
- Non-production: config.example.toml, README.md, docs/provider-rate-limits.md, relevant provider enumerations/counts, focused tests/fixtures and opt-in live smoke. Update existing count/order assertions additively (26 Web / 33 combined Web, academic and OA if the existing 32-service count is used).
- No implementation code is included in this design change.
