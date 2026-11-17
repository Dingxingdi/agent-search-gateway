# Architecture: Doubao、You.com、Keenable 与 open-webSearch 接入

- 日期：2026-11-17
- 状态：**待维护者审阅设计文档；未实施、未提交**
- 基线：`eeeff82`
- 同组文档：[错误处理](../error-handlings/20261117-doubao-youcom-keenable-openwebsearch.md) / [测试设计](../testings/20261117-doubao-youcom-keenable-openwebsearch.md)

## 1. Scope & Assumptions

### In Scope

- 新增四个独立 Web Provider：`doubao`、`youcom`、`keenable`、`open_websearch`，**均同时支持 Search 与 Fetch**。
- 复用既有 `KeywordSearchProvider` / `URLFetchProvider`、`HttpJsonExecutor`、`ProviderRegistry`、`Runtime`、`ProviderQuotaManager`、`URLStore`、搜索与抓取编排器。
- 新增适配器、默认注册、示例配置、README、定向测试；接口协议差异留在适配器内。
- `open-webSearch` 作为已运行的独立 HTTP 服务接入，Gateway 不管理其内部搜索引擎、安装、进程或浏览器组件。
- HTTP 200 内嵌错误、抓取空正文、部分坏搜索条目等情况沿用现有错误、日志框架处理。

### Todo（设计通过后实施）

1. 新建四个 `providers/web/*.py` 适配器，并更新 `providers/defaults.py`。
2. 为四者增加 README 配置、凭证/部署提醒、`config.example.toml` 示例。
3. 增加 fixture、适配器、配置/注册/运行时、错误及冒烟测试。
4. 根据官方 API 文档和实际样本核对个别搜索结果 envelope 后固定严格解析器。

### Out of Scope

- 不把 open-webSearch 的底层 Google/Bing 等引擎拆成 Gateway Provider，也不主动传 `engines`。
- 不自启动 open-webSearch，不引入 Node.js、Playwright、爬虫服务或额外缓存。
- 不扩展本轮 CLI/MCP 工具、不开发通用 `extra_body` / 任意供应商可调参数系统。
- 不修改其他供应商或无关工作树，不改变 LLM 搜索与抓取后处理流程。

### Assumptions

- Doubao、You.com、Keenable 使用需要凭据的正式 API；Keenable **仅**用 Keyed API，不使用无需凭据的公开端点。
- Doubao Fetch 需要**按量计费 API Key**；仅订阅套餐 Key 不能用于 Fetch。此限制属于配置与文档注意事项。
- 每家支持现有 `enable_search` / `enable_fetch` 独立开关；四者各仅开放 `api_url` 这一项额外配置。
- `api_url` 表示服务根地址，适配器用共享 `endpoint` 拼接固定端点；超时、重试、配额来自既有全局配置。
- 第四家的 daemon 通常监听本机回环地址；远程部署时由用户指定连接 URL。没有独立健康检查或自动拉起。
- 文档和远端 API 可能演进；实施时以官方契约与确定性 fixture 双重校验，遇到不确定字段先调查，不猜测成功内容。

## 2. Architecture Summary

搜索路径仍为 `keyword-search → SearchOrchestrator → KeywordSearchProvider.search → HttpJsonExecutor → KeywordSearchHit → URLStore / 既有结果处理`；抓取路径仍为 `url-fetch → FetchOrchestrator → FetchScheduler → URLFetchProvider.fetch → HttpJsonExecutor → URLFetchCandidate → 既有判定及 LLM 处理`。

| Provider | 默认服务根 `api_url` | Search | Fetch | 认证 | 能力 |
| --- | --- | --- | --- | --- | --- |
| `doubao` | `https://open.feedcoopapi.com/search_api` | `POST /web_search` | `POST /fetch` | Bearer token | Search + Fetch |
| `youcom` | `https://ydc-index.io` | `POST /v1/search` | `POST /v1/contents` | X-API-Key 头 | Search + Fetch |
| `keenable` | `https://api.keenable.ai` | `POST /v1/search` | `GET /v1/fetch` | X-API-Key 头 | Search + Fetch |
| `open_websearch` | `http://127.0.0.1:3210` | `POST /search` | `POST /fetch-web` | 无 Gateway API Key | Search + Fetch |

