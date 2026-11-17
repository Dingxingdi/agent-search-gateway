# Doubao、You.com、Keenable 与 open-webSearch Implementation Plan

**Goal:** 以四个独立 Web Provider 接入豆包、You.com、Keenable 和已有 open-webSearch HTTP 服务，使其各自支持关键词搜索和 URL 抓取；为 open-webSearch 增加可配置多引擎请求以扩大覆盖率，复用现有配置、重试、配额、诊断与编排流程。

**Architecture:** [docs/designs/architectures/20261117-doubao-youcom-keenable-openwebsearch.md](../architectures/20261117-doubao-youcom-keenable-openwebsearch.md)

**Error handling:** [docs/designs/error-handlings/20261117-doubao-youcom-keenable-openwebsearch.md](../error-handlings/20261117-doubao-youcom-keenable-openwebsearch.md)

**Testing:** [docs/designs/testings/20261117-doubao-youcom-keenable-openwebsearch.md](../testings/20261117-doubao-youcom-keenable-openwebsearch.md)

---

## 0. 执行上下文与已确认的边界

- 工作目录：`/home/agent/agent-search-gateway/.worktrees/add-doubao-youcom-keenable-openwebsearch-20261117`；沿用此工作树，不另建、不切换其他分支。
- 计划基线：`4f606d2`（设计文档提交），代码基线为 `eeeff82`（v0.2.2）。以下位置以该基线为准，实施时按符号定位，不能依赖行号永远不变。
- 本文是待实施计划，所有实施复选框保持未勾选；本文编写期间未运行供应商 API，也未把合成 fixture 当作真实请求记录。
- 2026-11-17 修订：用户确认方案 A，允许在 Gateway 的 open_websearch Provider 配置中声明 `engines`；原先一律省略 engines 的要求由本版替代。三份 spec 已同步，其他三家范围不变。
- 只新增 `doubao`、`youcom`、`keenable`、`open_websearch` 四个名称，均为 `ProviderCapabilities(search=True, fetch=True)`。前三家 vendor option 仅 `api_url` 且需要 Key；open_websearch 允许 `api_url`、可选 `engines`，不需要 Key。
- CLI、领域对象、通用配置 schema、Runtime、FetchScheduler、URLStore 和全局重试策略沿用已有实现。engines 走现有 `options` → factory kwargs 通道，值在新 adapter 构造时验证；不在通用层增加 provider 特判。新适配器不建立自己的 client、配额、缓存、retry loop 或进程管理。
- `open_websearch` 只请求已有 daemon；配置 engines 时发送列表及固定总预算 `limit=50`，省略时保持 query-only。Fetch 始终 url-only。仍不发送 searchMode 或抓取渲染策略，不枚举/发现/逐个调度引擎，不安装或启动 Node.js、Playwright、daemon。
- 本工作树不含其他工作树中的元宝、知乎及 Retry-After 改动。不得为本计划合并、重置或修改那些分支。
- 规划依据：`writing-plans`、`test-driven-development` 及其测试反模式参考。每次只增加一个行为测试，完成 RED → GREEN → REFACTOR，再处理下面表格的下一行。

### 0.1 既有调用链与职责

`Runtime.build` 根据 registry 和配置构造每家一个 adapter、一个 `HttpJsonExecutor`，再把同一个双能力实例交给搜索与抓取列表，并通过同一个 `ProviderQuotaManager` 共享该 provider 的并发额度。搜索返回 `list[KeywordSearchHit]`；现有 staging 负责 URL 规范化、Google 跳转处理与 URLStore 收录。抓取接收 `NormalizedURL`，返回 `URLFetchCandidate`，由原有 scheduler 执行候选检查、回退与 LLM 后处理。

`keyword-search --provider NAME` 可以筛选搜索提供商；`url-fetch` 没有 `--provider`。端到端抓取仍要求 URL 先在同一运行时中被收录。单家 Fetch 冒烟采用 adapter 测试或独立临时配置。

### 0.2 官方契约核对结果（2026-11-17）

| Provider | 请求合同 | 成功响应与本地映射 |
| --- | --- | --- |
| `doubao` Search | 根 `https://open.feedcoopapi.com/search_api`；`POST /web_search`；Bearer；`{"Query": query, "SearchType": "web"}` | `Result.WebResults[]`；`Url` → url、`Title` → title；优先非空 `Summary`，缺失/null/空白时用 `Snippet`。URL 可缺，按单条拒绝统计。 |
| `doubao` Fetch | `POST /fetch`；同一 Bearer；`{"Url": str(url)}` | 先检查 `ResponseMetadata.Error`；`Result.Data.StatusCode` 是原站状态，必须为整数 2xx；`ContentText` 必须为非空字符串。 |
| `youcom` Search | 根 `https://ydc-index.io`；`POST /v1/search`；`X-API-Key`；`{"query": query, "count": 10}` | `results.web[]`；`url`、`title`、同层 `snippets: list[str]`。摘要以 `"\n\n"` 连接 snippets；缺失/null/空数组按可选摘要处理；存在但类型错误则拒绝该条。 |
| `youcom` Fetch | `POST /v1/contents`；`{"urls": [str(url)], "formats": ["markdown"]}` | 根为数组；用 `normalized_match(record["url"], target, ...)` 选择目标，`markdown` 非空才成功。不使用未文档化的 `final_url` 或 HTML 替代正文。 |
| `keenable` Search | 根 `https://api.keenable.ai`；`POST /v1/search`；`X-API-Key`；`{"query": query, "max_results": 10}` | 根对象 `results[]`；`url`、`title`、`snippet`。不读取已废弃的 `description` 作为主摘要，也不猜 `summary`。 |
| `keenable` Fetch | `GET /v1/fetch?url=<编码后的目标>`；同一 Key；无 JSON body、无 `live` | 根对象 `content` 非空字符串。返回 `url` 是可能重定向后的地址，不套用 You.com 的匹配数组规则。 |
| `open_websearch` Search | 根 `http://127.0.0.1:3210`；`POST /search`；未配时 `{"query": query}`；显式列表时 `{"query": query, "engines": [...], "limit": 50}`；无认证头 | `status == "ok"`、`data.results[]`；`url`、`title`、`description` → snippet；非空 `data.partialFailures` 只生成安全局部失败诊断。 |
| `open_websearch` Fetch | `POST /fetch-web`；`{"url": str(url)}` | `status == "ok"`、`data.content` 为非空字符串。其他 metadata 不充当正文。 |

You.com 的 `count=10` 和 Keenable 的 `max_results=10` 是本轮固定的保守值，不新增配置项。You.com 不发送 `extraction`、`livecrawl`、`knowledge`，Keenable 不发送 `mode`、`live`、`prompt`。

**已同步的规格勘误：** 三份 spec 中的 `contents.snippets` 应为 `results.web[].snippets`；这是官方字段核对，不改变已确认的产品范围。

**来源与证据限制：**

