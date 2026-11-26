# Testing: Credential Pool, Explicit Fetch Ordering and Lazy URL Processing

- 日期：2026-11-25
- 基线：`8da7aeb`（v2.0.0）
- 工作树 / 分支：`design-credential-pool-fetch-state-20261125`
- 关联设计：[Architecture](../architectures/20261125-credential-pool-fetch-state.md) / [Error Handling](../error-handings/20261125-credential-pool-fetch-state.md)
- 状态：Testing Spec 评审稿；本文件描述实施后的测试要求，不表示已经实现或执行通过。

## 1. Testing Goals

1. Fetch 优先级可显式配置，按逻辑 provider 顺序选择当前有可用 credential 的候选；同一 URL 不进行 provider 并发竞速。
2. Web、Academic、OA Resolver、LLM 统一支持一个或多个 HTTP credential；保留逻辑 provider identity、原有协议与模型过滤；Agent 不参与多凭证。
3. 所有并发、速率窗口及最小间隔规则均独立可选，未配置不施加限制；删除 default_max_concurrency 及所有硬编码默认并发，包括 HTTPX 默认活跃连接数限制。配置示例中的建议值不被自动继承。
4. 每个 credential 独立计数；同一 credential 的多种 operation 与全部物理 HTTP attempt（含 retry、内部并行分支）遵循同一 rate policy。
5. Round Robin / Fill First 的选择、等待、原凭证 retry、有限 fallback、释放资源均正确；不增加凭证冷却、额度统计、失败记忆或额外重试轮次。
6. Keyword Search 不主动请求完整正文、不执行正文处理 LLM stage；自然附带的正文可入库为 NEED_JUDGE，合法 metadata-only 结果保持可抓取。
7. URLRecord 的六种状态、正文一致性及失败恢复行为满足 Architecture D6；只有 READY 正文可返回或用于 Focus Summary，成功的 Safety 可复用。
8. 所有公开 CLI/Socket/JSONL 契约、错误分类、脱敏、代理、重试、输入截断、关闭与已有 Provider 行为回归通过；仅配置结构和已确认的行为有意变更。

## 2. Test Strategy & Placement

### 2.1 分层矩阵

优先扩展现有测试文件与 fixtures；下表中新增文件名为建议，不要求因命名差异重构测试目录。

| 层次 | 测试位置（已有或新增建议） | 核心检查 |
|---|---|---|
| 配置与 registry | tests/unit/test_config_web_providers.py、test_config_academic_providers.py、test_config_llm.py、test_config_agent.py；必要时扩展 registry tests | 新 schema、必需认证、旧格式拒绝、默认不限流、显式排序 |
| CredentialPool | tests/runtime/test_credential_pool.py（新增） | 两种策略、稳定游标、可用性、request-local attempted、有限 fallback、取消 |
| Rate / Capacity | tests/runtime/test_quota_manager.py、test_rate_gate.py、tests/providers/test_http_executor.py | optional gates、物理请求计数、独立 credential、并发安全 |
| Runtime | tests/runtime/test_runtime_assembly.py、test_academic_runtime_assembly.py、test_new_web_runtime_assembly.py、test_optional_credentials.py、test_provider_proxy.py、test_agent_runtime_assembly.py | factory 多实例、资源生命周期、逻辑 provider 名称、HTTPX 无隐藏连接 cap |
| Agent / LLM | tests/providers/agent/、tests/providers/llm/、tests/unit/test_llm_stages.py、test_agent_stages.py | LLM 共享池与协议重试；Agent 可选 CLI gates；截断和现有协议不变 |
| Search adapters | tests/providers/web/ 与 tests/fixtures/providers/ | 主动正文请求审计、合法自然正文、元数据映射、业务错误不变 |
| Search workflows | tests/orchestrators/test_keyword_search_pipeline.py、test_keyword_search_state.py 及 paper/llm/agent search、filtering tests | 零正文处理 LLM 调用、admission、partial success、JSONL 和过滤 |
| Fetch scheduler | tests/scheduler/test_fetch_capacity.py、test_fetch_outcomes.py | 显式顺序、可用 credential、分层 fallback、Judge 与执行错误边界 |
| URLStore / Fetch | tests/unit/test_url_store.py、tests/orchestrators/test_url_fetch_flow.py、test_url_fetch_admission.py、test_url_fetch_singleflight.py、tests/runtime/test_singleflight.py | 状态/正文原子更新、延迟验证、READY 复用、重试恢复、并发 |
| Doctor / 日志 | tests/doctor/、tests/observability/、tests/unit/test_observability_logging.py | 逐凭证验证、非付费 health、脱敏标识、诊断 |
| 验收与回归 | tests/acceptance/、tests/cli/、tests/daemon/、tests/docs/ | 真实调用链的本地模拟、迁移文档、协议及生命周期 |
| 可选 live | tests/integration/ 及既有 smoke 入口 | 显式授权的真实接口连通性，非默认验收条件 |

