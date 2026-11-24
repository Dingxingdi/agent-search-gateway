# Architecture: You.com REST / Free MCP over HTTP

- 日期：2026-11-21
- 状态：**设计已确认，待实施**
- 基线：`e712f10`
- 关联：[错误处理](../error-handlings/20261121-youcom-free-mcp-rest.md) / [测试](../testings/20261121-youcom-free-mcp-rest.md)

## 1. Scope & Assumptions

### In Scope
- 保持一个 `youcom` 注册名以及既有 `search(query)` / `fetch(NormalizedURL)` 契约；在 `YouComAdapter` 内隔离现有 REST 与 You.com 免费 MCP。
- **默认 REST 不变**：`api_url` 不配置时仍为 `https://ydc-index.io`，必须具备 `YDC_API_KEY` 对应的 `api_key_env`；Search 仍调用 `/v1/search`，Fetch 仍调用 `/v1/contents`。
- 用户显式把 `api_url` 设为 `https://api.you.com/mcp/free` 后通过原有 `httpx` / HTTP 共享执行器调用托管 MCP，**不需要 Key**；`you-search` 支持 Search，`you-extract` 支持 Fetch（请求参数仅 `url`）。
- 根据 `api_url` 精确判断两种协议；不新增 `mode` / `youcom_mcp`；不新增付费回退。
- 为凭据做**通用最小扩展**，不在公共解析器和 Runtime 中写 `if provider == "youcom"`；复用现有重试、RateGate、日志与编排器。

### Todo / Out of Scope
- 不开发通用 MCP SDK / 守护进程 / 其他 MCP 工具，不支持本轮无关的付费 MCP / OAuth / x402。
- 不自动改用付费 REST，不实现供应商免费额度余额统计，也不改其他 Provider、CLI 或 doctor。
- 免费 MCP 官方当前声明 100 queries/day；此为供应商配额，不等于 Gateway 本地 `rate_limits` 固定窗口（不把 100/day 硬编码）。
- 不预设未经验证的 MCP 工具响应细节；实施测试需对照官方示例或受控真实结果建立确定性 fixture。

### Assumptions
- 官方免费地址为 `https://api.you.com/mcp/free`，允许无 Key `you-search`、`you-extract`、`you-discover`；本功能只使用前两项。
- 最新 MCP `2026-07-28` Streamable HTTP **无协议级 session / initialize**，旧 `2025-*` 版本可能有；以免费端点实际支持版本为准，只实施必要的一条最小兼容路径。
- 自定义普通 REST API root（含代理路径）的既有语义必须保留，不能仅因 host 不是 `ydc-index.io` 就拒绝。

## 2. Architecture Summary

`keyword-search/url-fetch → SearchOrchestrator/FetchOrchestrator → 同一个 YouComAdapter → [REST | 免费 MCP codec] → HttpJsonExecutor（RateGate、Retry、脱敏日志） → KeywordSearchHit/URLFetchCandidate → 原有 URLStore/LLM`。协议识别与请求/响应转换均局限于 You.com；公共组件仅认统一的 Provider 契约。

| 配置 | 协议 | 认证 | Search | Fetch |
| --- | --- | --- | --- | --- |
| 缺省 `api_url` 或普通 REST root | REST | `api_key_env` 必需 | `POST /v1/search` | `POST /v1/contents` |
| `https://api.you.com/mcp/free` | MCP Streamable HTTP | 无 Key，不发认证头 | `tools/call: you-search` | `tools/call: you-extract` |

## 3. Design Decisions

### Runtime Model / Provider Integration

**D1 — 单一 Provider + 精确 URL 选路**
- Description：注册名始终 `youcom`；Adapter 构造时用规范化的 scheme/host/path **精确**识别 `https://api.you.com/mcp/free`（可接受结尾斜线），不使用字符串包含判断；对其他 `api.you.com/mcp*` 变体拒绝为不受支持端点，避免误将付费 MCP 当 REST。其余原有自定义 root 继续走 REST。
- Rationale：维持已有配置格式、Provider 优先级、Search/Fetch 可独立开关；免费与付费完全由显式配置控制。
- Trade-offs：一个适配器文件内有两个内部实现分支，需要保持私有函数清晰。
- Rejected：新增 `youcom_mcp`、新增 `mode`、模糊 URL 自动推断。

**D2 — 最小通用可选凭据**
- Description：保留 `WebProviderRegistration.requires_api_key` 默认行为，增加一个缺省 `False` 的可选 Key 标志（拟名 `supports_optional_api_key`）。仅 `youcom` 注册 `requires_api_key=False, supports_optional_api_key=True`：Config 允许无 `api_key_env` 或有正确环境值；Runtime 仅在存在 `secret` 时注入；Adapter 启动时校验 REST 必须有 Key、免费 MCP 不得配置/传递 Key；冲突在网络发起前产生 `CONFIG_ERROR`。
- Rationale：其他 Provider 的 required/forbidden Key 校验行为原样保留；没有 You.com 特例散布到通用组件。
- Trade-offs：`registry.py`、`config.py`、`runtime.py` 各增少量条件；You.com 注册断言需更新。
- Rejected：重构所有 Provider 的凭据 DSL / enum；把凭据逻辑写为 `if name == "youcom"`。
- Invariant：配置 `api_key_env` 但对应环境变量为空必须报配置错误，不得静默切换免费；REST 默认缺 Key 也必须失败关闭。

