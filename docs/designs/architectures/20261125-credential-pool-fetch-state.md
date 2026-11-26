## Architecture: Credential Pool, Explicit Fetch Ordering and Lazy URL Processing

- 日期：2026-11-25
- 基线：`8da7aeb`（v2.0.0）
- 工作树 / 分支：`design-credential-pool-fetch-state-20261125`
- 状态：架构评审稿；未实施。

### 1. Scope & Assumptions

#### In Scope

- 显式 Fetch Provider 优先级；保持顺序尝试、首个通过 Judge 的候选获胜。
- 删除所有默认并发限制。并发、速率窗口、最小间隔均独立可选；不存在隐藏的 Gateway 或 HTTP 连接池活动连接数上限。
- Web Search 不主动请求完整正文，但保留自然附带的正文；Search 不执行正文相关的 LLM stage。
- 用显式 `URLStatus` 替换 `URLRecord.available`；按需执行尚未完成的 Fetch/Judge/Clean/Safety，复用 READY 正文。
- Web、Academic、OA Resolver、LLM 的 HTTP Provider 支持多凭证；每组 credential 独立限流。Round Robin / Fill First，原凭证优先重试，最终失败后切换其他凭证。
- Agent 保持 CLI 认证与执行方式，仅支持可选的 CLI invocation 限流。
- 更新 config、runtime、doctor、示例配置、迁移说明与公开接口文档。

#### Out of Scope / Todo

- 无 Provider 聚合限流、Quota Group、账户共享额度、加权轮询、模型级独立凭证池。
- 无凭证冷却、熔断、失败状态、额度/费用统计、主动余额查询、自动禁用、OAuth 刷新、持久化或后台探测。
- 无 Agent 多凭证或账户切换；不统计 Agent 内部 HTTP 请求。
- 不兼容旧 HTTP provider 配置格式和 default_max_concurrency；不自动修改用户实际配置。
- 不改变 CLI/Socket/Search JSONL 形式、逻辑 provider 身份、搜索 fan-out、模型过滤和 URL admission 约束；不增加任意 URL 抓取、TTL、持久化 URLStore、Focus Summary 缓存。

#### Assumptions

- 同一逻辑 HTTP provider 下各 credential 代表用户有权使用的独立额度来源（通常每账户一组凭证）。用户负责确认额度独立、服务条款允许、各 endpoint 支持相同逻辑操作。LLM 同一 alias 下各凭证应支持已配置的 model，不自动切换 model。
- 一次默认 Search 仍调用各启用的逻辑 provider，而不是各 credential；单个逻辑调用不会为了扩展召回而对各凭证 fan-out。
- URLStore 与 credential pool 生命周期为一个 daemon 进程；升级迁移配置并重启即可，无历史持久化数据迁移。
- 保留现有 Retry Policy，包括 Retry-After；不转化为跨请求凭证冷却状态。

---

### 2. Architecture Summary

保留 Thin CLI → Daemon → Orchestrator → Provider contracts → Adapter → HttpJsonExecutor。Runtime 为每个逻辑 HTTP provider 构建一个共享 CredentialPool；每组凭证使用现有 factory 生成独立 Adapter、HTTP client/executor 和可选 gates。类型化 façade 维持原有 provider contract，统一委托 pool 进行选择、限流和有限 fallback。FetchScheduler 以显式顺序选择当前可用的逻辑 provider；池内部处理凭证选择。Keyword Search 只做结果收录和未验证正文缓存；URLStore 负责显式状态和正文原子更新，FetchOrchestrator 根据状态复用既有 FetchScheduler 和 LLMStages。原有 Agent CLI runner 保持独立。

---

### 3. Design Decisions

#### Runtime Model / Provider Integration

##### D1. 公共 CredentialPool

