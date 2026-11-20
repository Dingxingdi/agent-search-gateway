# Testing: Provider Rate Limits、时间戳、OpenAlex Key、LLM/Agent 截断与 Body Preview

- 日期：2026-11-18
- 状态：Brainstorming 测试方案已由用户确认；尚未修改业务代码
- 关联：[架构](../architectures/20261118-provider-rate-observability-openalex-input-truncation.md) / [错误处理](../error-handlings/20261118-provider-rate-observability-openalex-input-truncation.md)
- 实施基线：edcc627，v0.4.0；使用已有 pytest + pytest-asyncio + httpx.MockTransport + uv 工具链

## 1. Testing Goals

1. 全部 24 Web + 6 Academic + 1 OA Resolver 的官方公开免费层级速率调查已覆盖；明确值进入配置示例，未知值不猜测，费用/credits/月度配额不自动管理。
2. 新 rate_limits 多滑动窗口和 min_interval_seconds 对每一条真实上游 HTTP attempt 生效；同一 Web provider Search/Fetch 共享，多个 provider 相互隔离，原 max_concurrency 不失效。
3. 与原 RetryPolicy（含 429 Retry-After、超时、连接失败、供应商 HTTP 200 业务错误）协作正确；长窗口等待有限、无忙等、可取消、能回退到其他 Provider。
4. 每条日志有可验证的 UTC 毫秒 time；debug 的每次 HTTP attempt/Agent subprocess 都有正确、脱敏、受限长度的 Request 与 Response Body 预览，含 200 JSON decode 失败路径。
5. OpenAlex 启用时 Key 必需、Bearer Header 精确、Key 不能出现在 URL/异常/日志；禁用和可选 contact email 兼容。
6. LLM 最终 HTTP JSON Body 不超过默认 128 KiB，Agent -p 参数不超过默认 96 KiB；在自定义小 cap、中文/emoji、巨型网页、Focus 场景仍安全裁剪或清楚失败。
7. 原 Search/Paper/Fetch、Agent 生命周期、日志轮转、配置兼容性、CLI/daemon 行为全量回归。

## 2. Test Strategy & Placement

### 2.1 分层矩阵

| 层次 | 目标测试文件（新建或扩展） | 核心检查 | 网络 |
|---|---|---|---|
| 配置 | tests/unit/test_config_web_providers.py、test_config_academic_providers.py、test_config_llm.py、test_config_agent.py | 新共享键、非法值、默认值、OpenAlex required Key、旧配置兼容 | 无 |
| RateGate 单测 | tests/runtime/test_rate_gate.py（新增） | 滑动窗口、多个窗口、最短间隔、原子预约、clock 注入、取消和等待上限 | 无 |
| 配额/Runtime | tests/runtime/test_quota_manager.py、test_runtime_assembly.py、test_academic_runtime_assembly.py、test_new_web_runtime_assembly.py | Search/Fetch Gate 共享、不同 Provider 隔离、Academic/OA 单独 Gate、CapacityGate 保留 | 无 |
| HTTP retry | tests/providers/test_http_executor.py | 每个 retry 重新限流、Retry-After、等待超时和无请求时不记 Body | MockTransport |
| Fetch 调度 | tests/scheduler/test_fetch_capacity.py、test_fetch_outcomes.py | Ready provider 优先、RateGate + CapacityGate、all-blocked、取消、fallback | Mock/fake |
| OpenAlex Adapter | tests/providers/academic/test_openalex.py、test_registry_defaults.py | Bearer、环境解析、optional contact_email、401/403/429 | MockTransport |
| LLM Input | tests/providers/llm/test_openai_chat.py、tests/unit/test_llm_stages.py | 最终 JSON 128 KiB、自定义值、system/Focus 保留、头尾比例、不可压缩超限 | MockTransport |
| Agent Input | tests/providers/agent/test_antigravity_cli.py、test_antigravity_lifecycle.py | argv -p 96 KiB、子进程 stdout、E2BIG、清理、超限不 spawn | 假 CLI/monkeypatch |
| 观测日志 | tests/unit/test_observability_logging.py、tests/observability/test_provider_event_logging.py、test_agent_event_logging.py、test_redaction.py | UTC 时间、attempt 每次一条双向预览、8 KiB、脱敏、日志失败降级 | 无/Mock |
| 端到端 | tests/acceptance/test_gateway_workflows.py、test_agent_search_workflows.py、test_debug_and_doctor.py | HTTP/Agent、Request ID、URL admission、Fetch fallback、错误码、debug | 本地 daemon/Mock |
| 文档合同 | tests/docs/test_documented_config.py、README/config.example 检查 | 样例语法可解析、示例速率、OA 凭据要求、31/31 调查核验 | 无 |
| 真实冒烟 | tests/integration/ 下的现有 live 测试和受控临时配置 | 已配置 provider 的最小真实 Search/Paper/Fetch/LLM/Agent | 仅显式启用 |