### Interface / Protocol

**D3 — 固定两个工具，复用 HTTP**
- Description：MCP 请求 `POST` 到完整 `api_url`（不追加 REST suffix），Search 工具 `you-search`、参数 `{"query":query,"count":10}`；Fetch 工具 `you-extract`、参数 **仅** `{"url":str(target)}`，不发 `X-API-Key`、`Authorization`、payment headers，也不额外请求 `you-contents`。
- Rationale：完全符合免费工具的公开参数范围，仍只向 Gateway 返回同一领域结构。
- Trade-offs：暂不支持高级搜索或 Extract 额外选项。
- Rejected：透传 REST 请求体到 MCP；提供任意 MCP tool 配置。

**D4 — 轻量 JSON-RPC / JSON+SSE 解码**
- Description：按 You.com 端点已确认的协议版本构造 JSON-RPC 2.0，支持 `application/json` 与 `text/event-stream` 响应，按请求 `id` 匹配最后的 `result/error`，SSE 前置通知不得被当成结果。2026-07-28 版本需包含和 `_meta` 一致的 `MCP-Protocol-Version`、`Mcp-Method` / `Mcp-Name` 等 Header；如实测端点仅接受 2025 握手协议，则局部实现必需的 `initialize`、初始化通知与 `Mcp-Session-Id`，不在全系统加入会话管理。
- Rationale：不依赖第三方 MCP SDK，同时不把 Streamable HTTP 误当普通 JSON REST。
- Trade-offs：需要少量响应 decoder 与协议版本测试。
- Rejected：永远假定有 session / 永远假定无 session；实现通用 MCP 客户端、旧独立 HTTP+SSE GET transport。
- Gate：**实施前先确定官方免费端点支持版本**。没有证据时不臆造会话行为；只做实际需要的兼容分支。

**D5 — 严格映射领域模型**
- Description：解析 `tools/call.result` 的 `structuredContent`（优先）或经验证 `content[].text` JSON 兼容表示；`isError=true` 和 JSON-RPC `error` 视为错误。Search 的有效 URL/title/snippet 映射为 `KeywordSearchHit`，坏单条按现有 `CandidateCounts` 拒绝；Fetch 的非空正文映射为 `URLFetchCandidate`，若结果带 URL 应做 normalized match，不能用搜索摘要代替页面正文。
- Rationale：不改变上游编排器的依赖与安全边界。
- Trade-offs：需要核对精确工具结果 schema，变更上游需更新 fixture。
- Rejected：向编排器暴露原始 JSON-RPC / SSE / 未经验证的自然语言结果。

### Concurrency / Scheduling, Security & Observability

**D6 — 不新增内部回退、配额账本**
- Description：原有 `RateGate`、并发配额、RetryPolicy、URLStore、SearchOrchestrator、FetchScheduler 继续工作。免费 MCP 失败后仅由 Gateway 尝试其他**已配置提供商**，绝不自动触发 You.com REST。
- Rationale：避免双重调度、无意扣费。
- Trade-offs：Gateway 无供应商 100/day 余额视图，不保证单进程速率限制等价免费日配额。
- Rejected：免费耗尽自动有偿补足、跨进程额度记账等额外功能。

**D7 — HTTP 共享层仅做必要的通用 decoder 扩展**
- Description：保留 `HttpJsonExecutor` 统一的尝试限流/HTTP 429、5xx 重试/`Retry-After`/耗时及截断请求体日志。现有 `request_json` 只执行 `response.json()`，若需 SSE，优先新增默认行为不变的可选 `response_decoder(response)` 或等价通用能力，**不**让 Adapter 直接创建新 `httpx` 客户端。诊断只允许固定类别、错误 code，不输出任意上游正文/Key/查询/URL/session 值。
- Rationale：HTTP 通道复用，且新增协议差异不泄漏到其他 Provider。
- Trade-offs：公共 HTTP 接口需一处向后兼容的微扩展和相应测试。
- Rejected：另写 You.com 私有网络/重试/日志执行器。

## 4. Component Catalog

