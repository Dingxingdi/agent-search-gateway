# LangSearch Web Search Provider Implementation Plan

**Goal:** 以最小侵入方式新增可选的 LangSearch Web Search 提供商，复用现有凭证池与 HTTP 基础设施，返回可入库的搜索元数据并保持 URL Fetch 显式执行。

**Architecture:** [docs/designs/architectures/20261128-langsearch.md](../architectures/20261128-langsearch.md)

**Error handling:** [docs/designs/error-handlings/20261128-langsearch.md](../error-handlings/20261128-langsearch.md)

**Testing:** [docs/designs/testings/20261128-langsearch.md](../testings/20261128-langsearch.md)

---

## 执行上下文与边界

- 工作树：/home/agent/agent-search-gateway/.worktrees/design-langsearch-20261128。
- 分支：design-langsearch-20261128；计划依据设计提交 9c9361c，其实现基线为 5f70c12 / v3.1.0。继续使用此工作树。
- 本文是待执行的实施计划。复选框不表示实现或测试已经完成。
- 生产修改仅允许新增 src/agent_search_gateway/providers/web/langsearch.py，以及修改 src/agent_search_gateway/providers/defaults.py 的 import 和末尾注册项。
- 不修改其他适配器、common.py、config.py、runtime.py、credential_pool.py、HTTP executor、编排器、CLI、daemon、MCP、公共模型、依赖或锁文件。
- 仅 Search；不加入 Fetch、rerank、SDK、MCP client、正文检索、供应商可选参数、账号额度跟踪或新重试策略。示例默认禁用。

### 已核验的调用链

现有配置解析依据 WebProviderRegistration 校验能力、凭证与选项。Runtime._build_web_providers 调用 _HTTPAssembly.build_pool，为每个凭证构造独立 executor、gate 和 adapter，再以一个 PooledWebProvider 对外提供逻辑服务。SearchOrchestrator.keyword_search 负责 provider 选择、查询首尾空白处理、URL 标准化、去重、元数据准入和结果文件写入；适配器原样使用收到的 query。

LangSearchAdapter 的 keyword-only 构造参数与 AnySearchAdapter 相同：

~~~python
name: str
api_url: str
secret: SecretValue
http_executor: JsonRequester
~~~

方法为 async search(query: str) -> list[KeywordSearchHit]，没有 fetch 方法。api_url 是构造参数；由现有凭证 options 注入，不新增默认地址分支。示例明确填写官方 base URL。缺失 endpoint 的 runtime 构造错误继续由现有 _vendor_adapter 转成 ConfigFailure。

注意 PooledWebProvider 是共用包装器，本身包含 fetch 方法。Search-only 验收应检查 registry capabilities 和 runtime.web_fetch_providers，并检查物理适配器不实现 URLFetchProvider；不要断言逻辑包装器没有 fetch 属性。

### 文件职责与改动清单

| 文件 | 操作 | 职责 |
|---|---|---|
| src/agent_search_gateway/providers/web/langsearch.py | 新增 | 请求、严格信封验证、候选映射、局部诊断与候选计数 |
| src/agent_search_gateway/providers/defaults.py | 修改 | 导入并在 baiduqianfan 后追加一个 Search-only 注册项 |
| tests/providers/web/test_langsearch.py | 新增 | A1–A13 的适配器行为测试 |
| tests/fixtures/providers/langsearch/search.json | 新增 | 合成成功响应；无真实凭证、查询或上游响应副本 |
| tests/providers/test_registry.py | 修改 | 新注册契约和原有顺序 |
| tests/unit/test_config_web_providers.py | 修改 | 复用 Search-only 配置参数化用例 |
| tests/runtime/test_runtime_assembly.py | 修改 | 完整注册能力列表追加 LangSearch |
| tests/runtime/test_new_web_runtime_assembly.py | 修改 | 实际工厂装配、成员资源和启用边界 |
| tests/runtime/test_pooled_providers.py | 修改 | 实际 LangSearch 解析结果与既有 fallback 的衔接 |
| tests/doctor/test_no_network.py | 修改 | 已启用 LangSearch 的 doctor 仍不构造网络客户端 |
| tests/acceptance/test_credential_pool_fetch_state.py | 修改 | LangSearch 搜索入库后由既有 Fetch provider 获取正文 |
| tests/integration/test_live_langsearch.py | 新增 | 独立、显式启用、LangSearch-only 的实网 smoke |
| tests/unit/test_live_langsearch_validation.py | 新增 | 离线验证 smoke 的正向证据、隔离、预算与失败判定 |
| tests/docs/test_documented_config.py | 修改 | 默认禁用、凭证、5/1 窗口、名单、数量、日期和能力断言 |
| config.example.toml | 修改 | 默认禁用的凭证数组示例 |
| README.md | 修改 | 能力、启用方式、账号限制与专用 smoke 命令 |
| docs/provider-rate-limits.md | 修改 | LangSearch 来源行及 26 Web / 33 服务计数 |
| CHANGELOG.md | 修改 | Unreleased 下描述用户可见的新增能力 |

以下文件只读复用：providers/web/anysearch.py、brave.py、common.py，providers/diagnostics.py、http.py、pooled.py，tests/support/http.py、web_diagnostics.py、logging.py，tests/runtime/conftest.py，tests/acceptance/test_search_cost_arxiv_proxy_cli.py、test_gateway_workflows.py，以及 .github/workflows/ci.yml。

### 验证约定

