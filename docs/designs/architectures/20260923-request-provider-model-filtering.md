## Architecture: Request-Scoped Provider and Model Filtering

### 1. Scope & Assumptions

#### In Scope

- Add request-scoped `--provider` filtering to `keyword-search`, `paper-search`, and `llm-search`.
- Add request-scoped `--model` filtering to `llm-search`.
- Allow `--provider` and `--model` to be used independently or together for `llm-search`.
- Preserve current behavior when neither filter is supplied.
- Keep filtering scoped to one CLI request; the daemon runtime remains fully assembled from configuration.
- Treat LLM provider identity as the configured provider alias referenced by `[[search_llm.providers]]`, not the transport protocol name.
- Evaluate LLM provider/model existence only against configured search invocations.

#### Todo

- No provider selection for `url-fetch`.
- No startup-time or daemon-global provider filtering.
- No multi-value `--provider` or `--model` syntax in this change.
- No wildcard, regex, partial, or case-insensitive matching.
- No provider-listing/discovery command.
- No filtering of `fetch_llm` stages such as judge, safety, content cleaning, or focus summary.

#### Assumptions

- Provider and model names are exact, case-sensitive strings.
- The local socket protocol is internal and may evolve to carry request-scoped selectors.
- Existing configured providers remain instantiated at daemon startup; request filtering selects from already assembled providers/invocations.
- `keyword-search` provider names come from `KeywordSearchProvider.name`.
- `paper-search` provider names come from `AcademicSearchProvider.name`.
- `llm-search` selection operates over `ResolvedLLMConfig.search_invocations` / `SearchOrchestrator._llm_invocations`.

---

### 2. Architecture Summary

The feature is implemented as request metadata that flows from CLI parsing through the typed request model and NDJSON socket protocol into daemon dispatch, then into the relevant orchestrator. The daemon continues to build the complete runtime from configuration exactly as it does today. Each orchestrator performs a narrow, request-local selection over its already assembled provider or invocation collection immediately before execution. This preserves the current long-lived daemon model, avoids runtime rebuilds, keeps provider credentials and transport clients centralized, and makes the new flags suitable for smoke testing one configured search backend at a time.

---

### 3. Design Decisions

#### Runtime Model

##### Keep the daemon runtime fully assembled

- Description: `Runtime.build()` continues to instantiate every enabled/configured provider, quota, client, and orchestrator. `--provider` and `--model` do not alter runtime construction.

- Rational: The requested behavior is per CLI invocation. Runtime-level filtering would make the flag stateful, require daemon restarts, and couple a diagnostic selector to lifecycle management.

- Trade-offs: Providers not selected by a given request are still initialized and their credentials must still be valid at daemon startup.

- Rejected Alternatives:
  - Filter providers in `Runtime.build()`:
    - Description: Build a reduced runtime containing only the selected provider.
    - Why Rejected: A single daemon serves multiple requests and has no request selector available at startup.
  - Rebuild runtime per request:
    - Description: Construct a temporary runtime using request filters.
    - Why Rejected: Duplicates clients/quotas, increases latency, complicates cleanup, and defeats the current foreground-daemon architecture.

#### Interface / Protocol

##### Carry selectors in typed search requests

- Description: Extend `KeywordSearchRequest` and `PaperSearchRequest` with an optional `provider: str | None`. Extend `LLMSearchRequest` with optional `provider: str | None` and `model: str | None`.

- Rational: These selectors are part of request intent and must survive the CLI/socket boundary. Keeping them in typed request models makes the data path explicit and testable.

- Trade-offs: The internal NDJSON schema changes and protocol codec tests must be updated.

- Rejected Alternatives:
  - Store selectors in process-global state:
    - Description: CLI or daemon writes a global filter before dispatch.
    - Why Rejected: Unsafe under concurrent requests and violates request isolation.
  - Encode selectors into query/prompt text:
    - Description: Pass provider choice implicitly through user content.
    - Why Rejected: Conflates routing metadata with semantic input and would leak internal control syntax into provider prompts.

