# Error Handling: You.com REST / 免费 MCP 双协议

- 日期：2026-11-21
- 状态：**设计已确认，待实施**
- 同组：[架构](../architectures/20261121-youcom-free-mcp-rest.md) / [测试](../testings/20261121-youcom-free-mcp-rest.md)
- 宗旨：不改变 REST 错误语义；MCP 的失败全部归入 Gateway 已有的 `ConfigFailure`、`ProtocolFailure`、`ExecutionFailure`、`ProviderResponseFailure` / HTTP 状态错误。

## 1. Boundary & Invariants

1. 免费 MCP 仅允许精确的 `https://api.you.com/mcp/free`；不得发送 `X-API-Key`、`Authorization`、x402/payment 头，也不得在错误时改用付费 REST。
2. 免费路径若配置了 `api_key_env`、REST 缺失 Key、Key 环境变量未设/空值、MCP URL 不支持等，必须在网络调用前抛 `CONFIG_ERROR`；错误信息不包含敏感值。
3. 现有 REST API 的错误检查（HTTP 200 内嵌错误、Known Error Code、Fetch 空 Markdown / URL 不匹配）维持不变；此次仅新增 MCP 特有的协议与工具错误分类。
4. 统一 executor 负责 HTTP 尝试、重试、超时、`Retry-After`、耗时及截断 body 预览；Adapter 不自建重试环或另起 HTTP 客户端。
5. Gateway 既有 Search/Fetch 编排器继续决定后续 Provider；You.com Adapter 不私自触发第二协议。

## 2. Error Matrix

| 条件 | 分类 / 处理 | 重试 | 日志 |
| --- | --- | --- | --- |
| 无效/冲突配置、免费 MCP 配 Key、REST 无 Key | `ConfigFailure(CONFIG_ERROR)`；启动前拒绝 | 无 | 安全配置错误，不记 Key |
| HTTP 408 / 429 / 5xx | 沿用共享 HTTP status failure / RetryPolicy | 按现有策略；适用时使用 `Retry-After` | HTTP 状态、尝试次数、延迟与耗时 |
| HTTP 401 / 402 / 403 | HTTP failure；认证、支付或授权问题 | 默认终态 | 仅安全状态码，不打印任意响应正文 |
| 连接错误、DNS、TLS、timeout | 共享 transport / timeout failure | 按现有 retry budget | 传输类型、尝试次数，不记完整 URL |
| HTTP 200 但响应不是 JSON 或合法 SSE | `ProtocolFailure(PROTOCOL_ERROR)` | 终态 | `category=decode` / 固定诊断 |
| SSE 前置心跳/通知、多个事件 | 按 `id` 找最终结果；忽略合法通知 | 不算失败 | 不输出 raw SSE |
| SSE 无最终结果、格式损坏、重复冲突的最终帧 | `ProtocolFailure` | 终态 | `category=protocol` / 统计信息 |
| JSON-RPC `id` 不匹配、缺失 `result/error`、错误 envelope | `ProtocolFailure` | 终态 | allowlist 协议原因 |
| JSON-RPC `error`（即使 HTTP 200） | `ExecutionFailure` / `ProviderResponseFailure` | 默认终态，不盲重试 | 仅允许列举过的 upstream code |
| 工具结果 `result.isError=true` | `ExecutionFailure` | 默认终态 | 固定 semantic_result，禁止泄漏 tool text |
| `result.structuredContent` / `content` 格式不合法 | `ProtocolFailure`（信封）或 `ExecutionFailure`（业务结果） | 默认终态 | 协议/业务类别；不能假装零结果 |
| Search 正常根结果含坏的单个 URL | 依既有 `CandidateCounts` 拒绝该条 | 无 | received/accepted/rejected |
| Search 根结构无有效结果数组 | 协议/业务结果错误，非正常空搜索 | 终态 | `invalid_envelope` |
| Fetch 空正文、目标 URL 不匹配 | 沿用 `failure`，不得使用 Search 摘要替代 | 终态 | `empty` / `no_match` |
| 供应商免费日配额耗尽（HTTP 429 或明确业务错误） | 将当前 You.com 调用视为失败，交给外层调度器 | HTTP 429 按既有 bounded Retry；明确业务配额耗尽不作无限重试 | 限流类型与安全上游 code |
| 版本不兼容（例：`UnsupportedProtocolVersion`） | 仅依据官方声明的可兼容版本作**有限**版本选择；否则协议失败 | 有界版本回退，不无限探测 | 固定版本/错误类别，不记录任意 body |
| 旧版 MCP session 过期/404 | 只有确实启用旧版握手时才进行至多一次重新初始化 | 有界恢复 | 固定 `protocol` 原因 |
| 新版 MCP 没有会话 | 不生成、不重置 session | 无 | 不输出任何 session ID |