1. 先加载当前适用指令，确认仍在上述工作树并检查并发修改。修改既有文件前读取最新 SHA-256；不覆盖其他人的工作。
2. 离线测试显式设置 WEB_SEARCH_RUN_INTEGRATION=0。真实凭证只能用于 Task 7 的专用 smoke。
3. 每个实现用例单独经历“写测试、观察正确失败、最小实现、验证、重构”；参数表按行为逐项加入，不先实现整张表。
4. 首个行为测试通过现有 registry.get("langsearch") 并 assert 非空后调用 factory，因此初次失败是“供应商尚未接入”的断言，而非新模块导入错误。
5. Task 5–6 验证既有基础设施的复用。若首次运行已通过，如实记录；不为制造 RED 修改核心代码。只有暴露真实适配器缺口时才新增对应 RED/GREEN 周期。
6. 下文“全套回归”统一指以下离线命令；实现任务完成 GREEN 和必要重构后执行一次。文档及最终检查不增加无意义的重复测试。

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest -v
~~~

首次实现前按项目环境准备：

~~~bash
uv sync --locked --all-groups --extra mcp
~~~

只同步既有锁定依赖；不得更新 pyproject.toml 或 uv.lock。

## Task 1: 接入最小搜索路径并同步注册契约

**Files:**

- Create: src/agent_search_gateway/providers/web/langsearch.py
- Create: tests/providers/web/test_langsearch.py
- Create: tests/fixtures/providers/langsearch/search.json
- Modify: src/agent_search_gateway/providers/defaults.py（imports 与 build_default_registry 的末尾）
- Test: tests/providers/test_registry.py（末尾 baiduqianfan 顺序断言与新增注册契约）
- Test: tests/runtime/test_runtime_assembly.py（完整注册能力列表）
- Test: tests/docs/test_documented_config.py（注册全集、示例名单、能力与速率来源）
- Docs: config.example.toml、README.md、docs/provider-rate-limits.md、CHANGELOG.md

注册全集与文档表格由现有测试绑定，因此这些伴随更新属于同一个可验收改动。只做新增 LangSearch 所必需的文档与枚举更新。

- [ ] **Step 1: Write the failing test**

首先新增 test_langsearch_posts_metadata_only_request，通过 build_default_registry().get("langsearch") 取得注册项并断言非空，再用 factory 构造真实适配器。用 KeywordSearchProvider 协议收窄类型后调用 search，不 mock search 或 registry。

Setup：RecordingJsonExecutor([synthetic_payload])、SecretValue("[REDACTED_SECRET]")、显式 api_url。fixture 包含整数 code=200、data.webPages.value 的两条有序记录，以及可忽略的 usage/log_id/datePublished。

Action：调用 search，query 使用包含 Unicode、标点、连续空格及首尾空格的字符串，证明适配器不自行改写输入。

Expected：

- 唯一请求为 POST，stage="search"，URL 为 endpoint(api_url, "/v1/web-search")。
- Bearer header 正确；json_body 整体等于 {"query": query, "count": 10}，不含 contents、freshness、includeDomains、excludeDomains。
- URL/name/snippet 映射成 KeywordSearchHit.url/title/snippet，顺序不变，raw_content == content == ""。
- 用参数化 api_url 覆盖官方根地址、尾斜杠以及 https://gateway.example.test/prefix/，追加路径分别正确。
- 在真实 HttpJsonExecutor + MockTransport 的小用例中检查 JSON Content-Type；RecordingJsonExecutor 只证明适配器传给 executor 的参数。

再逐项添加注册和文档契约断言：langsearch 唯一、位于 baiduqianfan 后、Search=True/Fetch=False、requires_api_key=True、supports_optional_api_key=False、allowed_config_keys 与 credential_option_keys 均为 {"api_url"}。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py::test_langsearch_posts_metadata_only_request -v
~~~

预期：registry.get("langsearch") 返回 None，注册存在性断言失败。新文档/枚举断言分别因缺少 LangSearch 项而失败；不把 collection、语法或 fixture 拼写错误作为 RED。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
LangSearchAdapter.__init__ stores name, api_url, secret and injected JsonRequester
search(query):
    payload = await requester.request_json(
        POST, endpoint(api_url, "/v1/web-search"),
        stage="search",
        Bearer secret,
        JSON exactly {query, count: 10})
    read data.webPages.value
    map ordered url/name/snippet into KeywordSearchHit
    leave both body fields at their existing empty defaults
    return hits
~~~

本步只建立成功路径；Task 2–4 逐项驱动严格验证、候选容错与诊断，不提前增加无用 helper 或供应商功能。

在 defaults.py 追加 WebProviderRegistration("langsearch", ProviderCapabilities(search=True, fetch=False), LangSearchAdapter, frozenset({"api_url"}))，保留默认 required credential 行为及所有已有注册顺序。

同步以下精确契约：

- test_registry.py 中原 registrations[24:] == ["baiduqianfan"] 改为 ["baiduqianfan", "langsearch"]；新增 LangSearch 自身 factory/能力/唯一性断言。既有前 20、20:24 分段不变。
- test_runtime_assembly.py 的完整列表追加 ("langsearch", True, False)，不改变该测试原有“已启用 provider”列表。
- test_documented_config.py 的速率表总数由 32 改为 33，新增 LangSearch 核验日期 2026-11-28；保留 baiduqianfan 的 2026-11-21 及其他原日期。
- 更新示例完整名单、默认禁用 provider 示例与 endpoint/key 映射、README 能力行；不要把 LangSearch 加入同时启用 Search/Fetch 的四供应商参数表。
- 为 LangSearch 单独验证启用后的 rate window 为 [(5, 1)]。默认禁用时 resolved credentials 为空，不能只依赖会跳过该成员的现有窗口循环。
- README 中服务总数 32→33；rate 文档 25 Web→26 Web、32 总服务→33。保持其他类别与 Fetch 数量不变。

