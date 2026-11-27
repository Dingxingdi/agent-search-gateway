# MCP Server (STDIO and Streamable HTTP) Implementation Plan

**Goal:** 在现有发行包内增加可选 MCP 入口，提供 STDIO 和 Streamable HTTP 两种传输；复用同一个 daemon，保持现有 CLI、核心业务、配置和 JSONL 合同，交付可离线验证的最小接入层。

**Architecture:** `docs/designs/architectures/20261127-mcp-server.md`

**Error handling:** `docs/designs/error-handlings/20261127-mcp-server.md`

**Testing:** `docs/designs/testings/20261127-mcp-server.md`

---

## 1. 执行基线与边界

- 工作树：`/home/agent/agent-search-gateway/.worktrees/design-mcp-server-20261127`。
- 分支：`design-mcp-server-20261127`；计划依据为 spec 提交 `4f499e80f1be64fb8baacf0e7f8a3d0616f571b2`，业务代码基线为 v3.0.0 / `dabfbbc`。
- 本文是待执行计划；复选框不代表已实现或测试通过。此阶段只提交本计划，不修改 spec、生产代码、依赖锁或版本号。
- 按任务顺序推进。每个场景先观察预期断言失败，再实现最小行为、验证和重构；一个任务内的参数化场景也逐组迭代，不先完成全部实现。
- 新模块尚不存在时，先通过明确的包/入口可用性断言建立最小骨架，再进入行为测试。导入错误、语法错误或测试环境缺依赖不算有效的行为 RED。
- 实施前重新确认该工作树及适用指令；不得使用开发者的 daemon、缓存、配置、凭据或真实 provider。此计划中的验收均可离线完成，测试拥有的 Unix socket 和 loopback TCP 不属于外部网络依赖。
- 保持四个新增生产文件的边界。若已核验 SDK/平台行为仍导致核心文件必须改动，先记录具体阻碍并回到设计决策，不能借实现方便扩展范围。

### 1.1 已核验的复用点

下列路径均相对本工作树；行号描述计划基线，实施时以名称定位。

| 既有接口 | 已有行为 | MCP 的使用方式 |
|---|---|---|
| `src/agent_search_gateway/models.py:107–174` | 五个业务 Request、`SuccessResponse(text)`、`ErrorResponse(error, message)`；scope 为 `web/paper/all` | 直接构造/接收现有类型，不发送 `ShutdownRequest` |
| `src/agent_search_gateway/protocol.py:262–291` | `send_request(socket_path, request, *, response_timeout_seconds=None)`；每次新连接并在 finally 关闭 | 每次接受的业务调用至多一次 RPC，不传新增 deadline、不重试 |
| `src/agent_search_gateway/cli.py:90–136` | query/prompt/selectors/focus 的边界规范化 | 在新 adapter 中复现这小段语义；不导入 CLI，不复用会补换行的 CLI 输出函数 |
| `src/agent_search_gateway/paths.py:7–31` | `RuntimePaths.default()/from_home()` | 生产使用 default；测试注入隔离 paths / HOME |
| `src/agent_search_gateway/request_ids.py:11–45` | `ResultKind`、`validate_request_id`、`result_filename` | 验证来源工具对应的 `{kind}-{8位小写十六进制}.jsonl` |
| `src/agent_search_gateway/result_writer.py:147–205` | 直接子文件、完整写入后返回绝对路径；空文件有效；mixed 保留既有顺序 | 读取原始 UTF-8 文本，不解析、重排、追加换行、删改或查找替代文件 |
| `src/agent_search_gateway/errors.py`、`src/agent_search_gateway/url_normalization.py:33–49` | 14 个现有 ErrorCode；DaemonUnavailable 独立于 GatewayError；既有 URL 规范化 | 保留代码，使用安全文字；复用 normalize_url |
| `src/agent_search_gateway/daemon.py:318–478`、`src/agent_search_gateway/runtime.py:298–420` | daemon 分派、真实 Runtime、共享 URLStore/credential pools/ResultWriter；断开客户端并不保证取消已执行任务 | 仅经 socket 进入；不创建第二套 Runtime，不承诺远端取消业务 |
| `scripts/build_docs.py:24–40` | pdoc 递归生成 package API 文档 | docs 环境安装 extra；不为文档扫描修改核心代码 |

### 1.2 实施完成后的公开合同

| Tool | 参数 | 现有请求 / 成功结果 |
|---|---|---|
| `keyword_search` | `query: str`；`provider: str \| None = None` | KeywordSearchRequest / keyword JSONL 全文 |
| `paper_search` | `query: str`；`provider: str \| None = None` | PaperSearchRequest / paper JSONL 全文 |
| `llm_search` | `prompt: str`；`scope: SearchScope = "web"`；provider/model 可空 | LLMSearchRequest / llm JSONL 全文 |
| `agent_search` | 同 llm_search | AgentSearchRequest / agent JSONL 全文 |
| `url_fetch` | `url: str`；`focus: str \| None = None` | URLFetchRequest / 原样 final text |

仅有这五个工具，没有 resources、prompts、管理、配置、凭据或任意文件读取接口。schema 拒绝未知字段；Fetch 不接受 provider/model。query/prompt 去除首尾空白后不得为空；selector 去空白但保留大小写，显式空串拒绝；省略/null 表示不筛选。scope 只接受精确枚举，不做 strip/casefold。focus 的空白串变为 None。语义上的 provider/model 匹配、URL admission、业务重试全部留给 daemon。

命令为 `agent-search-gateway-mcp`；默认 STDIO；HTTP 选项为 `--transport streamable-http --host 127.0.0.1 --port 8765`，固定路径 `/mcp`。STDIO 中显式出现 --host/--port 即为用法错误，包括显式给出默认值。daemon 不可用时仍可启动、握手和发现工具。

## 2. SDK 选型与具体接入决策

### 2.1 版本与依赖

以 2026-11-27 核验的官方 Python SDK **v2.3.0** 为实施基线。该 tag 的 metadata 要求 Python >=3.10，覆盖项目 Python 3.11–3.14；实际锁解析及平台可运行性由原有 CI 矩阵验收，不把 metadata 声明当作已完成跨平台测试。

在 `[project.optional-dependencies]` 新增：

~~~toml
mcp = [
    "mcp>=2.3.0,<3",
    "pydantic>=2.12,<3",
    "uvicorn>=0.31.1,<1",
]
~~~

Pydantic 与 Uvicorn 是 server.py 直接使用且 SDK 已依赖的组件，显式放在同一 extra 中。SDK v2.3.0 使用独立发行包 httpx2；保持网关现有 httpx>=0.27,<1 及其 provider/tests 不变，不进行 HTTP 客户端迁移。更新 uv.lock 时确认实际锁入 SDK 版本；若不再是核验版本，先重新核验受影响的公开 SDK API，再记录解析结果。基础安装不拉入上述可选依赖。

### 2.2 选择官方基础 Server，集中严格验证

采用官方公开 `mcp.server.Server`，不使用 SDK 内部 API，也不使用旧版 FastMCP 示例。选择原因：v2.3.0 高层函数包装的参数预解析可能把可空字符串中的字面量 `"null"` / `"[]"` 改变类型；其默认额外字段处理、字符串返回的结构化输出也不符合本项目的精确边界。

基础 Server 不自动完成工具参数验证。因此由 **server.py 的 SDK 接入边界** 使用 SDK 已依赖的 Pydantic 定义三个严格模型：query、prompt/scope、url/focus，统一设置 strict=True、extra="forbid"。同一个模型既产生公开 JSON Schema，又用于实际 model_validate，避免两套 schema 漂移。这个校验属于 MCP 协议适配，不进入 adapter，也不自行实现 JSON-RPC。不要将 get_tool_input_schema 回调误认为自动执行了校验。

