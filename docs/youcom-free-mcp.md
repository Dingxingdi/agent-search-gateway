# You.com free MCP Search and Extract

Use the existing `youcom` Web provider with You.com's keyless public MCP endpoint.
Search and Fetch remain independent capabilities of the same provider.

## Replace the existing You.com table

The [main example configuration](../config.example.toml) retains its original
disabled keyed REST table. Choose either that REST configuration or this free
MCP configuration. In your existing configuration, replace the entire
`[web_providers.youcom]` table with:

```toml
[web_providers.youcom]
enable_search = true
enable_fetch = true
api_url = "https://api.you.com/mcp/free"
```

The same copyable table is available in
[config.youcom-free.fragment.toml](../config.youcom-free.fragment.toml). It is a
replacement fragment, not a complete gateway configuration. Do not append it
alongside an existing table with the same name. Set either capability to
`false` if you only need Search or Fetch.

Delete the `api_key_env` line when switching to free MCP. An existing
`YDC_API_KEY` environment variable may remain: the gateway does not automatically
use it when `api_key_env` is omitted. Explicit credentials configured on this
free endpoint are rejected during configuration resolution, before any HTTP
request.

Use the exact complete URL `https://api.you.com/mcp/free`. The adapter sends MCP
tool calls to that endpoint without appending REST Search or Fetch paths.
Search calls `you-search`; Fetch calls `you-extract` with only the target URL.
The provider name remains `youcom`. No mode option, SDK installation, or MCP
service deployment is needed.

After updating an existing configuration, restart the gateway. Restarting clears
in-memory URL admission and prepared content, so search again before using
`url-fetch`.

## Existing REST behavior and quota

The original REST configuration remains available and unchanged. It uses
`https://ydc-index.io` as an API root and references the daemon's `YDC_API_KEY`
through `api_key_env`. The adapter appends the REST paths; Fetch uses the
separate `/v1/contents` API with separate billing and the appropriate Contents
permission. Existing custom REST proxy roots remain supported.

As of 2026-11-21,
[You.com's MCP documentation](https://you.com/docs/agents/mcp-server) publishes
a free quota of 100 queries per day. This is an external provider quota and may
change. Local `rate_limits` control attempts within this runtime; they are not a
remaining-balance meter or a daily quota ledger shared across processes. The
10 QPS policy in the existing REST example is not an official free MCP limit.
Choose a local rate policy suitable for your usage rather than treating the
public daily allowance as a tracked gateway balance.

Free MCP uses the existing shared HTTP executor, retry policy, concurrency quota,
and local rate gate for the `youcom` provider. Failures follow the existing
Search fan-out and Fetch fallback behavior through other configured providers.
The gateway never automatically switches a free MCP failure to paid You.com REST.

Redirects are never followed. With the locked HTTPX 0.28.1, an invalid `Location`
can fail before the original redirect status is exposed. That case follows the
configured transport-error retry policy and may be reported as a transport failure
instead of the redirect's HTTP status. The
[review discussion](https://github.com/Dingxingdi/agent-search-gateway/pull/106#discussion_r4657471106)
records the upstream interface limitation and its deferral.

## Opt-in keyless live check

Default tests remain offline. To opt in to only the free MCP check:

```bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k youcom_mcp -v -rs -rP
```

This case does not require or use `YDC_API_KEY`. It sends at most one Search
request and, only after usable Search hits, one Extract request for the first
hit. Each request has a 30-second timeout and no retries. A Search failure reports
Extract as SKIP; connection, quota, protocol, tool, and content failures report
FAIL with a fixed safe category. Stage output includes PASS/FAIL/SKIP, request
counts, elapsed time, and hit or content counts without printing queries, URLs,
page text, or credentials. The case is skipped unless
`WEB_SEARCH_RUN_INTEGRATION=1` is explicitly set.

Use the precise `-k youcom_mcp` selector. The broader `-k youcom` also selects
the original keyed REST tests. To run only those REST checks, use:

```bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k "youcom and not youcom_mcp" -v -rs -rP
```

The original REST checks still require `YDC_API_KEY`; their existing missing-key
SKIP and bounded retry behavior remain unchanged. Live checks test connectivity
and basic response contracts, not general provider availability. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for the normal offline checks.