### 2.2 测试方法与数据

- 使用已有 pytest + pytest-asyncio + httpx.MockTransport、tests/support fakes、假 Agent CLI；不引入新测试框架或付费网络依赖。
- 调度及限流使用注入的 monotonic clock、受控 sleep 和 asyncio.Event/barrier；禁止用真实 sleep 60 秒、1 小时等长等待证明限流。不依赖不确定的 task 完成顺序；需要验证选择顺序时显式控制请求到达及 release。
- 在实际发送边界捕获 credential identity、请求 URL、认证字段、开始时间和调用次数；只使用合成 fixture 和 fake environ。不得在日志、测试输出或报告中打印真实凭证、用户环境、带认证 URL。
- MockTransport 验证业务请求计数和逻辑调度，不用于证明 HTTPX 自身的实际连接池上限已消除；另以 factory 参数及本地 transport/连接池检查验证 max_connections 无上限。
- 对新增功能测试默认断开真实外网；原 live integration gate 保持默认关闭。短时 watchdog 只用于防止测试卡死，不充当生产并发/限流配置。
- 对已变更契约更新旧断言及样例 fixture，而不是跳过或删除有价值的回归测试；不将旧隐式默认值、Search 预先 Judge 或每次 Safety 重跑作为应保留行为。

## 3. Configuration / Runtime Cases

### 3.1 多凭证配置与迁移

| Case | 输入 / 行为 | 断言 |
|---|---|---|
| 单凭证 | Web、Academic、OA、LLM 分别只配置一个合法 credential | 正常解析并创建一成员池；不走旧单凭证分支 |
| 多凭证 | 两个不同 endpoint / env reference | 保留声明顺序，各组解析和验证独立 |
| 无需 key | arXiv、Jina、open-webSearch 等既有无 key 认证 | 一组合法 credential 可无 key，不伪造认证 |
| Optional auth | 支持 optional authentication 的既有 Provider | 认证有/无分别遵循 registry 约束 |
| 特殊 endpoint | TinyFish 两端点；You.com REST 与免费 MCP；Academic/OA email；BrightData zone | 按现有构造参数/validator 校验 effective config；不串用账户专属选项 |
| 不完整凭证 | 启用或引用的 provider 空 credentials、缺少必需环境引用、env 缺失/空、错误类型、非法字段 | CONFIG_ERROR，不构建或保留部分可用池，不发送真实请求 |
| 策略 | 缺省、round_robin、fill_first、非法字符串/类型 | 缺省 round_robin；两种合法值接受，非法值报错 |
| 旧配置 | 任一旧 default_max_concurrency、HTTP 顶层 endpoint/认证/limits、新旧混用 | 明确迁移错误；不静默忽略、继承或自动写回配置 |
| 启用状态 | 禁用 Web/Academic、未引用 LLM、未启用 OA | 不创建池或执行请求，沿用既有禁用验证边界 |
| OA | 多 credential 的一个 OA resolver；多个 enabled OA resolver | 前者合法，后者保持既有单 resolver 限制 |
| Agent | 既有 CLI table，带/不带可选 limits | 不要求 credentials，不接受 HTTP 多凭证配置，不修改认证/命令含义 |
| 既有选项 | use_proxy、enable flags、LLM protocol/model/invocation、search options | 保持现有验证及透传规则；pool 自身字段不传入 vendor adapter |