- 通过 `Server(on_list_tools=..., on_call_tool=..., get_tool_input_schema=...)` 注册唯一五工具集合；schema 和 dispatch 来自同一组定义。
- SDK 模型用 Python snake_case 字段：Tool.input_schema、CallToolResult.is_error、structured_content；wire 字段 isError 等由 SDK 序列化。
- 成功显式构造 `CallToolResult(content=[TextContent(type="text", text=value)], is_error=False)`；空串也保留一个 TextContent。无 output_schema、无额外 structured_content、无 JSON 字符串二次编码。
- adapter 失败构造同样的 text content，is_error=True；保留现有 code 文本。不能用普通成功字符串或裸 Exception 代替工具失败。
- 未知工具、参数类型/必填/额外字段错误使用 SDK `MCPError(code=INVALID_PARAMS, message=安全固定说明)`；不回显 Pydantic ValidationError 中的原始输入。通过实际 SDK client 验证它是协议/参数拒绝且零 RPC。
- SDK 负责握手、JSON-RPC、帧、HTTP/session 和自身资源管理。只注册 tools 能力，不添加 resources/prompts handler。

### 2.3 传输与生命周期

STDIO 使用官方 `stdio_server()` 的读写 streams，调用 `server.run(read, write, server.create_initialization_options())`；由 SDK 管理协议输出，诊断仅 stderr。

HTTP 使用基础 Server 的公开 `streamable_http_app(streamable_http_path="/mcp", json_response=False, stateless_http=False, transport_security=..., host=...)`，以 Uvicorn 运行。该方法已为返回的 Starlette app 绑定 session manager lifespan，不另造管理器/重复进入 lifespan。保留 Streamable HTTP 自身的 SSE 响应支持；不添加旧式 /sse endpoint。

工厂接受 GatewayAdapter；runner 只拥有 MCP transport。参数解析后才导入 server.py。不在启动、发现、退出或失败路径连接 daemon，不建立常驻 Unix socket，不新增整体业务 timeout、重试、队列或 semaphore。SDK 的协议/session 限制与测试的防挂起时限不是新的业务策略。

### 2.4 HTTP 安全配置收敛

无论监听地址是否 loopback，都显式传入 `TransportSecuritySettings(enable_dns_rebinding_protection=True, ...)`，不能依赖 SDK 对非 loopback host 的自动默认值。

初版使用窄 backend allowlist：Host 为 `127.0.0.1:*`、`localhost:*`、`[::1]:*`；Origin 为对应的 `http://` loopback origin。这里 * 只匹配端口，不接受任意域名。生产默认仍仅监听 127.0.0.1。外部 Host/Origin 直接访问被拒绝，即使操作者显式 bind 0.0.0.0。

外部访问使用同机可信反向代理或安全隧道。代理验证外部 Host/Origin、认证和 TLS 后，使用 allowlist 中的 backend Host；若需要转换 Origin，只能在外部 Origin 已被严格验证后转换为对应 backend origin。不要盲目删除/改写未验证的 Origin，不增加 allow-all、不关闭 DNS rebinding 防护。本版本不新增 allow-host/origin 配置面；外部认证/TLS/隔离不在 gateway 实现。测试这个已验证、窄转发的 backend 配置。非 loopback bind 向 stderr 输出安全提醒。

### 2.5 已核验的官方资料

版本与 API 以固定 tag 源码为准；滚动文档只补充语义，不混用 v1 示例。

- [v2.3.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)
- [v2.3.0 pyproject / Python 与依赖要求](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/pyproject.toml)
- [基础 Server、callbacks、STDIO run 与 HTTP app](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/server/lowlevel/server.py)
- [高层参数预解析与返回值处理](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/server/mcpserver/utilities/func_metadata.py)
- [Host/Origin 防护](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/server/transport_security.py)
- [官方 Client 与协议模式](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/client.py)
- [STDIO client](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/stdio.py) / [Streamable HTTP client](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/streamable_http.py)
- [官方 low-level server 文档](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/)
- 使用 Context7 先解析官方库，再查询固定版本 `/modelcontextprotocol/python-sdk/v2.3.0`；返回的 pinned 文档与上述 tag 源码交叉核对。

## 3. 文件结构与内部类型

### 3.1 生产与交付文件

| 操作 / 文件 | 单一职责 |
|---|---|
| 新增 `src/agent_search_gateway/mcp/__init__.py` | 无副作用的 package marker |
| 新增 `src/agent_search_gateway/mcp/__main__.py` | stdlib 参数解析、lazy import、RuntimePaths.default、启动/退出安全诊断 |
| 新增 `src/agent_search_gateway/mcp/adapter.py` | 输入语义、现有 Request/RPC、响应分类、安全文件读取；不导入 SDK/CLI |
| 新增 `src/agent_search_gateway/mcp/server.py` | 唯一 SDK/Pydantic/Uvicorn 依赖处；schema、SDK 结果、共用工厂、两个 transport runner |
| 修改 `pyproject.toml`、`uv.lock` | 可选 extra 与独立 console script；基础依赖/原入口不变 |
| 修改 `.github/workflows/ci.yml` | 原 jobs 中安装 extra、实际 transport 测试、base/extra wheel smoke |
| 修改 `.github/workflows/pages.yml` | API 文档环境安装 extra；现有发布权限与步骤不变 |
| 修改 `README.md`、`docs/public-interface.md`、`CHANGELOG.md` | 安装、使用、公开合同、安全/生命周期、Unreleased |
| 修改 `tests/docs/test_installation_contract.py` | 同步开发/CI 命令合同，保留原质量及包安装断言 |

现有核心 Python 文件、config.example.toml、构建脚本、provider 配置和发布版本均不在修改清单。SDK 相关类型只能向 server.py 汇聚；不为了两种 transport 创建通用服务框架。

### 3.2 新增测试文件

全部位于 `tests/mcp/`：`__init__.py`、`_support.py`、`conftest.py`、`test_adapter.py`、`test_result_files.py`、`test_server.py`、`test_entrypoint.py`、`test_stdio.py`、`test_streamable_http.py`、`test_shared_daemon.py`、`wheel_smoke.py`。

_support/conftest 只组织可复用 fixture、typed recording client、测试拥有的 transport/peer 生命周期；wheel_smoke 是由已安装 wheel 的解释器执行的独立验收程序。不要增加生产测试专用开关，不使用 importorskip 让缺 SDK 的 MCP CI 变绿。

### 3.3 内部类型约定

~~~text
BusinessRequest =
    KeywordSearchRequest | PaperSearchRequest |
    LLMSearchRequest | AgentSearchRequest | URLFetchRequest
SocketClient = Callable[[Path, Request], Awaitable[Response]]
AdapterFailure(Exception):
    code: ErrorCode | None
    safe_message: str
GatewayAdapter(paths: RuntimePaths, client: SocketClient = send_request):
    five async methods with public tool argument semantics -> str
    private execute(request: BusinessRequest) -> str
~~~

SocketClient 的 Request/Response 复用 models.py；其宽 Request 类型用于匹配现有 send_request 签名，adapter 只允许上述五类业务请求。ResultKind 根据请求类型在内部确定，不成为公开参数。GatewayError 和 ErrorResponse 转成 AdapterFailure；DaemonUnavailable 单独处理。AdapterFailure 不继承/扩展现有 ErrorCode，不携带可向客户端传播的原始异常文字。

server.py 使用三个严格参数模型和明确的 typed dispatch 分支，避免动态 dict 展开掩盖类型错配。SDK Server/ServerRequestContext 的泛型按 v2.3.0 公共签名补全；不使用 Any 或全局 type-ignore 绕过 strict mypy。main(argv: Sequence[str] | None = None) -> int，模块运行用 SystemExit(main())。

## 4. TDD 与测试基础约定

每个任务的五步依次执行。RED 命令只运行当下新增场景，确认因为目标行为缺失失败；GREEN 先目标文件，再该任务给出的全量离线命令。任务后重构不得扩大生产改动范围。除确有剩余风险或 CI gate 要求，不反复增加无关测试。

统一全量离线命令（下文称 FULL）：

~~~bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest -q
~~~

新增 extra 前的首次 metadata RED 使用现有 dev 环境 `uv run pytest ...`，不能先用不存在的 --extra mcp 造成解析错误。Task 01 完成后统一执行：

~~~bash
uv sync --locked --all-groups --extra mcp
~~~

离线 fixture 优先复用：

