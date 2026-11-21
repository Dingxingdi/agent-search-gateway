# Architecture: Baidu Qianfan 双分支搜索、open-webSearch Doctor、知乎配置说明

- Date: 2026-11-21
- Status: Draft for review; design only, no implementation
- Base commit: e712f10 (v1.0.0)
- Branch: design-baiduqianfan-doctor-zhihu-20261121
- Companion drafts: ../error-handlings/20261121-baiduqianfan-doctor-zhihu.md and ../testings/20261121-baiduqianfan-doctor-zhihu.md
- Constraint: 最小修改、最小侵入，复用现有知乎双分支模式及公共基础设施。

## 1. Scope & Assumptions

### In Scope
- 注册一个可按需启用、仅支持 Search 的 Web 提供商 baiduqianfan；凭证读取用户指定的 BAIDUQIANFAN_API_KEY 环境变量。
- 一次 baiduqianfan.search(query) 同时调用官方 Web Search 与 Agentic Search 两个 API；有一路成功即返回其结果，两路都失败才判定整个提供商失败。
- 转换网页、图片、视频中具有实际 HTTP(S) URL 的结果为现有 KeywordSearchHit；不新增多模态协议。
- 对已启用的 open_websearch，doctor 默认通过其官方 GET /health 做无副作用的服务存活检查。其他云端服务维持静态检查。
- 在 config.example.toml 的知乎配置段说明未公布统一固定 QPS，因此本次不设置 rate_limits 和 min_interval_seconds。
- 更新注册、配置示例、必要说明、参考表和定向测试，不变更 CLI 或 daemon 公开协议。

### Out of Scope / Future Work
- 不拆分百度为两个 Provider；不加分支开关、动态 API 额外参数、统一双分支框架。
- 不为百度增加 URL Fetch；不下载、解析、转录图片或视频实体，不强行解析没有 URL 的结构化卡片。
- 不扩展 doctor 为所有云端 API 的在线鉴权、额度或可用性测试；不检查 Agent CLI 可执行文件；不管理 open-webSearch 进程和内部搜索引擎。
- 不实现本地每日额度计数或费用拦截；不保证调用永远免费；不重构公共 RateGate、Retry、ResultWriter 等组件。

### Assumptions / Sources
- Web Search: POST https://qianfan.baidubce.com/v2/ai_search/web_search；输入 messages 内含单条 user 消息，并指定 search_source=baidu_search_v2、edition=standard；输出 references 含 url/title/snippet/type，部分媒体还可能含 image.url、video.url。
- Agentic Search: POST https://qianfan.baidubce.com/v2/ai_search/agentic_search；输入 query 与可选 count；输出 web_page_result.web_pages、image_result.images、video_result.videos；card_result 不保证可用于 URL 搜索。
- 两端采用同一种千帆 API Key 的 HTTP Bearer 认证方式，密钥值不写入设计或日志。官方查询长度均有 72 字符计量单位上限（汉字计两单位），超长输入上游自动截断。本补丁不增加额外语义改写。
- 官方说明两种搜索默认各为 3 QPS。采取更保守的提供商级共享限流 3 次实际 HTTP 尝试/秒（包括重试），即一次双分支搜索通常消耗两个令牌。
- 官方目前披露：Web Search 每月约 1500 次免费调用且按天发放；Agentic Search 每天 50 次免费调用。超额使用及后付费状态可能产生费用；不能据此断言两种额度一定独立。
- open-webSearch 官方约定 GET /health 返回存活状态，成功响应中 data.daemon 为 running。仅能证明 daemon 存活，不能证明每个底层搜索引擎可用。
- 当前内置的 Web Provider 中，open_websearch 是明确由用户单独启动的 HTTP daemon；其余外部云 API 和 Agent 命令此次不增加在线检查。

Official references:
- https://cloud.baidu.com/doc/qianfan-api/s/Jmwbl4qxs
- https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk
- https://cloud.baidu.com/doc/qianfan/s/nmweljpt3
- https://cloud.baidu.com/doc/qianfan/s/Ymttv7y41
- https://github.com/Aas-ee/open-webSearch/blob/main/docs/architecture/local-http-adapter.md
- Existing project reference: docs/provider-rate-limits.md