- Description: 每组 credential 创建独立 adapter 实例及 HTTP client/executor；每个逻辑 provider 只有一个 pool，对上层暴露原有 name 与 capability。Web search/fetch 共享同一个 pool 及每组 credential 的 gates；同一 LLM alias 的各 Search/Fetch LLM stages 也共享。一次逻辑调用绑定一个 credential，内部所有分支、HTTP retry 和既有 LLM protocol retry 使用该 credential。运行时使用四类 façade 适配既有 Web、Academic、OA、LLM 协议，池只实现一次调度算法。资源由 Runtime 管理，每个 client 关闭一次。Agent 不参与多凭证。
- Rationale: 现有 adapters 已将 endpoint、认证方式绑定构造器，复用 factory 不需要修改每个 adapter 的认证、请求构造和解析逻辑；避免运行时修改共享实例导致串用凭证。
- Trade-offs: 每组凭证持有独立连接池；不同类别需要薄 façade；多分支 adapter 最终失败后 fallback 可能重新执行已尝试的请求，但保留原有 partial-success 规则。
- Rejected Alternatives: 动态替换 Authorization/URL（认证方式不统一，且可能串用凭证）；每个 credential 注册为新 provider（会改变 fan-out、过滤与结果语义）；逐个 adapter 实现调度（重复代码）。

##### D2. 统一配置结构

- Description: 启用或被引用的 HTTP provider 必须配置非空有序 credentials array-of-tables，包括单凭证与无需 key 的 provider。每组 credential 保存原有 endpoint 字段、环境变量认证引用与可选限制。以现有 registry/factory 验证每组有效配置；TinyFish 保留 search_api_url/fetch_api_url，You.com 分别校验 REST/MCP endpoint 与可选 key，Academic/OA 支持现有 email 与可选/必需 key，arXiv/Jina/open-webSearch 不要求虚构认证。原有 enable flags、use_proxy、搜索业务参数、LLM protocol/api_endpoint 等 provider-level 选项不变。BrightData 的账户相关 search_zone/fetch_zone 允许按 credential 覆盖 provider 公共值，以支持不同账户；仅允许已注册选项，不引入任意参数透传。Agent 保持原有 CLI table 结构，增加可选 rate 配置。旧 endpoint/认证/限流字段不得与新结构混用。
- Rationale: 单、多、无认证凭证采用同一结构，保留上游协议差异；最小化配置分支。
- Trade-offs: Breaking Change；用户手动迁移已有配置，环境变量名字本身可以保持不变。
- Rejected Alternatives: 双格式兼容、自动迁移、provider/credential 两级限制，均增加维护或隐式行为。

#### Concurrency / Scheduling

##### D3. 无隐式并发和速率限制

- Description: 删除 default_max_concurrency 及 Web、Academic、LLM、Agent 的硬编码默认并发值。HTTP provider 的 max_concurrency、rate_limits、min_interval_seconds 仅在 credential 层配置，均独立可选。缺省不启用对应 gate；空 rate_limits 无窗口限制；显式的限制值仍按既有正整数/正有限数校验。Agent 在 CLI alias 层采用同样的可选语义。配置了 max_concurrency 时保留既有 CapacityGate 的逻辑 provider 调用计数语义（adapter 调用及内部 retry 完成前持有 lease）；Fetch 结束网络操作并释放 credential lease 后再进入独立 Judge。RateGate 在每个物理 HTTP attempt（含内部并行分支和所有 retry）发送前取许可；LLM 也接入 rate gate。Agent 的 rate unit 是 CLI invocation 启动次数。未设置任何限制时，立即调度所有独立工作，不等待并发/速率 gate。Runtime 公共 HTTP client factory（代理及直连）显式使用 httpx.Limits(max_connections=None, max_keepalive_connections=20) 或等效设置，去除 HTTPX 0.28.1 默认 100 活动连接限制；保留闲置连接回收上限 20，它不限制正在进行的连接。保留 URL keyed lock/singleflight、timeout 与 retry，不能将它们作为全局并发限制。
- Rationale: 用户控制是否限流；凭证独立额度无需聚合 gate。移除 HTTPX 默认活跃连接数，避免底层隐式限制。复用现有 gate，不用大整数模拟无限制。
- Trade-offs: 不限流配置可能导致较高本地资源使用、上游限流；操作系统、代理、服务商仍有自身限制。配置 max_concurrency 限制逻辑调用数，知乎/百度千帆内部并行 HTTP 分支仍分别计入 RateGate；它不是严格的每物理 HTTP in-flight cap。这一点维持现有语义。
- Rejected Alternatives: 隐式默认 cap、强制 Provider 层限流、无配置时使用巨大 finite limit。

