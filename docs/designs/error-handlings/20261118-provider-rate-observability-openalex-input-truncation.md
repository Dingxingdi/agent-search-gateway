# Error Handling: Provider 速率限制、OpenAlex Key、出站截断和双向 Body 日志

- 日期：2026-11-18
- 状态：Brainstorming 错误处理方案已由用户确认；尚未实施代码
- 关联：[架构](../architectures/20261118-provider-rate-observability-openalex-input-truncation.md)
- 基线：edcc627；仅在独立工作树编写规范

## 1. Goals & Non-goals

### Goals

1. 明确配置无效、速率等待、真正上游发送、重试、取消、输入不可截断、OpenAlex 鉴权失败和日志预览失败时各自的行为。
2. 确保不会因为一次失败或重试越过提供商的多窗口限流；也不会因长窗口限额耗尽令整个工作流无期限挂起。
3. 尽量保留现有 ErrorCode、RetryPolicy、HTTP 200 语义错误检查、Fetch 备选提供商和 OA Resolver 的非阻断性行为。
4. DEBUG 下记录每次实际调用的 Request/Response Body 预览，包括失败、重试、解析失败；不暴露 API Key、环境变量、Header、完整 URL 查询参数或被输入截断的中间部分。
5. 不让调试日志或其准备过程影响实际请求的成功、失败、取消。

### Non-goals

- 不重新设计供应商各自的业务错误分类；已有 HTTP 200 供应商错误 Inspector、候选逐条拒绝、原有重试次数均沿用。
- 不新增按月费用预算、Credits 计量、跨进程速率协调、自动分析套餐。
- 不允许输入截断系统 Prompt、Focus 或强制字段，不额外调用 LLM 摘要。
- 不自动将 OpenAlex 未授权请求降级到匿名方式。
- 不引入新的外部公开 ErrorCode；用已有 ConfigFailure、ExecutionFailure、ProtocolFailure 和安全 reason 表述。

## 2. Error Taxonomy & Handling Matrix