沿用当前 tests/support 下的 fakes、controlled clock、HTTP mock、logging capture、Agent CLI 支持文件；不引入第二套模拟框架。所有新例子应是确定性的，无需访问外部付费 API。

### 2.2 测试数据与注入规则

- 时钟：RateGate 用注入 monotonic 和可控 sleep/advance 模拟 1 秒、60 秒、3600 秒、86400 秒，不进行真实长时间等待。
- HTTP：MockTransport 捕获实际 Request 的 headers/body 和调用次数，不直接只测试 Adapter 方法参数，确保最终传输边界确实被限速。
- Agent：复用 tests/support/agent_cli.py 的模拟终端 envelope；捕获 argv 的 -p 参数，在测试中不打印它。
- Prompt：使用纯合成的中文、ASCII、emoji、组合字符、超长内容及虚构敏感哨兵，不从真实网站复制隐私内容。
- 日志：使用内存 StringIO / tmp_path，结构化解析单行 KV；断言字节长度，而不是仅断言 Python 字符数量。
- 环境变量：使用 pytest monkeypatch 的假值与显式清理；不读取、打印或覆盖用户的真实密钥。
- CI 中的 live integration 仍由现有 WEB_SEARCH_RUN_INTEGRATION=1 显式开启，默认跳过。

## 3. Rate Limiting Cases

### 3.1 RateGate 纯函数/状态机

| Case | 输入/动作 | 期待 |
|---|---|---|
| 单窗口 QPS | 10/1s，t=0 送出 10 次，第 11 次并发入队 | 10 次可立即预约；第 11 次直到 t≥1 才获得许可 |
| 单窗口 RPM | 100/60s；恰好耗尽窗口 | 时间没过不能第 101 次发；60 秒边界正确释放最旧记录 |
| 多窗口 | 30/60s + 500/3600s；分别将两个窗口耗尽 | 任一窗口受限都不能发送；必须全部符合后才能发 |
| 最短间隔 | min_interval_seconds=3 | 请求时间至少相隔 3 秒，即使没有 rate_limits 也成立 |
| 并发争抢 | 20+ 个协程同时发往同一 Gate，限制 1/1s | 锁内原子预约，无双花/超额，最终全部按序完成或被取消 |
| 不同 Provider | A=1/60s、B=10/1s | A 限额耗尽不影响 B |
| Search + Fetch | 同一个 provider Search 已用满 10/60s，再 Fetch | Fetch 必须等待同一 Gate，不可绕开 |
| Retry 计数 | 第一次 429、第二次 200，限制 1/1s | 第二次仍需新许可；总 HTTP request 数为 2 |
| 取消等待 | 在 permit 仍在等待时取消 | 传播 CancelledError；没有预约；下一个请求可用 |
| 预约后取消 | 已许可后 HTTP 阶段取消 | 本次许可计入窗口，不返还引起短时突发 |
| 空 policy | rate_limits 缺省/min_interval 缺省 | 不额外等待，旧 Provider 行为不变 |
| 跨窗口边界 | 刚好 t=1/60/3600 与小于该点的瞬间 | 不提前放行，不受浮点误差影响出现 busy-spin |
| 只读可用时间 | 调用 next_available_in_seconds 多次 | 不预占许可，返回保守估计，Executor 的 permit 仍最终裁决 |

测试保证：没有任务实际 sleep(60)、sleep(3600)；断言使用假时钟的精确发送顺序与次数，不依赖线程调度的偶然顺序。