对所有四类 HTTP provider 及 Agent 分别参数化验证以下字段：

- max_concurrency、rate_limits、min_interval_seconds 均省略：解析为无相应限制，不产生 1/2/3 或其他默认 cap。
- 仅 max_concurrency；仅 rate_limits；仅 min_interval_seconds；多项同时存在：各自独立启用。
- 空 rate_limits：无窗口限制；若另有 min_interval_seconds 则仍遵守该间隔。
- max_concurrency/requests 为零、负数、bool、字符串、小数；period_seconds/min_interval_seconds 为零、负数、bool、NaN、Infinity、非法类型；窗口缺字段、未知字段、非法容器：既有严格 ConfigFailure 语义。
- config.example.toml、You.com 示例片段、README/public-interface 中配置示例用合成环境解析；推荐限制明确写在 credential 或 Agent alias 中。未配置时不得从示例/registry 继承推荐值。

### 3.2 Fetch order

| 输入 | 期待结果 |
|---|---|
| 省略 fetch_order | 原有启用 fetch provider 声明顺序 |
| 显式列出所有 fetch provider | 严格按配置顺序优先选择 |
| 只列出部分 provider | 已列出的优先；其余启用 fetch provider 按原声明顺序追加 |
| 空数组 | 没有显式优先项，全部启用项按声明顺序追加 |
| 包含已注册、支持 fetch 但未启用的名称 | 合法，Runtime 跳过该名称 |
| 未知名称、重复、search-only provider、非字符串元素或非数组 | CONFIG_ERROR |

fetch_order 只改变 fetch 排序；不改变 enable_fetch、Search provider 列表和 fan-out，也不是 credential 排序或选择参数。

### 3.3 Runtime 资源与 HTTPX

- 每个启用/引用的逻辑 HTTP provider 建一个 pool；每 credential 建独立 adapter、client/executor、optional gates。Web search/fetch 共用该池，LLM alias 的多个 stage/model invocation 共用该池；不同 alias/provider/namespace 不混用状态。
- fake factory 检查 endpoint、secret reference 解析结果、专用 zone/协议配置与 adapter 参数绑定。并发 A/B 请求实际进入对应实例，不在共享对象上动态改写认证字段。
- 代理与直连两条 HTTPX factory 路径显式传入无限制活跃连接参数，max_connections 为 None；允许保留 max_keepalive_connections=20 的闲置回收策略。保留 trust_env、直接连接、CA/TLS 验证及环境代理回归。测试注入 factory 必须能观察实际参数，不能用无参数 fallback 悄悄丢弃 limits。
- 对连接池上限另以实际构造的 HTTPX transport/limits 或受控 localhost 连接测试验证，不把 MockTransport 的高并发结果误当作真实连接池验证。
- 所有 credential client 在正常关闭、重复关闭、部分构造失败、socket bind 失败路径均恰好关闭一次；清理异常不掩盖首个失败，已分配资源不泄漏。Agent subprocess lifecycle 仍独立。

## 4. Optional Limits / Credential Scheduling / Retry

### 4.1 真正的无限流与额度独立性

| 场景 | 测试动作 | 关键断言 |
|---|---|---|
| 无 concurrency / 无 rate | 对单 credential 同时提交至少 128 个独立 fake operation，在其执行边界阻塞 | 全部在首个 operation 完成前进入，无隐藏 1/2/3/100 gate；此断言针对 Gateway 调度 |
| 无 concurrency / 有 rate | 同时提交多于窗口容量的操作，受控时钟推进 | 仅速率规则决定开始时刻；无并发槽位排队 |
| 有 concurrency / 无 rate | cap=2，阻塞首批操作 | 最多 2 个逻辑调用 in-flight；释放即继续，无人工最小间隔 |
| 两者都有 | cap=2、2/60s | 两项同时满足，完成操作不立即恢复速率额度 |
| 仅最小间隔 | min_interval_seconds=3 | 即使无窗口/并发 cap，物理开始间隔仍至少 3 秒 |
| 多窗口 | 3/1s + 5/60s | 所有窗口同时满足，边界按现有 RateGate 规则处理 |
| A/B 独立 | A=1/60s，B=2/60s；同时阻塞 A | B 可用且其计数不受 A 影响；无 provider 级聚合 cap |
| 同一 Web credential | Search 消耗窗口或占满 capacity，再 Fetch | 共享同一逻辑容量和 rate policy，不绕开限制 |
| 同一 LLM alias | judge/clean/safety/focus/search 并发 | 使用同一池及相应 credential gates；model 配置不变 |
| 内部并行分支 | Zhihu / BaiduQianfan 等一次 operation 内多个 HTTP 请求 | max_concurrency 仍限制逻辑 adapter 调用数；每个实际分支单独获取 rate permit |
| Academic / OA | 多请求使用同一 credential，另一个不同类别/provider 同时执行 | 各自独立，不把不同 namespace 的 quota 混在一起 |
| Agent 无限制 / 显式限制 | 多个模拟 CLI invocation；配置并发/速率规则 | 未配置无 cap，已配置限制 CLI invocation 启动；不计数内部 HTTP |