| 场景 | 检测点 | 重试/等待 | 最终处理与诊断 |
|---|---|---|---|
| rate_limits 不是数组、窗口不是对象、字段拼错 | config.py 解析 | 不重试；不建 Runtime | ConfigFailure(CONFIG_ERROR)，提示字段路径和期望类型，绝不回显原始敏感配置 |
| requests 为零、负数、bool、浮点数或 period_seconds 非正、NaN/Inf | config.py 解析 | 不重试 | ConfigFailure(CONFIG_ERROR)，只报告哪一个窗口无效 |
| min_interval_seconds 无效，或 LLM/Agent byte cap 不是正整数 | config.py 解析 | 不重试 | ConfigFailure(CONFIG_ERROR)，启动失败，不偷偷回退到默认 |
| 启用 OpenAlex 但无 api_key_env 或变量缺失/空值 | Registry + config.py | 不重试；不发 HTTP | ConfigFailure(CONFIG_ERROR)；可标识环境变量名称，不打印值 |
| OpenAlex 已禁用且没有 Key | config.py | 不适用 | 正常启动，不要求环境变量 |
| 速率窗口尚未开放 | RateGate | 先异步等待、可取消 | rate_limit_waiting 诊断；此时不发网络请求、不消耗新许可 |
| rate limiter 需要等待很久，超过本地等待预算 | HttpJsonExecutor + RateGate | 本 provider 不盲目重试，转后备/provider failure | ExecutionFailure(ALL_PROVIDERS_FAILED, reason=rate_limit_wait_timeout)；不发 HTTP、不产生虚假的 Response Body 预览 |
| 取得 RateGate 许可后 TCP/DNS/Timeout 失败 | HttpJsonExecutor | 按现有 RetryPolicy 判断重试；下次发送仍需新许可 | 保留 transport 类别、尝试序号、elapsed_ms 和 Request 预览；无 Response 则 null |
| HTTP 408/429/5xx | HttpJsonExecutor | 现有 RetryPolicy；有效 Retry-After 优先，重试前再次过 RateGate | 每个收到响应的 attempt 都有 Body 预览；终态 http_failed 保留状态码 |
| HTTP 400/401/403/404/422 等非暂时性失败 | HttpJsonExecutor | 沿用不重试 | 终态 status failure；OpenAlex 401/403 不尝试匿名重发 |
| HTTP 200 有供应商声明的业务失败 | response_inspector | 仅已有证据表明 retryable 时重试 | 现有 ProviderResponseFailure，不把业务失败记为成功 |
| HTTP 200 但 Body 不符合 JSON、编码损坏 | request_json/Parser | 维持现有 decode/ProtocolFailure 规则 | 先写双向 Body 预览，再产生 decode 错误；不因解析失败漏掉上游响应 |
| LLM 全部受保护文本/额外请求字段已超过 byte cap | LLM Client 预发送检查 | 不重试；不发 HTTP | ExecutionFailure(LLM_STAGE_FAILED, reason=input_budget_unfit)，仅安全字节统计 |
| Agent system/role 标记已超过 prompt cap | Agent Runner 预进程检查 | 不重试；不启动进程 | ExecutionFailure(ALL_PROVIDERS_FAILED, reason=input_budget_unfit)，保留既有 Agent 失败边界 |
| 裁剪函数遇到多字节字符、标记不够空间、不可编码值 | 纯函数 + Client/Runner | 可根据空间确定性截断；不可恢复则预发送失败 | 不发送超限或非法编码的输入、不替换/损坏系统指令 |
| Agent 经截断后仍发生系统 E2BIG | run_antigravity | 维持现有不重试/清理 | 原有 argument_limit 分类；日志记录实际 prompt 字节数，检查其他 argv/env 影响 |
| LLM 已在 byte cap 内，上游仍返回模型 context exceeded | HttpJsonExecutor | 视为普通非重试参数错误；不在内部无限缩短重发 | 保留 HTTP 状态和安全供应商诊断，提示 byte cap 不等于 Token 上限 |
| HTTP 或 Agent 原始 Body 为二进制、不可解码或空 | Body Preview Helper | 不影响实际解析/回传 | 记录字节数，预览为空或使用安全替换文本，标明 text/binary，不把原始二进制写日志 |
| Body 脱敏/预览序列化失败 | observability.py | 不重试上游、不改变结果 | Body 预览置 null 或跳过该预览事件；可写固定 body_preview_failed 标记，不含原文 |
| 文件轮转、磁盘写入、日志格式化失败 | 现有安全日志 Handler | 不重试上游 | 沿用安全降级/紧急固定提示，不把日志异常提升为业务异常 |
| 网关任务被取消、Daemon 关闭 | RateGate/HTTP/Agent | 不重试 | 传播 CancelledError；清理等待任务、并发租约和 Agent 子进程 |

## 3. Rate Limiting, Wait Budgets & Retry Contract

### 3.1 每次真正发送都计数

- RateGate permit 必须紧挨 client.request 之前取得；同 provider 的 Search 和 Fetch 共用状态，Academic/OA 单独计算。
- 多时间窗和 min_interval_seconds 必须全部满足。检查与预约为锁内原子操作；每个实际 attempt 最多预约一次。
- 请求在许可取得后即使连接失败、超时或返回 429，也算一次本地预约；下一次 Retry 重新取得许可。这比仅统计成功响应严格，但能防止重试风暴突破本地免费速率。
- permit 之前取消的等待不消耗名额；预约后取消的请求视为已尝试发送并保留计数，避免许可归还造成短时间突发。
- Retry-After 是服务端提示的最早时间，不能由本地速率许可抵消；实际发送必须同时满足服务端重试时间和本地 RateGate。
- 不为 local rate-limit 等待单独增加一次 Retry；max_attempts 仍是上游尝试的上限，不因为等待失败而无限循环。

### 3.2 长窗口耗尽不能无限等待

**推荐的最小侵入方案**：RateGate 的每次许可等待使用现有 retry.request_timeout_seconds 作为**本地等待上限**，与真正的 HTTP timeout 独立计时但共用可配置的秒数。例如该设置为 180 时，等待 20 秒的 QPS/RPM 限制可正常排队；若需要等待 1 小时，则最多等待 180 秒，随后将此 provider 标记为本次尝试暂不可用。