## 2. Architecture Summary

保留 CLI → daemon → SearchOrchestrator → KeywordSearchProvider.search(query) → HttpJsonExecutor → KeywordSearchHit → 既有 URL 规范化、去重、URLStore 与结果输出流程。只增加一个 BaiduQianfanAdapter：仿照 ZhihuAdapter 将一次 Search 扇出为两个并发上游请求，汇总可用的 URL，并对单分支失败降级。Doctor 单独增加对已启用 open_websearch 的轻量健康探测，通过已有配置解析结果获取服务地址，不构造 Runtime。知乎修改仅限配置注释及必要文档澄清。

## 3. Design Decisions

### Provider Integration

#### D1 — 单一提供商、固定双分支
- Description: 新增 src/agent_search_gateway/providers/web/baiduqianfan.py；在 providers/defaults.py 注册一个 search=True、fetch=False 的条目，允许 api_url 这一专有配置。使用 asyncio.create_task + gather 并行请求，取消时清理分支，与 ZhihuAdapter.search 保持一致。
- Rationale: 实现用户要求的“启用即同时调用”，不改变公共编排器。
- Trade-off: 每次逻辑 Search 产生两次上游调用，消耗两边可用免费额度；两个分支共享一次提供商并发租约。
- Rejected: 注册两个独立提供商；新增通用多分支框架；给分支额外开关。

#### D2 — 各分支独立请求与响应解析
- Description: Web Search 固定使用 standard 版及官方请求结构，并在允许范围内请求 URL-bearing web、image、video 类型；Agentic Search 使用其 query/count 参数。两个解析器保持小而独立，阶段名分别为 web_search、agentic_search。
- Rationale: 响应封装不同，厂商细节应局限于适配器。
- Trade-off: 固定的请求参数、各模态数量需按官方上限测试，未来如需用户参数化再独立设计。
- Rejected: 在 Provider Protocol 中增加厂商特有参数和全局模式字段。

#### D3 — 仅输出真实 URL
- Description: Web Search 解析 references；媒体顶层 URL 不存在时仅使用官方定义的嵌套 image.url / video.url 作为后备。Agentic 分别解析 web_pages、images、videos。所有候选须为合法的 HTTP(S) URL。原有 SearchOrchestrator 继续负责标准化与最终去重。无 URL 卡片、非法条目按原有拒绝统计忽略。
- Rationale: 现有结果契约就是 URL-based，既满足多模态链接又不引入二进制协议。
- Trade-off: 图片尺寸、媒体时长、权威分数、发布时间和无链接卡片不会保留。
- Rejected: 伪造链接、扩展 KeywordSearchHit 或在适配器自行复制去重逻辑。

#### D4 — 正文与摘要区分
- Description: 网页标题/摘要直接映射 title/snippet；URL-only 图片/视频缺少标题时使用真实的类型标识以满足编排器非空摘要要求。Web Search 的 content 按官方说明为片段摘要，不能当作网页全文。Agentic web_pages.content 按原始网页正文放到 raw_content，不直接写入已清洗的 content；沿用现有 cheap_check、judge、content_clean、安全审查。
- Rationale: 防止绕过当前 URL 内容清洗流程，也避免把片段伪装成原文。
- Trade-off: 原始正文可能在下游判定中被拒绝；非网页媒体仅返回 URL。
- Rejected: 将任何 snippet 当成完整页面正文，或新增媒体 Fetch 流程。

### Runtime / Concurrency

#### D5 — 共享现有限流、重试和日志
- Description: Runtime._build_web_providers 为 baiduqianfan 注入同一个 JsonRequester/HttpJsonExecutor 和 RateGate。示例给出每秒 3 次物理 HTTP 尝试的共享保守窗口，延续全局默认并发量；每次发送和重试均计费于该窗口。沿用安全诊断与 provider_partial_failure 事件。
- Rationale: 现有 HTTP 边界正是上游实际尝试次数的计数点。
- Trade-off: 提供商的逻辑搜索吞吐量小于 3 次/秒，且本地限速无法防止超额计费或其他应用共享额度。
- Rejected: 将双请求视作一次速率消耗；为两个分支另设公开速率配置。