HTTPX 活跃连接无限制另见 3.3。保留 URL 锁、singleflight、timeout、retry 这些语义，不将它们当成全局 limiter。

### 4.2 策略与有限 fallback

使用 A/B/C 三个可观测假 adapter，显式控制 ready、blocked、failure、success；记录选择事件及物理尝试。

| Case | 预期 |
|---|---|
| RR 全可用，连续调用 | A、B、C、A、B、C |
| RR 暂时跳过 B | 从稳定顺序中的上次选择位置继续；B 恢复后按环形顺序再次参加，不因可用数组长度变化产生错误游标 |
| RR 并发 | 在选择时原子推进游标，不依据完成顺序推进；无重复 lease 或共享可变认证 |
| Fill First 全可用 | 连续选择 A；若 A 不限流且始终可用，B/C 可不被选中 |
| Fill First A 被 rate/capacity 阻塞 | 选择 B（若 B 也阻塞选 C）；A 恢复后新调用优先 A |
| A: retryable failure 后 success | 在 A 上完成既有 retry，不调用 B/C |
| A: retry 全失败，B: success | A 完成全部既有尝试，再 B；成功后不调用 C |
| A: 不可重试的 provider error | 保留现有失败分类和实际尝试数，随后换 B |
| 所有 credential 终端失败 | 每成员最多一轮 adapter operation，返回逻辑 provider failure；无第二轮 |
| 正常空搜索、OA no-match、有效 partial success | 正常结束，不调用剩余凭证 |
| InputFailure / cancellation / 非预期程序错误 | 不进行 credential fallback，保持现有异常边界 |
| 请求 X 中 A 失败，之后新请求 Y | attempted 是 request-local；按策略仍可选择 A，证明没有失败记忆或 cooldown |
| 多并发请求各自失败 | 一个请求的 attempted 集合不排除其他独立请求使用该 credential |
| 一个 Web provider 多凭证 | 默认 Search 仅一次逻辑 operation；过滤仍以 provider name 为单位，不将 credential index 作为新 provider |

### 4.3 物理 attempt、Retry-After、等待预算、清理

