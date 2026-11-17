# Testing: Doubao、You.com、Keenable 与 open-webSearch

- 日期：2026-11-17
- 状态：**设计已确认；2026-11-17 按方案 A 修订多引擎用例，未实现任何新测试**
- 关联：[架构](../architectures/20261117-doubao-youcom-keenable-openwebsearch.md) / [错误处理](../error-handlings/20261117-doubao-youcom-keenable-openwebsearch.md)

## 1. Testing Goals

验证四个独立提供商：

1. 都能经现有 Search / Fetch 协议正确完成一次成功调用。
2. 请求地址、方法、认证、最小请求参数和响应映射精确符合各自官方协议。
3. HTTP 与业务层错误分类正确，复用共享重试、日志、候选计数和调度回退。
4. 配置、registry、Runtime 只新增四个叶节点；`engines` 是 open_websearch 专属选项，沿用现有 options 透传与构造校验，不改变其他 Provider 或通用配置逻辑。
5. 能针对单家执行可选真实 API 冒烟；没有 Key 或未启动 daemon 时明确跳过/失败报告，不阻塞普通 CI。
6. 测试不输出或保存凭据、用户查询、网页正文等敏感数据。

## 2. Test Strategy

### 2.1 分层与路径

| 层次 | 候选文件 | 验证内容 | 是否访问外网 |
| --- | --- | --- | --- |
| Adapter unit | `tests/providers/web/test_doubao.py` | Web Search/Fetch 方法、Header/Body、字段、业务错误 | 否 |
| Adapter unit | `tests/providers/web/test_youcom.py` | Search、Contents 批量结构、Markdown URL 对齐 | 否 |
| Adapter unit | `tests/providers/web/test_keenable.py` | Keyed Search、GET Fetch、非空 Markdown、状态码 | 否 |
| Adapter unit | `tests/providers/web/test_open_websearch.py` | daemon 两端点、engines 省略/显式列表与固定总预算、构造校验/快照、局部失败诊断 | 否 |
| Protocol fixtures | `tests/fixtures/providers/{doubao,youcom,keenable,open_websearch}/` | 供应商的成功、失败、空内容、异常 schema 样本 | 否 |
| Registry/config | `tests/providers/test_registry.py`、`tests/unit/test_config_web_providers.py` | 名称、能力、Key、允许配置、独立开关 | 否 |
| Runtime | `tests/runtime/test_runtime_assembly.py` | 构造注入、共享 quota、双能力、关闭 executor | 否 |
| Errors/logging | `tests/observability/test_provider_diagnostics.py` 及 adapter 单测 | 分类、重试、脱敏、计数与终态 | 否 |
| Workflow | `tests/acceptance/test_gateway_workflows.py` | Search → URLStore → Fetch 回退；旧流程未变化 | 否 |
| Opt-in live | 项目现有 `tests/integration` 约定或专用可选脚本 | 四家 Search + Fetch 实际响应、部署与凭据说明 | 是（显式启用） |

优先使用项目已有 `RecordingJsonExecutor`、`httpx.MockTransport`、fixture、日志捕获工具，不建立第二套模拟 HTTP 库。

### 2.2 Fixture 原则

- 每家至少准备：Search 成功（含两条合法结果）、Search 合法空列表、Search 错误 envelope、Search 非法顶层 schema、Search 一条有效 + 一条错误记录、Fetch 成功正文、Fetch 空正文、Fetch 错误 envelope、非 JSON 内容。
- 共享 HTTP 案例使用 MockTransport 构造 401/402/408/422/429/500、超时和连接拒绝；验证全局 retry 次数、阶段/供应商信息。
- 测试数据采用中性假域名（如文档示例域名）和明确伪造的短正文，不能从真实请求复制凭据、受保护网页或完整敏感内容。
- 官方文档与实际响应有差别时，先取得**脱敏的最小 schema 样本**，核对后更新 fixture，再实现 parser；不先猜字段路径让测试强行通过。
- Mock 请求断言同时确认其他三家不额外设置开销较大的提取选项；open-webSearch 未配置时只发 query，显式配置时准确发送 engines 数组和固定 limit=50，Fetch 从不携带这两个 Search 字段。
- open-webSearch 另有 `search_partial_engines.json` 与 `search_all_engines_failed.json`：前者合法命中和 `partialFailures` 共存，后者合法空 results 和非空 `partialFailures` 共存；不能与“单条候选格式损坏”的 fixture 混为一谈。