- `tests/support/fakes.py` 的 FakeKeywordSearchProvider、FakeAcademicSearchProvider、FakeURLFetchProvider、FakeLLMClient、pooled_web。
- `tests/support/acceptance.py` 的 build_acceptance_runtime / FilteringAcceptanceRuntime，用于真实 orchestrator/store/writer 的简单场景。
- `tests/acceptance/test_gateway_workflows.py` 的 _running_daemon、_filtering_cli。
- `tests/acceptance/test_search_cost_arxiv_proxy_cli.py:51–118` 的 _gateway：以 httpx.MockTransport 构造真实 Runtime.build，适合 shared credential/scheduling 证明。
- `tests/support/controlled.py` 的 ManualClock，以及 `test_credential_pool_fetch_state.py` 已有 barrier/recorded send 模式。

只在 tests/mcp 增加小包装，避免抽取或改动无关既有测试框架。子进程 HOME 使用短的 test-owned 临时目录，预先检查默认 daemon socket 路径长度；现有 Darwin basetemp 缩短不保证嵌套 HOME 足够短。父测试进程运行 test-owned daemon，子 MCP 只连接相同 HOME。去除会误连真实安装/凭据的环境影响，不打印继承环境。HTTP fixture 预绑定 loopback port 0 后交给 Uvicorn，避免先探测空闲端口再重绑的竞争。

所有启动/关闭等待有测试级 bound；优先 events/barriers/进程退出通知，不用长 sleep 或靠精确耗时断言。清理只关闭当前测试创建的进程、socket、server 和目录，不能停止已有用户 daemon。

## 5. 分步实施

### Task 01 — 建立可选发行边界和无副作用包

**Files**

- Create: `src/agent_search_gateway/mcp/__init__.py`、`tests/mcp/__init__.py`、`tests/mcp/test_entrypoint.py`。
- Modify: `pyproject.toml`、`uv.lock`。
- Reference: pyproject 的 dependencies/scripts/groups；Testing §8。

- [ ] **1. RED：写发行边界测试。** 在 test_entrypoint.py 新增 test_optional_mcp_metadata，读取项目 metadata，断言新 console script 精确指向 `agent_search_gateway.mcp.__main__:main`、mcp extra 存在、基础 dependencies 仍只有原 httpx 约束、原 CLI 入口未变。新增独立解释器 import 检查：导入既有 CLI 与新 package marker 不加载顶层 mcp、pydantic、uvicorn，不创建 socket/结果目录。
- [ ] **2. 运行并确认预期失败。**
  ~~~bash
  uv run pytest -q tests/mcp/test_entrypoint.py -k optional_mcp_metadata
  ~~~
  预期因缺少 extra/新入口的断言失败。先完成 metadata，再逐步运行 marker 测试；不将不存在模块导致的 collection error 当作 RED。
- [ ] **3. 最小实现。** 增加 §2.1 的 extra、console script 和空 marker；保留原 dependencies/groups/Python classifier/version。执行 `uv lock`，检查锁入官方 SDK v2.3.0 及相关 optional 依赖；随后执行 §4 的 locked sync。只更新实际依赖变化，不能顺带升级无关依赖或更改 Python 支持范围。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_entrypoint.py`，然后 FULL。预期新 metadata/import 边界及原测试全绿；基础安装的真实环境证明留给 Task 16。
- [ ] **5. 重构。** marker 保持无导出/初始化逻辑；检查 pyproject 与 lock 的关系，删除不必要的测试辅助抽象；重跑本任务目标测试保持绿色。

### Task 02 — 实现独立入口的参数与 lazy import

**Files**

- Create: `src/agent_search_gateway/mcp/__main__.py`。
- Modify: `tests/mcp/test_entrypoint.py`。
- Reference: Architecture §3 Runtime Model / Interface、Error Handling §2。

- [ ] **1. RED：参数表与依赖诊断。** 使用 stub server runner 记录选项，验证默认/显式 STDIO、HTTP 默认 127.0.0.1:8765、HTTP override；对无效 transport、空白 host、包含 scheme/path/control 的 host、非法端口/0/65536、STDIO 携带显式 --host 或 --port（含默认值）断言 stderr usage、非零且未加载 runner。验证 --help 不加载 SDK；仅根模块 mcp 缺失时给 extra 安装指导；其他 ModuleNotFoundError、import 错误或 startup 异常不能伪装成缺 extra。检查 RuntimePaths.default 仅确定路径、不读配置/探测 daemon。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_entrypoint.py -k 'options or lazy_import or missing_extra'
  ~~~
  预期是选项、返回码或诊断断言失败。
- [ ] **3. 最小实现。**
  ~~~text
  parse with argparse; host/port initially None to retain "explicitly supplied"
  reject invalid transport/host/port and STDIO + either HTTP-only option
  choose HTTP defaults only after validation
  lazily import server.run_server
  if ModuleNotFoundError.name == "mcp": safe install guidance, return nonzero
  otherwise import/startup failure: safe startup diagnostic, return nonzero
  run_server(paths=RuntimePaths.default(), transport=..., host=..., port=...)
  ~~~
  host 校验只做地址/主机名语法，不为参数验证发起 DNS/network；IP 地址用 stdlib 验证，主机名拒绝端口/URL/空白。以独立 HTTP runner 参数传递，不新增 provider/config 参数。安装指导给本项目现有可执行的本地 `uv tool install '.[mcp]'` 示例，并说明在源码目录执行；不假定已有 PyPI 发布。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_entrypoint.py`，然后 FULL。所有失败路径 stdout 为空；--help 的正常 stdout 不属于运行中的 STDIO 协议流。
- [ ] **5. 重构。** 仅保留必要的解析/启动辅助函数；清理重复校验，确保 import server 位于 help/usage 处理之后；重跑目标测试。保留 unrelated import bug 的独立测试，不能用宽泛 ImportError 分支吞掉。

### Task 03 — 建立 typed adapter 与五种输入映射

**Files**

- Create: `src/agent_search_gateway/mcp/adapter.py`、`tests/mcp/_support.py`、`tests/mcp/test_adapter.py`。
- Reference: cli.py:90–136、models.py:107–174、url_normalization.py、Testing §4.1。

- [ ] **1. RED：参数化 typed request 与零/一次 RPC。** recording async client 接收路径与现有 Request。覆盖五工具、LLM/Agent 的 web/paper/all、provider/model 独立组合、大小写和前后空格、selector 省略/null、focus 省略/null/空白、URL 既有规范化。对 blank query/prompt、blank supplied selector、非法 scope、非法 URL 断言现有输入 code 且零调用。有效请求测试可在 recording client 的 entered/release barrier 暂停后核对请求，再由测试取消清理；不必伪造尚未实现的文件成功语义。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_adapter.py -k 'request_mapping or input_validation'
  ~~~
  预期字段、默认值、规范化或调用次数断言失败；先补最小包/类型骨架，避免用导入失败代替行为失败。
- [ ] **3. 最小实现。** 按 §3.3 建立 GatewayAdapter、SocketClient 与 AdapterFailure。小函数分别处理 required text、nullable selector、scope、focus；URL 只调用 normalize_url。五方法构造现有 request 后进入一个 _execute，使用注入的 paths.socket_file 和 client。没有 provider registry lookup、CLI import、daemon/private workflow 调用。类型错误由 server.py 严格模型拦截，adapter 仍明确处理 scope/字符串语义。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_adapter.py`，然后 FULL；确认所有接受的调用最多发送一次，所有前端语义拒绝零发送。
- [ ] **5. 重构。** 只抽取五接口确实重复的边界规范化；保留显式 Request 构造，避免通用字符串 operation registry。输入 helper 不持有状态，adapter 无 SDK/CLI 依赖。重跑目标测试。

### Task 04 — 返回完整搜索文本与原样 Fetch 文本

**Files**

- Create: `tests/mcp/test_result_files.py`。
- Modify: `src/agent_search_gateway/mcp/adapter.py`、`tests/mcp/_support.py`、`tests/mcp/test_adapter.py`。
- Reference: ResultWriter、request_ids.py、Architecture §3 Storage、Testing §4.2。

- [ ] **1. RED：精确字节解码后的文本合同。** 在隔离 results_dir 创建 keyword/paper/llm/agent 合法文件，各含 Unicode、混合 web/paper 行顺序、空白、LF/CRLF、最后有/无换行；fake typed client 返回对应绝对路径。断言返回值逐字符相等、文件未改动、只读取这一个文件。对四种空文件都断言空串成功。Fetch 参数化普通文本、空串、UNAVAILABLE_MESSAGE、类似绝对路径的文本，并令文件打开边界在被调用时失败，证明 Fetch 不读文件。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_result_files.py -k exact
  uv run --extra mcp pytest -q tests/mcp/test_adapter.py -k fetch_text
  ~~~
  预期因仍返回路径、补换行、改变换行或错误打开 Fetch 文本而断言失败。
- [ ] **3. 最小实现。**
  ~~~text
  success response:
      infer expected ResultKind from the existing Request class
      URLFetchRequest -> return response.text unchanged
      search -> await offloaded regular-file reader(response.text, expected kind)
  ~~~
  用完整 bytes.decode("utf-8") 或 newline="" 的 UTF-8 文本读取，保留 BOM/原有换行/空内容；不能复用 CLI 输出函数，不 json.loads/dumps、splitlines/join、strip、加头尾说明或截断。安全 reader 的位置/打开细则在 Task 05 补齐后才允许接入公开 server。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_result_files.py tests/mcp/test_adapter.py`，然后 FULL。结果不存在第二次 RPC、备用文件扫描或数据重写。
