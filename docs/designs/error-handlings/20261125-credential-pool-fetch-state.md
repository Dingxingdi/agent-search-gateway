## Error Handling: Credential Pool, Explicit Fetch Ordering and Lazy URL Processing

- 日期：2026-11-25
- 基线：`8da7aeb`（v2.0.0）
- 工作树 / 分支：`design-credential-pool-fetch-state-20261125`
- 对应架构：`../architectures/20261125-credential-pool-fetch-state.md`
- 状态：Error Handling 评审稿；遵循已认可的 Architecture，不包含生产代码修改。

### 1. Error Model & Ownership

保持 `errors.py` 现有公开错误码与 CLI/Socket envelope。不新增 credential 专属公开错误码，不修改成功输出格式。内部沿用 InputFailure、ExecutionFailure、ProtocolFailure、ConfigFailure 和现有 FetchOutcome；必要的凭证选择/等待诊断仅使用安全内部 reason。

| 类别 | 处理者 | 核心行为 | 是否影响 URL 可用性 |
|---|---|---|---|
| 启动配置错误 | Config / Runtime / Doctor | 拒绝无效配置；不启动部分可用 Runtime | 否 |
| 输入或请求状态错误 | CLI / Daemon / Orchestrator | 立即失败；不重试或换凭证 | 否 |
| 可重试执行错误 | 当前 adapter / HttpJsonExecutor / LLM client | 复用原 Retry Policy，在同一 credential 内重试 | 否 |
| 终端 provider 执行错误 | CredentialPool | 释放本次 lease；尝试下一组未尝试 credential；全部失败后返回 provider failure | 否 |
| 正文语义拒绝 | FetchScheduler / FetchOrchestrator | 拒绝候选；必要时切换逻辑 fetch provider | 仅按第 5 节既有终止规则 |
| 正常空结果 / OA no-match | 既有 adapter 与上层 | 成功，不触发 credential fallback | 否 |
| 取消 / shutdown | 当前执行层及 Daemon | 立即传播取消，释放资源；不启动新的 retry/fallback | 不推进未完成阶段 |
| 程序缺陷 / 非预期异常 | 现有异常边界 | 保留异常因果链与内部诊断；不伪装为凭证故障 | 否 |

原则：凭证失败是执行错误，不是目标网页不可用；不能仅因 key 无效、余额不足、429、网络失败、缺少限流许可或所有凭证失败而写入 UNAVAILABLE。CredentialPool 不负责判断正文、推测计费规则或解释各 vendor 私有错误码。

### 2. Configuration / Startup / Doctor

#### 2.1 配置校验

- 保留现有 registry 对 provider 是否支持 search/fetch、必需认证、endpoint、LLM protocol/model 和 vendor options 的校验；对每组 credential 的有效配置分别验证。
- 启用或被引用的 HTTP provider 没有非空合法 credentials 数组、凭证字段类型错误、缺少所需环境变量、存在未注册字段、策略非法或 endpoint/认证组合不受支持：返回 CONFIG_ERROR。禁止仅丢弃错误成员后继续启动。
- 删除的 default_max_concurrency 与 HTTP provider 级旧 endpoint/认证/限流字段，包括新旧混用，均返回明确的迁移错误；不得静默应用、忽略或转换旧限制。
- max_concurrency、rate_limits、min_interval_seconds 均独立可选；字段不存在代表无限制。空 rate_limits 表示无窗口限制；显式非正值、无穷、NaN、布尔值冒充数字或不符合现有窗口 schema 的值报配置错误。
- fetch_order 中未知名称、重复名称、不支持 fetch 的注册名称报配置错误；合法但未启用的 fetch provider 不参加 Runtime 调度。未列出的启用 fetch provider 按原声明顺序追加，不当作配置遗漏错误。
- 同一 HTTP provider 中各组 credential 的额度独立性、账户权限及 model/endpoint 兼容性是用户配置的前提；不主动向服务商查询余额、账户归属或 model 列表。不能用免费账户数量推断额度，也不引入自动合并或跨账户聚合限制。

#### 2.2 启动失败与资源回收

在对外接受请求前完成配置解析及构造。client、adapter、executor、pool 或 socket bind 发生失败，按现有启动错误边界报告；对已构造的资源执行清理（含部分 Runtime 构造失败），每个实际 HTTP client 关闭一次，不保留半初始化 pool。清理失败记录安全诊断，不能覆盖原始失败原因。不得降级到旧配置格式或为缺失的限制填入默认值。

#### 2.3 Doctor

Doctor 使用相同配置校验，逐个逻辑 provider/credential 报告可验证配置及现有适用健康检查结果。无认证的合法 endpoint 不要求 API key。保留原 Doctor 输出及退出码语义；缺少必需配置或既有检查失败时明确报告，不能因另一组凭证有效而隐藏该成员的错误。健康检查不主动执行付费 Search/Fetch，不验证未知的余额或费用上限，不新增定时探测。Doctor 成功不表示上游凭证一定仍有效或有剩余额度。