示例结构（凭证环境变量引用按紧随其后的说明补入）：

~~~toml
[web_providers.langsearch]
enable_search = false
enable_fetch = false

[[web_providers.langsearch.credentials]]
api_url = "https://api.langsearch.com"
rate_limits = [{ requests = 5, period_seconds = 1 }]
~~~

在上述 credentials 表中设置 api_key_env，其值必须是环境变量名称 LANGSEARCH_API_KEY，而非实际令牌。示例不设置 max_concurrency 或 min_interval_seconds。README 与 rate 行解释：只调用 REST Search；固定 count=10，省略 contents；需要显式启用后重启 daemon；既有 credential_strategy/use_proxy 可用；5 RPS 是成员配置示例，同账号 keys/MCP 共享上游额度，实际账号可能不同，TPM/TPD 不由本地请求窗口保证。429 耗尽日额度时不能承诺短退避恢复。CHANGELOG 只加 Unreleased 记录，不发布版本。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py tests/docs/test_documented_config.py -v
~~~

预期：成功请求、注册与文档契约全部 PASS。然后执行前述全套回归；实网测试保持 skip。

- [ ] **Step 5: Refactor (keep tests green)**

仅整理新模块的命名、导入和 fixture；保持通用 helper、其他适配器与核心不变。保留“正文为空”的明确断言。重新运行本任务测试；如发生代码重构，完成全套回归后再继续。

## Task 2: 严格验证业务状态与响应信封

**Files:**

- Modify: src/agent_search_gateway/providers/web/langsearch.py
- Test: tests/providers/web/test_langsearch.py
- Docs: docs/designs/error-handlings/20261128-langsearch.md（只读依据）

- [ ] **Step 1: Write the failing test**

逐项加入 test_langsearch_rejects_invalid_success_envelope 的参数用例：

| 条件 | 预期 |
|---|---|
| root 为 null、list、string、number | ExecutionFailure |
| code 缺失、null、True/False、"200"、200.0、对象 | ExecutionFailure |
| code 为非 200 整数或错误字符串，例如 429、"429" | ExecutionFailure，不得变成成功空数组 |
| data 或 webPages 缺失/null/非对象 | ExecutionFailure |
| value 缺失/null/非 list | ExecutionFailure |
| code 为整数 200 且 value=[] | 成功 [] |

保持一份 valid envelope，每个参数只替换一个被测层级。另加 test_langsearch_accepts_empty_results。对失败使用 pytest.raises(ExecutionFailure)，断言现有 ErrorCode.ALL_PROVIDERS_FAILED 及本地、稳定的错误上下文，不断言任意上游原文。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py -k "invalid_success_envelope or empty_results" -v
~~~

预期：Task 1 的成功路径不能正确拒绝全部错误类型，出现“未抛出预期异常”或错误异常类型的行为失败。若某个用例已由共享 helper 覆盖，记录初始通过，继续尚未覆盖的用例，不删除有效检查制造失败。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
root = require_object(payload, provider, "search", "response")
code = root.get("code")
if code is not an integer, is boolean, or differs from 200:
    raise failure(provider, "search", fixed local failure reason)
data = require_object(root.get("data"), ...)
web_pages = require_object(data.get("webPages"), ...)
results = require_list(web_pages.get("value"), ...)
parse results, including valid []
~~~

使用 isinstance(code, int) 且显式排除 bool，或等价的严格整数检查；不得用 int(code)、宽松 ==200 或 truthiness。不得通过 get(..., []) 隐藏缺失层级。HTTP 状态与 JSON decode 已由 executor 处理，本步不加 response_inspector、新异常类型或重试循环。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py -v
~~~

预期：所有信封边界和正常/空结果 PASS。随后执行全套回归。

- [ ] **Step 5: Refactor (keep tests green)**

仅消除新模块内已经出现的重复，保持字段名 data.webPages.value、search stage 和异常类型一致。重跑适配器测试；有重构时再完成全套回归。


## Task 3: 容错映射候选并报告准确计数

**Files:**

- Modify: src/agent_search_gateway/providers/web/langsearch.py
- Test: tests/providers/web/test_langsearch.py
- Read: src/agent_search_gateway/providers/web/brave.py、common.py
- Read: src/agent_search_gateway/providers/diagnostics.py

- [ ] **Step 1: Write the failing test**

新增以下单行为用例：

- test_langsearch_defaults_missing_metadata：name/snippet 分别缺失或 null 时映射为空字符串；空字符串保留。
- test_langsearch_ignores_response_body_fields：记录夹带 text、raw_content、content、日期及其他元数据，两个正文字段仍为空；不从 text 合成 snippet。
- test_langsearch_keeps_valid_candidates_in_order：一个有效结果数组内包含合法与不合法记录，仅丢弃不合法项。
- test_langsearch_preserves_duplicate_urls：相同 URL 的两条记录均按上游顺序返回；包含大小写/fragment 的 URL 不由适配器归一化。
- test_langsearch_reports_candidate_counts：使用 caplog DEBUG，断言恰好一个 provider_candidates_summary，provider="langsearch"、stage="search"、phase="adapter"。
- test_langsearch_accepts_all_rejected_candidates：合法数组全部为坏记录，成功返回 [] 并准确记录拒绝，不能抛整个供应商失败。

