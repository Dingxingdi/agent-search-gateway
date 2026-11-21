# You.com REST / Free MCP over HTTP Implementation Plan

**Goal:** 为现有 `youcom` 提供商增加显式选择的免费 MCP Search/Fetch，保留默认 REST、现有领域契约和共享调度机制。

**Architecture:** `docs/designs/architectures/20261121-youcom-free-mcp-rest.md`

**Error handling:** `docs/designs/error-handlings/20261121-youcom-free-mcp-rest.md`

**Testing:** `docs/designs/testings/20261121-youcom-free-mcp-rest.md`

---

## 1. 执行上下文与约束

- 日期：2026-11-21。
- 工作树：`[workspace:ws_df505fcab8f5d002cc946541]`。
- 分支：`design-youcom-free-mcp-20261121`；设计提交：`1ba9cc3283a6dfdbf30a0f6051d133db23f15306`；实现基线：`e712f10`。
- 在这个已有工作树实施。先加载适用 Agent 指令，检查其他 Agent 的修改；不创建新工作树、不切换分支、不修改共享服务或用户当前配置。
- 保持唯一注册名 `youcom`。不增加 `mode`、`youcom_mcp`、MCP SDK、独立 HTTP 客户端、配额账本或 MCP→REST 付费回退。
- 默认 `api_url=https://ydc-index.io` 仍为 REST，使用显式配置的 `api_key_env`。只在精确免费地址选择 MCP；免费配置即使环境中存在 `YDC_API_KEY` 也不得读取或发送它。
- Search/Fetch 独立开关、配置顺序、URLStore、编排器、并发配额、RateGate、RetryPolicy、CLI 和 doctor 的实现保持现有边界。全局凭据逻辑不得按供应商名称分支。
- 本文是待执行计划。下面的 PASS、FAIL 和 SKIP 是实施时的预期，不表示本轮已执行这些测试或线上冒烟。

### 已核对的代码事实

| 现有文件 / 符号 | 当前行为 | 实施影响 |
| --- | --- | --- |
| `providers/registry.py: WebProviderRegistration` | 只有 `requires_api_key: bool = True` | 增加默认关闭的通用可选 Key 标志 |
| `config.py: _resolve_one_web_provider` | `requires_api_key=False` 目前表示禁止配置 Key；全禁用表提前返回 | 扩展为 required/optional/forbidden，保留禁用与未知字段语义 |
| `runtime.py: Runtime._build_web_providers` | 只有 required 注册注入 Secret；每个启用 provider 构造一个 executor/adapter | 可选 Key 也按实际 Secret 注入，同一个 adapter 供 Search/Fetch 使用 |
| `providers/web/youcom.py: YouComAdapter` | 仅 REST；`secret` 必填；Search 使用 `results.web`，Fetch 使用 URL 匹配的 Markdown | 在供应商边界选路；MCP 不能直接套用 REST envelope/inspector |
| `providers/http.py: HttpJsonExecutor.request_json / _request_response` | 重试在共用发送层；成功 JSON 解码在重试外；`send` 默认完整缓存；现有 HTTPX timeout 是分阶段超时 | decoder 仍在重试外；MCP 需发送边界的有界读取和单次绝对时限 |
| `providers/http.py` 的 body preview | 原始 request/response body 在供应商 inspector 和 decoder 之前进入日志 | 仅新增 decoder 不能防止 MCP 嵌套正文或 SSE 泄漏 |
| `providers/diagnostics.py: safe_diagnostic_fields` | 限制字段与类型，但不是所有上游字符串的语义白名单 | MCP 上游 code 必须由私有 codec 先做固定枚举映射 |
| `tests/support/http.py` | Recording executor 直接返回已解析对象 | 可验证 REST 请求；协议、流、重试、日志必须用真实 executor + MockTransport |

以上路径均以 `src/agent_search_gateway/` 为前缀，除显式列出的测试路径。

## 2. 实施前置检查 G0：固定协议与工具结果证据

**这项检查在创建 MCP 成功 fixture、实现 MCP 请求或结果解析之前完成。** 可以先做与协议版本无关的 Task 1–2；Task 3–5 必须等待此检查通过。不要为了提前运行绿色测试而用 REST fixture 包一层 JSON-RPC 冒充真实 MCP。

### 已确认与尚未确认

2026-11-21 查阅的官方资料支持：

