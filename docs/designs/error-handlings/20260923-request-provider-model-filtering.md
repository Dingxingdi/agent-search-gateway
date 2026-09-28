## Error Handling: Request-Scoped Provider and Model Filtering

### 1. Error-Handling Principles

This feature reuses the existing gateway error taxonomy. Selector failures are request-input failures, not configuration failures and not provider execution failures.

Primary rules:

- No new `ErrorCode` is introduced.
- A syntactically present but blank `--provider` or `--model` is rejected as `InputFailure(ErrorCode.BAD_REQUEST, ...)`.
- A selector that does not match an enabled/configured search provider or LLM search invocation is rejected as `InputFailure(ErrorCode.BAD_REQUEST, ...)`.
- Existing `NO_*_PROVIDERS` errors continue to mean that the daemon runtime has no providers/invocations for that search class at all.
- Existing `ALL_PROVIDERS_FAILED` continues to mean that at least one provider/invocation was selected for execution, but every selected execution path failed.
- LLM provider/model existence is evaluated only against `search_llm.providers` invocations, never against unrelated `llm_providers` entries or `fetch_llm` stages.
- When both LLM selectors are supplied, errors distinguish: neither selector exists, provider only is missing, model only is missing, and both exist independently but no invocation contains that pair.
- Selector validation occurs before any selected provider receives network traffic.
- Selector values are control metadata and must not be inserted into provider prompts, queries, or remote payloads.
- Existing cancellation, timeout, retry, provider isolation, result-writing, and daemon exception-sanitization behavior is preserved.

---

### 2. Selector Normalization Failures

#### Blank Keyword/Paper Provider Selector

Condition:

- The CLI receives an explicitly supplied provider value that becomes empty after trimming, for example `--provider ""` or a whitespace-only argument.

Handling:

- Reject before sending a socket request where possible.
- Raise/return `InputFailure(ErrorCode.BAD_REQUEST, "Provider must not be empty")`.
- Do not treat this as “no filter”; omission and explicit blank input are different.
- The orchestrator should defensively reject blank selector values if a request is constructed internally or arrives through the socket boundary.

Rationale:

- Silently converting an explicit blank selector to `None` could accidentally fan out to all providers during a debugging command.

#### Blank LLM Model Selector

Condition:

- `llm-search` receives `--model ""` or whitespace-only input.

Handling:

- Reject as `InputFailure(ErrorCode.BAD_REQUEST, "Model must not be empty")`.
- Do not fall back to all configured models.

#### Blank LLM Provider Selector

Condition:

- `llm-search` receives an explicitly blank provider selector.

Handling:

- Reject as `InputFailure(ErrorCode.BAD_REQUEST, "Provider must not be empty")`.

---

### 3. Keyword-Search Selector Failures

#### Runtime Has No Keyword Providers

Condition:

- `SearchOrchestrator._keyword_providers` is empty before applying any request selector.

Handling:

- Preserve the existing:
  `ExecutionFailure(ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS, "No keyword search providers are enabled")`.

Rationale:

- This describes daemon/runtime state, not a bad selector.

#### Requested Keyword Provider Does Not Exist

Condition:

- At least one keyword provider exists, but no `KeywordSearchProvider.name` exactly matches the requested selector.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No enabled keyword-search provider matches '<provider>'")`.
- Do not execute any keyword provider.
- Do not create a result file.

Notes:

- Matching is exact and case-sensitive.
- A configured web provider that is enabled only for fetch does not count as a keyword-search match because it is absent from the orchestrator's keyword provider tuple.

#### Selected Keyword Provider Fails

Condition:

- The selector matches one or more executable keyword providers (normally one provider identity), but all selected pipelines fail during quota acquisition, HTTP execution, provider parsing, or downstream stages.

Handling:

- Preserve:
  `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "All keyword search provider pipelines failed")`.
- Do not convert this to `BAD_REQUEST`, because selection was valid and execution actually began.

---

### 4. Paper-Search Selector Failures

#### Runtime Has No Academic Search Providers

Condition:

- `PaperSearchOrchestrator.providers` is empty before request filtering.

Handling:

- Preserve:
  `ExecutionFailure(ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS, "No academic search providers are enabled")`.

#### Requested Academic Provider Does Not Exist

Condition:

- Academic search providers exist, but no enabled `AcademicSearchProvider.name` exactly matches the requested selector.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No enabled paper-search provider matches '<provider>'")`.
- Do not execute any academic provider.
- Do not invoke OA enrichment solely because of a failed selector.
- Do not create a result file.