##### D4. Round Robin / Fill First 与单轮凭证 fallback

- Description: credential_strategy 取 round_robin（缺省）或 fill_first。Round Robin 以固定 array 顺序从上次成功选择的位置之后 wrap-around，跳过当前不可用及本次已尝试的成员；共享游标在选择时推进，不能依据完成顺序推进。Fill First 每次从数组首项检查，选择最靠前的当前可用成员。可用 = 有未尝试成员、可选 capacity 有槽位、可选 rate 的首个许可可用。原子 try-lease 与游标更新不持锁等待 I/O；rate 预检查不消费许可，由 executor 在物理发送时真正原子获取，若被其他并发分支抢先占用则按已有有界等待机制处理。
- Description（重试）: 选定成员后使用既有 Retry Policy；已开始的 retry 不重新轮询，所有发送计入该凭证的限流。不可重试的错误不强制执行完整 max_attempts。最终 provider ExecutionFailure 时释放 lease，排除本次已尝试成员，按策略选择其他成员；不做新的状态码/额度分类。成功（含正常空搜索结果、OA no-match 和既有有效 partial success）立即返回。InputFailure、取消及意外程序缺陷不被当作凭证故障。无未尝试成员后返回 provider 执行失败，不开启第二轮。所有剩余候选暂不可用时等待任一可用通知/最早 rate 时间，沿用 request_timeout_seconds 等待预算，响应 cancellation，不 busy-loop。新请求不记住上次 credential 失败；无 cooldown、熔断和探测。
- Rationale: 最大复用当前 Retry，避免重试同时跨账户；单轮遍历及有限等待限制故障放大；优先级/轮询语义明确。
- Trade-offs: Fill First 的首个凭证长期耗尽额度时，每个新请求可能再次失败后切换，属于用户接受的取舍。对于单 HTTP operation，N 组凭证和 max_attempts=M 最多 N×M 次物理尝试；多分支及已有 LLM protocol retry 还会增加实际请求。既有 per-attempt timeout 和 Retry-After 不等于整个逻辑调用 deadline，不新增整体超时策略。
- Rejected Alternatives: 凭证永久失效状态、指数冷却、主动额度统计、每次 HTTP attempt 轮换、无限重复 pool traversal。均超出已确认需求。

##### D5. 显式 Fetch Provider 顺序

- Description: 新增 [web_providers].fetch_order 有序逻辑 provider 名称数组。缺省沿用原 TOML 声明顺序；配置时先按列表顺序排列已启用 fetch provider，未列出的已启用 fetch provider 按原声明顺序追加。未知名称、重复名称、不支持 fetch 的注册名称报配置错误；已注册且支持 fetch 但当前未启用的名称允许列出，Runtime 跳过。enable_fetch 仍决定是否参与，不将排序表当作过滤器。Scheduler 保留一个 URL 同时只尝试一个逻辑 provider，按优先级选择有至少一个当前可用 credential 的池；全部暂不可用时等待任一候选恢复。选定池后执行 D4；同池剩余凭证全被暂时限制时按已有等待预算等待，失败后尝试下一个逻辑 provider。Pool 提供 try-reserve/availability wait 内部接口，scheduler 不再单独占用 provider gate，不重复消耗 RateGate 许可；首次 rate wait 预算与现有 scheduler/executor 协调机制衔接。
- Description（语义）: Provider 返回正文后执行现有 cheap_check/Judge；拒绝正文则切换下一个逻辑 provider，而不是因内容质量问题换同一 provider 的 key。Judge 执行异常直接返回 LLM stage 执行错误，不视为 fetch credential failure。保留原 FetchOutcome 的 accepted、semantic_failure 和 execution_failure 分类。
- Rationale: 区分 provider 优先级和 credential 优先级，最小侵入复用顺序 Scheduler。
- Trade-offs: 部分排序表存在追加规则；选定池内部 retry/fallback 可能增加延迟。
- Rejected Alternatives: 严格阻塞等待最高优先级 provider；并行竞速抓取；将凭证配置到 fetch_order。