- 计时从开始等待许可时算起，不含服务端已经要求的 Retry-After 延迟。
- 到期时触发非可重试 provider 失败 reason=rate_limit_wait_timeout；不进行实际 HTTP 发送。
- 这不是费用配额管理：没有扣减额度或修改 provider 的远端计划，只是避免本地长时间排队。
- 对 Web Fetch，沿用 FetchScheduler 的 next-provider/fallback 行为；候选选择可以用非预约式 ready-in 估计优先跳过处于长等待的 Provider，避免卡住其它已就绪候选。
- 对 Web Search/Academic Search，局部 Provider 失败不阻断其它 Provider；全部不可用时继续返回现有 ALL_PROVIDERS_FAILED。
- OA Resolver 超时仍按 enrichment.py 的原非阻断处理保留原论文，不删除已有学术搜索结果。
- 如用户明确期望排队更久，可调大已有 request_timeout_seconds；本次不引入新的特殊配置项。

**事件区别**：
- rate_limit_waiting：provider、stage、retry_attempt、waited_ms 或 estimated_wait_ms，不输出用户正文。
- rate_limit_acquired：provider、stage、attempt、waited_ms。
- rate_limit_wait_timeout：provider、stage、attempt、elapsed_ms、reason=rate_limit_wait_timeout；该事件不冒充已发送的 HTTP attempt。
- HTTP attempt 已开始的时间戳应尽可能靠近真正调用 client.request 的时刻；将限流等待耗时与网络耗时分开记录，避免把排队看作提供商响应慢。

### 3.3 Fetch 调度、公平性和取消

- FetchScheduler 只读查询每个 Gate 的 next_available_in_seconds，不在候选选择阶段提前消耗请求名额。
- 竞争导致 ready-in 估计过期很正常，最终以 Executor 原子预约为准。
- 如果全部候选在等 concurrency 或 rate permit，等待最早状态变化/最短计时器并重新比较，避免频繁空转和长时间只盯一家。
- 一次 Fetch 请求已经选中并且失败的 Provider 只在本次候选列表中移除；下次新用户请求仍可尝试。
- asyncio 取消必须取消 wait tasks、释放锁和 CapacityLease，不转化成成功或假网络失败，不留后台等待者。
- 单调时钟变化或浮点精度造成接近边界的零/负等待时重新检查，不盲目 busy-spin；非法非有限配置在启动时已被拒绝。

## 4. Input Truncation and Authentication Failure Contract

### 4.1 字节预算与输入保真

- 新默认值：LLM JSON Request Body 131072 bytes（128 KiB）；Agent CLI Prompt 98304 bytes（96 KiB）。
- 大小以实际 UTF-8 编码与最终请求序列化计算，而非 Python 字符数；避免中文字/emoji 被切断。
- Head/Tail 比例是裁剪正文的 80%/20%，标记字节从正文预算中扣除。
- System、Prompt 模板和 Focus 指示永不删减；当其自身超过配置预算时，直接报告 input_budget_unfit，不把这个错误伪装成上游 HTTP 错误。
- LLM 包括 model、messages、extra_body 在内的实际编码 JSON 必须再次验证；Agent 包括消息角色标签的完整 -p 参数必须再次验证。
- 正文确实被裁剪属于**成功处理的正常事件**，只标记 request_input_truncated=true，不触发 retry、不计 provider error。
- 发送给上游的最终正文就是日志中被二次预览的正文；已丢弃的中间部分只保留原始字节长度，不记录全文。
- 用户选择非常小的 cap 时，截断标记也可能无法容纳，按确定性预发送失败处理，不允许静默发大包。

### 4.2 模型窗口与 Agent OS 限制

- byte cap 不是 token cap；API 报 context window too large 时仍是原有状态/协议失败，不自动重复递减阈值重发，也不破坏全局 retry 次数。
- Agent 使用 agy -p 单字符串参数；普通 4 KiB Linux 页面大小下单参数常见上限约 128 KiB，默认 96 KiB 预留余量。实际 OS/环境与 CLI 内部限制需在测试中确认。
- 即使已裁剪至 96 KiB，其他命令行/环境占用也可能触发 E2BIG；保留已有 argument_limit 分类及进程清理，不能假装 96 KiB 绝对杜绝所有失败。
- 若用户自设 Agent cap 超过当前系统允许的单参数长度，应在运行前根据系统上限做可解释的安全检查（不能静默超限），必要时终止本次调用并给安全配置提示。

### 4.3 OpenAlex Key