- [ ] **5. 重构。** 将“请求决定结果类型”和“读取文件”各保留一个实现点；不要从内容猜测 schema 或 Fetch 错误。重跑目标测试保持精确相等。

### Task 05 — 收紧文件位置、打开对象与异步读取安全

**Files**

- Modify: `src/agent_search_gateway/mcp/adapter.py`、`tests/mcp/test_result_files.py`、`tests/mcp/_support.py`。
- Reference: Error Handling §4、Testing §4.3、request_ids.py。

- [ ] **1. RED：按风险逐组增加文件测试。** 拒绝相对路径、含显式 .. 的路径、外部/相似前缀 sibling、非直接子文件、错误 kind/扩展名/ID、目录、FIFO、final symlink（目标在根内也拒绝）和外部 symlink；合法 resolved results root 仍能读取 writer 生成的文件。用受控 OS 边界故障注入覆盖缺失、校验后消失、permission denied、打开时替换成 symlink/其他对象、读取一部分后 OSError、非法 UTF-8。每例断言无部分成功、无路径/secret 泄露、一次 RPC、无删改/替代读取。用 barrier 证明阻塞 worker 不阻塞第二个 async 调用，取消后 worker 最终释放全部 fd。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_result_files.py -k 'safety or race or offload or resource_cleanup'
  ~~~
  预期在当前过宽路径、跟随链接、FIFO 阻塞或 fd 释放断言失败。FIFO/worker 等用测试级 bound 防止挂死；不要依赖当前 OS 用户的 chmod 特权表现。
- [ ] **3. 最小实现。**
  ~~~text
  worker:
      reject nonabsolute path / explicit traversal / embedded NUL
      resolve configured results root
      require candidate parent resolves to exactly that root
      validate basename using existing kind + validate_request_id/result_filename
      open root directory fd with O_DIRECTORY + O_CLOEXEC
      lstat child relative to root; require regular file (reject symlink)
      open only that child relative to dir_fd with O_RDONLY + O_NOFOLLOW + O_NONBLOCK + O_CLOEXEC
      fstat opened fd; require regular file; reject detected replacement
      read complete UTF-8 without newline conversion
      close file and directory descriptors in worker-owned finally/context managers
  await asyncio.to_thread(worker)
  ~~~
  Linux/macOS 均使用 POSIX no-follow/regular-file 检查；不为当前非目标平台偷偷降级安全保证。O_NONBLOCK 避免 race 后误开 FIFO 卡住；可比对验证与 fstat 的 dev/inode 拒绝已观察到的替换。验证实际打开的对象，不能只 path.resolve 后再普通 open。异常统一为安全的结果不可用/无效失败。线程取消不能中止已开始的同步 read，因此由 worker 自己负责结束后的关闭，不声称即时终止。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_result_files.py`，然后 FULL。确认无任意内容长度上限；同用户恶意并发修改的完全隔离不在承诺范围，不能因此省略实际 fd 检查。
- [ ] **5. 重构。** 保留单个受审查 reader，使用 stdlib 管理 fd；删除重复路径正则/整套自定义结果命名规则。精确内容、受控 race、offload 与 cleanup 测试仍绿。

### Task 06 — 完整错误、socket 协议、取消与并发边界

**Files**

- Modify: `src/agent_search_gateway/mcp/adapter.py`、`tests/mcp/test_adapter.py`、`tests/mcp/_support.py`。
- Reference: errors.py、protocol.py:233–291、Error Handling §2–6、Testing §4.4。

- [ ] **1. RED：失败矩阵。** 参数化全部 14 个 ErrorCode 的 ErrorResponse，以及本地抛出的 typed GatewayError，断言代码保留且安全指导合适。单列 DaemonUnavailable；其原始异常包含路径，不能反射。覆盖 PermissionError/reset/broken pipe、unexpected response object 和普通异常中的 synthetic path/secret。控制 Unix peer 使用真实 send_request，发送 malformed/未知 code/超长/缺换行/提前 EOF 响应，断言既有 protocol_error 和帧限制，不重复请求。两个 client barrier 同时 entered 证明无全局串行化；取消等待中调用后 socket 关闭、CancelledError 不被转成业务失败，随后独立调用可用。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_adapter.py -k 'error or protocol or concurrent or cancel'
  ~~~
  预期 code、脱敏、调用次数、并发 entered 或取消传播断言失败。
- [ ] **3. 最小实现。** 用一处 ErrorCode -> 固定安全说明映射（含明确可用的输入说明），同时处理 ErrorResponse 与 GatewayError；不无条件信任 daemon/provider message。DaemonUnavailable 提示 `agent-search-gateway start`，file/transport-only 失败可用 code=None，不造新 core code。unexpected Exception 在适当边界转换为安全失败并仅记录安全诊断；不捕获 BaseException，不吞 asyncio.CancelledError。保持调用 `await client(paths.socket_file, request)`，不传默认业务 timeout，不 reconnect/replay，不发 shutdown。真实 protocol 的 writer cleanup 继续由 protocol.py 负责。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_adapter.py tests/mcp/test_result_files.py`，然后 FULL。每个失败断言至多一次 RPC、无重复费用请求；无需等待真实 provider 超时来证明没有新 deadline。
- [ ] **5. 重构。** 收敛安全消息和 response 分派，去除重复 catch/log；可以把 code 集合完整性与现有 ErrorCode 枚举比较，但不能复制业务算法。复跑目标测试；检查 adapter imports 仍仅 stdlib 与既有轻量接口。

### Task 07 — 用官方 Server 定义唯一工具集合和严格 schema

**Files**

- Create: `src/agent_search_gateway/mcp/server.py`、`tests/mcp/test_server.py`。
- Modify: `tests/mcp/_support.py`。
- Reference: §2.2；Architecture §6；Testing §5.1。

- [ ] **1. RED：实际 SDK discovery/validation。** 通过官方 Client 与生产 create_server 建立内存 transport，tools/list 只出现五个工具。检查必填字段、nullable/default、scope 枚举、additionalProperties=false 和说明中的 JSONL/alias/admission 信息，不快照无关 schema 排版。未知工具、未知字段、Fetch provider/model、数字/bool/list/object、必填字段 null/缺失、非法 scope 均被协议/校验拒绝且零 RPC。有效的 selector/focus 字面字符串 "null"、"[]" 不被预解析；字段只按现有规范 trim。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_server.py -k 'discovery or schema or literal_strings'
  ~~~
  预期工具集合、公开 schema、输入拒绝或原样字符串转发断言失败。不能仅测试 Pydantic 模型而未经过 SDK call_tool。
