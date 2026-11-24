# Architecture: 搜索成本控制、arXiv 检索、HTTP Provider 代理选择与 CLI 等待

- 日期：2026-11-22
- 状态：设计稿，待用户审核；未实施、未提交
- 基线：e712f10（main / v1.0.0）
- 工作树 / 分支：design-firecrawl-arxiv-provider-proxy-cli-20261122
- 配套文件：../error-handlings/20261122-search-cost-arxiv-proxy-cli.md；../testings/20261122-search-cost-arxiv-proxy-cli.md

## 1. Scope & Assumptions

### In Scope

1. **Firecrawl**：默认 Search 仅返回 Web 结果的 URL、标题、摘要，不调用 Search 内嵌 Scrape；Fetch 才显式请求页面正文。暴露少量有实际价值的 Search / Scrape 选项，并更新示例配置。
2. **arXiv**：将普通关键词转换为 arXiv 合法的字段布尔表达式，默认“关键词之间 AND；每个关键词的标题/摘要字段之间 OR”；保留引号短语；暴露查询字段、结果数和排序。
3. **代理**：所有基于 Gateway 内部 HTTPX 客户端的 Web、Academic、OA Resolver、LLM Provider，统一支持可选布尔字段 use_proxy。未配置或 true 时沿用系统环境代理，false 时直连。保持证书验证及已有证书来源。
4. **CLI**：所有业务请求默认无限等待守护进程响应，不再在 600 秒后放弃；保留显式传入的超时覆盖及现有 socket 请求/响应生命周期。
5. 更新相关测试、config.example.toml 和简短使用文档；不改变各工作流的公开输入输出结构。

### Out of Scope / Todo

- 不改变全局重试参数、知乎两个并行搜索分支、Provider 并发及限速策略。
- 不在本次实现 HTTPX/httpcore 连接池重建、替换底层传输或每次请求新建客户端；不修改知乎 Adapter。
- 不修改外部 Agent CLI 子进程的代理或其传入环境变量，且不为其增加 use_proxy。
- 不修改通用 Keyword/Paper/Fetch Pipeline、Daemon socket 协议、请求 ID 与文件格式。
- 不做 LLM arXiv 查询改写、额外检索轮次或新的 Paper Search CLI 参数。
- 不引入 Firecrawl 费用硬预算、credits 结算追踪或收费 API 的自动实时测试。
- 200 credits 单笔历史费用未关联到供应商后台明细，不宣称已经确定单次计费根因。

### Assumptions

- 本项目通过 Runtime 为每个 Provider 创建自己的 HTTPX AsyncClient；执行器使用传入客户端，不负责创建。主要代理实现点是 config.py + runtime.py，而不是逐个 Adapter。
- 当前 HTTPX 0.28.1、httpcore 1.0.9；默认会读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 和 SSL_CERT_FILE/SSL_CERT_DIR。
- 现有可接纳 URL 的语义、Fetch 的安全检查与清洗链不变。没有正文的 KeywordSearchHit 可以正常进入 URL Store。
- Firecrawl 的 parsers=[] 并非 PDF 零收费：它关闭 PDF 逐页文字解析，但遇到 PDF 仍可能以原始 base64 返回并收取固定费用；此输出不是 Gateway 所需正文。
- 知乎的代理失败与 PoolTimeout 关联是强假设：本地测试在代理 TLS 失败场景复现了连接池占满，但没有完整的历史代理环境证据；本次只提供明确的直连配置方式，不声称修复上游库。
- 所有新字段只作用于对应命名 Provider，不改变其余 Provider 未配置时的行为。

## 2. Architecture Summary

保持现有 **TOML → config.py 解析 → Runtime 构造 → HttpJsonExecutor → 各 Provider Adapter → 原有 Orchestrator → CLI/Daemon** 流程。通用 use_proxy 在配置解析为不可变 Provider 字段，Runtime 通过一个共享 HTTP Client 创建辅助函数实现独立于 Provider 的代理选择，保持每 Provider 一个长期可复用的连接池。Firecrawl 的 Search/Fetch 载荷与结果映射只在 FirecrawlAdapter 调整；arXiv 的字段查询编译只在 ArxivProvider 调整；CLI 默认等待只改 protocol.send_request，Daemon 原样执行。所有配置有明确默认值、严格校验和回归测试。

## 3. Design Decisions

### 3.1 Runtime Model

#### 决策 A：HTTP 客户端在 Runtime 统一配置代理