- Registry 改为 required，只在启用 OpenAlex 时解析环境变量。无 Key 的启用配置在启动阶段失败，不触发 HTTP、不隐藏成空搜索结果。
- 用 Authorization Bearer 传 Key；Header 从不写日志，URL 不添加 Key query。可选 contact email 仍走原来的参数，但请求 URL 写日志时一律去 query。
- 上游 401/403 为认证/权限失败，不回退匿名、不自动更换来源，也不新增专用重试；429 仍依现有 HTTP Retry-After。
- 若配置里的 Key 失效，错误归属于 OpenAlex 的 provider_failed；其他 Academic Provider 的结果保留。

## 5. Debug Observability and Redaction

### 5.1 一次尝试、一条 Body 预览

- HTTP 成功/HTTP 错误/业务错误/200 JSON decode 错误：每一次真实 request 都有一个 upstream_body_preview，关联同一 request_id/provider/stage/attempt。
- Retry N+1 的 Request/Response 单独记录，不能只在所有重试都失败时才补一条终态汇总。
- Transport failure：请求 Body 有真实发送候选时预览，Response Body 为 null；不虚构 200/空 JSON。
- 本地尚未取得 RateGate 许可、Input 预发送失败：没有上游请求，因此写对应 rate_limit_wait_timeout 或 input_rejected 诊断，**不**伪造 HTTP Response Body 事件。
- Agent subprocess 一次调用只写一条 Body 预览；spawn/timeout/nonzero exit/cancellation 后有多少 stdout 就记录多少安全预览，无 stdout 为 null；既有 agent_process_diagnostic 保留。
- http_attempt_completed、provider_response_diagnostic、http_retrying、http_failed 与 provider_failed 仍有各自含义，Body 预览是补充事件，不复制最终失败事件。
- Request/Response Body 预览各最多 8192 UTF-8 bytes，并显示原始尺寸、最终发送尺寸、输入是否被裁剪和日志预览是否被裁剪。预览长文本包含显式省略标记。

### 5.2 防泄漏和失败降级

1. 绝不把 Authorization、Cookie、Set-Cookie、环境变量、完整 argv、URL 查询串、凭据值和完整用户输入隐含写入 DEBUG 事件。
2. 对 JSON Body 递归掩码常见敏感键；对文本与疑似凭据片段执行有限、确定性的安全归一化；然后按 UTF-8 字节限长，最后由现有 SecretRedactor 全行兜底。
3. 截断前先脱敏，避免长 Body 的前后保留部分碰巧暴露敏感字段；Formatter 对新行、引号、反斜线进行安全 JSON 逃逸，保证日志仍单行。
4. 上游原始响应为非 UTF-8 或二进制时，不尝试把任意控制码写进日志；只保留技术字节统计和可安全解码的文本片段。
5. 预览安全转换报错时不应中断 HTTP/Agent；无法安全脱敏时宁可省略 Body，只写固定失败标记。
6. debug=false 不对 Body 做额外全量序列化或敏感扫描；debug=true 才允许一定排错成本。
7. 即使只保存 8 KiB 预览，网页内容也可能包含私密信息；遵守既有仅 opt-in、最小权限、文件轮转，不把日志当永久审计归档。

### 5.3 墙上时间与耗时

- Formatter 为每条日志生成 time 字段，使用 record.created 的 UTC 毫秒时间，而不是依赖系统当前本地时区。
- time 用于定位“请求发生在什么时候”；elapsed_ms 继续由 monotonic 计算，不由两个 time 字符串相减。
- RateGate 的等候时间单独用 waited_ms/elapsed_ms 表示；http_attempt_started 尽量只表示真正出站。
- Formatter 的时间生成异常遵从现有日志安全处理，不因一个异常 LogRecord 中断业务执行。

## 6. Edge Cases and Fallbacks