### 3.2 与 HTTP、Retry-After 及 Timeout 集成

1. 使用 httpx.MockTransport 模拟 429（含 Retry-After=2）→200，检查时间线：第一次 HTTP 发送→服务端等待→新的本地 rate permit→第二次 HTTP 发送，不提前发送、不增加 max_attempts。
2. 模拟 429 + 没有/非法 Retry-After、408、5xx、TimeoutException、TransportError，确保现有共享重试分类和次数不变；每一次真实 attempt 都计入窗口。
3. 模拟 HTTP 200 + provider-inspector 报业务临时错误：若现有 retryable=true，第二次也必须重新过 RateGate；未知业务错误不增加额外 retry。
4. 将 rate policy 设置为下一个许可需要 3600 秒，现有 retry.request_timeout_seconds 设为 0.5（假时钟）：等待届满抛 rate_limit_wait_timeout，不发 HTTP、不产生伪造 HTTP Body；Orchestrator 把 provider 标成失败。
5. 测试 Retry-After 与 RateGate 均在等待时，真正第二次发送时间不少于二者各自要求，不用两个累加的人工固定间隔取代时序判断。
6. 检查 http_attempt_started 的 time 靠近真实 client.request、rate_limit_waiting 的 waited_ms 只统计排队；业务 elapsed_ms 不误用 UTC time 直接相减。
7. 取消发生于 RateGate await、retry sleep、HTTP Client await 时，均不额外发送或留下悬空任务。

### 3.3 Fetch Scheduler / Academic / OA

- 创建两个仅 Mock 的 Fetch provider A（RateGate 当前至少等待很久）、B（RateGate 当前可用）；二者 CapacityGate 均未满：验证先选择 B，结果有效时不访问 A。
- A 满 concurrency、B 满 rate limit：调度器等待状态改变，不立即抛错/忙循环；取消不会泄漏 CapacityLease。
- 多个待选 provider 的就绪顺序发生变化时，最后由 Executor 预约作准，确保没有 race-condition 绕过限制。
- Academic Provider 在 asyncio.gather 中并发请求同一个 provider 的多个任务时仍保持其 rate policy，不把 Paper Search 与 OA Resolver 混在同一个 Gate。
- OA Resolver 超过限流等待预算：现有 enrich_paper_records 捕获失败，原论文 URL/DOI/摘要未丢失；Resolver 失败日志有安全原因。
- Fetch 的 URL admission、cheap_check、LLM judge 失败和 provider fallback 仍符合已有 contract。

## 4. Configuration & OpenAlex Cases

### 4.1 配置参数化

对 Web/Academic/OA 三个组分别覆盖：

- 正常：省略 rate_limits、空数组、单窗口、多个窗口、有无 min_interval_seconds；保留现有 max_concurrency。
- 非法结构：非列表、列表里是标量、缺 requests、缺 period_seconds、未知键、对象嵌套错、数组嵌套错。
- 非法数值：requests=0/-1/1.5/true，period_seconds=0/-1/true/NaN/Infinity/字符串，min_interval_seconds 的同类错误。
- 防透传：rate_limits/min_interval_seconds 不会被当成 Provider 专属 options 交给 factory，避免旧适配器因未知 kwargs 崩溃。
- 无效配置即 ConfigFailure；不允许启动半成品 Runtime。禁用且未注册的 Provider 按现有行为处理。
- input_limits 的默认 131072/98304、分别自定义、缺字段、零/负数/bool/非整数及未知键。
- 示例 TOML 可经 tomllib 解析，所有实际启用 Provider 的凭据来自独立假 environ；旧配置不配置新字段仍可启动（OpenAlex 的 required Key 除外）。

### 4.2 OpenAlex

