## Testing: Gateway Maintenance and Bright Data Header-Aware Retry

### 1. Test Strategy

Verify this as a narrow **header-aware retry enhancement** plus three independent documentation/CLI/example changes. Existing interfaces, provider registries, transport retry behavior, and Web Unlocker request behavior must remain stable.

Goals:

- Prove that HTTP 200 can still be an actionable Bright Data SERP failure when `x-brd-*` headers report a vendor error.
- Prove the four documented recoverable codes retry within the **same** `RetryPolicy.max_attempts`, with bounded optional jitter and accurate logs.
- Prove all other codes and provider behaviors remain unchanged.
- Prove Bright Data successful `organic` extraction and Web Unlocker Fetch output remain intact.
- Prove top-level version flags are independent of daemon availability and configuration.
- Prove the example TOML omits only four requested example blocks, sets its timeout to 180.0, and does **not** remove implementation support for those provider types.
- Verify README dblp warning and version command examples.
- Keep normal tests offline, deterministic, credential-free, and fast. Live smoke tests are optional and must be explicitly enabled.

The principal proof points are `tests/providers/test_http_executor.py`, `tests/providers/web/test_brightdata.py`, and `tests/unit/test_retry.py`, reusing pytest, `httpx.MockTransport`, the existing fake sleeper/clock/logging helpers, and provider recording fakes.

---

### 2. Test Layers and Locations

| Test layer | Responsibility | Location | Real network? |
|---|---|---|---|
| Retry engine unit | Default exact backoff, optional jitter, cap, hooks, cancellation | `tests/unit/test_retry.py` | No |
| HTTP executor | Call optional response inspector within one retry loop; preserve generic/default path | `tests/providers/test_http_executor.py` | No (MockTransport) |
| Bright Data adapter | Header classification, unchanged outgoing Search payload and Fetch payload, normal `organic` mapping | `tests/providers/web/test_brightdata.py` | No |
| Adapter + HTTP integration | End-to-end header classification → shared retry → logging → JSON decode | `tests/providers/web/test_brightdata.py` or a focused HTTP executor test | No (MockTransport) |
| CLI | Global `-v` / `--version`, no daemon/config access, unaffected subcommand parsing | `tests/cli/test_cli.py` | No |
| Example config + README | Example blocks, timeout, registry continuity, docs commands, warning | `tests/docs/test_documented_config.py` | No |
| Runtime/orchestration regression | No changes to quota ownership, provider registration, mixed success aggregation | Existing `tests/runtime/` / `tests/orchestrators/` suites | No |
| Optional live smoke | Observe one real Bright Data keyword search and DEBUG events | Manual command or explicit opt-in integration | Yes, opt-in only |

If new optional request arguments require a change to `tests/support/http.py`'s recording executor, update that fake minimally so it accepts the new contract; do not replace the test infrastructure.

---

### 3. Bright Data Header Classification Matrix

Use `httpx.MockTransport` to return deterministic responses with varying outer status, response headers, and bodies. Confirm adapter-level classification runs **before** JSON decoding:

| Case | HTTP / headers / body | Expected behavior |
|---|---|---|
| Masked recoverable | 200; `x-brd-error-code=captcha`, `x-brd-error=redirect location was rejected`, `x-brd-status-code=502`; empty body | Retry or eventually raise typed vendor failure; **never** misleading JSON decode |
| Additional recoverable | `timeout`, `expect_element`, `expect_body` | Same retry eligibility |
| Recovery | First two attempts 200 + `captcha`, third 200 + valid JSON `organic` | Three sends total, expected `KeywordSearchHit`, two wait events |
| Terminal unknown | 200 + `x-brd-error-code=other_code` | Single attempt, no retry |
| Terminal auth | 401/403 or explicit nonrecoverable auth error | Single attempt, existing safe error semantics |
| Provider status only | 200 + `x-brd-status-code=502`, no error code, empty body | Vendor failure recognized; no guessed retry category |
| Benign status | 200 + `x-brd-status-code=200`, valid `organic` JSON | Normal success |
| Generic status | 500 then 200, no vendor headers | Existing exponential HTTP retry and success |
| Decode failure | 200, no vendor error header, empty or invalid JSON | Existing `ProtocolFailure`, no new vendor retry |
| Malformed header | Invalid `x-brd-status-code` or oversized/unsafe error text | Safe terminal/fallback classification; no leakage |
| Duplicate/ambiguous header | Mixed harmless and explicit error metadata | Explicit error wins; do not parse body as success |

Check that `organic` URL/title/description mappings match the existing unit tests. Keep assertions on Search's outgoing request unchanged: `POST /request`, Bearer auth, current `search_zone`, URL with `q` and `brd_json=1`, and `format="raw"` **without** `data_format="parsed"`.

---

### 4. Retry Count and Delay Tests

#### Single attempt budget

Use a deterministic transport sequence:

```text
attempt 1: HTTP 200 / x-brd-error-code=captcha / body empty
attempt 2: HTTP 500 / no vendor header
attempt 3: HTTP 200 / x-brd-error-code=captcha / body empty
```

Assert:

- Exactly **three** calls with `max_attempts=3`, not nine and not four.
- Both vendor-header and HTTP-status failures are handled by the same retry loop.
- The last error's safe provider classification is retained in the terminal DEBUG event.
- The existing orchestrator still returns its generic all-providers-failed error when no provider succeeds.

Also exercise `max_attempts=1`: no waiting and only one HTTP call.

#### Jitter behavior

Inject a deterministic jitter source, fake `sleep`, and capture `on_retry`:

- For provider-header errors, delay equals capped exponential backoff multiplied by a test-controlled factor within [0.8, 1.0].
- Each logged `delay_ms` corresponds to the *same* delay sent to fake sleep (accounting for millisecond rounding).
- The wait is finite, nonnegative, and at most `max_delay_seconds`.
- The optional delay transform is absent by default; the existing `tests/unit/test_retry.py` assertions `[0.25, 0.5]` remain **exactly** true.
- Normal `HttpJsonExecutor` status/transport retries (with no Bright Data inspector) retain exact old delay values.
- A mixture of errors does not reset the attempt counter.
- Cancellation still propagates immediately and is not wrapped or retried.

#### Boundary regressions

Use the current HTTP executor tests as the baseline for:

- Retryable 408/429/5xx status behavior;
- Terminal 4xx behavior;
- HTTP transport/timeout error behavior;
- Hook exception isolation and request timeout;
- No unexpected new HTTP request when the old behavior would stop.

---

### 5. Logging and Sensitive Data Tests

Use the existing `KeyValueFormatter` and `SecretRedactor` test helpers. Inject identifiable sentinels into:

- Bearer API key;
- Query string and target URL path;
- Provider response body;
- `x-brd-error` explanation including a URL, token-like substring, and control characters.

Assert that no sentinel appears in DEBUG logs or rendered exception messages. Structured safe metadata **must** be present:

- `provider=brightdata` and `stage=search`;
- `attempt`, outer `status=200` and `brightdata_status=502`;
- safe `brightdata_error_code=captcha`;
- retry decision and actual `delay_ms`;
- terminal failure category/reason without raw header/body content.

Verify standard HTTP executor logs continue to omit request-specific URL queries and credential material, and non-Bright-Data events do not accidentally gain vendor-header details.

---

### 6. Web Unlocker and Other-Provider Regression

#### Bright Data Fetch

Retain existing `tests/providers/web/test_brightdata.py` assertions that `fetch()` uses:

```json
{
  "zone": "<fetch_zone>",
  "url": "https://example.com/article",
  "format": "raw",
  "data_format": "markdown"
}
```

Confirm normal text/Markdown return and existing empty-body handling remain unchanged. Because Fetch uses `request_text()` without the optional Search inspector, it must not gain special SERP-header retry behavior.

#### Other web and LLM providers

Run existing HTTP executor and provider tests to confirm omitted optional hooks use the exact old behavior. Do not change the default retry policy, provider registrations, transport client factory, provider quotas, or search and fetch result schemas.

#### Search orchestration

Assert that a failed Bright Data branch does not prevent successful sibling providers from producing results, and all-failed retains the documented aggregate error. No new search output keys or specific vendor error text in public stdout/stderr.

---

### 7. CLI Version Tests

Extend `tests/cli/test_cli.py` using `build_parser()` or `main(argv)` with stdout capture:

- `agent-search-gateway --version` prints `agent-search-gateway 0.2.0` and exits 0.
- `agent-search-gateway -v` prints the same line and exits 0.
- Both work with no command, daemon, user config, or socket. If needed, monkeypatch runtime-path/daemon calls to fail if invoked.
- Output matches package `agent_search_gateway.__version__` rather than a separately hardcoded test version.
- Existing command grammar, error codes, parser help, and business stdout remain unchanged when neither version flag is present.
- Do not add or claim subcommand-local version flags.

---

### 8. Documentation and Config Tests

Extend `tests/docs/test_documented_config.py` or a focused neighboring docs test:

1. Parse `config.example.toml` as TOML and assert that example blocks for `zenrows`, `decodo`, `scrapingdog`, and `scrapegraphai` are absent.
2. Assert remaining intended provider examples still resolve correctly, including Bright Data's independent `search_zone`/`fetch_zone` and existing Search/Fetch enable flags.
3. Assert `[retry].request_timeout_seconds == 180.0` **in the example**, while `resolve_retry_policy({})` still yields its existing 30.0-second default.
4. Assert the default provider registry **still supports** all four omitted provider types. Adjust the current docs test that incorrectly treats all supported provider names as mandatory example stanzas; separate `registered_providers` and `configured_example_providers` expectations.
5. Verify README documents `--version` and `-v` and warns that dblp may trigger bot verification even under low request frequency; mention optional `enabled=false` as an operator mitigation.
6. Verify docs command examples still parse with the current CLI grammar and provider capability table remains truthful.

These tests must not modify installed user configurations or remove any adapter.

---

### 9. Verification Commands and Acceptance Gate

Normal offline verification after implementation:

```bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run pytest -v
uv run python scripts/build_docs.py
```

Relevant fast feedback during implementation:

```bash
uv run pytest -q tests/unit/test_retry.py tests/providers/test_http_executor.py tests/providers/web/test_brightdata.py tests/cli/test_cli.py tests/docs/test_documented_config.py
```

Optionally, in a configured local environment with a running daemon, use `agent-search-gateway keyword-search "..." --provider brightdata` and DEBUG tracing to inspect the new safe events. This live smoke is opt-in, can consume provider calls, and is not required in ordinary CI. An intermittent real CAPTCHA is not proof of a broken implementation if the retry/logging behavior meets contract.

#### Final pass conditions

- All four original user requests are covered.
- Captcha-like HTTP-200 header errors are correctly diagnosed and retried without double loops.
- Both success-after-retry and terminal-after-exhaustion paths preserve clear internal diagnostics.
- Jitter is bounded and opt-in; existing generic retry defaults and non-Bright-Data tests remain stable.
- No secrets or raw vendor payloads appear in DEBUG logs.
- Bright Data Fetch, CLI public errors, search results, socket protocol, and all provider registrations are unchanged.
