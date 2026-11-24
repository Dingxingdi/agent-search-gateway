# Testing: You.com REST / Free MCP over HTTP

- 日期：2026-11-21
- 状态：**设计已确认，待实施**
- 同组：[架构](../architectures/20261121-youcom-free-mcp-rest.md) / [错误处理](../error-handlings/20261121-youcom-free-mcp-rest.md)
- 目标：验证新增免费 MCP 后 **旧 REST 不变、免费 MCP Search/Fetch 可用、任何 MCP 失败都不会调用付费 REST**。

## 1. Scope, Gate & Fixtures

- **先测试后实施**。沿用 `pytest`、`pytest-asyncio`、`httpx.MockTransport`、`RecordingJsonExecutor`、现有中性合成 fixtures。不得增加无关的测试框架、全局模拟代理或外部 MCP SDK。
- 测试分三个级别：纯配置/Adapter 单元，真实 `HttpJsonExecutor` 的伪网络集成，用户显式 opt-in 的免费远端冒烟。
- 优先扩展 `tests/providers/web/test_youcom.py`、`tests/unit/test_config_web_providers.py`、`tests/runtime/test_new_web_runtime_assembly.py`、`tests/observability/test_provider_diagnostics.py`、`tests/providers/test_registry.py` 和必要的 `tests/docs/test_documented_config.py`、`tests/acceptance/test_gateway_workflows.py`。
- 合成 MCP fixture 放 `tests/fixtures/providers/youcom/`，名称区分 REST 与 MCP，不把用户请求、Key 或真实页面内容写进仓库。
- 必须先用 You.com 官方示例/受控 live 验证确切 MCP 协议版本与工具结果 schema，再固定 `structuredContent` / `content[].text` 字段；缺乏证据时测试应标为待确认，不写假成功 fixture。

## 2. Test Matrix

| 测试模块 / 层 | 场景 | 主要断言 |
| --- | --- | --- |
| `test_youcom.py` REST 回归 | 无 `api_url` 的 Search | 仍为 `POST https://ydc-index.io/v1/search`、`X-API-Key`、`{"query":...,"count":10}`，结果不变 |
| `test_youcom.py` REST 回归 | Fetch 与自定义带前缀 API root | 仍为 `/v1/contents`、`urls[]`、`formats=["markdown"]`，按 URL 匹配内容 |
| `test_youcom.py` MCP Search | 免费 endpoint + 无 Key | **只**向 `https://api.you.com/mcp/free` 发 `tools/call(you-search)`；JSON-RPC、必要 headers、参数正确；映射 `KeywordSearchHit` |
| `test_youcom.py` MCP Fetch | 免费 endpoint + 无 Key | **只**调用 `you-extract`、仅 `url` 参数，不带 `formats` / `X-API-Key`，得到非空正文 `URLFetchCandidate` |
| `test_youcom.py` MCP 解析 | 官方 `structuredContent` JSON、已验证的 `content[].text` 后备 | 结构有效才成功；合法空搜索列表返回 `[]`，非法根结构报错 |
| `test_youcom.py` MCP SSE | 前置通知、心跳、多行 data、最终 id 匹配帧 | 解析最终结果，不把第一个 SSE event 当成功 |
| `test_youcom.py` MCP 失败 | RPC `error`、`isError=true`、id 错、缺结果、无效 JSON、损坏/截断 SSE | 分别落到既有 `ExecutionFailure` / `ProtocolFailure`，不可当成功 |
| `test_youcom.py` MCP 业务错误 | Search 单条坏 URL；Fetch 空正文、不匹配目标 URL | 计数/失败分类与 REST 一致，不伪造内容 |
| `test_config_web_providers.py` | 默认 REST Key、MCP Key-free、MCP 错误配置、未知 config keys | 缺 REST Key、配置冲突在任何 HTTP 发生前报 `CONFIG_ERROR`；可选 Key 扩展只作用于注册声明 |
| `test_registry.py` | 注册元数据 | 仅一个 `youcom`；能力 search/fetch 不变；其他 Provider required/forbidden Key 行为不变 |
| `test_runtime_assembly.py` | 真实 Runtime.build | 同一 Adapter 同时提供 Search/Fetch，共用 executor/quota；MCP 不传 Secret，REST 传 Secret；client 关闭恰好一次 |
| `test_provider_diagnostics.py` | MCP HTTP 429/5xx/timeout、响应体 error、日志 | 共用重试、Retry-After、耗时、截断 request preview；`PRIVATE_*` 哨兵不会出现在日志 |
| `test_gateway_workflows.py` | MCP 成功/失败后外层调度 | 保持 URLStore/调度契约；失败尝试其他已配置提供商，不使用 You.com REST |
| `test_documented_config.py` | TOML/README 示例 | REST 默认例有效，注释中的 MCP 切换例无 Key、无 `mode` 或新 Provider 名 |

## 3. Protocol-Version Gate

**不要同时无条件发送新版 metadata 和旧版 handshake。**