#### State Management / Storage

##### D6. 五字段 URLRecord，显式状态机

- Description: URLStatus(StrEnum) 替代 available，仍为五字段 immutable snapshot。状态表示下一步需要执行的正文操作。URLStore 是唯一更新边界，原子写入状态与正文；raw_content/content 必须来自同一候选，不能独立 first-non-empty-wins 拼接不同候选。abstract 保留 first-non-empty-wins。Search 只在新 URL 或 NEED_FETCH 缓存第一份非空自然候选；不覆盖已有候选及验证状态。关键词 Search 的正文写入和 Fetch 的状态推进共享同一 per-URL serialization boundary，复用 PerKeyLockPool；批量 Search 一次只锁一个 URL，不持有全局锁或多个 URL 锁。metadata-only admission 不改写既有正文/状态，可继续走原有同步路径。
- Rationale: 正文存在不再代表已通过 Judge；显式状态避免漏掉 Judge/Safety，支持在中间阶段失败后恢复。
- Trade-offs: 同一 URL 的正文 admission 可能等待正在执行的 fetch；只缓存第一份自然候选，不跟踪历史被拒绝的正文指纹。未来 Search 仍可能重新提供先前被拒绝的候选，但未验证不得返回。内存模型变更，外部协议不变。
- Rejected Alternatives: 多个 judged/available/safe 布尔字段（非法组合）；丢弃自然正文；引入 FSM 框架。

| Status | Invariant | Transition |
|---|---|---|
| NEED_FETCH | raw_content/content 均为空 | metadata-only admission；缓存候选拒绝后清空两字段；后续自然候选可进入 NEED_JUDGE |
| NEED_JUDGE | 至少一份非空未验证候选 | Search admission；cheap_check/Judge 通过后，有 content → NEED_SAFETY，仅 raw → NEED_CLEAN；拒绝 → 清空正文 → NEED_FETCH |
| NEED_CLEAN | raw 非空、content 为空，Judge 已通过 | FetchScheduler accepted raw-only 或缓存 Judge 通过；Clean 成功 → NEED_SAFETY |
| NEED_SAFETY | content 非空，Judge 已通过 | accepted 已清洗候选或 Clean 成功；Safety 通过 → READY；拒绝 → UNAVAILABLE |
| READY | 最终正文非空，Safety 已通过 | 同一份正文可直接返回/按请求生成 Focus Summary |
| UNAVAILABLE | daemon 生命周期内 terminal | Safety 明确拒绝；FetchScheduler 按既有规则最终 semantic_failure；后续 Search 不自动恢复 |

- FetchScheduler 已包含 Fetch + cheap_check + Judge，accepted 候选直接进入 NEED_CLEAN 或 NEED_SAFETY，避免重复 Judge。
- 缓存候选拒绝本身不能导致 URL terminal；必须继续 Fetch。无启用 Fetch Provider 或所有实际 Fetch 尝试纯执行失败时，保持 NEED_FETCH 并返回执行错误。Scheduler 原有 semantic_failure 聚合与终止语义保留。
- 执行异常或取消不推进该 stage 状态；已成功提交的阶段不重做。Focus Summary 失败保留 READY。
- READY/UNAVAILABLE 不因 Search 覆盖或降级。本次没有正文刷新功能；若以后允许替换 READY 正文，必须重新经过验证。

##### D7. Search 不主动请求正文，不调用正文处理 stage