- **Description**：config.py 的 Web/Academic/OA/LLM 解析对象增加 use_proxy: bool = True；Web 与 Academic/OA 将其标记为 shared key，不传递给 Adapter options；LLM 从已引用的 Provider 表直接解析。Runtime 中复用一个客户端创建辅助函数，为每个 Provider 创建客户端，不触碰各 Provider 的请求代码。
- **Rationale**：已有 Runtime 创建点统一、各 Adapter 已共用 Executor；将控制权留在连接池构造时可维持连接复用。
- **Trade-offs**：内部配置数据类和 HTTP ClientFactory 注入点需要同步适配与测试。
- **Rejected Alternatives**：
  - 每个 Provider Adapter 各自设置代理：重复代码，耦合网络设置和业务转换。
  - 请求级短生命周期客户端：丧失连接复用，且无法直接解决代理不可达。
  - 设置进程级 NO_PROXY：只能按域名，且会影响所有 Provider 和子进程。

#### 决策 B：代理语义只有两态，不新增 URL 代理配置

- **Description**：use_proxy 未配置或 true = 保持 HTTPX 默认环境代理决策（含 NO_PROXY）；false = 不从环境获取代理，全部直接连接目标。未指定代理地址时不创建或强制选择代理，也不覆盖终端变量。
- **Rationale**：与现有环境代理机制兼容，最小新增接口；满足知乎可直连而其他服务仍经代理。
- **Trade-offs**：用户不能在本版本为不同 Provider 指定不同代理 URL；可在未来独立增加，而非暗中扩大范围。
- **Rejected Alternative**：新增 proxy_url、fallback_proxy、自动切换；引入凭据处理、故障切换与更复杂的生命周期。

#### 决策 C：直连时保留 TLS CA 环境配置

- **Description**：对 false 使用 HTTPX trust_env=False 以阻止环境代理；同时显式传入由 HTTPX 现有 SSL Context 构造流程在 trust_env=True 下生成的 verify=SSLContext（或经测试等价的标准方案），保留 SSL_CERT_FILE / SSL_CERT_DIR 与默认 CA 的优先级。绝不传入 verify=False。不在进程全局设置环境变量。
- **Rationale**：HTTPX trust_env 影响的范围不止代理；不能使代理切换附带证书行为变化。
- **Trade-offs**：工厂需要支持 SSL context/创建选项；证书读取出错须正确报告启动失败。
- **Rejected Alternative**：简单地将 trust_env=False 直接应用到客户端而不处理 CA 环境（会改变信任链）。

### 3.2 Provider Integration

#### 决策 D：Firecrawl Search 固定不内嵌 Scrape

- **Description**：FirecrawlAdapter.search 的 POST /v2/search 永远不发送 scrapeOptions；发送 query、web source、经校验的 limit 和可选 highlights。只映射 url/title/description，不将响应中偶发 markdown/rawHtml 设为 raw_content/content。保留现有关键词去重、采纳、摘要流程；无正文时跳过 cheap_check/judge。
- **Rationale**：将按结果数收费的搜索与按页面抓取收费的动作明确分离，且已有 Keyword Pipeline 允许无正文结果。
- **Trade-offs**：keyword-search 不再预填全文，后续 url-fetch 需再次请求；这是有意的成本/性能取舍。
- **Rejected Alternatives**：通过搜索请求配置开关恢复 scrapeOptions（容易再次隐式收费）；修改所有 Provider 的 Keyword Pipeline（范围无关且会改变既有行为）。

#### 决策 E：Firecrawl Fetch 可调且限制费用意外扩大

- **Description**：FirecrawlAdapter.fetch 仍调用 POST /v2/scrape；新增配置 search_limit（默认 10，范围 1..100）、search_highlights（默认沿用服务端 true）、scrape_formats（默认 ["markdown"]；只允许 markdown/rawHtml 的非空无重复数组）、scrape_parsers（默认 []；只允许 pdf/image 的无重复数组）。适配器只解析可供 Gateway 使用的文本正文。
- **Rationale**：避免默认请求 rawHtml 与 PDF 按页解析；显式配置才能选择额外格式或启用 PDF 解析。
- **Trade-offs**：PDF URL 在 parsers=[] 下可能只返回 rawBase64；因无可用文本而走已有 Fetch 失败/备用 Provider 路径，并仍可能收取固定 1 credit；若用户需要 PDF 文本，需主动启用 pdf parser 并接受可能的逐页费用。
- **Rejected Alternatives**：透传 Firecrawl 任意 JSON 选项（无法限定费用和响应形状）；把 PDF 全文处理加入 Gateway 核心。

