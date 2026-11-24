# Baidu Qianfan search fixtures

The Web and Agentic success fixtures use synthesized URLs, titles, snippets and page text. The table identifies which response shapes come from published examples and which were observed in a smoke test. They contain no captured responses or credentials. The empty and card-only fixtures exercise the gateway's zero-hit contract using synthesized responses.

| File | Documented fields and meaning | Official source |
| --- | --- | --- |
| `web_success.json` | A web reference with a URL, title, snippet and short content excerpt. | [Web Search API](https://cloud.baidu.com/doc/qianfan-api/s/Jmwbl4qxs) |
| `agentic_success.json` | A web page with original content. The success example uses null for both empty media lists. | [Agentic Search API](https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk) |
| `web_multimodal.json` | Official-derived web, image and video references. Top-level URLs take priority over type-specific `image.url` / `video.url` fallbacks; URL-only media carries no fabricated page text. All values are synthesized. | [Web Search API](https://cloud.baidu.com/doc/qianfan-api/s/Jmwbl4qxs) |
| `web_empty.json` | Synthesized `references=[]` result for the approved zero-hit contract. | [Web Search API](https://cloud.baidu.com/doc/qianfan-api/s/Jmwbl4qxs) |
| `agentic_multimodal.json` | Official-derived result containers with synthesized web page text, URL-only media and a card. Only the page carries original text. | [Agentic Search API](https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk) |
| `agentic_null_cards.json` | Synthetic page with explicit null image, video and card lists. The card-list null reproduces the shape observed in a successful Agentic response on 2026-11-21. All page values are invented. | Gateway smoke observation (2026-11-21). The [Agentic Search API example](https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk) shows a card array. |
| `agentic_empty.json` | Synthesized empty web, image and video arrays. Each array contributes zero candidates. | [Agentic Search API](https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk) |
| `agentic_card_only.json` | Synthesized card-only success. The example card type and nested URL exercise the rule that card contents never enter candidate counts or hits. | [Agentic Search API](https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk) |
| `business_errors.json` | Synthesized error fragments. Named and numeric examples follow the documented top-level code shape. Unknown, zero and malformed values exercise error handling. The tests combine the fragments with successful result data to verify error precedence. | [Search error codes](https://cloud.baidu.com/doc/qianfan-api/s/bmwbqlbad), [Web Search API](https://cloud.baidu.com/doc/qianfan-api/s/Jmwbl4qxs) |
