## Testing: Retry-After, Zhihu and Yuanbao Keyword Providers

**Status:** Draft for user review; no implementation  
**Date:** 2026-11-16  
**Base:** eeeff82 (v0.2.2)  
**Companion specs:** [Architecture](../architectures/20261116-zhihu-yuanbao-retry-after.md) | [Error handling](../error-handlings/20261116-zhihu-yuanbao-retry-after.md)

### 1. Test strategy and constraints

The project already has isolated HTTP executor tests, per-adapter tests, registry/config tests and end-to-end workflow tests. Reuse those seams. This feature's primary proof must be deterministic, **without** live API calls: `httpx.MockTransport` for the real `HttpJsonExecutor`, recording fakes for adapter-only mapping where appropriate, and injected sleep/clock functions for Retry-After waits.

No new testing infrastructure, runtime dependency, request-harness wrapper or live-service gate is needed. Avoid modifying existing test fakes merely to mirror the entire new vendor response schema. Keep all test credentials synthetic and test that they never appear in rendered logs.

### 2. Retry-After test matrix

Primary location: extend `tests/providers/test_http_executor.py` and existing `retry_async` tests; use the real shared executor with `httpx.MockTransport`, a fake sleep collector and, for HTTP dates, an injectable/fixed clock in the parsing helper.

| ID | Setup | Assertions |
| --- | --- | --- |
| RT-01 | 429 → 200, `Retry-After: 5` | Exactly one 5s fake sleep; successful result; one `http_retrying` and no terminal failure |
| RT-02 | 503 → 200, valid HTTP-date | Correct nonnegative delta after parsing date/time zone |
| RT-03 | 429 with no header | Existing capped exponential backoff, same retry counts |
| RT-04 | 429 with malformed/negative/non-finite header | No parse exception; normal fallback delay |
| RT-05 | 429 with valid header longer than configured `max_delay_seconds` | Chosen server wait is **not** clipped |
| RT-06 | 429 with zero or past-date header | Valid zero delay, no negative sleep |
| RT-07 | Repeated 429 up to `max_attempts` | Expected number of HTTP attempts; no sleep after terminal attempt; final HTTP failure |
| RT-08 | Nonretryable HTTP 401/403 with Retry-After header | No retry, no header-driven sleep |
| RT-09 | 200 malformed JSON / invalid Atom XML | Existing parse/protocol failures; no new header retry |
| RT-10 | Transport timeout and provider-header response inspector | Existing behavior and jitter preserved |
| RT-11 | Cancelled in attempt or during wait | Cancellation propagates; no trailing requests |
| RT-12 | DEBUG event capture | Actual `delay_ms` and source fields; intermediate failure never mislabeled `provider_failed` |

Add **one** integration case to each `tests/providers/academic/test_semantic_scholar.py` and `tests/providers/academic/test_arxiv.py` showing 429 + valid header followed by valid JSON or Atom response. These verify that both existing parsers already pass through the modified HTTP executor without changing adapter code.

### 3. Zhihu adapter test matrix

New file: `tests/providers/web/test_zhihu.py`. Prefer a real `HttpJsonExecutor` + MockTransport for request/response assertions and a small controlled recording requester for independent branch failures.

| ID | Fixture / condition | Assertions |
| --- | --- | --- |
| ZH-01 | Both official endpoints return valid result lists | Both GET requests made with exact original query; merged `KeywordSearchHit` results |
| ZH-02 | Request inspection | Bearer credential and fresh Unix-second `X-Request-Timestamp` in each request; no secret in logs |
| ZH-03 | Site branch success, global branch HTTP/transport failure | Site hits preserved; one `provider_partial_failure` with fixed global branch |
| ZH-04 | Global branch success, site branch response/protocol failure | Global hits preserved; corresponding partial-failure event |
| ZH-05 | Both branches fail | Existing typed adapter failure; pipeline emits one terminal `provider_failed` |
| ZH-06 | Both return valid empty results | Return `[]`; no false failure event |
| ZH-07 | Non-success provider `Code` inside HTTP 200 | Treat branch as upstream error with bounded code/message, never as empty success |
| ZH-08 | Invalid envelope / missing result list | Branch protocol failure, not silently empty |
| ZH-09 | Mixed good and bad individual hits | Valid siblings retained; rejection counts/known reasons correct |
| ZH-10 | Cancellation during either API call | Cancellation propagates, not downgraded to partial success |
| ZH-11 | Keyword CLI selected-provider integration | `--provider zhihu` selects exactly one logical provider; two internal API requests only |
| ZH-12 | Same URL returned by both branches | Existing SearchOrchestrator URL dedup produces one output URL, with no duplicate adapter logic |

Record sanitized fixtures based on the actual documented/current Zhihu search schema. Verify exact URL/title/summary field spellings before implementation acceptance; do not make speculative fallback aliases a hidden requirement.

### 4. Yuanbao adapter test matrix

New file: `tests/providers/web/test_yuanbao.py`. Ground happy-path examples in Tencent's official service API-key and SearchPro output documents.