- 当前免费地址为 `https://api.you.com/mcp/free`；提供 `you-search`、`you-extract`、`you-discover`，文档标示 100 queries/day；本功能仅使用前两项。免费 Extract 只发送 `url`。[You.com MCP 文档](https://you.com/docs/agents/mcp-server)、[认证文档](https://you.com/docs/using-the-api/authentication)。
- 官方 10 月的协议/搜索示例针对旧 `/mcp?profile=free`。它是选择首个探测候选的依据，不能证明新 `/mcp/free` 的版本或 Extract 输出。旧例中的省略号也不能作为 fixture。[You.com 协议实测文章](https://about.you.com/resources/mcp-web-search)。
- 若准确端点确认使用 `2026-07-28`，逐请求元数据和 HTTP headers 按该修订实现，不发送 initialize 或 session。若只确认旧版，实施后文的互斥旧版路径。[协议概述](https://modelcontextprotocol.io/specification/2026-07-28/basic/index)、[Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[版本协商](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)。

本轮只查文档，未请求免费服务；**新路径的实际版本、Search 字段组合、Extract 原始成功 envelope 和正文位置仍待 G0 验证**。Context7 在本会话没有可调用连接器，公开解析请求未取得可用 library ID，因此以上依据来自直接读取的一手文档。

### G0 操作与退出条件

- [x] 优先寻找准确路径的官方完整请求/响应或服务端源代码证据；足够时直接完成对应证据项，无需额外真实调用。以下 discovery、tools/list、两次工具调用属于文档证据不足时的备选探测。项目用户已允许 smoke，延续该授权；若接手环境没有真实网络授权，只记录证据缺口，不把未运行记为通过。
- [x] 探测只访问 `https://api.you.com/mcp/free`，显式无认证/payment headers、不跟随重定向、不从环境自动选 REST。以新版为第一个候选请求 `server/discover` 和 `tools/list`；记录 supported version、媒体类型、工具名称、inputSchema、可用 outputSchema。
- [x] 只有准确端点的官方明确版本信息或服务器明确响应才可选择一个已知旧版本；普通 400/404/403、限额或网络故障不是旧版证据。探测不循环枚举版本。
- [x] 校验 `you-search` 接受 `query` 与 `count=10`，`you-extract` 接受单个 `url`；检查使用到的 inputSchema 属性有无 `x-mcp-header`。若有，仅为这两个固定工具实现已确认的 header 映射，不开发通用 schema 引擎。
- [x] 各调用一次 Search 与 Extract，优先用搜索返回的公开有效 URL 抓取。区分第三方页面抓取失败、配额失败与协议错误。只持久化字段结构、类型、协议版本、媒体类型和去敏结论。
- [x] 明确 Search 的 URL/title/snippet 路径，以及 Extract 的正文、可选返回 URL、空值与错误结构。特别确认 snippet 是 `snippets`、`contents.highlights` 或其他已证实字段，不能靠猜测添加别名。
- [x] 对 Search 和 Extract 分别确认成功表示：`structuredContent`、`content[].text` 中的 JSON，或二者兼有。每个工具至少一种已证实的成功表示即可通过；两种均按证据条件化，不能要求不存在的 structured 表示。仅支持有官方或受控证据的包装；自然语言错误文本不算结果。
- [x] 在 `tests/fixtures/providers/youcom/mcp_contract.md` 写入日期、准确路径、选择的唯一版本、来源、输入输出字段映射、fixture 来源和未支持表示。把页面/查询替换为 `example.test`、中性标题和合成正文；不提交凭据、用户查询、真实页面正文或 session 值。
- [x] 未取得两个工具的成功契约时，将相应步骤标为 BLOCKED，并说明原因；不得宣称免费 Search/Fetch 已完成。G0 通过后再创建下列 MCP fixture。

**预计新增 fixture：**

| 路径（均在 `tests/fixtures/providers/youcom/`） | 内容 |
| --- | --- |
| `mcp_contract.md` | 证据与实际字段映射，不是新设计 spec |
| `mcp_search_structured.json` | 仅在 Search structured 表示获证实时创建，完整信封与合成值 |
| `mcp_fetch_structured.json` | 仅在 Extract structured 表示获证实时创建，完整信封与合成值 |
| `mcp_search_text.json` | 仅在文本 JSON 包装获证实时创建 |
| `mcp_fetch_text.json` | 仅在 Extract 文本包装获证实时创建 |
| `mcp_search.sse` | 用已确认结果构造的合法 SSE，含通知、心跳、多行 data、最终响应 |

JSON/SSE 格式测试可以按协议构造负例；它们不承担证明线上结果 schema 的职责。既有九个 REST fixture 保留。成功 fixture 内的 ID 是占位值：MockTransport 每次从收到的请求取 ID 后填入该响应的拷贝，不能修改共享 fixture 或硬编码 ID；错误 ID 用例刻意例外。

### 唯一协议路径

若 G0 确认 `2026-07-28`，请求使用：

```text
headers:
  Content-Type = application/json
  Accept = application/json, text/event-stream
  MCP-Protocol-Version = confirmed version
  Mcp-Method = tools/call
  Mcp-Name = fixed tool name

params:
  name = you-search | you-extract
  arguments = exact arguments for that tool
  _meta:
    io.modelcontextprotocol/protocolVersion = same version
    io.modelcontextprotocol/clientCapabilities = {}
    io.modelcontextprotocol/clientInfo = existing package name/version
```

`clientInfo` 是建议字段；版本与 capabilities 是必需字段。不要把 `_meta` 放到 JSON-RPC 顶层。正常请求不发 `server/discover` / `tools/list`；它们在 G0 固定契约，不增加每次搜索的网络开销。没有 session 状态。

若 G0 只确认一个旧 `2025-*` 版本，则 **替换** 上述请求构建步骤与对应测试：

1. 在 You.com 私有代码中懒初始化；通过同一个 executor 发送 initialize、接受必要 session header、发送初始化通知。通知成功 202 的空响应使用专门 decoder，不套工具响应校验。
2. 只保存必要的 session/初始化状态；锁住初始化并在取得锁后复查，避免 Search/Fetch 同时重复握手。工具调用本身不因这个锁被串行化。
3. Session ID 仅回传当前准确端点；不进入日志、fixture 或公共 Runtime。
4. 只有已确认的失效信号才至多重建一次，并限制原调用重发一次。普通 404 不触发重建；重建失败直接传播，不再回退 REST。
5. handshake、通知、重建每一跳均经同一 executor、RateGate 与现有有界重试；取消需释放锁与响应。测试并发初始化、一次恢复、再次失效终态、普通 404 不恢复。
6. 不同时保留未证实的新旧运行时版本探测分支，不增加旧独立 GET/SSE transport 或全局会话管理。

## 3. 文件结构与依赖

| 文件 | 责任 / 任务 |
| --- | --- |
| Modify: `src/agent_search_gateway/providers/registry.py` | 默认关闭的 `supports_optional_api_key`；Task 1 |
| Modify: `src/agent_search_gateway/config.py` | 通用凭据解析；Task 1 |
| Modify: `src/agent_search_gateway/runtime.py` | 按 Secret 是否存在注入可选凭据；Task 1、6 |
| Modify: `src/agent_search_gateway/providers/defaults.py` | 最后启用 You.com optional 注册声明；Task 6 |
| Modify: `src/agent_search_gateway/providers/web/youcom.py` | 构造期选路/凭据约束、REST 保持、MCP 领域映射；Task 1、3–6 |
| Create: `src/agent_search_gateway/providers/web/_youcom_mcp.py` | 私有请求构建、JSON/SSE 与 JSON-RPC/tool envelope 校验；Task 3–5 |
| Modify: `src/agent_search_gateway/providers/http.py` | 可选 decoder、有界响应读取与安全 body preview；Task 2 |
| Modify: `src/agent_search_gateway/providers/web/common.py` | `JsonRequester` 兼容新增可选参数；Task 2 |
| Modify: `tests/support/http.py` | 两个 recorder 的可选参数类型保持一致；Task 2 |
| Tests: `tests/providers/web/test_youcom.py` | REST/MCP 协议、映射、失败与端点边界 |
| Tests: `tests/providers/test_http_executor.py` | 解码扩展、真实流读取、关闭、重试、隐私 |
| Tests: `tests/providers/test_registry.py`、`tests/unit/test_config_web_providers.py` | 注册与通用凭据规则 |
| Tests: `tests/runtime/test_runtime_assembly.py`、`tests/runtime/test_new_web_runtime_assembly.py` | 通用 Secret 注入、You.com 装配、配额与生命周期 |
| Tests: `tests/observability/test_provider_diagnostics.py` | MCP 诊断、脱敏、重试与耗时 |
| Tests: `tests/acceptance/test_gateway_workflows.py` | URL admission、Search 隔离失败、Fetch 回退与零付费跳转 |
| Tests: `tests/integration/test_live_new_web_providers.py`、`tests/unit/test_live_smoke_validation.py` | 受控免费 smoke 与离线验证其安全入口 |
| Docs: `README.md`、`config.example.toml`、`CHANGELOG.md` | 免费切换说明、默认 REST 保持、变更说明 |
| Tests: `tests/docs/test_documented_config.py` | 从实际文档提取并解析示例，不只检查字符串存在 |

不修改其他 provider 源码、`observability.py` 公共日志 schema、核心 orchestrator/scheduler、依赖清单或锁文件。只有现有接口无法表达且有失败测试证明时，才重新评估边界；不要顺手重构。

**依赖顺序：** Task 1 → Task 2 → G0 通过 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9。默认注册切换延后到 Task 6，避免 optional 元数据先落地而 Adapter 仍不能工作。

### 本计划固定的内部接口

这些是拟实现的接口，不是已经存在的 API：

- `WebProviderRegistration.supports_optional_api_key: bool = False`，位于现有字段之后，保留既有构造兼容性。
- `YouComAdapter.__init__(*, name: str, api_url: str = "https://ydc-index.io", secret: SecretValue | None = None, http_executor: JsonRequester)`。
- `ResponseDecoder = Callable[[httpx.Response], object]`，定义在 `providers/http.py`，供 `JsonRequester` 与测试替身导入。
- `request_json` 增加三个 keyword-only 可选参数：`response_decoder: ResponseDecoder | None = None`、`max_response_bytes: int | None = None`、`preview_bodies: bool = True`。其他参数和返回值 `object` 保持不变。
- `_youcom_mcp.py` 只提供供应商私有消息处理；网络由 Adapter 的现有 executor 发出。请求 ID 用每次调用生成的字符串，并在该调用的 decoder 闭包中匹配；并发 Search/Fetch 不共用“最后一个 ID”。
- MCP 使用私有 `_MCP_MAX_RESPONSE_BYTES = 8 * 1024 * 1024` 作为初始响应上限。这是本地内存保护，不是供应商限制，不新增 TOML 选项；若 G0 证明合法结果超出此值，再有依据地调整常量及边界测试。
- `search(query: str) -> list[KeywordSearchHit]` 与 `fetch(url: NormalizedURL) -> URLFetchCandidate` 不变。

## 4. 实施任务

每个任务中的多个测试场景应逐项执行 RED → GREEN → REFACTOR，不一次写完大量生产逻辑。先观察因缺少目标行为而失败，再实现最小改动；不要把导入错误、拼写错误或错误 fixture 当成 RED。已有行为的回归测试可能立即通过，记录即可，不故意破坏代码制造失败。

所有命令从本工作树根运行。正常测试保持无真实网络；全量测试前确保当前测试进程未开启 `WEB_SEARCH_RUN_INTEGRATION`。环境按 `CONTRIBUTING.md` 使用 `uv sync --locked --all-groups`；复用已有可用环境，不改共享依赖环境。

### Task 1: 通用可选凭据与 You.com 构造期端点约束

**Files:**

- Modify: `src/agent_search_gateway/providers/registry.py`
- Modify: `src/agent_search_gateway/config.py`
- Modify: `src/agent_search_gateway/runtime.py`
- Modify: `src/agent_search_gateway/providers/web/youcom.py`
- Test: `tests/providers/test_registry.py`
- Test: `tests/unit/test_config_web_providers.py`
- Test: `tests/runtime/test_runtime_assembly.py`
- Test: `tests/providers/web/test_youcom.py`

- [x] **Step 1: Write the failing test**

首先使用合成注册名 `optional` 验证通用能力，不先改变默认 You.com 注册：

| 场景 / 建议测试名 | Setup / Action | 预期 |
| --- | --- | --- |
| `test_optional_web_provider_resolves_without_key` | 注册 required=False、optional=True；启用且不配置 api_key_env；resolve | Secret/env-name 均 None |
| `test_optional_web_provider_resolves_explicit_key` | 显式 env-name 与值 | Secret 注入且 repr 不泄漏 |
| `test_optional_web_provider_rejects_invalid_explicit_key` | 空/非字符串 env-name、未定义或空环境值 | CONFIG_ERROR，不降级 credentialless |
| `test_runtime_injects_optional_secret_only_when_present` | 用捕获 kwargs 的合成 factory 构建 Runtime | 有值才传 secret；无值不传，不按 provider 名字判断 |
| `test_youcom_accepts_exact_keyless_mcp_endpoint` | 直接构造 YouComAdapter，免费 URL、无 Secret | 构造成功、零 HTTP |
| `test_youcom_rejects_conflicting_protocol_credentials` | REST 无 Secret、免费 MCP 有 Secret | ConfigFailure，零 HTTP |
| `test_youcom_rejects_unsupported_official_mcp_urls` | 官方其他 /mcp*、付费/旧 query 路径、不安全变体 | ConfigFailure，零 HTTP |
| `test_youcom_rest_custom_prefix_preserves_requests` | 带 Key 默认/自定义含前缀 REST root | 原方法/path/header/body、Fetch 归一化匹配不变 |

端点用 URL 组件比较：规范化 scheme/host，接受 HTTPS 默认端口与单个结尾斜线；免费地址不得有 userinfo、非默认端口、query、fragment、额外路径或编码路径别名。其他 `api.you.com/mcp*` 直接拒绝。非官方 host 的普通 REST root（包括路径中出现类似文字的代理 URL）仍按原有 REST 语义使用，不能按字符串包含选免费协议。

同时保留 required provider 与 Jina/open_websearch forbidden 规则、禁用跳过凭据校验、未知字段即使禁用也失败等现有测试。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_config_web_providers.py -k optional -v
uv run pytest tests/runtime/test_runtime_assembly.py -k optional -v
uv run pytest tests/providers/web/test_youcom.py -k "keyless or conflicting or unsupported_official" -v
```

预期：当前元数据/解析器不支持 optional，当前 Adapter 拒绝无 Secret 构造或错误处理不满足断言。确认是目标能力缺失。

- [x] **Step 3: Describe the minimal implementation**

```text
generic config resolution:
    keep disabled-provider early return and unknown-option validation
    required = registration.requires_api_key
    optional = registration.supports_optional_api_key
    if not required and not optional:
        reject explicitly present api_key_env; keep old forbidden behavior
    elif api_key_env is present or required:
        validate nonempty name and configured environment value
        build SecretValue using the existing rules
    else:
        leave api_key_env and secret as None

generic runtime:
    keep required-provider missing-secret failure
    if (required or optional) and resolved.secret is not None:
        pass secret to the factory
    retain reserved kwargs, existing executor, quota and capability assembly

YouComAdapter constructor:
    classify exact free endpoint / unsupported official MCP / ordinary REST
    free requires secret is None
    REST requires secret is not None
    violations raise a safe ConfigFailure before any network operation
    retain default REST URL and private REST request/parsing behavior
```

required 优先，避免改变其他声明；只有默认 You.com 最终使用 optional，注册切换留给 Task 6。配置解析器不看 You.com URL，不尝试自动读取 `YDC_API_KEY`。REST 的 Secret 类型在分支内收窄，不向其他 Provider 增加可选构造参数。

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/unit/test_config_web_providers.py tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py tests/providers/web/test_youcom.py -v
uv run mypy src tests scripts
uv run pytest -q
```

预期：新构造/通用凭据测试和全部已有 REST/credentialless 测试通过。

- [x] **Step 5: Refactor (keep tests green)**

仅去除当前条件重复、完善安全错误信息；不引入 credential DSL。重跑本任务定向集；有代码重构则重跑全量测试。

### Task 2: 共享 HTTP 的可选解码、有界读取与安全预览

**Files:**

- Modify: `src/agent_search_gateway/providers/http.py`
- Modify: `src/agent_search_gateway/providers/web/common.py`
- Modify: `tests/support/http.py`
- Test: `tests/providers/test_http_executor.py`
- Reference: `src/agent_search_gateway/observability.py`
- Reference: `tests/support/logging.py`

- [x] **Step 1: Write the failing test**

用真实 HttpJsonExecutor、MockTransport 及能记录读取/关闭次数的 `httpx.AsyncByteStream`：

1. `test_response_decoder_receives_bounded_response`：传入 decoder，从 response body/header 得到自定义结果；默认路径仍使用 JSON。
2. `test_bounded_response_stops_before_buffer_exceeds_limit`：小测试上限、分块响应、无或伪造 Content-Length；限制实际解码后的字节，越界后不再消费后续块。恰等于上限成功，超过一字节失败。
3. `test_bounded_response_closes_on_success_failure_and_cancel`：成功、超限、读取超时/异常与取消都关闭响应；取消原样传播。
4. `test_bounded_error_response_preserves_retry_after`：429/5xx 的巨大/无效正文不盖过状态分类、重试次数或 Retry-After；401/402/403 保持终态。补充“429 headers 已返回、body 永不产出/不结束”场景，断言不读取 body，仍按 Retry-After 重试。
5. `test_private_body_preview_omits_nested_payloads`：`preview_bodies=False`；请求、响应、SSE、畸形 JSON 中含 PRIVATE_*；真实 structured DEBUG formatter 输出安全省略标记/字节数，仍有每次 preview 事件。
6. `test_custom_decoder_failure_is_not_retried`：HTTP 200 后 decoder 抛 ProtocolFailure，只有一次 send；现有 ValueError JSON 解码失败行为不变。
7. `test_bounded_response_deadline_stops_continuous_heartbeats`：流持续提供小块/心跳，仍在配置的单次绝对时限内结束并关闭，按传输超时进入既有有界重试；配额等待不计入这个时限。
8. `test_bounded_response_rejects_unsupported_compression_before_decode`：受限请求使用 Accept-Encoding: identity；非 identity 的成功响应在自动解压前安全拒绝。
9. `test_custom_decoder_emits_one_post_decode_diagnostic`：成功、坏 JSON/envelope、RPC error 与 tool error 各有唯一准确的 provider_response_diagnostic；失败还有安全 http_failed，不能先记业务成功再补失败。
10. 保留 no-redirect、每次 send 的 RateGate permit、每次重试 preview/耗时与正常 prepared_body 日志的现有测试。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/providers/test_http_executor.py -k "bounded_response or response_decoder or private_body_preview or custom_decoder or bounded_error_response" -v
```

预期：当前 executor 缺少可选能力，提前完整缓存/默认预览也无法满足上限与保密断言。测试不连接公网。

- [x] **Step 3: Describe the minimal implementation**

落实第 3 节的三个可选参数，默认值不改变任何现有调用：

```text
request_json:
    obtain response via the existing retry-controlled _request_response
    after HTTP success / outside retry loop:
        if decoder supplied:
            run decoder once, including caller's tool/domain validation
            collect only safe per-call diagnostics / safe failure diagnostics
            emit the deferred provider_response_diagnostic exactly once
            log a safe http_failed on protocol/provider failure, then propagate
            return decoded object on success
        otherwise: preserve response.json() and current diagnostics
    protocol/provider failures remain terminal; do not swallow cancellation

_request_response:
    default max_response_bytes=None: preserve current send/read path
    bounded path:
        set Accept-Encoding: identity for this bounded request only
        send using existing client, stream=True, follow_redirects=False
        non-2xx: close without reading body; preserve status and Retry-After
        2xx: reject non-identity Content-Encoding before decompression
        consume identity raw chunks with running byte count
        before adding each chunk, enforce positive max_response_bytes
        never trust Content-Length as the only limit
        always close live response in finally
        expose bounded content + necessary original headers/status to decoder
        avoid an unbounded decompressor or double-decompression
    execute transport reads within existing retry/timeout budget

body preview at every send outcome:
    preview_bodies=True: unchanged
    preview_bodies=False:
        keep existing upstream_body_preview event and exact known byte counts
        pass no raw request/response body and no prepared_body override
        mark incomplete response using existing field when appropriate
        retain existing fixed omitted-preview representation and truncation fields
```

成功 2xx 响应超限报安全 ProtocolFailure，禁止返回截断结果冒充合法 JSON。受限路径取得非 2xx headers 后即关闭，不等候或读取 body，先保留状态、Retry-After 和已读字节统计；正文未读取以现有 incomplete 字段标记。这样 429 的慢/无穷正文不会被误判为读取超时。受限路径请求 identity 并在解压前拒绝不支持的编码；只在 `aiter_bytes()` 之后检查长度不足以约束 HTTPX 解压器的中间内存。当前 `retry_async` 没有绝对时限，HTTPX 的 request timeout 也不能阻止持续心跳延长流读取。因此仅在 `max_response_bytes` 启用的分支，在获得 RateGate permit 后，用现有正值 `request_timeout_seconds` 的 `asyncio.timeout(...)` 包住 send 和整个读取；不要把配额等待算进去。超出绝对时限转成固定安全信息的 `httpx.ReadTimeout`，交现有 transport retry 处理；`CancelledError` 仍原样传播。REST 默认路径和 `retry.py` 不改，不增加超时配置字段。

对 `response_decoder is not None` 的成功 2xx，`_request_response` 仅保留 http_attempt_completed 等 HTTP 事件，把现有 provider_response_diagnostic 的发出延后到 `request_json` 的解码/校验后。使用私有控制标志，保留已有返回元组中的 attempt、start time、HTTP diagnostics，不新增公共参数。非 2xx 仍在原位置诊断；受限读取在取得 2xx headers 后发生 size/encoding 失败时，也必须发一条安全诊断与失败事件，不漏记或重复。

MCP Adapter 的每次调用 decoder 闭包先解包，再调用对应领域 parser 完成 schema/正文检查；只缓存本调用的安全诊断供现有 `response_diagnostics(response)` 签名读取。executor 在 custom-decoder 路径统一将安全 failure.diagnostics 合入 HTTP 诊断并发事件；不把缓存放到共享实例，不对同一响应再执行一次 decoder。默认 JSON/REST 的 inspector/diagnostics 时机不改。

只给调用方通用开关；公共 executor 不识别 You.com、tool 名或 JSON-RPC 字段。MCP 在后续任务显式传 `preview_bodies=False`，避免日志在 decoder 之前泄漏数据。此选择保留每次请求的现有预览事件和字节统计，不改变其他 Provider 的正文日志。

两个 Recording executor 仅兼容参数签名；不要把它们扩成假 SSE/重试引擎。其返回已解码对象的旧语义保留；新协议测试用真实 executor。

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/providers/test_http_executor.py tests/observability/test_provider_event_logging.py tests/observability/test_body_preview.py -v
uv run mypy src tests scripts
uv run pytest -q
```

预期：受限路径测试通过，全部非 MCP 的 HTTP/日志测试保持通过。

- [x] **Step 5: Refactor (keep tests green)**

最多提取一个私有 bounded-response reader，减少关闭/错误分支重复。不得引入传输框架、公共 MCP 类或新日志 schema；有重构则重跑上述测试和全量集。

### Task 3: 已验证的 MCP JSON Search 请求与领域映射

**Prerequisite:** G0 通过并记录唯一协议版本及 Search schema。

**Files:**

- Create: `src/agent_search_gateway/providers/web/_youcom_mcp.py`
- Modify: `src/agent_search_gateway/providers/web/youcom.py`
- Create: `tests/fixtures/providers/youcom/mcp_contract.md`
- Conditional create: `tests/fixtures/providers/youcom/mcp_search_structured.json`
- Conditional create: `tests/fixtures/providers/youcom/mcp_search_text.json`
- Test: `tests/providers/web/test_youcom.py`

- [x] **Step 1: Write the failing test**

从已有公开 Adapter 调用进入，避免新模块不存在导致测试无法收集：

- `test_mcp_search_posts_only_you_search`：免费无 Key Adapter + 真实 executor/MockTransport；仅向准确免费路径 POST。断言 JSON-RPC ID、协议 headers/meta 与 `name=you-search`，arguments 恰为 `{"query": query, "count": 10}`；禁认证/payment headers和 REST suffix。
- `test_mcp_search_maps_verified_tool_content`：针对 G0 确认的每种成功表示分别用合成完整 envelope，断言 KeywordSearchHit 的 URL/title/snippet 与空正文契约；只证实 text JSON 时也有效。
- `test_mcp_search_accepts_valid_empty_result_list`：合法结果列表为空返回 []；缺少该列表必须失败。
- `test_mcp_search_rejects_bad_candidates_without_losing_good_hits`：复用既有 URL 验证/CandidateCounts；坏单条被拒绝，好记录保留。
- `test_concurrent_mcp_calls_match_their_own_ids`：并发调用、反序完成；两个在途请求 ID 不冲突，不读取共享“最近结果”。
- 有证据才增加文本 JSON 兼容成功测试；`structuredContent` 存在但类型错误不能被文本内容悄悄掩盖。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/providers/web/test_youcom.py -k "mcp_search or concurrent_mcp" -v
```

预期：现有 Search 仍按 REST 发送，MockTransport 捕获错误 path/body 或模型映射失败。

- [x] **Step 3: Describe the minimal implementation**

```text
YouComAdapter.search(query):
    REST: execute existing request/inspector/parser unchanged
    MCP:
        create a unique per-call string rpc_id
        build the fixed you-search request for G0-confirmed protocol
        call existing executor.request_json(
            POST, full free endpoint, stage="search",
            exact protocol headers and JSON-RPC body,
            response_decoder=per-call closure matching this rpc_id,
            response_diagnostics=per-call safe diagnostics callback,
            max_response_bytes=_MCP_MAX_RESPONSE_BYTES,
            preview_bodies=False
        )
        decoder closure unwraps the tool result then calls this Adapter's parser
        parser maps verified rows into KeywordSearchHit before semantic logging
        validate each URL with existing normalize_url(url)
        preserve the original URL string in KeywordSearchHit and CandidateCounts rules
        narrow the decoded object to list[KeywordSearchHit] and return it
```

`_youcom_mcp.py` 负责消息/信封，`youcom.py` 负责供应商结果到领域模型。若 Search 内部结构与 REST 完全一致，可提取同一个小型纯解析函数；有差异则分别选取已证实字段，不让 REST parser 变成宽松的多 schema 猜测器。保留 REST 的空标题/snippet 策略、snippets 双换行合并和候选计数。

若 G0 选择旧版，在这一任务按 G0 的互斥步骤加入最小私有握手；无需新版 session 状态。网络始终由共享 executor 负责。

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/providers/web/test_youcom.py -v
uv run mypy src tests scripts
uv run pytest -q
```

预期：JSON Search 契约通过，REST 请求与解析断言仍通过；无 Key MCP 请求中不出现任何认证头。

- [x] **Step 5: Refactor (keep tests green)**

保持私有 codec 小且聚焦。只复用确认相同的领域解析，不抽象成全 Provider MCP 基类；重跑定向和全量测试。

### Task 4: SSE、JSON-RPC 与工具错误的严格分类

**Files:**

- Modify: `src/agent_search_gateway/providers/web/_youcom_mcp.py`
- Modify: `src/agent_search_gateway/providers/web/youcom.py`
- Create: `tests/fixtures/providers/youcom/mcp_search.sse`
- Test: `tests/providers/web/test_youcom.py`
- Reference: `src/agent_search_gateway/errors.py`
- Reference: `src/agent_search_gateway/providers/diagnostics.py`

- [x] **Step 1: Write the failing test**

逐个增加以下参数化场景，每个断言固定失败类型、一次 tools/call（无业务/协议重试）和安全诊断；若 G0 选择旧版，必要 initialize/通知单独计数：

| 场景 | 预期 |
| --- | --- |
| JSON / SSE Content-Type 带 charset | 按正确媒体类型处理 |
| SSE 心跳、前置合法 notification、LF/CRLF、多行 data | 忽略合法前置内容，拼接多行 data，取得匹配 ID 的完整最终结果 |
| SSE 没有最终响应、data 非 JSON、截断事件、重复冲突最终帧 | ProtocolFailure，不返回部分结果 |
| JSON 根非对象、jsonrpc 错误、id 缺失/错误/布尔类型、result/error 都有或都无 | ProtocolFailure |
| SSE 出现非本请求最终 response | 不接受为结果；按 request-scoped 协议错误处理 |
| HTTP 200 + 合法 JSON-RPC error | ProviderResponseFailure/ExecutionFailure，固定 category/upstream_code |
| error 字段结构错误、code/message 类型错误 | ProtocolFailure；不把坏 error 当业务错误 |
| result.isError=true | ExecutionFailure；tool text 不作为搜索或正文 |
| result.isError 非布尔、非法 structuredContent/content | ProtocolFailure 或固定业务 schema 失败 |
| structuredContent 缺失 + 已确认 JSON text 包装 | 成功解包；空/任意自然语言/多个冲突正文块拒绝 |
| resultType=complete；旧兼容结果未带 resultType | 按选定协议的兼容规则处理 |
| resultType=input_required 或未知类型 | 终态协议/能力失败，不发支付、OAuth 或额外交互 |
| 已知/未知 RPC code，恶意 message/data/content | 已知 code 固定映射，未知映 unknown，PRIVATE_* 不入诊断 |
| 不支持版本、普通 404 | 安全失败；无无限版本探测、无自动初始化或 REST 跳转 |

JSON-RPC 匹配不能把 Python 的 `True == 1` 当作 ID 相等。为简化使用字符串请求 ID，返回值类型和值都要匹配。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/providers/web/test_youcom.py -k "mcp_sse or mcp_envelope or mcp_rpc_error or mcp_tool_error or mcp_result_type or mcp_text" -v
```

预期：仅 JSON 成功路径不能处理 SSE 或严格拒绝新负例。每个失败应定位到目标分类/结果断言。

- [x] **Step 3: Describe the minimal implementation**

```text
decode response using Content-Type:
    JSON: parse exactly one object
    SSE:
        split using standard blank event boundaries
        ignore comments; join consecutive data lines with newline
        parse event data as JSON-RPC
        ignore only valid notifications
        collect a single consistent final response for this request
        reject malformed/truncated/conflicting/missing final envelopes

validate JSON-RPC envelope:
    require jsonrpc 2.0 and exact request id
    require exactly one of result/error
    validate error shape, map code through local fixed allowlist, raise safely
    validate result type and tool error flags
    choose valid structuredContent first
    use only G0-confirmed JSON text representation as fallback

return the validated tool payload to the Adapter
```

不要用搜索 `data:` 或 `results` 子串代替事件/信封解析。不对任意上游 message 调用 DiagnosticText 后直接写日志；只生成固定类别与代码。HTTP 200 下的 protocol/tool failure 不进入网络重试。所有异常继续标记 `provider=youcom`、调用方 `stage=search/fetch`。

SSE 读取已经由 Task 2 限制总字节与 attempt deadline；decoder 不另建无界缓冲或后台流任务。收到多个完全相同最终帧时选择一个；任何互相冲突的最终帧都失败。结果仅在完整有界响应完成后交付，不提前接受第一条通知。

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/providers/web/test_youcom.py tests/providers/test_http_executor.py -v
uv run mypy src tests scripts
uv run pytest -q
```

预期：JSON/SSE 成功与所有协议/工具失败类别通过；没有假空结果或额外网络请求。

- [x] **Step 5: Refactor (keep tests green)**

合并 JSON 和 SSE 最终信封的公共验证，保持 SSE framing 与领域映射分离；不支持任意工具、订阅或 MRTR。重跑定向和全量测试。

### Task 5: MCP Extract 正文映射

**Prerequisite:** G0 的 Extract 成功 schema 已确认。

**Files:**

- Modify: `src/agent_search_gateway/providers/web/youcom.py`
- Modify: `src/agent_search_gateway/providers/web/_youcom_mcp.py`（仅共享 tool 解包需要时）
- Conditional create: `tests/fixtures/providers/youcom/mcp_fetch_structured.json`
- Conditional create: `tests/fixtures/providers/youcom/mcp_fetch_text.json`
- Test: `tests/providers/web/test_youcom.py`

- [x] **Step 1: Write the failing test**

- `test_mcp_fetch_calls_you_extract_with_url_only`：`fetch(normalize_url(...))`；arguments 恰为 `{"url": str(target)}`，不含 formats、urls、timeout，也不调用 you-contents。
- `test_mcp_fetch_returns_verified_nonempty_body`：JSON/SSE 同一内容，`raw_content` 与 `content` 保持领域契约。
- `test_mcp_fetch_checks_returned_url_when_present`：已归一化相等成功，不同 URL 失败；schema 允许省略 URL 时仍按请求目标关联。
- `test_mcp_fetch_rejects_empty_or_nonbody_result`：空白正文、错误类型、Search 摘要、isError 文本不能伪装页面正文。
- 复用 Task 4 的 malformed envelope/RPC/tool error 样例验证 fetch stage。
- REST 的 `/v1/contents`、`urls=[target]`、`formats=["markdown"]`、空正文/无匹配测试保持不变。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/providers/web/test_youcom.py -k mcp_fetch -v
```

预期：当前 Fetch 尚未走免费工具或不能解析其已验证正文结构。

- [x] **Step 3: Describe the minimal implementation**

```text
YouComAdapter.fetch(target):
    REST: preserve existing request and _parse_fetch semantics
    MCP:
        call the same private tools/call path with you-extract and url only
        decoder closure validates protocol, then calls this Adapter's fetch parser
        parser reads the exact body field confirmed in mcp_contract.md
        if optional returned URL exists: use normalized_match
        require nonempty page body before semantic diagnostics are emitted
        parser returns URLFetchCandidate(raw_content=body, content=body)
        narrow and return the executor's decoded candidate
```

只复用匹配和失败分类，不能把 REST 的数组包装硬套到 Extract。失败使用既有 empty/no_match/invalid_envelope 等有限语义，不尝试增加免费不支持的参数“补救”。

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/providers/web/test_youcom.py -v
uv run pytest -q
```

预期：MCP 与 REST Fetch 全部通过；任何免费失败均未访问 REST。

- [x] **Step 5: Refactor (keep tests green)**

Search/Fetch 共用消息发送/解包，但保留各自领域转换函数；重跑定向和全量测试。

### Task 6: 切换 You.com 注册声明并验证真实 Runtime

**Files:**

- Modify: `src/agent_search_gateway/providers/defaults.py`
- Test/Modify: `tests/providers/test_registry.py`
- Test/Modify: `tests/unit/test_config_web_providers.py`
- Test/Modify: `tests/runtime/test_new_web_runtime_assembly.py`
- Reference: `src/agent_search_gateway/runtime.py`

- [x] **Step 1: Write the failing test**

使用已有 `_ClientFactory`、`_CountingAsyncClient`、`_config` 和真实 `resolve_config → Runtime.build`：

- `test_youcom_mcp_runtime_builds_without_secret`：免费 URL，无 api_key_env；即使环境存在 YDC_API_KEY 也构造成功、不携带 Key。
- `test_youcom_mcp_runtime_shares_adapter_executor_and_rate_gate`：Search/Fetch 是同一个对象；只有一个 You.com executor/client；共享 quota/RateGate；不在 build 阶段发网络。
- `test_youcom_runtime_rejects_invalid_protocol_credentials_before_http`：默认 REST 缺 Key、免费 MCP 显式配 Key、空环境值、未知/不支持 MCP URL，CONFIG_ERROR 且请求数组为空。
- `test_youcom_mcp_runtime_obeys_capability_switches`：search-only/fetch-only/both/disabled；禁用不验证凭据，不构造 You.com client。
- `test_youcom_mcp_runtime_closes_client_once`：两次 runtime.aclose 仍只关闭一次。
- `test_youcom_mcp_search_and_fetch_share_concurrency`：复用现有事件同步模式，不用墙钟 sleep。
- 注册只出现一个 youcom，能力仍 search/fetch；optional 注册集合只有 youcom，纯 forbidden 仍只有 jina/open_websearch。

关键回归：`test_new_keyed_provider_requires_named_environment_value` 目前把 youcom 列为解析阶段必填。将“必须显式配置 env-name”的参数集限制为仍 required 的 provider；You.com 的默认 REST 缺 Key 断言迁到本任务真实 Runtime，**不能直接删掉缺 Key 失败要求**。显式 env-name 为空/未定义的 You.com 仍在通用解析阶段失败。其他 keyed 参数组保留 You.com 的 REST 覆盖。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/runtime/test_new_web_runtime_assembly.py -k youcom_mcp -v
uv run pytest tests/providers/test_registry.py -v
```

预期：默认注册仍要求 Key，免费 Runtime 无法构造。

- [x] **Step 3: Describe the minimal implementation**

只把默认 You.com 注册改为 `requires_api_key=False, supports_optional_api_key=True`；使用 Task 1 已有的通用解析/注入。保持注册顺序、capabilities、allowed_config_keys 不变。

更新测试中“not requires_api_key 等于禁止 Key”的断言，明确区分 optional 和 forbidden；不要全局放宽 keyed provider 的校验。测试 helper 创建 MCP 配置时必须省略 Key，而不是让现有 `_provider_config("youcom")` 自动塞入 TEST_WEB。

成功 Runtime 的关闭保持现有生命周期。构造失败测试若已创建 client，按现有测试模式在 finally 关闭测试拥有的 client；不借此扩大为整个 Runtime 构建回滚重构。

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py tests/runtime/test_new_web_runtime_assembly.py -v
uv run mypy src tests scripts
uv run pytest -q
```

预期：免费配置可构造、错误配置联网前失败，REST/Jina/open_websearch 和其他 provider 行为保持。

- [x] **Step 5: Refactor (keep tests green)**

仅整理测试参数集，保留四家新 provider 的现有默认/自定义 root、Secret、capability 与关闭测试；重跑定向和全量测试。

### Task 7: 重试、日志、候选计数与外层回退验收

**Files:**

- Test/Modify: `tests/observability/test_provider_diagnostics.py`
- Test/Modify: `tests/acceptance/test_gateway_workflows.py`
- Reference: `tests/providers/test_http_executor.py`
- Reference: `tests/support/web_diagnostics.py`
- Reference: `tests/support/logging.py`
- Modify only if a new test exposes a defect: `providers/web/youcom.py`、`providers/web/_youcom_mcp.py`、`providers/http.py`（源目录同上）

- [x] **Step 1: Write the failing test / missing composition regression**

用真实 YouComAdapter、HttpJsonExecutor、MockTransport、注入 sleep/ManualClock 和真实结构化 DEBUG 日志验证：

| 场景 | 必须断言 |
| --- | --- |
| 408/429/5xx → 成功或耗尽 | 既有 bounded attempts；每次 send 获取 permit；Retry-After 优先；elapsed_ms 和 preview 次数正确 |
| 401/402/403、RPC error、isError、坏 envelope | 终态；没有新的内部重试环；每个已取得 HTTP response 的调用有且仅有一条准确 provider_response_diagnostic，区分 HTTP/RPC/tool/protocol；失败另有安全 http_failed |
| connect/TLS/DNS 类 TransportError、ReadTimeout/ReadError | 既有分类及重试预算；流响应关闭 |
| PRIVATE_AUTH / PRIVATE_QUERY / PRIVATE_URL / PRIVATE_SESSION / PRIVATE_BODY 注入各层 | 格式化 DEBUG 日志、异常信息、诊断字段均不含哨兵；不隐藏 body-preview 事件 |
| Search 一好一坏候选 | provider_candidates_summary 的 received/accepted/rejected 精确 |
| MCP Search 成功后 url-fetch | 原 URLStore admission、judge/safety/clean 流程与正文状态成立 |
| MCP Search 失败、另一已配置 Provider 成功 | 其他搜索结果仍交付；Search 是已有 fan-out，不新增顺序回退算法 |
| MCP Fetch 配额/工具失败 | 真实 FetchScheduler 按既有优先级调用已配置的 Tavily 等后继 Provider |
| 失败与重试的全部请求 | You.com host/path 仅 api.you.com/mcp/free；零 ydc-index.io、付费 MCP、you-contents；无凭据或 payment header |
| 302 Location 指向 REST 或付费 MCP | 不跟随；安全失败，不向 Location 发请求 |

可在既有 `_NewWebWorkflow` 测试 helper 中加入显式 MCP 设置，或新增一个很小的 MCP helper；不修改 production orchestrator。MCP helper 必须使用有限正值的 `request_timeout_seconds`，不能照搬旧 MockTransport helper 的 `RetryPolicy(1, 0, 1, 0)`（最后一个 0 在新绝对时限下会立即超时）；REST 测试原配置不改。日志不得使用会过滤 preview 的测试 helper 掩盖问题，使用 `structured_test_logger` 和完整渲染结果。

- [x] **Step 2: Run tests and inspect the first unmet assertion**

```bash
uv run pytest tests/observability/test_provider_diagnostics.py -k youcom_mcp -v
uv run pytest tests/acceptance/test_gateway_workflows.py -k youcom_mcp -v
```

预期：前面已实现的组合行为可能直接 PASS；这属于必要验收，不人为制造 RED。任何失败都先定位为可重复的最小场景再修复。

- [x] **Step 3: Describe the minimal implementation**

协议错误只在私有 codec 做有限分类，未知 code 映 unknown；不复制 HTTP retry。若 raw preview 泄漏，修正 Task 2 的通用 opt-in 路径，不对日志字符串做供应商专属事后替换。若回退失败，先检查 Adapter 是否错误返回 []/空候选掩盖失败，不为本功能新增 orchestrator 分支。

`ProtocolFailure` 是 `ExecutionFailure` 的子类，捕获/分类顺序从具体到一般。保留 provider/stage，避免解码器抛出带原始响应文本的异常。

- [x] **Step 4: Run tests to verify they pass (and full suite)**

```bash
uv run pytest tests/providers/test_http_executor.py tests/providers/web/test_youcom.py tests/observability/test_provider_diagnostics.py tests/acceptance/test_gateway_workflows.py -v
uv run pytest tests/runtime/test_rate_gate.py tests/runtime/test_quota_manager.py tests/orchestrators tests/scheduler -q
uv run pytest -q
```

预期：离线组合验收通过；免费失败从未触达任何 You.com 付费接口。

- [x] **Step 5: Refactor (keep tests green)**

仅提取重复 fixture/断言，保留测试覆盖真实生产边界；不新增生产测试专用方法。修复或重构后重跑定向和全量集。

### Task 8: 增加独立、定向、无 Key 的可选 Live Smoke

**Files:**

- Modify: `tests/integration/test_live_new_web_providers.py`
- Modify: `tests/unit/test_live_smoke_validation.py`
- Reference: `tests/fixtures/providers/youcom/mcp_contract.md`

- [x] **Step 1: Write the failing test**

先离线测试新的小型 smoke helper：

- `test_youcom_mcp_smoke_ignores_existing_ydc_key`：环境设 PRIVATE_AUTH，但 helper 固定免费地址、Secret=None、认证头为空。
- `test_youcom_mcp_smoke_blocks_unexpected_destinations`：请求 observer 在发送前拒绝任何其他 host/path、认证/payment headers；不跟随重定向。
- `test_youcom_mcp_smoke_uses_search_url_for_extract`：通过 fake transport 输入 Search 成功结果，只抓取一个合法返回 URL，工具调用次数各一。
- `test_youcom_mcp_smoke_reports_contract_failure_safely`：空/非法结果、配额、外部页面失败，安全输出 FAIL 分类；无真实 query、URL、正文、异常文本。
- 默认未设置现有 `WEB_SEARCH_RUN_INTEGRATION=1` 时 integration case SKIP，CI 不访问端点。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_live_smoke_validation.py -k youcom_mcp -v
```

预期：现有 helper 只支持 REST 并读取 YDC_API_KEY，尚无独立免费 smoke 路径。

- [x] **Step 3: Describe the minimal implementation**

在现有 integration 模块增加单个 `test_live_youcom_mcp_search_then_extract`：

1. 保留模块已有 opt-in；不把 MCP 加入 `_PROVIDERS` 或伪装成新注册名。
2. 直接构造 YouComAdapter，固定免费地址、无 Secret、真实共享 executor。使用最多一次尝试的 smoke 策略，限制为一次 Search 和一次 Extract 业务调用；旧版必要握手另计。
3. 在发送前验证目标、协议相关 headers 与无凭据；observer 只读状态/头/计数，不能复用会 `await response.aread()` 的旧 `_Observation.response` 绕过 Task 2 的流上限。
4. 不把正文打印出来；输出 provider=youcom、protocol=mcp、stage、固定 PASS/FAIL/SKIP、safe category、结果数/正文长度、elapsed_ms、版本和请求数。
5. Search 未成功则不执行 Extract；第三方内容不可访问与协议错误分开报告。用 `pytest.fail(..., pytrace=False)` 等既有安全模式，不打印异常 repr。

不要要求新 API Key、增加新的集成开关，或修改其他 provider 的既有 REST smoke。

- [x] **Step 4: Run tests / optional live**

离线：

```bash
uv run pytest tests/unit/test_live_smoke_validation.py -v
uv run pytest tests/integration/test_live_new_web_providers.py -k youcom_mcp -v -rs
uv run pytest -q
```

预期：离线测试 PASS；未开启真实网络的定向 integration 结果为明确 SKIP。

本项目用户已允许定向 smoke，实施完成后可沿用授权执行一次：

```bash
WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_new_web_providers.py -k youcom_mcp -v -rs -s
```

预期：只选中免费 MCP 新测试，不触发原参数化 REST 用例；分别记录 Search/Extract PASS、FAIL 或 SKIP。不要用 `-k youcom` 作为免费验证命令，因为它也会选中现有付费 REST smoke。若当前连接需要代理，只按用户规则给本次网络命令临时恢复代理，不修改共享代理配置。

- [x] **Step 5: Refactor (keep tests green)**

只整理独立 smoke helper 的重复逻辑；重跑离线验证。不要为可选 smoke 再重复消耗免费额度；若失败先根据已记录分类定位。

### Task 9: 更新可执行配置示例、说明与最终验收

**Files:**

- Modify: `README.md`
- Modify: `config.example.toml`
- Modify: `CHANGELOG.md`
- Test/Modify: `tests/docs/test_documented_config.py`
- Reference: `CONTRIBUTING.md`

- [x] **Step 1: Write the failing test**

`test_documented_youcom_mcp_example_builds_without_credentials` 从实际 README 的专用 TOML fence 和 config.example 的注释替换块提取配置、解析后构建真实 Runtime（拒绝所有 HTTP 的 MockTransport）。断言免费 URL、无 api_key_env、原 provider 名与能力开关。不要在测试中手写另一份与文档无关的配置。

保留 REST 默认示例与 disabled 状态的现有断言；文档两段配置是同一表的替换方案，不是同一文件重复定义。保留旧 provider 数量、排序与现有限流示例。避免给纯说明措辞堆砌脆弱字符串测试。

- [x] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/docs/test_documented_config.py -k youcom -v
```

预期：当前文档没有免费切换示例，无法提取并构造目标 Runtime。

- [x] **Step 3: Describe the minimal implementation**

- README 在 You.com 现有章节增加两种互斥用法；保留默认 REST、YDC_API_KEY、Contents 原有语义。
- 免费替换示例使用相同 `[web_providers.youcom]`、免费完整 URL，并明确**删除 api_key_env 行**。已有环境 Key 可以保留，不会被自动使用。
- 修正文档“adapter 总会追加 Search/Fetch path”的泛化描述：REST 使用 root，免费 MCP 使用完整 endpoint。
- 不把 100/day 写为本地 rate_limits 默认；说明它是供应商当时公开的免费配额，可能变化，Gateway 的限流不等价其余额或跨进程配额。
- config.example 保留当前 REST 默认项、enable_search=false/enable_fetch=false、既有 REST rate_limits；在该表附近给出完整注释替换块。切换示例不把 REST 的 10 QPS 宣称为 MCP 官方限制。
- 说明 MCP 失败交给已配置 Provider 的既有流程，不自动使用 You.com REST。无需用户设置新 mode、注册新名字、部署 MCP 服务或安装 SDK。
- 增加 Task 8 的精确 `-k youcom_mcp` 命令、无 Key 说明，以及默认离线/SKIP 规则。
- CHANGELOG 的 Unreleased 增加这项用户可见功能，不改版本号、不准备发布。

- [x] **Step 4: Run test to verify it passes (and required final checks)**

```bash
uv run pytest tests/docs/test_documented_config.py -v
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run pytest -q
uv run python scripts/build_docs.py
```

预期：示例可解析和装配，格式/lint/typecheck/默认离线全量测试/docs build 通过。这些是 CONTRIBUTING.md 的既有检查；本功能无打包/依赖变动，不为此追加发布或部署。

- [x] **Step 5: Refactor / inspect final diff**

检查最终 diff 只包含计划内文件和有证据的必要小改动。修正配置/文档测试发现的问题后仅重跑受影响检查；避免无意义重复线上 smoke。记录实施提交、离线测试结果、G0 版本与字段证据、live 的明确状态及剩余问题。

## 5. Spec 覆盖与完成判据

| Spec 要求 | 对应任务 / 验收 |
| --- | --- |
| Architecture D1：同一 provider、精确 URL、默认 REST 与自定义 root | Task 1、3、5、6、9 |
| D2：通用 optional Key，错误启动前拒绝，其他 provider 不变 | Task 1、6 |
| D3：固定 you-search / you-extract，参数和无认证要求 | G0、Task 3、5、8 |
| D4：实际版本、JSON/SSE、匹配 id、必要兼容路径 | G0、Task 2–4 |
| D5：真实 schema、候选拒绝、非空正文与 URL 匹配 | G0、Task 3–5、7 |
| D6：共享配额、RateGate/Retry、无付费回退 | Task 2、6、7 |
| D7：共享 HTTP 微扩展，日志安全 | Task 2、4、7 |
| Error matrix：HTTP、transport、decode、envelope、tool、quota、版本/session | Task 1–2、4–7；旧 session 行仅在 G0 选旧版时适用 |
| Testing：注册、Runtime 生命周期、能力开关、文档可运行 | Task 1、6、9 |
| Testing：无网络默认、准确 PASS/FAIL/SKIP、限量 live | G0、Task 8–9 |

实施完成时逐项勾选：

- [x] G0 有准确端点的协议及两个工具结果证据；新旧路径的代码与测试选择一致。
- [x] 旧 REST 请求、错误语义和自定义代理 root 回归通过。
- [x] 免费模式构造/请求无 Key；显式错误凭据配置在任何网络操作前失败。
- [x] MCP JSON/SSE、严格 envelope、工具错误、候选与正文语义测试通过。
- [x] 受限读取在缓冲增长前检查上限，响应在成功/失败/超时/取消时关闭，attempt 有界。
- [x] 重试只沿用共享机制，Retry-After/RateGate/并发配额/elapsed_ms 与日志预览事件有效。
- [x] 完整结构化日志和异常中没有 PRIVATE_*、上游任意 message/text、原始 SSE 或 session。
- [x] 真实编排器验收证明其他结果和正常回退仍有效，零 You.com 付费跳转。
- [x] 文档明确如何替换同一个配置表；默认 REST 和 provider 注册数量保持。
- [x] CONTRIBUTING.md 的离线最终检查通过；live 的运行状态和失败原因准确记录。

### 计划自审记录

- 文件职责和路径已与 `1ba9cc3` 工作树核对。
- 凭据可选类型、`ResponseDecoder`、三个 HTTP 可选参数、固定 provider/stage 和领域返回类型在任务间保持一致。
- 注册切换延后到功能就绪，避免 metadata 与构造器要求不一致。
- 已补足“只加事后 decoder”遗漏的流上限、关闭与原始 body preview 问题。
- 未把旧免费路径的实例当新路径实测，也未编造 Extract 成功 fixture；每个工具的 structured/text 表示按证据条件化。
- 自审和独立审查发现的绝对时限、非 2xx 慢正文、压缩读取、解码后唯一诊断、真实 URL helper 和动态 RPC ID 问题已纳入相应步骤。
- 原计划提交仅包含实现与验证准备；以上保留计划自审记录，后续执行情况见第 6 节。

## 6. 本次实施与验证记录

### 完成状态

Task 1–9 的实施与验收步骤均已完成并勾选；Task 8 的唯一一次线上 smoke 保留了下面列出的验证限制。用户随后明确授权使用 Bash 写入，本次在当前 SHA 校验与目标路径限定下完成了原 `README.md`、`config.example.toml` 及对应来源测试的更新。独立指南、配置片段和 Changelog 保留为配套交付，最终离线门禁全部通过。

配套交付均已提交；原 README/config.example 与独立指南/片段四个实际来源都经过解析及真实 Runtime 装配验证：

- `docs/youcom-free-mcp.md`：免费 MCP 使用说明。
- `config.youcom-free.fragment.toml`：同一个 `youcom` 配置表的无凭据替换片段。
- `docs/designs/plans/20261121-youcom-free-mcp-pending-docs.patch`：原文档及相应测试的补丁，已在本次授权收尾中应用；保留原文件名作为实施过程记录。应用前通过 `git apply --check`。

### 实施提交

| 阶段 | 提交 | 结果 |
| --- | --- | --- |
| Task 1 | `5bb5825` | 通用 optional credential 元数据、配置和 Runtime 注入；You.com 构造期模式/凭据约束。 |
| Task 2 | `3b58787` | 共享 HTTP opt-in decoder、有界读取、绝对 attempt 时限、关闭和安全预览。 |
| Task 3 / G0 记录 | `05cad23` | 固定版本 JSON MCP Search、合成夹具和已验证契约。 |
| HTTP 诊断补修 | `3489091` | 已取得响应后的部分读取失败、超时、取消保留准确且唯一的安全诊断。 |
| Task 4 | `3bfd357` | JSON/SSE、严格 RPC 信封和工具错误分类。 |
| Task 5 | `d8db545` | url-only Extract 与精确 Markdown/可选 URL 映射。 |
| Task 6 | `62ecad8` | 默认 You.com optional 注册；真实 Runtime、能力开关、生命周期和共享并发验收。 |
| 独立审查补修 | `6212da1` | 官方主机 IDNA 点号别名拒绝、Search 根结构 invalid_envelope、严格 UTF-8 顶层 JSON 解码。 |
| Task 7 | `c01969a` | 23 个真实 HTTP/日志/URLStore/FetchScheduler 组合验收用例，无新增生产改动。 |
| Task 8 | `bacb285` | 独立 keyless smoke、安全 request guard、默认 SKIP 和 35 个新增离线验证用例。 |
| Task 9 安全替代交付 | `f9cbe6f` | 指南、配置片段、4 个实际文档来源/环境组合测试、Changelog 及待应用补丁。 |
| Task 9 原路径收尾 | `70560fc` | 原 README/config.example 更新；四个实际来源 × 有/无环境 Key 共 8 个 Runtime 用例；全部最终离线门禁通过。 |

Task 7 依据现有领域契约验收：MCP Extract 同时提供 `content` 与 `raw_content`，在 judge / safety 后按既有条件跳过 clean；Tavily raw-only 回退路径实际执行 clean。没有为符合测试假设而改变编排器。

### G0 的准确端点证据

- 准确地址为 `https://api.you.com/mcp/free`，唯一选定版本为 `2026-07-28`。
- 共完成四次无认证 POST：`server/discover`、`tools/list`、一次 `you-search` 和一次 `you-extract`；均为 HTTP 200 / JSON，两个工具成功结构都已确认。
- Search 使用 `query` 与 `count=10`；Extract 仅使用单个 `url`。使用到的 inputSchema 属性没有 `x-mcp-header` 映射要求。
- Search 对应 `results.web`，摘要优先为 `contents.highlights`，再使用 schema 声明的 `snippets`。Extract 正文对应 `output.contents.full_page.markdown`，可选返回 `output.url` 需归一化匹配请求。
- 两个工具实测均同时返回 `structuredContent` 与单个 JSON text block。单包装夹具是对已证实表示的合成裁剪；SSE、异常和空结果夹具为规范派生的离线用例，没有宣称线上观察到 SSE。
- 采用现代 per-request metadata 路径；旧版 initialize/session/recovery 分支不适用，未开发或假称验证旧版路径。
- 可复核字段、版本、来源和合成夹具说明保存在 `tests/fixtures/providers/youcom/mcp_contract.md`；没有持久化真实查询、页面正文、凭据或 session。

### 最终离线门禁

检查使用当前工作树的 `PYTHONPATH=src`，复用主项目现有虚拟环境并使用 `uv run --no-sync`，显式清除 `WEB_SEARCH_RUN_INTEGRATION`。没有同步或安装共享依赖。

| 检查 | 结果 |
| --- | --- |
| `pytest tests/docs/test_documented_config.py -v` | 30 passed；包含 README/config.example/独立指南/片段四个实际来源 × 无 Key/已有但不用的环境 Key，共八个真实 Runtime 用例。 |
| `ruff format --check src tests scripts` | 234 files already formatted。 |
| `ruff check .` | 通过。 |
| `mypy src tests scripts` | 231 source files，通过。 |
| `pytest -q` | 3967 passed，19 skipped。 |
| `python scripts/build_docs.py` | 退出码 0。 |
| 补丁应用前的 `git apply --check` | 退出码 0；随后在 SHA 复核和目标路径限定下完成应用。 |

这些结果属于已提交的原路径收尾内容；原 README/config.example 与配套指南/片段的全部来源用例均已实际执行。此次收尾没有追加线上 smoke，线上记录仍按下面的原始观测保留。

### Task 8 唯一一次线上 smoke

仅选中 `test_live_youcom_mcp_search_then_extract`，使用 `-k youcom_mcp`；运行一次，pytest 退出码为 1。未调用原 REST smoke，也未再次执行线上 smoke。

| 阶段 | 实际状态 | 已发 HTTP 请求 | 安全观测 |
| --- | --- | --- | --- |
| Search | PASS | 1 | 10 hits，2235 ms，协议 2026-07-28。 |
| Extract | FAIL / request_guard | 0 | 发送前被检查器拦截，2 ms；未观察到 Extract 服务响应。 |

离线 A/B 测试复现了 Search 响应设置 Cookie 后同一私有 HTTPX client 向 Extract 自动携带 Cookie、从而触发 guard 的情况。smoke 已在两步之间清理其专用 client 的 cookie jar；显式注入 Cookie 仍被 guard 拒绝。实际线上触发的 header 没有记录，因此 Cookie 是可复现的推断原因，不能写成已证实的线上原因。修正后 52 个 smoke 离线验证通过，但未再次发送线上请求。前述 G0 Extract 成功属于独立的前置契约探测，不替代本轮集成 smoke 尚未观察到 Extract 响应的事实。

默认未开启集成开关时，定向 smoke 结果为 1 skipped / 8 deselected。

### 工具限制与文件位置偏差

- Task 1 的既有 `tests/runtime/test_runtime_assembly.py` 普通编辑被内容保护器拒绝，因此使用新的 `tests/runtime/test_optional_credentials.py` 覆盖同一通用凭据装配行为；保留并运行原模块回归。
- Task 2 使用 `tests/providers/test_bounded_http_executor.py` 独立承载有界路径测试，避免继续扩大原有 HTTP 测试模块。
- Task 9 最初通过专用 edit 对 README 普通段落及 config.example 普通注释的 SHA 保护编辑被拒绝，错误为 `Secret-looking content is blocked from edit`，因此先提交了替代说明与待应用补丁。用户随后明确授权 Bash 写入；本次按该授权完成原文档更新，范围仅限指定工作树内的三个计划文件。
- 专用 `apply_patch` 不提供 `expected_sha256` 参数。本次使用用户明确授权的 Bash/Python 写入操作：先用专用 read 获取当前 SHA，核对 README/config/test/patch 四个 SHA，限定补丁只改三个计划目标，执行 apply-check，并在实际应用前再次核对全部 SHA。没有更改服务器配置或其他工作区。

以下为本次授权收尾前核对的原文件 SHA-256，作为历史基线保留：

- `README.md`：`05972defb92a15f2552e0a91daf290c77bfed64a24e9a9006619ec6cace4a649`。
- `config.example.toml`：`883dd2c3ac0ace03463d1e7d5eab5eb1f576135e411feb561602b77ff41100e3`。

### 原计划收尾结果

原文档及测试更新已提交为 `70560fccf7346b672292e581c52a2761339df0a9`，原先两项受阻工作均已完成。最终文档测试 30 passed、完整离线测试 3967 passed / 19 skipped，格式、lint、类型检查及文档构建全部通过。默认 REST、disabled 状态、provider 数量/顺序和既有限流断言保留且通过；没有改动生产代码、依赖或版本。Task 8 的线上验证限制仍按实测记录保留。


## 7. 贡献者 PR 前复核（2026-11-21）

### 审阅修复

独立审阅在原实现 `f5cc5e3` 上发现两项问题，修复提交 `eb509b7` 已保存在原分支 `design-youcom-free-mcp-20261121`：

- 免费 MCP 请求原先会继承 HTTPX 客户端的默认 headers、auth、cookies 和查询参数。共享执行器新增默认保持兼容的 `inherit_client_defaults` 选项；仅免费 MCP 关闭继承，独立构造请求并显式禁用客户端认证。仍使用同一个客户端、连接池、RateGate 和重试策略；不修改共享客户端状态。Smoke 中手动清理 cookie 的代码已移除，验证直接经过生产路径。
- 读取超时后的响应关闭原先可能继续等待，阻塞重试、回退或调用者取消。发送、读取及异常清理现在使用同一次尝试的绝对截止时间，并保留原始读取异常和已读取字节数。预算已耗尽或外部取消时仍启动关闭，但只允许即时清理；可取消的异步关闭在下次挂起时退出，不创建后台关闭任务。此时不保证底层传输已经完成异步资源释放。

新增四个复现用例先观察到失败，修复后三个定向测试文件共 460 passed。独立复核另运行八个定向用例，全部通过，确认上述两项均已关闭；未发现新的阻断问题。

### 发布历史

CI 的 URL 检查会扫描每个新增提交，原实现历史中的合成用户信息 URL 也会被判为非示例凭据 URL。测试现以明确的 URL 组件构造这些负例，运行时拒绝行为及 `PRIVATE` 泄漏断言保留，扫描器及其规则不变。

原实现分支及全部提交保留。另从 `upstream/main` 创建 `pr-youcom-free-mcp-20261121`，汇总已完成的实现和审阅修复，作为贡献者 PR 分支；没有重写原分支历史。

### 发布分支最终验证

验证复用现有开发环境，设置 `PYTHONPATH=src`，使用 `uv run --no-sync`；默认测试显式关闭线上集成开关。

| 检查 | 结果 |
| --- | --- |
| `ruff format --check src tests scripts` | 234 files already formatted |
| `ruff check .` | 通过 |
| `mypy src tests scripts` | 231 source files，通过 |
| `pytest -q` | 3971 passed，19 skipped |
| `python scripts/build_docs.py` | 退出码 0 |
| 定向免费 MCP live smoke：`-k youcom_mcp -v -rs -s` | 1 passed，8 deselected |

本次最终线上验证在移除 smoke 手动清理 cookie 后执行，准确端点和无凭据 guard 保持启用：

| 阶段 | 状态 | HTTP 请求数 | 安全观测 |
| --- | --- | --- | --- |
| Search | PASS | 1 | 10 hits，2226 ms，协议 2026-07-28 |
| Extract | PASS | 1 | 非空正文 2931 字符，1391 ms |

这次完整 Search→Extract 成功补齐了第 6 节所记录的旧 Task 8 线上验证缺口；原始历史观测保留，不追溯改写为成功。此次未运行 REST 线上用例。

