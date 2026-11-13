# Gateway Timeout, Provider Diagnostics, and Google Goto Patch Implementation Plan

**Goal:** 移除 Gateway 对 Agent 超时配置及外层等待的固定上限，统一所有提供商成功、失败与部分拒绝的安全诊断，并在关键词结果入口统一解包已确认的 Google goto 链接。

**Architecture:** [docs/designs/architectures/20261113-gateway-patch-timeout-diagnostics-google-goto.md](../architectures/20261113-gateway-patch-timeout-diagnostics-google-goto.md)

**Error handling:** [docs/designs/error-handlings/20261113-gateway-patch-timeout-diagnostics-google-goto.md](../error-handlings/20261113-gateway-patch-timeout-diagnostics-google-goto.md)

**Testing:** [docs/designs/testings/20261113-gateway-patch-timeout-diagnostics-google-goto.md](../testings/20261113-gateway-patch-timeout-diagnostics-google-goto.md)

---

## 0. 执行上下文与边界

- 日期：2026-11-13。
- 工作树：`/home/agent/agent-search-gateway/.worktrees/gateway-patch-timeout-logs-google-goto`。
- 分支：`gateway-patch-timeout-logs-google-goto`；计划基线：`f431d10`（三份 spec），生产代码基线：`d0ff1bc / v0.2.1`。
- 所有下述路径均相对于此工作树；沿用该工作树。按本计划顺序实施，不创建第二个工作树。
- 本文件是实施计划；生成计划时不添加生产实现或测试。计划自审完成后仅提交本文件。
- 已遵循 `writing-plans`，并阅读其要求的 `test-driven-development` 及 `references/testing-anti-patterns.md`。执行者按每个行为逐次 RED → 验证 RED → GREEN → 验证 GREEN → REFACTOR，不把所有待测行为一次写完。
- 新建模块/函数的第一个 RED 应通过已有业务入口观察缺失行为；接口存在断言不能替代行为验证。不要把 ImportError、错误 fixture、命令选错工作目录当作有效 RED。
- 回归用例若已通过，记录为回归保护，不谎称 RED，也不人为破坏已有实现制造失败。
- 延续当前 CLI、NDJSON、JSONL、异常类别、HTTP 重试次数/退避、Fetch 顺序和部分成功语义。新诊断只进入现有 DEBUG 日志。
- “无限”指允许任意可表示的有限正数，不是让进程永久运行。默认仍为 300 秒；保留每次调用的 agy deadline、Python +5 秒和进程组清理。
- agy 的时长参数解析属于外部依赖；移除 Gateway 上限不保证它接受每一种极端浮点时长。外部拒绝沿原 Agent 边界报错，不在 Gateway 新增替代上限。
- URL 规则仅针对 `google.com` / `www.google.com` 的精确 `/goto` 路径。未知格式原样交给现有流程；不增加网络重定向、其他搜索引擎规则或全局 URL 重写。
- [issue #74](https://github.com/Dingxingdi/agent-search-gateway/issues/74) 提供 ScraperAPI / Scrape.do 的 goto 证据；当前 Bright Data 同样直接传递 `link`，共享入口可以覆盖相同形状，但不宣称已获得 Bright Data 的真实 goto 样本。
- 不修改 provider registry、LLMStages 职责划分、RetryPolicy、学术聚合算法、发布版本号或旧历史设计记录。

### 0.1 环境与验证命令

实施前确认命令的 cwd 为上述工作树，先记录现有差异，再准备锁定环境。需要联网安装时仅为该命令恢复代理：

~~~bash
https_proxy="$CODEXPRO_TOOL_HTTPS_PROXY" http_proxy="$CODEXPRO_TOOL_HTTP_PROXY" uv sync --locked --all-groups
~~~

本次检查 HTTP/HTTPS 代理变量存在，ALL_PROXY 备选变量未设置；不要输出代理值。后续环境若只有 ALL_PROXY，按用户规则使用相应备选值；所需代理都缺失时报告，而不是写死代理或改 ngrok。

下述测试统一使用 `uv run --offline`；默认不触发真实提供商。每个 Task 的 GREEN 和 REFACTOR 都执行所列完整相关组件测试。依 TDD 要求，完成一个可独立实施的 Task 后再运行：

~~~bash
uv run --offline pytest -q
~~~

已完整验证且没有重构/后续修改时不重复同一轮全量验证。最终门禁使用 CONTRIBUTING.md 的命令，见末节。当前任务只是写计划，不在本轮把这些命令记作“已测试通过”。

## 1. 已核实的现状

| 位置 | 现有行为 | 计划处理 |
| --- | --- | --- |
| `config.py:resolve_agent_config` | 拒绝大于 540 秒；`_positive_float` 已拒绝 bool、非数值、非有限数、非正数 | 只删除上限分支，复用数值验证 |
| `orchestrators/search.py:agent_search` | `asyncio.timeout(1140)` 包住队列、调用、OA 和写出 | 移除整个固定工作流 deadline 及专属错误转换 |
| `protocol.py:send_request` | Agent 默认 1200 秒，其他请求默认 600 秒 | Agent 默认传 `None` 给 wait_for；显式数值优先 |
| `providers/agent/antigravity_cli.py` | quota 后开始进程 deadline；stdout 有界保存、stderr 有界丢弃 | 保留生命周期，增加字节计数与安全终态诊断 |
| `providers/http.py` | 已有 ResponseInspector、ProviderResponseFailure、重试事件 | 复用控制流，补齐每个响应的诊断及失败携带字段 |
| `providers/web/*.py` | 多处 `except ExecutionFailure: continue` 不计数 | 每次调用局部累计原因，统一输出摘要 |
| `providers/academic/*.py` | 已有部分 paper_candidate_rejected | 保留事件并补全聚合摘要、响应分类 |
| `providers/llm/openai_chat.py` | 协议重试记为 provider_failed；内部成功也记 provider_completed | 改为尝试/协议事件，终态由流水线所有者记录 |
| `observability.py` | 最终渲染才脱敏；任意 dict 渲染为 `<dict>`；target URL 保留 query | 专用安全文本值在脱敏后截断；受控计数字典可读；日志 URL 去 query |
| `SearchOrchestrator._stage_keyword_hit` | 直接 normalize_url(hit.url) | 在这里先解包，再使用原规范化、去重、准入 |
| README | 还描述 540/1140/1200；并把其他请求写成旧的 300 秒 | 更新实际语义，非 Agent 说明修正为 600 秒 |

## 2. 文件结构与职责

仅新增两个生产文件，其他改动放回现有责任层：

| 文件 | 职责 |
| --- | --- |
| 新建 `src/agent_search_gateway/providers/diagnostics.py` | 小型内部字段白名单、失败诊断提取、候选计数、无副作用诊断调用辅助；不建注册表或事件总线 |
| 新建 `src/agent_search_gateway/search_result_urls.py` | 纯 URL 解包函数及本地 recognized-invalid 异常；不导入 HTTP/provider/store |
| 修改 `src/agent_search_gateway/observability.py` | 复用 SecretRedactor/formatter/sinks，保证外部诊断文本先脱敏再截断，受控计数输出、日志 URL 清理 |
| 修改 `src/agent_search_gateway/errors.py` | ExecutionFailure 的可选内部 diagnostics 映射；不改 ErrorCode、message 或线上错误帧 |
| 修改 `src/agent_search_gateway/providers/http.py` | 传输元信息、每次真实响应的诊断钩子、终止失败的诊断携带 |
| 修改 `providers/web/common.py`、`providers/academic/common.py`（完整前缀同上） | HTTP Protocol 签名同步、局部候选分类辅助；不代替各厂商业务判断 |
| 修改具体 Web / Academic / LLM / Agent adapter | 只在自己理解的响应字段处提取诊断；保持原严格/宽松解析策略 |
| 修改 `orchestrators/search.py`、`orchestrators/paper.py`、`scheduler/fetch.py`、`academic/enrichment.py`、`llm/stages.py` | 各自所有的终态/依赖事件补安全字段；关键词共用入口解包 |
| 修改 `config.py`、`protocol.py` | 配置和 socket 的 Agent 限制 |
| 修改 `runtime.py:_build_agent_runners` | 给进程诊断绑定配置 alias；不改 provider 装配协议 |
| 修改 README、config.example.toml、docs/public-interface.md、CHANGELOG | 超时、DEBUG、goto 的用户说明；只更新当前文档 |

测试优先扩展现有目录。仅新增 `tests/unit/test_search_result_urls.py` 和 `tests/observability/test_provider_diagnostics.py` 用于独立纯逻辑/安全边界。各 Task 列出准确测试路径；不要为每个 Web provider 复制一套端到端测试。

## 3. 实施前锁定的内部契约

### 3.1 诊断值、失败携带与渲染

选择现有风格的 `Mapping[str, object]` 作为内部诊断容器；只允许固定字段与可验证类型。容器不是公共 JSON schema。

- `DiagnosticText` 放在 observability.py，是一个很小的内部不可变值对象，持有**已由 adapter 选中的标量错误字段**及最大长度；其 str/repr 不暴露内容。不要把 body、prompt、response、stdout/stderr 包装进去。
- 字段选择/类型检查在调用 log_event 前完成；真正的文本渲染由当前 KeyValueFormatter 的 SecretRedactor 完成：原始标量先脱敏，再去敏感 URL、query/fragment、授权/token 材料，移除控制字符/折叠空白，最后截断。保留现有整行二次脱敏兜底。
- 这样不需要全局 redactor 单例、不遍历 logger 私有状态，也不会先切断秘密再导致 SecretRedactor 无法匹配。测试必须覆盖秘密跨截断边界与包含换行的秘密。
- code 上限 128 字符，message 上限 512 字符，分类/原因使用短固定值；保留 `upstream_message_chars`、`upstream_message_bytes` 及 `upstream_message_truncated`，长度指选中错误消息，不是网页正文。
- 仅对 `rejection_counts` 这一受控字段序列化稳定的 `Mapping[str, int]`：原因来自固定集合、值为非负整数、总键数有界。保持其他任意容器/对象的安全占位，不通用序列化任意 dict。
- `safe_diagnostic_fields(fields: Mapping[str, object]) -> dict[str, object]` 与 `diagnostic_fields_from_failure(exc: BaseException) -> dict[str, object]` 放在 providers/diagnostics.py。后者只读已知 ExecutionFailure 的诊断字段，不扫描任意异常属性、不 str(exc)、不抓取 __cause__ 里的 response。它只返回白名单中的 upstream/transport 诊断，不返回 provider、stage、model、attempt、event、reason、error_type、elapsed_ms 等所有者字段，避免 **kwargs 冲突。
- `ExecutionFailure(..., reason: str | None = None, diagnostics: Mapping[str, object] | None = None)` 保存一份内部字段副本。`ProtocolFailure` 继续继承；`ProviderResponseFailure` 复用相同属性，不出现第二种 diagnostics 命名。`HttpStatusFailure` 可接收同名可选参数。
- 公共异常 message 保持原样简短，诊断不拼入 message；protocol 编解码继续只发送既有字段。
- Web 的 `failure(..., reason_code=None, diagnostics: Mapping[str, object] | None = None)` 与 Academic 的 `protocol_failure(..., stage="paper_search", reason=None, diagnostics: Mapping[str, object] | None = None)` 增加同名可选内部参数并转交基类。分别在 Task 6、Task 13 同步实现；原调用继续兼容。
- 新诊断辅助函数及日志 sink 的异常不能改变业务结果；只吞诊断自身的普通异常，不吞 CancelledError 或业务 response inspector 的失败。

### 3.2 HTTP 钩子与字段

在 providers/http.py 定义：

~~~text
ResponseDiagnosticExtractor = Callable[[httpx.Response], Mapping[str, object]]

HttpJsonExecutor.request_json(...,
    response_inspector: ResponseInspector | None = None,
    response_diagnostics: ResponseDiagnosticExtractor | None = None) -> object

HttpJsonExecutor.request_text(...,
    response_diagnostics: ResponseDiagnosticExtractor | None = None) -> str
~~~

同步 web/academic HTTP Protocol 及 Recording*Executor 的同名关键字；测试 double 只接受参数，不仿写真实执行器的诊断逻辑。诊断集成测试使用真实 HttpJsonExecutor + httpx.MockTransport。

- 每个收到的响应恰有一次 `http_attempt_completed` 和一次 `provider_response_diagnostic`，包括最后失败前的响应及 200 头部失败。没有响应的传输错误不伪造这两个事件。
- 保留现有 `status` 字段；新增 `http_status` 作为同值规范名称，避免无故破坏旧日志消费。`upstream_status` 始终指厂商内层状态，不能覆盖外层 HTTP 200。
- `response_bytes` 为实际已读取的 `len(response.content)`，不是相信 Content-Length，也不声称是压缩前后网络线上的字节数。content_type 只留经过验证的媒体类型。
- 诊断钩子只返回数据，绝不决定重试、异常类别或返回值。未知形状用 unknown / invalid_envelope / decode 等安全分类，不猜错误字段。
- adapter 明确选择自己的错误路径；可共享取标量的 helper，但禁止全库递归搜索任何名为 error/message 的键。候选正文和模型 answer 中同名键不是上游诊断。
- 每个响应中钩子、现有 ResponseInspector 提供的字段由 executor 合并为一条诊断；不要在 adapter 再为同一 HTTP 响应发第二条同名事件。adapter 的解析/候选摘要是另外的事件。
- 旧 ResponseInspector 仍在 JSON 解码及 HTTP status 判定前决定 Bright Data 头部失败；诊断故障不能吞掉这项判断。
- 终止的 HTTP status/header 失败携带**最后一次尝试**的诊断；若最后一次是无响应的 transport error，不沿用此前 429 的状态/字节数。所有状态、计数与暂存都在单次调用的闭包/局部对象中。
- 普通 408/429/5xx、transport retry、Bright Data captcha jitter 和非重试 JSON decode 策略保持不变。LLM 外层 protocol retry 仍独立于 HTTP retry。

### 3.3 事件所有权

| 层 | 记录 | 不能重复记录 |
| --- | --- | --- |
| HTTP executor | 每次 HTTP attempt、response diagnostic、retry；最终 http_failed | provider_failed / provider_completed |
| OpenAI Chat | llm_protocol_attempt_started、provider_retrying、llm_protocol_completed；安全 API 诊断来自 HTTP 钩子 | 流水线终态 provider_* |
| LLMStages | 现有 llm_stage_*；judge/clean/safety/focus 的依赖结果 | 已由搜索/Fetch 所有者记录的同一个 provider 终态 |
| Agent runner | agent_process 的安全响应/进程诊断；cleanup failure | 搜索分支 provider_* 终态 |
| SearchOrchestrator | 每个选中的关键词/LLM/Agent invocation 与 scope 分支一个终态 | 把中途 HTTP/协议 retry 记成最终失败 |
| PaperSearchOrchestrator | 每个学术搜索 provider 分支一个终态 | 把后续 OA 失败算成该搜索失败 |
| FetchScheduler._attempt | 每个尝试的 Fetch provider 一个终态，fallback 独立 | 对同一次 Judge 失败重复发 provider_failed |
| enrich_paper_records 的未缓存 resolver 调用 | OA 调用终态；保留 paper_enrichment_failed | 缓存命中重复调用/重复终态；404 标为失败 |
| Daemon | 原 workflow_completed / workflow_failed | 一家 provider 失败就误报整个 workflow 失败 |

同一 provider 的两个 scope、重复配置 invocation 是两个独立参与者，应各有一个终态，而不是全请求按 provider 名粗暴去重。日志仍使用现有 request-id；stage/model 在已知处准确提供。不要增加跨请求全局计数或“已经失败”集合。

### 3.4 候选计数

`CandidateCounts` 是 providers/diagnostics.py 内一个单次响应局部的小对象，保存 received、accepted、rejection_counts、rewritten；不放到 adapter 实例或模块全局。

- 正常完成的候选处理满足 `received == accepted + rejected`，`rejected == sum(rejection_counts.values())`，全坏和空列表也输出摘要。
- adapter 解析用 `phase=adapter`；关键词准入前处理用 `phase=staging`，避免把同一层的两个分母混为一谈。
- 跳过原因明确选择，例如 missing_url、invalid_record、missing_required_field、recognized_invalid_redirect、empty_abstract、judge_execution_failed。保留已有合法分类；不解析异常英文句子生成分类。
- 关键词“正文不合格但保留摘要”的条目仍是已接受候选，只额外记录原 body_rejected。
- 如果严格处理途中终止，不能把尚未处理项说成 rejected；摘要明确 processed / unprocessed，或仅发带已知计数的失败诊断，不伪造完整分区。
- Fetch 的 per-URL status/error/location 集合不是搜索结果数量；其 summary 说明对应集合/phase，未匹配 target 不是格式损坏。
- 保留既有 paper_candidate_rejected，不再追加新的逐条原文事件。最终写出数量与跨 provider 去重数量仍由现有流水线负责。

### 3.5 URL helper

~~~text
unwrap_search_result_url(raw: str) -> str
InvalidSearchResultRedirect(ValueError)
~~~

未知形状返回原字符串；已知 goto 但无效抛本地异常，异常只含固定原因，不携带 URL。调用方只捕获这一类异常以跳过该候选。普通直接 URL 的现有 InputFailure 行为不改变。

新增 helper 只用 urllib.parse 与纯验证。_stage_keyword_hit 在任何 normalize_url 调用前调用 helper。不改 normalize_url、URLStore 或 url-fetch 的 URL 规则。

## 4. Tasks

每项按步骤逐一执行；参数化列中的后续行为也逐次经过 TDD，不先实现整组行为。完整测试与最终门禁都不得开启 live provider 开关。


### Task 1: 放开 Agent 超时配置范围

**Files:**

- Modify: `src/agent_search_gateway/config.py:598-606,609-676`。
- Test: `tests/unit/test_config_agent.py`。
- Docs: `README.md`、`config.example.toml`（Task 21 统一更新）。

- [ ] **Step 1: Write the failing test**

在现有 `test_agent_valid_numeric_boundaries` 中逐次加入 540.1、3600、7200；新增明确命名的 `test_agent_timeout_has_no_gateway_upper_bound`，断言解析结果等于原值。把 540.1 从“非法 provider fields”表移出。保留/补齐 0、-1、负小数、bool、字符串、NaN、±inf 拒绝用例及默认 300 用例。

Setup 使用已有 _agent_data / _environment / ProviderRegistry，不启动进程。首个 RED 是 3600 被现有 >540 分支拒绝。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/unit/test_config_agent.py -k agent_timeout_has_no_gateway_upper_bound -v
~~~

Expected: FAIL，当前 ConfigFailure 表明 must not exceed 540；不是 fixture/导入错误。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
resolve_agent_config:
    读取 timeout_seconds，缺省 300
    删除独立的 >540 检查
    仍调用 _positive_float
    将得到的 float 原样写入 AgentProviderConfig
~~~

不将 inf/None/0 解释为永久运行，也不更改 HTTP retry.request_timeout_seconds。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/unit/test_config_agent.py tests/runtime/test_agent_runtime_assembly.py tests/doctor/test_config.py -v
~~~

Expected: 所有有效大值通过、原非法值仍失败，原默认值和配置装配通过；再按 `0.1 执行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

只去除失效的“最大值”测试说明，复用 _positive_float，不新增通用超时策略。若有重构，重跑本 Task 的完整命令及全量测试。

### Task 2: 移除 Agent 外层工作流 deadline，保留取消与部分成功

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:38,253-288`。
- Test: `tests/orchestrators/test_agent_search_deadline.py`。
- Test: `tests/orchestrators/test_agent_search.py`、`tests/daemon/test_agent_shutdown.py`。
- Reference: `tests/acceptance/test_gateway_workflows.py:test_cli_waits_for_example_http_retry_budget`。

- [ ] **Step 1: Write the failing test**

把原先断言 1140 秒会中止的三组测试替换为相反的业务契约：

1. `test_agent_workflow_can_complete_after_old_deadline`：web/paper/all 参数化，受控 runner 在虚拟 1141 秒后返回，最终 JSONL 存在且内容正确。
2. 队列中第二次调用仍能在第一次后获得 quota 并完成；等待 quota 不消耗该进程自己的预算。
3. Web 已完成时 Paper/OA 最终阶段可继续等待；成功后按原次序写出，不受旧整体计时器取消。

复用既有虚拟 loop.time 方式、Event 和局部 monkeypatch.context；让真实 asyncio timer 回调运行。测试外层防挂 watchdog 使用真实的短时间控制且避免被虚拟时钟推进误触发。不要只把生产 timeout 常量改成 0.03 再等真实超时。

保留外部 cancellation、两分支部分成功、全部失败和 LLM 不受影响的回归；移除回归对已删除常量的依赖。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/orchestrators/test_agent_search_deadline.py -k can_complete_after_old_deadline -v
~~~

Expected: FAIL，旧工作流 deadline 取消请求，无法得到预期结果；无 1140 秒真实等待。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
agent_search:
    保留 request_id / prompt / scope / invocation 选择验证
    直接 await 原 _text_search(...)
    删除 _AGENT_WORKFLOW_TIMEOUT_SECONDS
    删除仅服务于该 deadline 的 TimeoutError -> workflow timed out 转换

保留 _text_search 的 gather、部分成功、aggregation 和 writer
不移动 runner 内部 timeout / quota / cleanup
~~~

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/orchestrators/test_agent_search_deadline.py tests/orchestrators/test_agent_search.py tests/orchestrators/test_agent_provider_model_filtering.py tests/daemon/test_agent_shutdown.py -v
~~~

Expected: 虚拟旧上限之后仍成功；外部取消等待清理；单次 Agent timeout 的成功兄弟分支仍保留；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

删除无用 deadline 分支与误导测试名。虚拟时间辅助仅在确有第二处复用时移入 tests/support，不给生产类加测试专用方法。重构后重跑上述组及全量测试。

### Task 3: Agent socket 默认无固定等待上限

**Files:**

- Modify: `src/agent_search_gateway/protocol.py:27-32,266-302`。
- Test: `tests/unit/test_socket_client.py`。
- Test: `tests/acceptance/test_agent_search_workflows.py`。
- Test: `tests/acceptance/test_gateway_workflows.py`。
- Reference: `tests/support/acceptance.py`、`tests/support/agent_search.py`。

- [ ] **Step 1: Write the failing test**

更新 `test_socket_timeout_depends_on_request_type_and_honors_override` 的期望矩阵：默认 Agent 为 None，其他五类仍为 600；0.01/42 等显式覆盖继续优先。

增加 `test_agent_cli_receives_response_after_old_socket_deadline`：通过真实 CLI → 本地 Unix socket → Daemon → 已移除外层限制的 Agent pipeline；用受控 runner/虚拟时钟推进到 1201 秒后完成，断言 CLI exit=0、stdout 仍只输出结果路径、JSONL 内容不变。

补回归：Agent 显式短数值 timeout 仍失败；daemon 断连/EOF 立即给原 ProtocolFailure；任务取消后 writer 关闭、无忙循环；现有非 Agent 三次 180 秒重试验收保持有效。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/unit/test_socket_client.py -k socket_timeout_depends_on_request_type_and_honors_override -v
uv run --offline pytest tests/acceptance/test_agent_search_workflows.py -k receives_response_after_old_socket_deadline -v
~~~

Expected: Agent 默认值仍为 1200 导致断言失败；端到端请求在旧 socket deadline 失败。其他 request 类型的矩阵通过。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
send_request:
    if response_timeout_seconds is None and request 不是 AgentSearchRequest:
        response_timeout_seconds = 600
    Agent 的默认 None 保留到 asyncio.wait_for
    显式数值保持原值

删除 _AGENT_RESPONSE_TIMEOUT_SECONDS
保留 socket frame limit、readline、EOF、finally close
~~~

不加 sentinel 参数、不读取 daemon TOML、不新增 NDJSON deadline 字段。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/unit/test_socket_client.py tests/unit/test_protocol_codec.py tests/acceptance/test_agent_search_workflows.py tests/acceptance/test_gateway_workflows.py -v
~~~

Expected: 新默认与显式覆盖正确，1201 秒虚拟响应到达，原 600 秒 HTTP 验收和协议 framing 通过；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

将测试 monkeypatch 的范围限制到自身，恢复 loop.time；测试不能把 waiter 本身换成“永远成功”而绕过真实 deadline。重构后复跑上述组及全量测试。

### Task 4: 建立安全、有界的公共诊断值与失败携带

**Files:**

- Create: `src/agent_search_gateway/providers/diagnostics.py`。
- Modify: `src/agent_search_gateway/observability.py:82-214`。
- Modify: `src/agent_search_gateway/errors.py:48-53`。
- Create: `tests/observability/test_provider_diagnostics.py`。
- Test: `tests/observability/test_redaction.py`、`tests/unit/test_observability_logging.py`、`tests/unit/test_errors.py`。
- Reference: `tests/support/logging.py`。

- [ ] **Step 1: Write the failing test**

先通过现有 log_event + KeyValueFormatter 写一个受控 rejection_counts，验证实际序列化结果含准确计数而不是 <dict>，命名 `test_candidate_counts_render_as_bounded_structured_values`。

逐个追加公共诊断契约测试：

- 512/128 字符上限、原始字符/UTF-8 字节长度、截断标识。
- 已配置 secret 跨截断边界或含换行时不会泄漏任何残片；SecretRedactor 必须先于清理/截断。
- 消息里的带凭据 URL、签名 query、token=、Authorization/Bearer 和控制字符清理；单行日志无注入。
- 未知键、嵌套 dict/list、带危险 __repr__/__str__ 的值被省略，不调用 repr。
- 普通候选 url 日志也去 query/fragment/userinfo；存储用 URL 完全不改。
- ExecutionFailure 可携带诊断，但 message/str/error frame 不出现诊断内容。
- 诊断提取器、formatter 或自定义 handler 抛普通异常时，业务返回值不改变；取消仍传播。
- CandidateCounts 每次调用隔离，0 个输入与全坏输入也准确，非法原因/负计数不会进入序列化事件。

新 helper 的测试在现有入口首个 RED 已证实缺陷后逐步建立；不要提交 import 错误充当 RED。诊断文本边界测试可直接走现有 log_event/formatter 的相应字段，新实现把选中的外部字段包装为 DiagnosticText。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/observability/test_provider_diagnostics.py -k candidate_counts_render_as_bounded_structured_values -v
~~~

Expected: FAIL，现有 formatter 将字典输出为 <dict>。后续各 RED 必须分别由缺少边界保护、诊断携带或长度约束触发。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
observability:
    增加 DiagnosticText，只供选中的上游标量字段使用
    formatter: redactor -> URL/敏感键清理 -> 控制字符/空白 -> 长度上限
    生成长度/截断字段，再用既有 key=value 格式渲染
    rejection_counts 只允许固定原因到非负整数的有界映射
    target_url_for_log 复用既有 _url_for_log，去掉 query/fragment
    维持最终整行 redactor；日志 sink 普通异常不冒入业务

errors.ExecutionFailure:
    接收可选 diagnostics，复制映射，不改 message/code/reason 兼容语义

providers/diagnostics:
    固定字段选择与安全值构造
    从已知 ExecutionFailure 提取内部诊断
    提供局部 CandidateCounts 与 best-effort 诊断调用辅助
~~~

不将完整 payload 交给通用 serializer；不引入 logger 注册中心或新的配置项。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/observability/test_provider_diagnostics.py tests/observability/test_redaction.py tests/unit/test_observability_logging.py tests/unit/test_errors.py tests/unit/test_protocol_codec.py -v
~~~

Expected: 对整条序列化日志做 sentinel 检查全部通过，轮转/DEBUG 开关/公共异常与协议旧测试保持通过；再运行全量测试。只更新确因日志 URL 去 query 的旧断言，不更改实际结果 URL 断言。

- [ ] **Step 5: Refactor (keep tests green)**

合并重复 sanitizer/计数渲染；通用模块不导入具体 adapter、HTTP、orchestrator 或 runtime。检查错误模块只依赖标准库类型，避免 errors → observability → errors 循环。复跑上述组及全量测试。

### Task 5: 每次 HTTP 响应都有诊断，且不改变重试

**Files:**

- Modify: `src/agent_search_gateway/providers/http.py`。
- Modify: `src/agent_search_gateway/providers/web/common.py:11-37`。
- Modify: `src/agent_search_gateway/providers/academic/common.py:16-37`。
- Modify: `tests/support/http.py`。
- Test: `tests/providers/test_http_executor.py`。
- Test: `tests/observability/test_provider_diagnostics.py`。

- [ ] **Step 1: Write the failing test**

增加 `test_every_received_response_has_bounded_diagnostics`：真实 HttpJsonExecutor + MockTransport，脚本返回 429 → 502 → 200，断言三条 response diagnostic、三条 completed、两条 http_retrying、没有 http_failed，记录正确 status/type/bytes 和 attempt。

随后逐次覆盖：201 JSON 成功、允许的 text 2xx、status 重试耗尽、transport 无响应、先 429 再 transport 终止、200 非法 JSON、巨大 JSON/HTML、未知错误结构、诊断回调抛错/尝试覆盖 provider/stage/attempt。

同时断言 sleep 延迟、请求次数、返回 payload 和异常类别等原行为。诊断 callback 不可借此改变重试；若 ResponseInspector 仍判定失败，诊断 helper 故障不能把它变成功。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/test_http_executor.py -k every_received_response_has_bounded_diagnostics -v
~~~

Expected: FAIL，目前缺少 provider_response_diagnostic 和 byte/type 字段；不能只测试 mock 的调用次数。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
每次真实 response 到达:
    计算安全 transport 元信息
    记录 http_attempt_completed
    在保留原 ResponseInspector 判断的前提下，收集其安全诊断
    在独立的普通异常保护范围内调用 response_diagnostics
    合并白名单字段，记录一次 provider_response_diagnostic
    按原 inspector / HTTP status 顺序进入原重试逻辑

请求结束:
    原 request_json 解码与 ProtocolFailure 行为不变
    http_failed 带最后一次可用的 transport/upstream 字段
    HttpStatusFailure / ProviderResponseFailure 使用统一 diagnostics 属性
~~~

request_json/request_text 及 Protocol/double 同步 `response_diagnostics` 关键字。旧 status 保留且 http_status 同值。诊断不能用 executor 实例字段缓存 last_response；不把上次响应错误地附到后来无响应的 transport 失败。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/test_http_executor.py tests/unit/test_retry.py tests/observability/test_provider_diagnostics.py tests/providers/web/test_brightdata.py -v
uv run --offline mypy src tests scripts
~~~

Expected: attempts、原 backoff/jitter、异常语义不变；每个收到的响应有一条诊断，缺失元信息不伪造；Protocol 和 fake 签名通过 mypy；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

复用 _provider_failure_details 和已有失败类型；若增加 _RetryableStatus 的 diagnostics 参数，所有构造点同步。删除可重复的元信息拼装，不改 retry_async 的策略。重构后跑完整 HTTP/Retry 组和全量测试。


### Task 6: 用 ScraperAPI 建立 Web 候选摘要与局部错误分类模式

**Files:**

- Modify: `src/agent_search_gateway/providers/diagnostics.py`。
- Modify: `src/agent_search_gateway/providers/web/common.py`。
- Modify: `src/agent_search_gateway/providers/web/scraperapi.py`。
- Test: `tests/providers/web/test_scraperapi.py`。
- Test: `tests/providers/web/test_search_result_tolerance.py`。
- Test: `tests/observability/test_provider_diagnostics.py`。

- [ ] **Step 1: Write the failing test**

`test_scraperapi_reports_partial_candidate_rejections`：输入 10 条 organic_results，其中 5 条有效、3 条缺 link、2 条不是 object。断言输出仍为 5 个正确 KeywordSearchHit，并且只有一条 phase=adapter summary，received=10、accepted=5、rejected=5、原因计数准确。

逐次扩展：空列表、全坏列表、错误 presentation 字段、顶层 error、顶层 envelope 损坏。通过真实 executor + MockTransport 的错误响应测试，确认已知 error 标量在 DEBUG 可见而公共异常 message 不出现上游文本。新诊断用例的 factory 参数使用 JsonRequester/HttpRequester 这些结构协议，使真实 executor 与 Recording* fake 都可用；不在 fake 内重现诊断事件来让断言通过。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web/test_scraperapi.py -k reports_partial_candidate_rejections -v
~~~

Expected: 有效 hit 断言通过，但缺少摘要事件导致 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
Web common:
    为固定 URL 键 url/link 提供局部候选拒绝分类
    非对象 -> invalid_record
    缺失/空 URL -> missing_url
    其余已有解析失败 -> invalid_record（不用异常字符串猜原因）
    提供选择已知顶层 error 标量/明确 code/message 子字段的小 helper

ScraperAPI.search:
    把已知响应诊断 extractor 传给 HTTP 调用
    保留当前 root.error 判定与 require_list
    建立本次局部 CandidateCounts
    解析每项；原 except continue 前累加固定原因
    结束后发一次 summary，返回原 hits
    业务失败携带选中字段，原 failure message 不变
~~~

HTTP diagnostic 由 executor 发；adapter 不重复发第二条 response diagnostic。不得因 added diagnostics 把原可接受 payload 改成失败。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web/test_scraperapi.py tests/providers/web/test_search_result_tolerance.py tests/observability/test_provider_diagnostics.py -v
~~~

Expected: 正常/空/全坏摘要准确，原结果容错与公共失败断言通过；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

只抽取已重复出现的字段选择和计数输出，不抽象 provider 的响应 envelope 或 parser。保持 RecordingHttpExecutor 测结果、真实 HttpJsonExecutor 测诊断的分工。重构后跑上述组及全量测试。

### Task 7: Bright Data 头部与业务响应诊断统一

**Files:**

- Modify: `src/agent_search_gateway/providers/web/brightdata.py:27-157`。
- Modify: `src/agent_search_gateway/providers/http.py`（仅复用现有 inspector diagnostics 接口）。
- Test: `tests/providers/web/test_brightdata.py`。
- Test: `tests/providers/test_http_executor.py`。

- [ ] **Step 1: Write the failing test**

`test_brightdata_response_diagnostic_preserves_outer_and_upstream_status`：HTTP 200、x-brd-status-code=502、x-brd-error-code=captcha、x-brd-error=redirect location was rejected、空 body。断言 HTTP 200 与上游 502/captcha/短消息同时可见，response_bytes=0，分类为 header/provider 错误而非 decode；重试次数/延迟仍按旧行为。

再逐次覆盖成功 organic、混合/全坏 organic 的计数、HTTP 200 JSON error、未知错误 code、畸形/重复 header、秘密与超长 header。保留 401/403 不重试、未知 code 原策略、普通 HTTP 重试及既有 jitter 测试。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web/test_brightdata.py -k response_diagnostic_preserves_outer_and_upstream_status -v
~~~

Expected: 旧 Bright Data 专用字段虽存在，但缺少统一 upstream 字段/响应摘要，目标断言 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
复用 _bounded_header / _inspect_search_response:
    原分类、retryable、safe_reason、HTTP status 优先级保持
    diagnostics 中补 canonical upstream_status/code/message
    原 brightdata_status / brightdata_error_code 字段保留兼容
    对已验证的标量 header 使用公共安全文本值
    畸形/重复 header 只留分类，不拼接或输出原始内容

search 的 JSON extractor:
    仅观察已知 error/organic envelope
    与 inspector diagnostics 合并为一条响应事件

organic 解析沿用 Task 6 局部计数模式
~~~

保留现有搜索请求体和 Fetch 请求参数。body 为空时绝不调用 JSON 解码掩盖头部失败。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web/test_brightdata.py tests/providers/test_http_executor.py tests/unit/test_retry.py -v
~~~

Expected: 原重试分类、jitter、鉴权与请求快照不变，统一诊断和计数通过；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

诊断与控制流共用已有 header 解析，不增加第二套 retryability 规则。保留只有安全字段可越过边界的 reserved-key 防护。重构后重跑上述组及全量测试。

### Task 8: 其余 Google SERP 适配器接入同一诊断模式

**Files:**

- Modify: `src/agent_search_gateway/providers/web/scrape_do.py`。
- Modify: `src/agent_search_gateway/providers/web/scrapingdog.py`。
- Modify: `src/agent_search_gateway/providers/web/serpapi.py`。
- Test: `tests/providers/web/test_scrape_do.py`、`tests/providers/web/test_scrapingdog.py`、`tests/providers/web/test_serpapi.py`。
- Test: `tests/providers/web/test_search_result_tolerance.py`。

- [ ] **Step 1: Write the failing test**

在现有 parameterized factory/payload 表扩展 `test_serp_adapters_report_rejected_candidate_counts`，按 scrape_do → scrapingdog → serpapi 逐一执行 RED/GREEN。各自输入仍使用 organic_results/link；验证 mixed、empty、all-invalid 三种摘要，以及顶层 error 的安全诊断。

此时不在 adapter 解包 Google URL；结果 URL 原样返回的单元契约仍成立，真正解包由 Task 19–20 实现。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web/test_search_result_tolerance.py -k serp_adapters_report_rejected_candidate_counts -v
~~~

Expected: 尚未接入的各 adapter 缺 summary；原有效结果本身仍正确。一次只推进一个 adapter 的实现。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
每个 search 方法:
    声明自身 organic_results / error 字段诊断提取
    传 response_diagnostics，不改请求和业务异常
    按 Task 6 统计解析接受/拒绝
    返回原 KeywordSearchHit 列表

同一错误字段的选择可复用 web/common
不增加 if provider_name == ... 的共享调度器
~~~

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web/test_scrape_do.py tests/providers/web/test_scrapingdog.py tests/providers/web/test_serpapi.py tests/providers/web/test_search_result_tolerance.py -v
~~~

Expected: 每种 provider 的三种候选数量场景和当前请求/错误语义通过；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

删除复制的安全文本截断/计数序列化，使用公共 helper；保留不同厂商请求代码。重构后复跑上述组及全量测试。

### Task 9: AnySearch、Brave、Linkup、ScrapeGraphAI 接入诊断

**Files:**

- Modify: `src/agent_search_gateway/providers/web/anysearch.py`。
- Modify: `src/agent_search_gateway/providers/web/brave.py`。
- Modify: `src/agent_search_gateway/providers/web/linkup.py`。
- Modify: `src/agent_search_gateway/providers/web/scrapegraphai.py`。
- Test: `tests/providers/web/test_anysearch.py`、`tests/providers/web/test_brave.py`、`tests/providers/web/test_linkup.py`、`tests/providers/web/test_scrapegraphai.py`。
- Test: `tests/providers/web/test_search_result_tolerance.py`。

- [ ] **Step 1: Write the failing test**

增加参数化 `test_json_search_adapters_report_response_and_candidate_diagnostics`，逐个 provider 实施。用当前 fixture 的真实 envelope：

| Provider | 已知结果/状态边界 | 特别断言 |
| --- | --- | --- |
| AnySearch | code / data.results | code=-1、message=failed 在 DEBUG 保留；code=0 成功；bool 不是有效 code |
| Brave | web.results | 原“没有 web”空结果行为不变；只识别明确的 API error envelope |
| Linkup | results，name/content | malformed name/content 不静默丢弃，正文/摘要不记日志 |
| ScrapeGraphAI | error/detail、results | error/detail 的标量可诊断；嵌套未知详情只留类型/分类 |

缺少已验证上游错误 shape 的场景只断言安全 metadata/category，不凭空编造错误 code。每家都有 mixed/empty/all-invalid 计数。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web/test_search_result_tolerance.py -k json_search_adapters_report_response_and_candidate_diagnostics -v
~~~

Expected: 新摘要/已知 vendor 字段尚不存在，目标断言 FAIL；现有结果测试不应被 fixture 差异打坏。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
分别保留原 code / web / results / error-detail 判断
为每个已知 envelope 提供纯诊断选择函数
错误值只选择字符串/有限数值及明确 code/message 子字段
候选循环局部累计拒绝原因
空结果分支也输出 received=accepted=rejected=0
~~~

诊断选择器不递归搜索 content 中的 error/message，也不扩大原业务失败条件。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web/test_anysearch.py tests/providers/web/test_brave.py tests/providers/web/test_linkup.py tests/providers/web/test_scrapegraphai.py tests/providers/web/test_search_result_tolerance.py -v
~~~

Expected: 当前 provider 自身解析/请求契约及新日志行为通过；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

只复用已知的字段类型检查，避免一个“任何 JSON 都认得”的错误解释器。重构后跑上述完整组和全量测试。

### Task 10: Tavily、Exa、Firecrawl 搜索结果与内容诊断

**Files:**

- Modify: `src/agent_search_gateway/providers/web/tavily.py:37-71`。
- Modify: `src/agent_search_gateway/providers/web/exa.py:37-66,86-95`。
- Modify: `src/agent_search_gateway/providers/web/firecrawl.py:56-100,114-118`。
- Test: `tests/providers/web/test_tavily.py`、`tests/providers/web/test_exa.py`、`tests/providers/web/test_firecrawl.py`。
- Test: `tests/providers/web/test_search_result_tolerance.py`。

- [ ] **Step 1: Write the failing test**

`test_content_search_adapters_report_counts_without_logging_bodies` 按 provider 参数化，使用现有 search fixture，在 content/raw_content/text/markdown/rawHtml/highlights 中加入唯一正文 sentinel，并加入 missing URL、错误字段类型候选。

断言：返回 hit 内容仍完整；只增加响应大小、结果数量和拒绝原因；序列化日志无正文 sentinel。Firecrawl success=false 的短错误消息可诊断；其他 adapter 只观察已有可识别错误字段，不把正文内 error 当 API 错误。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web/test_search_result_tolerance.py -k content_search_adapters_report_counts_without_logging_bodies -v
~~~

Expected: 缺少 candidate summary 的断言 FAIL。现有正文不泄漏的回归可能已经通过，应保留而不是声称其 RED。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
保留三个现有结果映射、snippet fallback 和 body 优先级
HTTP 请求接入明确的 response_diagnostics
Firecrawl _successful_root 的业务失败附安全 diagnostics
原 except ExecutionFailure: continue 前记录拒绝类别
只从实际业务列表计算数量，不对整个 JSON repr 或截取正文
~~~

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web/test_tavily.py tests/providers/web/test_exa.py tests/providers/web/test_firecrawl.py tests/providers/web/test_search_result_tolerance.py tests/orchestrators/test_keyword_search_pipeline.py -v
~~~

Expected: snippets/body 语义、容错、Judge 入口不变；元信息/计数准确；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

复用 CandidateCounts 和 safe fields；保留 Firecrawl v2、Exa highlights 和 Tavily raw content 的各自语义。重构后复跑上述组及全量测试。

### Task 11: Parallel、TinyFish、Decodo 的特殊结果 envelope

**Files:**

- Modify: `src/agent_search_gateway/providers/web/parallel.py:96-128`。
- Modify: `src/agent_search_gateway/providers/web/tinyfish.py:40-68`。
- Modify: `src/agent_search_gateway/providers/web/decodo.py:37-63,77-95`。
- Test: `tests/providers/web/test_parallel.py`、`tests/providers/web/test_tinyfish.py`、`tests/providers/web/test_decodo.py`。
- Test: `tests/providers/web/test_search_result_tolerance.py`。

- [ ] **Step 1: Write the failing test**

`test_nested_search_adapters_report_candidate_diagnostics` 逐个覆盖：

- Parallel：results/excerpts，错误 excerpts 的条目被计数；加入现有 tolerance 表之前遗漏的 Parallel。
- TinyFish：results/snippet，query 只用于请求，诊断 endpoint 不带 query。
- Decodo：results[0].status_code、content.errors、content.results.results.organic；计数分母为 organic 条数，不是外层 pages 数。

Decodo 原解析只使用第一页，诊断不得把其余页改成新搜索结果，也不把状态 12000 等其他模式误判为本接口成功。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web/test_search_result_tolerance.py -k nested_search_adapters_report_candidate_diagnostics -v
~~~

Expected: 每家未接入的摘要/特殊内层状态断言 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
保持原 results / excerpts / organic 解析
各自仅提取自己的已知 envelope/status/error 元数据
Decodo errors 列表只提取有界个数的已知标量错误字段/数量
不展开未知错误对象或所有错误记录
为实际结果列表建立局部计数，输出一次 summary
~~~

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web/test_parallel.py tests/providers/web/test_tinyfish.py tests/providers/web/test_decodo.py tests/providers/web/test_search_result_tolerance.py -v
~~~

Expected: 三家专有配置、请求快照和原失败策略不变；15 家关键词 adapter 均有计数覆盖；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

将通用诊断安全逻辑收回 helper，保留专有 envelope 路径在 adapter。完整 Web tolerance 表应显式列出 15 家，避免仅测试类名集合而不执行请求。重构后跑上述组及全量测试。


### Task 12: 全部 Fetch 后端保留安全上游错误与内容结果

**Files:**

- Modify: `src/agent_search_gateway/providers/web/tavily.py`、`src/agent_search_gateway/providers/web/exa.py`、`src/agent_search_gateway/providers/web/firecrawl.py`。
- Modify: `src/agent_search_gateway/providers/web/linkup.py`、`src/agent_search_gateway/providers/web/parallel.py`、`src/agent_search_gateway/providers/web/tinyfish.py`。
- Modify: `src/agent_search_gateway/providers/web/scrapegraphai.py`、`src/agent_search_gateway/providers/web/scrapingant.py`。
- Modify: `src/agent_search_gateway/providers/web/brightdata.py`、`src/agent_search_gateway/providers/web/decodo.py`、`src/agent_search_gateway/providers/web/scrape_do.py`。
- Modify: `src/agent_search_gateway/providers/web/scraperapi.py`、`src/agent_search_gateway/providers/web/scrapingdog.py`、`src/agent_search_gateway/providers/web/jina.py`、`src/agent_search_gateway/providers/web/zenrows.py`。
- Test: `tests/providers/web/test_tavily.py`、`tests/providers/web/test_exa.py`、`tests/providers/web/test_firecrawl.py`、`tests/providers/web/test_linkup.py`、`tests/providers/web/test_parallel.py`、`tests/providers/web/test_tinyfish.py`、`tests/providers/web/test_scrapegraphai.py`、`tests/providers/web/test_scrapingant.py`、`tests/providers/web/test_brightdata.py`、`tests/providers/web/test_decodo.py`、`tests/providers/web/test_scrape_do.py`、`tests/providers/web/test_scraperapi.py`、`tests/providers/web/test_scrapingdog.py`、`tests/providers/web/test_jina.py`、`tests/providers/web/test_zenrows.py`。
- Test: `tests/scheduler/test_fetch_outcomes.py`。

- [ ] **Step 1: Write the failing test**

按下表逐个新增 `test_fetch_reports_safe_response_diagnostics`（放在各自现有测试文件），每次只实施一个 adapter/独特错误路径，不能一口气改 15 家：

| 路径组 | 用例与应保留语义 |
| --- | --- |
| Tavily | mixed results 跳过、未匹配 URL、匹配但 raw_content 为空；保留 invalid_results_envelope / no_matching_result / empty_raw_content |
| Exa | 目标匹配的 statuses[].status、结果 text；错误状态可见但不泄漏 id URL |
| Parallel / TinyFish | 目标匹配的 errors[] 与成功结果；错误项只取已知安全 error/code/message，不打印整个数组 |
| Firecrawl / Linkup / ScrapeGraphAI / ScrapingAnt | 原 success/error/detail/body envelope 校验；结果内容只记长度或是否存在 |
| 七个 text Fetch 后端 | request_text 的成功/空内容/HTTP 错误元信息；不把 HTML/Markdown 正文当错误 message |

每种路径验证成功、失败及原有严格/容错顺序。正文、目标 URL query、鉴权 header 放 sentinel；日志不出现。若 Fetch parser 原本遇到首个畸形项就失败，不能为“统计完整”扫描并接受后续项。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/web -k fetch_reports_safe_response_diagnostics -v
~~~

Expected: 新的 extraction/上游错误诊断缺失导致 FAIL；已由 Task 5 提供的普通传输元信息是回归项，不人为制造 RED。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
分别在原 fetch 调用接入对应的纯 response_diagnostics
保留 target URL 匹配、结果返回和异常分支顺序
已知 per-URL 错误仅选择匹配目标的安全 code/message/status
Tavily 原 silent skips 记录局部数量和明确原因
text 后端只记录类型、字节数、非空/空分类
所有 failure 保持原 message/reason；附内部诊断
~~~

不把搜索用 Bright Data header retry 策略迁移到 Fetch；不改变任何 URL-fetch 请求参数。统计早返回的数组时明确 processed/unprocessed，不能伪称未检查项被拒绝。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/web tests/scheduler/test_fetch_outcomes.py tests/orchestrators/test_url_fetch_flow.py -v
~~~

Expected: 全部 Fetch 映射、错误/空内容、fallback 和 Judge 策略保持，通过新响应诊断断言；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

把 text 后端重复元信息留给 shared executor；只在 adapter 保留自身语义字段选择。日志 endpoint 若嵌入目标 URL（例如 Jina 路径），清理嵌套的敏感 URL 或只记录服务端 host，不泄漏内层凭据/query。复跑完整 Web/Fetch 组及全量测试。

### Task 13: 四个 JSON 学术提供商的响应与候选摘要

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/common.py`。
- Modify: `src/agent_search_gateway/providers/academic/crossref.py`。
- Modify: `src/agent_search_gateway/providers/academic/openalex.py`。
- Modify: `src/agent_search_gateway/providers/academic/semantic_scholar.py`。
- Modify: `src/agent_search_gateway/providers/academic/core.py`。
- Test: `tests/providers/academic/test_crossref.py`、`tests/providers/academic/test_openalex.py`、`tests/providers/academic/test_semantic_scholar.py`、`tests/providers/academic/test_core.py`。
- Test: `tests/orchestrators/test_paper_search_pipeline.py`。

- [ ] **Step 1: Write the failing test**

在各现有文件新增 `test_paper_response_reports_candidate_summary`，逐次使用各自 envelope：Crossref message.items、OpenAlex results、Semantic Scholar data、CORE results。

每家测试 valid/mixed/all-invalid/empty；检查 received/accepted/rejected 与原 paper_candidate_rejected 一致。Semantic Scholar 的 `{"error": "..."}` fixture 仍抛 missing_data_envelope，但 DEBUG 可保留安全 error 标量；公共异常 message 不包含上游文本。未知 envelope 不臆造 code。

不要把正常缺失的可选 authors/topics/abstract 当作“整条论文拒绝”；也不改变既有字段标准化/聚合策略。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/academic/test_crossref.py tests/providers/academic/test_openalex.py tests/providers/academic/test_semantic_scholar.py tests/providers/academic/test_core.py -k paper_response_reports_candidate_summary -v
~~~

Expected: 原拒绝事件可能存在，但没有准确 aggregate summary，导致 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
每个 search:
    传本 provider 的响应诊断 extractor
    保留原 envelope 检查及 ProtocolFailure reason
    在现有候选循环统计接受/拒绝
    保留 reject_item，必要时传明确稳定 reason
    原缺必填字段处记录 missing_required_field
    未知记录形状保留 invalid_record_shape
    空/全坏列表也输出一次 summary
~~~

如为说明更精确原因需要调整私有映射 helper，只改变内部调用，search 返回类型仍为 list[PaperSearchHit]；不要把 paper aggregation 移入 provider。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/academic tests/orchestrators/test_paper_search_pipeline.py tests/academic -v
~~~

Expected: 学术字段映射、身份去重、元数据优先级、OA 调用顺序均保持；响应/计数诊断准确；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

公共 helper 仅负责安全字段与计数，不持有 response/paper 内容；rejection reason 来自实际已有校验分支，不复制一套新的验证策略。重构后跑上述组及全量测试。

### Task 14: arXiv / DBLP 的 XML 诊断与候选拒绝

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/arxiv.py`。
- Modify: `src/agent_search_gateway/providers/academic/dblp.py`。
- Modify: `src/agent_search_gateway/providers/academic/common.py`（仅复用计数）。
- Test: `tests/providers/academic/test_arxiv.py`、`tests/providers/academic/test_dblp.py`。
- Reference: `tests/fixtures/providers/academic/arxiv/`、`tests/fixtures/providers/academic/dblp/`。

- [ ] **Step 1: Write the failing test**

`test_xml_provider_reports_response_and_candidate_diagnostics`：使用已有 XML fixtures，覆盖有效 feed/结果、一个缺必填字段、全坏、空、malformed XML 和错误根元素。断言 type/bytes、decode 或 envelope 分类、准确 entry/hit 数量；XML/HTML 正文 sentinel 不进日志。

DBLP 返回验证页面时只根据已有解析边界记录 invalid XML/envelope 或 transport status，不根据 HTML 文本猜测机器人状态。现有 README 低频也可能触发验证的说明保留。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/academic/test_arxiv.py tests/providers/academic/test_dblp.py -k xml_provider_reports_response_and_candidate_diagnostics -v
~~~

Expected: 缺少响应分类/计数摘要导致 FAIL，原 ProtocolFailure 本身不应改变。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
request_text 继续走 shared transport diagnostics
复用 adapter 已有 XML parser 的结果/失败分类
在解析失败时附安全 category，不输出 ET.ParseError 原文或 XML
候选循环保留当前 _map_entry / mapper 选择和 reject_item
按实际 entry/hit 计数并输出 summary
~~~

如果响应钩子为了提前分类需要解析 XML，使用同一纯分类逻辑并保持诊断异常隔离；不要为日志增加网络调用或接受旧代码不接受的 XML 错误格式。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/academic/test_arxiv.py tests/providers/academic/test_dblp.py tests/providers/test_http_executor.py tests/orchestrators/test_paper_search_pipeline.py -v
~~~

Expected: XML 映射及严格失败语义不变，错误和正常响应都可诊断；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

不要保留为日志而重复读取/格式化整段 XML 的代码。复用现有 namespace、mapper 和安全事件。重构后复跑上述组及全量测试。

### Task 15: Unpaywall 正常无匹配与 OA 失败区别记录

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/unpaywall.py:33-93`。
- Modify: `src/agent_search_gateway/academic/enrichment.py:16-48`。
- Test: `tests/providers/academic/test_unpaywall.py`。
- Test: `tests/academic/test_oa_enrichment.py`。
- Test: `tests/orchestrators/test_paper_search_pipeline.py`。

- [ ] **Step 1: Write the failing test**

`test_oa_no_match_and_outage_have_distinct_terminal_diagnostics`：分别脚本返回 404、200 有效 OA、500 重试耗尽和 200 错误 envelope。验证 404 返回 None，业务终态是 completed/no_match，只有 500/解析失败有该 resolver 调用的 provider_failed，并保留 paper_enrichment_failed。论文全部保留。

补回归：同 DOI 缓存仅调用一次、已缓存 None 不重新调用；畸形 location 的跳过只记录计数/原因，不泄漏 URL；email secret 不出现在任何日志或 endpoint。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/academic/test_oa_enrichment.py -k no_match_and_outage_have_distinct_terminal_diagnostics -v
~~~

Expected: 当前 enrichment 只有部分失败事件，缺少对应 OA 调用终态/正常 no_match 分类，目标断言 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
Unpaywall:
    在自身 response extractor 中把已知 404 分类为 no_match
    原 HttpStatusFailure(404) -> None 保持
    200 的 OA envelope/候选 location 只加安全分类与计数
    非 404 仍按原策略传播

enrich_paper_records:
    仅在 doi 不在 cache、确实调用 resolver 时记录 started
    resolve 返回值后 completed（resolved 或 no_match）
    出错时一次 provider_failed，加原 paper_enrichment_failed
    cache[doi] = None 并保留论文的原策略不变
~~~

底层 HTTP 的 http_failed/status=404 可以保留，它与业务层正常无匹配不是同一层结果。resolver adapter 不再额外发一套 provider 终态。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/academic/test_unpaywall.py tests/academic/test_oa_enrichment.py tests/orchestrators/test_paper_search_pipeline.py tests/runtime/test_academic_runtime_assembly.py -v
~~~

Expected: 404、outage、缓存、论文保留和安全归属字段全部通过；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

复用失败字段 helper；不改变 OA cache key 或 aggregator，不把每个重复 DOI 的缓存命中当新 provider 调用。重构后复跑上述组及全量测试。


### Task 16: LLM 协议重试和流水线终态分开

**Files:**

- Modify: `src/agent_search_gateway/providers/llm/openai_chat.py`。
- Modify: `src/agent_search_gateway/llm/stages.py:152-302`（只补依赖失败诊断，不重构职责）。
- Test: `tests/providers/llm/test_openai_chat.py`。
- Test: `tests/orchestrators/test_llm_search.py`、`tests/orchestrators/test_scoped_llm_search.py`。
- Test: `tests/unit/test_llm_stages.py`、`tests/unit/test_llm_judge_stage.py`。

- [ ] **Step 1: Write the failing test**

通过真实 OpenAIChatCompletionsClient、HTTP executor 和搜索 orchestrator，新增 `test_protocol_retry_has_one_provider_terminal_outcome`：

- 第一次 200 的 response choices/content 无效，第二次合法。
- 期待一次 provider_retrying，next_attempt 正确，最终只有一个由搜索 pipeline 发出的 provider_completed，零 provider_failed。
- 两次协议都坏时，有 llm_stage_failed 和一个 pipeline provider_failed，不存在底层提前成功/失败终态。
- HTTP 200 的已知 OpenAI error object 可保留 error.code/type/message；公共失败类别及协议重试政策仍与当前代码一致。
- complete_json 的内容 JSON 非法、Judge 不符合约定、clean/safety/focus 失败都有安全 stage/category，完整 answer 不进日志。

更新当前把 protocol_retry 认作 provider_failed 的测试，不能仅把旧字符串替换后不验证完整调用链。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/providers/llm/test_openai_chat.py tests/orchestrators/test_llm_search.py -k protocol_retry_has_one_provider_terminal_outcome -v
~~~

Expected: 当前协议重试包含错误 provider_failed，成功还可能有底层与外层两条 provider_completed，因此 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
OpenAI client:
    _log_protocol_attempt -> llm_protocol_attempt_started
    _log_protocol_retry -> provider_retrying
        attempt / next_attempt / retry_scope=protocol / delay_ms
    _log_protocol_completed -> llm_protocol_completed
    _request_once 传本协议的 response_diagnostics
    _extract_content / complete_json 保留当前 ProtocolFailure 和重试次数
    已知 API 错误只附安全 diagnostics，不改变公共 message

LLMStages:
    保留现有 llm_stage_started/completed/failed
    _stage_failed 用公共失败字段 helper 补诊断
    无原始 completion / prompt / arbitrary exception message
~~~

底层 protocol success 不代表严格 Markdown/Judge 校验通过；终态仍在原 pipeline 完整完成后发。此处不添加整次 Agent 重试。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/llm/test_openai_chat.py tests/orchestrators/test_llm_search.py tests/orchestrators/test_scoped_llm_search.py tests/unit/test_llm_stages.py tests/unit/test_llm_judge_stage.py tests/unit/test_llm_paper_stage.py -v
~~~

Expected: 协议 retry 事件与终态数量正确，模型选择/extra_body/JSON/text/分支部分成功保持；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

复用两个 complete_* 的已有 metadata helper，不为此次补丁拆分 LLMStages 或改 LLMClient protocol。重构后跑上述组及全量测试。

### Task 17: Agent 进程/终态诊断，保留全部生命周期保护

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`。
- Modify: `src/agent_search_gateway/runtime.py:362-381`。
- Test: `tests/providers/agent/test_antigravity_cli.py`。
- Test: `tests/providers/agent/test_antigravity_lifecycle.py`。
- Test: `tests/observability/test_agent_event_logging.py`。
- Test: `tests/runtime/test_agent_runtime_assembly.py`、`tests/daemon/test_agent_shutdown.py`。
- Reference: `tests/support/agent_cli.py`、`tests/support/agent_search.py`。

- [ ] **Step 1: Write the failing test**

`test_agent_terminal_diagnostic_preserves_safe_status_and_byte_counts` 使用 write_fake_cli：

- exit=0，terminal status=ERROR，error 是明确包含 code/message 的 object，response/stderr 中有不同 sentinel。
- 日志能看到配置 alias、model、agent_process stage、ERROR、允许的 code/短消息、exit_code、elapsed_ms、真实 stdout/stderr 字节数；不见 response、prompt、stderr、环境变量值。
- 对未知 error 字符串/嵌套对象保守只记结构类别；现有 ERROR_SENTINEL 不泄漏断言继续成立。
- 逐次覆盖 SUCCESS、非零退出、无/坏 JSON、缺 response、空 response、timeout、output limit、spawn failure、取消、成功后 cleanup failure。
- 特别断言 cleanup 出错时不先记录“进程成功”再让外层失败；provider terminal 仍由搜索分支记录一次。

补回归而非假 RED：3600/7200 正确传为 --print-timeout，并由配置值+5 的 Python deadline 管理；用受控进程/虚拟 clock，不实际运行一小时。保留 descendant、pipe、重复 cancellation 和 quota 释放测试。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/observability/test_agent_event_logging.py -k terminal_diagnostic_preserves_safe_status_and_byte_counts -v
~~~

Expected: 当前只见外层安全 reason，缺终态 object/exit/字节诊断，目标断言 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
run_antigravity 新增必需的 keyword-only provider_name: str
Runtime._build_agent_runners 的 partial 绑定 provider.name
对所有直接调用 run_antigravity 的测试同步该参数
AgentTextRunner 仍是 (model, messages) -> Awaitable[str]

每次调用:
    局部保存进程起始时间、状态与 stdout/stderr 字节计数器
    _read_stream 继续返回 bytes；stderr capture=False 仍不存正文
    计数在读 chunk 时更新，包括超限时已观察的字节
    终态 JSON 解析只选允许的 metadata；不输出原 envelope
    保留 nonzero_exit 优先于 JSON/status 的既有错误语义
    完成原 shielded cleanup 后输出一次安全进程诊断
    失败附内部 diagnostics，外层记录唯一 provider 终态
~~~

计数器不改变 cleanup task 的 bytes/int 返回类型。timeout/cancel/spawn 在没有某项事实时省略字段，不能伪造 returncode/terminal status。不得移动 quota 计时起点、去掉 +5、放宽输出上限、取消 start_new_session 或降低清理等待要求。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/providers/agent tests/observability/test_agent_event_logging.py tests/runtime/test_agent_runtime_assembly.py tests/daemon/test_agent_shutdown.py tests/orchestrators/test_agent_search_deadline.py -v
uv run --offline mypy src tests scripts
~~~

Expected: 新诊断正确且无 payload；长配置预算、超时、双取消、pipe/descendant 清理和 quota 释放通过；别名参数与 partial 类型一致；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

不为日志保存 stderr 字节串；最多新增小型局部字节计数对象。终态分类和真实退出流程共用解析结果，避免二次解释/改变错误优先级。重构后重跑完整 Agent 生命周期组及全量测试。

### Task 18: 所有流水线终态统一携带安全原因，端到端验证计数与 fallback

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:466-797`。
- Modify: `src/agent_search_gateway/orchestrators/paper.py:120-189`。
- Modify: `src/agent_search_gateway/scheduler/fetch.py:96-273`。
- Test: `tests/observability/test_provider_event_logging.py`。
- Test: `tests/orchestrators/test_keyword_search_state.py`、`tests/orchestrators/test_paper_search_pipeline.py`。
- Test: `tests/scheduler/test_fetch_outcomes.py`、`tests/orchestrators/test_url_fetch_flow.py`。
- Test: `tests/acceptance/test_debug_and_doctor.py`。
- Reference: `src/agent_search_gateway/daemon.py`（现有 workflow 所有权，不计划重写）。

- [ ] **Step 1: Write the failing test**

`test_provider_terminal_event_preserves_upstream_code_after_retries` 走真实 executor + 真实 adapter + orchestrator，设置：

- A：429 → 502 → 200；2 retry、3 response diagnostic、1 completed、0 failed。
- B：耗尽失败；1 http_failed、1 provider_failed，并且 terminal provider_failed 也有上游 code。
- C：10 项中 5 正常、3 missing_url、2 invalid_record；仅一次对应 adapter summary，结果保留 5 条。
- workflow 成功并输出 A/C；不能因为 B 失败出现 workflow_failed。

再逐次测试：Fetch 第一家执行失败后下一家成功、全执行失败、语义拒绝后 fallback、Judge execution failure 原样传播；同一 LLM/Agent 的 web/paper 分支或重复 invocation 各计一次；两个并发 request 的日志关联与计数不交叉。

新 RED 首先针对**外层 terminal 缺失 upstream_code**，已在前面任务通过的 retry/candidate 计数作为集成回归。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/observability/test_provider_event_logging.py -k terminal_event_preserves_upstream_code_after_retries -v
~~~

Expected: 内层 diagnostics 已存在，但原 _log_provider_failure 仅含 error_type，terminal upstream code 断言 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
Search/Paper/Fetch 的终态日志:
    使用 diagnostic_fields_from_failure
    保留 owner 的 provider/stage/model/elapsed/reason
    只合并不冲突的安全 upstream/transport 字段
    不把任何 attempt 事件升级为 provider terminal

关键词 Judge 部分失败:
    原 failed_hits / provider_partial_failure 保留
    有成功兄弟 hit -> provider_completed 一次
    无成功 hit 且有执行失败 -> provider_failed 一次

Fetch:
    adapter 失败 -> failed 一次 + 原 fallback
    语义拒绝 -> completed(outcome=semantic_failure) + body_rejected + 原 fallback
    Judge 失败 -> 一个 fetch failed，含 failed_stage/dependency；照旧传播
~~~

如包装 ExecutionFailure（例如 Fetch Judge 分支），显式复制安全 diagnostics，保留原 code/message/reason；不能只复制 message 后丢失字段。对已经发出 provider_started、尚未发终态的取消分支，所有者记录一次 provider_failed(reason=cancelled)，继续 raise；不进入 fallback，不吞取消。普通失败的 reason 只使用已知稳定分类，未知外部 reason 用 execution_failure，不直接输出任意字符串。

不要通过按 provider 名去重日志“修复”重复终态；改正每个调用栈的事件所有权。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/observability tests/orchestrators tests/scheduler tests/academic/test_oa_enrichment.py tests/acceptance/test_debug_and_doctor.py -v
~~~

Expected: 每个实际 provider branch 恰有一个终态，A/C 保留、B 不污染 workflow，fallback/Judge 策略不变，两个并发 request 各自准确；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

只合并重复字段拼装；删除重复终态发射点，不重写调度。用 DEBUG on/off、文件轮转和 stdout/stderr 的现有验收确认变化局限于诊断。重构后复跑上述组及全量测试。


### Task 19: 实现精确 Google goto 解包，并接入关键词入口

**Files:**

- Create: `src/agent_search_gateway/search_result_urls.py`。
- Modify: `src/agent_search_gateway/orchestrators/search.py:671-706`。
- Create: `tests/unit/test_search_result_urls.py`。
- Test: `tests/orchestrators/test_keyword_search_pipeline.py`。
- Reference: `src/agent_search_gateway/url_normalization.py`（不修改 normalize_url）。

- [ ] **Step 1: Write the failing test**

第一个 RED 通过现有真实关键词 pipeline 新增 `test_keyword_hit_uses_google_goto_destination`：provider 返回合法 goto URL，断言写出的 URL 是 example.org 的目标地址，而不是 Google wrapper。此用例不依赖尚不存在的新模块，失败直接表达业务缺陷。

观察该 RED 后实现最小 helper 与入口；再逐次新增纯 helper 的表驱动行为，每次都走 RED/GREEN：

| 输入/边界 | 期望 |
| --- | --- |
| www.google.com 与 google.com，/goto，单个编码 url | 返回 HTTP(S) 目标 |
| 普通直接 URL；其他域；其他路径；/url?q= | 返回输入原字符串 |
| google.com.evil.test 或 google.com@evil.test | 不认作 Google；沿用普通 URL 处理 |
| 可信 Google /goto 带 userinfo | 不信任该 wrapper，recognized-invalid；不解包 |
| url 缺失、空、重复（含编码键重复） | recognized-invalid |
| javascript/file/相对目标、无 host、非法 port | recognized-invalid |
| Unicode、已编码 query 分隔符、%252F、%2B | 仅解码外层一次，保留目标本身编码/含义 |
| 错误百分号转义、无效 UTF-8、控制字符 | recognized-invalid |
| 目标本身又是 goto | 只解外层，不递归 |
| 签名目标 URL | helper 保留目标 query；日志测试另行确保 query 不输出 |

普通未知 URL 的非法格式继续由现有 normalize_url 处理，本 helper 不重新定义所有 URL 的合法性。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/orchestrators/test_keyword_search_pipeline.py -k keyword_hit_uses_google_goto_destination -v
~~~

Expected: FAIL，当前结果仍是 google.com/goto。纯 helper 后续用例需因对应解析规则缺失而失败，不以新文件 ImportError 充当首次 RED。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
unwrap_search_result_url(raw):
    只将绝对 HTTP(S)、精确 hostname、精确 /goto 作为已知规则
    未匹配 -> 返回 raw
    对已识别 wrapper 检查原始控制字符，不能被 urlsplit 静默剥除
    拒绝 userinfo / 无效 port 等不可信 wrapper
    检查 query 百分号转义，严格 UTF-8 解码
    保留重复参数地解析 query；恰好一个非空 url
    校验目标无控制字符、HTTP(S)、host 与合法 port/转义
    返回解码一次的目标；不再 unquote，不请求网络，不递归
    识别后验证失败 -> InvalidSearchResultRedirect(固定原因)

_stage_keyword_hit:
    字段类型检查后、任何 normalize_url 前调用 helper
    仅 catch InvalidSearchResultRedirect:
        candidate_rejected(reason=recognized_invalid_redirect)，不含 raw URL
        return None
    用返回值继续现有 abstract / normalize / Judge / store 流程
~~~

匹配只看精确 host/path，不用 endswith("google.com") 或字符串 contains。不要把新规则放入每个 adapter、normalize_url 或 url-fetch。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/unit/test_search_result_urls.py tests/unit/test_url_normalization.py tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py -v
~~~

Expected: GU-01–10、13–15 的纯解析边界通过，入口业务用例通过；原全局 normalize_url 行为完全保持；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

只保留一个纯 helper 与一个本地异常。去掉重复 unquote 和任何规则表/网络 resolver；确认对未知 URL 不新增处理。复跑纯 URL、关键词组及全量测试。

### Task 20: 关键词 staging 计数、跨提供商去重和后续 Fetch 验收

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:594-797`。
- Test: `tests/orchestrators/test_keyword_search_pipeline.py`。
- Test: `tests/orchestrators/test_keyword_search_state.py`。
- Test: `tests/orchestrators/test_url_fetch_admission.py`。
- Test: `tests/acceptance/test_gateway_workflows.py`、`tests/acceptance/test_debug_and_doctor.py`。
- Reference: `src/agent_search_gateway/url_store.py`、`src/agent_search_gateway/result_writer.py`（不改变 schema）。

- [ ] **Step 1: Write the failing test**

`test_keyword_staging_reports_redirect_rewrites_and_rejections`：同一 provider 返回两个有效 goto、一个已识别但无效 goto、一个直接链接，检查 phase=staging 的 received/accepted/rejected/rewritten 与 recognized_invalid_redirect 数量；不能把一个坏 wrapper 算作 provider_failed。

增加/保留集成回归：

- 两个不同 provider（至少 Bright Data 与 ScraperAPI/Scrape.do 的真实 adapter fixture）提供 wrapper 和同目标直接链接；JSONL 去重为一个目标，URLStore 只有目标身份，正确合并已有摘要/body。
- 后续 url-fetch 对目标 URL 准入成功，传给真实 FetchScheduler 的是目标；旧 wrapper 不自动迁移/新增准入。
- 在带 signature/token query 的目标上，上述 candidate_accepted/rejected、body 决策、dedup 和 Fetch 日志不泄漏 query，但结果/存储 URL 保留原有语义。
- 未知 /url?q= 保持原样；LLM/Agent/paper 的 URL 不进入这个规则。
- helper 无网络请求；HTTP MockTransport 只收到原 provider/Fetch 请求次数，没有额外解析重定向请求。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/orchestrators/test_keyword_search_pipeline.py -k keyword_staging_reports_redirect_rewrites_and_rejections -v
~~~

Expected: 解包已由 Task 19 工作，但 staging 还缺 summary/rewritten 统计，因此 FAIL。已经通过的去重行为作为回归，不称作新的 RED。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
_run_keyword_pipeline:
    针对返回 hits 建立本次 phase=staging 的 CandidateCounts
    传入 _stage_keyword_hit(..., provider=..., counts=...)
    staged_hit 非 None -> accepted
    Judge 执行错误 -> 固定 reason 计数；保留既有 partial-failure 策略
    结束/已知错误分支输出准确 summary

_stage_keyword_hit:
    helper 返回值与原 URL 不同 -> rewritten += 1
    recognized-invalid -> rejection_counts 相应累加
    empty_abstract 等原拒绝 -> 对应计数
    abstract 保留但 body 被拒绝 -> 仍按原逻辑作为 accepted hit
~~~

新的 counts 参数只在该私有方法与唯一生产调用点之间传递；它不改变 _StagedKeyword、KeywordSearchHit、公开 provider interface 或结果类型。严格路径若途中终止，显式区分 processed/unprocessed。URLStore 的准入/跨 provider 去重逻辑继续使用现有实现。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/unit/test_search_result_urls.py tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_url_fetch_admission.py tests/orchestrators/test_url_fetch_singleflight.py tests/acceptance/test_gateway_workflows.py tests/acceptance/test_debug_and_doctor.py -v
~~~

Expected: 精确 rewrite/rejection 计数通过，两个 provider 的目标合并与 Fetch 准入正确，无新增网络或内容泄漏；再运行全量测试。

- [ ] **Step 5: Refactor (keep tests green)**

删除重复 URL 解析和不必要的原 URL 日志变量；修正 empty_abstract 原分支先 normalize 的顺序。不要让 adapter_count 与 staging_count 共用实例或互相覆盖。重构后复跑上述组及全量测试。

### Task 21: 更新当前文档与补丁说明

**Files:**

- Modify: `README.md:228-300` 以及现有 DEBUG/关键词使用说明。
- Modify: `config.example.toml`（Agent timeout 注释，示例默认值仍可为 300）。
- Modify: `docs/public-interface.md`。
- Modify: `CHANGELOG.md` 的 Unreleased。
- Test: `tests/docs/test_documented_config.py`。
- Test: `tests/docs/test_open_source_contract.py`、`tests/doctor/test_config.py`。
- Reference: `CONTRIBUTING.md`、`scripts/build_docs.py`。

- [ ] **Step 1: Write the failing test**

扩展现有文档契约测试 `test_documented_agent_timeout_policy_matches_runtime`：文档的当前 Agent timeout 表不能宣称 540 配置上限/1140 工作流/1200 socket；对应示例配置能用 7200 覆盖并通过真实 resolver。非 Agent socket 的说明与已存在的 600 秒实现一致。

检查的是实际数字与约束，不逐字锁定整段中文/英文文案。历史 specs/plans 不是“当前用户说明”，不能全库粗暴禁止出现这些数字。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run --offline pytest tests/docs/test_documented_config.py -k documented_agent_timeout_policy_matches_runtime -v
~~~

Expected: README 当前仍包含过时 Agent 预算，且非 Agent 错写为 300，文档约束断言 FAIL。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
README/config/public-interface:
    Agent timeout finite positive、无 Gateway 上限、默认 300
    每次进程仍由 agy timeout 与 Python +5 保护
    quota 等待/两个 scope/OA 的总耗时不等于一个进程预算
    Agent socket 默认无固定 deadline，可取消/断连结束
    说明外部 agy 对极端时长的支持不由 Gateway 保证
    非 Agent 仍 600
    stdout/stderr 的正常命令契约不变
    DEBUG 的 attempts / responses / retry / provider terminal / workflow 区别
    成功记录 metadata/count，失败保留安全短错误；不存正文
    Google goto 只在关键词结果入口解一次，未知格式保持

CHANGELOG Unreleased:
    分别说明三项修复
    不修改 package version、tag 或发布资产
~~~

移除 README “whole-workflow timeout 会取消并不写部分结果”的过期句子，替换为配置的单次 deadline 与取消行为的准确说明。保留 OS 阻塞 spawn、escaped descendants 等现有生命周期限制。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
uv run --offline pytest tests/docs tests/doctor/test_config.py -v
uv run --offline python scripts/build_docs.py
~~~

Expected: 文档/config 契约与生成文档检查通过；再运行全量测试。若 build_docs 产生跟踪文件，按脚本现有约定审阅输出，不把工具生成内容误当作无关手工改动。

- [ ] **Step 5: Refactor (keep tests green)**

用当前公开说明集中解释政策，不修改历史设计记录来掩盖原行为。检查 README、config 和 public-interface 没有互相矛盾。重构/修订后复跑 docs/doctor 和全量测试。

## 5. 最终实施验收门禁（仅验证，不伪造新的 RED）

以下是实施完成时必须执行的门禁；本次写计划没有执行生产测试，也没有运行真实提供商。

~~~bash
uv run --offline ruff format --check src tests scripts
uv run --offline ruff check .
uv run --offline mypy src tests scripts
uv run --offline pytest -v
uv run --offline python scripts/build_docs.py
~~~

记录各命令的 exit code、结果摘要及任何预先存在的失败。实现若没有 packaging 变化，不为此额外 uv build 或发布版本。

人工检查代表性的 DEBUG 序列：

1. HTTP 429 → 502 → 200：每响应元信息完整，两次 retry，一次 branch completed。
2. 最终失败：一个 http_failed 加一个 provider_failed；两者是不同层，不是重复最终提供商失败。
3. LLM 协议失败后恢复：provider_retrying；底层完成叫 llm_protocol_completed；搜索 branch completed 只有一次。
4. Agent ERROR / timeout / cleanup failure：安全状态、字节数与分类，provider_failed 一次，无 prompt/stdout/stderr。
5. 10→5 的部分坏结果：准确原因字典；适配器解析和 staging 的计数分开。
6. Unpaywall 404：HTTP 404 可以出现；业务 completed/no_match，不产生 provider_failed 或误报 workflow 失败。
7. Fetch：全部执行失败保持原执行错误；语义失败保持原 unavailable/fallback；Judge 执行失败不被吞为普通 body rejection。
8. goto：已识别坏 wrapper 仅丢本条；跨 provider 目标去重、准入和后续 Fetch 正确；未知链接不改。
9. 对**完整序列化日志**做 sentinel 检查；同时检查 stderr sink 与 rotating file，不只检查 caplog 选中的几个字段。
10. 并发 request、同 alias 不同 model/scope、重复 invocation 的关联与数量正确；默认非 DEBUG 输出没有新增诊断。

真实 smoke 不作为强制门禁。若离线测试留下具体的外部协议疑点，再按用户已授权环境与精确 `--provider` 做少量 smoke；不遍历所有付费 provider，不等待 7200 秒，不把 agy 首字慢认定为网络故障。真实 smoke 使用恢复代理的单条命令，日志只保存安全摘要；不得把密钥/原始响应/搜索隐私写入计划或提交。

## 6. Spec 覆盖矩阵

| Spec 要求 / 测试 ID | 任务或门禁 | 核心证据 |
| --- | --- | --- |
| AT-01–04 | 1 | 默认/任意可表示有限正数；非法值拒绝 |
| AT-05 | 2 | 虚拟 1141 秒、quota/分支/OA 无旧整体中断 |
| AT-06、10–12 | 3 | 虚拟 1201 秒真实 socket、override、非 Agent 600、断连 |
| AT-07–09 | 2、17 | 每次配置 deadline、+5、超时/取消/清理与部分成功 |
| HT-01–06、09–12 | 4、5 | 每个响应 metadata，旧 retry/backoff，decode/unknown/large/transport |
| HT-07–08 | 7、9、13、16 | 200 业务错误、Bright Data 内外状态、真实安全消息 |
| PD-01–03 | 6–11 | 全部 15 家 search adapter 的 mixed/empty/all-invalid 与特殊 envelope |
| PD-04 | 13–14 | JSON/XML 学术候选与 parser 诊断 |
| PD-05 | 15 | 404 正常无匹配与 500/协议失败、缓存、论文保留 |
| PD-06、09 | 16、18 | LLM protocol retry、依赖 stage、唯一终态 |
| PD-07 | 17–18 | Agent 状态、exit/bytes/timeout/cancel、无 payload |
| PD-08 | 12、18 | 全部 15 个 Fetch 后端及执行/语义/Judge 分支 |
| 事件层级、唯一终态、部分成功 | 5、15–18 | 三 provider 组合、scope/重复 invocation、workflow 成功 |
| LG-01–07、10–12 | 4–5、7、12、16–18、20 | 白名单、512/128、先脱敏再截断、未知字段/异常安全 |
| LG-08–09 | 18、最终门禁 | DEBUG/rotation/stdout 与并发 request-id 关联 |
| GU-01–10、13–15 | 19 | 精确匹配、严格单次解码、非法目标、签名 query |
| GU-11–12 | 20 | 跨 provider 去重、单条拒绝、rewrite/rejection 计数 |
| URL helper 零网络/global normalization 不变 | 19–20 | 纯 helper、调用次数、既有 normalize/fetch 回归 |
| CLI、协议、结果、选择器兼容 | 3、18、20、最终门禁 | 现有 acceptance/filtering/protocol/JSONL 测试 |
| 当前文档与执行命令 | 21、最终门禁 | README/config/public-interface/CHANGELOG + CI checks |

## 7. 计划自审与交接

计划编写阶段完成的自审：

- [x] 阅读并逐段覆盖三份 20261113 spec，以上矩阵没有遗漏的需求。
- [x] 以 f431d10 工作树实际路径/符号为依据；timeout 三处、HTTP inspector、LLM 重复终态、URL staging、论文/OA、Fetch 均已核对。
- [x] HTTP 新参数统一叫 response_diagnostics，类型为 ResponseDiagnosticExtractor；request_json/request_text、Web/Academic Protocol 与 Recording* fake 同步。
- [x] 失败内部字段统一叫 diagnostics，ExecutionFailure / ProtocolFailure / ProviderResponseFailure / HttpStatusFailure 沿同一链路传递；不产生第二套公共错误 schema。
- [x] AgentTextRunner 返回类型不变；新增 provider_name 只在 run_antigravity keyword 参数、runtime partial 和直接调用测试之间同步。
- [x] _read_stream 继续返回 bytes，现有 cleanup task 类型不用改成新的返回对象；stderr 仅新增计数。
- [x] URL helper 返回 str 或本地 InvalidSearchResultRedirect；调用位置先于 normalize_url；只识别规则内无效 wrapper，不扩张到所有 URL。
- [x] CandidateCounts 的对象是单次调用局部；phase、已处理/未处理、partial success 与零结果的计数语义一致。
- [x] 对日志 formatter 先脱敏后截断、受控计数字典和现有保留 query 的日志 helper 已安排必要任务，避免遗漏泄漏路径。
- [x] 去重的是事件所有权，不是 provider 名；scope/all、重复 invocation、并发请求都有具体测试。
- [x] 历史 specs/plans 保留为历史；只有当前用户文档和 Unreleased 更新。
- [x] 实施任务的第一个 RED 从已有业务入口暴露缺陷；已经通过的保护用例明确归为回归，不声称全部新测试都应失败。
- [x] 计划只描述实现与验收，未实施代码、未运行付费调用、未把计划中的预期结果写成实际测试结果。

实施者按 Task 1–21 顺序执行。发生与 spec 冲突的实际新证据时记录具体差异，先做不受影响部分；不要静默放宽安全规则或重试策略。完成各步骤后记录真实验证结果，再进入用户后续指定的实现审查流程。