### 3. CredentialPool / Retry / Rate & Capacity

#### 3.1 单次逻辑调用中的失败顺序

凭证池只增加一层有限的 fallback，不重写现有 HTTP Retry、Retry-After、LLM protocol retry 或 adapter 局部重试/partial-success 机制。

1. 按 round_robin（默认）或 fill_first 选择当前可用且本次尚未尝试的 credential，并获得可选 capacity lease；无 gate 时直接通过。
2. 绑定该 credential 执行 adapter 操作。所有内部 HTTP 分支、retry 以及既有 LLM protocol retry 仍使用该组配置；每次物理尝试按对应 rate policy 计数。
3. retryable error 先交现有 Retry Policy。仅在该 adapter operation 最终抛出 provider ExecutionFailure 时进入 credential fallback；非 retryable provider error 不强制耗尽 max_attempts。
4. 释放该成员的 lease，保留安全失败诊断；本次调用不再次选择该成员。若有未尝试成员，按同一策略选择下一组可用成员。
5. 成功立即结束，包括空搜索列表、OA no-match 和现有 adapter 接受的 partial success。若没有未尝试成员，返回本逻辑 provider 的 execution failure，由上层现有错误聚合逻辑处理。
6. InputFailure、ConfigFailure、取消或未被现有 adapter 解释为 provider execution failure 的程序缺陷直接传播到原异常边界，不作为换凭证理由。

本次 attempted 集合在逻辑调用结束后丢弃。下一次独立请求可重新选择此前失败的 credential（仍受本地限流规则限制）。不创建持久化或运行时失败历史、冷却截止时间、禁用状态、额度状态或熔断器。

| 上游/本地事件 | 本次 credential 行为 | 后续行为 |
|---|---|---|
| HTTP 408/429/5xx、现有可重试网络错误 | 按现有 Retry Policy，保留 Retry-After 解释 | 最终执行失败才换下一组 |
| HTTP 401/402/403 或其他既有不可重试 HTTP 错误 | 保持原 executor 分类，不额外重试 | 终端 provider ExecutionFailure 后换下一组；不判断额度/权限是否永久失效 |
| vendor response business error | 复用已有 adapter/inspector 的错误分类 | 只有现有终端 ExecutionFailure 才换组；不新增厂商专属识别 |
| LLM HTTP / protocol error | 保留该 credential 内原有 retry；不嵌套新增 retry 层 | LLM client 最终 ExecutionFailure 后切换 |
| 合法 HTTP/provider 响应但返回空结果或无 OA 匹配 | 成功 | 不换组 |
| Search 返回正文但质量未知、Fetch 返回候选 | 按原 adapter contract 成功返回 | 交 URL 正文处理层，不因候选内容换组 |
| 输入错误 / 已取消 | 不 retry | 不 fallback |
| 本地 rate 等待超时 | 当前 operation 终止，沿用 rate_limit_wait_timeout 分类 | 本次剩余凭证按策略处理；不新增冷却 |

这里的状态码仅描述既有 HTTP retry 行为，而非新增分类规则；已有 ProviderResponseFailure / ProtocolFailure 的判断继续由原组件完成。LLM/Agent Search 的 markdown parser 位于原上层流程；其失败沿用既有 pipeline 边界，不因本次凭证池改造新增重放或搜索轮次。

#### 3.2 等待、额度计数与并发安全

- 可用性预检查不消费 RateGate 许可；实际物理 HTTP attempt 发送前原子获取许可。若检查后另一任务/分支先获得许可，使用现有 executor 有界 rate 等待，不超发、不重复计费。
- 所有 retry 仍使用同一 credential 的 rate policy；不能因为换凭证或重试而绕过窗口/最小间隔。已发出的尝试不因失败或取消而退回速率额度；未真正进入发送边界的等待不预扣许可。
- 配置了 max_concurrency 时，lease 覆盖逻辑 adapter operation（含内部 retry）；release 在 finally 中执行一次。HTTP client 与 façade 不重复获取同一 capacity gate。Fetch operation 完成后先释放 credential lease，再调用独立 Judge LLM stage。
- 无 max_concurrency 时不创建等待队列或有限 semaphore；无速率规则时不进行 rate 等待。代理与直连不保留 HTTPX 活跃连接数默认上限；不得把连接池默认超时或连接池限制当作替代的限流机制。
- 候选存在但全受限时，按现有 request_timeout_seconds 作为选取等待预算，等待 capacity 通知或最早 rate 可用时间。反复唤醒/重新检查消耗同一选择等待预算，不能因虚假唤醒反复重置；首次 fetch rate 预等待与 executor 的现有 budget 协调，不重复给予完整预算。
- 无可尝试成员与暂时全部不可用不同：前者直接失败，后者有限等待；不无限等待或 busy-loop。已开始的单凭证 Retry-After 及 per-attempt timeout 保持原行为，不人为增加新的逻辑调用总 deadline。
- 取消发生在 pool wait、try-reserve 交接、rate wait、HTTP send 或 retry sleep 任一边界，必须清理 waiter/已获得的 lease，传播 cancellation，不能重新选凭证。跨并发请求的 RR 游标推进与容量占用必须原子且不在锁内等待网络。
- Agent 保持既有 CLI 生命周期。若配置 gate，仅对 Gateway 发起的 CLI invocation 计数；spawn/timeout/取消时保证原 subprocess reap 与 gate release，不进入 HTTP credential fallback。无 gate 时不得添加默认限制。