- 先对 You.com 免费端点读取/验证 `tools/list` 或官方实例，确认支持版本；独立 fixture 模拟实际版本完整路径。
- 若确认 `2026-07-28`：验证 `MCP-Protocol-Version`、`Mcp-Method`、`Mcp-Name`、`_meta` 一致，正常流程无 `initialize` / Session ID。
- 若仅确认旧 `2025-*`：验证 `initialize`、必要 notification、Session ID 传递、session 失效后最多一次重建；不把“普通 404”视为会话失效。
- 对未知/不支持版本必须安全失败，不要无限尝试各种版本；旧独立 HTTP+SSE GET 端点不在范围内。
- 覆盖 JSON response 与 request-scoped SSE response，SSE 只选择与请求 ID 相同的最终帧。

## 4. Credentials & No-Paid-Request Safety

使用 `httpx.MockTransport` 将所有请求记入数组，同时设置哨兵 `PRIVATE_AUTH`、`PRIVATE_QUERY`、`PRIVATE_URL`：

1. 免费 MCP 模式缺 `api_key_env`，配置和 Runtime 构造通过；捕获所有请求，不含 `X-API-Key`、`Authorization` 或 payment header。
2. 免费 MCP 配 `api_key_env`，应在启动期失败；**请求数组长度为 0**。
3. REST 缺少 `api_key_env`、环境变量未定义/空值均启动失败；**请求数组长度为 0**。
4. REST 配 Key 仍发送原 `X-API-Key`；只有 REST 分支允许付费根地址。
5. 用先返回 quota error 的 MCP mock + 单独的其他 Provider mock 模拟 Gateway 回退，断言**零次**触达 `https://ydc-index.io` 或任何其他 You.com 付费路径。
6. 自定义 REST root（含路径前缀）按原规则连接；`api.you.com/mcp` 等不支持/可能收费的 MCP 变体必须在首次网络调用前被拒绝。
7. 配置全局改动需验证 Jina、open_websearch（无 Key）与其他 keyed Provider 仍遵守原先规则。

## 5. Error, Retry & Observability

- 注入 `RetryPolicy`、`sleep` / fake clock，精确断言 HTTP 429 / 5xx 的尝试数、`Retry-After` 优先级与日志 `elapsed_ms`；RPC error、`isError`、错误 envelope 不会触发无限网络重试。
- HTTP 200 + 业务错误必须显式失败；不能仅以 `status_code==200` 代表成功。
- `safe_diagnostic_fields` 只允许有限、固定的 code/category，任意上游 `message`、MCP `content[].text`、Key、查询和完整 URL 不出现于 logs。
- 请求 body 仍受已有截断日志策略约束；不得因为 MCP 请求是 JSON-RPC 而绕过 HTTP 执行器/RateGate。
- Search 坏单条仍发 `provider_candidates_summary`，其 `received/accepted/rejected` 与实际记录一致。
- 所有失败必须保留提供商 `youcom` 与 stage `search` / `fetch` 以便 doctor / 日志诊断，不发散到独立 provider 名称。

## 6. Optional Live Smoke (Only Explicit Opt-in)

- **严禁 CI 默认调用免费端点**，避免消耗共享 100/day 额度。使用现有 `WEB_SEARCH_RUN_INTEGRATION=1` 加定向 `-k youcom_mcp` 或等价显式 opt-in 条件，限量执行。
- 不依赖 `YDC_API_KEY`。明确设置 `api_url="https://api.you.com/mcp/free"`，在真实 HTTP 共享执行器下执行一次 `you-search`、一次 `you-extract`；记录仅状态分类、结果数量、耗时、协议版本与是否符合契约，不记录真实 query 或正文。
- 优先从返回的已收录有效 URL 测试 Fetch；若第三方页面无法访问，应记录外部抓取失败，不把它冒充协议失败。
- 捕获请求目标的 host/path，验证全程只有 `api.you.com/mcp/free`，且无认证/payment header，不能借现有环境中的 `YDC_API_KEY` 意外选择 REST。
- 若用户未明确开启 live 测试：`SKIP`，不能宣称已经验证线上可用。

## 7. Acceptance Checklist / Commands

全部满足才视为实现完成：

- [ ] REST 既有测试 100% 通过（包括默认/自定义 URL、Key、错误码与日志）。
- [ ] 免费 MCP Search、Fetch、JSON/SSE、工具错误、版本与安全测试通过。
- [ ] 配置扩展没有改变其他 Provider 的必填/禁止 Key 规则。
- [ ] 无成功路径伪造、无无意付费回退、无秘密泄漏。
- [ ] 共用 HTTP 执行器的重试、限流和日志仍按原规则生效。
- [ ] 文档明确免费端点、Key-free、100/day 供应商配额与本地 rate limit 差异。
- [ ] 实际 live smoke 若运行，输出具体 PASS/FAIL/SKIP，不写模糊“已测试”。

```bash
uv run pytest tests/providers/web/test_youcom.py -v
uv run pytest tests/unit/test_config_web_providers.py tests/providers/test_registry.py -v
uv run pytest tests/runtime/test_new_web_runtime_assembly.py tests/observability/test_provider_diagnostics.py -v
uv run pytest tests/docs/test_documented_config.py tests/acceptance/test_gateway_workflows.py -v
uv run ruff check .
uv run mypy src
uv run pytest -q
# 仅在显式允许真实网络后运行定向 MCP live 测试
# WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k youcom_mcp -v -rs
```

## 8. References
- https://you.com/docs/agents/mcp-server
- https://about.you.com/resources/mcp-web-search
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