## 3. Provider-Specific Cases

### 3.1 Doubao

| Case | 输入 / Mock | 期待 |
| --- | --- | --- |
| Search request | 一个中文查询 | `POST /web_search`，Bearer 认证，`Query`、`SearchType=web` |
| Search success | `Result.WebResults`，含 URL/title/摘要字段 | 映射两条 `KeywordSearchHit`，标题与摘要正确 |
| Search partial | 一条有效，一条缺 `Url` | 有效命中保留，拒绝计数准确 |
| Search empty | 合法 `WebResults=[]` | 返回空列表；不报 schema 错 |
| HTTP 200 business error | `ResponseMetadata.Error` 有值 | 供应商失败，而非空搜索 |
| Recoverable error | 官方限流/临时错误码 | `ProviderResponseFailure` 通过共享重试次数运行 |
| Permanent error | 参数/权限错误 | 不重试，安全诊断含错误类别 |
| Fetch request | 已规范化页面 URL | `POST /fetch`，字段 `Url` |
| Fetch success | `Result.Data.ContentText` 非空 | `URLFetchCandidate` 正文正确 |
| Fetch failure | 空 `ContentText`、原站错误状态 | 失败并允许下一个 Fetch Provider |
| Account limitation | 订阅 Key 无 Fetch 权限的模拟响应 | 终态权限失败；文档标注按量付费 Key |

### 3.2 You.com

- Search 请求必须指向 `ydc-index.io/v1/search`、采用 `X-API-Key`，具备 `query` 和固定保守 `count`，**不包含额外 `extraction`**。
- `results.web` 正常列表、空列表、部分坏条目、错误类型和内嵌错误分别覆盖；`results.web[].snippets` 是数组时应得到正确文本摘要，不能误返回列表字符串字面量。
- Fetch 必须 `POST /v1/contents`，传单个 `urls` 和 `formats=["markdown"]`。
- Contents 返回记录与目标 URL 对齐；无匹配 URL、`markdown=null`、空字符串、只有 HTML、非法数组项均不可被当作成功 Fetch。
- 401/402/429/500 模拟响应分别验证不重试/重试边界；凭据值不得进入日志。

### 3.3 Keenable

- `POST /v1/search` 带 `X-API-Key`、`query`、保守 `max_results`，结果字段按官方样本断言。
- `GET /v1/fetch` 使用 URL 查询参数；不发送 `live`，不改用 public 端点。
- Fetch 合法 Markdown 转为候选；`content` 缺失、空白、非法类型均失败。
- HTTP 402 信用额度不足、422 请求/提取失败均不重试；429/5xx 按共享全局策略重试。
- 只测试正式 keyed API，不能写“鉴权失败后访问公开接口”的用例。

### 3.4 open-webSearch