| ID | Fixture / condition | Assertions |
| --- | --- | --- |
| YB-01 | Happy-path `Response.Pages` containing JSON strings | POST official `/SearchPro` with only the Query body and Bearer header; correct `KeywordSearchHit` list |
| YB-02 | Page provides `content` and `passage` | Nonempty `content` is snippet; page body fields remain empty |
| YB-03 | Page has only `passage` | Snippet falls back to `passage` |
| YB-04 | HTTP 200 with `Response.Error` | Typed provider failure; safe vendor code/message; no false zero-hit success |
| YB-05 | Invalid/missing `Response` or wrong-type `Pages` | Whole-adapter failure with existing envelope diagnostics |
| YB-06 | `Response.Pages=[]` | Valid successful empty result |
| YB-07 | Mixed valid, malformed JSON and non-object Pages entries | Return valid hits, skip bad entries with `decode_error` / `invalid_record_shape` counts |
| YB-08 | Entry missing URL / with invalid required shape | Skip only entry, record correct known rejection reason |
| YB-09 | HTTP 429/5xx then success | Shared retry used, no second adapter retry loop |
| YB-10 | Captured DEBUG output | No credentials, raw request body, raw response documents or full Pages entries |
| YB-11 | Stored search result followed by URL Fetch | Search snippet does not masquerade as stored `raw_content` or `content`; existing Fetch remains applicable |

### 5. Registration, config and orchestrator regressions

Extend existing focused suites, without changing production orchestration:

- `tests/providers/test_registry.py`: both entries registered, `search=True`, `fetch=False`, correct factory/options, no duplicate name or alteration of existing entries.
- Existing `tests/config` suites: default/explicit `api_url`, required env credential, unknown options rejected, Fetch capability rejected when requested, enabled/disabled behavior unchanged.
- `tests/orchestrators/test_keyword_search_pipeline.py`: new hits are normalized and deduped by existing SearchOrchestrator; provider results can succeed despite another provider failure; malformed items do not discard siblings.
- `tests/providers/web/test_brightdata.py`: all existing header-based HTTP-200 upstream error handling and search/fetch tests stay green; no new Bright Data behavior.
- `tests/providers/academic`: existing Semantic Scholar and arXiv parsing/normalization unaffected.
- `tests/docs/test_documented_config.py` and existing CLI/acceptance tests: added sample blocks are accepted, CLI `--provider` filtering and JSONL schemas unchanged.
- Existing observability tests: exact one logical terminal provider outcome; independent HTTP failures and partial failure are logged at appropriate scopes.

### 6. Test execution and verification sequence

1. **Baseline check:** run focused existing HTTP, registry, academic and Bright Data tests before implementing changes; record unrelated baseline failures rather than assuming them away.
2. **Red-first focused checks:** add new Retry-After tests; exercise both server header forms, invalid header fallback and max-delay separation. Then implement only the small shared HTTP/retry changes.
3. **Adapter tests:** add Zhihu and Yuanbao test fixtures; implement adapters independently. Validate vendor business errors and partial/invalid-record behavior.
4. **Compatibility:** run registry/config/orchestrator tests; assert no new dependencies, no new public result/config fields and no Fetch registration.
5. **Full local verification:** run project `pytest`, `ruff check` and strict `mypy` using existing dev tooling; test documented config/README and inspect representative DEBUG events for redaction, useful metadata and counts.
6. **Small real smoke tests:** if user-configured API environments are available, invoke precisely one selected provider at a time with `--provider`. Examples:
   ~~~text
   agent-search-gateway keyword-search "人工智能" --provider zhihu
   agent-search-gateway keyword-search "人工智能" --provider yuanbao
   agent-search-gateway paper-search "machine learning" --provider semantic_scholar
   agent-search-gateway paper-search "machine learning" --provider arxiv
   ~~~
   Each Zhihu smoke search intentionally makes **two** upstream requests. Do not default to every provider, loop on a paid API until a rare 429 appears, or assert live service availability as a deterministic CI requirement.
7. **Review/release gate:** after implementation passes review and smoke checks, use the existing release workflow to decide/bump the new version and publish; no release is part of this brainstorming spec.

### 7. Acceptance criteria

- [ ] Valid server Retry-After overrides backoff; ordinary configured backoff and attempt count remain unchanged.
- [ ] Both Academic HTTP JSON and Atom-text paths demonstrate the same retry behavior with no adapter-specific logic.
- [ ] Zhihu uses one provider identity, both official APIs, independent branch failure and accurate partial success logs.
- [ ] Yuanbao correctly uses service-key SearchPro and distinguishes `Response.Error` from a successful empty search.
- [ ] Both adapters emit existing `KeywordSearchHit` values, skip malformed individual records, record counts and do not prefill page bodies.
- [ ] Existing URL normalization, dedup, Fetch fallback, config validation, CLI filtering and other provider behavior are unchanged.
- [ ] Existing Bright Data tests remain green; README describes only the observed uncertain limitation.
- [ ] No secret or full response body appears in debug logs; all focused/full tests, lint and typecheck pass or baseline failures are explicitly identified.
- [ ] User approves design docs before they are committed; implementation is a later separate step.
