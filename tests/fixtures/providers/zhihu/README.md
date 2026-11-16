# Zhihu search fixtures

Checked: 2026-11-16.

Official sources:
- https://developer.zhihu.com/docs?key=zhihu_search
- https://developer.zhihu.com/docs?key=global_search
- https://developer.zhihu.com/ — Bearer authentication documentation.
- https://developer.zhihu.com/console/api/v3/docs — public first-party document
  feed used by the website. Its independent entries keyed zhihu_search and
  global_search supply the response tables and examples below.

The rendered search pages were unreadable through web lookup, so the website's
public document feed was retrieved directly without authentication, following
the feed reference in its public JavaScript. No authenticated search requests
were made. All fixtures are **doc-synthesized**, not sanitized observed responses.
Text, identifiers, dates, counts, scores and example.com/example.org URLs are
synthetic. No real account/author, key, request identifier value or raw search
response is retained.

## Independently verified contracts

| Contract | zhihu_search | global_search |
| --- | --- | --- |
| Method/path | GET /api/v1/content/zhihu_search | GET /api/v1/content/global_search |
| Query parameter | Query: string | Query: string |
| Success code in endpoint example | Code: 0, JSON number (integer) | Code: 0, JSON number (integer) |
| Success message | Message: "success" | Message: "success" |
| Results | Data.Items: array of objects | Data.Items: array of objects |
| URL | Url: string | Url: string |
| Title | Title: string | Title: string |
| Summary | ContentText: string | ContentText: string |
| Data siblings | HasMore: boolean; SearchHashId: string; optional EmptyReason: string | HasMore: boolean |

Both endpoints document Authorization: Bearer <Access Secret>,
X-Request-Timestamp in Unix seconds, and Content-Type: application/json.
A timestamp is generated per branch request; no headers are stored here.
Success Code was independently checked in both search examples, never inferred
from quota documentation. String "0" and boolean false are not the documented
numeric success value.

Map exactly Url, Title and ContentText to hit url, title and snippet; leave
raw_content and content empty. Url is not URL or url. Global ContentText may
contain em highlight tags; the fixture retains that documented string form.
No speculative aliases are represented.

## Construction and evidence limits

- zhihu_search.json retains HasMore, SearchHashId, item metadata, an empty
  CommentInfoList and numeric RankingScore. CreateTime comes from the required
  response table even though the prose example omits it. Optional Headline is
  absent.
- global_search.json follows its own table/example and includes CommentInfoList
  objects with Content. Site-only SearchHashId and RankingScore are not added.
- Both *_empty.json files are **schema-derived empty cases**, obtained by
  setting the documented Items array to [] and HasMore to false. They were not
  observed live. The site's optional EmptyReason uses synthetic explanatory
  text.
- Site docs list numeric codes 0 (success), 10001 (parameter error), 20001
  (authentication), 30001 (rate limit), and 90001 (internal error). The global
  document independently demonstrates numeric success 0 but has no error-code
  table or business-error response example. Constructed nonzero-Code negative
  tests must not be called captured global errors; the site-only code catalogue
  is not evidence of a global catalogue.
- The published global example includes unescaped prose quotes and trailing
  commas. These fixtures are valid JSON synthesized from the field tables and
  intended example structure, not byte copies.