- 现有 HTTP retry 分类（408/429/5xx、网络超时/连接失败、现有 vendor response inspector）与 max_attempts 不变。对一个 HTTP operation：A 配置三个尝试，全部失败，再 B 成功，断言物理顺序 A/A/A/B，每次实际发送均计入相应 credential rate。
- 有效 Retry-After 与本地 RateGate 同时存在时，发送时间满足两者；保留既有无效/零值/header 解析行为。测试使用虚拟时间，不引入跨请求冷却。
- HTTP 401/402/403 等不可重试状态及现有业务错误按当前 adapter/executor 既有 typed failure 分类处理；不为余额不足新增专用解析器、失效状态或 retry 分支。合成凭证故障应只产生现有失败和一次有限 fallback。
- LLM client 的既有 protocol retry 与 HTTP retry 仍绑定同一 credential；pool 不额外重跑已经完成的 retry 轮次；wrapper 和 client 不重复 acquire capacity（cap=1 时不得死锁）。LLM 搜索上层 markdown parser 的失败保持原 pipeline 边界，不触发新增 credential replay。
- 无可用候选但有未尝试且暂时阻塞的成员时，等待 capacity 通知或 rate 最早可用时间；全部已尝试则立即失败。无 busy-spin；重复通知/重检不重置本次选择等待预算。
- 本地 rate wait 超时：无新增 HTTP send、不产生伪造 HTTP attempt，保留 rate_limit_wait_timeout 诊断；尝试其他允许的 credential/逻辑 provider。首次 Fetch 的预等待与 executor 衔接，不重复获取许可或重新获得一份完整预算。
- 预检查与发送间插入竞争者消耗许可，验证 executor 最终裁决且不超发；多并行分支争用同一 RateGate，按原滑动窗口限制发送。已经预约并进入发送边界的尝试不因取消/异常退回；尚未预约的等待不扣额度。
- 参数化 cancellation 注入点：pool wait、capacity lease 交接、rate wait、HTTP send、Retry-After/backoff sleep、adapter operation 结束交接。断言不启动下一 retry/credential；lease、waiter、任务按原语义清理，后续请求能取得资源，取消不伪装为 provider failure。
- 关闭中的 Runtime 不接收新请求；清理既有 in-flight 工作、socket、HTTP client 和 Agent 进程。保持现有 CLI 默认无隐式 600 秒超时及显式 timeout 的原有契约；不为本次增加断线 watcher、整体逻辑请求 deadline 或新的后台任务。

## 5. Search / Fetch Scheduler Tests

### 5.1 所有 Web Search adapter 的正文审计

建立按当前 registry 枚举全部 Web search-capable adapter 的审计矩阵（不是为每个厂商重新实现调度）。记录各 Search 请求是否主动要求完整正文、响应是否原生附带正文、摘要字段与对应 mock fixture。对不需要修改的 adapter 维持既有请求/响应测试；对主动正文选项需要修改的 adapter 增加请求体回归断言。

| 代表 | 请求与响应断言 |
|---|---|
| Tavily | 不再主动要求 include_raw_content 完整正文；URL/title/摘要映射保持正确 |
| Exa | 不再主动请求 contents.text 完整正文；保留摘要所需的搜索字段，不能因关闭正文而丢失合法结果 |
| Firecrawl | Search 继续不发送 scrapeOptions、不隐式 Scrape，保持既有 metadata-only 映射 |
| 百度千帆 Agentic / ScrapeGraphAI | 不增加额外请求；已有原生正文映射保留并交给 URLStore，Search 不验证正文 |
| 其他注册 adapter（含 Search 多分支、REST/MCP） | 不主动增加完整正文获取；复用既有业务参数、必要摘要和正常 partial-success 语义 |

仅凭本地测试能证明无主动正文选项或额外 Fetch，不能断言供应商费用为零。对无法由既有请求契约确认的正文选项，实施时查证厂商文档/fixture，不臆造开关或改坏摘要。Fetch 专属请求及原有成功/失败解析不因 Search 修改而改变。

### 5.2 Search workflows

- 只含 URL/title/snippet：合法记录进入 NEED_FETCH；JSONL 只有既有字段；无正文处理 LLM 调用。
- 自然附带 raw-only、content-only、同源 raw+content：进入 NEED_JUDGE；Search 的 judge/clean/safety/focus 调用计数全部为 0。
- 空白正文、无正文、非空摘要 fallback、URL normalization/redirect、重复 URL、无效 URL/空摘要等保留既有合法/拒绝语义；空白正文不冒充已验证正文。
- 同 URL 多 provider 返回不同正文：first nonempty abstract 保留，第一份候选成对写入，不混合 A 的 raw 与 B 的 content；重复 metadata 不降级已存在状态。
- 一个 provider 的所有 credential 失败、另一个 provider 成功/合法空结果：Search 仍按现有规则成功输出；全失败才 ALL_PROVIDERS_FAILED。
- keyword/paper/llm/agent search 的逻辑 provider/model/scope 过滤、学术聚合、OA no-match/best-effort failure、结果 writer/协议继续通过。LLM/Agent Search 的本身调用不能误计为额外正文 stage。
- 已 READY / UNAVAILABLE 的 URL 再出现于搜索结果，保留原状态/正文及可见的合法摘要；不恢复终态、不触发 Judge 或额外 fetch。