这里 `ydc-index.io` 是 You.com 的正式 API Host，不应替换成网站域名。open-webSearch 是服务入口；它最终选用哪些搜索引擎，**完全由 open-webSearch 自身配置决定**。

## 3. Design Decisions

### Provider Integration

**D1 — 一个供应商一个适配器**

- Description：新建 `DoubaoAdapter`、`YouComAdapter`、`KeenableAdapter`、`OpenWebSearchAdapter`；分别实现 `search(query)`、`fetch(NormalizedURL)`。
- Rationale：四家请求协议、字段和业务错误分类不同，适配层隔离差异，便于增删和独立测试。
- Trade-offs：四份独立模块及测试文件，少量共有解析逻辑通过 `common.py` 复用。
- Rejected：万能“HTTP JSON 字段路径配置”适配器；增加隐式分支、配置耦合和调试难度。

**D2 — 复用现有 Provider 注册/配置**

- Description：`build_default_registry` 注册四项 `ProviderCapabilities(search=True,fetch=True)`，各项允许的 vendor option 只包含 `api_url`；前三家 `requires_api_key=True`，第四家 `False`。Runtime 注入 `name`、`http_executor`、`secret`（如需要）和 `api_url`。
- Rationale：已有配置验证、SecretValue、独立 Search/Fetch 开关、配额、executor 生命周期，无需新配置 schema。
- Trade-offs：结果数量、语言、地域、实时缓存策略等暂不可由用户单独配置。
- Rejected：本次新增每家复杂配置树或修改 `config.py` 建立通用额外参数系统。

**D3 — 采用供应商低侵入、低成本 API 选项**

- Doubao Search：发送 `Query` 与 `SearchType="web"`，解析 `Result.WebResults` 的 `Url`、`Title`、`Summary` 或 `Snippet`。Fetch：发送 `Url`，解析 `Result.Data.ContentText`；检查响应元数据和网页抓取状态。
- You.com Search：发送 `query` 和保守固定 `count`，解析 `results.web` 列表及其 `results.web[].snippets`；不启用额外 `extraction`。Fetch：`urls` 仅包含目标 URL，`formats` 指定 Markdown，取匹配目标的非空 `markdown`。
- Keenable Search：`query`、固定保守 `max_results`，将文档定义的结果 URL/标题/摘要映射。Fetch：`GET` 查询参数 `url`，读取 Markdown `content`；不发送 `live`，服从默认缓存/实时策略。
- open-webSearch Search：仅发送 `query`（如必要可设保守固定 `limit`），**省略 `engines`**；解析 `status=ok` 之后的 `data`。Fetch：`url`，读取 `status=ok` 后的 `data.content`。
- Rationale：维持与既有适配器相同的“固定内部参数 + 稳定领域输出”风格；不意外引入费用更高的选项。
- Trade-offs：当前不暴露全部高级参数；将来可独立设计可配置性。
- Rejected：返回原始 API JSON 给编排器；在 Fetch 无正文时用搜索摘要假扮网页正文。

### Runtime Model / Interface & Protocol

**D4 — HTTP、重试与诊断只有一个实现**

- Description：四个适配器统一调用 `HttpRequester.request_json`，使用共享 `HttpJsonExecutor`、`RetryPolicy`、`response_inspector`、`response_diagnostics`、`safe_diagnostic_fields`。HTTP 200 的已知业务错误通过 `ProviderResponseFailure` 识别，结构性错误由严格 parser 转为现有失败类型。
- Rationale：保留既有 408/429/5xx、传输超时/错误的重试和日志机制。
- Trade-offs：JSON 解码或成功 payload schema 失败通常终态，不为之加入盲目重试。
- Rejected：各适配器自行创建 HTTP Client、无限重试、私有指数退避。

**D5 — 不改用户入口**

- Description：`keyword-search --provider NAME` 只过滤一个搜索提供商；`url-fetch` 没有 `--provider` 参数。抓取目标仍必须是**同一运行时内已收录**的 URL，由原有 scheduler 选取启用的 Fetch Provider。
- Rationale：无须重开 URL 访问安全模型或变更 CLI。
- Trade-offs：隔离单家 Fetch 时用临时配置或直接 adapter 冒烟测试，而不是凭空添加 CLI 选项。
- Rejected：为本功能修改 CLI 参数或 FetchScheduler 调度策略。