### Doctor / Diagnostics

#### D6 — 只探测已启用的自部署 open-webSearch
- Description: 当 open_websearch.enable_search 或 enable_fetch 为 true 时，对其 api_url 拼接 /health 发起 HTTP GET；限制超时（建议 2 秒）、禁用重定向且不发送凭证。只有 HTTP 200 且 JSON 包装内 status=ok、data.daemon=running 时返回 OK，其余连接失败/超时/协议或正文错误返回 FAIL。未启用/未配置时不发任何请求。
- Rationale: 用官方无需搜索的存活探针解决“已配置未启动”问题，不消耗第三方配额。
- Trade-off: Doctor 在这个特定场景会产生一次网络流量；只能证明 daemon 本身可用，不能保证底层引擎实际搜索成功。
- Rejected: 执行 POST /search、通用全提供商连通性检查、探测 TCP 端口即宣称健康、自动拉起 daemon。

#### D7 — 保留现有 Doctor 公共接口
- Description: 对私有配置检查辅助逻辑做最小调整，使成功 resolve_config 后能将 open_websearch 的启用状态及 api_url 传给 run_doctor，避免重复解析或构造 Runtime。为新探测器提供窄范围的可注入异步 callable，便于测试；DoctorCheck/DoctorReport/CLI 不变。健康检查结果放在现有 Unix socket 状态行之前，尽量保留既有输出顺序。
- Rationale: 增强定点功能，不引入新的 doctor --live 或数据结构。
- Trade-off: 需要更新“doctor 不创建 HTTP client”相关测试的适用范围（未启用自部署服务时仍不创建）。
- Rejected: 将真实搜索请求塞进 doctor，或对配置做第二次解析。

### Configuration / Security

#### D8 — 知乎保持无固定窗口
- Description: config.example.toml 的 zhihu 段仅补充“统一固定 QPS 未公开、账户额度独立、未添加固定 rate_limits”的说明；沿用 default_max_concurrency 及已有 HTTP 429 重试。
- Rationale: 用户选择 B，不应捏造频率要求。
- Trade-off: 特定账户或上游策略下仍可能出现 429。
- Rejected: 随意设定 1 QPS 等未经证实的限制。

#### D9 — 密钥与错误信息不泄露
- Description: 保持凭证由 SecretValue 在发送时装入认证头；doctor 不输出健康 HTTP body、原始异常堆栈或含查询参数的原始地址；只记录允许的失败原因、阶段名、HTTP 状态和安全字段。
- Rationale: 复用既有日志脱敏逻辑，避免扩散额外敏感信息。
- Trade-off: 不能仅从 doctor 输出完整重放上游响应。
- Rejected: 原始请求/响应全量日志、统一日志系统重做。

## 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interface | Dependencies | Owns State? | Data-Flow Role |
|---|---|---|---|---|---|---|
| BaiduQianfanAdapter (new) | 双 API 搜索 | 双分支请求、解析、部分容错 | search(query) -> list[KeywordSearchHit] | JsonRequester, SecretValue, diagnostics | 无持久状态 | 来源/转换 |
| ProviderRegistry (existing) | 提供商注册 | 增加一个 Search-only 条目 | build_default_registry() | Factory | 注册信息 | 注册 |
| Runtime._build_web_providers (unchanged) | 组装运行时 | 注入 key、共享执行器和限速器 | Runtime.build | Config, quota manager | HTTP clients | 装配 |
| HttpJsonExecutor / RateGate (unchanged) | HTTP 调用 | 限流、重试、安全 HTTP 错误 | request_json(), permit() | httpx | 限速历史 | 执行/调度 |
| SearchOrchestrator (unchanged) | 关键词搜索编排 | URL 校验、去重、正文检查与持久化 | keyword-search workflow | Providers, URLStore, LLM stages | 工作流状态 | 编排 |
| FetchOrchestrator (unchanged) | URL 正文处理 | 已缓存 raw body 的清洗及必要抓取 | url_fetch | URLStore, fetch scheduler | 请求态 | 编排 |
| Doctor config helper (small edit) | 静态检查 | 配置解析与可选自部署地址提取 | 私有 helper | resolve_config | 无 | 验证 |
| Doctor health probe (small addition) | daemon 存活检查 | GET /health、界定超时/失败、脱敏 | run_doctor 可注入异步探针 | httpx | 无 | 探测 |
| Config and docs | 可发现配置 | Baidu opt-in、限速、知乎说明、doctor 限制 | TOML/Markdown | 既有配置 schema | 配置 | 文档 |