- Description: Keyword Search 仅执行 provider 搜索、返回结构校验、URL/redirect normalization、摘要校验、去重、admission、JSONL 写出。主动请求完整正文的选项关闭，当前至少 Tavily include_raw_content 和 Exa contents.text 需要调整；保留摘要/标题等搜索字段，Firecrawl 已是 metadata-only，不改回内嵌 Scrape。实现阶段逐一审计 Web adapter 的搜索请求与 response mapping，只改主动正文请求，保留 API 原生附带正文（如百度千帆 agentic、ScrapeGraphAI 的既有字段），不假定未核实的计费规则。不删除本来原生附带的正文，不发起额外 Fetch。不在 Search 执行 Judge、Clean、Safety、Focus Summary。Academic/LLM/Agent Search 保持原有搜索、解析、聚合和 OA enrichment；LLM/Agent 本身的搜索调用不是正文处理 stage。
- Rationale: Search 与 Fetch 职责清晰，避免为未阅读网页消耗额外 API/LLM 调用。
- Trade-offs: 部分正文必须显式 Fetch；自然附带的候选延后验证。
- Rejected Alternatives: 继续预先 Judge；丢弃所有自然正文；为所有 adapter 引入假设上游支持的统一正文选项。

#### Security / Observability / Future Migration

##### D8. 安全与可观测性

- Description: 只有 READY 可以返回正文或用于 Focus Summary；保持现有 LLM 输入截断、SecretValue、环境变量认证、脱敏日志与安全错误边界。Credential identity 使用逻辑 namespace/name + array index，不输出实际 key、带认证的 URL、email 或认证 header；选择、切换、耗尽、gate 与 URL 状态转换日志采用该安全标识；不将凭证 index 暴露为新 provider。Doctor 对每个启用或被引用的 credential 执行已有配置与适用健康检查，例如 open-webSearch 的已有 health check，不主动调用付费搜索或抓取检查余额。URLStore、gates、pool 游标均仅存内存。
- Rationale: 保持安全边界并方便定位哪组凭证消耗、失败及切换。
- Trade-offs: 不跟踪凭证健康/余额历史；日志与既有 Doctor 非付费检查是诊断手段。
- Rejected Alternatives: 输出原始认证、主动消耗额度做 health 探测。

##### D9. 配置迁移

- Description: 示例配置、README、public-interface、provider-rate-limits、You.com 配置片段、Doctor 和配置测试全部更新至新 schema。旧 default_max_concurrency 及 HTTP provider 顶层旧 endpoint/认证/限流字段报清晰配置错误；不能静默忽略旧限制。如果希望保持原默认并发语义，用户应把原值明确写入每组 credential 或 Agent alias。配置示例可保留服务商推荐值，但解析器绝不应用示例默认限制。CLI/Socket/search JSONL/逻辑 provider 与模型过滤保持兼容；内部 URLRecord 的变更有意为之。
- Rationale: 已认可 Breaking Change；不维护两套 schema，不悄悄改变现有限流语义。
- Trade-offs: 需要迁移配置，后续发布按项目规则处理；本设计不决定版本号或执行发布。
- Rejected Alternatives: 自动改写用户实际配置、静默丢弃旧配置字段、保留默认并发。

---

### 4. Component Catalog