#### 决策 F：arXiv 查询编译与配置保持 Provider 内聚

- **Description**：ArxivProvider 内新增纯函数/私有辅助，将规范化后的查询切成普通词与双引号短语；每个词/短语在 query_fields 的每个字段生成 field:term，再以 OR 连接字段，以 AND 连接词组。例如：graph transformers → (ti:graph OR abs:graph) AND (ti:transformers OR abs:transformers)。显式短语 "graph neural networks" 原样作为一个 term；用户输入必须保持字面意义，不允许变成任意 arXiv 操作符。经官方解析链验证不能安全、无损表示的输入，在发出 HTTP 请求前以现有 Provider ProtocolFailure 受控失败，不删除字符或默默归一，其他 Provider 的执行不受影响。用 HTTPX params 编码。此边界经 2026-11-22 用户确认。
- **Rationale**：与官方语法一致，默认提高多个关键词共同匹配的精度；不对通用 paper-search 增加 Provider 专属参数。
- **Trade-offs**：与 all: 的宽泛匹配相比，长自然语言可能降低召回；本版不自动发起额外放宽检索。
- **Rejected Alternatives**：将原串直接包进 all:；LLM 重写；在 CLI 增加 ti/abs/au/cat 专属参数。

#### 决策 G：arXiv 四项配置参数

- **Description**：query_fields（默认 ["ti", "abs"]，本版只允许 ti/abs、去重且非空）、max_results（默认 10，1..100）、sort_by（默认 relevance，允许 relevance/submittedDate/lastUpdatedDate）、sort_order（默认 descending，允许 ascending/descending），配合现有 api_url/max_concurrency/min_interval_seconds。仅 arXiv 注册这些 options，不扩展其他 Academic Provider。
- **Rationale**：覆盖检索字段和结果组织的实际需求，同时避免 author/category 参数导致不同语义的复合查询。
- **Trade-offs**：复杂的分类过滤或按作者查询留待后续独立设计。

### 3.3 Interface / Protocol

#### 决策 H：CLI 默认无限等待响应

- **Description**：send_request 默认 response_timeout_seconds=None 即不传有限等待上限；显式传入数值仍由 asyncio.wait_for 实施超时。删除“普通请求除 Agent Search 外默认 600 秒”的分支；保留一次请求一次 CLI 调用、NDJSON 响应解析与 finally 内 writer.close。
- **Rationale**：与用户的长期执行需求一致，最少改动即可消除 CLI 先于 Daemon 放弃普通请求的问题。
- **Trade-offs**：当 Daemon 失去响应而未断开时，CLI 可无限等待；属于已明确接受的语义。
- **Rejected Alternatives**：让 Daemon 改为每请求强制主动断开；新增 cancel 协议；修改 Daemon 工作流上限。

### 3.4 Concurrency / Scheduling、State Management

- **Description**：沿用每 Provider 单客户端、CapacityGate、RateGate 和 HTTP 重试。use_proxy 在 Daemon 构造 Runtime 时被读取一次；修改配置需要正常重启。没有客户端池热替换；不让一个 Provider 的代理设置污染其他 Provider。
- **Trade-offs**：失败的代理本身仍不可用，且上游 httpcore 泄漏问题不在此修复；建议为需要直连的知乎显式配置 false。
- **Rejected Alternatives**：连接池自愈/运行时代理热切换；超出本次问题的最小范围。

### 3.5 Security、Observability 与 Future Migration

- 现有 SecretValue、日志脱敏、URL 查询参数清理和 body_preview 保持；禁止记录代理 URL 中的认证字段以及原始授权 Header。
- 在已有 runtime_built 或 DEBUG 配置摘要中仅输出 use_proxy 布尔模式，无需增加敏感代理地址；失败路径仍用已有 provider/stage/attempt/transport_type。
- 用户可不填写任何新字段：只有 Firecrawl Search 不再自动抓正文和 CLI 等待行为发生预期的默认变化。代理与 arXiv 既有 sort/limit 默认保持。
- 不将 Web/Academic/LLM 的专属配置传到公共 Request/Response 模型；之后要扩展只需沿用配置解析与 Runtime 注入。