| Case | 行为 | 断言 |
|---|---|---|
| enabled + no api_key_env | 解析配置 | ConfigFailure；不存在任何 HTTP 调用 |
| enabled + 指定 env 但值缺失/空 | 解析配置 | ConfigFailure；只包含环境变量名、绝不包含真实值 |
| disabled + Key 缺失 | 解析配置 | 正常，没有匿名 Provider 实例 |
| enabled + 假 Key | Adapter GET works | Header 为 Authorization Bearer，URL 不含 Key；参数 search/per_page 保持 |
| enabled + 可选 contact email | Adapter GET works | mailto 仍被发送，公开日志中的 URL 不含查询串 |
| 上游 401/403 | HTTP Mock | 只请求一次、不回退匿名、不吞掉错误 |
| 上游 429 | HTTP Mock | 共享 Retry-After + RateGate 生效 |
| 上游 200 合法/非法 JSON | Mock fixture | 现有映射正确；非法数据仍走原 ProtocolFailure |
| 日志脱敏 | 捕获所有 log 行 | 不含假 Key、Header、query 的秘密哨兵 |

## 5. LLM and Agent Input Truncation Cases

### 5.1 UTF-8 纯函数

1. 未超预算文本原样返回，truncated=false，字节数准确。
2. 超预算英文文本按 80/20 保留首尾，标记只出现一次；最终 UTF-8 字节数 ≤ cap。
3. 使用大量汉字、emoji、组合字符、换行、非 BMP 字符和 JSON 反斜杠，验证不产生非法 Unicode/截断半个 code point。
4. 预算刚好等于原字符串或只比它少 1 byte：边界计算正确，不超限也不无限循环。
5. 预算比 marker 还小、以及受保护片段超过预算：明确 input_budget_unfit，不会隐式删除系统/Focus。
6. 特殊文本包含看起来与标记相同的内容时，不把原始正文错判为已截断；返回的 metadata 来自计算结果。
7. 覆盖合成长度 2 MiB 的大网页正文，验证内存与耗时仍有限，不执行真网络请求。

### 5.2 LLM HTTP JSON 实际编码体

- 将所有可变文本构造成 messages + model + extra_body，经 LLM Client 组装，最终捕获 MockTransport 的 request.content，断言真实字节长度 ≤131072（默认）及用户定义的小 cap。
- 包括 content_clean、judge、safety、focus_summary、llm_search、llm_paper_search 的不同 Stage；在 focus_summary 中无论截断如何都完整保留 Focus 值与系统 Prompt。
- 全部正文未超限：字节完全一致，不添加“截断”标记；JSON messages 内容和角色与旧实现兼容。
- 长网页 / 大片中文 + JSON 特殊字符：最终 JSON 可正常解析，头尾正文可见，原始中段不会作为隐藏字段被发送。
- 若 model、extra_body、system/Focus 已超过预算：Client 在进入 HTTP Executor 前失败，理由 input_budget_unfit；没有网络调用和虚构 Response Body。
- 对实际发出的截断请求，DEBUG 上游 Body preview 只反映最终发送内容，request_input_truncated=true，原/后字节数正确。
- 对小上下文模型的 Mock HTTP 400：仍不被 byte cap 错误地视为成功，也不因模型拒绝而无限缩小重发。

### 5.3 Agent argv 与生命周期

- 捕获 run_antigravity 调用 create_subprocess_exec 的 argv，取单个 -p 参数长度；默认 ≤98304 bytes，自定义小 cap 同样生效。
- 测试 120 KiB 的中文/英文混合 Prompt 被安全截断，角色标签、系统指令和必要正文头尾保留；没有超出预算的编码字节。
- 使用已存在的假 agy 进程验证正常 SUCCESS envelope、非零退出、timeout、spawn error 和 cancellation 后的清理。
- 受保护内容本身超过 cap 时不创建子进程，既有并发租约不泄漏；输出确定性 input_budget_unfit。
- monkeypatch OSError(errno.E2BIG) 验证仍报告现有 argument_limit、没有未经控制的第二次 argv retry、并正确清理。
- 对 OS/平台参数上限的检查使用注入式边界或仅在兼容 POSIX 环境运行，不能让 macOS CI 受固定 Linux PAGE_SIZE 假设影响。
- Agent debug Body preview 为实际 -p Prompt 和已收集 stdout 的脱敏预览；没有 stdout 时为 null；不暴露进程 env/完整 argv。

## 6. Timestamp, Body Preview & Security Tests

### 6.1 Format 与时间语义