边界：BaiduQianfanAdapter 不访问 URLStore/CLI/FetchScheduler；doctor 不构造 Runtime，也不调用搜索业务端点。

## 5. Data Flow

### 5.1 Keyword Search via baiduqianfan

~~~text
CLI keyword-search(query) -> daemon -> SearchOrchestrator:
    if provider disabled or selector invalid: existing rejection
    else:
        acquire one existing provider workflow lease
        BaiduQianfanAdapter.search(query):
            start Web Search POST task
            start Agentic Search POST task
            each task -> shared HttpJsonExecutor:
                before each send/retry: reserve RateGate permit
                if upstream HTTP/business failure: use existing retry/error policy
                else: parse URL-bearing hits, reject malformed entries
            await both with Zhihu-style cancellation cleanup
            if both branches fail: raise existing provider failure
            else:
                log safe provider_partial_failure for each failed branch
                concatenate successful hits in deterministic branch order
        SearchOrchestrator:
            for each hit:
                if URL invalid or abstract missing: reject using existing path
                else: normalize URL, stage/merge existing candidate and dedup
                if raw_content exists: existing cheap_check / judge pipeline
            write results through existing ResultWriter
        release provider lease
~~~

### 5.2 Doctor

~~~text
CLI doctor -> run_doctor(paths, environ):
    private config helper:
        if config missing/invalid: append existing FAIL, no liveness target
        else:
            append existing valid config and environment-name checks
            if open_websearch enabled: expose its configured api_url
    probe directory writability as today
    if an enabled open-webSearch target exists:
        GET <api_url>/health with bounded timeout and redirects disabled
        if connection/timeout/protocol/health error:
            append sanitized FAIL for open-webSearch
        else:
            append OK for open-webSearch
    probe gateway Unix socket with existing semantics
    render [ok]/[info]/[fail] status lines
    exit 1 iff any FAIL occurred, otherwise 0
~~~

### 5.3 Configuration

~~~text
Future config.example.toml changes:
    web_providers.baiduqianfan:
        enable_search: false      # opt-in; true always calls both branches
        enable_fetch: false       # not supported
        api_url: https://qianfan.baidubce.com
        api_key_env: environment name BAIDUQIANFAN_API_KEY
        rate_limits: 3 requests per 1 second (shared physical attempts)
    web_providers.zhihu:
        add explanatory comment; no fixed rate_limits
~~~

## 6. Interfaces & Contracts

- Public search contract unchanged: KeywordSearchProvider.search(query: str) -> list[KeywordSearchHit].
- Domain object unchanged: KeywordSearchHit(url, title, snippet, raw_content, content). Valid URL-only media has a type-aware nonmisleading title and empty body; Agentic original webpage text may populate raw_content.
- Baidu config uses existing shared keys and allows only api_url as a vendor-specific key. No separate branch-level config or public response type.
- Doctor public signature and exit convention unchanged. The new probe is internal; its only externally visible effect is one more OK/FAIL line when open_websearch is enabled.

## Review Gate

This file is an architecture draft. Obtain user approval before presenting/writing the error-handling and testing companion sections. Do not modify source code in the brainstorming phase.
