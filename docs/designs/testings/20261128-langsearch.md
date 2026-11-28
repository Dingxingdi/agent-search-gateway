# Testing: LangSearch Web Search Provider

- Date: 2026-11-28
- Status: Implemented; see the [implementation and verification record](../plans/20261128-langsearch.md).
- Base: v3.1.0 / 5f70c12
- Architecture: ../architectures/20261128-langsearch.md
- Error handling: ../error-handlings/20261128-langsearch.md

## 1. Strategy

Follow existing pytest, HTTPX MockTransport, fixtures and fake/recording requester conventions. Prefer focused provider tests and extensions to registry/runtime/config/documentation tests over duplicating generic tests. Offline CI must not require real credentials or network access. Use synthetic fixtures; actual upstream traffic is limited to explicit live smoke testing.

## 2. Adapter tests

Add tests/providers/web/test_langsearch.py; use tests/fixtures/providers/langsearch/ where JSON fixtures help. Follow AnySearch and Brave test conventions.

| ID | Case | Expected |
|---|---|---|
| A1 | Normal search including Unicode/punctuation | One successful POST attempt, configured base plus /v1/web-search, Bearer authentication, query preserved, count 10, stage search |
| A2 | Base URL with trailing slash or compatible prefix | Existing endpoint helper joins correctly |
| A3 | Valid integer code 200 and multiple results | Ordered url/name/snippet mapped to URL/title/snippet; bodies empty |
| A4 | Missing/null optional name or snippet | Empty field; no invented metadata |
| A5 | Unexpected text, content, dates, usage or trace identifier | No mapping into body fields or new public fields |
| A6 | Valid empty array | Successful [] |
| A7 | Mixed malformed records, missing/blank/non-string URLs, wrong metadata types | Retain valid records in order and correct candidate counts/reasons |
| A8 | All malformed records in valid result array | Successful []; correct rejection counts |
| A9 | Non-object root, missing code, bool/string success code, non-success code | Existing ExecutionFailure |
| A10 | Missing/null/wrong-type data, webPages or value | ExecutionFailure, not false empty success |
| A11 | String/numeric error codes with message or msg | Selected safe diagnostics; no arbitrary fields or credential disclosure |
| A12 | Invalid JSON / HTTP errors | Existing executor behavior via existing mock transport or focused integration |
| A13 | Diagnostics | Decode, invalid envelope, provider response failure and success classified consistently; no effect on result |

A1 asserts that JSON contains only query and count: no contents, freshness, includeDomains or excludeDomains. Body fields must remain empty even when upstream returns full text. Do not expect the adapter itself to normalize or deduplicate URLs.

## 3. Registry, configuration and runtime

Extend existing relevant tests; exact placement follows current suite conventions.

- Registry: append exactly one registration named langsearch; existing registration order preserved; Search true, Fetch false, api_url as the only vendor option and required credential.
- Configuration / doctor: new example disabled by default. Enabling with a valid configured environment reference resolves normally; missing credentials, unsupported Fetch or unknown vendor options are rejected through existing validation. No LangSearch online doctor check.
- Runtime: existing generic factory constructs a credential-specific adapter/executor; multiple credentials produce one logical Search provider, not duplicate registrations. No LangSearch Fetch candidate.
- Focused pool regression: valid empty/partial responses do not dispatch another member; terminal provider failure follows existing fallback. Reuse existing pool test harness, not a new retry matrix.
- Search / URL admission: a selected hit with valid URL and useful metadata follows ordinary search staging/admission, with no body. A later explicit Fetch uses an existing Fetch-capable provider, never LangSearch. Reuse existing acceptance/orchestrator support rather than separate CLI/MCP protocol fixtures.
- Documentation/config contract: update affected exact provider count and capability assertions. Example uses LANGSEARCH_API_KEY as the credential environment variable name, 5 requests/1 second per member, no hidden concurrency cap, and explains shared upstream account limits. Update relevant provider lists and rate-reference date/count.

## 4. Existing infrastructure regression

Run existing tests covering HTTP executor, retries, rate gate, credential pool, runtime, URL normalization/admission/fetch state, observability/redaction, CLI and MCP. Their generic tests already cover selection algorithms, rate-per-attempt, concurrency, cancellation, proxy/TLS and body previews. Do not replicate those matrices or change the core contracts.

Run the full offline suite and existing lint/format/type/quality and documentation checks according to current repository CI. Check that the production diff is limited to the added adapter and registry entry, without dependency or lockfile changes.

## 5. Opt-in live smoke

Use the existing live-test convention or a focused LangSearch equivalent. Check whether the configured environment variable LANGSEARCH_API_KEY is available without displaying its value. If absent, explicitly skip or report the limitation. Use a small ordinary non-sensitive query and the fixed count 10; no full text. Real search must not fan out to other providers.

For end-to-end verification, use isolated test config, daemon and runtime paths, or another existing safe test harness that does not disrupt running daemons or user config. Enable LangSearch in that test config with its required credential environment reference. Use the existing startup and keyword-search interface with --provider langsearch.

Confirm the actual successful upstream invocation and at least one plausible URL-bearing hit for a suitable query, mapped title/snippet and normal admission/result behavior. A valid empty response is a valid API outcome but not positive retrieval validation; if appropriate, try a suitable alternative query within a bounded smoke run. Do not invoke other live providers just to test LangSearch or attempt LangSearch URL Fetch.

Do not disclose credentials, sensitive query data or quota dashboard secrets. Missing credentials, network outage, rate/allowance exhaustion or unexpectedly empty search must be reported as live-smoke limitations, not fabricated passes.

## 6. Acceptance criteria

- Focused provider tests, full offline suite and existing quality checks pass.
- LangSearch-only live smoke passes when credentials and upstream are available; otherwise report the precise blocker and offline verification.
- Production changes are limited to langsearch.py and the registry import/entry; no other provider, dependency, public interface or core runtime changes.
- Example config and docs reflect opt-in Search-only behavior, required credential and example 5 RPS member window; distinguish account-wide token allowance from local rate gates.
- LangSearch requests no full text and admits no body; URL Fetch remains explicit and only uses Fetch-capable providers.