- 构造固定 created 时间（含毫秒、UTC 跨日边界）和固定 request_id，检查 KeyValueFormatter 输出 ISO 8601 带 Z 的 time，单行，字段排序和 request_id 未变化。
- 使用可控 monotonic 检查 elapsed_ms、waited_ms，保证与真实 time 值不同源；不能以本地时区转换、浮点舍入影响顺序。
- 普通日志记录（非 gateway_event）也有 time，包含安全 message，仍能经过 Redactor。
- log 文件轮转到 5 MiB 边界时，已出现事件的 time/request_id 保持完整，不产生多物理行。

### 6.2 每次尝试唯一预览矩阵

| HTTP / Agent 模拟场景 | Request preview | Response preview | 事件数 |
|---|---|---|---|
| POST JSON 200 success | 实际发送的 JSON 预览 | 合法 JSON 预览 | 1/attempt |
| GET 200（OpenAlex/arXiv） | null | 原始 JSON/text 预览 | 1/attempt |
| POST 429→200（两次请求） | 两次各独立记录 | 两次各有对应状态 Body | 2 |
| HTTP 500 或 HTTP 401 非重试失败 | 有实际请求体 | 错误响应体（脱敏） | 1 |
| TCP 连接拒绝 / timeout | 实际尝试发送体 | null | 1/attempt |
| HTTP 200 + invalid JSON | 有实际请求体 | 原始非法 JSON/text 的安全预览 | 1 |
| HTTP 200 + provider inspector 业务错误 | 有实际请求体 | 错误 envelope 预览 | 1/attempt |
| 本地 RateGate 等待超时（未发送） | 无上游尝试 | 无上游尝试 | 0 HTTP preview |
| LLM preflight 无可裁剪空间（未发送） | 无上游尝试 | 无上游尝试 | 0 HTTP preview |
| Agent SUCCESS | -p Prompt 预览 | stdout terminal envelope 预览 | 1 |
| Agent spawn 失败、nonzero exit、timeout | 可捕获已准备 Prompt | 有多少安全 stdout 记录多少，可能 null | 1 |
| debug=false，各场景 | 不生成/不存储 | 不生成/不存储 | 0 |

验收区分三种概念：请求未曾发送、实际出站请求经输入裁剪、仅日志预览二次裁剪。不能将日志预览被裁剪误认为上游请求被裁剪。

### 6.3 Byte cap、Secret 和性能哨兵

- 对两侧 8191/8192/8193 bytes 的预览，精确测量 UTF-8 byte 长度，验证 8KiB 预算包含省略标记，不能因 emoji、JSON escape 超标。
- 凭据哨兵放在 JSON 深层字段、常见 Header 命名、原始正文、响应错误 message、相邻引号、首尾截断位置；捕获最终物理日志全文，所有可识别凭据都被遮蔽。对于不可安全脱敏的非文本，预览应丢弃而不是盲打。
- Request/Response 里放 newlines、tabs、CRLF、quotes、backslashes、长 HTML 和无效 UTF-8，检查每个日志事件仍是一行，且不改变业务响应或错误分类。
- 对嵌入在 URL query 中的假凭据（含转义），检查 endpoint 字段不含 query、Fragment、userinfo，也不意外落到 debug Body Header。
- 检查 request_body_original_bytes ≥ request_body_sent_bytes、response_body_bytes 与 MockTransport 原始长度匹配、request_input_truncated 与 request_preview_truncated 独立。
- 模拟 Preview Helper 抛异常、Formatter 故障、文件 Handler 无法写、磁盘满错误：业务请求照常完成或报告它原本的业务失败；没有未经脱敏的异常栈/Body 被输出。
- 用计数 Spy 验证 debug=false 时未调用 Body 预览序列化/脱敏函数，避免默认请求路径出现明显额外成本。
- 连续 100 次小型 mock attempt 时单请求一事件，无重复、无跨 attempt 误关联；按 request_id、stage、attempt 可区分重试。

## 7. Acceptance & Regression Coverage

### 7.1 端到端本地验收