| Component | Purpose | Key Responsibilities | Public Interfaces | Dependencies | Owns State? | Role |
|---|---|---|---|---|---|---|
| config / registries | 配置边界 | 解析并校验 credentials、strategy、optional limits、fetch_order，复用 factory validators | resolve_config | TOML/env/registry | No | Validator |
| Runtime | 组装资源 | 构造池与独立 client/adapter/gate，关闭一次 | build/aclose | config, factories, pool | 生命周期 | Coordinator |
| CredentialPool（新增） | 通用调度 | 选择、try-reserve、单次 fallback、等待/cancel | invoke / try-reserve / wait_available（内部概念接口） | optional gates, typed failure | RR 游标、成员；attempted request-local | Coordinator |
| Provider façades | 保留协议 | 委托各方法到 pool，fetch 暴露内部 availability | 原有 Web/Academic/OA/LLM Protocols | pool, adapters | 无独立状态 | Adapter |
| CapacityGate / RateGate | 限制额度 | 只在显式配置时限制并发或发起速率 | lease / try_lease / permit | async primitives | active slots / timestamps | Gate |
| HttpJsonExecutor / LLM common client | 执行 | 既有请求、retry、Retry-After、截断和诊断；每次发送取可选许可；避免与 pool 重复 capacity acquire | request_json/text, complete_* | HTTPX, retry, rate | client lifecycle | Executor |
| Agent runner | CLI 执行 | 保留 CLI auth/命令，按 invocation 限制 | run_antigravity | optional gates, subprocess | 既有进程状态 | Executor |
| URLRecord / URLStore | URL 数据 | 五字段与状态不变量、原子更新、共享 keyed lock | admit/get/transition（内部） | NormalizedURL, enum, lock | snapshots | Store |
| SearchOrchestrator | 搜索 | 逻辑 provider fan-out、收录、结果写出 | keyword_search/llm_search/agent_search | façades, store, writer | No | Coordinator |
| PaperSearchOrchestrator / OA | 学术 | 原有聚合、OA enrich、metadata admission | paper_search/finalize_paper_hits | pooled providers, store | 既有局部缓存 | Coordinator |
| FetchScheduler | 抓取 | 按序选择可用池；Fetch + cheap_check/Judge；原有 outcome | fetch_until_accepted | ordered façades, pool availability, LLMStages | request-local candidates | Scheduler |
| FetchOrchestrator | 正文处理 | singleflight、per-URL lock、状态推进和 focus | url_fetch | store, scheduler, stages | 既有 singleflight | Coordinator |
| Doctor / Observability | 检查 | 逐凭证验证及适用 health，安全日志 | doctor/log_event | config, diagnostics | No | Validator/Sink |

Provider Adapter 不知道 CredentialPool 或 URLStore；Pool 不知道厂商认证格式、Judge/Safety 或 URL 状态；FetchScheduler 不处理 credential 的认证方式；LLMStages 不感知物理 credential identity。

---

### 5. Data Flow

#### 5.1 start / Runtime

```text
Daemon.start -> resolve_config(TOML, registries, env)
  if invalid new schema or legacy limit fields: CONFIG_ERROR
  for each enabled/referenced logical HTTP provider:
    for credential in array:
      resolve env references and validate effective factory options
      create optional capacity/rate gates
      create HTTPX client (unlimited active connections, existing proxy behavior)
      create existing adapter/executor using resolved credential
    build shared pool + typed façade
  build Agent runners with optional alias gates
  sort enabled web fetch façades (fetch_order then unlisted)
  construct shared URLStore, LLMStages, orchestrators
  bind socket
  if construction/bind fails: close any allocated resources, existing error boundary
```

#### 5.2 HTTP logical operation

```text
Facade.invoke(method, args):
  attempted = empty set
  while unattempted credentials remain:
    select ready member according to strategy; reserve optional capacity
    if no ready member:
      wait for unattempted member within rate/capacity wait budget
      if timeout: provider execution failure
      if cancelled: propagate
      continue
    attempted.add(member)
    try:
      result = await member.adapter.method(args)
        each physical HTTP attempt -> optional RateGate -> send
        retryable errors -> existing Retry Policy, same member
      return result (includes empty list/no-match/valid partial success)
    catch InputFailure / cancellation: propagate
    catch terminal provider ExecutionFailure: retain safe failure, try next
    finally: release capacity if configured
  raise provider execution failure
```

LLM protocol retry 仍在原 client 内运行；wrapper 和 client 不能同时持有同一容量 gate。意外程序错误按原错误边界处理，不进行凭证故障分类。

#### 5.3 keyword-search

