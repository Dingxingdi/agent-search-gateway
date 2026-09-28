## Testing: Request-Scoped Provider and Model Filtering

### 1. Test Strategy

The feature should be verified as a request-routing change rather than as a provider-adapter change. The core risk is not remote API correctness; it is accidentally invoking the wrong provider(s), silently falling back to all providers, misclassifying selector failures, or changing default behavior when no selectors are present.

Primary goals:

- Prove CLI flags exist only on the intended commands.
- Prove selector values are trimmed, validated, and encoded into typed requests.
- Prove protocol frames remain backward compatible when selector fields are absent.
- Prove daemon dispatch forwards selectors without introducing daemon-global state.
- Prove keyword and paper search execute only the selected provider.
- Prove LLM search supports provider-only, model-only, and provider+model intersection filtering.
- Prove multiple matching LLM invocations are all executed.
- Prove all four LLM mismatch cases produce the intended `BAD_REQUEST` semantics before provider work begins.
- Prove existing `NO_*_PROVIDERS` and `ALL_PROVIDERS_FAILED` meanings remain unchanged.
- Prove default selector-free behavior still fans out exactly as before.
- Prove `url-fetch` does not accept `--provider` or `--model`.
- Keep the default suite offline and deterministic.

No new test framework or network dependency is required for the normal test suite.

---

### 2. Test Layers

| Layer | Purpose | Real network? | Suggested location |
|---|---|---:|---|
| CLI parser/request construction | Verify flag scope, trimming, typed request fields, blank values | No | `tests/cli/test_cli.py` |
| Protocol codec | Verify optional fields, malformed fields, backward-compatible frames | No | `tests/unit/test_protocol_codec.py` |
| Daemon dispatch | Verify selectors are forwarded unchanged to orchestrators | No | `tests/daemon/test_daemon_dispatch.py` |
| Keyword orchestration | Verify exact provider selection and no unintended provider calls | No | `tests/orchestrators/test_keyword_search_pipeline.py` / focused new tests if cleaner |
| Paper orchestration | Verify exact academic-provider selection and mismatch behavior | No | `tests/orchestrators/test_paper_search_pipeline.py` |
| LLM orchestration | Verify provider/model selection matrix and scope behavior | No | `tests/orchestrators/test_llm_search.py` / `test_scoped_llm_search.py` |
| Acceptance | Verify CLI -> socket -> daemon -> orchestrator path and public stderr/stdout behavior | No | `tests/acceptance/test_gateway_workflows.py` |
| Live smoke | Confirm one real backend is invoked when selected | Yes, opt-in only | existing `tests/integration/` patterns or manual CLI smoke commands |

The main correctness boundary is the orchestrator. CLI/protocol/daemon tests prove metadata transport; orchestrator tests prove routing behavior.

---

### 3. CLI Tests

Extend `tests/cli/test_cli.py`.

#### Keyword Search Provider Flag

Assert:

```python
parser.parse_args(["keyword-search", "query", "--provider", "tavily"])
```

produces a request equivalent to:

```python
KeywordSearchRequest("query", provider="tavily")
```

Also assert leading/trailing whitespace is trimmed from the selector.

#### Paper Search Provider Flag

Assert:

```python
PaperSearchRequest("papers", provider="arxiv")
```

is constructed from the corresponding CLI invocation.

#### LLM Provider-Only Flag

Assert:

```text
llm-search "prompt" --provider openai_main
```

creates an LLM request whose provider is `openai_main`, model is `None`, and scope remains the default `web`.

#### LLM Model-Only Flag

Assert:

```text
llm-search "prompt" --model gpt-5
```

creates an LLM request whose provider is `None` and model is `gpt-5`.

#### LLM Provider + Model

Assert both fields survive together, including with `--scope paper` and `--scope all`.

#### Blank Selector Rejection

Parameterize:

- `keyword-search query --provider "   "`
- `paper-search query --provider "   "`
- `llm-search prompt --provider "   "`
- `llm-search prompt --model "   "`

Assert:

- CLI exits through existing error path;
- stderr contains the specific non-empty validation message;
- socket client is not called.

#### Unsupported Flag Placement

Assert argparse rejects:

- `url-fetch URL --provider tavily`
- `url-fetch URL --model anything`
- `start --provider tavily`
- `doctor --provider tavily`
- `keyword-search query --model gpt-5`
- `paper-search query --model gpt-5`

This locks down the intended public CLI surface.

#### No-Flag Regression

Keep existing assertions for selector-free requests and extend them to confirm optional fields are `None`.

---

### 4. Protocol Codec Tests

Extend `tests/unit/test_protocol_codec.py`.

#### Keyword Provider Round Trip

Assert:

```python
encode_request(KeywordSearchRequest("hello", provider="tavily"))
```

emits:

```json
{"type":"keyword_search","query":"hello","provider":"tavily"}
```

and decodes back to the same request.

#### Paper Provider Round Trip

Equivalent test for `PaperSearchRequest(..., provider="arxiv")`.

#### LLM Selector Combinations

Cover all request shapes:

1. no provider/model;
2. provider only;
3. model only;
4. provider + model;
5. each of the above with non-default scope.

Assert omitted optional fields remain omitted rather than being encoded as null.

#### Backward Compatibility

Retain explicit tests proving legacy frames still decode:

```json
{"type":"keyword_search","query":"hello"}
{"type":"paper_search","query":"papers"}
{"type":"llm_search","prompt":"find"}
{"type":"llm_search","prompt":"find","scope":"paper"}
```

These must produce requests with selectors set to `None`.

#### Invalid Selector Types

Parameterize malformed frames such as:

```json
{"type":"keyword_search","query":"x","provider":1}
{"type":"paper_search","query":"x","provider":[]}
{"type":"llm_search","prompt":"x","provider":false}
{"type":"llm_search","prompt":"x","model":{}}
```

Assert existing generic `BAD_REQUEST` protocol response.

#### Unknown Extra Fields

Keep exact-schema behavior by asserting unrelated fields remain rejected.

---

### 5. Daemon Dispatch Tests

Extend `tests/daemon/test_daemon_dispatch.py`.

Update fake orchestrators so their methods record selector arguments.

#### Keyword Dispatch

Send:

```python
KeywordSearchRequest("hello", provider="tavily")
```

Assert daemon calls:

```python
runtime.search_orchestrator.keyword_search(
    "hello",
    request_id=<generated>,
    provider="tavily",
)
```

#### Paper Dispatch

Equivalent assertion for academic provider selector.

#### LLM Dispatch

Cover:

- provider only;
- model only;
- provider + model;
- provider + model + non-default scope.

Assert values are forwarded unchanged.

#### No Global Leakage

Send two sequential requests:

1. keyword request with provider `tavily`;
2. keyword request without provider.

Assert the second call receives `provider=None`.

If convenient, add a concurrent dispatch test using two different provider values to prove no request-global mutable selector state exists.

---

### 6. Keyword Search Orchestrator Tests

Use lightweight fake `KeywordSearchProvider` instances with distinct names and call counters.

Given providers:

```text
tavily
exa
brave
```

#### No Selector

Call `keyword_search(..., provider=None)`.

Assert all configured providers are invoked exactly once, preserving existing fan-out behavior.

#### Exact Provider Selection

Call with `provider="exa"`.

Assert:

- only `exa` is invoked;
- `tavily` and `brave` are not called;
- normal result-writing/aggregation occurs from the selected provider only.

#### Unknown Provider

Call with `provider="missing"`.

Assert:

- `InputFailure(BAD_REQUEST, ...)`;
- no provider call counters change;
- no result file is created.

#### Case Sensitivity

If provider is named `exa`, request `EXA`.

Assert mismatch and no calls.

#### Runtime Has No Providers

With an empty provider tuple and any selector state, retain existing `NO_KEYWORD_SEARCH_PROVIDERS` semantics.

#### Selected Provider Execution Failure

Have the selected provider raise an execution failure.

Assert result is existing `ALL_PROVIDERS_FAILED`, not `BAD_REQUEST`.