1. Mock Web Search → URLStore 录入 → FetchScheduler 从受限 Provider 回退到另一家 → LLM judge/clean/focus：结果与原流程一致，只有满足 RateGate 许可才发真实 Mock HTTP。
2. Mock Paper Search 运行多个 Academic provider，其中 OpenAlex 使用假 Bearer Key；另一路 provider 失败时保留已成功论文，OA Resolver 失败不删除 DOI/URL。
3. Mock LLM Search 和 Agent Search 输入 >各自默认 cap 的正文，实际 JSON/argv 均 ≤预算，返回格式/模型过滤/错误码与原实现兼容。
4. 启动本地 daemon --debug → 调用一次测试请求 → 检查全部事件 time/request_id、预览、脱敏和轮转；普通模式没有 Body。
5. 确认 Agent 关停、取消、进程组清理与长等待队列不会留下孤儿进程和超时后台任务。

### 7.2 官方免费层覆盖与 config.example.toml

- 对照 build_default_registry、build_default_academic_registry、build_default_oa_resolver_registry 的当前注册集合，确认架构调查包含全部 31 个名称；不能因示例配置只列部分 Web Provider 就遗漏其余注册项。
- 对官方明确有数值的案例检查样例 rate_limits 与单位（秒）相符；不同 Search/Fetch 采用提供商级保守规则。
- 对仅有并发阈值的免费层保留 max_concurrency；未找到固定请求速率的 Provider 不捏造 RPM。
- OpenAlex 示例明确需要 Key 环境变量而非匿名；保留 contact_email 可选说明。
- 不将月度/credits/收费预算放入 RateGate 样例；官方链接、调查日期与认证模式在文档中可见。
- 在 CI 使用假 env 加载 config.example.toml，确认支持所有实际配置字段并通过类型校验。

### 7.3 现有 CI 门槛

按 .github/workflows/ci.yml 的仓库检查顺序运行：

1. uv sync --locked --group dev
2. uv run ruff format --check src tests scripts
3. uv run ruff check .
4. uv run mypy src tests scripts
5. WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -v
6. uv run python scripts/build_docs.py（按 docs group 环境执行）
7. uv build，构建轮子后检查 CLI help。
8. 需要时按 CI matrix 验证 Python 3.11、3.12、3.13、3.14 和 macOS Python 3.12；确保没有把 4KiB Linux PAGE_SIZE 作为所有平台的硬编码。

有条件时可加入 uv audit 和 secret-scan 既有 CI，但不得因测实时调用而把凭据或网页原文提交到 Git。全部离线测试是硬门槛；受提供商故障、额度、外部网络和 CLI 安装状态影响的 live test 明确单独记录。

## 8. Opt-in Live Smoke & Reporting

### 8.1 Preconditions and Boundaries

- 用户已说明各 Provider 所需环境变量已经设置；直接复用已配置 Key，不将值打印到命令、测试日志、issue 或设计文档。
- 本阶段只有测试计划，不立即启动真实请求；实施阶段 CI 默认 WEB_SEARCH_RUN_INTEGRATION=0，显式启用 live 才调用真实服务。
- 优先使用一条简单公开查询，不主动打满免费 RPM、制造 429 或消耗每日/每月赠送 credits。速率准确性通过可控 Mock/Fake 验证。
- 有 Key 但账户没有权限/余额或服务端返回限额时记录失败/跳过原因，不换用匿名接口冒充通过。
- 对可能涉及成本或外部部署的 Provider，可通过 --provider 选单家或 subset，报告真实执行范围；不是必须在一次 CI 全部测完。
- Web/Paper Search CLI 支持 --provider NAME；LLM/Agent 搜索也有 --provider；url-fetch 没有该选项，必须通过临时隔离配置或直接调用 Adapter 来限定 Fetch Provider。
- 使用 URLFetch 前需先通过 Search 收录 URLStore，不能绕过既有安全准入规则。
- Agent 的 96 KiB 实测仅在 agy 已安装、已认证、模型可用时执行；记录 OS/进程和实际成功阈值，不假定它一定能接收任意接近 128KiB 的 Prompt。
- OpenAlex 的真实冒烟使用已配置 Key 验证简单 works 查询，只报告成功/状态类别；如 401/403 则报告账号配置问题，不打印响应里可能回显的敏感信息。

### 8.2 建议最小实测矩阵

