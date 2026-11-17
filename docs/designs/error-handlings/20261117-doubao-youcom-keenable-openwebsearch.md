# Error Handling: Doubao、You.com、Keenable 与 open-webSearch

- 日期：2026-11-17
- 状态：**设计待审阅，未实施**
- 关联：[架构](../architectures/20261117-doubao-youcom-keenable-openwebsearch.md) / [测试](../testings/20261117-doubao-youcom-keenable-openwebsearch.md)

## 1. Goals & Non-goals

### Goals

1. 区分 **HTTP 失败、HTTP 200 内业务失败、解析失败、结果局部损坏、空网页正文**，避免把供应商失败报告成成功/空结果。
2. 保持当前统一的重试、超时、错误事件、终态和 Fetch Provider 回退机制。
3. 上游出错时保留足够的可定位信息（供应商、阶段、状态、短错误码、类别、重试尝试、候选条目统计），同时保护凭据、查询及网页内容。
4. 不因为某一个 Provider 失败而改变其他 Provider 的调用路径。

### Non-goals

- 不实现新重试引擎、无限重试或独立熔断器。
- 不调整别的供应商已有的日志或错误语义。
- 不因本次新增提供商而默认全文保存供应商响应或请求。
- 不引入私有领域异常种类、新的对外 `ErrorCode` 或异步守护健康检查。

## 2. Error Taxonomy & Handling Matrix

| 场景 | 检测位置 | 默认重试 | 最终处理与诊断 |
| --- | --- | --- | --- |
| TCP/DNS/连接拒绝、连接断开、请求超时 | 共享 `HttpJsonExecutor` | 是，受全局 `RetryPolicy` 限制 | 终态 `ExecutionFailure`；`category=transport`、异常类型、尝试次数 |
| HTTP 408、429、5xx | 共享 `HttpJsonExecutor` | 是，受全局配置限制 | 失败时保留 `http_status`，Scheduler 可继续其他 Provider |
| HTTP 400/401/402/403/404/422 等 | 共享 `HttpJsonExecutor` | 否 | HTTP 失败；401/403 提示凭据/权限，402 可能是额度，422 可能是请求/提取问题，不重试 |
| HTTP 200、JSON 有明确业务错误 | 各适配器的 `response_inspector` + 安全诊断 | **仅明确可恢复的错误码**；其他不重试 | `ProviderResponseFailure`；保留经白名单限制的上游 code/message |
| HTTP 200、响应体空或非法 JSON | `request_json` | 否（沿用现有行为） | `ProtocolFailure`；`category=decode`、长度等，无原始 body |
| HTTP 200、顶层 envelope/必需列表类型错误 | 适配器 parser | 否 | `ExecutionFailure`；`category=invalid_envelope` |
| 合法搜索 envelope，少数结果 URL 缺失/类型坏 | 适配器逐条解析 | 不适用 | 跳过坏条、保留好条；`provider_candidates_summary` 统计 |
| 合法搜索 envelope，真实零结果 | 适配器 parser | 不适用 | 返回 `[]`，与错误信封区分 |
| 成功响应内 Fetch 正文缺失或仅空白 | 适配器 Fetch parser | 否（没有证据表明重发可修复） | `ExecutionFailure`；`semantic_result=empty`；Scheduler 尝试下一家 |
| Fetch 候选文本未通过 cheap_check 或 LLM judge | 现有 `FetchScheduler` | 不在 HTTP 层重试 | 既有候选拒绝和下一 Provider 逻辑 |
| 任务取消或 Runtime 退出 | 既有异步边界 | 不自动重新发起 | 按现有取消/关闭路径传播，不吞掉取消 |

**重要基线事实：** 当前 `HttpJsonExecutor` 对 408/429/5xx 使用全局重试，但**没有通用的 `Retry-After` 响应头解析**；本次不会把“尊重 Retry-After”写成已经实现的能力。后续若需要此特性，应单独在共享 executor 中设计并测试，不在四个适配器里各写一份。

## 3. Provider-Specific Classification

### 3.1 Doubao