##### Scope CLI flags only to relevant commands

- Description:
  - `keyword-search QUERY [--provider NAME]`
  - `paper-search QUERY [--provider NAME]`
  - `llm-search PROMPT [--scope ...] [--provider NAME] [--model NAME]`
  - `url-fetch` remains unchanged.

- Rational: Provider/model filtering is intended for search-provider smoke testing. `url-fetch` has a different public abstraction: fetch an admitted URL while its scheduler handles provider fallback internally.

- Trade-offs: CLI option availability is intentionally asymmetric across commands.

- Rejected Alternatives:
  - Add `--provider` to every business command:
    - Description: Expose fetch scheduler selection as well.
    - Why Rejected: Expands scope beyond the debugging need and leaks an internal scheduling detail into the public interface.

#### State Management

##### Keep selectors immutable and request-local

- Description: Selector values are normalized at CLI input boundaries by trimming whitespace and stored in frozen request dataclasses.

- Rational: Requests may run concurrently in the daemon; immutable request-local state avoids cross-request contamination.

- Trade-offs: Selection is recomputed for each request, but collections are small and the cost is negligible.

#### Provider Integration

##### Filter by exact provider identity

- Description:
  - Keyword search matches `provider.name == request.provider`.
  - Paper search matches `provider.name == request.provider`.
  - LLM search matches `invocation.provider == request.provider`.

- Rational: Provider aliases are the identities users configure. In LLM configuration, `protocol = "openai"` only describes transport compatibility and is not unique provider identity.

- Trade-offs: Renaming a provider alias in configuration changes the CLI selector value.

- Rejected Alternatives:
  - Match LLM provider by protocol:
    - Description: Use `protocol = "openai"` as `--provider`.
    - Why Rejected: Multiple configured providers can share the same OpenAI-compatible protocol.
  - Match by API URL:
    - Description: Treat endpoint identity as provider identity.
    - Why Rejected: URLs are implementation/configuration details and are poor stable CLI identifiers.

##### Filter LLM invocations by provider/model intersection

- Description:
  - No selectors: use all search invocations.
  - Provider only: select every invocation whose `provider` matches.
  - Model only: select every invocation whose `model` matches.
  - Both: select every invocation matching both fields.

- Rational: Provider and model are orthogonal invocation attributes. A provider alias may legitimately have multiple search models, and a model name may appear under multiple providers.

- Trade-offs: `--model` alone can intentionally run multiple providers if they share the same model string.

- Rejected Alternatives:
  - Require `--model` to be paired with `--provider`:
    - Description: Disallow model-only selection.
    - Why Rejected: Adds an unnecessary dependency between two independently meaningful selectors.
  - Require a provider to map to exactly one invocation:
    - Description: Error if one provider alias has multiple search models.
    - Why Rejected: Conflicts with the existing list-based `search_llm.providers` configuration model.

#### Concurrency / Scheduling

##### Filter before fan-out, keep existing execution behavior afterward

- Description: Each orchestrator derives a selected tuple before `asyncio.gather`. Once selected, execution, quota acquisition, ordering, aggregation, logging, and result writing follow the existing code path.

- Rational: This produces the smallest behavioral change and preserves current concurrency and aggregation semantics.

- Trade-offs: Selection logic becomes part of orchestration entry methods.

- Rejected Alternatives:
  - Run all providers and discard unselected results:
    - Description: Keep current fan-out and filter outputs.
    - Why Rejected: Defeats smoke testing because unselected providers still receive network traffic.

#### Security

##### Validate selectors as control metadata, never provider payload

- Description: Provider/model selectors are used only for local selection and are not inserted into search queries, prompts, URLs, HTTP request bodies, or authentication metadata.

- Rational: This prevents routing controls from becoming remote provider input and maintains the current separation between user content and local execution metadata.

- Trade-offs: Error messages may echo the user-supplied selector value; these values are non-secret CLI control strings.

#### Observability

##### Preserve existing provider events

- Description: Existing provider-stage events remain authoritative. Because only selected providers/invocations execute, DEBUG logs naturally show only selected search pipelines.