- [ ] **3. 最小实现。** 增加三个严格模型、五个 Tool 元数据和 typed dispatch。同一模型生成 input_schema 并验证 params.arguments；无关字段不忽略。Server 构造时只注册 list/call/get-input-schema callbacks。缺失/非法工具或参数在调用 adapter 前抛 SDK INVALID_PARAMS，固定安全消息；不要把 ValidationError 的输入或 traceback 附到协议错误。依次接入五方法，不建立第二套 adapter/business registry。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_server.py`，然后 FULL；运行 `uv run --extra mcp mypy src tests scripts` 检查新 SDK handler 类型。官方 Client.list_tools 返回 ListToolsResult，断言其 .tools；不能套用旧 SDK 的返回形状。
- [ ] **5. 重构。** 消除注册列表、schema callback、dispatch 之间无必要的重复，保留清晰显式类型。不能用私有 SDK Tool metadata 修改、高层 monkeypatch、无界 Any 或全局 ignores 达成绿色；重跑目标测试。

### Task 08 — 验证真实 MCP 成功/失败序列化

**Files**

- Modify: `src/agent_search_gateway/mcp/server.py`、`tests/mcp/test_server.py`、`tests/mcp/_support.py`。
- Reference: Error Handling §2–3/6、Testing §5.1。

- [ ] **1. RED：检查 SDK CallToolResult。** 经官方 client 调用五工具，使用真实临时结果文件/typed client 回复。成功必须恰好包含原始 text，空搜索和空 Fetch 仍成功；无 output_schema、新 structured_content、filepath wrapper、额外 summary 或 JSON 双重编码。每个 ErrorCode、DaemonUnavailable、结果读取失败、unexpected exception 都是 is_error=True，保留适用 code 而不含 synthetic secret/path/trace。每次失败后同一 client 再发合法调用应成功。schema/unknown-tool 错误仍是 Task 07 的 SDK 参数拒绝。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_server.py -k 'result or business_error or subsequent_call'
  ~~~
  预期在错误旗标、空内容或额外结构化输出上失败，不能把普通返回字符串“包含 error”当作通过。
- [ ] **3. 最小实现。**
  ~~~text
  validate/lookup outside business-exception conversion
  try text = await typed adapter dispatch
  except AdapterFailure:
      return CallToolResult(TextContent(safe code/message), is_error=True)
  except ordinary unexpected Exception:
      emit one safe diagnostic to stderr
      return CallToolResult(TextContent(generic safe failure), is_error=True)
  return CallToolResult([TextContent(type="text", text=text)], is_error=False)
  ~~~
  CancelledError 继续传播，不返回伪造成功/业务错误。定义安全消息的单一责任，不能 server 与 adapter 重复输出业务日志；请求正文、URL 内容、凭据与本地路径不写入新的诊断。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_server.py tests/mcp/test_adapter.py tests/mcp/test_result_files.py`，然后 FULL。检查实际 wire 序列化对应 isError 与 text，而不是只断言内部 dataclass。
- [ ] **5. 重构。** 保留小的 success/failure text 构造点，不能把空串误判为 false/error。业务错误与 SDK 参数错误分支清楚；重跑上述目标文件。

### Task 09 — 接通真实 STDIO runner 和进程边界

**Files**

- Create: `tests/mcp/conftest.py`、`tests/mcp/test_stdio.py`。
- Modify: `src/agent_search_gateway/mcp/server.py`、`src/agent_search_gateway/mcp/__main__.py`、`tests/mcp/_support.py`、`tests/mcp/test_entrypoint.py`。
- Reference: §2.3；Testing §6.2。

- [ ] **1. RED：真实子进程 STDIO。** 官方 Client 用 StdioServerParameters 启动 `sys.executable -m agent_search_gateway.mcp`，设置短、独立 HOME。daemon 不存在时仍能连接、发现；业务请求得到明确 tool error。透明捕获 child 的 stdout 原始流/独立 stderr：stdout 每条均为 SDK 协议帧，没有 banner、路径、日志或 traceback；业务请求仍由官方 SDK client 发出，不在测试实现另一套 MCP client。关闭 client 后 child 在测试 bound 内结束，socket peer 不收到 shutdown。单独注入 SDK startup 失败验证 stderr/非零退出。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_stdio.py
  uv run --extra mcp pytest -q tests/mcp/test_entrypoint.py -k startup
  ~~~
  预期启动/发现、输出通道、缺 daemon 的恢复性错误或进程清理断言失败。
- [ ] **3. 最小实现。** 在 server.py 实现 stdio async runner：stdio_server -> server.run(... create_initialization_options())，由同步 run_server 用 asyncio.run 启动；GatewayAdapter 使用传入 RuntimePaths 构造。entry 只 lazy-load 并调用 runner，模块 guard 正确返回 exit code。SDK 日志/安全启动诊断只用 stderr；不打开额外业务日志文件。SDK 的退出只清理自身 transport，不读/写 daemon 生命周期状态。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_stdio.py tests/mcp/test_entrypoint.py`，然后 FULL。重复测试不残留 MCP child 或测试 socket；不以 terminate 其他进程来掩盖清理错误。
- [ ] **5. 重构。** 将 subprocess/stdio fixture 的所有权与 cleanup 集中在 tests/mcp，不在生产新增 test mode；默认 STDIO 与显式 --transport stdio 共用 runner。重跑目标测试。

### Task 10 — 接通 Streamable HTTP、固定端点与 Host/Origin 防护

**Files**

- Create: `tests/mcp/test_streamable_http.py`。
- Modify: `src/agent_search_gateway/mcp/server.py`、`src/agent_search_gateway/mcp/__main__.py`、`tests/mcp/_support.py`、`tests/mcp/conftest.py`、`tests/mcp/test_entrypoint.py`。
- Reference: §2.3–2.4；Error Handling §2；Testing §6.3。

- [ ] **1. RED：真实 HTTP transport 与安全边界。** 测试预绑定 loopback 随机端口并运行生产创建的 app，官方 Streamable HTTP Client 在 /mcp 发现与调用。未知路径不变成其他公开 API。实际请求分别带合法 loopback、非法 Host、非法 Origin、外部 Origin 配合法 Host；拒绝必须发生在 adapter/RPC 前。验证已验证代理的 backend Host/Origin 转发可以工作。构造非 loopback bind 选项时，检查防护仍启用且 stderr 提醒；无需向真实公网暴露测试。额外用真实冲突 listener 验证 CLI bind 失败安全退出，发现工具不要求 daemon。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_streamable_http.py -k 'endpoint or host or origin or bind'
  uv run --extra mcp pytest -q tests/mcp/test_entrypoint.py -k http
  ~~~
  预期端点、拒绝位置、防护选项、诊断/退出状态断言失败；仅 TCP connect 成功不是通过。
- [ ] **3. 最小实现。** 使用 server.streamable_http_app 的 SDK lifespan 与 §2.4 明确 allowlist，再交给 Uvicorn。不直接实现 ASGI JSON-RPC/session manager，不添加 REST、旧 /sse、auth/OAuth/TLS handler。默认 json_response=False/stateless_http=False 保留官方正常传输和可用取消语义。Uvicorn access_log 关闭以避免多余 URL 记录；正常 SDK 启动/错误日志 stderr。正确处理 SDK/Uvicorn 的 startup Exception 或非零 SystemExit，而不误抓 argparse help 的正常退出。没有 daemon readiness probe。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_streamable_http.py tests/mcp/test_entrypoint.py`，然后 FULL。Host/Origin 测试通过真实 TCP 与 SDK middleware，不仅检查构造参数。
- [ ] **5. 重构。** 共用工具 factory；HTTP helper 仅创建 SDK app 与安全设置，runner 仅监听与清理。不要为了自定义 bind 关闭防护或新增公开配置面；重跑目标测试。

### Task 11 — 共用真实 transport 合同、握手模式与缺 daemon 恢复

**Files**

- Modify: `tests/mcp/_support.py`、`tests/mcp/conftest.py`、`tests/mcp/test_stdio.py`、`tests/mcp/test_streamable_http.py`。
- Production: 只允许修正已引入的 MCP 文件中被本任务 RED 证明的接线错误。
- Reference: Testing §6.1–6.3。

- [ ] **1. RED：一套案例运行两个 transport。** 通过参数化 frontend fixture，对五工具检查真实请求类型/参数、完整成功文本、空成功、daemon business error、invalid input 零 RPC 和后续成功；比较语义 schema 与 isError/text，不比较 session ID 或 JSON 排版。每种 transport 至少一个 search 必须经真实 ForegroundDaemon/orchestrators/ResultWriter 产出文件，再比较客户端 text 与文件原文；Fetch 经真实 workflow 返回 final text。daemon absent 时启动/发现/失败，随后启动 test-owned daemon，保持同一 MCP 进程/服务与客户端，下一调用成功。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_stdio.py tests/mcp/test_streamable_http.py -k 'contract or recovery or handshake'
  ~~~
  预期在遗漏工具映射、实际文件转换、错误后的会话可用性或恢复路径失败。若已有实现一次即绿，验证场景确实经过真实 transport/daemon，并保留该集成证据；不要破坏正确代码制造 RED。