| Component | Purpose | Key responsibilities / interfaces | Dependencies | State owner / role |
| --- | --- | --- | --- | --- |
| `WebProviderRegistration` | 通用身份凭据能力声明 | 旧 requires API Key + optional 标志 | 无 | metadata / validator |
| `config.py` | 配置决议 | required/forbidden/optional 通用解析，`ResolvedWebProviderConfig` | Registry、environ | resolved config / validator |
| `Runtime._build_web_providers` | 构造及生命周期 | 可选 Secret 注入、原有 HTTP executor、RateGate | Registry、HttpJsonExecutor | executor / coordinator |
| `YouComAdapter` | 供应商行为边界 | `search` / `fetch`、协议选路、领域映射 | shared HTTP、NormalizedURL | 只保存协议与配置 / transformer |
| `You.com 私有 MCP codec` | 消息层 | 构造 JSON-RPC、解析 JSON/SSE、校验 id/result | MCP protocol | 临时请求状态 / protocol boundary |
| `HttpJsonExecutor` | HTTP 共用执行 | Retry、RateGate、日志、必要的可选 decoder | httpx | HTTP 生命周期 / I/O boundary |
| `SearchOrchestrator / FetchOrchestrator` | 回退及交付 | 既有选择、失败处理、URLStore/FetchScheduler | Provider contracts | 保持不变 / coordinator |

## 5. Data Flow

### 5.1 入口：配置加载 / Runtime.build
```text
registration = registry.require("youcom")
resolved = resolve_one_web_provider(table, registration, environ)
  if required/forbidden/optional credential policy violated: raise CONFIG_ERROR
  if api_key_env configured but env value empty: raise CONFIG_ERROR
runtime creates shared HttpJsonExecutor and RateGate
adapter = YouComAdapter(api_url=resolved.options.get("api_url", REST_DEFAULT),
                        secret=resolved.secret, http_executor=executor)
  if URL is exact official free MCP: require secret is None; protocol = MCP
  else if URL is unsupported api.you.com/mcp* variant: raise CONFIG_ERROR
  else: require secret present; protocol = REST
register same adapter for enabled search and/or fetch
```

### 5.2 入口：keyword-search / web_search
```text
SearchOrchestrator selects enabled "youcom" by existing rules
try:
  if REST:
    payload = HttpJsonExecutor.request_json(POST, api_url + "/v1/search",
                                            X-API-Key, {query, count:10})
    return existing REST parser(payload)  # list[KeywordSearchHit]
  else:
    rpc = MCP_codec.build_tools_call("you-search", {query, count:10})
    payload = shared_executor.POST(api_url, rpc, version_headers, JSON/SSE decoder)
    if rpc.error or payload.result.isError: raise provider failure
    return MCP_codec.parse_verified_search_result(payload)  # list[KeywordSearchHit]
except ExecutionFailure/ProtocolFailure:
  use existing SearchOrchestrator fallback; NEVER invoke paid You.com REST
```

### 5.3 入口：url-fetch
```text
FetchOrchestrator / FetchScheduler select admitted URL and enabled "youcom"
try:
  if REST:
    payload = HttpJsonExecutor.request_json(POST, api_url + "/v1/contents",
                                            X-API-Key, {urls:[target], formats:["markdown"]})
    return existing REST normalized_match+markdown parser(payload)
  else:
    rpc = MCP_codec.build_tools_call("you-extract", {url:target})
    payload = shared_executor.POST(api_url, rpc, version_headers, JSON/SSE decoder)
    if rpc.error or payload.result.isError: raise provider failure
    return MCP_codec.parse_verified_fetch_result(payload, target)
except ExecutionFailure/ProtocolFailure:
  use existing FetchScheduler fallback; NEVER invoke paid You.com REST
```

## 6. Interfaces & Contracts

### 用户配置（两段为互斥示例，非同时生效）
```toml
# REST：默认，兼容旧用户
[web_providers.youcom]
enable_search = true
enable_fetch = true
api_url = "https://ydc-index.io"  # 也可省略
api_key_env = "YDC_API_KEY"

# 免费 MCP：将同一个表替换为以下内容
# [web_providers.youcom]
# enable_search = true
# enable_fetch = true
# api_url = "https://api.you.com/mcp/free"
# # 必须省略 api_key_env
```

### MCP 请求示意（实际按协商版本补 `_meta`/headers）
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "you-search",
    "arguments": {"query": "example", "count": 10}
  }
}
```
Fetch 工具的 `params` 为 `{"name":"you-extract","arguments":{"url":"https://example.com/page"}}`。客户端必须检查 JSON-RPC 版本、`id`、`result/error`、`isError` 与真正的结果形状，不能仅根据 HTTP 200 判定成功。

### Stable Gateway contracts
- `search(query: str) -> list[KeywordSearchHit]`
- `fetch(url: NormalizedURL) -> URLFetchCandidate`
- `name == "youcom"`、旧 REST path/header/body、用户入口与现有失败类别保持兼容。

## 7. Minimal Change Surface & References

预期仅涉及：`src/agent_search_gateway/providers/web/youcom.py`；必要的小型通用凭据调整 `providers/registry.py`、`providers/defaults.py`、`config.py`、`runtime.py`；必要时 HTTP decoder / requester protocol；`README.md`、`config.example.toml`、现有测试。**不**修改其他 provider 或核心 orchestrator。

官方依据（查阅 2026-11-21）：
- https://you.com/docs/agents/mcp-server
- https://you.com/docs/using-the-api/authentication
- https://about.you.com/resources/mcp-web-search
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