- 未配置 `engines`：`POST /search` 的 body 严格等于 `{"query": query}`；不发送 engines、limit 或 searchMode，保留 daemon 默认策略。
- 显式配置 `engines=["bing", "duckduckgo"]`：body 严格等于 `{"query": query, "engines": ["bing", "duckduckgo"], "limit": 50}`；只有一个 daemon 请求，没有逐引擎 fan-out。
- 用两个合成名称和重复项验证顺序/重复项原样保留；未知非空名称也转发，生产实现不内置引擎枚举、不将 `"all"` 展开。
- 参数化 `[]`、标量字符串、mapping、数字、bool、非字符串元素、空串/纯空白项：直接构造抛 `TypeError`，不发 HTTP；省略/None 合法。不要把空数组解释为全部引擎。
- 验证构造后修改原始 engines list 不影响请求；修改前一次记录到的请求 engines list 不影响后续请求，确认内部 tuple 快照与每次新 JSON list。
- 验证 `status="ok"` 及官方格式的 `data.results` 正常映射；多引擎结果仍只有 `provider=open_websearch`，不把 result.engine 转成 Gateway Provider。
- `status="error"` 即使 HTTP 200 且另有 results 也判失败；未知状态/非法 results 不因配置 engines 而绕过严格解析。
- 用真实 executor + MockTransport 验证 `status=ok`、合法命中、非空 `partialFailures`：保留命中，恰好一条响应诊断包含固定 `upstream_code="partial_engine_failures"` 与本地生成的 count 摘要，不新增重试。
- 同样覆盖合法空 results + 非空 `partialFailures`：仍返回 []，但失败标记不能消失；省略/空 partialFailures 时无该标记。不得把这一 PASS 解释成每个引擎成功。
- 在 partialFailures 的 engine/code/message 和 query 中放隐私哨兵，断言日志不含原文；不新增 `semantic_result="partial"` 或借用 candidate rejected 计数。
- Fetch 请求目标 `POST /fetch-web`，即使配置了 engines，body 仍严格为目标 URL；不含 engines、limit、searchMode，`data.content` 非空才能返回候选。
- 服务未启动/连接被拒绝时按共享传输重试有限次数后失败，不自动启动进程；HTTP 重试中的 engines/limit 保持一致，不偷偷去掉列表回退默认引擎。
- 模拟 daemon 报浏览器/Playwright 不可用：仍为该 Provider 失败，不安装依赖、不改选引擎；若 HTTP 200 业务错误没有已知可恢复定义则不重试，普通 5xx 仍走既有全局策略。

## 4. Shared Regression Matrix

| 关注点 | 断言 |
| --- | --- |
| 能力注册 | 四个 name 恰好各自出现一次，Search/Fetch 均可用 |
| Key 配置 | 前三家必须 `api_key_env` 可解析；open_websearch 不需要 Key |
| 选项允许键 | 前三家仅 api_url；open_websearch 为 api_url + engines；其他 Provider 仍拒绝 engines，live/count/limit 等未知键仍失败 |
| engines 值校验 | resolve_config 原样保留 list；已启用 adapter 构造错误由 Runtime 包装 ConfigFailure；完全禁用时不验证值，但未知键仍失败 |
| engines 透传 | TOML options → factory → 单次 Search 精确传递 list + 固定 limit=50；未配为 query-only，Fetch 为 url-only |
| 配置快照 | adapter 构造后输入 list 与旧请求 list 的修改不影响后续 Search；不声称 options MappingProxyType 已深度冻结 |
| 开关独立 | `enable_search=false` 不妨碍 Fetch，反之亦然 |
| 实例复用 | 同一 Provider 的 Search/Fetch 使用同一 adapter/executor 与 quota |
| 配额 | 无新的独立并发限制，现有 `max_concurrency` 有效 |
| HTTP 重试 | 408/429/5xx、timeout、transport error 按配置重试；非临时 4xx 不重试 |
| 业务 200 | 显式错误经 inspector 分类；非法 JSON 与无效 envelope 清楚区分 |
| 终态日志 | 有 `http_failed`，而不是对每次失败重复报“最终失败” |
| 局部结果 | 逐条跳过错误的 Search 记录并记录候选数/原因 |
| Fetch 回退 | 一家失败后既有 `FetchScheduler` 可试下一家 |
| URL 安全 | 非 URLStore 收录的任意 URL 无法直接触发抓取 |
| 脱敏 | 模拟密钥、敏感查询、长正文、URL query marker 不在捕获日志内 |
| 原有 Provider | 现有注册与正常测试仍通过，接口字段和 CLI 行为不变 |
| 资源关闭 | Runtime 关闭四个 provider 的 executor，不泄漏 AsyncClient |