1. 同时 10 个 Search/Fetch 请求争同一 provider：只在 RateGate 锁内原子预约，绝不出现每路各拿满 RPM 的情况。
2. 两个时间窗口同时触限（例如 30/60s 与 500/3600s）：只有全部满足才能发；不得只检查窗口中的最宽松一个。
3. 快接近滑动窗口边界：在精确到点后重新检查一次，避免 event loop 唤醒早于实际时间。
4. 同 provider 一次 HTTP 429 + Retry-After：本地和服务端限制同时有效，后续 attempt 重新计数。
5. 只有一家 Provider 且被长窗口卡住：等待时间超过 retry.request_timeout_seconds 后明确失败，不无限占用 daemon 连接。
6. Fetch 多 Provider：一家受限但另一家可用时优先后者；都失败时保持原 FetchOutcome 结果分类。
7. OA Resolver 受限或失败：不删除已有论文结果或 DOI，符合现有 enrichment 容错。
8. OpenAlex enabled 且 Key 丢失：只启动失败；OpenAlex disabled 则不要求 Key。
9. 带中文、组合字符、emoji、HTML 大正文：字节裁剪不生成非法 Unicode，不抹掉必须的 Focus。
10. LLM extra_body 或系统消息极大：若保留全部保护片段已经超过总 cap，就快速失败且不发请求。
11. Agent 超长 Prompt：预截断防止一般单参数限制；极端 E2BIG 仍被现有 subprocess handler 捕获和清理。
12. HTTP response 200 但 JSON 解析失败或 body 为空：保存安全预览和字节数，再报告既有 ProtocolFailure。
13. Response Body 含回显的未知 token、HTML/二进制或多行：不能记录 Header/URL query，脱敏失败则丢弃 preview，不能输出原始响应。
14. DEBUG 日志轮转发生在连续重试期间：每一条日志自包含 request_id、时间、Provider、Attempt；不依赖它们全部在同一个文件。
15. 取消发生在限流等待、重试 sleep、Agent spawn 或 debug 日志过程中：必须传播取消，不能留下 permit 任务或子进程。
16. Provider 使用其他进程共享的 Key、服务端实际比示例值更低：本地阈值不能保证远端无 429；继续尊重服务端失败和 Retry-After，不自行修改账户速率。

## 7. Acceptance Criteria (Design)

- [ ] 非法速率/间隔/字节预算在配置阶段立即失败，错误不含实际 SecretValue。
- [ ] OpenAlex 启用且无 Key 立即配置失败；禁用时可不设置；401/403 不匿名重试。
- [ ] 相同 Web Provider 的 Search/Fetch/重试共用多窗口计数，不突破任一窗口或间隔。
- [ ] 本地长窗口等待有限、可取消；单家阻塞不饿死其他候选；无实际发送不产生伪 HTTP Body 事件。
- [ ] 原有 Retry-After、max_attempts、transport/业务错误/200 解析失败分类不回归。
- [ ] 默认 LLM 128 KiB 和 Agent 96 KiB；超过时裁剪可变正文，system/Focus 仍完整；不支持裁剪时预发送失败。
- [ ] 每个已实际发出的 HTTP attempt（包括重试、错误状态、200 decode error）恰有一条双向 debug Body 预览；Agent 每次 subprocess 调用同理。
- [ ] Body 预览单边不超过 8 KiB；保留原/后字节统计、input_truncated 与 preview_truncated 区分；无响应时为 null。
- [ ] 未开启 debug 时不产生 Body 内容，也没有额外的昂贵序列化；日志处理失败不改变业务结果。
- [ ] 每条格式化日志带 UTC 毫秒 time，并维持单行、request_id、elapsed_ms、轮转与防泄漏。
- [ ] 实际 OS 限制或 LLM context window 即使与字节预算不一致，也得到明确现有失败分类，不承诺所有大请求都必定成功。

## 8. Code References

- src/agent_search_gateway/config.py：共享配置识别、环境变量解析与验证。
- src/agent_search_gateway/concurrency.py：CapacityGate、ProviderQuotaManager。
- src/agent_search_gateway/providers/http.py：HttpJsonExecutor、Retry-After、Decode 与日志尝试边界。
- src/agent_search_gateway/retry.py：before_attempt/on_retry 和重试预算。
- src/agent_search_gateway/scheduler/fetch.py：候选挑选、回退和并发租约。
- src/agent_search_gateway/academic/enrichment.py：OA Resolver 失败不删论文。
- src/agent_search_gateway/llm/stages.py、providers/llm/openai_chat.py：LLM Stage 和 Body。
- src/agent_search_gateway/providers/agent/antigravity_cli.py：CLI Prompt、E2BIG、进程清理。
- src/agent_search_gateway/observability.py：SecretRedactor、KeyValueFormatter、安全 Handler。

此文档待用户审阅。确认后再提交第三部分 Testing 设计；全部设计经确认后才实施补丁。