计数用例可使用恰好八条记录：合法 A、null、{}、{"url": ""}、{"url": 7}、合法 URL 但 name=[]、合法 B、合法 URL 但 snippet=False。预期 received=8、accepted=2、rejected=6、rejection_counts={"missing_url": 2, "invalid_record": 4}、processed=8、unprocessed=0、rewritten=0。另验证 value=[] 时只记录一次全零计数。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py -k "metadata or body_fields or candidates or duplicate or counts" -v
~~~

预期：此前实现尚未具备完整候选容错或计数，行为断言失败。每次只补当前用例要求，不改变合法信封与空成功的含义。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
counts = CandidateCounts(len(results))
hits = []
for item in results:
    try:
        result = require_object(item, ..., "result")
        url = non_empty_string(result.get("url"), ..., "result.url")
        title = optional_string(result.get("name"), ..., "result.name")
        snippet = optional_string(result.get("snippet"), ..., "result.snippet")
        append KeywordSearchHit(url=url, title=title, snippet=snippet)
    except ExecutionFailure:
        counts.reject(candidate_rejection_reason(item, "url"))
        continue
    counts.accept()
emit existing provider_candidates_summary with counts.as_fields()
return hits
~~~

只捕获单条记录验证产生的 ExecutionFailure，不捕获取消，不把信封失败变为部分成功。保持 strict envelope / tolerant records 两层边界。仿照 BraveAdapter 的日志字段，使用现有 log_event。

不修改 keyword_hits_from_results 为它增加 title_key 参数，不复制 URL 归一化、有效性过滤、去重或入库逻辑。非空 URL 的最终有效性由编排器判断。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py tests/providers/web/test_anysearch.py tests/providers/web/test_brave.py -v
~~~

预期：候选容错、可选元数据、空正文、保序及计数全部 PASS，已有参考适配器不受影响。随后执行全套回归。

- [ ] **Step 5: Refactor (keep tests green)**

新模块内确有重复时提取小的局部映射函数；不抽取跨供应商通用层。重跑本任务测试；有重构时再完成全套回归。

## Task 4: 接入安全诊断并证明传输错误仍由 executor 处理

**Files:**

- Modify: src/agent_search_gateway/providers/web/langsearch.py
- Test: tests/providers/web/test_langsearch.py
- Read: tests/support/web_diagnostics.py、tests/support/logging.py
- Read: tests/providers/test_http_executor.py、tests/observability/test_provider_diagnostics.py

- [ ] **Step 1: Write the failing test**

使用真实 HttpJsonExecutor、httpx.MockTransport 及 RetryPolicy(1, 0, 0, 5)。RecordingJsonExecutor 不执行 response_diagnostics 回调，不能用于证明以下行为。

新增 test_langsearch_classifies_response_diagnostics：

| 响应 | 分类 |
|---|---|
| 无法解析 JSON | category=decode，semantic_result=unknown |
| 非对象 root、缺失/错误类型的成功码、code=200 的坏层级 | category=invalid_envelope，semantic_result=unknown |
| 明确非成功整数码或错误字符串码 | category=provider_response，semantic_result=error |
| 严格成功信封，包括空数组 | semantic_result=success，response_kind=object |

字符串 "200" 不视为成功，按坏成功码处理；HTTP 非成功状态仍采用 executor 的终态分类。不要把诊断分类当成请求成功的判断条件。

新增 test_langsearch_selects_safe_error_message：

- 错误码分别为整数与字符串。
- message 是字符串时优先使用，包括合法空字符串；否则使用字符串 msg；两者均非字符串时不采集消息。
- 故意加入 object/list 消息、usage、log_id、任意 nested error 和认证哨兵。只选已知 code/message 标量，不遍历任意字段。
- 比较诊断时遵守 DiagnosticText 类型；通过现有 redactor/rendered log 验证截断和脱敏，不要求内部诊断对象在渲染前已经把秘密替换掉。
- 异常 message 采用固定本地理由，不拼接上游消息。通过 without_debug_body_previews 隔离现有有界 body preview，不能误把“禁止新增任意诊断字段”扩大为禁用现有预览。

新增代表性边界：

- invalid JSON、HTTP 401：现有 ExecutionFailure；不加入适配器层重试。
- HTTP 429 后成功：用 RetryPolicy(2, 0, 0, 5) 证明走现有 executor 的两次物理尝试；更完整的 Retry-After/状态/网络错误矩阵复用已有测试。
- HTTP 200 携带失败业务码：即使 HTTP retry.max_attempts=2，也只有一次本成员物理发送；解析失败发生在 executor 返回 JSON 后。
- 局部诊断提取意外抛出普通异常时，不能遮蔽原本的 ExecutionFailure。有效搜索不因诊断失败而失败；复用已有 best-effort 测试惯例，不复制整套取消与日志矩阵。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py -k "diagnostic or error_message or http or decode or business" -v
~~~

预期：尚未连接的 LangSearch response extractor 无法提供所需分类和 message/msg 选择；相关断言失败。共享 executor 已经支持的错误分支可以初始为绿。

- [ ] **Step 3: Describe the minimal implementation**

在 langsearch.py 内新增 _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]，通过 request_json 的 response_diagnostics 参数注入。

~~~text
response extractor:
    try response.json(); decode error -> decode/unknown
    non-object -> invalid_envelope/unknown
    classify strict success code and known error-code scalar
    for explicit business failure:
        return provider_response/error plus safe selected scalar diagnostics
    strict code=200 and data/webPages/value valid:
        return success/object
    otherwise:
        return invalid_envelope/unknown