### 5.3 FetchScheduler

| 场景 | 断言 |
|---|---|
| 显式顺序 A/B/C 全 ready | 从 A 开始；首个 accepted 后立即停止 |
| A 的全部 credential rate/capacity blocked，B ready | 跳过 A，先执行 B；不阻塞等待高优先级 |
| A 有一组 blocked、一组 ready | A 仍是可用逻辑 provider；池按策略选 ready 成员 |
| 所有 provider 暂不可用 | 等待任意候选可用；budget 超时执行失败，取消清理，无忙等 |
| A credential 1 重试失败、credential 2 成功且 Judge 通过 | 不调用 B，Judge 只对最终逻辑 Fetch 候选执行一次 |
| A 所有 credential 执行失败 | 进入下一个逻辑 provider，无同池第二轮 |
| A 候选 cheap_check / Judge 明确拒绝 | 转 B，不因内容质量问题换 A 的其他 credential |
| Judge LLM pool 执行失败 | 直接 LLM stage error，不继续 Web provider fallback，不改变 URL terminal 状态 |
| 所有候选纯执行失败 | execution_failure |
| 至少一次实际候选 semantic_failure 且无 accepted | 维持既有 semantic_failure 聚合规则（含混合执行失败） |
| 取消 | 停止所有后续 retry/fallback，清理 provider 与 credential 选择 lease |

确认同一 URL 的不同 Fetch Provider 不并行竞速；成功/失败的 credential lease 在 Judge 开始前释放，不因 LLM stage 等待占用 Web 凭证容量。多个独立 URL 仍可并发。

## 6. URLStore / Fetch State Machine

### 6.1 状态和正文不变量

| 起始状态 | 输入 / 事件 | 最终状态与调用断言 |
|---|---|---|
| 不存在 | 合法 metadata-only admission | NEED_FETCH，无正文 |
| 不存在 / NEED_FETCH | 合法非空自然正文 | NEED_JUDGE，候选完整同源，无 Judge |
| NEED_JUDGE | raw-only，Judge 通过 | NEED_CLEAN；再 Clean→NEED_SAFETY→Safety→READY，不 Fetch |
| NEED_JUDGE | content-only / raw+content，Judge 通过 | NEED_SAFETY→READY，不 Fetch、不 Clean |
| NEED_JUDGE | cheap_check 或 Judge 明确拒绝 | 原 raw/content 同时清空→NEED_FETCH；随后实际 Fetch |
| NEED_JUDGE | Judge 执行失败/取消 | 原候选及状态不变；下一次仅重试未完成 stage |
| NEED_FETCH | FetchScheduler accepted raw-only | NEED_CLEAN，不重复 Judge |
| NEED_FETCH | FetchScheduler accepted 已清洗候选 | NEED_SAFETY，不重复 Judge/Clean |
| NEED_FETCH | 无 provider / 所有实际候选纯执行失败 / 取消 | 保持 NEED_FETCH；现有错误；不写 UNAVAILABLE |
| NEED_FETCH | Scheduler 最终 semantic_failure | UNAVAILABLE，返回既有 unavailable message |
| NEED_CLEAN | Clean 成功 | content+NEED_SAFETY 原子写入 |
| NEED_CLEAN | Clean 失败/无效输出/取消 | 保持原状态和 raw，下一次不重做 Fetch/Judge |
| NEED_SAFETY | Safety 通过 | READY，保持 content |
| NEED_SAFETY | Safety 明确拒绝 | UNAVAILABLE，不返回正文、不调用 Focus |
| NEED_SAFETY | Safety 执行失败/取消 | 保持原状态；下一次只继续 Safety |
| READY | 重复 Fetch（无 focus） | 直接返回相同正文，Fetch/Judge/Clean/Safety 全为 0 |
| READY | 不同 focus / 同 focus 再次调用 | 仅生成请求级 summary；无正文 stage 重跑；不新增 summary cache |
| READY | Focus Summary 失败/取消 | 保持 READY，不返回完整正文作为降级 |
| UNAVAILABLE | 再次 Fetch / Search admission | 仍 UNAVAILABLE；Fetch 无上游调用，Search 不自动恢复 |