## 5. Opt-in Live Smoke Plan

### 5.1 Preconditions

- 环境变量由操作者在运行环境设置；测试读取变量名称但**不输出凭据值**。
- Doubao Key 必须有 Fetch 所需的按量付费权限；没有时记录“Fetch 未获权限”，不把供应商功能断言为不存在。
- You.com / Keenable 账户应有可用的 API 配额/credits，测试需避免高频率与大批量请求。
- open-webSearch 的 HTTP daemon 已由用户启动；显式 engines 列表在 Gateway Provider 配置中选择，daemon 负责可用引擎/白名单/浏览器/网络。白名单应允许所选引擎，否则可能过滤或回退到默认引擎。
- live open-webSearch Search 用 `engines=["bing", "duckduckgo"]` 的两引擎小样本覆盖新分支，发送固定总预算 50；这是测试输入，不是生产默认列表。未配置分支由离线合同测试覆盖。一次 daemon 请求仍会触发多个底层任务，不能把它宣传成每个引擎各有 50 条。
- 仅在显式设置项目既有 live 测试开关（如 `WEB_SEARCH_RUN_INTEGRATION=1`）时触发真实请求；默认 CI 不调用付费 API。
- 使用低请求量、已知公开且安全的测试网站，不要求检索固定排序、网页完整内容或 LLM 固定措辞。

### 5.2 Per-provider Search

逐一运行 `doubao`、`youcom`、`keenable`、`open_websearch`：

1. 构建仅包含待测 provider 的隔离测试配置（不改写用户常用配置），确认启动与认证。
2. 发起一个低风险的简单关键词 Search。
3. 验证至少得到**格式有效**的结果、可解析 URL，并统计耗时/状态；不要求固定命中数或排名。
4. 失败时报告 Provider、阶段、HTTP/业务类别与是否可重试；凭据和敏感响应不得输出。
5. 对 open-webSearch 记录 daemon 版本及本次测试使用显式两引擎列表。优先核对上游有效 engines / partialFailures 的安全最小证据；没有逐引擎证据时只报告 Provider 级 PASS，不宣称两个引擎均成功。若白名单过滤/单默认引擎回退被发现，标记覆盖未验证并报告部署配置问题，而不是当作预期多引擎成功。`data.engines` 只是 daemon 解析后的请求名单；名单匹配且无 partialFailures 也不是逐引擎成功证明，`results[*].engine` 只证明该引擎贡献了对应命中。

现有 CLI `keyword-search "query" --provider NAME` 可用于指定**单个搜索提供商**；这并不意味着 Fetch 支持相同参数。

### 5.3 Per-provider Fetch

1. 在同一正在运行的 Gateway Runtime 中先通过 Search 收录一个可公开抓取的 URL；`url-fetch` 对未收录的 URL 会拒绝。
2. 使用**临时的测试配置**，使仅待测 Provider 的 `enable_fetch=true`（或者通过直接调用其适配器执行隔离 live 测试）；保留既有安全与认证设置。
3. 对该已收录 URL 发起 Fetch，检查 `URLFetchCandidate` 非空正文、编码与格式合理，短内容摘要足够，不打印原文。
4. 验证失败能正确分类，且不会被其他 Provider 的成功掩盖。
5. 不调用不存在的 `url-fetch --provider` 参数；Fetch 隔离取决于配置/adapter test。
6. 对 open-webSearch，如果 daemon 返回“浏览器组件不可用”等错误，记录为外部部署前提未满足，不修改本项目以安装其依赖。
7. Live 返回为动态网页，可能需要确认站点的访问许可与反爬限制；不通过连续重试付费端点来伪造成功。

### 5.4 Result Recording