```text
Daemon -> SearchOrchestrator.keyword_search
  validate query, apply existing logical --provider filter
  gather one search per selected logical provider via pool
  if all fail: ALL_PROVIDERS_FAILED
  for successful hit:
    validate structure, unwrap/normalize URL, derive nonempty abstract
    if invalid: existing rejection/error policy
    keep natural nonempty candidate pair, if present
    URLStore.admit:
      preserve first abstract; existing body or terminal -> do not overwrite
      if body and new/NEED_FETCH:
        shared per-URL lock; commit candidate + NEED_JUDGE
      else: metadata-only admission, new URL -> NEED_FETCH
  deduplicate, write existing JSONL, return path
  # No additional Judge/Clean/Safety/Focus LLM stage.
```

#### 5.4 paper-search / llm-search / agent-search

```text
Daemon -> existing scope and provider/model filters
  Academic: gather each logical search through pool
  LLM: gather configured invocation(s) through same LLM alias pool
  Agent: execute existing CLI runner, optional invocation gates
  if all applicable branches fail: existing error
  Paper scope: aggregate and enrich via optional pooled OA resolver
    if OA execution failure: preserve paper record (existing best effort)
  Web scope: parse existing LLM/Agent search format
  admit metadata-only landing URLs; write existing scope JSONL and return path
```

#### 5.5 url-fetch

```text
Daemon -> FetchOrchestrator.url_fetch(url, focus)
  normalize URL/focus; singleflight(url, focus)
  acquire shared per-URL lock
  if URL not admitted: URL_NOT_ADMITTED
  while status not in {READY, UNAVAILABLE}:
    NEED_JUDGE:
      candidate = content or raw_content
      if cheap_check fails: clear body pair -> NEED_FETCH
      else:
        decision = LLMStages.judge(candidate)
        if execution failure/cancel: preserve state, propagate
        if rejected: clear body pair -> NEED_FETCH
        else: NEED_SAFETY if content else NEED_CLEAN
    NEED_FETCH:
      if no enabled fetch provider: NO_URL_FETCH_PROVIDERS
      outcome = FetchScheduler.fetch_until_accepted
        ordered logical provider candidates; skip blocked pools
        selected pool: per-credential retry then fallback
        successful provider response: cheap_check and Judge
        provider execution failure/semantic candidate rejection: next provider
        Judge execution failure: propagate LLM-stage error
      if execution_failure: preserve NEED_FETCH, raise
      if semantic_failure: mark UNAVAILABLE
      if accepted: atomically replace candidate pair
        -> NEED_SAFETY if content else NEED_CLEAN
    NEED_CLEAN:
      cleaned = LLMStages.content_clean(raw_content)
      if failed/cancelled: preserve NEED_CLEAN, propagate
      write content + NEED_SAFETY atomically
    NEED_SAFETY:
      decision = LLMStages.safety(content)
      if failed/cancelled: preserve NEED_SAFETY, propagate
      if rejected: UNAVAILABLE
      else: READY
  if UNAVAILABLE: existing unavailable message
  if focus absent: return READY content
  else: return LLMStages.focus_summary(content, focus)
    if failed/cancelled: keep READY, propagate
  release lock/singleflight with existing cleanup
```

#### 5.6 doctor / stop

```text
doctor:
  resolve new config with shared validators
  for each applicable provider + credential:
    validate configuration; report sanitized identity and configured limits
    run existing applicable non-billable health check
  preserve socket/filesystem/exit-code semantics
stop:
  existing graceful shutdown/cancellation; close each HTTP client once
  reap Agent subprocesses; discard in-memory pool/gate/URL state
```

---

### 6. Interfaces & Contracts

#### 6.1 Configuration (public; breaking change)

示意 TOML（示例不是默认限值）。带认证的 credential 需按对应 registry 增加现有环境变量引用字段；示例不写实际秘密或环境变量值。