---

### 7. Paper Search Orchestrator Tests

Use fake academic providers named, for example:

```text
arxiv
openalex
crossref
```

Mirror the keyword cases:

- no selector invokes all;
- `provider="openalex"` invokes only OpenAlex fake;
- unknown provider raises `BAD_REQUEST` before any call;
- empty runtime retains `NO_ACADEMIC_SEARCH_PROVIDERS`;
- selected provider execution failure retains `ALL_PROVIDERS_FAILED`.

Also assert OA resolver/enrichment is not invoked after selector validation fails.

---

### 8. LLM Search Selection Matrix

This is the highest-value test area.

Use deterministic fake `LLMInvocation` values:

```python
A1 = LLMInvocation(provider="openai_main", model="gpt-5")
A2 = LLMInvocation(provider="openai_main", model="gpt-5-mini")
B1 = LLMInvocation(provider="deepseek_main", model="deepseek-v3")
C1 = LLMInvocation(provider="azure_main", model="gpt-5")
```

Instrument the fake LLM stage/client path so tests can record exactly which invocation objects were executed.

#### No Selectors

Expected selected set:

```text
A1, A2, B1, C1
```

This is the key regression test for legacy behavior.

#### Provider Only

Request:

```text
--provider openai_main
```

Expected:

```text
A1, A2
```

This proves provider-only selection intentionally permits multiple models.

#### Model Only

Request:

```text
--model gpt-5
```

Expected:

```text
A1, C1
```

This proves model-only selection intentionally permits multiple providers.

#### Provider + Model

Request:

```text
--provider openai_main --model gpt-5
```

Expected:

```text
A1
```

#### Duplicate Matching Pair

If configuration contains two identical provider/model invocations, both should remain selected and execute. The feature is a filter, not a deduplicator.

---

### 9. LLM Selector Error Matrix Tests

Using the same invocation set, test all required diagnostic branches before any LLM execution occurs.

#### Provider Only Missing

```text
provider = "missing"
model = None
```

Assert `BAD_REQUEST` and provider-specific message.

#### Model Only Missing

```text
provider = None
model = "missing-model"
```

Assert `BAD_REQUEST` and model-specific message.

#### Both Missing

```text
provider = "missing"
model = "missing-model"
```

Assert the message reports that neither selector matches.

#### Provider Missing, Model Exists

```text
provider = "missing"
model = "gpt-5"
```

Assert provider-specific missing message.

#### Provider Exists, Model Missing

```text
provider = "openai_main"
model = "missing-model"
```

Assert model-specific missing message.

#### Both Exist Separately, Pair Missing

```text
provider = "openai_main"
model = "deepseek-v3"
```

Both identifiers exist among search invocations, but no pair exists.

Assert pair-specific mismatch message.

For every mismatch case, assert zero LLM invocation executions and no result file.

---

### 10. LLM Scope Regression Tests

Run the selection matrix through all relevant scopes where practical.

#### Scope: web

Assert only selected invocations enter `_run_llm_pipeline`.

#### Scope: paper

Assert only selected invocations enter `_run_llm_paper_pipeline`.

#### Scope: all

Assert both web and paper branches use the same selected invocation subset.

This is important: filtering must happen once logically and constrain both branches. A bug where the web branch is filtered but the paper branch still uses `self._llm_invocations` must be caught.

#### Selected Invocation Failure

For each scope, at least one focused regression should prove valid selection followed by provider failure still produces existing `ALL_PROVIDERS_FAILED` behavior.

---

### 11. Acceptance Tests

Add one end-to-end offline acceptance scenario using the existing fake/controlled runtime pattern.

Suggested flow:

1. Start daemon with fake keyword providers `alpha` and `beta`.
2. Invoke CLI-equivalent request with `--provider beta`.
3. Assert only `beta` recorded a call.
4. Assert stdout is still only the result path.
5. Invoke without `--provider`.
6. Assert both providers execute.

Repeat one compact LLM case:

1. configure multiple search invocations;
2. request provider+model combination;
3. assert only matching invocation runs;
4. request mismatched pair;
5. assert CLI receives `BAD_REQUEST` text on stderr and no output file is created.

The acceptance layer should not duplicate the full selector matrix already covered by orchestrator unit tests.

---

### 12. Documentation Contract Tests

Because CLI flags are a supported public interface, update the relevant documentation tests if they assert command examples or `--help` output.

Verify README documents:

```text
keyword-search "query" --provider tavily
paper-search "topic" --provider arxiv
llm-search "prompt" --provider openai_main
llm-search "prompt" --model gpt-5
llm-search "prompt" --provider openai_main --model gpt-5
```

Documentation must state:

- provider names are configured aliases;
- LLM `--provider` is not the `protocol = "openai"` value;
- model-only filtering can select multiple providers;
- provider-only filtering can select multiple LLM models;
- `url-fetch` does not expose provider selection.

---

### 13. Opt-In Live Smoke Tests

The feature exists primarily to make live provider debugging easier, so manual or opt-in live smoke coverage is valuable even though it must not be part of the default CI suite.

#### Keyword Provider Smoke

For each configured web-search provider:

```bash
agent-search-gateway keyword-search "OpenAI latest model" --provider tavily
agent-search-gateway keyword-search "OpenAI latest model" --provider exa
```

Expected:

- command succeeds or reports that provider's real failure;
- DEBUG log contains search-stage events only for the selected web provider;
- no other web-search provider receives a request.

#### Academic Provider Smoke

Example:

```bash
agent-search-gateway paper-search "CRISPR cancer immunotherapy" --provider arxiv
```

Expected:

- only the selected academic discovery provider runs;
- normal paper result schema is produced.

#### LLM Provider Smoke

Example:

```bash
agent-search-gateway llm-search "Find recent work on agent search" --provider openai_main
```

Expected:

- all search invocations belonging to `openai_main` run;
- search invocations under other provider aliases do not.

#### LLM Model Smoke

Example:

```bash
agent-search-gateway llm-search "Find recent work on agent search" --model gpt-5
```

Expected:

- every search invocation using exactly `gpt-5` runs, even if multiple provider aliases use that model string.

#### LLM Provider + Model Smoke

Example:

```bash
agent-search-gateway llm-search "Find recent work on agent search" \
  --provider openai_main \
  --model gpt-5
```

Expected:

- only invocations matching both selectors run.

These live checks should use the existing environment-variable credentials and normal daemon configuration. They are smoke tests, not assertions about ranking quality or result count.

---

### 14. Regression Checklist

Before considering implementation complete, the following existing behavior must still pass unchanged:

- selector-free `keyword-search` calls all enabled keyword providers;
- selector-free `paper-search` calls all enabled academic providers;
- selector-free `llm-search` calls all configured search invocations;
- `llm-search --scope web|paper|all` result formats are unchanged;
- `url-fetch` scheduling and fallback are unchanged;
- result filenames and JSONL schemas are unchanged;
- successful business stdout remains final-output-only;
- existing provider quota behavior remains unchanged;
- existing provider failure isolation remains unchanged;
- existing daemon request IDs and debug logging remain unchanged;
- no selector is persisted across requests.

---

### 15. Suggested Verification Commands

Offline development verification:

```bash
uv run pytest tests/cli/test_cli.py -v
uv run pytest tests/unit/test_protocol_codec.py -v
uv run pytest tests/daemon/test_daemon_dispatch.py -v
uv run pytest tests/orchestrators/test_keyword_search_pipeline.py -v
uv run pytest tests/orchestrators/test_paper_search_pipeline.py -v
uv run pytest tests/orchestrators/test_llm_search.py tests/orchestrators/test_scoped_llm_search.py -v
uv run pytest tests/acceptance/test_gateway_workflows.py -v
uv run ruff format --check src tests
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

After documentation updates:

```bash
uv run pytest tests/docs -v
uv run python scripts/build_docs.py
```

Live smoke tests remain opt-in and should be run explicitly with configured credentials after the offline suite passes.