## 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| config.py / Resolved*Config | 配置边界 | 解析 use_proxy、Firecrawl/arXiv 附加参数并严格验证 | resolve_config(data, registries, env) | Registry、TOML、环境变量 | 不可变配置 | validator |
| runtime.py / HTTP Client factory helper | HTTP 传输创建点 | 每 Provider 根据代理模式创建 AsyncClient；保留 CA 配置 | 内部 create_client(config, factory) | HTTPX、ResolvedConfig | 拥有每 Provider 客户端 | coordinator |
| HttpJsonExecutor | 共享传输层 | HTTP 请求、限速、重试、日志和关闭已有客户端 | request_json/request_text/aclose | AsyncClient、RetryPolicy、RateGate | 仅持有客户端引用 | transformer |
| FirecrawlAdapter | 付费搜索/抓取适配 | 分离元数据 Search 与显式 Scrape，映射结果 | search(query), fetch(url) | HttpJsonExecutor | 持有配置值 | adapter |
| ArxivProvider | arXiv 查询适配 | 纯查询编译、Atom 映射与结果限制 | search(query) | AcademicHttpExecutor | 持有配置值 | adapter |
| SearchOrchestrator | Keyword 工作流 | 接纳 URL、正文有无判断、聚合和写出 | keyword_search | Provider、URLStore、LLMStages | 使用既有 Store | coordinator |
| FetchOrchestrator/Scheduler | Fetch 工作流 | 只在请求抓取时读取正文，失败 fallback，清洗与安全检查 | url_fetch | URLStore、Fetch Providers | 既有缓存及状态 | coordinator |
| protocol.send_request | CLI 与守护进程传输 | 建连、发送、无限等响应、解析、关闭客户端端点 | send_request(socket, request, response_timeout_seconds=None) | asyncio、NDJSON | 单次连接 | boundary |
| ForegroundDaemon | 运行时服务 | 收请求、执行业务、发送响应、清理服务端端点 | start/_handle_connection | Runtime、Protocol | 工作流任务 | coordinator |

HttpJsonExecutor 不感知 use_proxy，Provider Adapter 不感知代理客户端的创建方式；Daemon 不感知 Firecrawl/arXiv 的选项。

## 5. Data Flow

### 5.1 启动与配置入口（agent-search-gateway start）

~~~text
config = config.resolve_config(TOML, provider_registry, env)
for each enabled Web / Academic / OA / referenced LLM provider:
    if use_proxy missing: use_proxy = True
    if use_proxy invalid type: raise ConfigFailure(CONFIG_ERROR)
    if provider-specific options invalid: raise ConfigFailure(CONFIG_ERROR)
    if use_proxy is True:
        client = injected_http_client_factory()          # 继承现有环境设置
    else:
        ssl_context = HTTPX_equivalent_default_SSL_context(trust_env=True)
        client = injected_http_client_factory(
            trust_env=False, verify=ssl_context
        )                                               # 不使用环境代理
    executor = HttpJsonExecutor(client, same_retry_and_gate)
    adapter = provider_factory(executor, provider_specific_options)
if any client creation fails:
    abort startup via existing ConfigFailure cleanup; no partial daemon
daemon.start()  # 原有流程
~~~

### 5.2 Keyword Search（Firecrawl）

~~~text
CLI sends KeywordSearchRequest unchanged
Daemon -> SearchOrchestrator.keyword_search()
  -> FirecrawlAdapter.search(query):
       POST /v2/search {query, sources: web, limit, highlights?}
       NEVER send scrapeOptions
       if transport/status/provider error: existing executor retry/failure
       else parse web items to KeywordSearchHit(url, title, snippet)
       always raw_content = "", content = ""
  -> SearchOrchestrator._stage_keyword_hit:
       if invalid URL or absent abstract: reject as today
       if no body: skip cheap_check and judge; still admit metadata
  -> URLStore, ResultWriter, SuccessResponse(path)
~~~

### 5.3 URL Fetch（Firecrawl）

~~~text
CLI sends URLFetchRequest unchanged
Daemon -> FetchOrchestrator.url_fetch(url):
    if URL not admitted or previously unavailable: existing response
    if content cached: reuse content
    else FetchScheduler -> FirecrawlAdapter.fetch(url):
        POST /v2/scrape {url, formats: scrape_formats, parsers: scrape_parsers}
        if response has usable markdown/rawHtml:
            URLFetchCandidate(raw_content, content)
        else:
            existing semantic/execution failure, next eligible fetch provider
    after candidate accepted:
        existing content_clean (only where needed), safety, focus, cache
  -> SuccessResponse(text) or existing error path
~~~

### 5.4 Paper Search（arXiv）