关键额外反例：自然缓存候选被拒绝后，若没有 fetch provider 或所有实际 fetch 纯执行失败，保持 NEED_FETCH 并返回错误，不能因为先前缓存候选语义拒绝而永久禁用 URL。若后来 Search 附带另一个候选，可重新从 NEED_JUDGE 验证；不得未经验证返回。

### 6.2 并发、失败恢复与安全边界

- URLStore 仅通过受控原子更新维护五字段 immutable snapshot；验证非法状态/字段组合不能成为可返回正文（例如 READY 无内容、NEED_CLEAN 已写 content、NEED_JUDGE 空白候选）。不采用独立字段合并而混入其他来源正文。
- 同一 URL 的两个并发 body admission，在受控 barrier 下验证第一份候选原子写入；后来的候选/metadata 不覆盖现有验证状态。不同 URL 的合法处理仍独立。
- Search 在 Fetch 的 Judge/Clean/Safety await 期间返回同 URL 正文：共享 keyed lock；写入方取得锁后重新读取记录，不使用旧 snapshot 导致 READY→NEED_JUDGE 或正文错配。缓存候选被拒绝并成功换正文的场景也覆盖。
- 相同 normalized URL/focus 的同时 Fetch 复用既有 singleflight；不同 focus 同一 URL 共享正文处理，按各自请求生成摘要；不同 URL 不共用 keyed lock。
- 测试 follower cancellation 不影响 leader 的既有保护，leader cancellation/共享结果的传播保留原 singleflight 契约；不引入所有权转移或新的跨请求取消机制。任一取消后锁与 waiter 正确清理。
- 在每个 stage 成功提交前后注入 execution failure 或 cancellation，确认仅保留已经原子提交的阶段，不产生半写入、错误 READY 或意外 terminal；下一次调用从合法已提交状态恢复。
- INVALID_URL、URL_NOT_ADMITTED、无合法摘要等保留旧 admission 和输入错误契约；无效状态或被 Safety 拒绝的正文不得作为 full content 或 summary 输入。
- READY 的 Safety 缓存只针对该份不可被 Search 替换的正文；不新增刷新、TTL、持久化、按 focus 的安全状态或新的安全模型策略。

## 7. Doctor / Observability / Regression

- Doctor 逐个启用/引用 HTTP provider 的 credential 验证配置，覆盖第二组凭证无效、无需认证、You.com 可选认证及 open-webSearch 多 endpoint 的已有健康检查。某成员错误不能被另一成功成员掩盖；保留原 exit code / socket / filesystem checks，不调用收费搜索或抓取检查额度。
- 选择、retry、切换、rate/capacity wait、exhaustion 和 URL 状态转换事件使用非秘密 namespace/provider/index 标识；能够区分不同 credential 的实际 HTTP attempts，保留现有 request ID、UTC 时间、body preview、诊断和日志等级契约。
- 用合成秘密哨兵测试多 credential 的错误、日志、Doctor 及清理路径：不暴露真实认证、私有 email、带认证的 URL、未脱敏响应或异常字符串；保留 LLM/Agent 输入 body cap 和 UTF-8 截断回归。
- 原 public ErrorCode、CLI/Socket envelope、stdout/stderr、unavailable message、JSONL、provider/model filter、Agent lifecycle、arXiv query、Firecrawl PDF/parser 配置、You.com MCP、provider proxy/direct、请求超时与既有 Retry Policy 均纳入全量回归。历史设计文档保留为历史记录；更新当前生效的公开配置说明与示例。
- 旧测试中有关默认并发、旧 HTTP provider 格式、Search 自动 Judge、重复 Fetch 每次 Safety 的断言需要改为本次已认可的新语义；不以删除失败断言代替验证。非本次涉及的旧测试应保持通过。

## 8. End-to-End Acceptance (Mock / Local)

至少以下场景应通过现有 CLI/daemon acceptance harness，并观察真实运行时组装、实际出站 mock 请求和结果内容，而不是仅检查组件返回值：