local error-field selection:
    message = root.message if it is a string, else root.msg if it is a string
    pass only {"code": root.code, "message": message} to selected_error_fields
    use existing safe_diagnostic_fields / best_effort_diagnostics boundary

semantic failure:
    raise failure(name, "search", fixed reason,
                  diagnostics=best_effort_diagnostics(local scalar selection))
~~~

复用 selected_error_fields，局部将 msg 归入 message 输入，不修改 shared helper。安全选取仍由通用路径处理 bool、错误类型、长度和脱敏。诊断字段中不新增 usage/log_id，不保留原始 headers，不把供应商要求的其他日志字段扩展到公共模型。

不加入 SDK、HTTP 客户端、response decoder、供应商重试循环、特殊 429 处理或新的退避参数。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/providers/web/test_langsearch.py tests/providers/test_http_executor.py tests/observability/test_provider_diagnostics.py tests/observability/test_redaction.py -v
~~~

预期：分类、msg fallback、安全字段和 HTTP/业务错误边界全部 PASS。随后执行全套回归。

- [ ] **Step 5: Refactor (keep tests green)**

只整理新模块里的重复状态判断与安全字段选择，保持真正的响应验证为权威路径、诊断为 best effort。重跑本任务测试；有重构时再完成全套回归。

## Task 5: 验证配置、运行时、doctor 与凭证 fallback 的通用接入

**Files:**

- Test: tests/unit/test_config_web_providers.py
- Test: tests/runtime/test_new_web_runtime_assembly.py
- Test: tests/runtime/test_pooled_providers.py
- Test: tests/doctor/test_no_network.py
- Read: tests/runtime/conftest.py、src/agent_search_gateway/providers/pooled.py
- Production: 预期无需额外修改

- [ ] **Step 1: Write the failing test / focused reuse regression**

配置：在现有三个 Search-only 参数表追加 langsearch，复用 test_service_key_search_config_rejects_invalid_provider、test_service_key_search_config_preserves_only_explicit_options、test_disabled_service_key_search_provider_skips_credentials。覆盖缺 api_key_env、缺/空环境值、不支持 Fetch、未知配置/凭证选项、不合法窗口、禁用时不读取秘密。策略和 endpoint 的验证保持原有组件边界：配置解析接受的可省略字段不等于适配器构造也可省略，不新设 LangSearch 默认值。

运行时：复用 _ClientFactory、_build_runtime 及只有一个成员时的 only_member。

- 未配置/禁用时不构造 LangSearch，不发生网络请求。
- 单成员配置生成一个 LangSearchAdapter，所注入 executor 的 provider_name、endpoint、secret 和成员 rate gate 来自现有装配；显式 Search-only，不进入 runtime.web_fetch_providers。
- 两凭证只暴露一个名为 langsearch 的逻辑 Search provider。给两成员不同 endpoint 和合成 secret，用两次实际 search 的 MockTransport 请求证明对应注入正确；默认 round_robin 依次使用两成员。
- 显式凭证数组省略所有限制时，配置中的 max_concurrency 为 None、rate windows 为空且无 min_interval，不添加隐藏 5 RPS。注意 _provider_config 现有 helper 默认放入 max_concurrency=7：本用例必须替换为完整显式 credentials，不能把 helper 的测试默认值误判为产品默认值。
- Runtime.aclose 后所创建客户端均按既有资源约定关闭一次。helper 还创建 LLM 客户端，不把总客户端数武断断言成 Web 成员数。

凭证池：在 test_pooled_providers.py 用两个真实 LangSearchAdapter、HttpJsonExecutor 与 CredentialMember 包装成 CredentialPool[WebAdapter] / PooledWebProvider，设置 fill_first 使顺序确定。仅补四个接缝用例：

| 首成员结果 | 预期物理发送 |
|---|---|
| 合法空数组 | 仅 A，返回 [] |
| 混合记录但有有效结果；以及全坏记录形成合法 [] | 仅 A，返回其解析结果 |
| HTTP 503，既有两次尝试耗尽；B 成功 | A、A、B |
| HTTP 200 失败业务信封；B 成功 | A、B，不再重试 A 的解析失败 |

doctor：复用 ForbiddenClient / forbidden_runtime_build 的测试方式，以合法环境引用启用 LangSearch 后 run_doctor 通过；不创建 HTTP 客户端、不构造 Runtime、不发送供应商请求、不输出合成 secret。此配置不启用 open_websearch，避免混入它已有的健康检查。

- [ ] **Step 2: Run test to verify it fails or confirm existing behavior**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/unit/test_config_web_providers.py tests/runtime/test_new_web_runtime_assembly.py tests/runtime/test_pooled_providers.py tests/doctor/test_no_network.py -v
~~~

这些是既有机制的接入回归，Task 1–4 正确时应直接通过。若失败，记录实际失败断言，区分 fixture 漏填 api_url、错误能力开关和真实产品缺陷；不修改核心来迎合错误测试设置。

- [ ] **Step 3: Describe the minimal implementation**

预期只扩展测试参数或添加上述 focused tests。配置、runtime、doctor、pool 的生产代码保持原样；现有注册及适配器已提供接入点。若实际发现设计假设不成立，先定位具体矛盾，再处理限定在新适配器/注册中的缺口，不静默扩展生产改动面。

通用 round_robin/fill_first、公平性、速率按尝试计费、并发、等待预算、取消、proxy/TLS 等继续由既有测试矩阵证明。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