### 4. Search / OA / Fetch Scheduler Error Boundaries

#### 4.1 Search 及 OA

- Keyword/Paper/LLM/Agent Search 保留原 provider/invocation 级并发 fan-out、过滤与结果聚合。HTTP provider 的内部凭证失败由 pool 消化；最终 failure 才使该逻辑 pipeline 失败。
- 任一逻辑 pipeline 正常完成（包括合法空结果），按现有规则输出结果 JSONL；全部适用 pipeline 执行失败才返回既有 ALL_PROVIDERS_FAILED 等错误。取消整条请求不能伪装为某个 provider 的普通失败。
- URL/摘要/redirect/结果结构校验保留既有拒绝或失败策略。自然附带正文不是 Search 成败的必要条件；正文候选为空或不可用时允许合法 metadata-only admission。Search 不调用 Judge/Clean/Safety/Focus Summary，因此正文 LLM stage 失败不再使 Keyword Search pipeline 失败。
- Academic 聚合、LLM/Agent restricted parser 和 OA enrichment 保留现有错误边界。OA credential 全部执行失败时仍为 best effort：保留已有 paper record，不丢弃其 metadata，不影响其他 paper/provider 的结果。
- ResultWriter 失败仍按现有请求错误处理，不触发 HTTP credential fallback；已原子提交的合法 URL admission 不做跨请求回滚。

#### 4.2 FetchScheduler

- 无启用 Fetch Provider 且 URL 处于 NEED_FETCH：返回 NO_URL_FETCH_PROVIDERS；状态不变。
- 按 fetch_order 及追加顺序，选择至少一组 credential 当前可用的逻辑 provider；当前全部受限的池先跳过。全部候选暂不可用时有限等待任一候选，耗尽等待预算为执行失败。
- 进入选定逻辑 provider 后，先完成其 credential retry/fallback；pool 最终失败属于该逻辑 provider 的 execution failure，转入剩余 provider，而不是尝试同一 pool 的第二轮。
- 逻辑 fetch 成功返回候选后执行既有 cheap_check + Judge。正文语义拒绝属于 provider-level semantic_failure，切换下一个逻辑 provider，不因正文质量换同一 provider 的其他凭证。
- Judge 自身执行失败（含 Judge LLM pool 重试及 fallback 全部失败）直接传播既有 LLM stage failure；不能把 Judge 的依赖错误计入 Web 凭证失败、继续换 fetch provider 或据此标记 URL UNAVAILABLE。
- 保留现有 FetchOutcome 分类和终止规则：首个 accepted 结束；所有候选结束后，若至少一个实际 fetch 候选发生既有 semantic_failure 且没有 accepted，结果为 semantic_failure；否则为 execution_failure。暂时受限、等待超时、凭证错误、无响应均不能凭自身计为 semantic_failure。
- 混合语义拒绝与执行失败时保持现有语义拒绝优先的终止规则；这是既有行为，并不意味着每个 provider 都已经成功返回且拒绝。取消或 Judge 执行异常不进入终止结果聚合。

### 5. URL 状态机：失败后的恢复

状态字段及转移以 Architecture D6 为准。URLStore 以 immutable snapshot 原子写入正文对与状态；不单独写入半个 candidate，不让 adapter 直接修改 URLStore。以下为错误路径及后续行为：

