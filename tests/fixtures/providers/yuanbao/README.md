# Yuanbao SearchPro fixtures

Checked: 2026-11-16.

Official sources:
- https://cloud.tencent.com/document/product/1806/130615 — service API-key transport and response example.
- https://cloud.tencent.com/document/product/1806/121811 — SearchPro fields.
- https://cloud.tencent.com/document/api/1806/121820 — Error envelope and codes.

Both fixtures are **doc-synthesized**, not live recordings. All content, dates,
scores and example.com/example.org URLs are synthetic. No key, account,
RequestId or raw service response is retained.

Use POST https://api.wsa.cloud.tencent.com/SearchPro, Bearer service API key,
and application/json; the adapter body contains only Query (string). This is
the service-key transport, not the standard signed endpoint in the field docs.

Response.Pages is an array of JSON-encoded strings, not result objects.
Response.Query is the original query. Decode each page, mapping url and title;
snippet prefers non-whitespace content (dynamic summary), otherwise passage
(standard summary). Neither is a fetched page body.

search.json preserves representative documented siblings and types. The first
page has both summaries; the second models documented fast-tier omission of
content, score and pics. These are constructed cases, not observed account/tier
behavior. Response.Pages=[] is a schema-derived empty success; no live empty
response was captured.

error.json combines the documented Response.Error shape and RequestLimitExceeded
code with a short synthetic Message. Code and Message are strings with exact
casing. Presence of Error denotes failure even alongside Pages. It must not
become empty success or be reclassified as transport HTTP 429. RequestId from
the documentation examples is deliberately omitted.