重跑 Step 2 命令；预期配置、装配、doctor 和四个凭证池接缝 PASS。随后执行全套回归。

- [ ] **Step 5: Refactor (keep tests green)**

只在测试内复用已有配置/transport helper，合成请求的 endpoint、header、结果断言保持清晰。不为测试新增生产 API，也不扩大共用测试 helper 的职责。重跑受影响测试；有代码重构时再执行全套回归。


## Task 6: 验证搜索准入与后续显式 Fetch

**Files:**

- Test: tests/acceptance/test_credential_pool_fetch_state.py
- Read: tests/acceptance/test_search_cost_arxiv_proxy_cli.py
- Read: tests/acceptance/test_gateway_workflows.py
- Production: 预期无需额外修改

- [ ] **Step 1: Write the failing test / focused reuse regression**

新增 test_langsearch_search_requires_explicit_fetch，使用现有 _gateway、_filtering_cli、_successful_path、_completion 及 RuntimePaths 测试 fixture。

Setup：

- web=("langsearch", "jina")。
- 显式 web_options["langsearch"]["enable_fetch"]=False；_gateway 默认同时启用两种能力，不能遗漏这个覆盖。
- Jina 只启用 Fetch，使用合成 endpoint https://jina.test。
- MockTransport 对 /v1/web-search 返回严格成功信封，唯一命中为 _TARGET、name="Article"、snippet="Abstract"，额外夹带不应被采用的 text/content。
- Jina 请求返回合成正文；LLM 请求交由现有 _completion。所有 HTTP 均由 MockTransport 接管。
- 可参数化 CLI provider 参数为空或 ["--provider", "langsearch"]，覆盖正常默认选择和显式选择。

Action / Expected：

1. 通过真实本地 socket 路径执行 keyword-search。
2. 结果 JSONL 保持现有 {"url", "abstract"} 合同；store 中该 URL 状态为 URLStatus.NEED_FETCH，raw_content 和 content 都为空。
3. 搜索阶段没有 Fetch、judge、clean 或 safety 请求；LangSearch 的唯一 HTTP 请求是搜索请求。runtime.web_fetch_providers 不含 langsearch。
4. 对同一 URL 执行 url-fetch；断言既有 Jina provider 实际被调用，沿用既有正文处理和返回行为。
5. LangSearch 搜索请求计数没有增加，也没有 LangSearch Fetch 请求；已就绪结果保持既有缓存语义。
6. context manager 退出后本测试创建的 socket 与客户端按既有 helper 完成清理。

- [ ] **Step 2: Run test to verify it fails or confirm existing behavior**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/acceptance/test_credential_pool_fetch_state.py -k langsearch -v
~~~

预期：此前适配器与注册正确时，该接入验收直接通过。若 body 或能力边界断言失败，将其作为适配器/注册的真实缺陷，不更改 URLStore、搜索编排或 Fetch 状态机。

- [ ] **Step 3: Describe the minimal implementation**

复用现有离线 acceptance support，新增上述测试与一个局部合成响应即可。不要建立另一套 daemon、CLI 或 MCP 测试协议，不调用任何真实 Fetch 或 LLM 服务。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/acceptance/test_credential_pool_fetch_state.py tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_url_fetch_flow.py -v
~~~

预期：LangSearch 搜索只准入元数据，显式 Fetch 由现有支持者执行；既有选择、状态与正文流程 PASS。随后执行全套回归，其中包含现有 CLI 和 MCP 测试。

- [ ] **Step 5: Refactor (keep tests green)**

保持业务断言位于测试本身；仅复用现有响应、生命周期与 CLI helper，不新增核心测试开关。重跑受影响测试；有代码重构时再执行全套回归。

## Task 7: 建立 LangSearch-only 的 opt-in live smoke

**Files:**

- Create: tests/integration/test_live_langsearch.py
- Create: tests/unit/test_live_langsearch_validation.py
- Docs: README.md（加入精确运行命令和成功判定）
- Read: tests/integration/test_live_baiduqianfan.py、tests/unit/test_live_baiduqianfan_validation.py
- Read: tests/acceptance/test_gateway_workflows.py、src/agent_search_gateway/cli.py
- Production: 无修改

- [ ] **Step 1: Write the failing test**

先给 smoke 本身写离线验证，复用已有“integration 模块内可注入 _run；unit 模块调用 _run”的模式，令 _run 接收环境映射、明确的 RuntimePaths 和可注入 HTTP client factory。在 unit 用例中始终传 MockTransport；不读取真实用户凭证、不访问网络。

建议新增用例：

- test_live_langsearch_requires_a_key_without_opening_clients：无/空白 LANGSEARCH_API_KEY 时 skip，客户端尚未创建；仅凭证缺失可按既有惯例 skip。
- test_live_langsearch_proves_retrieval_through_cli：正常成功信封，经真实 Runtime、独立 ForegroundDaemon、CLI run_command 和 socket 返回结果文件；有真实模拟 HTTP 调用证据、至少一个已准入的合理 HTTP(S) URL、有效元数据和空正文。
- test_live_langsearch_empty_results_are_not_positive_retrieval：第一条查询为空时最多再尝试一条预定普通查询；两次仍空即报告未能验证正向检索，不宣称 PASS。
- test_live_langsearch_rejects_failed_or_unusable_results：401/429、无效 JSON、业务失败、全部坏记录或只有无法准入的记录，不能成为成功 smoke；打印安全的状态/计数/类别，不打印原始异常或 payload。
- test_live_langsearch_uses_only_the_selected_provider：所有请求必须是 LangSearch Search 的预定 host/path；没有真实 LLM、Fetch 或其他供应商请求，显式 CLI 参数包含 --provider langsearch。
- test_live_langsearch_has_a_bounded_total_budget：用迟滞的 MockTransport 和缩短的测试预算证明超时会取消操作、关闭本测试 Runtime/daemon；不等待真实长时间 Retry-After。
- test_live_langsearch_keeps_credentials_private：在失败响应中放合成凭证哨兵，结果报告和异常文本不泄漏它。