| 当前状态 / 操作 | 正常成功 | 明确拒绝 | 执行失败 / 取消 |
|---|---|---|---|
| 新 URL 或 NEED_FETCH 收录自然正文 | NEED_JUDGE，保留同源 candidate | 空白候选不写入正文；合法 metadata 保留 NEED_FETCH | 不提交未完成更新 |
| NEED_JUDGE / cheap_check + Judge | NEED_CLEAN 或 NEED_SAFETY | 清空原候选两字段，进入 NEED_FETCH，继续本次抓取 | 保留 NEED_JUDGE 与原候选 |
| NEED_FETCH / FetchScheduler | 原子写入 accepted candidate，NEED_CLEAN 或 NEED_SAFETY | 最终 semantic_failure → UNAVAILABLE | 保留 NEED_FETCH；返回 execution failure |
| NEED_CLEAN / Content Clean | 原子写入有效 content，NEED_SAFETY | 无额外语义终止规则 | 空白/无效输出按既有执行错误处理，保留 NEED_CLEAN |
| NEED_SAFETY / Safety | READY | 明确否决 → UNAVAILABLE | 保留 NEED_SAFETY，返回 LLM_STAGE_FAILED 或取消 |
| READY / 请求级 Focus Summary | 返回摘要；仍 READY | 不新增正文拒绝状态 | 保留 READY，不回退返回完整正文 |
| UNAVAILABLE / 再次 url-fetch | 返回既有 unavailable message | 仍 UNAVAILABLE | 不再发起 provider 或正文 LLM 调用 |

#### 5.1 特别边界

- 自然附带的缓存候选被拒绝不等价于 FetchScheduler 最终语义拒绝。清空候选后，如果没有 fetch provider 或所有实际 fetch 尝试纯执行失败，保持 NEED_FETCH，不因为缓存候选曾被拒绝而 terminal。
- READY 表示同一份最终正文已通过 Safety；不重新 Judge/Clean/Safety。当前不支持正文刷新，Search 不得覆盖 READY。若将来新增刷新，必须重新进入验证状态，不能沿用旧 READY。
- LLM stage 异常保留最后一次成功提交的状态，下一次请求从该状态继续，而非重做已经完成的 stage。UNAVAILABLE 不因后续 Search 或凭证切换而自动恢复。
- Focus Summary 是 request-local，不属于 URL 状态机，不缓存、不降级为 full content；执行错误不污染 READY 正文。
- INVALID_URL / URL_NOT_ADMITTED 等输入错误不改变 store。合法 metadata admission 仍采用原摘要保留规则；同一 URL 的正文不可跨来源拼接。

#### 5.2 并发与取消

沿用 URL keyed lock 和 singleflight。Search 写正文与 Fetch 转移共享同一 URL 的串行边界；取得锁后必须重新读取当前状态，禁止使用等待前的 snapshot 覆盖新状态。重复 Search 可保留已有摘要，不覆盖已选定或验证的正文。取消释放锁、清理等待者并保留已原子提交的合法阶段；不提交尚未完成的阶段。相同 URL 的共享请求遵循既有 singleflight 取消/清理语义，不取消无关 URL 或其他调用者仍需要的共享工作。

### 6. Observability / Public Errors / Lifecycle

- 延续现有错误码与 sanitized message：配置错误 CONFIG_ERROR，输入错误 BAD_REQUEST / EMPTY_QUERY / INVALID_URL / URL_NOT_ADMITTED 等，provider 失败 ALL_PROVIDERS_FAILED，正文 LLM stage 失败 LLM_STAGE_FAILED，协议和 shutdown 错误沿用既有边界。公开错误不新增 credential ID 作为 provider 名。
- 诊断使用逻辑 namespace/provider name + credentials 数组下标等非秘密标识，必要时记录 operation、attempt index、fallback、等待原因/耗时、是否耗尽未尝试凭证、URL 状态转换、取消。继续既有 HTTP event、retry log、body preview 与脱敏机制。不得输出认证原值、header、带认证的 URL、contact email、未经脱敏的响应或异常字符串；不因聚合多组 failure 而扩大敏感信息范围。
- 当 pool exhausted 时保留足以定位失败成员与原因的安全诊断，同时对外仍返回原逻辑 provider 执行失败。清理/诊断错误不能覆盖主错误。
- CLI stdout/stderr、JSONL 与 unavailable message 按现有契约。CLI 超时/断开连接后 daemon 的取消处理沿用既有机制；不得在调用取消后启动新 HTTP retry 或 credential fallback。
- graceful stop 保持现有 shutdown 机制，拒绝新任务，终止/清理未完成工作与等待者，关闭已创建的资源一次；不保存 pool cursor、URL 状态、失败 history 或额度状态。

### 7. Review Boundaries

本节故意不新增凭证级错误码表、余额检查、跨请求冷却、额外重试轮次或默认限流值。与 Architecture 一致的细化点是：选择等待预算不因重检而重置；首次 fetch 等待与 executor 共享原 budget；异常回收不得掩盖主失败；混合 semantic/execution 失败沿用旧优先级；缓存候选拒绝单独不足以标记 URL terminal。后续 Testing Spec 应覆盖这些边界，特别是取消、并发 lease 释放、凭证切换、速率计数及状态恢复。当前未实施或运行测试。