为四家分别汇总以下字段，既可在测试输出，也可在维护者审阅记录中使用：

| 字段 | 示例含义 |
| --- | --- |
| Provider | 四个固定名称之一 |
| Search | PASS / FAIL / SKIP（原因） |
| Fetch | PASS / FAIL / SKIP（原因） |
| Request type | Search / Fetch |
| Failure category | HTTP status、transport、business error、invalid envelope、empty body |
| Retry observations | 预期重试次数是否与全局配置一致 |
| Dependencies | Key 权限、额度、daemon 是否在线 |
| Sensitive data | 一律不记录 |

不把具体网页正文、认证 Header、支付信息、完整搜索查询粘贴进 PR/测试日志。

## 6. Suggested Execution Order

1. **静态契约核对**：将上游官方 Search/Fetch JSON 结构与 fixture 校对，尤其是 open-webSearch `data`、Keenable Search，以及 Doubao Fetch 原站状态。
2. **Adapter 单测**：先成功与最小请求协议，再非法信封/业务错误/空正文/单条损坏；保持纯离线、确定性。
3. **基础设施回归**：HTTP 全局重试/日志、注册、配置解析、Runtime 装配和关闭。
4. **工作流验收**：Search 收录 URL、Fetch fallback、LLM 后处理保持原行为。
5. **全量离线测试与类型/格式检查**：执行仓库现有测试套件与项目定义的检查命令，修复新增缺陷。
6. **可选 live smoke**：凭据与 daemon 就绪时逐家单独 Search + Fetch；按 PASS/FAIL/SKIP 报告，不强制 CI 使用真实 API。

## 7. Exit Criteria

- [ ] 四个 Provider 注册、配置和接口均通过离线验证。
- [ ] 四个适配器对成功、合法空结果、局部坏条目、内嵌错误、非法 JSON、空白 Fetch 均有明确测试。
- [ ] 全局重试分类、HTTP 200 业务错误重试、脱敏日志和回退符合错误设计。
- [ ] Search → URLStore → Fetch 工作流不受影响，现有回归套件全部通过。
- [ ] 各 Provider Search + Fetch 的 live 结果被明确记录为 PASS/FAIL/SKIP；无法连接/没有权限时不谎报 PASS。
- [ ] open-webSearch 未配置时 query-only；显式配置时原样 engines + 固定 limit=50；Fetch 始终 url-only；Gateway 不托管 daemon 或执行子引擎。
- [ ] engines 非法结构、禁用项、Runtime 透传、不可变快照、单 Provider/quota 身份、局部失败安全诊断均覆盖；不硬编码引擎枚举。
- [ ] 示例配置提供高覆盖率名单并默认关闭，README 说明总结果预算、白名单过滤、重复项风险和局部失败，不承诺所有引擎都产生结果。
- [ ] 文档解释 Doubao Fetch 付费 Key 要求、Keenable Keyed API、You.com Contents 费用/域名、open-webSearch 运行要求及 CLI Fetch 限制。

## 8. 参考

- `tests/providers/web/`、`tests/fixtures/providers/`、`tests/providers/test_registry.py`。
- `tests/unit/test_config_web_providers.py`、`tests/runtime/test_runtime_assembly.py`。
- `tests/observability/test_provider_diagnostics.py`、`tests/acceptance/test_gateway_workflows.py`。
- Doubao：https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh
- You.com：https://you.com/docs/api-reference/search/v1-search
- Keenable：https://docs.keenable.ai/api-reference/search.md
- open-webSearch：https://github.com/Aas-ee/open-webSearch/blob/main/docs/http-api.md
- open-webSearch machine contract：https://github.com/Aas-ee/open-webSearch/blob/main/docs/architecture/cli-json-protocol.md
- engines/limit/partialFailures 的源码证据固定于上游 `2562c9233a945c18fa9d08b7e450ca218a8853b6`（见架构文档）；live 必须另外报告实际运行版本。