| 上游形态 | 处理 |
| --- | --- |
| `ResponseMetadata.Error` 非空 | 视为供应商失败，不能仅凭 HTTP 200 判定成功 |
| 明确的频率限制码（例如 `700429`） | 经 `response_inspector` 转成 `ProviderResponseFailure(retryable=True)`，沿用共享重试次数 |
| 明确的临时内部错误码（例如 `10500`） | 官方文档/fixture 证实可恢复才重试 |
| 参数错误（例如 `10400`）、凭据问题或未知错误 | 不重试；归因于上游业务失败 |
| Search 中 `Result.WebResults` 非数组/缺失 | 非法 envelope，失败 |
| Fetch 中 `Result.Data.ContentText` 空/缺失 | Fetch 失败并允许调度器回退 |
| Fetch 返回原站 `StatusCode` 非成功 | 依据官方字段语义判定为抓取失败；**不能**因 HTTP 200 的外层传输成功而伪造内容 |
| Fetch 使用订阅型 Key 不支持该 API | 明确配置/权限失败；README 提示需按量付费 Key，无盲目重试 |

设计要点：供应商级代码与 HTTP 状态不是同一个概念。只对**文档证实**的临时错误设置可重试；不把所有非空 `ResponseMetadata.Error` 都当作可重试。

### 3.2 You.com

- Search：顶层或嵌套 `results.web` 不符合预期类型时失败；`results.web=[]` 是合法空结果。单个 `url` 或 `contents.snippets` 类型损坏的结果按单条拒绝，其他结果保留。
- Contents Fetch：只接受与请求目标匹配的返回记录；`markdown` 字段缺失、为空、只含空白或不是字符串时为抓取失败，**不允许回退到 `html` 冒充已经要求的 Markdown**。
- JSON 中出现明确官方错误 envelope 时不伪装成功；HTTP 401/403/402/429/5xx 继续由共享 executor 分类。
- Search 不主动请求更高成本的 `extraction`；Fetch 是单独计费能力，错误诊断不暴露付费 Key。

### 3.3 Keenable

- 仅处理正式 keyed `/v1/search` 与 `/v1/fetch`，不因鉴权失败自动切换到免 Key 公共接口。
- HTTP 402（信用额度不足）不重试；HTTP 429 走已有退避；HTTP 422（提取/参数失败）不重试。
- Fetch 的 `content` 必须是非空 Markdown 文本，不能把错误字段、标题或消息当成正文。
- 默认 indexed/live 策略由上游决定；如果上游选择了不可用的抓取模式，本 Provider 按正常失败向调度器返回，Gateway 不试图改写 `live` 参数补救。
- Search 结果的列表字段应以官方文档及固定 fixture 为准；字段不符合要求报 schema 错误，不能无限级别猜测字段路径。

### 3.4 open-webSearch

- `status="ok"` 才表示成功；`status="error"`（即使 HTTP 200）也必须检测并报告。
- 当响应有 `error.code` / `error.message` 时，日志只能保留共享安全白名单允许的受限字段；`hint`、原始 engine 信息、任意堆栈不能原样落日志。
- 只有文档支持且明确属于临时问题的状态/码允许走 `response_inspector` 的 retryable 路径；未知业务错误默认不可重试。
- TCP 连接拒绝通常说明 daemon 没运行，HTTP executor 可能按全局短重试后终态失败；**Gateway 不负责启动、安装或重启 daemon**。
- open-webSearch 可能因 Playwright/浏览器组件缺失而无法抓取某些站点；这属于上游部署环境问题，不能由 Gateway 静默安装依赖。
- 若错误来自 daemon 内部选用的某搜索引擎，Gateway 只标记 `provider=open_websearch`，不把引擎拆成新的 Provider，也不自动切换/设置 `engines`。
- `/fetch-web` 的 `data.content` 须为非空正文；其他信息如标题/最终 URL 只作适配器内部校验或安全元数据，不替代正文。

## 4. Shared Retry Contract

1. 全局 `retry.max_attempts`、`base_delay_seconds`、`max_delay_seconds`、`request_timeout_seconds` 不因新增 Provider 自动改变。
2. HTTP 408/429/5xx、`httpx.TimeoutException`、`httpx.TransportError` 由 `HttpJsonExecutor` 决定是否重试。
3. 仅对**HTTP 200 / 已知供应商 envelope** 用 `ResponseInspector` 返回带安全原因、诊断和 `retryable` 布尔值的 `ProviderResponseFailure`；不复制新的 retry loop。
4. Inspector 无法解析 JSON 时允许共享 `request_json` 的 JSON decode 处理报告 `ProtocolFailure`；不可把 decode 错误转成零结果。
5. ProviderSearch/Fetch parser 在成功 HTTP 响应的字段验证期间抛出现有 `ExecutionFailure`，由调用方/调度器按现有失败分支处理。
6. 不改变现有全局错误码映射；没有依据时优先**不可重试**，以免对付费 Fetch 或已被禁用的 API 重复扣费。