#### Selected Academic Provider Fails

Condition:

- The selector is valid but every selected academic provider execution fails.

Handling:

- Preserve:
  `ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "All academic search provider pipelines failed")`.

---

### 5. LLM-Search Selector Failures

All LLM selector existence checks operate against the configured `SearchOrchestrator._llm_invocations` collection, which originates from `[[search_llm.providers]]`.

A provider existing only under `[llm_providers.<name>]` is not selectable by `llm-search` unless at least one search invocation references it. Likewise, a provider used only by `fetch_llm` is not considered a valid `llm-search --provider` target.

#### Runtime Has No LLM Search Invocations

Condition:

- `SearchOrchestrator._llm_invocations` is empty before selector filtering.

Handling:

- Preserve:
  `ExecutionFailure(ErrorCode.NO_LLM_SEARCH_PROVIDERS, "No LLM search providers are configured")`.

This check occurs before selector-specific “not found” checks because there is no searchable invocation namespace at all.

#### Provider-Only Selector Does Not Match

Request shape:

```text
llm-search "..." --provider P
```

Condition:

- No search invocation has `invocation.provider == P`.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No LLM search invocation matches provider 'P'")`.

#### Model-Only Selector Does Not Match

Request shape:

```text
llm-search "..." --model M
```

Condition:

- No search invocation has `invocation.model == M`.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No LLM search invocation matches model 'M'")`.

#### Both Provider and Model Are Missing

Request shape:

```text
llm-search "..." --provider P --model M
```

Condition:

- No search invocation has provider `P`.
- No search invocation has model `M`.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No LLM search invocation matches provider 'P' or model 'M'")`.

Rationale:

- This reports that both selector dimensions are unknown rather than misleadingly blaming only the first check performed.

#### Provider Is Missing, Model Exists

Condition:

- At least one search invocation uses model `M`.
- No search invocation uses provider `P`.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No LLM search invocation matches provider 'P'")`.

#### Model Is Missing, Provider Exists

Condition:

- At least one search invocation uses provider `P`.
- No search invocation uses model `M`.

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No LLM search invocation matches model 'M'")`.

#### Provider and Model Exist Separately but the Pair Does Not

Condition:

- At least one search invocation uses provider `P`.
- At least one search invocation uses model `M`.
- No single search invocation satisfies both `provider == P` and `model == M`.

Example:

```toml
[[search_llm.providers]]
provider = "openai_main"
model = "gpt-5"

[[search_llm.providers]]
provider = "deepseek_main"
model = "deepseek-v3"
```

Request:

```text
llm-search "..." --provider openai_main --model deepseek-v3
```

Handling:

- Raise:
  `InputFailure(ErrorCode.BAD_REQUEST, "No LLM search invocation matches provider 'openai_main' with model 'deepseek-v3'")`.

Rationale:

- Both identifiers are valid independently, so “provider not found” or “model not found” would be diagnostically false.

#### Multiple LLM Invocations Match

Condition:

- Provider-only, model-only, or provider+model filtering selects multiple `LLMInvocation` values.

Handling:

- This is valid.
- Execute all matching invocations using the existing concurrent fan-out behavior.
- Do not raise ambiguity errors.

Examples:

- One provider alias with multiple models and `--provider P`.
- Multiple provider aliases sharing a model string and `--model M`.
- Duplicate search invocation entries with the same provider/model pair.

#### Selected LLM Invocations Fail

Condition:

- Selector validation succeeds and one or more invocations are selected, but every selected execution path fails.

Handling:

- Preserve existing scope-specific execution failures:
  - web branch: `ALL_PROVIDERS_FAILED` with existing LLM web-search failure wording;
  - paper branch: `ALL_PROVIDERS_FAILED` with existing LLM paper-search failure wording;
  - all scope: preserve existing all-branches failure behavior.
- Do not translate execution failures into selector `BAD_REQUEST` errors.

---

### 6. Protocol Boundary Failures

#### Optional Selector Field Has Wrong Type

Conditions include:

- `"provider": 123`
- `"model": []`
- `"provider": null` when the field is present rather than omitted, if the codec contract chooses omission as the sole representation of “not selected”.

Handling:

- `decode_request_frame()` returns the existing:
  `ErrorResponse(ErrorCode.BAD_REQUEST, "Request fields do not match schema")`.
- No daemon workflow begins.

#### Unknown Extra Fields

Condition:

- A search request contains fields beyond the accepted optional selector set.

Handling:

- Preserve exact-schema validation and return existing `BAD_REQUEST` response.

#### Backward-Compatible Frames Without Selectors

Condition:

- Existing clients encode keyword, paper, or LLM search requests without provider/model fields.

Handling:

- Decode as selector values `None`.
- Preserve current all-provider/all-invocation behavior.

#### Encoding Rules

- Omit optional selector keys when their value is `None`.
- Do not encode empty-string selectors as a representation of absence.
- Preserve current LLM `scope="web"` omission behavior unless implementation convenience strongly favors including it; this feature does not require changing that behavior.

---

### 7. Daemon Dispatch and Unexpected Failures

#### Typed Input Failure From Selection

Condition:

- An orchestrator raises `InputFailure(BAD_REQUEST, ...)` for an invalid selector.

Handling:

- Preserve the daemon's existing typed-`GatewayError` conversion into an `ErrorResponse`.
- CLI prints the message to stderr and exits with the existing error status.
- No traceback is returned to the CLI.

#### Unexpected Selection Bug

Examples:

- Internal selector helper raises `KeyError`, `AssertionError`, or another unexpected exception.

Handling:

- Preserve daemon unexpected-workflow handling.
- Client receives the existing generic internal/protocol error response.
- DEBUG mode may record the traceback under existing sanitization rules.
- Do not expose query/prompt contents or credential values in newly introduced error paths.

#### Cancellation

Condition:

- Request cancellation occurs before or during selected provider execution.

Handling:

- Preserve `asyncio.CancelledError` propagation and daemon shutdown behavior.
- Never convert cancellation into selector `BAD_REQUEST` or `ALL_PROVIDERS_FAILED`.

---

### 8. Result-File and Side-Effect Rules

Selector-validation failures occur before provider execution and therefore:

- create no result file;
- make no remote provider request;
- do not admit URLs into `URLStore`;
- do not mutate paper aggregation state beyond ordinary immutable/local selection computation.

After selector validation succeeds:

- result-file semantics remain unchanged;
- a successful empty result may still create an empty JSONL file according to existing behavior;
- partial provider success remains allowed where the existing orchestrator already allows it;
- result schemas and stdout success contracts do not change.

---

### 9. Error Matrix

| Command / Condition | Error Code | Message / Existing Behavior | Provider Work Starts? |
|---|---|---|---|
| Any supported command, explicit blank `--provider` | `BAD_REQUEST` | `Provider must not be empty` | No |
| `llm-search`, explicit blank `--model` | `BAD_REQUEST` | `Model must not be empty` | No |
| Keyword runtime has zero providers | `NO_KEYWORD_SEARCH_PROVIDERS` | Existing message | No |
| Keyword provider selector unknown | `BAD_REQUEST` | No enabled keyword-search provider matches selector | No |
| Valid selected keyword provider(s) all fail | `ALL_PROVIDERS_FAILED` | Existing message | Yes |
| Paper runtime has zero providers | `NO_ACADEMIC_SEARCH_PROVIDERS` | Existing message | No |
| Paper provider selector unknown | `BAD_REQUEST` | No enabled paper-search provider matches selector | No |
| Valid selected paper provider(s) all fail | `ALL_PROVIDERS_FAILED` | Existing message | Yes |
| LLM runtime has zero search invocations | `NO_LLM_SEARCH_PROVIDERS` | Existing message | No |
| LLM provider-only unknown | `BAD_REQUEST` | Provider-specific not-found message | No |
| LLM model-only unknown | `BAD_REQUEST` | Model-specific not-found message | No |
| LLM both unknown | `BAD_REQUEST` | Provider-or-model not-found message | No |
| LLM provider unknown, model known | `BAD_REQUEST` | Provider-specific not-found message | No |
| LLM provider known, model unknown | `BAD_REQUEST` | Model-specific not-found message | No |
| LLM provider/model known separately, pair absent | `BAD_REQUEST` | Pair-specific mismatch message | No |
| Valid selected LLM invocation(s) all fail | existing `ALL_PROVIDERS_FAILED` paths | Existing scope-specific message | Yes |

---

### 10. Compatibility Constraints

- Do not change the meaning of existing error codes.
- Do not add selector text to successful stdout.
- Do not change result schemas.
- Do not require existing clients to send selector fields.
- Do not change `url-fetch` error behavior.
- Do not make LLM selector validity depend on `fetch_llm` configuration.
- Do not make `--provider` mean LLM transport protocol (for example, `openai`); it always refers to the configured search provider alias.