先让缺少 smoke runner 或尚无正向检索判定暴露正确失败；只有测试框架/接口的最小声明不足以算完成行为。不要将未实现模块的 collection 错误记为完成 RED；先使测试能够定位可调用 runner，再用实际断言证明缺口。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/unit/test_live_langsearch_validation.py -v
~~~

预期：尚未实现的 smoke 证据/预算/隔离行为导致明确断言失败。integration 模块的 opt-in 标记不应阻止 unit 模块离线验证 runner。

- [ ] **Step 3: Describe the minimal implementation**

采用现有 WEB_SEARCH_RUN_INTEGRATION == "1" 模块级 skip guard。test_live_langsearch_search 使用 pytest tmp_path 构造 RuntimePaths(tmp_path / "config.toml", tmp_path / "daemon.sock", tmp_path / "results")；不重设 HOME、不使用用户默认 socket、不停止现有 daemon、不改用户配置。

~~~text
_run(environment, paths, http_client_factory):
    check LANGSEARCH_API_KEY exists and is not blank, without printing it
    construct an isolated resolved config:
        one enabled Web Search provider: langsearch
        enable_fetch=false
        one credential: official api_url, LANGSEARCH_API_KEY, 5/1 member window
        no other enabled Web/academic/OA/Agent providers
        use only required inert LLM configuration for resolve_config
        retry: max_attempts=2, base_delay_seconds=0.25,
               max_delay_seconds=2.0, request_timeout_seconds=15.0
    build production Runtime with the injected HTTP client factory
    start an independent ForegroundDaemon using existing lifecycle support
    within a total asyncio.timeout budget:
        parse ["keyword-search", ordinary_query, "--provider", "langsearch"]
        await production cli.run_command(..., paths, isolated stdout/stderr)
        validate successful CLI outcome and JSONL
        confirm observed successful upstream HTTP request and admitted usable result
        confirm store state NEED_FETCH and empty body
        only for valid-but-empty retrieval, allow one predefined alternative query
    report safe scalar evidence and ensure owned resources are closed
~~~

实施要点：

- 直接使用 build_parser / run_command 与显式 paths。不要调用 _filtering_cli 做实网请求，它在 test_gateway_workflows.py 中固定只有 2 秒超时。_running_daemon 可以复用其启动/清理管理，但实网请求使用独立总预算，例如 90 秒。
- HTTP 客户端 factory 兼容 Runtime 传入的 kwargs，并保留既有 proxy/TLS 配置；不覆盖共享系统代理。
- 需要的未使用 LLM 配置采用合成环境值和保留测试域名；任何 LLM 请求都让测试失败。不能因为 config 需要 LLM 节点便调用真实模型。
- 用 request/response hooks 记录 attempt 数、HTTP status、允许的 host/path，成功响应与 JSONL/URLStore 一同构成证据；不输出 Authorization、原始消息、完整响应或账号额度。
- 限制最多两条普通查询、固定 count=10、不请求全文。HTTP/业务失败不通过换查询反复消耗额度；429 日额度耗尽明确列为限制。
- 外层总预算包含重试和 Retry-After 等待；它只用于 smoke，不改变产品默认 timeout 或 retry policy。
- 凭证存在但网络、额度、schema 或检索证据失败时报告失败/明确限制，不把它转换为“跳过因此通过”。合法空数组依旧是适配器成功，只是没有完成正向实网检索验收。

README 加入以下专用命令，明确不要启用 integration 后运行整个 tests/integration 目录：

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run --extra mcp pytest tests/integration/test_live_langsearch.py -v -rs -rP
~~~

该测试内部的生产 CLI 调用必须使用 --provider langsearch；不手工启动默认配置中的全提供商 fan-out。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

先离线：

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest tests/unit/test_live_langsearch_validation.py tests/integration/test_live_langsearch.py tests/docs/test_documented_config.py -v
~~~

预期：unit 验证 PASS；live test 因 opt-in guard SKIP。执行全套离线回归，再运行上面的 LangSearch-only live 命令。

实网成功标准：确有成功上游调用，至少一个正常准入的 URL 命中，标题/摘要经既有映射与 staging 使用，正文为空，未调用其他供应商，独立 daemon 退出完成。仅“命令退出 0”或“响应合法为空”不足以声称正向检索验证成功。

- [ ] **Step 5: Refactor (keep tests green)**

复用已有生命周期和输出解析方式；不要为一个 smoke 引入通用测试框架、持久状态或新生产配置。离线验证保持可重复。若只调整报告文案，不重复消耗实网额度；只有影响请求或验收语义的修改才重跑对应 live check。

## 最终质量门与交付

这是实现完成后的验收清单，不代表本计划编写阶段已经执行这些命令。

- [ ] 确认全部 Task 1–7 行为测试和 spec 映射完成，未把初始为绿的基础设施回归伪报为 RED。
- [ ] 完成与现有 CI 对齐的离线验证：

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest -v
uv run --extra mcp ruff format --check src tests scripts
uv run --extra mcp ruff check .
uv run --extra mcp mypy src tests scripts
uv audit --locked --preview-features audit-command
uv run --group docs --extra mcp python scripts/build_docs.py
uv build
~~~