```toml
[web_providers]
fetch_order = ["jina", "tavily", "exa", "firecrawl"]

[web_providers.tavily]
enable_search = true
enable_fetch = true
use_proxy = true
credential_strategy = "round_robin"

[[web_providers.tavily.credentials]]
api_url = "https://api.tavily.com"
# Add the required API-key environment reference for account A.
rate_limits = [{ requests = 100, period_seconds = 60 }]

[[web_providers.tavily.credentials]]
api_url = "https://api.tavily.com"
# Add the required API-key environment reference for account B.
max_concurrency = 5

[web_providers.jina]
enable_search = false
enable_fetch = true
credential_strategy = "fill_first"

[[web_providers.jina.credentials]]
api_url = "https://r.jina.ai"
# No configured rate or concurrency limits.

[academic_providers.arxiv]
enabled = true

[[academic_providers.arxiv.credentials]]
api_url = "https://export.arxiv.org/api/query"
max_concurrency = 1
min_interval_seconds = 3

[oa_resolvers.unpaywall]
enabled = true

[[oa_resolvers.unpaywall.credentials]]
api_url = "https://api.unpaywall.org/v2"
# Add the required contact-email environment reference.

[llm_providers.openai_main]
protocol = "openai"
api_endpoint = "chat_completions"

[[llm_providers.openai_main.credentials]]
api_url = "https://api.openai.com"
# Add the required API-key environment reference.
# Existing model/invocation settings stay outside credentials.

[agent_providers.antigravity_main]
harness = "antigravity"
command = "agy"
# Existing pass_env/timeout behavior; optional limits.
```

- Credential 的认证字段名称仍为现有 `api_key_env` / `contact_email_env`（依 registry 要求使用字符串环境变量名，绝不接受明文 secret）；endpoint 使用现有 `api_url` 或原有专用字段，不引入新 base_url 拼写。实际 example 必须包含有效的环境变量引用示例。
- 每个 HTTP provider 的 strategy 可选，缺省 round_robin；fill_first 由 array 声明顺序决定。
- 三个 limits 独立可选，无继承和默认；HTTP provider 级限流字段与所有 default_max_concurrency 均不支持。
- 缺失 required auth、非法字段/strategy/limit/order、旧新混用、已启用/引用的 HTTP provider 空 credentials 均报配置错误；禁用/未引用者不构建池；原有单 enabled OA resolver 限制保留。

#### 6.2 Domain object (internal)

```python
class URLStatus(StrEnum):
    NEED_FETCH = "need_fetch"
    NEED_JUDGE = "need_judge"
    NEED_CLEAN = "need_clean"
    NEED_SAFETY = "need_safety"
    READY = "ready"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True, slots=True)
class URLRecord:
    url: NormalizedURL
    raw_content: str = ""
    content: str = ""
    abstract: str = ""
    status: URLStatus = URLStatus.NEED_FETCH
```

URLStore 是状态和正文更新的唯一入口，维护上文状态不变量。原有 KeywordSearchHit、URLFetchCandidate、PaperSearchHit、LLMInvocation、AgentInvocation 不新增 credential-specific 字段。CredentialPool 和其 selection lease 是内部接口。

#### 6.3 Public request / response

保持原 CLI、Socket envelope、搜索 JSONL（例如 `{"url":"https://example.com/page","abstract":"Summary"}`），fetch 返回原有正文/摘要/unavailable 文本；逻辑 provider 名称和模型过滤不变，不添加 CLI credential 选择参数。

#### 6.4 Implementation boundaries / review points

- 公共改动集中 config/registries/runtime/concurrency/rate_limit/pool/HTTP executor/LLM 公共 client/Agent optional gates/doctor/fetch scheduler/URLStore/models/orchestrators。多凭证不要求逐个修改 vendor adapter；如果构造参数或账户专属选项确需适配，保持局部且最小；Search 主动正文选项审计是另一项独立修改。
- 需明确复核的技术选择：部分 fetch_order 追加未列出项；fill_first 拼写；逻辑调用 concurrency 保持既有语义；HTTPX 只保留闲置连接回收限值；同 URL 正文 admission 共享锁；无跨请求失败记录及额外总 deadline。
- Error Handling 与 Testing 分别进入下一评审阶段；三个 Spec 通过书面 review gate 后再提交。本阶段不实现、测试或发布代码。