## 5. Observability & Redaction

### 必要事件与归因

- `http_attempt_started`：请求开始，含 provider、stage、尝试序号。
- `http_attempt_completed`：HTTP 尝试结束，记录状态、耗时、响应长度等允许的技术标量。
- `provider_response_diagnostic`：上游响应成功/业务失败/非法 envelope 分类。
- `http_retrying`：每次安排重试，说明技术类别与尝试次数。
- `http_failed`：HTTP 级终态，且不额外复制同一事件。
- `provider_candidates_summary`：Search 条目数、接受数、拒绝数与安全原因分类。
- 抓取解析失败：借助现有 `parse_fetch_with_diagnostics` / `fetch_response_diagnostics`（适用时）保留 `invalid_envelope`、`empty` 等诊断。

### 安全范围

允许在通过 `safe_diagnostic_fields`/固定白名单后写入：

- `provider`、`stage`、`category`、`semantic_result`、`response_kind`；
- `http_status`、`response_bytes`、`attempt`、`elapsed_ms`、`retryable`；
- 受限长度/字符集的上游错误码/消息，候选统计。

明确禁止日志中出现：

- 环境变量对应的凭据值、认证头、Token、Cookie、完整请求/响应 body；
- 原始搜索查询、完整网页 Markdown/HTML、全文、可能含敏感参数的完整 URL；
- 外部 JSON 的任意自由字段（例如 `hint`、错误堆栈、HTML 错误页面）。

若供应商错误文本可能包含用户查询或认证信息，应只输出安全归类/代码，而不是盲目信任并记录消息。测试要有专门的脱敏断言。

## 6. Edge Cases and Fallbacks

1. **单条坏结果**：搜索 10 条中 2 条缺 URL 时保留 8 条，记 `accepted=8`、`rejected=2`，不让 2 条坏记录毁掉整个响应。
2. **全部条目坏掉**：顶层 envelope 合法但所有结果无效，返回空命中与拒绝计数；是否整体视为搜索失败由现有 SearchOrchestrator 规则决定，适配器不隐藏计数。
3. **有效但空搜索列表**：不当作协议错误，返回空列表且不重试。
4. **HTTP 200 + 空响应体**：`ProtocolFailure` 而非“成功、无数据”。
5. **HTTP 200 + 标识 error 的 JSON**：`ProviderResponseFailure`，且须和“合法空搜索”明确区分。
6. **Fetch 200 + 空白 Markdown**：抓取候选不可用，尝试下一家；不能以标题或摘要代替完整内容。
7. **抓取 URL 不在 URLStore**：在调用 Adapter 之前直接走既有拒绝分支，不新增任意 URL 抓取后门。
8. **daemon 不可达**：仅 `open_websearch` 失败；其他适配器仍按配置运行。
9. **超时/取消**：请求超时可按全局策略重试；任务取消不吞掉也不重试，避免请求停不下来。
10. **上游响应结构改变**：有证据的解析失败应暴露安全诊断并加入回归 fixture，不能吞异常或返回虚构结果。

## 7. Acceptance Criteria

- [ ] 四个 Provider 的 200 业务错误均正确识别，不误报成功。
- [ ] 已知临时业务错误按共享尝试次数重试；认证、额度、参数/未知错误不重试。
- [ ] HTTP 429/5xx、网络超时按已有日志及策略处理，未引入供应商独立 sleep。
- [ ] 结构错误、非法 JSON、Fetch 空正文均按对应错误分类失败。
- [ ] 单条坏 Search 记录被跳过且有计数；有效记录不丢。
- [ ] 日志包含阶段、Provider、错误类别、尝试；敏感值和网页正文不泄漏。
- [ ] 单个 Fetch Provider 失败时下一 Provider 可以接手；不修改调用方协议。
- [ ] 不假设 `Retry-After` 已被执行器支持，不擅自改写全局重试语义。

## 8. 参考

- `src/agent_search_gateway/providers/http.py`：`HttpJsonExecutor`、`ProviderResponseFailure`。
- `src/agent_search_gateway/providers/web/common.py`：`failure`、`CandidateCounts`、`parse_fetch_with_diagnostics`。
- `src/agent_search_gateway/providers/web/brightdata.py`：现有 HTTP 200 供应商错误 Inspector 示例。
- Doubao：https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh
- You.com：https://you.com/docs/api-reference/contents
- Keenable：https://docs.keenable.ai/api-reference/fetch.md
- open-webSearch：https://github.com/Aas-ee/open-webSearch/blob/main/docs/http-api.md