- [ ] **3. 最小实现/fixture。** 共用现有 fake providers、真实 daemon fixture 与 MCP 官方客户端；测试不把真实 daemon 替换成固定字串返回器来声称共享状态。v2.3.0 Client 的 auto 模式可能走 server/discover；另外对两个 transport 各用 mode="legacy" 明确完成 initialize/协商与 tools/list/call，覆盖 2025-11-25 路径。设置 cache=None 防止仅命中本地工具缓存。修正生产接线时仍经同一 adapter/factory，不添加重连后台任务。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_stdio.py tests/mcp/test_streamable_http.py`，然后 FULL。增加超过 SDK client 默认 SSE 单事件上限的完整结果案例时，仅在测试官方 streamable_http_client 设置 max_sse_event_size=None；不在 gateway 截断、重发，也不误把 client 上限当作 server 业务上限。
- [ ] **5. 重构。** 将共同业务断言集中一份，transport fixture 只处理启动/连线/清理；保留 STDIO 帧、HTTP Host/Origin、真实 legacy handshake 等各自专属测试。不要以整套 transport 缺依赖 skip 达成绿色。

### Task 12 — 证明跨 CLI / STDIO / HTTP 的 admission 与 Fetch 状态共享

**Files**

- Create: `tests/mcp/test_shared_daemon.py`。
- Modify: `tests/mcp/_support.py`、`tests/mcp/conftest.py`。
- Production: 仅修正新增 MCP 模块的已证实边界错误，不改 URLStore/orchestrators。
- Reference: Testing §7；既有 test_gateway_workflows、test_credential_pool_fetch_state。

- [ ] **1. RED：真实状态与阶段计数。** 同一个 test-owned daemon/runtime：CLI keyword Search -> STDIO Fetch；HTTP keyword Search -> CLI Fetch；两个 MCP frontend 再 Fetch ready URL，断言不重复 Fetch/Safety。用自然带 body 的 Search，记录 Judge/Clean/Safety/Focus 调用为零；实际 Fetch 后才按既有语义运行。Paper Search 验证 landing URL admitted，PDF metadata URL 单独 Fetch 为 url_not_admitted。加入 UNAVAILABLE_MESSAGE 的成功缓存与未 admitted 的错误区分，输出不包含私有 body/error 信息。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py -k 'admission or fetch_cache or deferred_stages or paper'
  ~~~
  预期若连接错 HOME/daemon、重复 runtime、错误文本判定或结果转换不一致，状态/阶段/真实结果断言失败。不能仅通过“各自都成功”证明共享。
- [ ] **3. 最小实现/fixture。** 复用真实 Runtime.build + MockTransport 或既有真实 orchestrator fixture，记录 provider 与 LLM stage 次数；三入口都通过同一 Unix socket。CLI 使用现有 run_command/验收 helper，不 shell-parse 文件输出模拟 daemon。结果与真实 ResultWriter 文件比较；修正只落在新的 adapter/server/entry 接线中，不新增 store/cache/admission 逻辑。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py`，然后 FULL。所有外部返回来自受控 fake/MockTransport，无 API key、真实网站、真实 daemon。
- [ ] **5. 重构。** 保持有限跨接口证据，不复制既有 Fetch 阶段算法的全部测试；消除三种 frontend 重复 fixture，保留真实状态与计数断言。

### Task 13 — 证明 scope、selector 与部分成功沿用 daemon 语义

**Files**

- Modify: `tests/mcp/test_shared_daemon.py`、`tests/mcp/_support.py`。
- Production: 仅按失败证据修正新增 MCP adapter/server。
- Reference: Testing §7、既有 FilteringAcceptanceRuntime / provider selection 验收。

- [ ] **1. RED：代表性真实业务组合。** LLM/Agent 选择 web/paper/all 的代表组合，matched provider/model、独立筛选、保留大小写的 unmatched alias；检查真正被调用的 fake/MockTransport provider、已有 mixed JSONL 行序。一个 provider 成功另一个失败时仍返回部分成功文本；所有被选 provider 失败、有效但无匹配 provider 时为现有 ErrorCode，无额外 fallback。为两个 transport 共用这些案例，不把四工具都写成 keyword 的别名。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py -k 'scope or selector or partial or all_failed'
  ~~~
  预期若 scope/alias/model 被丢弃、默认值变动或结果被重排，实际调用/内容/错误断言失败。
- [ ] **3. 最小实现/fixture。** 使用既有多 provider / 多 LLM alias fixture，控制失败仅发生在外部 provider 边界，沿用原 orchestrator。必要时在合成 config 中设定已支持的单次 attempt，便于区分 daemon 自己的重试与前端重复调用；不改生产重试策略。adapter 只传已有字段，不实现 provider 选择或再执行搜索。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py`，然后 FULL。断言请求次数符合已有 daemon 规则，新增 MCP 层没有多发请求。
- [ ] **5. 重构。** 只保留证明接线正确的代表矩阵；完整 provider/parser/scope 算法继续由既有测试负责。相同错误断言复用 Task 08 的 text/isError 约定。

### Task 14 — 证明三入口共享 credential pool 与调度限制

**Files**

- Modify: `tests/mcp/test_shared_daemon.py`、`tests/mcp/_support.py`、`tests/mcp/conftest.py`。
- Reference: Runtime.build；test_credential_pool_fetch_state.py 的 actual-send、ManualClock、barrier 模式。

- [ ] **1. RED：可观测共享限制。** CLI、STDIO、HTTP 交错请求同一真实 daemon 的逻辑 provider。用受控 credential member 可用性和合成标识记录实际 sends，证明来自一个共享 pool；再用同一 credential 的 capacity/rate 配置、ManualClock 和 events，证明三入口受到同一 gate 约束。另例省略 limits，多个独立调用在 release 之前都到达 upstream barrier，证明没有 adapter 级总闸。断言实际 in-flight/配额状态与计数，不只看最终成功或墙钟耗时。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py -k 'shared_pool or shared_limits or omitted_limits'
  ~~~
  若前端新建 pool/runtime、增加 semaphore 或重复请求，upstream 计数/gate 断言应失败。不要为制造 RED 改坏既有调度器。
- [ ] **3. 最小实现/fixture。** 复用 _gateway 的真实 Runtime.build + httpx.MockTransport；按既有测试方式在测试侧注入 ManualClock 到原 gate/credential/executor 边界，不给生产新增 clock/test 参数。保持三个 frontend 的同一 socket/HOME。只修正新 MCP 层的状态/并发错误；不搬运 pool 算法、设新默认 limit 或 provider-specific 规则。credential 标识只用合成数据，永不读取真实 key。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py`，然后 FULL。测试中 readiness、release、关闭均有 bound，无长期 sleep；各 gate 证明可重复。
- [ ] **5. 重构。** 将受控 fixture 约束在本测试目录，保留最少的 capacity、rate、member-selection 和 unlimited 证据；不硬编码不属于合同的随机/时序选择顺序。

### Task 15 — 证明取消、daemon 重启与 frontend 关闭互相独立

**Files**

- Modify: `tests/mcp/test_shared_daemon.py`、`tests/mcp/test_stdio.py`、`tests/mcp/test_streamable_http.py`、`tests/mcp/_support.py`。
- Production: 仅修正新增 MCP 文件的资源所有权/异常边界。
- Reference: Error Handling §5；Testing §6.3 / §7。

- [ ] **1. RED：生命周期组合。** 两个 MCP frontend 保持运行：daemon 暂停后调用安全失败，重启后新调用使用新 socket；旧 admission 可能失效，应得到 url_not_admitted，再 Search 后 Fetch 成功。关闭 STDIO client 或 HTTP session/server，另一 frontend 与 CLI 仍能访问 daemon。请求已到达受控 upstream 后取消客户端/断开连接，释放 barrier 并观测既有 daemon 允许的完成/结果文件行为；新请求不依赖旧 session，没有第二次提交或 ShutdownRequest。HTTP 并发多 client/session 其中一个失败或关闭不破坏其他会话。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py tests/mcp/test_stdio.py tests/mcp/test_streamable_http.py -k 'restart or independent_lifecycle or disconnect or cancel or concurrent_sessions'
  ~~~
  预期在错误的 daemon 所有权、复用失效 socket、自动 replay 或 session 串扰上失败。
