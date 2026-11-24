# You.com free MCP contract evidence

Verified on 2026-11-21 at exactly `https://api.you.com/mcp/free`.
Task 3 consumes controlled G0 observations supplied to implementation and makes
no additional live calls.

## Sources

- https://you.com/docs/agents/mcp-server
- https://you.com/docs/using-the-api/authentication
- https://modelcontextprotocol.io/specification/2026-07-28/basic/index
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning

Context7 resolution was unavailable. G0 used direct authoritative documentation
and a controlled probe. The older
https://about.you.com/resources/mcp-web-search describes a different
`/mcp?profile=free` address and does not establish this endpoint or Extract.

## G0 observations

Exactly four unauthenticated POST requests: server/discover, tools/list,
one Search, one Extract. Redirects were disabled; no credentials were accessed
or sent. All responses were HTTP 200 application/json, echoed their request ID,
and had no session header. Discovery supportedVersions was [2026-07-28].
Tools were you-search, you-extract, you-discover. The selected protocol is
stateless: no initialize, notification, session, version probing or recovery.

Search query is required string minLength 1. Optional count is integer 1..100,
default 30; count=10 succeeded. Extract url is required URI. The free service
restricts Extract to url-only despite extra schema fields.
No query/count/url x-mcp-header annotations were present.

## Requests

Headers: Content-Type application/json; Accept application/json, text/event-stream;
MCP-Protocol-Version 2026-07-28; Mcp-Method tools/call; Mcp-Name fixed tool name.
No API key, authorization or payment headers.

JSON-RPC 2.0 body: unique string id, method tools/call,
params containing name, arguments, and _meta.
params._meta contains io.modelcontextprotocol/protocolVersion=2026-07-28,
io.modelcontextprotocol/clientCapabilities={}, and
io.modelcontextprotocol/clientInfo={name: agent-search-gateway,
version: existing package __version__}. _meta is not a root field.
Search arguments are exactly {query: query, count: 10}.
Future Extract arguments are exactly {url: url}.

## Successful envelope and fixture provenance

Both calls returned {jsonrpc: "2.0", id: echoed-id, result: {_meta: META,
content: [{type: "text", text: JSON(PAYLOAD)}], structuredContent: PAYLOAD,
resultType: "complete"}}. isError was absent. Text JSON decoded to exactly the
same object as structuredContent in each real response.

mcp_search_structured.json and mcp_search_text.json are synthetic reductions of
the simultaneously observed structured and single text JSON representations.
They retain the relevant outer structure and ten-row shape, with shortened
one-entry highlight arrays. They are not raw recordings or wrapped REST fixtures.
All URLs, titles, excerpts, usage/server values and metadata are synthetic.
The metadata UUID is 00000000-0000-4000-8000-000000000001 and the query is neutral.
MockTransport loads a fresh fixture object for each call and copies the received
request id into that response; the stored placeholder is never the expected ID.

Search META contained you/usage with sku:string, results_returned:10,
extraction_mode:string, and io.modelcontextprotocol/serverInfo with
name/version strings. Extract usage contained sku, results_returned:1,
pages_extracted:1.

## Search payload and adapter mapping

PAYLOAD: {results: {web: [10 rows]}, metadata:
{search_uuid:string, query:string, latency:number}}.
Every observed row had url/title/description/page_age/favicon_url strings and
contents.highlights:string[]. All URLs/titles/highlights were nonempty.
Descriptions were empty in rows 1, 2, 3, 7, 9.
thumbnail_url appeared in rows 3, 5, 8, 10.
No snippets keys were observed, but the live official outputSchema declared
optional top-level snippets:string[] and description:string.

The adapter validates URLs with normalize_url, retaining the original string.
Title uses the REST optional string policy (missing/null becomes empty).
contents.highlights has priority whenever non-null, including an empty array;
otherwise use schema-declared top-level snippets. Missing/null snippets becomes
empty. The selected array must contain strings, joined with two newlines.
Malformed candidates follow existing CandidateCounts rejection behavior.
Description, contents.snippets, Markdown and guessed aliases are not snippet
sources. Search leaves all page body fields empty. results.web=[] is valid;
a missing/non-array root list fails.
Present invalid structuredContent cannot silently fall back to text JSON.

## Extract evidence reserved for Task 5

PAYLOAD: {output: {url?:string, extraction_source:string, title:string,
contents:{full_page:{markdown:string}},
metadata:{site_name:"", favicon_url:string}}}.
Observed url matched the public Search result selected for extraction and
Markdown was nonblank. The schema does not require url. Optional HTML is
schema-declared but was not observed by default. full_page.markdown is page
body, not snippets. Task 3 adds no Fetch implementation or Fetch fixture.

## Unsupported and unobserved behavior

No live SSE response was observed. Later SSE/error/empty fixtures are
protocol-derived synthetic cases. Natural-language text is not a payload.
OAuth/payment flows, arbitrary tools, legacy versions, guessed aliases and
automatic paid REST fallback are unsupported.