- [Doubao Search 官方接口](https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh) 与 [Fetch 官方接口](https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh)：请求、字段、Key 权限、错误码。
- [You.com Search](https://you.com/docs/api-reference/search/v1-search.md)、[Contents](https://you.com/docs/api-reference/contents.md)、[错误参考](https://you.com/docs/using-the-api/error-code-reference)。
- [Keenable Search](https://docs.keenable.ai/api-reference/search)、[Fetch](https://docs.keenable.ai/api-reference/fetch.md)、[错误参考](https://docs.keenable.ai/errors)。
- [open-webSearch HTTP API](https://github.com/Aas-ee/open-webSearch/blob/main/docs/http-api.md) 与 [CLI/Daemon machine contract](https://github.com/Aas-ee/open-webSearch/blob/main/docs/architecture/cli-json-protocol.md) 定义 data.results/engines/partialFailures。本轮另核对上游提交 `2562c9233a945c18fa9d08b7e450ca218a8853b6` 的 [HTTP handler](https://github.com/Aas-ee/open-webSearch/blob/2562c9233a945c18fa9d08b7e450ca218a8853b6/src/adapters/http/localDaemon.ts)、[引擎选择与总预算](https://github.com/Aas-ee/open-webSearch/blob/2562c9233a945c18fa9d08b7e450ca218a8853b6/src/core/search/searchEngines.ts)、[多引擎搜索服务](https://github.com/Aas-ee/open-webSearch/blob/2562c9233a945c18fa9d08b7e450ca218a8853b6/src/core/search/searchService.ts)。源码证实 limit 为总预算（默认 10、上限 50），白名单会过滤引擎且全被过滤时回退默认，普通子引擎错误进入 partialFailures。未调用用户 daemon；Task 13 live 必须另外记录运行版本，不能把源码核对当作部署已验证。
- Context7 工具未提供，公开 resolution 未取得可用 library ID；此次使用上述官方文档核对，未声称执行了 Context7 query-docs。

### 0.3 错误、诊断与类型约定

1. 认证或业务错误不能变成 `[]`。HTTP 非 2xx 仍交给共享 executor：408/429/5xx、timeout、transport 有限重试；400/401/402/403/404/422 终态。只在 HTTP 200 的已知错误形状上使用 adapter 的 `response_inspector`；不得抢先把普通 429/5xx 改成不可重试错误。
2. Doubao 200 业务重试白名单：Search 为 `700429`、`10500`；Fetch 增加 `10502`。`Code` 为数字字符串、`CodeN` 为整数；只有一种存在时可按文档解析，二者都有且冲突/类型异常时不可重试。参数、Key、额度、订阅限制、`10413` 和未知码终态。
3. You.com/Keenable 文档主要定义非 2xx 错误；把其已文档化错误形状放到 200 的 fixture 是**防御性合成用例**，不是声称官方常态返回 200 错误。未知业务错误均终态；不要直接信任外部任意 `retryable=true`。
4. 当前全局策略不等于各家全部业务码策略。例如 You.com 的 `429/spend_cap_exceeded` 及 Keenable 某些 422/500 带额外业务语义；本轮按 spec 保留 HTTP 状态策略，不在适配器旁加 retry loop，也不声称已实现所有供应商的特殊重试建议。此基线未通用支持 Retry-After。
5. `response_inspector(response: httpx.Response) -> ProviderResponseFailure | None`；构造失败必须传 `provider_name`、`stage`、`status_code`、`retryable`、固定 `safe_reason`、安全 `diagnostics`。非 JSON 时返回 None，让 executor 抛 `ProtocolFailure`。schema 和空正文用 `common.failure` 返回 `ExecutionFailure`。
6. `request_json` 在 `common.JsonRequester` 上没有 `params` 参数。Keenable 使用 `urllib.parse.urlencode({"url": str(url)})` 拼入 endpoint，沿用现有 GET 适配器风格，不为这一家扩大公共 Protocol。用带 query/fragment/中文/`&` 的目标验证编码不串参。
7. 搜索逐条校验 URL 非空且可经 `normalize_url` 接受；只捕获 `ExecutionFailure` / `InputFailure` 等预期解析失败，保留原始 URL 给既有 staging。用 `CandidateCounts` 统计 `received/accepted/rejected/rejection_counts`，原因使用现有 `missing_url`、`invalid_record` 等白名单。不在 common 的 `Literal["url", "link"]` 中硬塞 Doubao `Url`；在 Doubao 本地判定原因。
8. Fetch 解析使用 `parse_fetch_with_diagnostics`；空白正文显式标记 `semantic_result="empty"`，未知 envelope 为 `category="invalid_envelope"`；You.com 根数组的 `response_kind` 应为 `array`。`fetch_response_diagnostics` 默认 object，You.com 应本地薄封装覆盖为 array，避免为此改全局 helper。
9. `safe_diagnostic_fields` 只是允许字段与脱敏边界，不保证任意上游 prose 不含查询。新适配器只记录固定分类、已知错误码、计数、HTTP 状态；未识别的 code 用固定 `unknown`，不原样记录任意 `message/detail/error/hint/fix`。已知错误可用本地固定安全说明。记录的异常 message 不插入原始 JSON、URL、查询或正文。
10. executor 拥有 `http_*` 事件，adapter 不重复发 `http_failed`。基线对 ProviderResponseFailure 的 retry/final 事件 category 仍叫 `provider_header`，诊断事件为 `provider_response`；本轮不顺带改名。取消直接传播，清理由 Runtime/executor 完成。
11. open-webSearch 构造参数 `engines: list[str] | None = None`；只验证非空 list/非空且非纯空白字符串元素，保存为私有 `tuple[str, ...] | None`，保留名称、顺序及重复项。每次 Search 新建 JSON list。无本地枚举/别名/去重；错误值抛 `TypeError`，由现有 Runtime 转 `ConfigFailure`。禁用项不构造、不做值校验，未知键仍拒绝。
12. 显式 engines 同时发送固定 `limit=50`，只新增 engines 一个专属可调字段；limit 是所有引擎的总预算，不是逐引擎数量，也不是无限返回保证。未配置时完全省略 engines 和 limit，保留默认行为。
13. `status=ok` 且合法 results 下，非空 partialFailures 只通过现有 response_diagnostics 添加固定 `upstream_code="partial_engine_failures"`、本地生成 `upstream_message="partial_engine_failures count=N"`；semantic_result 仍为 envelope 级 success。不新增不被白名单支持的 partial 枚举或 count 字段，不借用候选 rejected 计数，不转发 inner engine/code/message。保留合法命中/空列表，不触发子引擎或整次补偿重试。

## 1. 文件结构与职责

| 文件 | 操作及责任 |
| --- | --- |
| `src/agent_search_gateway/providers/web/doubao.py` | 新建；Doubao 请求、严格解析、业务 inspector、局部计数。 |
| `src/agent_search_gateway/providers/web/youcom.py` | 新建；You.com Search 与 Contents、目标 URL 对齐。 |
| `src/agent_search_gateway/providers/web/keenable.py` | 新建；Keyed Search、编码 GET Fetch。 |
| `src/agent_search_gateway/providers/web/open_websearch.py` | 新建；engines 构造校验/不可变快照、单次多引擎 Search、URL-only Fetch、成功信封及安全局部失败诊断；无生命周期托管。 |
| `src/agent_search_gateway/providers/defaults.py` | 修改 `build_default_registry`；末尾追加四家注册，保留旧顺序；仅 open_websearch 放行 api_url + engines。 |
| `tests/providers/web/test_doubao.py`、`test_youcom.py`、`test_keenable.py`、`test_open_websearch.py`（均位于同一目录） | 新建；真实 adapter 行为与请求合同。每个任务下给出完整路径。 |
| `tests/fixtures/providers/doubao/`、`youcom/`、`keenable/`、`open_websearch/`（均位于同一目录） | 新建中性合成 fixture；具体文件见 Tasks 1–8。 |
| `tests/providers/test_registry.py` | 扩展能力、Key 策略与旧提供商保留断言。 |
| `tests/unit/test_config_web_providers.py` | 扩展配置、密钥解析、vendor option、独立能力开关。 |
| `tests/runtime/test_runtime_assembly.py` | 扩展同实例、quota、client/executor 关闭及装配。 |
| `tests/observability/test_provider_diagnostics.py` | 扩展真实 executor/adapter 日志、重试、脱敏矩阵。 |
| `tests/acceptance/test_gateway_workflows.py` | 扩展搜索收录、单家失败隔离、Fetch 回退及 URLStore 安全。 |
| `tests/docs/test_documented_config.py` | 扩展默认关闭的四家示例与文档合同。 |
| `tests/integration/test_live_new_web_providers.py` | 新建；按已有 opt-in 门逐家 Search/Fetch。 |
| `README.md`、`config.example.toml`、`CHANGELOG.md` | 更新提供商/配置说明与 Unreleased 条目。 |

**阅读复用，默认不修改：** `providers/contracts.py`、`providers/registry.py`、`providers/http.py`、`providers/diagnostics.py`、`providers/web/common.py`、`config.py`、`runtime.py`、`url_normalization.py`、`orchestrators`、`scheduler`、`tests/support/http.py`、`tests/support/web_diagnostics.py`、`tests/support/acceptance.py`、`tests/support/controlled.py`、`tests/support/logging.py`、`docs/public-interface.md`、`docs/site/index.html`、`scripts/build_docs.py`。文档构建输出在 `site/`，`docs/site/index.html` 是输入模板，不把输出目录提交进 Git。

不引入新的 SDK 或依赖；不修改 `pyproject.toml` / `uv.lock`。若实施中发现确有公共缺陷，先用失败测试和差异说明证明必要性，不把重构当成本计划默认内容。

## 2. 实施前准备

- 确认仍位于上述工作树，阅读当前全局/目录指令，保留他人修改。已有文件写入使用读取时的 SHA-256；冲突时重新读取，不覆盖。
- 下列命令都在工作树根执行。安装依赖使用 `uv sync --locked --all-groups`（若已同步无需重复）。
- fixture 均使用 `example.com` 等中性 URL 与短合成文本；成功 Search 至少两条；另有合法空列表、单条损坏、错误 envelope、非法 envelope、Fetch 正常/空/错误和非 JSON。
- 协议映射测试用 `RecordingJsonExecutor`；业务重试、HTTP 错误、decode、取消和真实日志必须用 `HttpJsonExecutor + httpx.MockTransport`，不能让记录型 fake 假装执行 inspector。
- 表中多个用例必须逐个执行完整 TDD 循环，不先把全部生产实现写完再统一补测试。首个新模块测试应记录缺失组件，并在最小实现打通导入后继续观察业务断言的红绿过程；不把收集阶段 ImportError、fixture路径错误、拼写错误或环境故障当作行为验证完成，最终测试只保留业务合同断言。
- 每个 Task 的 Step 4 跑目标文件及相关目录；全仓检查集中到最终验收。仅为本功能新增行为/具体回归风险写测试，不为纯文案单独造测试。

## 3. TDD Tasks

### Task 1: Doubao Search 与业务错误边界

**Files:**

- Create: `src/agent_search_gateway/providers/web/doubao.py`
- Test/Create: `tests/providers/web/test_doubao.py`
- Create: `tests/fixtures/providers/doubao/search_success.json`
- Create: `tests/fixtures/providers/doubao/search_empty.json`
- Create: `tests/fixtures/providers/doubao/search_partial.json`
- Create: `tests/fixtures/providers/doubao/search_error.json`
- Create: `tests/fixtures/providers/doubao/search_malformed.json`
- Create: `tests/fixtures/providers/doubao/invalid_json.txt`
- Read: `src/agent_search_gateway/providers/web/brightdata.py`、`common.py`（同目录）、`tests/support/http.py`

- [x] **Step 1: Write the failing test**

先写 `test_search_maps_web_results`：给两个文档形状结果，一个含 Summary，另一个仅 Snippet；调用真实 adapter.search；断言两个 KeywordSearchHit 的字段与原始 URL，且 raw_content/content 为空。逐次补充以下行为：

| 测试名 | Setup / Action / Expected |
| --- | --- |
| `test_search_sends_minimal_custom_request` | 记录一次请求；准确 POST 路径、Bearer、Query/SearchType；无额外 Filter/Count/Content 参数。 |
| `test_search_returns_empty_for_valid_empty_results` | WebResults=[] → []，一次调用。 |
| `test_search_rejects_only_invalid_records` | 合法项+无 Url/非法 URL/非对象/错误字段类型 → 保留好项并准确计数。 |
| `test_search_fails_on_invalid_envelope` | 顶层/Result 非对象、WebResults 缺失或非数组 → ExecutionFailure。 |
| `test_search_retries_documented_business_codes` | MockTransport 先 200 Error(700429 或10500)后成功 → 共享 executor 重试后命中。 |
| `test_search_does_not_retry_permanent_or_unknown_business_error` | 10400/10403/额度/未知码、冲突 Code/CodeN → 一次失败，不变成空结果。 |

缺失功能的预期失败是映射、请求或错误分类断言不成立。非法 JSON 的传输验证放 Task 11，不能用字典 fake 代替 decode。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_doubao.py -k search -v
~~~

预期：新行为 assertion FAIL；不是 fixture 文件名、import 拼写或工具故障。

- [x] **Step 3: Describe the minimal implementation**

~~~text
DoubaoAdapter(*, name, api_url=官方根地址, secret: SecretValue, http_executor: JsonRequester)
  保存配置和注入引用；endpoint 统一拼接后缀
search(query)
  request_json(POST, /web_search, stage=search, Bearer, Query/SearchType,
               response_inspector=本地search检查器, response_diagnostics=本地安全分类器)
  严格检查 ResponseMetadata（存在时必须对象）和非空 Error
  Result 必须对象，WebResults 必须列表
  逐条读取 Url / Title / Summary / Snippet；只对缺失或空摘要使用确定的备选字段
  验证 URL、记录 CandidateCounts；错误类型不能用 str() 强制“修好”
  发一次 provider_candidates_summary；返回 hits
inspector
  只检查 HTTP 200；非法 JSON 返回 None
  官方错误 → 固定 safe_reason + 已知 code + retryable白名单
  不记录 Message、不sleep；其余HTTP留给executor
~~~

Summary 存在但非字符串属于坏条目，不得悄悄退回 Snippet 掩盖协议错误。只保存搜索摘要，不把 Summary 填入网页正文。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_doubao.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：目标行为与现有 web/executor 测试 PASS。

- [x] **Step 5: Refactor (keep tests green)**

将该供应商的元数据错误识别抽为本模块私有 helper，给 Task 2 复用；不要抽通用字段路径框架。再次运行 Step 4 命令。

### Task 2: Doubao Fetch 原站状态与正文

**Files:**

- Modify: `src/agent_search_gateway/providers/web/doubao.py`
- Test/Modify: `tests/providers/web/test_doubao.py`
- Create: `tests/fixtures/providers/doubao/fetch_success.json`
- Create: `tests/fixtures/providers/doubao/fetch_empty.json`
- Create: `tests/fixtures/providers/doubao/fetch_error.json`
- Docs/Read: `docs/designs/error-handlings/20261117-doubao-youcom-keenable-openwebsearch.md`

- [x] **Step 1: Write the failing test**

首个 `test_fetch_maps_content_text` 使用 StatusCode=200、两段合成正文，断言 raw_content == content == 原正文，POST /fetch 只传 Url。逐个增加：

- `test_fetch_rejects_failed_origin_status`：原站403/404/500 即使 ContentText 有文本也失败；不按接口200成功处理。
- `test_fetch_rejects_invalid_status_code`：缺失、字符串、bool、对象状态码均失败。
- `test_fetch_rejects_empty_content`：ContentText 缺失/null/空白 → empty；非字符串 → invalid_envelope。
- `test_fetch_retries_documented_temporary_errors`：700429/10500/10502 后成功；共享尝试次数有效。
- `test_fetch_rejects_subscription_key_error_without_retry`：200 Error10409 一次终态；10413同样不重试。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_doubao.py -k fetch -v
~~~

预期：Fetch 未完成相应映射/拒绝的断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
fetch(normalized_url)
  发送 POST /fetch + Url + Bearer；使用 fetch 阶段 inspector/diagnostics
  parse_fetch_with_diagnostics(payload, _parse_fetch)
_parse_fetch
  拒绝 API 元数据错误
  严格取得 Result.Data
  StatusCode 为真实 int 且 200 <= code < 300，否则 failure
  ContentText 是字符串且 strip 后非空，否则 failure(empty或invalid_envelope)
  URLFetchCandidate(raw_content=原正文, content=原正文)
~~~

原站非成功是本地解析终态，交给 scheduler 尝试其他 provider；不添加原站状态的独立重试。保留正文原始格式，不用 strip 后的文本覆盖返回值。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_doubao.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：Search/Fetch 全部 PASS。

- [x] **Step 5: Refactor (keep tests green)**

复用 Task 1 的错误码解析与鉴权 helper；Search/Fetch 使用各自可恢复码集合与 stage。重跑 Step 4。

### Task 3: You.com Search 的 snippets 数组映射

**Files:**

- Create: `src/agent_search_gateway/providers/web/youcom.py`
- Test/Create: `tests/providers/web/test_youcom.py`
- Create: `tests/fixtures/providers/youcom/search_success.json`
- Create: `tests/fixtures/providers/youcom/search_empty.json`
- Create: `tests/fixtures/providers/youcom/search_partial.json`
- Create: `tests/fixtures/providers/youcom/search_error.json`
- Create: `tests/fixtures/providers/youcom/search_malformed.json`
- Create: `tests/fixtures/providers/youcom/invalid_json.txt`
- Read: `src/agent_search_gateway/providers/web/parallel.py`

- [x] **Step 1: Write the failing test**

`test_search_joins_top_level_result_snippets`：results.web 有两条，snippets=["first", "second"]，contents 无摘要；期望 snippet 为 `"first\n\nsecond"`，不能为列表字面量。然后逐次覆盖最小请求、空数组、snippets缺失/null/[]、snippets存在但非列表或成员非字符串、无效URL、坏条目与顶层结构错误。错误形状的合成200用例用官方 `detail` 或 `error` 结构，但明确标注为防御性 fixture。

`test_search_omits_optional_extraction` 必须精确断言 json_body == {"query": query, "count": 10}；使用官方 host、X-API-Key，禁止附加 extraction/livecrawl。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_youcom.py -k search -v
~~~

预期：映射/最小协议断言 FAIL，不得用旧的 contents.snippets fixture 制造假通过。

- [x] **Step 3: Describe the minimal implementation**

~~~text
YouComAdapter(*, name, api_url=官方根地址, secret, http_executor: JsonRequester)
search(query)
  POST /v1/search，X-API-Key，query/count=10，inspector与diagnostics
  验证 root.results.web 是列表
  每项读取 url/title，snippets 可选
  snippets非空列表时逐个验证string后以两个换行连接；可选缺失为""
  URL验证、局部拒绝、候选计数、KeywordSearchHit
inspector
  200中明确的已知错误对象 → 不可重试 ProviderResponseFailure
  不因存在任意业务字段就误判正常结果；其余状态由executor处理
~~~

不自动使用 `contents.html/markdown/highlights`，不新增描述回退或提取开关。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_youcom.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：目标/相关回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

仅将 You.com 已知错误形状检查复用给 Fetch。清理测试重复 setup，保留单行为断言。重跑 Step 4。

### Task 4: You.com Contents 根数组与 URL 匹配

**Files:**

- Modify: `src/agent_search_gateway/providers/web/youcom.py`
- Test/Modify: `tests/providers/web/test_youcom.py`
- Create: `tests/fixtures/providers/youcom/fetch_success.json`
- Create: `tests/fixtures/providers/youcom/fetch_empty.json`
- Create: `tests/fixtures/providers/youcom/fetch_error.json`
- Read: `src/agent_search_gateway/providers/web/tavily.py`、`src/agent_search_gateway/providers/web/common.py`

- [x] **Step 1: Write the failing test**

`test_fetch_selects_matching_markdown_from_root_array`：响应前一条是不匹配URL，后一条匹配目标；必须选择后者正文，不能直接拿数组第一项。请求只含单个 urls 和 formats=["markdown"]。逐次覆盖：

- 规范化后相等 URL 可匹配；无匹配、空数组不能成功。
- markdown缺失/null/""/全空白、仅html均失败；数字/对象不能强转字符串。
- 非对象数组项不能被当作正文；可跳过坏项后接受后续有效匹配项，并在全无可用匹配时失败。
- 根对象/字符串等非法容器失败；200错误形状终态。
- 正常Fetch diagnostics的 response_kind=array；空白匹配标记empty，无匹配标记no_match。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_youcom.py -k fetch -v
~~~

预期：目标匹配、正文或 array 诊断断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
fetch(target)
  POST /v1/contents，X-API-Key，单个urls与formats
  用parse_fetch_with_diagnostics包装本地_parse_fetch
_parse_fetch
  require_list(payload)
  逐项验证对象/url，以normalized_match匹配target
  仅接受匹配且非空的markdown；记录是否有空白匹配
  成功返回URLFetchCandidate(markdown, markdown)
  无可用正文 → failure(empty/no_match/invalid_envelope)
diagnostics
  与同一个parser复用分类；正确标记根array；不输出url或markdown
~~~

不臆造重定向字段匹配；当前规范化匹配是 Gateway 的保守合同，真实重定向差异必须在 live 记录中说明。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_youcom.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：新Search/Fetch与旧web回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

Fetch成功解析与diagnostics使用同一判定，避免一处array一处object。重跑 Step 4。


### Task 5: Keenable Keyed Search

**Files:**

- Create: `src/agent_search_gateway/providers/web/keenable.py`
- Test/Create: `tests/providers/web/test_keenable.py`
- Create: `tests/fixtures/providers/keenable/search_success.json`
- Create: `tests/fixtures/providers/keenable/search_empty.json`
- Create: `tests/fixtures/providers/keenable/search_partial.json`
- Create: `tests/fixtures/providers/keenable/search_error.json`
- Create: `tests/fixtures/providers/keenable/search_malformed.json`
- Create: `tests/fixtures/providers/keenable/invalid_json.txt`
- Read: `src/agent_search_gateway/providers/web/common.py`

- [x] **Step 1: Write the failing test**

`test_search_maps_keyed_results`：根对象含 results 两项，断言 url/title/snippet，description 即使出现也不覆盖 snippet。逐次添加：

| 测试名 | 关键断言 |
| --- | --- |
| `test_search_sends_only_query_and_max_results` | POST https://api.keenable.ai/v1/search，X-API-Key，body精确含query/max_results=10。 |
| `test_search_returns_valid_empty_results` | results=[]返回[]，不误判错误。 |
| `test_search_preserves_valid_results` | 好条目与坏条目混合，保留好项、拒绝统计准确；全坏案例另测。 |
| `test_search_rejects_invalid_results_envelope` | 非对象/缺results/非列表 → failure。 |
| `test_search_rejects_embedded_keyed_error` | 把已文档化 flat error/code/message 对象置于200（防御性合成） → ProviderResponseFailure，不当零结果。 |
| `test_search_never_falls_back_to_public_endpoint` | 401/402之后不出现 /public 请求，无第二套请求或Key替换。 |

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_keenable.py -k search -v
~~~

预期：请求路径、结果或失败分类 assertion FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
KeenableAdapter(*, name, api_url=官方根地址, secret, http_executor: JsonRequester)
search(query)
  POST /v1/search + X-API-Key + query/max_results=10
  严格 root.results 列表；url/title/snippet 映射
  CandidateCounts逐条统计，输出KeywordSearchHit
inspector
  只检查200 flat错误形状；不把error字段当正文或把message当摘要
  安全已知code/固定unknown，未知错误不可重试
  不使用public、SDK隐式fallback、任意retryable字段或sleep
~~~

Keenable 官方 title 必填；以单条字段校验落实。snippet 可缺失，类型错误拒绝该条。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_keenable.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：目标与相关回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

本地复用 error 形状/鉴权，不复制共享 retry。重跑 Step 4。

### Task 6: Keenable GET Fetch 与查询参数编码

**Files:**

- Modify: `src/agent_search_gateway/providers/web/keenable.py`
- Test/Modify: `tests/providers/web/test_keenable.py`
- Create: `tests/fixtures/providers/keenable/fetch_success.json`
- Create: `tests/fixtures/providers/keenable/fetch_empty.json`
- Create: `tests/fixtures/providers/keenable/fetch_error.json`
- Read: `src/agent_search_gateway/providers/web/scraperapi.py`（GET URL 编码模式参考）、`src/agent_search_gateway/providers/web/common.py`

- [x] **Step 1: Write the failing test**

`test_fetch_encodes_target_url_once`：目标包含 query、`&`、`+`、中文及 fragment；检查 GET request 的 `httpx.URL(request.url).params["url"] == str(target)`，只存在 url 查询参数，json_body=None。随后依次测试：

- `test_fetch_maps_markdown_content`：raw_content/content 均是原始content。
- `test_fetch_allows_documented_final_url`：响应url与请求不同但content合法仍可接受，避免误套Contents规则。
- `test_fetch_omits_live_and_uses_keyed_endpoint`：不发live/prompt/max_chars，不用/public。
- `test_fetch_rejects_missing_or_invalid_content`：缺失/null/空白/非字符串分类准确；error对象不能当正文。
- 402/422一次终态、429/500受全局尝试次数限制（与Task11共同覆盖，勿重复两套相同测试）。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_keenable.py -k fetch -v
~~~

预期：编码、正文或端点断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
fetch(target)
  request_url = endpoint(api_url, "/v1/fetch") + "?" + urlencode({"url": str(target)})
  request_json("GET", request_url, stage="fetch", X-API-Key, 无json_body,
               response_inspector, response_diagnostics)
  parse_fetch_with_diagnostics(payload, _parse_fetch)
_parse_fetch
  根对象，不是已知错误envelope
  必需url字段为合法非空URL（允许重定向）
  content必须为非空string；否则failure
  返回URLFetchCandidate(content, content)
~~~

不要求Markdown必须有标题符号；正文格式检查保持上游文本与现有候选判定，不引入Markdown parser。完整目标URL不能进入异常或日志。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_keenable.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：编码 roundtrip 与搜索/抓取回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

只抽本供应商的解析小函数；保持 JsonRequester/HttpRequester 类型不变。重跑 Step 4。

### Task 7: open-webSearch engines 配置、HTTP Search 与 daemon 边界

**Files:**

- Create: `src/agent_search_gateway/providers/web/open_websearch.py`
- Test/Create: `tests/providers/web/test_open_websearch.py`
- Create: `tests/fixtures/providers/open_websearch/search_success.json`
- Create: `tests/fixtures/providers/open_websearch/search_empty.json`
- Create: `tests/fixtures/providers/open_websearch/search_partial.json`
- Create: `tests/fixtures/providers/open_websearch/search_partial_engines.json`
- Create: `tests/fixtures/providers/open_websearch/search_all_engines_failed.json`
- Create: `tests/fixtures/providers/open_websearch/search_error.json`
- Create: `tests/fixtures/providers/open_websearch/search_malformed.json`
- Create: `tests/fixtures/providers/open_websearch/invalid_json.txt`
- Docs/Read: 官方 `docs/http-api.md`、`docs/architecture/cli-json-protocol.md` 及本次固定提交的 handler/searchService/searchEngines（URL见0.2）
- Read: `src/agent_search_gateway/providers/web/parallel.py`（构造 TypeError、可选参数与 Search/Fetch 隔离）
- Read: `src/agent_search_gateway/providers/diagnostics.py`、`tests/support/web_diagnostics.py`

- [x] **Step 1: Write the failing test**

编写 fixture 前核对所支持 daemon 版本与 0.2 的文档/固定源码；data.results、partialFailures、engines 和总预算语义以这些证据为准。证据不同先更新合同/fixture并记录差异，不做“多个猜测字段中取一个”的解析。没有 daemon 时可先离线实施，明确 live 未验证；不把源码支持的引擎名单固化为 Gateway 枚举。

首个 `test_search_omits_engines_when_not_configured`：RecordingJsonExecutor 返回 status=ok,data.results 两项；断言 POST /search、body严格等于 `{"query": query}`、无认证/engines/searchMode/limit。然后按下面每一行分别完成完整 TDD 循环：

| 测试 | Setup / Action / 预期 |
| --- | --- |
| `test_search_sends_configured_engines_with_max_total_limit` | 构造 engines=["bing","duckduckgo"] 后 Search；body 严格为 query + 相同 list + limit=50，恰好一个请求。 |
| `test_engines_rejects_invalid_structure` | 参数化 []、"bing,duckduckgo"、{}、1、True、["" ]、["   "]、["bing",1]；构造时 TypeError，无 HTTP；省略/None 合法。每项分别验证，不把缺模块错误当作行为 RED。 |
| `test_search_preserves_opaque_engine_names_order_and_duplicates` | 用 ["custom_z","custom_a","custom_z"]；顺序、重复、名字不变，不要求本地注册，不自动展开 all。 |
| `test_engines_snapshot_ignores_later_input_mutation` | 构造后 append/修改原 list；Search 仍使用构造时快照。 |
| `test_search_builds_a_fresh_engines_list_each_time` | 改动第一次 fake 记录到的请求 list；第二次请求不受影响，内部 tuple 不外泄。 |
| `test_search_maps_result_description` | 多引擎 fixture 的 description → snippet；engine/source 不变成 Gateway Provider。 |
| 独立的 empty / partial-record / invalid-container 用例 | 合法空数组、坏 URL/坏条目计数、data.results 非法容器仍按原合同；与局部引擎失败区别。 |
| `test_search_keeps_results_when_some_engines_fail` | 真实 executor + MockTransport：status=ok、有效 results、非空 partialFailures；保留 hits，一次响应诊断带固定 partial_engine_failures 和本地 count 摘要，不重试。 |
| `test_search_marks_engine_failures_even_when_results_are_empty` | 合法空 results + 非空 partialFailures；返回 []，仍有固定标记；不能声称所有引擎成功。 |
| `test_search_omits_engine_failure_marker_without_failures` | partialFailures 省略或 []；无局部失败标记。 |
| `test_search_redacts_engine_failure_details` | inner engine/code/message 与 query 注入 PRIVATE_*；只有固定 code/本地计数入日志，既有 candidate 统计不被挪用。 |
| `test_search_rejects_error_status_even_with_results` | status=error 且另带看似成功 data；错误优先。 |
| `test_search_rejects_unknown_status` | status 缺失或不合法；invalid_envelope。 |
| `test_search_does_not_retry_unknown_daemon_error` | HTTP200 error.code 为未知或 browser_unavailable；一次终态。普通 HTTP5xx 保持共享策略，不以此用例覆盖。 |

故意传错误类型的测试沿用 `test_parallel.py` 的写法保证 strict mypy 可检查；不要因此放宽生产构造签名或关闭整文件类型检查。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_open_websearch.py -k "search or engines" -v
~~~

预期：缺少显式列表/固定总预算、结构验证、快照或安全诊断时，相应行为断言 FAIL；不破坏已正确的省略分支制造 RED。

- [x] **Step 3: Describe the minimal implementation**

~~~text
private validate_engines(value: object) -> tuple[str, ...] | None
  if value is None: return None
  if 不是非空list 或任一元素非str/空/纯空白: raise TypeError(固定字段说明)
  return tuple(value)  # 不修改名称、顺序或重复项
OpenWebSearchAdapter(*, name, api_url="http://127.0.0.1:3210",
                     http_executor: JsonRequester, engines: list[str] | None = None)
  self._engines = validate_engines(engines)
  constructor不接受secret、不探测daemon
search(query)
  body = {"query": query}  # 每次新对象
  if self._engines is not None:
    body["engines"] = list(self._engines)
    body["limit"] = 50
  POST /search，只有一个daemon请求；不按引擎创建任务/Provider/配额
  200 inspector: status=error → 不可重试ProviderResponseFailure
  parser: root对象、status严格ok、data对象、results列表
  url/title/description映射，逐条校验与CandidateCounts
response_diagnostics(合法status=ok/搜索信封)
  既有category=provider_response,response_kind=object,semantic_result=success
  若data.partialFailures是非空list:
    upstream_code = 固定partial_engine_failures
    upstream_message = 本地生成的partial_engine_failures count=N
  不取inner engine/code/message，不发第二条事件、不改变返回结果/重试
  其余metadata忽略；不处理CLI文本输出
~~~

`daemon_unavailable` 的CLI示例里有 retryable 标志，不等于HTTP服务的重试合同；HTTP连接失败仍由共享传输重试处理。不要从原始message/hint猜重试。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_open_websearch.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：省略时默认行为不变，显式时名单与固定总预算准确发送；构造验证、快照、局部失败诊断及相关测试 PASS。

- [x] **Step 5: Refactor (keep tests green)**

本模块统一status/envelope验证，供Fetch复用。没有公共进程/浏览器抽象。重跑 Step 4。

### Task 8: open-webSearch Fetch、外部故障与取消

**Files:**

- Modify: `src/agent_search_gateway/providers/web/open_websearch.py`
- Test/Modify: `tests/providers/web/test_open_websearch.py`
- Create: `tests/fixtures/providers/open_websearch/fetch_success.json`
- Create: `tests/fixtures/providers/open_websearch/fetch_empty.json`
- Create: `tests/fixtures/providers/open_websearch/fetch_error.json`
- Read: `src/agent_search_gateway/providers/http.py`

- [x] **Step 1: Write the failing test**

`test_fetch_maps_daemon_content`：status=ok,data.content非空 → URLFetchCandidate；请求仅url。再用 `test_fetch_omits_search_options_when_engines_are_configured` 明确构造 engines=["bing","duckduckgo"] 后 Fetch，body 仍严格为 url-only、无 engines/limit/searchMode。逐次覆盖：

- status=error、未知状态、data缺失/类型错误。
- content缺失/null/空白/数字、只有title/finalUrl均不可返回成功候选。
- `test_connection_refusal_uses_shared_retry_budget`：MockTransport抛ConnectError，实际次数等于配置，最终ExecutionFailure；不调用任何额外路由/进程。
- `test_fetch_cancellation_propagates`：用Event控制的传输抛/触发CancelledError，原样传播，无新一轮请求。不用真实sleep制造时序。
- browser_unavailable为单provider失败，hint/details不入日志。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_open_websearch.py -k fetch -v
~~~

连接拒绝测试单独运行：

~~~bash
uv run pytest tests/providers/web/test_open_websearch.py -k connection_refusal -v
~~~

预期：新Fetch行为尚未满足；取消/传输本来已由executor正确处理的断言可以直接PASS，记录为回归证据，不刻意破坏旧代码制造RED。

- [x] **Step 3: Describe the minimal implementation**

~~~text
fetch(target)
  POST /fetch-web，body只有url；同一status inspector
  不读取/发送self._engines，不复用Search的请求body或limit
  parse_fetch_with_diagnostics
  status必须ok，data必须对象，content必须非空string
  返回URLFetchCandidate(content, content)
  不捕获并吞掉CancelledError，不创建进程/浏览器/健康检查
~~~

错误处理复用模块私有helper，传输和取消无新增代码路径。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_open_websearch.py -v
uv run pytest tests/providers/web tests/providers/test_http_executor.py -q
~~~

预期：Search/Fetch、有限重试和取消回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

检查函数只负责协议转换；移除重复诊断与无用异常包裹。重跑 Step 4。

### Task 9: 默认注册、密钥策略与独立能力配置

**Files:**

- Modify: `src/agent_search_gateway/providers/defaults.py`（imports及build_default_registry）
- Test/Modify: `tests/providers/test_registry.py`（原82–136附近）
- Test/Modify: `tests/unit/test_config_web_providers.py`
- Test/Modify: `tests/runtime/test_runtime_assembly.py`（原119–145精确名单）
- Read: `src/agent_search_gateway/providers/registry.py`、`src/agent_search_gateway/config.py`

- [x] **Step 1: Write the failing test**

`test_default_registry_adds_four_dual_capability_providers` 通过已存在的 `build_default_registry().list_in_registration_order()` 断言四个新名称各出现一次、class factory、双能力、allowed_config_keys及Key要求。四家尚未注册时为清晰的assertion FAIL。

然后使用 `resolve_web_provider_config(data, registry, environment)`，按provider参数化逐一增加：

| 场景 | 预期 |
| --- | --- |
| 三家启用且环境中有指定变量 | secret为SecretValue，值只在授权头使用；repr不泄漏。 |
| 三家缺api_key_env或指定变量缺失 | ConfigFailure，不能默认取其他Key。 |
| open_websearch无Key启用 | 成功；不注入secret。 |
| open_websearch启用却配置api_key_env | 沿用ConfigFailure，不静默忽略。 |
| 四种开关组合 | Search/Fetch独立；双关闭不解析Key、不实例化、不联网。 |
| api_url与max_concurrency=7 | 前三家 options 只有 api_url；open_websearch 若配置 engines 则 options 原样保留该 list；max_concurrency 仍为共享字段。 |
| open_websearch 显式 engines | 配置解析成功，options 原样传 list（MappingProxyType 不是深冻结）；不在 config.py 做值校验。 |
| disabled open_websearch 的 engines=[] / api_url=" " | 允许键合法则配置解析成功并保留值；Runtime 不构造、不联网。启用后的错误值由 Task10 验证。 |
| 其他三家或旧Provider使用 engines | 仍是未知键，ConfigFailure；不把它加入 shared keys。 |
| 未知vendor option，如live/count/limit/searchMode | ConfigFailure，即使 Provider 禁用仍拒绝；不引入通用 extra 参数。 |
| group default_max_concurrency / 非正整数override | 继承已有默认/按现有规则拒绝。 |

修正旧断言的意图：旧18家顺序不变；不再用“新名单最后10个仍是旧名单”的 `[-10:]`；credentialless集合为 `{"jina","open_websearch"}`，相应测试重命名。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py -v
~~~

预期：新注册/配置不满足；旧provider测试不能通过删除断言来“修绿”。

- [x] **Step 3: Describe the minimal implementation**

~~~text
defaults.py imports四个真实adapter
在现有registrations末尾按doubao,youcom,keenable,open_websearch顺序追加:
  前三家的allowed_config_keys = frozenset({"api_url"})
  open_websearch的allowed_config_keys = frozenset({"api_url", "engines"})
  WebProviderRegistration(name, ProviderCapabilities(True,True), Adapter,
                          allowed_config_keys, requires_api_key=对应值)
复用registry/config已有允许键校验与options透传；不加provider特判/validator系统/默认URL字典
同步旧名单测试，保留对旧18家与顺序的完整断言
~~~

新默认根地址由adapter constructor提供；自定义api_url及末尾斜杠由各家合同测试验证。`__init__.py` 现无统一导出需求，不为了注册新增导出层。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py -v
uv run pytest tests/providers tests/unit/test_config_web_providers.py -q
~~~

预期：新注册配置和旧provider回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

只提取测试数据表，避免按provider复制整套配置测试。保留明显的requires_api_key差异，重跑 Step 4。


### Task 10: Runtime 装配、共享配额与资源关闭

**Files:**

- Test/Create: `tests/runtime/test_new_web_runtime_assembly.py`
- Regression/Read: `tests/runtime/test_runtime_assembly.py`
- 执行调整：原测试文件的纯 import 窄编辑（带 expected_sha256）仍被 LocalHost 的 Secret-looking content 检测拒绝；将本任务新增回归放入独立新文件，保留全部验收行为、现有运行时测试与公共生产代码。
- Read: `src/agent_search_gateway/runtime.py`（Runtime.build、_build_web_providers、aclose）
- Read: `src/agent_search_gateway/config.py`、`tests/runtime/test_quota_manager.py`
- Conditional Modify: 四个新adapter的constructor，仅在注入/默认地址测试证明有错时修正

- [x] **Step 1: Write the failing test**

`test_new_web_provider_shares_adapter_between_search_and_fetch` 按四家参数化，使用现有 `_config()` 的LLM部分与仅一家新的web配置。配置max_concurrency=7，真实Runtime构造；断言：

~~~python
[search] = runtime.web_search_providers
[fetch] = runtime.web_fetch_providers
assert search is fetch
assert isinstance(search, expected_adapter_class)
assert runtime.quotas.get_web(provider_name).limit == 7
~~~

继续逐个增加：

- `test_new_web_provider_uses_default_api_url`：省略api_url，经注入MockTransport实际调用Search/Fetch，观察官方根+后缀。
- `test_new_web_provider_honors_custom_api_url`：自定义根含路径前缀与末尾斜杠，精确观察请求，无重复斜杠或吞路径。
- Search only / Fetch only / disabled 装配到正确列表，disabled不创建该家的client。
- `test_new_web_provider_client_is_closed_once`：Runtime.aclose调用两次，每个client关闭恰好一次。
- 共享quota沿用现有并发测试模式，由 SearchOrchestrator 与 FetchScheduler 发起调用，用Event断言同一家Search和Fetch使用同一额度；不直接并发调用adapter来测试quota，也不给adapter新增semaphore。
- `test_new_web_provider_forwards_open_websearch_engines`：真实 resolve_config → Runtime.build → MockTransport，TOML engines list 到达 factory/adapter；Search 精确发送原列表+limit=50，Fetch 严格 url-only；两阶段仍同一实例，quota不乘引擎数。
- `test_new_web_provider_wraps_invalid_engines_as_config_failure`：逐个错误列表值由启用 adapter 构造抛 TypeError，Runtime 转 ConfigFailure，未发网络；测试主动关闭失败构造过程中已创建的 clients，不为本任务重写 Runtime 生命周期。
- `test_new_web_provider_skips_engines_validation_when_disabled`：双关闭配置 engines=[]/api_url=" "，不构造该 adapter/client；未知键仍在配置解析阶段失败。

这些是新注册项的集成合同。如果现有通用层已使其首跑PASS，记录回归通过，不为制造RED修改正确的Runtime；只有缺失/错误注入才需修复生产代码。

- [x] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/runtime/test_new_web_runtime_assembly.py -k new_web_provider -v
~~~

缺陷时预期失败应定位到实例、URL、能力或关闭次数；不得把LLM配置不完整误判成新provider缺陷。

- [x] **Step 3: Describe the minimal implementation**

~~~text
复用resolve_config与现有Runtime.build
factory kwargs = name + http_executor + **provider_config.options；有Key才接收secret
open_websearch的engines直接由options透传；值错误TypeError由现有Runtime包装ConfigFailure
不修改config.py/runtime.py的生产路径；只在新adapter保存不可变快照
通过http_client_factory注入AsyncClient(MockTransport(handler))
每家一个adapter一个web executor，双能力共享同一实例
max_concurrency由既有quotas处理；aclose保持幂等
如果不通过，只修新adapter的默认参数或Task9注册
~~~

真实签名：

~~~python
runtime = Runtime.build(
    resolved,
    RuntimePaths.from_home(tmp_path),
    registry=registry,
    http_client_factory=client_factory,
)
~~~

不存在 `http_transport_factory`。复用基线 `_CountingAsyncClient`；若保留两家LLM，一家Web总client数为3，四家Web为6。不要把双能力算两个client。测试用try/finally关闭，不能泄漏会话。

- [x] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/runtime/test_runtime_assembly.py tests/runtime/test_new_web_runtime_assembly.py tests/runtime/test_quota_manager.py -v
uv run pytest tests/runtime -q
~~~

预期：装配、配额、关闭、旧LLM/学术/Agent回归 PASS。

- [x] **Step 5: Refactor (keep tests green)**

共用参数化输入与client收集fixture，不改Runtime生命周期策略。重跑 Step 4。

### Task 11: 统一 HTTP 错误、诊断与脱敏验收

**Files:**

- Test/Modify: `tests/observability/test_provider_diagnostics.py`
- Test/Modify: `tests/providers/web/test_doubao.py`
- Test/Modify: `tests/providers/web/test_youcom.py`
- Test/Modify: `tests/providers/web/test_keenable.py`
- Test/Modify: `tests/providers/web/test_open_websearch.py`
- Conditional Modify: 对应新adapter的inspector/diagnostics/parser
- Read: `tests/support/web_diagnostics.py`、`tests/support/logging.py`、`tests/providers/test_http_executor.py`、`src/agent_search_gateway/providers/diagnostics.py`

- [ ] **Step 1: Write the failing test**

对四家、search/fetch各阶段，用真实executor和MockTransport按以下合同逐项验证。基础HTTP机制已覆盖的情况优先参数化新adapter入口，不复制executor测试的全部内部断言。

| 场景 | 预期 |
| --- | --- |
| 401/402/403/404/422 | 一次尝试，现有HttpStatusFailure/ExecutionFailure；保留http_status。 |
| 408/429/500后成功 | 配置max_attempts=3；先失败再成功，正确调用次数、阶段/供应商，无http_failed。 |
| 持续429/500、timeout/ConnectError | 受配置最大次数限制；N-1次http_retrying、一次http_failed，无独立sleep。 |
| Doubao200临时业务码后成功/耗尽 | 使用同一尝试预算；成功不产生最终失败，耗尽只一次终态事件。 |
| 四家200终态业务错误 | ProviderResponseFailure，semantic_result=error，未返回空结果。 |
| 200非JSON/空body | ProtocolFailure，category=decode，不重试；每家invalid_json.txt均实际进入response.json。 |
| 合法JSON但schema错误 | ExecutionFailure，诊断invalid_envelope；无盲目重试。 |
| 空Fetch正文 | semantic_result=empty，不把title/摘要/错误消息作为正文。 |
| Search一好一坏/全坏/合法空 | 每次一次adapter候选summary，分区计数准确；真正空结果不误判错误。 |
| 任务取消 | CancelledError传播，不产生额外attempt或吞取消。 |
| open_websearch 显式列表遇429后成功 | 共享尝试预算；每次请求 engines/limit=50 相同，不去字段回退默认，不生成子引擎重试。 |
| open_websearch status=ok且partialFailures非空 | 保留结果/合法空数组；一次响应诊断带固定code和本地count摘要，无retry/额外终态事件；候选数仍只统计results。 |

脱敏输入放入Key、中文查询、正文及URL query marker（例如 PRIVATE_QUERY、PRIVATE_BODY、PRIVATE_URL）；错误message/detail/hint/fix/code也注入marker。断言格式化后的日志和向上传播的异常message均不含marker或凭据；保留固定技术归因、阶段、状态与安全计数。日志中不出现任意原始响应字段。open-webSearch 额外给 partialFailures 的 engine/code/message 放 marker；只允许本地生成的固定 code/count 摘要，不新增 semantic_result=partial 或全局诊断字段。

已有 `assert_json_fetch_diagnostics` 内含固定正文/错误文字假设，不要为了复用它改供应商语义。必要时用现有logger/caplog在本测试文件写小型参数化断言；不建立第二套HTTP fake。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/observability/test_provider_diagnostics.py tests/providers/web/test_doubao.py tests/providers/web/test_youcom.py tests/providers/web/test_keenable.py tests/providers/web/test_open_websearch.py -v
~~~

预期：若有漏检，失败必须指出attempt计数、诊断类别、泄露marker或错误类型；已正确的公共机制保留PASS，不修改它制造RED。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
新adapter在request_json传入两个本地回调
inspector仅负责已知200业务失败；decode交回executor
diagnostics输出安全固定标量，使用safe_diagnostic_fields
fetch parser为empty/no_match/invalid_envelope附加明确diagnostics
搜索每个响应只发一个CandidateCounts汇总
所有HTTP尝试/重试/最终失败保持executor唯一owner
~~~

不同HTTP事件有自己的类别；不要要求所有终态都叫provider_response，也不要为parser失败补发一条不存在的HTTP失败。测试检查实际责任边界，保留基线provider_header事件名称。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/observability tests/providers/web tests/providers/test_http_executor.py tests/unit/test_retry.py -q
~~~

预期：新四家及旧诊断/重试回归 PASS；日志无敏感marker。

- [ ] **Step 5: Refactor (keep tests green)**

集中测试数据，保留每个案例的供应商/阶段id方便定位。不修改全局重试、ErrorCode或脱敏白名单来迎合错误fixture。重跑 Step 4。

### Task 12: Search → URLStore → Fetch 回退与调用隔离

**Files:**

- Test/Modify: `tests/acceptance/test_gateway_workflows.py`（参考原559–695真实adapter案例）
- Read: `tests/support/fakes.py`、`tests/support/acceptance.py`
- Read: `tests/orchestrators/test_keyword_provider_filtering.py`、`tests/orchestrators/test_url_fetch_admission.py`
- Conditional Modify: 新adapter实际协议缺陷；不默认改编排器/scheduler/store

- [ ] **Step 1: Write the failing test**

`test_new_provider_search_admits_url_for_fetch_fallback` 按四家参数化：

~~~text
Setup:
  真实待测adapter + 真实TavilyAdapter + MockTransport
  URLStore、ProviderQuotaManager、LLMStages（FakeLLMClient仅替代外部LLM）
  新provider Search返回合法目标；Fetch返回200业务错误或空正文
  Tavily Extract返回同一目标有效正文
Action:
  SearchOrchestrator.keyword_search收录目标
  FetchOrchestrator.url_fetch抓取目标
Expected:
  目标真实入库；第一次失败后下一家被调用；最终正文来自fallback
  输出/Store身份一致；没有额外网络解析或改变LLM流程
~~~

逐项补充：

- `test_new_provider_failure_does_not_discard_other_search_results`：一家搜索失败、另一家成功，整体仍保留成功结果。
- `test_new_provider_cannot_fetch_unadmitted_url`：未知URL在adapter前被拒绝，http调用计数不增加，ErrorCode.URL_NOT_ADMITTED。
- `test_new_provider_reuses_prepared_content`：目标已有正文时复用，不触发provider抓取。
- 新provider与旧provider返回相同URL，沿用现有去重；若用Google goto样本，仅验证共享staging，adapter保留原URL。
- `test_new_provider_open_websearch_multi_engine_results_use_existing_staging`：显式 engines 的一次 daemon 请求返回不同 engine 标记的命中（含重复URL）；仍按现有 staging 收录/去重，provider身份始终 open_websearch，无子Provider/额外HTTP扇出。
- 现有provider选择测试继续验证精确筛选、不回退到未选择者；无需新增CLI参数。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/acceptance/test_gateway_workflows.py -k new_provider -v
~~~

缺陷时预期为收录、实际回退请求或正文断言FAIL；全链路已满足时记为回归PASS。用受控HTTP序列，不用sleep猜并发执行次序。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
复用既有SearchOrchestrator/FetchOrchestrator/FetchScheduler构造
新adapter.search -> 原staging -> 同一个URLStore
新adapter.fetch失败 -> 原scheduler -> Tavily.fetch成功
FakeLLMClient只替代网络LLM，真实调用编排/存储/配额
仅修新adapter造成的字段/错误契约缺陷
~~~

不把 `build_acceptance_runtime` 的固定fake provider集合改成全局新列表，避免影响不相关验收；在当前文件新增参数化装配即可。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/acceptance/test_gateway_workflows.py tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_url_fetch_admission.py tests/orchestrators/test_url_fetch_flow.py -v
uv run pytest tests/acceptance tests/orchestrators tests/scheduler -q
~~~

预期：真实新adapter链路、旧回退、admission和结果合同 PASS。

- [ ] **Step 5: Refactor (keep tests green)**

收拢本文件内新case的参数化构造，保留可以看到完整业务行为的断言；不用mock绕过store/scheduler。重跑 Step 4。

### Task 13: 默认关闭示例、文档与可选 live 入口

**Files:**

- Modify: `config.example.toml`（Web区末尾、academic区之前）
- Modify: `README.md`（能力表、提供商配置、抓取说明）
- Modify: `CHANGELOG.md`（Unreleased / Added）
- Test/Modify: `tests/docs/test_documented_config.py`（原250–374附近）
- Test/Create: `tests/integration/test_live_new_web_providers.py`
- Read: `tests/integration/test_live_parallel.py`、`tests/integration/test_live_tavily_and_openai.py`
- Read: `CONTRIBUTING.md`、`scripts/build_docs.py`、`docs/public-interface.md`

- [ ] **Step 1: Write the failing test**

先写 `test_new_web_provider_examples_are_disabled_and_complete`：解析真实config.example.toml，四家表齐全、两个enable均false、根地址正确。前三家原始TOML有下面的api_key_env，open_websearch无该键。新表未存在时assertion FAIL。

| Provider | 示例api_key_env | 默认api_url |
| --- | --- | --- |
| doubao | DOUBAO_API_KEY | https://open.feedcoopapi.com/search_api |
| youcom | YDC_API_KEY | https://ydc-index.io |
| keenable | KEENABLE_API_KEY | https://api.keenable.ai |
| open_websearch | 不设置 | http://127.0.0.1:3210 |

继续逐项验证：

- 在环境不含这三家新Key的情况下，示例新表仍可解析；其他既有启用provider用原测试的中性环境字典。
- disabled resolved provider 的secret和api_key_env均为None；不要误认为原始TOML写了名称就应解析凭据。
- 单独复制并启用一家，测试环境提供对应Key时成功；不改用户正在用的配置。
- 既有示例provider集合断言补四家，但旧默认开关保持不动；被特意移除的ZenRows/Decodo/ScrapingDog/ScrapeGraphAI示例不回填。
- 必要README合同断言覆盖四个名称、API入口、Key/daemon前提和 engines 专属配置；不为普通句式写脆弱逐字测试。
- `test_open_websearch_example_has_explicit_multi_engine_list`：原始示例有下面的非空/无重复名单，默认两个开关仍false；它是示例而非生产硬编码默认。仅启用 open_websearch 的临时配置经 Runtime 后，请求携带该列表和固定limit=50；也覆盖移除engines后的query-only兼容行为。

在 `tests/integration/test_live_new_web_providers.py` 中实现live入口，先定义两个参数化验收行为（每家独立test id），并只在该文件设置下列模块门；docs测试文件不受live门控制：

~~~python
pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)
~~~

`test_live_search_returns_valid_urls` 和 `test_live_fetch_returns_nonempty_content` 分开；默认关闭时八个case均SKIP，不执行网络fixture。open_websearch 的 live Search 构造时使用 engines=["bing","duckduckgo"] 的两引擎小样本来覆盖新分支；这不是生产默认列表，也不对示例中全部11个引擎执行额外请求。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/docs/test_documented_config.py -v
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/integration/test_live_new_web_providers.py -q -rs
~~~

预期：docs新表合同FAIL；live八项SKIP。不能为了让门测试变红先调用付费API，也不测试pytest自身skip实现。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
config.example.toml新增四个表:
  enable_search=false, enable_fetch=false
  显式api_url；前三家api_key_env，open_websearch无Key
  open_websearch另加下方显式engines示例；不新增limit/searchMode配置键
README:
  能力表新增四家；解释如何分别启用两种能力
  Doubao Fetch需按量付费Key与Fetch权限
  You.com使用ydc-index.io，Contents为独立调用/计费且需要相应scope
  Keenable只用keyed API，不发送live不保证完全无实时费用
  open_websearch由用户先运行HTTP daemon；用户在该Provider配置engines，Gateway只透传
  未配置时使用daemon单默认；显式时固定limit=50总预算，不保证所有引擎都有结果
  daemon白名单须允许所选引擎；否则可能过滤甚至回退单默认，DEFAULT_SEARCH_ENGINE不是列表
  Gateway不枚举/发现/执行子引擎；重复项会消耗预算，linuxdo暂不可用不纳入示例
  partialFailures以安全marker/count记录，Provider级成功不代表全部引擎成功
  Search可用--provider，Fetch通过临时配置/adapter隔离
  api_url只为根地址；超时/重试/max_concurrency仍是已有字段
CHANGELOG:
  Unreleased下记录四家Search+Fetch，无版本号提升/发版
live tests:
  复用真实adapter/executor，每个case结束关闭client
  keyed缺变量则SKIP并仅报告变量名称
  open_websearch缺显式OPEN_WEBSEARCH_API_URL则SKIP
  已提供daemon地址但连接失败则FAIL，不能广泛except后skip
  默认有限预算、每家一个Search/一个Fetch，无额外外层循环
  open_websearch Search采用两引擎小样本+固定总预算50；底层两任务由daemon执行
  可用测试本地response hook在不增加请求的前提核对data.engines与partialFailures
  只存/报告版本、effective-list是否匹配和失败数量；不打印原始响应/上游错误文字
  若观察到白名单过滤/默认回退，则多引擎覆盖未通过；不能据一条有效URL声称两个引擎均成功
~~~

`OPEN_WEBSEARCH_API_URL` 仅是测试入口选择；生产配置 engines 写入现有 Provider TOML 表，不新增产品环境变量/通用schema。示例的高覆盖率列表为：

~~~toml
[web_providers.open_websearch]
enable_search = false
enable_fetch = false
api_url = "http://127.0.0.1:3210"
engines = [
  "bing", "duckduckgo", "brave", "startpage", "baidu", "sogou",
  "exa", "keenable", "csdn", "juejin", "hackernews",
]
~~~

名单只作为用户可改的示例，实际可用性取决于daemon版本/网络/白名单；不写进生产验证枚举或constructor默认值。预算50是总数，不是每家50；省略列表不发送limit。live小样本只选两项，避免默认一次冒烟扇出全部示例引擎；无需新增engines测试环境变量。

`data.engines` 是 daemon 解析/过滤后的请求名单，不是逐引擎完成/成功清单；名单匹配且 partialFailures 为空也不能证明每项均成功。测试 hook 的名单比较只用于发现白名单过滤/默认回退；`results[*].engine` 只证明对应引擎提供了该条命中，缺少命中项的引擎保持未验证。

live Fetch直接调用adapter读取固定公开页面（例如 `https://www.python.org/about/`），无需启动用户Gateway；若改用CLI验收，必须在隔离Runtime先Search收录，且不用不存在的Fetch --provider。

Live失败报告只保留provider/stage、安全类别、HTTP状态/已知业务码与实际attempt计数；不打印secret、查询、URL敏感参数或正文。可输出命中数量、正文长度和耗时。无Key/无显式daemon是SKIP；实际执行后的权限、账户额度、网络、schema错误是FAIL并说明前提，不能伪装成功。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/docs/test_documented_config.py -v
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/integration/test_live_new_web_providers.py -q -rs
uv run python scripts/build_docs.py
~~~

预期：docs PASS，默认live八项SKIP，文档生成成功至 `site/`。`docs/site/index.html` 是模板；本轮无须修改模板，构建产物不加入提交。

- [ ] **Step 5: Refactor (keep tests green)**

文档用一个配置说明表避免重复；测试fixture只封装构造/关闭，不重写provider逻辑。重跑 Step 4，保留live gate和默认关闭行为。

## 4. 执行顺序与最终验收

### 4.1 依赖和提交边界

Tasks 1→2、3→4、5→6、7→8各是一家完整适配；四家可在各自新文件内并行实现，但 `defaults.py`、共享测试、README/config/changelog由一个执行者串行整合。Task9依赖四家adapter；Task10–12验证已整合链路；Task13完成用户入口说明与可选live。

每次只暂存当前任务明确涉及的文件，提交前检查差异。建议按“四家适配、注册/配置、链路诊断、文档/live”形成可审查提交；不要把别人的改动、真实响应或 `site/` 带入。实现完成后报告而不自行开PR、合并或发布版本。

### 4.2 全量离线门

所有目标测试通过后，在工作树根执行仓库定义的门；显式关闭live，防止继承终端中的启用变量：

~~~bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -v
uv run python scripts/build_docs.py
~~~

预期：

- Ruff格式/规则、strict mypy全部成功。
- 全量离线测试PASS，可选live按门SKIP；无真实外网或凭据依赖。
- docs构建成功，无新增依赖。
- 原有提供商、CLI参数/输出schema、URL准入、LLM/Agent/学术流程不因本轮变化。
- 如需修复格式，用既有Ruff只格式化实际修改的文件，再重跑对应门；不进行全仓无关格式化。
- 本轮无打包配置改动，不额外安排发版/上传或为计划添加新的CI job。

### 4.3 可选真实请求逐家执行

仅在实施完成且已有凭据/服务前提满足时运行。用户环境已经预置各家Key，可直接读取配置指定的变量；不要求把值发到聊天，不遍历/打印环境。若现有变量名与示例不同，使用临时测试配置或测试的明确映射，不复制、改写用户常用配置。

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k doubao -v -rs
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k youcom -v -rs
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k keenable -v -rs
env WEB_SEARCH_RUN_INTEGRATION=1 OPEN_WEBSEARCH_API_URL=http://127.0.0.1:3210 uv run pytest tests/integration/test_live_new_web_providers.py -k open_websearch -v -rs
~~~

最后一条仅在用户已启动该地址daemon且允许测试中的 bing/duckduckgo 两项时使用；若白名单限制或引擎不可用，记录覆盖/部署问题，不能自动换成其他引擎掩盖。若用户使用别处服务，替换为其明确提供地址，不扫描其他端口/进程、不启动服务。按需要只恢复本次网络命令的代理；本机回环应使用已有NO_PROXY配置，不更改共享代理环境。

验收只要求Search有可解析HTTP(S) URL、Fetch正文非空，记录实际response结构是否与fixture一致，不检查固定排名/数量或LLM措辞。若有差异，保存脱敏的最小字段/类型证据，修正本家fixture与parser并重跑定向测试，不能用宽泛fallback掩盖。不要为获得PASS持续重复付费请求。

**实施者必须填的结果记录（当前均未执行）：**

| Provider | Search | Fetch | 安全失败类别/已知码 | 实际重试情况 | 外部前提 |
| --- | --- | --- | --- | --- | --- |
| doubao | NOT RUN | NOT RUN | — | — | Key存在；Fetch按量权限 |
| youcom | NOT RUN | NOT RUN | — | — | Search/Contents scope与额度 |
| keenable | NOT RUN | NOT RUN | — | — | keyed Key/credits |
| open_websearch | NOT RUN | NOT RUN | — | — | daemon版本/URL；两引擎是否实际生效、局部失败数量（未知则注明未验证） |

执行后分别填PASS/FAIL/SKIP和原因；不把SKIP写作已验证。只记录安全元数据；正文与完整请求/响应不进Git、PR或计划附录。

## 5. 规格覆盖与类型一致性自检

| 规格要求 | 落实位置 |
| --- | --- |
| 架构D1–D3：独立四家、双能力、api_url与open_websearch专属engines | 0.2–0.3、Tasks1–10、Task13 |
| 架构D4：唯一executor/RetryPolicy与诊断 | 0.3、Tasks1–8、Task11 |
| 架构D5：入口不变、Fetch无provider过滤参数 | 0.1、Tasks12–13 |
| 架构D6：同实例/quota、统一关闭、无存储责任 | Task10、Task12 |
| 架构D7：SecretValue和安全诊断 | 0.3、Tasks9、11、13 |
| 架构D8：只透传用户列表，不托管daemon/枚举/执行子引擎 | Tasks7–8、10、13 |
| 错误矩阵：HTTP/业务200/decode/schema/empty/取消 | 0.3、各adapter任务、Task11 |
| 坏条目保留好结果；空列表与错误区分 | Tasks1、3、5、7、11 |
| Fetch下一家回退、URLStore拒绝、已有正文复用 | Task12 |
| Doubao原站状态/Fetch付费Key/已知临时码 | Tasks1–2、13 |
| You.com root数组、目标URL、snippets数组、无extraction | Tasks3–4、13 |
| Keenable keyed-only、GET编码、无live、非空content | Tasks5–6、13 |
| open-webSearch 省略/显式engines、固定总预算、Fetch隔离、连接拒绝/browser错误 | Tasks7–8、10–11、13 |
| engines构造TypeError→ConfigFailure、禁用项、不可变快照、名称原样透传 | 0.3、Tasks7、9–10 |
| 多引擎partialFailures安全marker/count、共享去重、无新增重试/诊断字段 | Tasks7、11–13 |
| Registry/config四开关组合、默认地址和关闭示例 | Tasks9–10、13 |
| 测试分层、合成fixture、HTTP真实执行边界 | 第2节、Tasks1–13 |
| 全量离线检查、每家live独立结果与前提 | 4.2–4.3 |
| README/config/Changelog与用户可见能力说明 | Task13 |

一致性核对：

- 四个class名始终为 `DoubaoAdapter`、`YouComAdapter`、`KeenableAdapter`、`OpenWebSearchAdapter`；provider名为 `doubao`、`youcom`、`keenable`、`open_websearch`。
- 方法始终为 `async search(query: str) -> list[KeywordSearchHit]`、`async fetch(url: NormalizedURL) -> URLFetchCandidate`；构造参数与Runtime factory注入一致，credentialless不要求secret。
- 请求参数始终通过 `json_body`，Keenable GET使用已编码URL；不将不存在的Protocol params或Runtime http_transport_factory写入实现。
- `KeywordSearchHit` 的Search正文保持空；Fetch两个content字段接收非空真实正文。You.com摘要是同层snippets；Keenable摘要是snippet。
- `ResponseInspector` 返回失败对象或None，不直接返回payload；`response_diagnostics` 返回安全Mapping；CandidateCounts由 `providers.diagnostics` 复用。
- `default_max_concurrency` 为group字段，`max_concurrency`为单家覆盖；env字符串是变量名称而非凭据。
- engines 只属于 open_websearch；TOML/options 为 list，constructor 参数 list[str] | None，内部 tuple[str, ...] | None，HTTP body 为每次新建的 list。未配时无engines/limit，已配时limit固定50；Fetch仍仅url。
- 无效engines直接构造TypeError、Runtime包装ConfigFailure；完全禁用时不构造。partialFailures仅有既有success语义+固定code/count摘要，不新增partial枚举、候选拒绝计数或原文日志。
- 本计划的目标测试/命令是待执行要求；编写/修订计划时未声称它们已PASS，也未启动daemon或执行live。