- Rational: No new logging subsystem is required to verify that filtering worked.

- Trade-offs: Without DEBUG logging, the primary confirmation is the request outcome/result file rather than a new explicit selection event.

- Rejected Alternatives:
  - Add mandatory stdout selection diagnostics:
    - Description: Print selected provider/model before results.
    - Why Rejected: Would break the documented business-command stdout contract.

#### Future Migration

##### Keep selection reusable without creating a generic provider-selector abstraction now

- Description: Implement direct selection helpers close to the orchestrators or as small private helpers if needed for testability. Do not introduce a cross-domain selector framework.

- Rational: Web providers, academic providers, and LLM invocations have different types and validation semantics. A generic abstraction would add indirection before there is a demonstrated shared lifecycle.

- Trade-offs: Some small filtering patterns may be repeated.

- Rejected Alternatives:
  - Introduce a universal provider selection service:
    - Description: Centralize all provider/model matching in a new shared component.
    - Why Rejected: YAGNI; it would couple three domains whose selection rules are not identical.

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| CLI parser (`cli.py`) | Capture request-scoped selectors | Parse `--provider` / `--model`, reject unsupported placement through argparse, trim selector text | CLI commands | argparse | No | Source / validator |
| Request models (`models.py`) | Represent immutable routing intent | Carry query/prompt/scope plus optional selectors | Internal typed dataclasses | None | Yes, immutable request values | Contract |
| Protocol codec (`protocol.py`) | Transport selectors across local socket | Encode/decode optional request fields while preserving defaults | Internal NDJSON protocol | JSON codec, request models | No | Transformer / validator |
| Foreground daemon (`daemon.py`) | Dispatch typed requests | Pass selectors to corresponding orchestrator methods | Internal dispatch methods | Runtime, request models | No selector state | Coordinator |
| `SearchOrchestrator` | Execute keyword and LLM search workflows | Select web providers or LLM invocations, validate selector matches, fan out selected work | `keyword_search(...)`, `llm_search(...)` | Providers, LLM stages, quotas, writer | Existing provider/invocation tuples | Coordinator / validator |
| `PaperSearchOrchestrator` | Execute direct academic search | Select academic providers, validate selector match, fan out selected work | `paper_search(...)` | Academic providers, aggregator, quotas, writer | Existing provider tuple | Coordinator / validator |
| Runtime assembly (`runtime.py`) | Build full long-lived runtime | Instantiate all configured enabled providers and clients | `Runtime.build()` | Resolved config, registries | Yes, runtime resources | Provider factory / owner |
| Existing providers and LLM clients | Perform remote work | Execute only when selected by an orchestrator | Existing provider/client contracts | HTTP executor, credentials | Provider-local runtime state | Adapter |

Runtime assembly must not know about per-request selectors. Provider adapters must not know whether they were selected through a CLI filter or reached through the default all-provider path.

---

### 5. Data Flow

#### 5.1 Keyword Search

```text
CLI:
    parse query
    parse optional --provider
    trim values
    if query empty:
        return EMPTY_QUERY
    if provider flag supplied but trims to empty:
        return BAD_REQUEST
    request = KeywordSearchRequest(query, provider)

Protocol:
    encode request with optional provider field
    daemon decodes request

Daemon:
    call SearchOrchestrator.keyword_search(
        query,
        request_id=generated_request_id,
        provider=provider,
    )

SearchOrchestrator:
    validate query/request_id
    if configured keyword provider tuple is empty:
        raise NO_KEYWORD_SEARCH_PROVIDERS

    selected = all keyword providers
    if provider is not None:
        selected = providers where provider.name == requested provider
        if selected is empty:
            raise BAD_REQUEST for unmatched keyword-search provider

    outcomes = gather(run pipeline for each selected provider)
    if all selected pipelines fail:
        raise ALL_PROVIDERS_FAILED
    aggregate existing successful outcomes
    write result file
    return path
```

#### 5.2 Paper Search