## 3. Protocol Decoder Rules

- JSON-RPC：验证 `jsonrpc=="2.0"`、请求 `id` 与返回 `id` 一致、恰好一种 `result` 或 `error`；`HTTP 200` 不是充分成功条件。
- JSON Content-Type：只解析 JSON 对象信封；合法 `error` 走提供商失败，不归类为“decode success”。
- SSE Content-Type：按标准事件边界处理 `data:`（包括多行 data）、空行、心跳注释、允许通知；**只有与请求 id 匹配的最终 JSON-RPC response 才是成功**，并限制消息体大小，禁止无限缓存。
- 最新 `2026-07-28` 使用逐请求元数据，无 session；旧 `2025-*` 的初始化、session 只能在 You.com 确定使用旧版时实现，不得将所有 400/404 视为 session 失效。
- 解码失败不得把 `content[].text` 任意自然语言伪装为有效 JSON；仅在官方已确认兼容格式中做 fallback。先验证 `structuredContent` 的类型与目标字段，再使用后备内容。
- `you-extract` 免费参数严格 `{"url": target}`，不重试增加 `formats`、`timeout` 等仅限 Key 场景的选项。

## 4. Logging, Secrets & Redaction

- 所有 HTTP 请求继续走 `HttpJsonExecutor` 的事件：`http_retrying`、失败事件、每次请求耗时、经脱敏和截断后的请求 preview。
- 允许结构化诊断：`provider=youcom`、`stage=search/fetch`、`category`、`semantic_result`、`http_status`、`attempt`、`elapsed_ms`、有限的 `upstream_code`、结果接受/拒绝计数。
- 禁止原样写入：`YDC_API_KEY`、`Authorization` / `X-API-Key`、任意用户 query、目标完整 URL、上游错误 `message`、工具 `content[].text`、原始 SSE、`Mcp-Session-Id`。
- 诊断必须遵循现有 `safe_diagnostic_fields` 的 allowlist；未知/任意上游错误码映射为 `unknown`，避免反射恶意 payload。
- 日志中应能区分 HTTP 拒绝、JSON-RPC 错误、工具 `isError`、无效 schema，而不重新打印带敏感信息的请求/响应正文。

## 5. Recovery / Fallback

```text
attempt_youcom(stage):
    if bad_config:
        raise ConfigFailure before any network call
    try:
        use shared executor with existing bounded RetryPolicy
        decode JSON or SSE -> validate JSON-RPC -> validate tool result
        return KeywordSearchHit[] or URLFetchCandidate
    except HTTP_429_or_5xx:
        shared executor may retry according to RetryPolicy / Retry-After
        if exhausted: propagate failure to original orchestrator
    except rpc_error_or_isError_or_protocol_error:
        propagate terminal provider failure to original orchestrator
    # no MCP-to-REST hop, no paid request, no new provider fallback logic
```

## 6. Explicit Negative Cases

- 不因为响应中包含 `results`、`markdown` 等字样就跳过 JSON-RPC envelope 验证。
- 不把 `isError` 的错误内容当可抓取正文。
- 不把 2026 新版协议的普通 404 误判成旧版 session 失效。
- 不对失败结果伪造 `[]` / 空内容作正常成功，从而掩盖回退。
- 不通过 You.com REST 的额外一跳来“拯救”免费 MCP。
- 不扩展无关的通用错误码目录或日志 schema，除非有现有字段无法表达的具体证据。

## 7. Test Hooks / References

每行错误矩阵必须由定向 mock 测试覆盖；HTTP/SSE 原始响应使用 `httpx.MockTransport`，日志安全用哨兵字符串断言，重试次数由共享执行器的可注入 `sleep` / clock 验证。

依据：
- https://you.com/docs/agents/mcp-server
- https://about.you.com/resources/mcp-web-search
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