### State Management / Storage / Concurrency

**D6 — 叶节点 Provider 无持久化责任**

- Description：新适配器只保存根 URL/私密凭证/executor 引用；`URLStore`、搜索合并、并发限制、LLM 评分/内容清理照旧。生命周期由 `Runtime.aclose` 收尾。
- Rationale：保持当前低耦合设计；不需要额外数据库或线程。
- Trade-offs：无法从 Gateway 查看 open-webSearch 内部引擎状态或缓存。
- Rejected：托管 daemon、增加第二份 URL 缓存。

### Security / Observability / Future Migration

**D7 — 保持凭证最小暴露与安全日志**

- Description：密钥只通过现有 SecretValue 注入授权头；允许安全诊断字段 `status`、`upstream_code`、`upstream_message`、`semantic_result`、`response_kind`、候选数等，不记录原始 Key、请求正文、完整查询、网页正文、URL 敏感参数或原始响应。
- Rationale：当前日志已提供失败归因与重试过程，保持数据边界。
- Trade-offs：不能从日志恢复网页全文；这是隐私取舍。
- Rejected：为排障打印 request/response 全文。

**D8 — open-webSearch 是独立部署边界**

- Description：服务不可达、浏览器依赖不可用、上游引擎失败，都按该 Provider 的失败向已有调度器报告；Gateway 不自动安装或重启服务。
- Rationale：不引入 JS/Playwright 依赖，维护边界清楚。
- Trade-offs：用户负责服务升级和可用性。
- Rejected：直接内嵌 open-webSearch 源码或在 Gateway 枚举/控制其引擎。

## 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Data-Flow Role |
| --- | --- | --- | --- | --- | --- | --- |
| `providers/web/doubao.py` | 豆包双能力适配 | Bearer、业务错误、字段映射 | `search` / `fetch` | common、HTTP、contracts | 配置 | 外部数据 → 领域值 |
| `providers/web/youcom.py` | You.com 双能力适配 | Search/Contents、Markdown | 同上 | 同上 | 配置 | 同上 |
| `providers/web/keenable.py` | Keenable 双能力适配 | Keyed API、Search/Fetch | 同上 | 同上 | 配置 | 同上 |
| `providers/web/open_websearch.py` | 独立 daemon HTTP 适配 | `/search`、`/fetch-web`、状态信封 | 同上 | 同上 | 地址 | 同上 |
| `providers/defaults.py` | 默认注册 | 四家能力、Key 要求与配置白名单 | `build_default_registry` | registry、适配器 | registry | 启动配置 |
| `config.py` / `runtime.py` | 通用装配 | 凭证与选项校验、实例与 executor 注入 | 既有 API | registry、quota | 运行期实例 | 连接各组件 |
| `providers/http.py` / `web/common.py` | HTTP 与解析公共能力 | 重试、日志、安全诊断、计数 | `request_json`、helpers | httpx | 连接池 | 请求与错误边界 |
| `SearchOrchestrator` / `URLStore` | 搜索和 URL 收录 | 合并、规范化、收录 | 既有入口 | keyword providers | URL 状态 | Search 下游 |
| `FetchOrchestrator` / `FetchScheduler` | 抓取与回退 | URL 授权、候选判定、LLM 流程 | 既有入口 | fetch providers、URLStore | 请求期状态 | Fetch 下游 |

## 5. Data Flow

### 配置启动

```text
for provider_config in 启用的 Web Providers:
  registration = registry.get(provider_config.name)
  if 名称、能力、选项不合法: raise 既有 ConfigFailure
  if registration.requires_api_key:
    if 环境变量缺失: raise 既有 ConfigFailure
    secret = 已解析 SecretValue
  executor = HttpJsonExecutor(全局 retry policy)
  adapter = registration.factory(name, api_url, http_executor, secret?)
  if enable_search: SearchOrchestrator 增加 adapter
  if enable_fetch: FetchScheduler 增加 adapter
Runtime 统一管理 executor 的关闭
```