~~~text
CLI sends PaperSearchRequest(query, provider?) unchanged
Daemon -> PaperSearchOrchestrator -> ArxivProvider.search(query):
    terms = tokenize_literal_terms_and_quoted_phrases(query)
    if any term cannot be safely and losslessly serialized:
        raise existing provider ProtocolFailure before any HTTP request
    if no effective term after normalization: use existing provider-failure path
    for term in terms:
        field_query = OR(field + ":" + literalized(term) for field in query_fields)
    search_query = AND("(" + field_query + ")" for term in terms)
    GET arxiv API params:
        search_query = compiled
        max_results, sortBy, sortOrder = configured values
    if HTTP/XML error: existing retry and provider failure
    else map Atom entries and aggregate through existing pipeline
  -> SuccessResponse(result_path)
~~~

### 5.5 CLI Socket（keyword/paper/url-fetch/llm/agent）

~~~text
CLI -> send_request(socket_path, request, optional_explicit_timeout)
    connect Unix socket
    write NDJSON request and drain
    try:
        if caller gave explicit timeout: wait_for(readline, timeout)
        else: await readline() without deadline
        if EOF without newline: ProtocolFailure as before
        if malformed or oversized response: ProtocolFailure as before
        else: decode success/error and return
    except CancelledError / communication failure:
        propagate existing exception
    finally:
        close client socket endpoint
Daemon:
    run existing workflow and write response; no new cancellation watcher
    close server socket endpoint when current connection ends
~~~

## 6. Interfaces & Contracts

### 6.1 TOML（用户配置；新字段均可省略）

~~~toml
[web_providers.firecrawl]
enable_search = true
enable_fetch = true
use_proxy = true
search_limit = 10
search_highlights = true
scrape_formats = ["markdown"]
scrape_parsers = []     # 不逐页解析 PDF；PDF 仍可能有固定收费

[web_providers.zhihu]
enable_search = true
use_proxy = false       # 建议值：绕过存在故障的 HTTP 代理

[academic_providers.arxiv]
enabled = true
use_proxy = true
query_fields = ["ti", "abs"]
max_results = 10
sort_by = "relevance"
sort_order = "descending"

[llm_providers.openai_main]
use_proxy = true
# 其他既有必需字段保持

[oa_resolvers.unpaywall]
use_proxy = true
# 其他既有必需字段保持
~~~

- **配置契约**：严格布尔值，不接受字符串 "false"、数字 0/1；未配置 use_proxy 为 true。`use_proxy` 与各 Provider 的既有 options 校验路径兼容，不被注入 Adapter 构造函数。
- **查询契约**：Firecrawl 仍返回 KeywordSearchHit 的既有字段；元数据搜索的正文为空。arXiv 仍返回 PaperSearchHit。
- **协议契约**：NDJSON Schema 不变；send_request 显式有限超时的语义不变；默认 None 从“600 秒（非 agent）”变为“无限”。
- **日志契约**：沿用当前 request_id/stage/attempt/elapsed_ms；严禁输出完整代理 URL、API Key、CA 证书数据。
- **存储契约**：不新增表、文件类型或 URL Store 字段；现有 JSONL 输出兼容。

### 6.2 预计修改的实现文件

- src/agent_search_gateway/config.py — 共享字段、解析与 Provider 配置数据类（Web/Academic/OA/LLM）。
- src/agent_search_gateway/runtime.py — 一个 HTTP 客户端创建辅助函数，所有四类 HTTP Provider 共用，保留测试工厂注入。
- src/agent_search_gateway/providers/web/firecrawl.py 与 providers/defaults.py — 两个端点的负载/映射与仅 Firecrawl 的配置白名单。
- src/agent_search_gateway/providers/academic/arxiv.py 与 providers/academic/defaults.py — arXiv 查询编译、配置白名单。
- src/agent_search_gateway/protocol.py — 修改默认等待逻辑。
- config.example.toml、README.md / 必要说明、对应 tests 目录 — 文档与回归测试。

**明确不需要**改动 providers/http.py、orchestrators/*.py、daemon.py、zhihu.py 或其他单独 Provider Adapter。

## 参考资料与约束来源

- Firecrawl Search: https://docs.firecrawl.dev/api-reference/endpoint/search
- Firecrawl Scrape / parsers 与 PDF 费用: https://docs.firecrawl.dev/api-reference/endpoint/scrape
- arXiv API 用户手册: https://info.arxiv.org/help/api/user-manual.html
- HTTPX 环境与 SSL: https://www.python-httpx.org/environment_variables/ ; https://www.python-httpx.org/advanced/ssl/
- 现有代码基线：config.py、runtime.py、providers/web/firecrawl.py、providers/academic/arxiv.py、protocol.py、daemon.py