```text
CLI:
    parse query
    parse optional --provider
    validate/trim
    request = PaperSearchRequest(query, provider)

Protocol:
    encode/decode optional provider

Daemon:
    call PaperSearchOrchestrator.paper_search(
        query,
        request_id=generated_request_id,
        provider=provider,
    )

PaperSearchOrchestrator:
    validate query/request_id
    if configured academic provider tuple is empty:
        raise NO_ACADEMIC_SEARCH_PROVIDERS

    selected = all academic providers
    if provider is not None:
        selected = providers where provider.name == requested provider
        if selected is empty:
            raise BAD_REQUEST for unmatched paper-search provider

    outcomes = gather(run provider for each selected provider)
    if all selected providers fail:
        raise ALL_PROVIDERS_FAILED
    finalize/aggregate hits using existing path
    write result file
    return path
```

#### 5.3 LLM Search

```text
CLI:
    parse prompt
    parse --scope
    parse optional --provider
    parse optional --model
    validate/trim
    request = LLMSearchRequest(prompt, scope, provider, model)

Protocol:
    encode/decode optional provider/model fields

Daemon:
    call SearchOrchestrator.llm_search(
        prompt,
        request_id=generated_request_id,
        scope=scope,
        provider=provider,
        model=model,
    )

SearchOrchestrator:
    validate prompt/request_id/scope
    if configured search invocation tuple is empty:
        raise NO_LLM_SEARCH_PROVIDERS

    invocations = configured search invocations

    if provider is None and model is None:
        selected = invocations

    else if provider is not None and model is None:
        selected = invocations where invocation.provider == provider
        if selected is empty:
            raise BAD_REQUEST: provider has no search invocation

    else if provider is None and model is not None:
        selected = invocations where invocation.model == model
        if selected is empty:
            raise BAD_REQUEST: model has no search invocation

    else:
        provider_matches = invocations where invocation.provider == provider
        model_matches = invocations where invocation.model == model

        if provider_matches is empty and model_matches is empty:
            raise BAD_REQUEST: neither selector exists among search invocations
        if provider_matches is empty:
            raise BAD_REQUEST: provider does not exist among search invocations
        if model_matches is empty:
            raise BAD_REQUEST: model does not exist among search invocations

        selected = provider_matches where invocation.model == model
        if selected is empty:
            raise BAD_REQUEST: provider/model combination has no search invocation

    execute existing scope branch (web / paper / all)
    every internal LLM-search fan-out uses selected instead of full invocation tuple

    if all selected provider pipelines/branches fail:
        preserve existing ALL_PROVIDERS_FAILED behavior
    write existing result format
    return path
```

---

### 6. Interfaces & Contracts

#### CLI Contract

Public CLI additions:

```text
agent-search-gateway keyword-search QUERY [--provider NAME]
agent-search-gateway paper-search QUERY [--provider NAME]
agent-search-gateway llm-search PROMPT [--scope {web,paper,all}] [--provider NAME] [--model NAME]
```

`url-fetch`, `start`, `stop`, and `doctor` do not accept these flags.

#### Request Contract

Internal keyword request:

```json
{
  "type": "keyword_search",
  "query": "example",
  "provider": "tavily"
}
```

The `provider` field is omitted when no filter is requested.

Internal paper request:

```json
{
  "type": "paper_search",
  "query": "example",
  "provider": "arxiv"
}
```

The `provider` field is omitted when no filter is requested.

Internal LLM request:

```json
{
  "type": "llm_search",
  "prompt": "example",
  "scope": "web",
  "provider": "openai_main",
  "model": "gpt-4o-mini"
}
```

`scope` may retain its current omission/default behavior for `web`. `provider` and `model` are omitted independently when not supplied.

#### Selection Contract

```text
keyword provider match:
    exact(provider.name, requested_provider)

paper provider match:
    exact(provider.name, requested_provider)

LLM provider match:
    exact(invocation.provider, requested_provider)

LLM model match:
    exact(invocation.model, requested_model)

LLM provider + model:
    logical AND
```

These matching rules are public CLI behavior. The socket representation and Python request classes remain internal implementation details.