### Search 入口

```text
keyword-search(query, optional --provider)
  -> SearchOrchestrator/共享 quota
  -> adapter.search(query)
     -> 请求体与鉴权仅在本适配器构建
     -> HttpJsonExecutor.request_json(stage="search")
        if timeout/transport error/408/429/5xx: 按既有策略 retry；耗尽失败
        if HTTP200 + 已知供应商错误: inspector 分类并按既有机制失败/重试
        if 非合法 JSON: ProtocolFailure
     -> 解析成功 envelope
        if 缺必需字段/类型不符: 当前 Provider 失败
     -> for 每条搜索记录:
        if 无合法 URL / 字段非法: 跳过并统计拒绝原因
        else: KeywordSearchHit(url, title, snippet)
     -> 记录 provider_candidates_summary
  -> 已有 URL 规范化、结果合并、URLStore 与输出
  -> 单 Provider 失败不改变其他 Provider 的处理
```

### Fetch 入口

```text
url-fetch(url, optional focus)
  -> FetchOrchestrator
  if URL 无法规范化或不在当前 URLStore: 原有拒绝
  else if 已收录且有可用正文: 原有复用分支
  else:
    -> FetchScheduler 按原有配置顺序、quota 尝试 providers
       -> adapter.fetch(NormalizedURL)
          -> HttpJsonExecutor.request_json(stage="fetch")
          if HTTP 失败/timeout/业务错误/无效信封/空白正文:
             当前 Provider 失败，scheduler 尝试下一个
          else: URLFetchCandidate(raw_content=text, content=text)
       -> cheap_check / LLM judge
          if 拒绝: 下一个候选
          if 接受: 原有安全检测、清理、焦点总结、输出/存储
    if 全部失败: 返回现有聚合失败
```

## 6. Interfaces & Contracts

### 领域接口（保持不变）

```python
class KeywordSearchProvider(Protocol):
    async def search(self, query: str) -> list[KeywordSearchHit]: ...

class URLFetchProvider(Protocol):
    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...

# KeywordSearchHit: url, title, snippet, raw_content, content
# URLFetchCandidate: raw_content, content
```

### 配置合同（保持 `[web_providers.NAME]` 风格）

- 每家有 `enable_search`、`enable_fetch`、`api_url`、可选 `max_concurrency`。
- Doubao、You.com、Keenable 各配置 `api_key_env` 指向**环境变量名称**，不在配置中粘贴凭据值。
- `[web_providers.open_websearch]` 不需要 `api_key_env`，且需提前运行对应 HTTP daemon。
- 示例配置要避免“默认启用而缺凭证/daemon 造成整个 Gateway 启动失败”；可采用默认关闭与清楚的启用说明，保持与现有文档风格一致。

### HTTP 请求及响应合同

| Provider | Search 关键字段 | Fetch 关键字段 | 解析成功条件 |
| --- | --- | --- | --- |
| Doubao | `Query`、`SearchType=web` | `Url` | 元数据无错误，`WebResults` 合法 / `ContentText` 非空 |
| You.com | `query`、`count` | `urls`、`formats=["markdown"]` | `results.web` 合规 / 匹配 URL 的 `markdown` 非空 |
| Keenable | `query`、`max_results` | GET query `url` | 符合官方搜索 schema / `content` 非空 |
| open-webSearch | `query`，不含 `engines` | `url` | `status=ok` / `data.content` 非空 |

**必须复核后再编码：** Keenable Search 实际列表结构、open-webSearch `data` 的搜索条目格式、Doubao Fetch 原站状态字段与计费 Key 使用条件；不使用宽泛、静默的字段兜底。

## 7. 官方来源

- Doubao Search：https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh
- Doubao Fetch：https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh
- You.com Search：https://you.com/docs/api-reference/search/v1-search
- You.com Contents：https://you.com/docs/api-reference/contents
- Keenable Search：https://docs.keenable.ai/api-reference/search.md
- Keenable Fetch：https://docs.keenable.ai/api-reference/fetch.md
- open-webSearch HTTP API：https://github.com/Aas-ee/open-webSearch/blob/main/docs/http-api.md