全套已经在同一个最终代码状态通过时，不为了清单重复执行；引用该次结果。uv audit 如遇数据库/网络不可用，应准确报告，不改锁文件来隐藏问题。

- [ ] 检查文档构建产物 site/index.html 和 site/api/agent_search_gateway.html 存在，首页没有未替换的 {{PROJECT_VERSION}}。这是既有 CI 的构建验收，不手工编辑生成的 site 文件。
- [ ] 检查构建 wheel 包含 agent_search_gateway/providers/web/langsearch.py，现有 package / secret-scan / 跨 Python 与 OS 矩阵继续由 .github/workflows/ci.yml 执行；不更改 CI。需要本地复现 wheel 安装时只使用隔离 UV_TOOL_DIR / UV_TOOL_BIN_DIR，不能覆盖用户安装。
- [ ] 实网 smoke 记录成功证据或明确阻碍。凭证未配置、网络不可用、额度耗尽、一直空结果分别说明；不声称未运行的检查通过。
- [ ] 审阅与 9c9361c 的差异：生产文件只有 langsearch.py 和 defaults.py；后者只有 import/末尾注册。无其他 adapter、核心、依赖或锁文件变化。
- [ ] 确认 README/config/rate 表、注册顺序、26 Web / 33 服务、LANGSEARCH_API_KEY、5/1 成员窗口和默认禁用一致；没有隐含并发上限。
- [ ] 按任务完成边界提交实现所需文件，仅暂存本次明确路径，不使用会包含其他 Agent 工作的全量暂存。此计划不授权发布、合并或清理其他工作树。
- [ ] 本地验收后在原工作树交付绝对路径、commit 与检查结果；不复制到其他环境。

## Spec 覆盖与类型自检

| 要求 | 实现/验证位置 |
|---|---|
| D1 独立 Search-only、单注册、原顺序 | Task 1；Task 5 运行时能力 |
| D2 固定 REST、count=10、原样 query、无额外参数；A1–A2 | Task 1；Task 7 实网 |
| D3 严格 code/data/webPages/value；A3、A6、A9–A10 | Task 1–2 |
| D3 逐项容错、可选字段、空成功、计数；A4–A8 | Task 3 |
| D4 通用 config/factory、一个逻辑 provider、CLI/MCP 无新路由 | Task 5–6；全套 CLI/MCP 回归 |
| D5 空正文与显式 Fetch | Task 1、3、6、7 |
| D6 无默认限额、每成员控制、成功不 fallback、终结失败 fallback | Task 5；已有 rate/pool/HTTP 回归 |
| D7 HTTP/业务错误边界、安全诊断；A11–A13 | Task 4；既有 observability/redaction/cancellation 回归 |
| D8 默认禁用、既有配置兼容、必要凭证 | Task 1 文档契约；Task 5 配置/doctor |
| 服务枚举、数量、核验日期和账号额度说明 | Task 1 |
| 独立 LangSearch-only smoke、阳性检索证据、限制说明 | Task 7 |
| 全套离线、质量、文档、构建及生产改动边界 | 最终质量门 |

统一使用以下现有类型与名称：

- LangSearchAdapter.search(query: str) -> list[KeywordSearchHit]。
- JsonRequester.request_json 的参数名为 json_body 和 response_diagnostics，不是 json 或 diagnostics。
- KeywordSearchHit 的字段为 url/title/snippet/raw_content/content；SearchOrchestrator 写出的公开搜索记录为 url/abstract。
- URLStatus.NEED_FETCH 表示搜索已准入但正文未准备；正常搜索不变成 NEED_JUDGE 或 READY。
- CandidateCounts 的统计来自 accept()/reject()/as_fields()；拒绝原因复用 missing_url/invalid_record。
- ExecutionFailure / ErrorCode.ALL_PROVIDERS_FAILED / ConfigFailure 均为既有类型。
- 成员的可选设置放在 credentials 内；use_proxy 与 credential_strategy 按现有 provider 级规则使用。
- 物理适配器 Search-only；共用 PooledWebProvider 的方法集合不等于该注册项启用的能力。

### 来源与计划审查记录

- 已完整阅读三份 LangSearch spec，并核对 AnySearch/Brave/common、注册、配置、runtime、凭证包装、搜索准入、测试 helper、文档契约和现有 CI。
- 官方请求、响应与错误格式于 2026-11-28 对照 [Web Search API](https://docs.langsearch.com/api/web-search-api)、[Search API guide](https://docs.langsearch.com/reference/search-api-guide)、[Errors](https://docs.langsearch.com/api/errors) 和 [Plan & usage](https://langsearch.com/overview)。速率文档的新增来源可同时引用 [Pricing](https://langsearch.com/pricing)。
- 自检重点：API code 必须是整数 200；message/msg 仅选合法字符串；错误字符串 "200" 不能成为成功；候选数组合法但无幸存项不能触发 fallback；适配器不得改写 URL；实网不能沿用 2 秒离线 CLI helper；禁用模板的速率配置须另行启用后验证。
- 现有 test_default_registry_adds_one_baiduqianfan_search_only_provider 与速率表全集约束已纳入 Task 1，避免新增注册后留下已知失败。
- 本计划无需改动现有 spec，也不依赖额外设计确认。若实现过程中发现与基线不同的并发修改，以当前源码重新定位，保留已确认的最小生产修改边界。