1. **显式 Fetch order + 多凭证 fallback**：Search 收录 URL，无正文；A provider 第一组 credential 重试耗尽、第二组成功并通过 Judge，后续 provider 不被调用；验证状态和结果。
2. **无隐藏限流**：新配置省略全部 limits，向不同 URL/独立 Search 请求提交受控并发操作；验证不存在全局或 provider 默认排队，HTTPX 构造无活动连接数上限。
3. **独立限流与策略**：同一 provider 两组独立窗口，分别执行 Round Robin / Fill First；被限流成员暂时跳过，恢复后可再次使用；实际 retry 计数正确。
4. **自然正文复用**：Keyword Search 返回自然正文但不调用 LLM；首次 Fetch 延迟 Judge、按需 Clean、安全检查；重复 Fetch 直接复用 READY，指定 focus 时只调用摘要模型。
5. **缓存正文拒绝后的 Fetch 恢复**：缓存候选被 Judge 拒绝，实际 Fetch 返回另一份同源 candidate；验证原内容清空且不混拼，后续 READY 正文正确。再覆盖所有实际 Fetch 纯执行失败时状态仍为 NEED_FETCH。
6. **多类 HTTP + Agent**：Web/Academic/OA/LLM 的池能独立运行；LLM search 和正文 stage 共享配置 alias 的凭证池；Agent 保持 CLI，按显式规则限制 invocation；逻辑 provider 名称和现有 filter 不变。
7. **失败与取消**：在 retry、选择等待、正文 stage 等边界失败/取消；客户端错误、缓存状态、lease 清理和 daemon shutdown 符合 Error Handling，后续独立调用仍可工作。
8. **迁移与诊断**：旧 config 明确拒绝，新示例可以在合成 env 下解析/Doctor；输出可定位各组 credential，不泄漏秘密，无意外收费网络请求。

## 9. Implementation Verification & Exit Criteria

### 9.1 命令（实施后执行）

先运行受影响的目标测试，再运行项目现有完整检查。新增测试文件若采用不同命名，应纳入对应目录，不要求固定名称。

~~~bash
uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py tests/unit/test_url_store.py
uv run pytest tests/runtime tests/scheduler tests/orchestrators
uv run pytest tests/providers tests/doctor tests/observability
uv run pytest tests/acceptance tests/cli tests/daemon tests/docs
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
~~~

按项目现有 CI/开发工具链覆盖支持的 Python 版本。实施报告需记录实际运行命令、通过/失败/跳过数量及失败原因。测试未运行、缺少环境、live gate 默认跳过不得表述为通过。

### 9.2 真实 API Smoke（可选，非默认）

- 用户明确授权后，以隔离的临时配置、单个或少量指定 provider、短查询和有限次数做最小验证，避免触发默认全 provider fan-out。若新 schema 尚未部署到用户运行配置，不自动改写现有配置，也不停止无关 daemon。
- 只验证已授权的 endpoint 及凭证，凭证归属/额度独立性由用户确认。不刻意耗尽真实免费额度、不人为制造大量 429、不进行大规模并发或真实失败风暴。凭证切换、无默认 cap 与 retry 时序的主要验收依靠确定性 mock。
- 记录实际尝试数量和成功/失败；真实请求可能产生费用。缺乏多组真实凭证或服务不可用时说明覆盖限制；不伪造账单或额度结论。

### 9.3 退出标准

- 本文第 3–8 节有对应测试及有效断言，尤其是无默认并发/速率限制、独立 credential、一次 fallback、所有重试计数、Search 零正文 stage、显式 URL 状态机和 Safety 复用。
- 公共配置及 Doctor 测试覆盖新 schema 和 Breaking Change，所有关键错误/取消路径可证明无资源泄漏、无隐藏重试轮次、无状态倒退或凭证泄漏。
- 原有未变更契约的全量回归、Ruff 与 mypy 检查通过；如真实冒烟未运行，明确标记，不阻塞纯 mock 验收。
- 不为了通过测试引入默认 limiter、凭证冷却/额度状态、vendor-specific 认证调度、Agent 多账户支持或其他未获认可的新功能。

本阶段只编写 Testing Spec，不执行实施或测试。三份 Spec 需通过用户最终书面 review gate 后方可提交。