- [ ] **3. 最小实现/fixture。** 由测试持有 daemon stop/start，不让 MCP 调用任何管理方法；测试记录业务请求及真实 socket cleanup。区分“SDK 已送达任务取消”（必须传播）与“HTTP 连接断开未送达取消”（不要求强制停止 handler）。既有 daemon 执行可能继续且写文件，MCP 不补偿/删文件、不承诺取消付费请求。不用新 RPC、探活或重连队列处理恢复；每次调用本来就是 fresh socket。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/mcp/test_shared_daemon.py tests/mcp/test_stdio.py tests/mcp/test_streamable_http.py`，然后 FULL。确认只清理 test-owned 资源，取消/重启后的新调用成功。
- [ ] **5. 重构。** 将生命周期等待/cleanup 放进 test-owned context managers；避免把所有普通异常视为“daemon 已退出”，不要吞测试中的取消/泄漏。重跑目标测试。

### Task 16 — 从实际 wheel 验证基础安装与 MCP 安装

**Files**

- Create: `tests/mcp/wheel_smoke.py`。
- Modify: `tests/mcp/test_entrypoint.py`（仅新增有意义的发行合同断言）。
- Reference: Testing §8；现有 package smoke；[uv tools 指南](https://docs.astral.sh/uv/guides/tools/)。

- [ ] **1. RED：先定义 artifact smoke 断言。** smoke 程序不依赖 pytest，也不在顶层 import MCP。两个模式共同验证既有 CLI --help/--version/start --help；分别在全新解释器中导入并调用 CLI，确认未加载 agent_search_gateway.mcp、顶层 mcp、pydantic 或 uvicorn。共同检查两个 console scripts 来自当前 wheel 安装。base 模式另断言 SDK 未安装、新 MCP --help 成功、实际运行非零且 stderr 安装指导/stdout 空。mcp 模式才导入官方 SDK，连接所安装的 MCP 可执行文件，完成真实握手、发现准确五工具、daemon 缺失时安全 tool error、正常关闭。用短临时 HOME、临时 cwd，移除 PYTHONPATH，检查导入文件位于已安装环境而非源码目录。
- [ ] **2. 构建并运行 smoke，记录真实结果。**
  ~~~bash
  uv build
  ~~~
  在两个全新的测试工具目录执行下面安装/运行命令。预期暴露 wheel 漏文件、entrypoint、optional import 或 executable 协议问题；如果功能已正确打包而直接绿，记录实际验收证据，不制造回归。不能用 project editable 环境的 --help 冒充 wheel 验收。
- [ ] **3. 最小实现/修正。** 在 tests/mcp/wheel_smoke.py 完成所需独立检查；共同的 CLI 行为/import 隔离检查与 base 路径仅用 stdlib；SDK imports 仅在 mcp 模式完成共同检查之后执行。安装操作由外层命令/CI 负责，脚本本身不下载依赖、不管理 daemon。不在默认 UV_TOOL_DIR/BIN_DIR 安装。只修复 pyproject/新增 MCP 文件中经实际 artifact 证明的问题。
- [ ] **4. GREEN：两模式独立成功，然后 FULL。** CI base step 保留现有专属目录与原安装命令，在同一步追加 smoke：
  ~~~bash
  uv tool install --force dist/*.whl
  "$UV_TOOL_BIN_DIR/agent-search-gateway" --help
  "$UV_TOOL_BIN_DIR/agent-search-gateway" start --help
  "$UV_TOOL_DIR/agent-search-gateway/bin/python" tests/mcp/wheel_smoke.py --mode base --bin-dir "$UV_TOOL_BIN_DIR"
  ~~~
  extra step 使用另一组全新的 UV_TOOL_DIR/UV_TOOL_BIN_DIR，以同一个 wheel 的 extra requirement 安装：
  ~~~bash
  mcp_wheel_requirement="$(python -c 'from pathlib import Path; wheels = list(Path("dist").glob("*.whl")); assert len(wheels) == 1; print("agent-search-gateway[mcp] @ " + wheels[0].resolve().as_uri())')"
  uv tool install --force "$mcp_wheel_requirement"
  "$UV_TOOL_DIR/agent-search-gateway/bin/python" tests/mcp/wheel_smoke.py --mode mcp --bin-dir "$UV_TOOL_BIN_DIR"
  ~~~
  tool install 接受 package requirement，不使用属于 tool run 的 --from。本地复现先以 mktemp 创建自己拥有的根目录，并为 base/extra 两轮分别设置上述两个 UV 变量；不得复用开发者已有工具目录。测试临时 HOME 由 smoke 子进程 env 设置，不改当前 shell HOME。
- [ ] **5. 重构。** smoke 程序保持小且可单独运行，不重做整个 transport/业务矩阵；SDK 实际版本纳入结果记录。基础环境始终独立，不能先安装 extra 后再声称验证了无 SDK 的安装。

### Task 17 — 接入现有 CI jobs、平台矩阵与质量门禁

**Files**

- Modify: `.github/workflows/ci.yml`、`tests/docs/test_installation_contract.py`。
- Reference: ci.yml 的 quality/tests/docs/package/secret-scan/verify；Testing §8。

- [ ] **1. RED：更新真实的安装/CI 合同。** 在 test_installation_contract.py 增加/调整断言：quality/tests/docs 的 sync/run 显式使用 mcp extra；原 Linux Python 3.11–3.14、macOS Python 3.12 不缩减；package 保留 base smoke 并新增独立 extra smoke 与实际 wheel_smoke 调用；保留 audit、secret-scan、verify 依赖、权限及 pinned actions。现有测试硬编码 `uv run ruff check .` / mypy / pytest 字符串，需精确迁移为带 extra 的命令，不能删除检查以适应新 workflow。此任务只迁移 CI 对应断言；README 对应断言留在 Task 18 与文档一同变更，保证逐任务 FULL 可通过。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/docs/test_installation_contract.py -k ci
  ~~~
  预期因 workflow 尚未安装/运行 extra、缺第二 wheel 环境等断言失败。
- [ ] **3. 最小 workflow 修改。** quality: `uv sync --locked --group dev --extra mcp`，ruff/mypy run 加 --extra mcp；保留 `uv audit --locked --preview-features audit-command`，不排除新 optional dependencies。已核对 uv audit 的排除式 --no-extra 选项，不能臆造 --extra 参数。tests: 原 sync 增加 --extra mcp 并保留 --python "$PYTHON_VERSION"，pytest 使用 extra，WEB_SEARCH_RUN_INTEGRATION=0。docs: `uv sync --locked --group docs --extra mcp`，用 `uv run --group docs --extra mcp python scripts/build_docs.py`，保留既有 artifact 检查。package 保留原 UV_TOOL_DIR 的 `agent-search-gateway-tools` 与 UV_TOOL_BIN_DIR 的 `agent-search-gateway-bin` 两个目录值，额外 step 用不同的 `agent-search-gateway-mcp-tools` / `agent-search-gateway-mcp-bin` 目录运行 Task 16。不新增 CI 架构，不改变 secret-scan/final verify、安全权限、checkout 或 actions 固定 SHA。
- [ ] **4. GREEN。** 运行 `uv run --extra mcp pytest -q tests/docs/test_installation_contract.py tests/docs/test_open_source_contract.py`，然后 FULL、§6 的 Ruff/mypy/audit/build/docs 命令及两模式 wheel smoke。实施交付记录本地实际可验证的平台；其余平台必须由已有 CI matrix 真正跑过才能宣称通过。缺 SDK 的 skip 不算验收。
- [ ] **5. 重构。** 保留 workflow 原结构与 names，命令只增加必要 environment/extra 和 smoke；检查 uv run 不因缺 extra/group 与 sync 环境脱节。删除重复设置，但不降低任何原 gate。

### Task 18 — 完成公开文档、Pages 环境与最终验收

**Files**

- Modify: `README.md`、`docs/public-interface.md`、`CHANGELOG.md`、`.github/workflows/pages.yml`、`tests/docs/test_installation_contract.py`。
- Reference: README 安装/Commands/Development/Security；public-interface 的稳定接口与兼容策略；Testing §8–9。

- [ ] **1. RED：文档合同与可执行示例。** 增加聚焦断言：可选安装、独立入口、两种 transport/default/endpoint、五工具/nullable/scope、JSONL 全文与空成功、Fetch literal/UNAVAILABLE、错误旗标、daemon 前提与独立生命周期、single trust domain/remote auth+TLS。Pages 文档构建也必须安装 extra。保持原 `uv tool install .` 基础安装示例和现有 CLI 合同；不假称 PyPI 已发布、不承诺可靠远端取消、无限客户端承载或租户隔离。
- [ ] **2. 运行 RED。**
  ~~~bash
  uv run --extra mcp pytest -q tests/docs/test_installation_contract.py
  ~~~
  预期在新增 MCP 使用/公开合同或 Pages extra 说明缺失处失败；文档断言关注稳定选项/命令，不快照整段措辞。
- [ ] **3. 最小文档修改。** README 加 `uv tool install '.[mcp]'`（源码目录执行）、先由操作者运行 daemon、STDIO command/args 与 HTTP URL 配置要点；客户端品牌的配置 schema 不臆造。说明远端客户端无需本地结果路径，MCP 与 daemon 必须同机同信任账号，默认路径/凭据仍归 daemon；大结果完整返回但客户端可能有限制。按 §2.4 写可信 proxy/tunnel、安全的 Host/Origin 转发与非 loopback 警告。列出不能自动 retry 的未知执行结果、restart admission 丢失和取消限制。public-interface 加新公开命令/tools/error/text 合同，保持 Python imports/socket 为内部接口。CHANGELOG Unreleased/Added 记录功能，不 bump 版本。README 开发命令与 CI 一致；Pages 的 sync/run 添加 --extra mcp，run 同时明确 --group docs，不修改 build_docs.py 或部署权限。
- [ ] **4. GREEN：完整验收。** 运行 `uv run --extra mcp pytest -q tests/docs`，再执行 §6 全部门禁、Task 16 两模式 artifact smoke，并查看 diff 是否严格限定在 §3 文件清单。逐项核对 §7 覆盖矩阵。此时才可以报告实现完成；实际不具备的平台/CI 结果如实标注，不能将计划中的预期当作已通过。
- [ ] **5. 重构与交付。** 去掉 README 中重复或实现内部细节，保持公开合同完整。修正最后发现的局部问题并运行受影响检查及必需 gate；足够验证后停止额外测试。交付实现差异、真实命令/结果、SDK/lock 版本、平台结果、任何未通过项或设计偏离，产物在当前工作树原地交付。

## 6. 最终实施验收命令

以下命令是**未来实施时**的必需 gate，本计划编写阶段不执行实现测试或安装新依赖。

~~~bash
uv sync --locked --all-groups --extra mcp
uv run --extra mcp ruff format --check src tests scripts
uv run --extra mcp ruff check .
uv run --extra mcp mypy src tests scripts
uv run --extra mcp pytest -v tests/mcp
WEB_SEARCH_RUN_INTEGRATION=0 uv run --extra mcp pytest -v
uv audit --locked --preview-features audit-command
uv run --group docs --extra mcp python scripts/build_docs.py
uv build
~~~

另外执行 Task 16 的两个隔离 wheel 安装/实际 executable smoke；保留 CI docs artifact 检查、secret-scan 和 final verify。不能用 provider 线上 smoke 替代本计划的离线证据。若操作者另行执行线上 smoke，应单独报告，不属于完成这些 deterministic tests 的前提。

最终 diff 审查须确认：只新增四个生产文件，core/config.example.toml/既有 CLI 入口及基础依赖/版本均未改动；不存在额外 Runtime、provider 调用、ShutdownRequest、整体业务 timeout、全局 gate、自动重试、结果截断或公开文件读取参数。

## 7. Spec 覆盖与计划自检

| 约束 / spec 来源 | 对应任务 | 可验收证据 |
|---|---|---|
| Architecture §1/3/4：独立入口、可选依赖、四文件/核心不变 | 01、02、07、16–18 | metadata/import、base wheel、diff 边界 |
| Architecture §6 / Testing §4.1：五工具、默认值、null、exact selector/scope/URL | 03、07、11、13 | typed request 与零/一次 RPC、SDK schema、实际 daemon 选择 |
| Architecture Storage / Testing §4.2：四种 JSONL 全文、空成功、Fetch literal | 04、08、11 | 逐字符相等、空 TextContent 成功、无额外结构化结果 |
| Error Handling §4 / Testing §4.3：path/name/kind、symlink、FIFO、race、UTF-8、fd | 05 | 正反例、真实 opened fd、无部分成功/改写/重搜、worker cleanup |
| Error Handling §2–3/6：14 code、DaemonUnavailable、protocol/unexpected | 06、08、11 | 保留 code、isError、脱敏、零/一次 RPC、随后成功 |
| Error Handling §5：无新 timeout/retry/gate，取消传播/不保证 daemon 取消 | 06、14、15、18 | barrier、真实 socket cleanup、计数、明确生命周期文档 |
| Testing §5：SDK discovery/result、optional import、参数/startup/bind | 01、02、07–10、16 | 官方 Client 与真实 executable/安装证据 |
| Testing §6.1：两 transport 同语义、五工具、真实文件与 Fetch | 08–11 | 公共合同矩阵、auto 与 legacy handshake、实际 TCP/STDIO |
| Testing §6.2：STDIO 纯协议 stdout、隔离 HOME、退出清理 | 09、11、15 | raw stdout/stderr、子进程清理、daemon 继续可达 |
| Testing §6.3 / Architecture Security：/mcp、Host/Origin、非 loopback | 10、11、15、18 | 真实 SDK middleware 拒绝、窄代理转发、session/并发/绑定错误 |
| Testing §6–7：daemon 缺失启动、同进程恢复、不自动管理 | 09–11、15 | 发现成功、调用失败后恢复、零 shutdown/replay |
| Testing §7：CLI/MCP admission、ready cache、Search 不跑 Fetch stages、paper PDF | 12 | 真实 URLStore、阶段/provider 计数、现有错误 |
| Testing §7：scope/selectors、partial/all-provider/no-match | 13 | 实际 provider sends、原始 mixed JSONL、现有 ErrorCode |
| Testing §7：共享 credentials/rate/capacity、省略 limits | 14 | 同一 Runtime/pool、ManualClock/barrier 与计数 |
| Testing §7：重启、关闭一个 frontend、cancel 后仍可继续 | 15 | 双 frontend + CLI、fresh socket、新 search、资源独立 |
| Testing §8–9：Python/OS matrix、双安装、audit/docs/secret/gates、无版本 bump | 16–18 | CI 原矩阵、wheel smoke、完整 gate、README/public-interface/Unreleased |

### 7.1 类型与依赖一致性核对

- BusinessRequest 只有五类，SocketClient 与现有 Request/Response 匹配；URLFetchRequest 不附加 provider/model。
- SearchScope 复用现有 Literal；provider/model/focus 可空与 schema 默认一致；query/prompt/url 必填且仅字符串。
- 四 search 的 expected ResultKind 来自 Request；ResultWriter 的命名/顺序是唯一权威，不在 MCP 新建结果 schema。
- AdapterFailure 只存安全文字与可选现有 code；GatewayError/ErrorResponse 与独立 DaemonUnavailable 分支明确。
- adapter 不依赖 CLI/SDK；server 是 SDK 专属边界；__main__ 的 help/usage/base installation 不导入 server。
- 公开 text 与 SDK CallToolResult 的 wire 旗标一致；schema 错误与业务错误分开，不混用普通 Exception/字符串。
- 每个 task 都给出具体文件、测试场景、RED 命令、最小实现/伪代码、GREEN 与重构步骤；整体验收及已有门禁未降级。

### 7.2 计划阶段完成条件

本文件已逐项对照三份 spec、现有代码接口、官方 SDK v2.3.0 和现有 CI/测试合同编写。计划评审只检查覆盖、可执行性、类型/API/文件边界；不将未来的 pytest/CI 结果记为通过。自检无遗漏后，仅提交本计划文件并结束计划任务。