| 类别 | 实测目标 | 调用量建议 | 验收 |
|---|---|---|---|
| Web 有固定 RPM | Tavily/Firecrawl/Exa/Brave 等代表提供商 | 每家 1 次 Search，必要时 1 次 Fetch | 能获取合法结果或明确上游失败，不频繁 429 |
| Academic Key | OpenAlex | 1 次 Paper Search | Bearer 成功，格式有效，Key 不落日志 |
| Academic 慢速 | arXiv、Semantic Scholar | 每家 1 次 Paper Search，间隔满足示例 | 不人为触发 429，timeout/重试归因明确 |
| OA Resolver | Unpaywall（若已启用） | 1 个 DOI | 可解析或按 optional enrichment 失败安全回退 |
| LLM 大输入 | 用户配置的一个 LLM | 一次略超过 128 KiB 的合成正文（实际请求经裁剪） | 无 body-too-large/JSON encoding 失败，保留头尾 |
| Agent 大输入 | 用户配置的 agy | 一次略超过 96 KiB 的合成正文（实际 prompt 经裁剪） | 无常见 E2BIG，返回或明确 CLI 错误 |
| Debug | 选一个 HTTP Provider + Agent | 各 1 次 | UTC time、Request/Response Preview、尺寸和脱敏全在 |

这只是低成本冒烟样本，不代替 31 家的**官方速率调查覆盖**；其他 Provider 可通过 --provider 按需补测，必须在结果里写明 SKIP/未测。

### 8.3 结果记录

每次独立报以下字段：Provider、operation、Auth mode、预期/实际请求次数、rate wait ms、HTTP/业务状态类别、input_original_bytes、input_sent_bytes、input_truncated、debug_preview_present、time_format、PASS/FAIL/SKIP、说明；所有值均不得包含实际密钥、完整目标 URL query 或私有网页正文。

## 9. Execution Order & Exit Criteria

### Suggested order

1. 先修测试规格与现有类型/协议兼容性：Config/Registry 与 OpenAlex required Key 的测试。
2. 实施 RateGate 的假时钟测试，锁定多窗口、间隔、等待/取消、重试语义。
3. 注入 Runtime/HttpJsonExecutor、覆盖 Search/Fetch 共享与 FetchScheduler fallback。
4. 实施 UTF-8 裁剪纯函数、LLM JSON/Agent argv 的真实编码预算和不可压缩错误路径。
5. 实施 Formatter timestamp 和 debug Body preview 一次尝试一条事件；强制安全哨兵测试。
6. 跑全套离线测试/静态检查/文档验证，并消除回归。
7. 明确受控地做真实冒烟，不进行压测/配额耗尽测试；记录全部 PASS/FAIL/SKIP 与限制。

### Exit criteria

- [ ] rate_limits 任意窗口及 min_interval 在同一 Provider 的 Search/Fetch/重试中严格生效；不同 Provider 不互相锁死。
- [ ] 配置错误、OpenAlex 缺 Key、Body 不可压缩、超时、429/Retry-After、取消均有确定性结果和安全诊断。
- [ ] RateGate 超长等待在现有 retry.request_timeout_seconds 内结束并允许 provider fallback；无实际发送时不记录虚假的 HTTP Body。
- [ ] 默认 LLM 128 KiB / Agent 96 KiB，UTF-8 与 Focus 保真，不超最终序列化字节预算；Agent E2BIG 旧分类仍在。
- [ ] DEBUG 每 HTTP attempt/Agent 调用有且仅有一条 Request/Response 安全 Body 预览，记录字节数、截断标志，200 解码失败和无响应均覆盖；默认非 DEBUG 没有正文。
- [ ] 所有日志行为保持 UTC 毫秒 time、request_id、elapsed_ms、单物理行、脱敏与轮转，且日志故障不改变业务结果。
- [ ] 对 31/31 已注册非 LLM/Agent 服务完成来源对照，只有有根据的数值进入配置示例，未包含费用配额。
- [ ] CI 离线测试/格式/类型通过；真实 smoke 按测试范围如实记录通过、失败、未测，不能将 SKIP 写成 PASS。
- [ ] 所有三个设计文档获用户审阅确认后才写实施计划与业务代码。

第三阶段 Testing 设计及三份设计规范整体已获用户确认；后续进入实施计划阶段。
