# Agent Search with Antigravity CLI Implementation Plan

**Goal:** 新增独立的 `agent-search` 命令，通过 Antigravity `agy` 的全新非交互进程执行 Web/Paper/All 搜索，复用现有搜索业务流程并保持全部既有 LLM、keyword、paper 和 fetch 合约。

**Architecture:** `docs/designs/architectures/20261022-agent-search-antigravity.md`

**Error handling:** `docs/designs/error-handlings/20261022-agent-search-antigravity.md`

**Testing:** `docs/designs/testings/20261022-agent-search-antigravity.md`

---

## 执行上下文

- 工作树：`/home/agent/agent-search-gateway/.worktrees/agent-search-antigravity-design`。
- 分支：`agent-search-antigravity-design`；初次编写时 HEAD：`96c0d3e`，代码基线：`fe72cc1`；本次修订基于计划提交 `660f890`。
- 全部相对路径和执行命令都以此工作树为根。行号是编写计划时的位置，实施时按同名符号定位。沿用该工作树。
- 此文档保留原实现计划与 Expected，并在下方记录实际完成结果。原 testing spec 的基线记录与本次最终验收结果分别列出。
- 按已批准设计，首版 Agent 边界是本地 `agy -p`，不引入远程 Agent API/SDK。计划提交与本次功能实现分开记录。
- 2026-10-23 用户要求修订：Agent invocation 的 `model` 可省略，缺省沿用用户在 Antigravity 中保存的模型；仅显式配置时传 `--model`。`--output-format json` 和显式 `--print-timeout` 保持。三份 spec 与本计划同步更新。

## 实施与验收记录（2026-10-23）

本工作树已完成 Tasks 1–23 的实现和离线验收。115 个步骤下的复选框对应实际执行；
正常测试没有调用真实网络、LLM 或 Agent。Task 21 的真实本地链路直接覆盖配置、
CLI、Unix socket、Runtime、可执行测试进程、parser/store/writer 和后续 fetch；
未为制造 RED 回退已正确实现的生产代码。原有测试断言保持，OpenAI adapter 仅移动目录。

| 最终质量门 | 实际结果 |
|---|---|
| `uv run ruff format --check src tests scripts` | 195 files already formatted |
| `uv run ruff check .` | All checks passed |
| `uv run mypy src tests scripts` | Success: no issues found in 195 source files |
| `WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q` | **1195 passed, 4 skipped** |
| `uv run python scripts/build_docs.py` | exit 0 |

实施前另行重跑普通测试确认 **871 passed, 4 skipped**。新增验收覆盖默认模型不传
`--model`、显式模型精确传递、请求筛选不污染后续请求、重复 invocation、Web/Paper/All
部分与空成功、真实子/孙进程回收、反复取消、stdout/stderr byte 上限，以及 shutdown
完成前的清理。文档示例由真实 TOML resolver 和 CLI parser 校验；版本号未更改。

### 初次真实环境检查（代理映射前）

- 安装版本的 `agy --help` 已确认 `-p`、`--output-format json`、`--print-timeout`
  和 `--model` 参数存在。
- `agy models` 分别在 10 秒、45 秒等待内未返回可用模型列表，当时没有猜测模型 slug。
- 独立临时 Runtime / socket 下，真实默认模型 Web 请求以 60 秒 Harness 预算运行，
  Python deadline 到期后返回安全的 all-provider failure；实际子进程没有传入
  `--model`。lease 为 0，直接子进程已回收，私有 cwd、socket 已删除，所属进程组
  无存活进程。
- 真实 `agy` 的主动取消场景通过：取消后 lease 回收、cwd 删除、子进程及所属进程组清理。
- 继承终端环境的最小直接 `agy -p` 请求在 30 秒外部限时内没有产生终态。当时尚未定位原因。
  下面的代理复验取代“全部真实成功检索均未验证”的初次结论。

### 代理映射后的真实验收与阻塞（2026-10-23）

实现提交为 `edb55f3`。本轮按用户指示，从已存在的
`CODEXPRO_TOOL_HTTP_PROXY`、`CODEXPRO_TOOL_HTTPS_PROXY` 读取值，按协议临时设置
大小写标准代理变量；`CODEXPRO_TOOL_ALL_PROXY` 不存在，但不影响使用前两项。
只在临时 Runtime 的 Agent provider `pass_env` 中列入这些标准变量名和已有的
`NO_PROXY` / `no_proxy`。没有记录地址、端口或凭据，没有更改持久代理或 ngrok 设置。
这也验证了两个独立前提：daemon 环境必须已有代理值，Agent `pass_env` 必须允许传递这些值。

`agy --version` 为 **1.4.5**。代理映射后 `agy models` 成功返回模型列表，
`gemini-3.8-flash-high` 和 `gemini-3.8-flash-medium` 均来自该实际列表。
继承终端环境的最小直接 `agy -p` 请求已返回 `SUCCESS` 和 `OK`。
后续搜索使用真实 CLI → Unix socket → daemon → Runtime → `agy` →
parser/store/writer 链路；仅为未使用的 mandatory fetch 配置提供占位，HTTP transport
拒绝意外请求。以下完成的搜索检查均观察到 **0 Gateway HTTP calls / 0 LLM stage events**。

| 真实检查 | 观察结果 |
|---|---|
| 默认模型 Web | 不传 `--model`；约 66 秒后 `empty_response`，没有结果文件 |
| 显式 high 模型 Web | **成功**；约 70 秒，输出路径独占 stdout，2 条 Web JSONL，字段及 URLStore admission 通过 |
| 默认模型 Paper | 不传 `--model`；约 42 秒后 `empty_response`，没有结果文件 |
| 显式 high 模型 Paper | exit 0、`status=SUCCESS`、`num_turns=1`，但正文长度为 0；网关拒绝为空答复，没有进入 parser |
| 显式 high 模型 All | **部分成功**；约 58 秒，Web 成功生成 2 条带 `type=web` 的结果，Paper 子进程 exit 3 / `status=ERROR`；失败分支未写入结果 |
| 显式 medium 模型 Paper | 约 15 秒后 `SUCCESS` 空答复；stderr 明确指出所需 `command` 权限被 headless 自动拒绝 |
| Agent 启动后的真实 CLI `stop` | `stop` exit 0；约 9.5 秒内请求以 `nonzero_exit` 结束、daemon 退出，清理检查通过 |

成功 Web 返回的 URL 是
`https://docs.python.org/3/library/asyncio-task.html` 与
`https://docs.python.org/3/library/asyncio-exceptions.html`。
All 的部分成功验证了 mixed schema、分支隔离与有效结果保留；
由于没有成功的 Paper 行，不能据此宣称两分支成功时的真实排序或来源字段已经验收。
这些行为已由离线真实可执行进程测试覆盖。

所有完成的临时 Runtime 检查同时验证：未知模型筛选零进程调用、显式模型精确传递、
省略模型不传参、JSON 输出和显式预算、独立进程组、代理变量被允许传递、
无关服务凭据未传入、没有续接会话或自动批准参数；
结束时 Agent lease 为 0、直接子进程已回收、所属进程组不存在、私有 cwd 和 socket
均删除。此次 stop 检查中 Harness 在 shutdown 等待期间自行返回错误；
强制取消/超时路径的真实证据来自上面的初次检查，完整竞态由离线测试覆盖。

**剩余真实环境阻塞及诊断边界：**

1. Paper 空答复已在 gateway 外用相同 prompt 的 `stream-json` 诊断复现：
   1 个 init、7 个 step_update、1 个 result，最终 `SUCCESS` 但正文为空，
   stderr 有权限拒绝提示。之后真实 gateway 的 medium 模型调用明确报告：
   需要 `command` 权限的工具在 headless 下无法询问授权，因而被自动拒绝。
   这是已复现 Paper 失败的 Harness 原因；不能把所有未披露的 ERROR 终态都归为同一原因。
   未修改 Harness 工具权限、MCP、认证、保存的默认模型，也没有增加自动批准或重试。
   默认模型成功检索、成功 Paper，以及 All 两分支同时成功，仍未完成真实验收。
2. 已安装 gateway 的 mandatory fetch 配置引用 `CPA_API_KEY`，
   该变量在 LocalHost 进程环境和 login shell 均缺失。因此尚未执行真实后续
   `url-fetch`；URLStore admission 已真实验证，后续 fetch 链路已离线验证。
   占位 fetch endpoint 没有被调用，用户现有配置及运行中服务没有被替换。
3. 追加传递现有 `NODE_USE_ENV_PROXY` 未解决默认模型失败。一轮前台检查超过
   LocalHost 请求超时，没有取得完整结果，不计为成功；随后确认该轮进程已退出。
   有完整证据的复验得到 exit 3 / `status=ERROR`。没有把代理已通解释为权限也已具备。

`SUCCESS`、格式校验或 URL admission 均不证明 Harness 实际访问过返回的来源。
普通测试本轮再次通过 **1195 passed, 4 skipped**。代理传递操作约定补充在
`docs/public-interface.md`。临时 smoke/诊断脚本和本地证据文件在验收记录整理后删除；
仓库仅保留这里的安全摘要。两个外部前提（Harness 所需工具权限、fetch 配置凭据）
具备后，应继续复验尚未通过的真实场景。

### 沙箱配置后的真实续验（2026-11-06）

本节更新上方 2026-10-23 的历史性阻塞结论。LocalHost 重新提供
`CODEXPRO_TOOL_HTTP_PROXY` 和 `CODEXPRO_TOOL_HTTPS_PROXY`（`CODEXPRO_TOOL_ALL_PROXY`
仍不存在），测试只在进程中临时映射标准代理变量；未更改保存的
Antigravity 配置、代理服务或 ngrok。CLI 版本为 **1.4.7**，`agy models`
可正常列出模型。

- 最小默认模型请求在 45 秒后返回空 `SUCCESS`、零 token；显式
  `gemini-3.8-flash-high` 和 `gemini-3.8-flash-medium` 均在生成前超时。
  对 medium 模型临时关闭沙箱后仍超时，因此这次零 token 问题不是
  `proceed-in-sandbox` 直接造成的。显式 `claude-sonnet-4-6` 的最小
  非交互请求则约 10 秒成功回复 `OK`。
- 使用 Claude、真实 `agy -p`、独立临时配置和拒绝意外 HTTP 请求的
  MockTransport，完成 CLI → Unix socket → daemon → Runtime → Agent →
  parser/store/writer 端到端验收；**未调用 LLM API**：
  - `--scope web`：约 33 秒成功，6 条 Web JSONL。
  - `--scope paper`：明确查询 *Attention Is All You Need* (2017)，
    约 13 秒成功，1 条 Paper JSONL，`sources=["agent:agy"]`。
  - `--scope all`：同一明确论文查询，约 13 秒成功，
    1 条 Web + 1 条 Paper，两分支均成功且顺序、来源正确。
  - 三项均正确生成 `agent-*.jsonl`，Agent quota 归零，临时 Socket 和目录清理。
- CLI 流式事件验证 `permission_mode=proceed-in-sandbox`；Agent 在工作树
  主动使用 `run_command` 执行只读 `pwd`，真实退出码 0、返回工作目录，
  已证实沙箱内终端工具不再被 headless 自动拒绝。
- 一次较宽泛的 Paper 查询在 150 秒预算内未返回完整结果，之后确认
  无遗留 Agent/验收进程；因此不宣称任意论文查询均有稳定延迟。
  **沙箱外路径的自动拒绝尚未实际验证**。按用户约定忽略旧 fetch 凭据
  问题，本轮也未执行真实 `url-fetch`。
- 普通离线测试和质量门复跑：**1195 passed、4 skipped**，Ruff、mypy、
  格式检查和文档构建均通过。

### 计划编写检查单

- [x] 阅读三份已批准 spec，核对架构、错误处理、测试要求。
- [x] 核对实际源码、测试、CI 和公共接口文档，确定文件职责。
- [x] 按依赖顺序分解测试驱动任务。
- [x] 写出具体文件、测试场景、失败预期、实现伪代码和执行命令。
- [x] 对照三份 spec 完成覆盖和类型一致性自查。

## 代码定位与文件职责

当前流程是 CLI → request dataclass → NDJSON → daemon → SearchOrchestrator → LLMStages → provider → 严格 Markdown parser → URLStore / paper finalization → ResultWriter。Agent 仅替换生成文本的后端，并沿用后半段。

| 文件 / 当前定位 | 计划职责 |
|---|---|
| `src/agent_search_gateway/models.py:70-127` | AgentInvocation、AgentSearchRequest、共享 scope/kind 类型；保留 LLMInvocation.extra_body 和旧请求字段顺序 |
| `src/agent_search_gateway/providers/contracts.py:3-12` | 定义 AgentTextRunner callable 类型，LLMClient 协议保持原样 |
| `src/agent_search_gateway/config.py:390-418,580-634` | AgentProviderConfig、ResolvedAgentConfig、引用解析和独立校验；ResolvedConfig.agent 默认空 |
| `src/agent_search_gateway/errors.py:6-19` | 只新增 NO_AGENT_SEARCH_PROVIDERS；复用已有 GatewayError.reason |
| `src/agent_search_gateway/concurrency.py:125-174` | 独立 agent_limits / get_agent，沿用 CapacityGate/lease |
| `src/agent_search_gateway/providers/agent/__init__.py` | 新增 provider 家族包；没有导入副作用 |
| `src/agent_search_gateway/providers/agent/antigravity_cli.py` | 新增消息序列化、受控 env/cwd、真实异步 subprocess、终态 envelope、限流/限时/输出上限/进程清理 |
| `src/agent_search_gateway/providers/openai_chat.py` → `src/agent_search_gateway/providers/llm/openai_chat.py` | 机械移动既有 OpenAI adapter；调整相对 imports，不改 HTTP 逻辑 |
| `src/agent_search_gateway/providers/llm/__init__.py` | 新增 LLM provider 家族包 |
| `src/agent_search_gateway/llm/stages.py:29-47,101-125` | 注入独立 Agent runner map，新增两个 Agent Markdown facade；保留既有六个方法及 llm_stage_* |
| `src/agent_search_gateway/orchestrators/search.py:39-65,139-406,576-591` | 独立 agent_search/选择器，共享 text-search 生成/解析/合并/写入，Agent workflow deadline 和安全日志 |
| `src/agent_search_gateway/paper_search_parser.py:32-91` | source_kind 默认 llm；Agent 显式传 agent；语法保持严格 |
| `src/agent_search_gateway/academic/aggregator.py:26,175-195,259-264` | 两个 generated-source 分支同时识别 llm: 与 agent: |
| `src/agent_search_gateway/request_ids.py:11-15,42-45,92-96` | agent filename 和四种结果文件的 ID 碰撞检查 |
| `src/agent_search_gateway/result_writer.py:15,151-202` | 扩展 paper/mixed kind；mixed 默认仍为 llm；保持序列化字节和失败回滚 |
| `src/agent_search_gateway/runtime.py:38,83-198,353-385` | 绑定 runner factory、Agent quotas、facade、invocations 和论文 source priority；保留 HTTP 关闭流程 |
| `src/agent_search_gateway/protocol.py:67-107,172-189,239-268` | Agent 严格帧编解码，按请求类型选择默认 socket timeout |
| `src/agent_search_gateway/cli.py:47-115` | agent-search 及 scope/provider/model；输入 trim、空值拒绝和 path-only stdout |
| `src/agent_search_gateway/daemon.py:43-56,98-107,324-438` | 类型协议、命令名、ID reservation 和 Agent 分发；复用 shutdown coordinator |
| `tests/support/agent_cli.py` | 新增可执行假 agy fixture builder 和受控进程观察辅助，不运行真实 Harness |
| `tests/support/agent_search.py` | 新增少量可复用 Agent runner / orchestrator fixture；依赖真实 stages/parser/store/writer |
| `README.md`、`config.example.toml`、`docs/public-interface.md`、`CHANGELOG.md` | 新公共命令、独立配置、限制、输出和运维说明 |

`doctor.py::_configuration_checks` 已调用 `resolve_config`，Agent 配置校验通过该入口自然生效；增加测试，不建立第二套校验。复用 `orchestrators/paper.py::finalize_paper_hits` 和 `academic/normalization.py` 的现有实现。既有 prompts、URLStore、fetch orchestrator/scheduler、Web/Academic adapters 无需业务修改。

现有 OpenAI 路径在源码/测试中的引用只有 `runtime.py`、`tests/providers/test_openai_chat.py`、`tests/integration/test_live_tavily_and_openai.py`。历史 design/plan 中的旧路径保留为历史记录。`site/` 由构建生成，不手改生成页面。

## 统一类型与边界

以下是接口约定（伪代码签名），不是要求提前写入生产代码：

```text
# models.py
SearchScope = Literal["web", "paper", "all"]
LLMSearchScope = SearchScope
TextSearchKind = Literal["llm", "agent"]

# frozen=True, slots=True
AgentInvocation(provider: str, model: str | None = None)
AgentSearchRequest(prompt: str, scope: SearchScope = "web",
                   provider: str | None = None, model: str | None = None)

# providers/contracts.py
AgentTextRunner = Callable[[str | None, Sequence[ChatMessage]], Awaitable[str]]

# config.py: frozen=True, slots=True; no credential values
AgentProviderConfig(name: str, harness: str, command: str,
                    max_concurrency: int, timeout_seconds: float,
                    pass_env: tuple[str, ...])
ResolvedAgentConfig(providers: tuple[AgentProviderConfig, ...] = (),
                    search_invocations: tuple[AgentInvocation, ...] = ())
# ResolvedConfig.agent uses field(default_factory=ResolvedAgentConfig).

# providers/agent/antigravity_cli.py
async def run_antigravity(
    model: str | None, messages: Sequence[ChatMessage], *,
    command: str, timeout_seconds: float, pass_env: Sequence[str],
    quota: CapacityGate,
) -> str: ...

# LLMStages: new keyword-only agent_runners=None; existing parameters stay compatible.
async def agent_search_markdown(self, invocation: AgentInvocation, prompt: str) -> str: ...
async def agent_paper_search_markdown(self, invocation: AgentInvocation, prompt: str) -> str: ...

# SearchOrchestrator and daemon._SearchOrchestratorLike
async def agent_search(self, prompt: str, *, request_id: str,
                       scope: SearchScope = "web",
                       provider: str | None = None, model: str | None = None) -> str: ...

# Parser/writer compatibility defaults
def parse_paper_markdown(markdown: str, *, provider: str,
                         source_kind: TextSearchKind = "llm") -> list[PaperSearchHit]: ...
# ResultKind = Literal["keyword", "llm", "paper", "agent"]
# PaperResultKind = Literal["paper", "llm", "agent"]
def write_mixed_results(self, web_records: Iterable[SearchRecord],
                        paper_records: Iterable[PaperRecord], *,
                        request_id: str, kind: TextSearchKind = "llm") -> Path: ...

# protocol.py: None means choose the request-specific default.
async def send_request(socket_path: Path, request: Request, *,
                        response_timeout_seconds: float | None = None) -> Response: ...
```

共享 orchestration 保留实际 invocation 类型：在 `orchestrators/search.py` 定义私有 `TInvocation = TypeVar("TInvocation", LLMInvocation, AgentInvocation)`。共享 helper 接收 `tuple[TInvocation, ...]` 与 `Callable[[TInvocation, str], Awaitable[str]]`，不把 Agent 强转为 LLMInvocation，不丢失 LLM 的 extra_body。Helper 名称统一为 `_select_text_invocations`、`_text_search`、`_text_web_records`、`_text_paper_records`、`_run_text_web_pipeline`、`_run_text_paper_pipeline`。保留 `_select_llm_invocations` 作为原选择入口，新增对应 `_select_agent_invocations`。所有 records helper 必须显式接收已选择的 invocation tuple，不能默认回退到完整集合。

### 关键执行规则

| 边界 | 实现约定 |
|---|---|
| 校验顺序 | request ID → prompt → scope → 配置列表是否为空 → selector 空值/精确匹配 → 工作流 |
| 配置隔离 | Agent 只解析 search_agent 中引用的 alias；不继承 global_default_llm / extra_body；未引用 alias 不可选 |
| 默认模型 | Agent 配置省略 model → AgentInvocation.model=None；runtime/stages 原样传递；adapter 不传 --model，交给 agy 读取用户保存的默认配置 |
| 显式模型 | 配置 model 存在时必须为非空字符串；只对此 invocation 传 --model；不读取/修改 Antigravity 设置，不解析或缓存实际默认模型 |
| model 选择器 | Gateway 的 --model 仍是配置筛选器，仅匹配显式 model；未设 selector 则包含默认模型 entry；不把 selector 写入 entry 或作为缺省值 |
| invocation 顺序 | tuple 保留配置顺序和重复条目；结果按配置顺序处理，不按完成先后 |
| 每进程预算 | timeout_seconds 默认 300，有限正数且 ≤540；agy 收到相同秒数的 --print-timeout |
| Python 硬期限 | 每进程配置预算 + 5 秒；此常量用于容纳 CLI 终态退出，不形成新的用户配置 |
| 清理 | SIGTERM 进程组，最多等待 2 秒后 SIGKILL；等待直属进程退出，收束 stream reader/wait tasks，再移除 cwd，最后释放 quota |
| 整体请求 | agent_search 选好 invocation 后用 1,140 秒 deadline 包住全部 queue/run/finalization/write 流程 |
| Socket | Agent 默认 1,200 秒，其他请求默认 300 秒；显式数值 timeout 优先 |
| 输出 | stdout ≤4 MiB，stderr ≤256 KiB；按 bytes 并行读取，超限拒绝，不能截断后当成功 |
| 重试 | 不重试整个 Agent；配置的重复 invocation 是独立调用，不是 retry |
| 成功 | 默认/显式模型路径均固定传 --output-format json；退出码 0 + UTF-8 单 JSON object + status=SUCCESS + 非空 string response，再交给现有 Markdown parser |
| 数据副作用 | parser 完整成功后才交给 shared store；完整失败/请求超时不新建文件；沿用现有 URLStore admission 语义，不引入全请求 store rollback |
| 取消 | CancelledError 清理后重抛，不转成普通 provider 失败或结果文件 |
| source | parser 的默认 llm: 不变；Agent 是 agent:；academic 强身份/优先级规则保留 |

“fetch_llm 仍必需”指四个 fetch invocation 必须按既有 stage → fetch_llm → global_default_llm 继承规则完整解析；不新增“必须显式写出四个表”的限制，也不取消现有合法继承。

Agent 日志的 `model` 表示显式配置的 pin；未配置时传 `None`，由已有 formatter 输出 `model=null`，不声称知道实际默认模型。`AgentSearchRequest.model=None` 表示不按模型筛选，`AgentInvocation.model=None` 表示委托 Harness 选择模型，两者不得混同。`LLMInvocation.model` 和 `LLMClient` 仍要求字符串。

固定诊断分类写入已有 `GatewayError.reason`，日志仅从 allowlist 取值：`spawn_failure`、`nonzero_exit`、`terminal_status_failure`、`invalid_json`、`empty_response`、`invalid_markdown`、`timeout`、`output_limit`、`cancelled`；意外错误可归入固定 `execution_failure`。不记录 prompt、messages、argv、完整 command/path、response、stderr、JSON error、环境变量值或异常原文。

## TDD 执行方式

任务按编号实施。每项的场景矩阵逐个行为执行 RED → GREEN → REFACTOR，不一次预写整组实现。新模块/符号尚不存在时，让测试在函数内部用清晰 assertion / pytest.fail 表达“所需接口未提供”，不要让模块顶层 ImportError、语法错误或 typo 成为 RED 证据。优先从现有公开边界（resolve_config、codec、CLI、ResultWriter）测试新增行为；纯机械搬文件使用原测试回归，不为文件移动制造业务行为测试。

每个 Task 的 Step 4 执行列出的目标测试及 `uv run pytest -q`；Step 5 如实际重构，再执行同一组命令。保持现有 assertions，不能通过删减旧用例使共享流程变绿。代码/类型边界相关任务也执行标出的 mypy。若已有防线使补充回归用例直接通过，记录为既有行为证据；该 Task 的首个新行为仍需真实 RED。

准备实施环境时使用：

```bash
uv sync --locked --group dev --group docs
```

普通验证始终禁用 opt-in live tests；不能因为本机已有密钥就自动运行真实 Agent。使用 pytest 的临时目录和短 Unix socket 路径，保持 Linux/macOS CI 可运行。进程并发/取消测试用 Event、ready 文件或受控 pipe 同步，使用小的有界等待，避免长 sleep 或等待真实 300/1,140/1,200 秒。

## 实现任务

### Task 1: 解析独立的 Agent 配置与调用数据

**依赖：** 无；以已有有效 fetch fixture 为起点。

**Files:**

- Modify: `src/agent_search_gateway/models.py:70-127`
- Modify: `src/agent_search_gateway/config.py:390-418,610-634`
- Modify: `src/agent_search_gateway/errors.py:6-19`
- Create/Test: `tests/unit/test_config_agent.py`
- Test: `tests/unit/test_config_llm.py`
- Docs: `docs/designs/architectures/20261022-agent-search-antigravity.md` §3 Configuration

- [x] **Step 1: Write the failing test**

首个用例 `test_resolve_config_builds_independent_agent_invocations`：使用现有有效 fetch_llm 配置，加两个 Agent alias、四个调用（混合省略 model、显式 pin 和重复默认模型 entry）；调用真实 resolve_config，断言 agent namespace 存在、字段/defaults 正确、缺省 model 为 None、tuple 顺序与重复项保留。
随后逐个补充：完全无 Agent 配置、空 search_agent.providers、只有 LLM search、只有 Agent search、两者都有；同名 LLM/Agent alias 的配置互不覆盖。新增 `test_agent_config_omitted_model_defers_to_harness`：省略 model 的 entry 不继承非空 global_default_llm.model，也不读取 Harness 设置或运行发现命令；显式 model 字符串保留原值。对四个 fetch 阶段及原 LLM extra_body 保持原断言。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_agent.py::test_resolve_config_builds_independent_agent_invocations -v
```

Expected: FAIL：resolve_config 尚未返回独立 agent namespace；不能是 fixture 缺失 fetch 配置造成的 CONFIG_ERROR。

- [x] **Step 3: Describe the minimal implementation**

```text
在 models.py 新增 frozen AgentInvocation(provider: str, model: str | None = None)，并先定义 SearchScope，
将既有 LLMSearchScope 保留为其兼容 alias；后续 Task 11 即可使用该类型。
在 config.py 新增 AgentProviderConfig 与默认空的 ResolvedAgentConfig。
resolve_agent_config(data) 读取 search_agent.providers，按原序构造 AgentInvocation。
每个 entry 必填 provider；仅当 "model" in entry 时调用 _required_string 校验并保留原值，否则 model=None。
不读取 Antigravity settings，不调用 agy/models，不将 None 替换成 global_default_llm、空串或魔法 default 名称。
从 agent_providers 中只解析被引用 alias，给 command/max_concurrency/timeout_seconds/pass_env 应用约定默认值。
ResolvedConfig 最后增加默认空 agent 字段；resolve_config 保持原 llm/fetch 解析并填充 agent。
errors.py 只新增 NO_AGENT_SEARCH_PROVIDERS = "no_agent_search_providers"。
此步仅实现有效配置和缺省行为；严密拒绝矩阵由 Task 2 逐项驱动。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_agent.py tests/unit/test_config_llm.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

复用 _require_mapping、_required_string 等现有局部 helper；不让 AgentInvocation 继承 LLMInvocation，也不复用会隐式继承 global_default_llm 的 _resolve_invocation。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_agent.py tests/unit/test_config_llm.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 2: 拒绝无效 Agent 配置，保留必需 fetch 校验

**依赖：** Task 1。

**Files:**

- Modify: `src/agent_search_gateway/config.py`（Task 1 新增 Agent resolver；现有 `_positive_int` / `_positive_float`）
- Test: `tests/unit/test_config_agent.py`
- Test: `tests/unit/test_config_llm.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §3.3

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_config_rejects_unknown_harness`：引用 alias 的 harness="unknown"，断言 ConfigFailure/CONFIG_ERROR。
随后一次一个参数化行为：search_agent/providers/条目类型错误；缺/空 provider、harness；显式 model 为空/纯空白/非字符串（含原始 resolver 输入中的 None、bool、数字、列表）；引用缺失；command 非字符串、空值、附带 flags、管道/重定向/控制字符；max_concurrency 为 0、负数、bool、fraction；timeout 为 bool、NaN、±inf、0、负数、>540；pass_env 非数组、非字符串、空/非法 env 名；Agent 配置中的不支持字段。
有效边界覆盖 model 完全省略、显式非空 model、concurrency 1/2、timeout 0.5/300/540、显式 command 路径与 env 名列表；缺失的 pass_env 名没有值时允许省略。未引用 alias 不装配、不使其可选。四个 fetch invocation 无法按原继承规则完整解析时仍失败；原本合法的 global_default_llm fallback 保持可用。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_agent.py::test_agent_config_rejects_unknown_harness -v
```

Expected: FAIL：未拒绝不支持的 harness；其他错误矩阵逐个跑出对应缺失校验的失败。

- [x] **Step 3: Describe the minimal implementation**

```text
Agent 顶层结构先校验类型；只验证 search_agent 引用的 provider 配置。
harness 精确等于 antigravity；provider/model 不从 LLM 配置继承。
区分 model key 缺失与显式非法值；缺失允许 None，显式存在但不是非空 string 一律 CONFIG_ERROR。
command 接受不含空白的可执行名，或含路径分隔符的单个路径；完整路径中的普通空格允许。
拒绝控制字符、shell 运算符/展开片段及附加 flags（例如 agy --flag、/opt/agy --flag、agy | tee）。
路径整体仍是一个 argv 元素；绝不 shlex 拆分、不 expandvars、不通过执行文件做配置验证。
max_concurrency 用 _positive_int；timeout 用 _positive_float 后检查 <=540。
pass_env 只保存满足 [A-Za-z_][A-Za-z0-9_]* 的名称 tuple，不解析/持久化 secret 值。
配置错误只描述字段/位置/规则，不拼接原始非法值。
启动和 doctor 不检查登录、不运行 agy/models，不要求 command 此刻已安装。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_agent.py tests/unit/test_config_llm.py tests/doctor/test_config.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

让 Agent-specific validation 集中在新增 resolver 中。现有 LLM、Web、Academic 配置规则和错误优先级不变。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_agent.py tests/unit/test_config_llm.py tests/doctor/test_config.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 3: 扩展 Agent 文件类型与 request-ID 碰撞保护

**依赖：** Task 1（models.py 基础）；与进程实现无耦合。

**Files:**

- Modify: `src/agent_search_gateway/models.py`（新增 TextSearchKind）
- Modify: `src/agent_search_gateway/request_ids.py:11-15,42-45,92-96`
- Modify: `src/agent_search_gateway/result_writer.py:15,151-202`
- Test: `tests/unit/test_request_ids.py`
- Test: `tests/unit/test_result_writer.py`
- Docs: `docs/designs/testings/20261022-agent-search-antigravity.md` §7.1

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_result_filename_uses_request_id`：result_filename("agent", "a1b2c3d4") 应为 agent-a1b2c3d4.jsonl。
逐步增加 Agent Web/Paper/Mixed 真文件断言：路径绝对、JSONL 逐字节等于旧 schema、mixed Web 在 Paper 前、paper-only 无 type。默认 write_mixed_results 仍输出 llm-* 及旧 bytes。用现有生成器 fixture 验证四种旧/新结果文件均触发 ID 重选；同一 ID 正在使用也不能重复。
保留 exclusive-create：预存文件不得覆盖；序列化/写入失败不留下新半文件。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_request_ids.py::test_agent_result_filename_uses_request_id -v
```

Expected: FAIL：agent 尚未是合法 result kind。

- [x] **Step 3: Describe the minimal implementation**

```text
定义 TextSearchKind = Literal["llm", "agent"]。
ResultKind 与 _RESULT_KINDS 增加 agent；原 request ID 格式保持八位小写 hex。
PaperResultKind/其运行时校验增加 agent。
write_mixed_results 新增 keyword-only kind: TextSearchKind="llm"，校验后传给 result_filename。
复用 _web_payload/_paper_payload/各 serializer 和 _write_serialized；不改变 JSON key/order/separators。
不新增 Agent writer，也不改变旧结果前缀。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_request_ids.py tests/unit/test_result_writer.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

只消除新 kind 校验中的明显重复；保留现有 open('x') 和 unlink-on-failure 路径。错误文件回滚测试优先复用原有 failure fixtures。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_request_ids.py tests/unit/test_result_writer.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 4: 建立独立 Agent 并发配额

**依赖：** Task 1 的 max_concurrency 约定。

**Files:**

- Modify: `src/agent_search_gateway/concurrency.py:125-174`
- Test: `tests/runtime/test_quota_manager.py`
- Docs: `docs/designs/architectures/20261022-agent-search-antigravity.md` §3 Concurrency

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_quota_is_independent_of_same_named_llm_quota`：agent 与 llm 都有 alias="same"，占住 Agent lease 后 LLM lease 仍可获得。
再测试 Agent limit=1 的第二个 lease 等待、释放后继续；limit=2 可同时进入；取消等待者不损坏 gate；省略 agent_limits 不影响所有旧 constructor 调用。用 asyncio.Event 证明顺序，不靠 sleep 时长。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_quota_manager.py::test_agent_quota_is_independent_of_same_named_llm_quota -v
```

Expected: FAIL：独立 Agent quota 尚不存在；先用接口断言保证测试正常收集。

- [x] **Step 3: Describe the minimal implementation**

```text
ProviderQuotaManager 增加可选 agent_limits=None。
为每个 alias 构造 CapacityGate(limit, provider=alias, quota_kind="agent", ...)，保存到 _agent。
get_agent(name) 返回该 gate。
不修改 CapacityGate 的 acquire/release/cancellation 算法，也不将 Agent gate 放入 _llm。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_quota_manager.py tests/scheduler -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

沿用三种现有 quota map 的结构即可；无需引入 quota registry 或新调度器。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_quota_manager.py tests/scheduler -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 5: 定义 Antigravity 终态 JSON 成功与失败分类

**依赖：** Task 1 的错误分类；尚不启动进程。

**Files:**

- Create: `src/agent_search_gateway/providers/agent/__init__.py`
- Create: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Create/Test: `tests/providers/agent/test_antigravity_cli.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §4.1-4.2

- [x] **Step 1: Write the failing test**

首个用例 `test_terminal_envelope_returns_only_success_response`：给 decoder 退出码 0 和包含 conversation_id/status/response/usage 的完整 JSON，断言仅返回原 response 文本。
每个 failure case 单独迭代：非零退出（即使 SUCCESS）、所有非 SUCCESS 状态（包括 INVALID/WAITING/RUNNING/未知字符串）、缺 status、status 类型错误、非 object JSON、无效 UTF-8、多个 JSON/额外文本、缺 response、response 非字符串、空白 response。
合法 JSON 可包含未使用 metadata；不返回或保存 session ID。SUCCESS + 非空坏 Markdown 在此层仍返回，留给 parser。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py::test_terminal_envelope_returns_only_success_response -v
```

Expected: FAIL：终态 decoder 行为尚未实现；使用测试函数内部的清晰缺失接口 assertion，不以 collection error 代替。

- [x] **Step 3: Describe the minimal implementation**

```text
在 adapter 定义私有 _decode_terminal_response(stdout: bytes, returncode: int) -> str。
先拒绝非零退出：ExecutionFailure(ALL_PROVIDERS_FAILED, 安全文本, reason="nonzero_exit")。
严格解码 UTF-8 和单个 JSON object；拒绝额外 stdout 及非标准 NaN/Infinity JSON 常量。
先要求 status 存在且为 string；JSON/对象/status 结构错误：
    ProtocolFailure(PROTOCOL_ERROR, 安全文本, reason="invalid_json")。
status 为字符串但不是 SUCCESS：
    ExecutionFailure(..., reason="terminal_status_failure")，不要求错误 envelope 含 response。
只有 SUCCESS 才要求 response 存在且为 string；缺失/类型错误仍是 invalid_json。
response 为 string 但 strip 后为空：
    ExecutionFailure(..., reason="empty_response")。
返回原 string；不解析 Markdown，不容忍附加 stdout，不回退提取 URL。
异常只携带固定安全说明，不传播原 JSON/解码器内容。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

将同类错误构造收敛为少量私有 helper；用现有 ProtocolFailure/ExecutionFailure.reason，不新建错误层级或公开状态模型。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 6: 通过真实 subprocess 启动全新 agy 会话

**依赖：** Tasks 4、5。

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Create: `tests/support/agent_cli.py`
- Test: `tests/providers/agent/test_antigravity_cli.py`
- Docs: `docs/designs/architectures/20261022-agent-search-antigravity.md` §5.5

- [x] **Step 1: Write the failing test**

首个用例 `test_runner_spawns_one_fresh_process_and_returns_markdown`：测试 helper 在 tmp_path 写入带 sys.executable shebang 的可执行假 agy，运行真实 create_subprocess_exec，输出完整 SUCCESS envelope；真实 runner 返回其中 Markdown。
假程序向专用测试控制目录记录 pid/cwd/argv（仅测试哨兵数据）。核对一个 -p 参数、原 system/user 文本、--output-format json、--print-timeout "<配置秒数>s"。默认模型路径新增 `test_runner_omits_model_flag_when_model_is_none`：假进程遇到 --model 即失败，缺省时返回 fixture 的默认模型标记，断言真实 response 正常返回且 argv 没有 --model/占位值。显式 pin 路径新增 `test_runner_passes_only_explicit_model`：恰好一组 --model 与原字符串；两个路径的 JSON/timeout flags 均保持。prompt 包含空格、引号、换行、分号和 shell 元字符时仍是单个 argv，外部 marker 不被执行创建。两次调用 pid/cwd 独立，退出后目录删除。无 session resume/自动批准 flags。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py::test_runner_spawns_one_fresh_process_and_returns_markdown -v
```

Expected: FAIL：异步 runner 尚未提供真实进程→终态 response 行为。

- [x] **Step 3: Describe the minimal implementation**

```text
定义 run_antigravity，参数严格按统一接口。
async with quota.lease():
    为本次调用创建私有 TemporaryDirectory，默认权限沿用安全 stdlib 临时目录。
    将 messages 依原序序列化为明确 role 区块，例如 [SYSTEM] + 原内容、[USER] + 原内容；
    不改 prompt builders，也不混入 provider/model selector。
    argv = [command, "-p", serialized_prompt,
            "--output-format", "json", "--print-timeout", "<timeout_seconds:g>s"]。
    if model is not None: argv.extend(["--model", model])。
    model=None 时省略整组参数，不读取用户设置、不推断实际模型、不修改持久配置。
    await create_subprocess_exec(*argv, cwd=private_cwd, env=基础 allowlist,
                                 start_new_session=True, stdin=DEVNULL,
                                 stdout=PIPE, stderr=PIPE)。
    并行按固定 chunk 读取 stdout/stderr，同时等待进程结束。
    将 stdout + returncode 交给 _decode_terminal_response。
    正常退出后删除 cwd，再退出 quota lease。
本步建立正常路径及并行排空；Task 8 补齐全部异常清理，Task 9 补齐累计 byte 上限。不得使用 shell=True 或 unbounded communicate()。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

假 CLI builder 只提供 fixture 所需的行为/控制目录；用系统 Python 和标准库，不依赖 agy 安装、网络或 psutil。夹具自己在 finally 清理测试子进程，保证 RED 也不会留下挂起进程。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 7: 收紧 Agent 环境继承并支持显式 pass_env

**依赖：** Tasks 2、6。

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Modify: `tests/support/agent_cli.py`
- Test: `tests/providers/agent/test_antigravity_cli.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §4.5

- [x] **Step 1: Write the failing test**

首个用例 `test_runner_passes_only_explicit_extra_environment`：用 monkeypatch 设置假 HOME/XDG/cache/locale、OPENAI_API_KEY/TAVILY_API_KEY 哨兵、TEST_HARNESS_SETTING 和另一未知变量；仅 pass_env=["TEST_HARNESS_SETTING"]。由真实假进程报告指定键的可见性，断言基础 auth-cache 位置保留，显式变量存在，其他 provider secrets/未知变量缺失。
补充 pass_env 未设置值时省略；显式点名的敏感变量仅在用户配置指定时允许通过，但其值不得出现在日志/错误；调用前后 daemon os.environ 不变。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py::test_runner_passes_only_explicit_extra_environment -v
```

Expected: FAIL：显式 pass_env 或完整基础执行/auth-cache 白名单尚未按约定传入；不能通过假 runner 自己返回预期字典。

- [x] **Step 3: Describe the minimal implementation**

```text
私有 _curated_environment(pass_env) 每次调用新建 env。
仅复制 PATH、HOME、XDG_CONFIG_HOME、XDG_CACHE_HOME、XDG_DATA_HOME、
XDG_STATE_HOME、XDG_RUNTIME_DIR、TMPDIR/TEMP/TMP、LANG/LANGUAGE/LC_ALL
及明确列举的标准 LC_* locale 名称；不以任意前缀继承所有变量。
追加 pass_env 中已存在的名称对应值。
不复制整个 os.environ、不写回父环境、不将环境值放到 config repr/argv/日志。
spawn 继续在新 cwd 中进行；保留 HOME/XDG 配置位置供 Harness 自己读取已保存的默认模型与认证，不改其 model/MCP/tools/permissions 配置。
缺少认证时沿 provider failure 返回，不自动扩大 env 白名单或启动登录。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

将白名单作为 adapter 内部固定常量，保持可审阅；不为 env 增加单独配置子系统或 secret 管理抽象。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 8: 保证超时、取消和退出异常后的进程组清理

**依赖：** Tasks 6、7；此任务完成后再接入业务 runtime。

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Modify: `tests/support/agent_cli.py`
- Create/Test: `tests/providers/agent/test_antigravity_lifecycle.py`
- Test: `tests/runtime/test_quota_manager.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §4.3

- [x] **Step 1: Write the failing test**

首个用例 `test_timeout_terminates_process_group_before_releasing_quota`：假 agy 通知 ready 后挂起并创建子进程；使用缩短的测试 deadline，断言 runner 抛固定 timeout failure，直属进程已 wait/reap、无存活后代、cwd 已移除，之后同一 gate 可再次进入。
依次覆盖：SIGTERM 被忽略须 SIGKILL；父进程退出但子进程仍持有 pipe；外部 Task.cancel；取消排队中的 quota waiter（零 spawn）；取消发生在 spawn 刚建立、尚未返回 handle 的窗口；清理中的第二次 cancel；spawn FileNotFound/PermissionError；非零退出且有子进程；终态协议失败。每例都有测试自身的最终清理。
通过真实 pid/process group 观察退出与 lease/cwd 后置条件，不能仅断言 kill mock 被调用。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_lifecycle.py::test_timeout_terminates_process_group_before_releasing_quota -v
```

Expected: FAIL：runner 未在短的测试期限内完成清理/未释放可用 gate；外层测试 watchdog 必须有界并能清理假进程。

- [x] **Step 3: Describe the minimal implementation**

```text
quota acquired 后启动每调用 Python deadline = timeout_seconds + 5 秒，包含 spawn、stream IO 和 wait。
保留 spawn task/process handle；即使在 create_subprocess_exec 返回窗口被取消，也先收束创建任务并取得 handle，再执行清理，不能遗失已创建的 child。
所有异常退出路径调用同一 terminate/reap helper：
    若已创建独立进程组，向组发 SIGTERM；ProcessLookupError 视为已退出；
    最多等待 2 秒，必要时向组发 SIGKILL；
    不因 leader.returncode 已设置就跳过仍存活后代的组清理；
    await 直属 process.wait()；收束/取消并 await 所有 reader/wait task；
    最后移除 cwd；之后才释放 quota。
timeout -> ExecutionFailure(ALL_PROVIDERS_FAILED, 安全文本, reason="timeout")。
spawn OSError -> reason="spawn_failure"，不暴露异常路径/内容。
CancelledError -> 等待受保护的 cleanup 完成，再重抛；
shield 必须有被持有并最终 await 的 cleanup task，不能产生后台遗留清理。
正常终态也收尾该 session 的剩余后代；保留主进程退出码供 envelope 校验。
Gateway 可 wait/reap 直属子进程；后代以进程组终止并验证无存活进程，不假设能跨平台 wait 非直属孙进程。
不接入 retry_async。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_lifecycle.py tests/providers/agent/test_antigravity_cli.py tests/runtime/test_quota_manager.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

只在 adapter 内抽取清理 helper；将短超时/信号处理细节集中。测试 monkeypatch 私有时间常量或使用短 timeout，不添加仅为测试暴露的生产 API。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_lifecycle.py tests/providers/agent/test_antigravity_cli.py tests/runtime/test_quota_manager.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 9: 限制 stdout/stderr 且避免管道死锁

**依赖：** Task 8。

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Modify: `tests/support/agent_cli.py`
- Test: `tests/providers/agent/test_antigravity_lifecycle.py`
- Test: `tests/providers/agent/test_antigravity_cli.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §4.4

- [x] **Step 1: Write the failing test**

首个用例 `test_stdout_over_limit_fails_and_reaps_child`：假进程先输出看似成功的 JSON，再持续写到超过 4 MiB；必须 output_limit failure，不能返回部分 response。独立测试 stderr 超过 256 KiB。
边界覆盖恰好上限/上限+1 byte、多字节 UTF-8 按 bytes 计、stderr 先大量写入后 stdout 完成、两条流交错写入、一个 reader 超限时另一条仍阻塞。超限路径继续断言 process/group、reader task、cwd 和 quota 已收尾。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_lifecycle.py::test_stdout_over_limit_fails_and_reaps_child -v
```

Expected: FAIL：超量 stdout 被接受或没有及时触发 output_limit；测试使用外层有界 watchdog。

- [x] **Step 3: Describe the minimal implementation**

```text
stdout/stderr 并行 drain，以例如 64 KiB 固定 chunk 读取。
在 append 之前累计 bytes；stdout 超 4*1024*1024、stderr 超 256*1024 即中断。
stderr 只需计数/排空，不为 diagnostics 保存正文。
协调 reader 与 wait task；任一 reader overflow 时立即走 Task 8 的组清理，
不能用 gather(return_exceptions=True) 等待另一个永不结束的 reader 才发现失败。
overflow -> ExecutionFailure(..., reason="output_limit")。
仅在进程退出和两条流完整 EOF 后调用 terminal decoder；
不能截断 JSON、不能接受超时/超限前的部分有效输出。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

将 stream byte-limit 逻辑复用为一个私有读取 helper；异常清理只保留 Task 8 的一份实现。保留边界值与真实双管道回归。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 10: 在 LLMStages 增加 Agent facade，并整理 provider 家族

**依赖：** Tasks 1、5-9。

**Files:**

- Modify: `src/agent_search_gateway/providers/contracts.py:3-12`
- Modify: `src/agent_search_gateway/llm/stages.py:29-47,101-125`
- Create/Test: `tests/unit/test_agent_stages.py`
- Create: `src/agent_search_gateway/providers/llm/__init__.py`
- Move: `src/agent_search_gateway/providers/openai_chat.py` → `src/agent_search_gateway/providers/llm/openai_chat.py`
- Move: `tests/providers/test_openai_chat.py` → `tests/providers/llm/test_openai_chat.py`
- Modify: `src/agent_search_gateway/runtime.py:38`
- Modify: `tests/integration/test_live_tavily_and_openai.py:10`
- Test: `tests/unit/test_llm_stages.py`, `tests/unit/test_llm_paper_stage.py`, `tests/unit/test_llm_judge_stage.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_web_stage_uses_existing_prompt_messages`：注入记录参数的 async callable，调用 agent_search_markdown；断言 model 等于 invocation.model，完整 messages 精确等于现有 llm_search_messages(prompt)，返回既有 strip/nonempty 约定的文本。
再添加 paper prompt equality；Web/Paper 两个 facade 均参数化 model=None 与显式字符串，断言原样传递，不用 fetch/LLM model 补值。另覆盖同名 Agent/LLM alias 分离、missing runner 安全失败、空文本拒绝、CancelledError 重抛。Agent 不调用 complete_text/complete_json，也不触发 llm_stage_*；六个旧方法继续只调用 LLMClient，错误和日志不变。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_agent_stages.py::test_agent_web_stage_uses_existing_prompt_messages -v
```

Expected: FAIL：Agent facade 尚未存在/未把真实 prompt sequence 交给 callable。

- [x] **Step 3: Describe the minimal implementation**

```text
在 contracts.py 添加 AgentTextRunner 类型别名。
LLMStages.__init__ 新增可选 keyword-only agent_runners，复制为独立 map。
两个 Agent 方法按 invocation.provider 取 runner，使用原 web/paper prompt builder，
await runner(invocation.model, messages)，model 可为 None；仅对返回文本做 string/nonempty/strip 校验后返回。
missing runner/空 response 使用 Agent 的安全 ExecutionFailure，不借用 LLM_STAGE_FAILED。
不调用 _run_text_stage（该 helper 固定发 llm_stage_*）；Agent provider 生命周期由 orchestrator 统一记录。
facade 不知道 subprocess，不解析 Markdown，不接触 URLStore。
现有六个方法和 LLMClient 协议保持原实现。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_agent_stages.py tests/unit/test_llm_stages.py tests/unit/test_llm_paper_stage.py tests/unit/test_llm_judge_stage.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

在 GREEN 后机械移动 OpenAI adapter/test 到 llm/。adapter 原 ..concurrency/errors/models/observability/retry 改为 ...；原 .contracts/.http 改为 ..；更新上述 runtime 和 integration imports。保留类、HTTP payload/headers/retry/extra_body/quota/aclose 原样，不增加旧路径 shim。旧测试按新路径完整执行；普通集成模块仍保持 opt-in skipped。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_agent_stages.py tests/unit/test_llm_stages.py tests/unit/test_llm_paper_stage.py tests/unit/test_llm_judge_stage.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/llm/test_openai_chat.py tests/runtime/test_runtime_assembly.py tests/integration/test_live_tavily_and_openai.py -q
uv run mypy src tests scripts
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 11: 接入 Agent Web 搜索并提取共享 Web 流程

**依赖：** Tasks 1、3、10。

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:39-65,139-282,326-365`
- Create: `tests/support/agent_search.py`
- Create/Test: `tests/orchestrators/test_agent_search.py`
- Test: `tests/orchestrators/test_llm_search.py`
- Test: `tests/orchestrators/test_scoped_llm_search.py`
- Test: `tests/orchestrators/test_keyword_search_pipeline.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_web_search_writes_existing_web_schema`：真实 stages/store/writer，注入 Agent callable 返回 ## Result；agent_search 产出 agent-<id>.jsonl，内容逐字节为 url/abstract 两键，URL 已在同一 store。
逐个加入默认/显式 web 等价、URL normalization/dedup、完成顺序相反仍按 invocation 顺序写入、一个坏 Markdown provider 不污染 store 且其他结果存活、合法空 abstract 形成成功空结果、全部失败无文件。LLM 默认/显式 Web bytes 与原日志继续通过。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py::test_agent_web_search_writes_existing_web_schema -v
```

Expected: FAIL：尚无 Agent Web workflow。

- [x] **Step 3: Describe the minimal implementation**

```text
constructor 新增可选 agent_invocations=()，保存独立 tuple；不覆盖 _llm_invocations。
新增 agent_search：先按约定校验 request_id/prompt/scope，再确认 Agent invocation 列表非空。
初始无 selector 路径执行配置 tuple；Task 12 补齐全 selector 矩阵。
提取 _run_text_web_pipeline(invocation, prompt, generate, kind)：
    await 对应 facade；parse_search_markdown；一份完整 list 才视为成功。
提取 _text_web_records：ordered gather(return_exceptions=True)，至少一个 list 即成功（[] 也成功），
按 outcomes 原序 admit + normalize URL dedup，最后从现有 store 生成 canonical SearchRecord。
LLM Web 使用原 llm facade、原 label/error/log/filename；Agent 使用 agent facade 和 agent kind。
共享写入最终收拢进 _text_search；本步只驱动 Web，旧 LLM paper/all 保持可运行，后续 Tasks 14-15 再抽取。
不改 keyword pipelines、parse_search_markdown 或 URLStore。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py tests/orchestrators/test_llm_search.py tests/orchestrators/test_scoped_llm_search.py tests/orchestrators/test_keyword_search_pipeline.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

使用受约束 TypeVar + typed generation callable；避免 Any/cast(LLMInvocation, agent)。将测试辅助仅包含的 runner 记录与组装放入 tests/support/agent_search.py；测试断言真实文件和 admission，而非仅 fake call count。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py tests/orchestrators/test_llm_search.py tests/orchestrators/test_scoped_llm_search.py tests/orchestrators/test_keyword_search_pipeline.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 12: 实现 Agent provider/model 的请求局部选择

**依赖：** Task 11。

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:139-208`（及新 Agent entry/helper）
- Create/Test: `tests/orchestrators/test_agent_provider_model_filtering.py`
- Modify: `tests/support/agent_search.py`
- Test: `tests/orchestrators/test_llm_invocation_selection.py`
- Test: `tests/orchestrators/test_llm_provider_model_filtering.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §3.1-3.2

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_provider_filter_executes_only_matching_invocations`：多个 alias/model（包含 model=None 与显式 pin），选择一个 provider，实际输出与 runner 记录包含该 alias 的默认及显式模型 entry，后续无 selector 请求仍执行全部。
逐个加入 model-only、intersection、精确大小写、重复 entry（含重复默认模型 entry）两次执行、缺 provider/model/两者均缺/两者各自存在但组合不存在、显式空白 selectors、Agent 配置为空且 selectors 无效的优先级。
新增 `test_agent_model_filter_excludes_unpinned_invocations`：model selector 只匹配显式 model；default-only alias 即使 Harness 默认可能是该模型也 BAD_REQUEST、零调用；不做发现、不把 selector 注入 model=None entry。无 model selector 时 None entry 正常执行。
选择 LLM-only 或仅在 agent_providers 声明但未列入 search_agent 的 alias 均 BAD_REQUEST，无调用/无文件/无 admission。保留非空服务端 selector 的精确字符串；trim 只由 CLI 做。
补充直接调用 agent_search 的无效 request ID、空 prompt、非法 scope：零 runner 调用、无文件；空 prompt/非法 scope 优先于空 Agent 配置错误。
先覆盖 web；Tasks 14-15 把同矩阵扩展到 paper/all。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_provider_model_filtering.py::test_agent_provider_filter_executes_only_matching_invocations -v
```

Expected: FAIL：选择一个 Agent provider 仍执行了未选 entry。

- [x] **Step 3: Describe the minimal implementation**

```text
将现有 LLM 选择算法提取为 _select_text_invocations，参数包含 typed tuple、label、no-provider code。
_select_llm_invocations 传原 tuple/label="LLM"/NO_LLM_SEARCH_PROVIDERS；
_select_agent_invocations 传 Agent tuple/label="Agent"/NO_AGENT_SEARCH_PROVIDERS。
保留所有原 LLM 错误原文、验证优先级和 tuple identity/entry identity。
Agent 错误原文使用 error spec 的五种精确 mismatch 消息。
model selector 为 None 时不过滤模型，保留 default/pinned entries；selector 为字符串时仅 item.model == selector 的显式配置匹配。
不把 AgentInvocation.model=None 当作 wildcard；不将请求 model 写入任何 invocation，也不调用 Harness 获得默认模型。
所有选中 tuple 显式传入 records/pipeline；配置 tuple 不排序、不去重、不修改。
每个请求单独筛选，异步并发请求不共享 selector 状态。
无可用 Agent 配置先报 no-provider；CLI 显式空 selector 在 socket 前拒绝是另一层。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_provider_model_filtering.py tests/orchestrators/test_llm_invocation_selection.py tests/orchestrators/test_llm_provider_model_filtering.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

LLM 和 Agent 仅保留小 wrapper，不复制两套筛选矩阵。以旧测试所有精确 messages/额外参数 assertions 作为提取约束。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_provider_model_filtering.py tests/orchestrators/test_llm_invocation_selection.py tests/orchestrators/test_llm_provider_model_filtering.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 13: 扩展论文来源和 generated-source 优先级

**依赖：** Task 3 的 TextSearchKind。

**Files:**

- Modify: `src/agent_search_gateway/paper_search_parser.py:32-91`
- Modify: `src/agent_search_gateway/academic/aggregator.py:26,175-195,259-264`
- Test: `tests/unit/test_paper_search_parser.py`
- Test: `tests/academic/test_paper_aggregator_identity.py`
- Test: `tests/academic/test_paper_aggregator_merge.py`
- Test: `tests/academic/test_identifier_normalization.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_paper_parser_tags_agent_source_when_requested`：相同严格 Paper block，通过 source_kind="agent" 得到 agent:X；未传 source_kind 仍为 llm:X，其他字段逐项相同。
增加 academic/LLM/Agent 具有相同 DOI/arXiv 的 hits：academic 非空 metadata 优先，generated sources 按配置 priority 确定，旧 llm 相对顺序完全保留；未配置 agent source 同 llm 使用 generated fallback rank。
覆盖相同强 ID 聚合、不同 source native ID 不碰撞、弱 fingerprint 限制、sources/citation_counts 保留完整命名空间。原坏 URL/date/grammar/identifier 用例保持严格。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_paper_search_parser.py::test_paper_parser_tags_agent_source_when_requested -v
```

Expected: FAIL：source_kind 扩展尚未提供，或输出仍被标为 llm:X；用清晰接口断言而非意外 TypeError 作为首个 RED。

- [x] **Step 3: Describe the minimal implementation**

```text
parse_paper_markdown 新增 keyword-only source_kind: TextSearchKind="llm"。
将 source_kind 传入 _parse_block，唯一语义变化是 source=f"{source_kind}:{provider}"。
保留原 provider normalization、grammar、字段解析及旧 LLM 错误文本。
PaperAggregator 将 _LLM_PREFIX 替换为 generated prefixes ("llm:", "agent:")，
同时修改 precedence 第一项和 _source_rank fallback 两处判定。
保留完整 source name，不移除前缀后比较 alias；其余 identity/merge/citation 算法不变。
academic.normalization 已支持非特定 provider 的 source_id，验证其兼容即可，无需改写。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_paper_search_parser.py tests/academic -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

只复用一个 generated-prefix tuple/判定，避免修改一处漏掉另一处；不建立第二份 Agent parser 或论文 aggregator。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_paper_search_parser.py tests/academic -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 14: 复用论文 finalization 完成 Agent Paper 搜索

**依赖：** Tasks 11-13。

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:60-62,216-224,284-305,367-406`
- Test: `tests/orchestrators/test_agent_search.py`
- Test: `tests/orchestrators/test_agent_provider_model_filtering.py`
- Modify: `tests/support/agent_search.py`
- Test: `tests/orchestrators/test_scoped_llm_search.py`
- Test: `tests/academic/test_oa_enrichment.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_paper_search_uses_shared_aggregation_and_admission`：两个 Agent 返回重复 DOI Paper block、一个返回坏 grammar；真实 PaperAggregator/finalize_paper_hits 加 FakeOAResolver，输出一个去重的 agent-* PaperRecord，sources 为幸存 agent:*，OA 调用仅一次，landing URL 入库而 PDF 不入库。
依次覆盖每 entry 仅生成/解析一次、独立有效 hits、parser 成功但 aggregation 全丢弃仍是成功空结果；paper-only 无 type。
将 Task 12 选择器矩阵纳入 paper，确保未选择 entry 不参与 resolver/store。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py::test_agent_paper_search_uses_shared_aggregation_and_admission -v
```

Expected: FAIL：Agent paper 流程未连接共享 parser/finalization，或来源/结果 schema 不正确。

- [x] **Step 3: Describe the minimal implementation**

```text
提取 _run_text_paper_pipeline：一次 await generate，一次 parse_paper_markdown(..., source_kind=kind)。
提取 _text_paper_records：gather + 成功 list 判定，按原 invocation 顺序展平 hits，
调用原 finalize_paper_hits(aggregator, resolver, store)；不复制 OA/身份处理。
LLM 使用 kind="llm"，保留原错误文本与 llm_paper_search logs；
Agent 使用 kind="agent"、agent_paper_search 和 Agent 文本。
SearchOrchestrator 默认 aggregator priority 在既有 llm:* 后追加 agent:*；
不影响显式传入的共享 aggregator，Runtime 的 priority 在 Task 17 统一组装。
通过 writer.write_paper_results(kind, records, request_id=...) 写出。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py tests/orchestrators/test_agent_provider_model_filtering.py tests/orchestrators/test_scoped_llm_search.py tests/academic/test_oa_enrichment.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

把两种后端的 Paper 记录处理收敛为同一 helper；保留 Task 13 的 parser default，以免旧直接调用意外得到 agent:。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py tests/orchestrators/test_agent_provider_model_filtering.py tests/orchestrators/test_scoped_llm_search.py tests/academic/test_oa_enrichment.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 15: 完成共享 All scope 与部分成功矩阵

**依赖：** Tasks 3、4、11-14。

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py:193-324`
- Test: `tests/orchestrators/test_agent_search.py`
- Test: `tests/orchestrators/test_agent_provider_model_filtering.py`
- Test: `tests/orchestrators/test_scoped_llm_search.py`
- Test: `tests/orchestrators/test_llm_provider_model_filtering.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_all_schedules_both_branches_and_writes_web_then_paper`：用 Event 屏障证明两分支都被调度，再允许各自返回，真实 mixed writer 输出 Web 在前/Paper 在后。
逐个覆盖双成功、仅 Web 成功、仅 Paper 成功、双失败、Web 成功空 list + Paper 失败、Paper 聚合成功空 list + Web 失败、parser 失败但 sibling 存活。双失败无文件；空成功不能按 truthiness 当失败。
使用绑定真实 gate 的 runner：limit=2 的两分支重叠，limit=1 排队串行；每个分支是独立调用。Agent selector 矩阵扩展 all，包括重复 entry 每分支各运行一次。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py::test_agent_all_schedules_both_branches_and_writes_web_then_paper -v
```

Expected: FAIL：Agent all 尚未同时运行两分支或未生成正确的 mixed 结果。

- [x] **Step 3: Describe the minimal implementation**

```text
完成 _text_search(prompt, request_id, scope, kind, invocations, generate_web, generate_paper)。
web/paper 调用对应共享 records helper + writer；
all 使用 gather(web_records, paper_records, return_exceptions=True)。
两边均 Exception -> 原有 ALL_PROVIDERS_FAILED（LLM 原文不变、Agent label 对应改变）。
任一成功 list（包括 []） -> 只取成功分支，write_mixed_results(..., kind=kind)。
_log_results_written 增加 compatible kind；_log_branch_failure 按后端发独立事件，
旧 llm_search_branch_failed 与 LLM metadata 不变。
llm_search / agent_search 仅保留各自校验、选择和 typed facade 注入，
不在两入口各维护一份 Web/Paper/All 流程。
outer task cancellation 必须取消并收束所有 gather children；不把外部取消变成 partial success。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py tests/orchestrators/test_agent_provider_model_filtering.py tests/orchestrators/test_scoped_llm_search.py tests/orchestrators/test_llm_provider_model_filtering.py -q
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

删除已被共享 helper 完整替代的重复 LLM records/pipeline 实现，但保留原 _select_llm_invocations wrapper。禁止用全部结果非空作为成功条件；保留每个失败矩阵的精确字节/错误断言。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search.py tests/orchestrators/test_agent_provider_model_filtering.py tests/orchestrators/test_scoped_llm_search.py tests/orchestrators/test_llm_provider_model_filtering.py -q
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 16: 为整个 Agent 工作流设置 deadline

**依赖：** Tasks 8、15。

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py`（agent_search entry）
- Create/Test: `tests/orchestrators/test_agent_search_deadline.py`
- Modify: `tests/support/agent_search.py`
- Test: `tests/providers/agent/test_antigravity_lifecycle.py`
- Docs: `docs/designs/error-handlings/20261022-agent-search-antigravity.md` §4.3

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_workflow_deadline_cancels_running_and_queued_invocations`：同一 limit=1 provider 有两个 entry，首个运行、第二个排队，缩短 _AGENT_WORKFLOW_TIMEOUT_SECONDS；断言整体 ALL_PROVIDERS_FAILED、安全 timeout message、reason=timeout、无结果文件、排队任务不再启动、全部 lease 回收。
分别覆盖 Web/Paper/All，含一个 all branch 已成功而另一个不结束、OA finalization 阻塞、请求显式取消。超时不能把已完成 sibling 写为成功；普通单 provider timeout 仍按原 partial-failure 处理。LLM 请求不受这个新增 deadline 影响。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search_deadline.py::test_agent_workflow_deadline_cancels_running_and_queued_invocations -v
```

Expected: FAIL：请求仍挂起/排队调用仍启动或成功写文件；测试外层 watchdog 有界。

- [x] **Step 3: Describe the minimal implementation**

```text
定义私有 _AGENT_WORKFLOW_TIMEOUT_SECONDS=1140.0。
在 agent_search 校验/选择完成后，用 asyncio.timeout 包住整个 _text_search await。
仅将这一层 deadline 到期映射为 ExecutionFailure(ALL_PROVIDERS_FAILED,
"Agent search workflow timed out", reason="timeout")。
让 cancellation 传播到 gather children、quota waiters、runner 和 OA resolver；
等待其清理结束后再交 daemon 返回错误。
外部 CancelledError 原样重抛。
writer 只在正常完成共享 workflow 后路径中执行，不在 timeout/取消 except/finally 中写部分结果。
保持既有 store admission 行为，不增加跨分支 URLStore rollback。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search_deadline.py tests/orchestrators/test_agent_search.py tests/providers/agent/test_antigravity_lifecycle.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

统一 Agent workflow deadline 常量；测试缩短该常量即可，不增加用户 timeout flag 或无期限 override。不得把 llm_search 包进新 deadline。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_agent_search_deadline.py tests/orchestrators/test_agent_search.py tests/providers/agent/test_antigravity_lifecycle.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 17: 在 Runtime 绑定 Agent runners、配额和论文优先级

**依赖：** Tasks 2、4、8-10、13、16。

**Files:**

- Modify: `src/agent_search_gateway/runtime.py:83-198,353-385`
- Create/Test: `tests/runtime/test_agent_runtime_assembly.py`
- Test: `tests/runtime/test_runtime_assembly.py`
- Test: `tests/runtime/test_academic_runtime_assembly.py`
- Test: `tests/runtime/test_request_filtering.py`
- Modify: `tests/support/agent_cli.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_runtime_agent_search_uses_configured_cli_without_llm_transport`：真实 resolve_config + Runtime.build，Agent command 指向假 CLI，search_agent entry 省略 model，HTTP transport 为离线 MockTransport；执行 Agent Web 后文件正确，实际进程 argv 无 --model，搜索阶段没有 LLM HTTP 请求。随后加入显式 pin 变体，断言 argv 精确传递该值；启动时不解析/缓存 Harness 默认模型。
逐个覆盖多个 alias 的 command/timeout/pass_env 正确绑定（防闭包晚绑定）、重复 entry 共用 alias gate但独立执行、与 LLM alias 同名、只有 Agent search 但 fetch 有效、无 Agent config 的旧 runtime、未选择 alias 零执行、无 agy 安装也能 build。
另设 retry.max_attempts=3 且假 Agent 非零退出，断言每个选中 entry 只启动一次，证明未套用 HTTP/LLM retry。
验证 runtime.aclose 原 HTTP 资源只关闭一次；runner 不需要 aclose/持久会话；真实论文 priority 包含 academic、旧 llm、agent 且旧序不变。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_agent_runtime_assembly.py::test_runtime_agent_search_uses_configured_cli_without_llm_transport -v
```

Expected: FAIL：Runtime 未注入 Agent invocations/runners，或错误走 LLM/no-provider。

- [x] **Step 3: Describe the minimal implementation**

```text
ProviderQuotaManager 传 agent_limits={name:max_concurrency}。
新增 Runtime._build_agent_runners(agent_config, quotas) -> dict[str, AgentTextRunner]。
对每个已引用配置 harness="antigravity"，用 partial 或绑定参数的局部 callable 封装：
    run_antigravity(model, messages, command=..., timeout_seconds=...,
                    pass_env=..., quota=quotas.get_agent(alias))。
不启动 subprocess；不调用 models/login；未知 harness 用 ConfigFailure 防御性拒绝。
LLMStages 传 agent_runners；SearchOrchestrator 传 agent_invocations；model=None 原样经过绑定 callable 到 runner，不在 Runtime 补默认模型。
共享 PaperAggregator priority = academic names + 原 llm:* + agent:*。
_build_llm_clients 和 Runtime.aclose 保持 HTTP 所有权；active process 的清理归 runner task。
无需新增通用 registry、AgentClient 类或为测试暴露 runtime 内部 maps。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_agent_runtime_assembly.py tests/runtime/test_runtime_assembly.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_request_filtering.py -q
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

factory 只做首个 Harness 的最小分派。所有 per-alias 参数在绑定时固定；不要依赖循环变量的延迟捕获。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_agent_runtime_assembly.py tests/runtime/test_runtime_assembly.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_request_filtering.py -q
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 18: 添加 Agent 请求帧和按请求类型选择 socket deadline

**依赖：** Tasks 1、3；无需启动 Agent 或 daemon。

**Files:**

- Modify: `src/agent_search_gateway/models.py:103-127`
- Modify: `src/agent_search_gateway/protocol.py:12-28,67-107,172-189,239-268`
- Test: `tests/unit/test_protocol_codec.py`
- Test: `tests/unit/test_search_request_selectors.py`
- Test: `tests/unit/test_socket_client.py`
- Docs: `docs/designs/testings/20261022-agent-search-antigravity.md` §3.2

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_search_frame_round_trips_without_default_fields`：从现有 decode_request_frame 输入 {"type":"agent_search","prompt":"topic"}，首先断言不返回 ErrorResponse，再核对 typed fields、默认 web、编码回原 bytes。
逐一覆盖 paper/all/provider/model/组合、缺字段、非字符串/null selectors、未知字段、scope 非法、未知 request type、响应错误 code round-trip。空 selector 字符串保留给业务校验，不能 codec 默默删掉。
socket 用受控 stream/timeout recorder：Agent 默认1200、其他全部旧请求默认300、显式0.01/自定义数值覆盖默认；保留响应 framing、oversize 和 disconnect tests。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_protocol_codec.py::test_agent_search_frame_round_trips_without_default_fields -v
```

Expected: FAIL：当前 decoder 返回 Unknown request type；旧 frame 用例仍应正常通过。

- [x] **Step 3: Describe the minimal implementation**

```text
复用 Task 1 已定义的 SearchScope / LLMSearchScope 兼容 alias。
增加 frozen AgentSearchRequest，并扩展 Request union。
protocol 新增 _parse_agent，与 LLM 保持相同字段/严格类型规则但构造 AgentSearchRequest；
注册 agent_search；编码省略 scope=web 和未设 selectors，保留原请求字段顺序。
send_request 的 response_timeout_seconds 改为 float|None=None：
    显式值不是 None -> 直接用；
    AgentSearchRequest -> 1200.0；
    其他 -> 原 _RESPONSE_TIMEOUT_SECONDS=300.0。
保持 reader limit、连接失败、ErrorResponse 和最终关闭行为。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_protocol_codec.py tests/unit/test_search_request_selectors.py tests/unit/test_socket_client.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

可复用少量 schema 字段检查，但不要把 Agent 解码成 LLM request，或改动旧 request/response bytes。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_protocol_codec.py tests/unit/test_search_request_selectors.py tests/unit/test_socket_client.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 19: 注册 agent-search CLI 并保持文档命令清单同步

**依赖：** Task 18。

**Files:**

- Modify: `src/agent_search_gateway/cli.py:15-25,47-115`
- Test: `tests/cli/test_cli.py`
- Modify: `tests/docs/test_documented_config.py:95-160`
- Modify: `README.md`（新增独立 Agent search 小节与最小可解析示例）
- Docs: `docs/designs/testings/20261022-agent-search-antigravity.md` §3.1

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_cli_builds_independent_request`：build_parser + run_command 使用记录 socket request 的 fake client，默认命令构造 AgentSearchRequest，成功 stdout 仅绝对结果路径及单个换行。
参数化 scope/provider/model/组合、prompt与selectors trim、空 prompt/空 selectors 在 client 前失败、非法 scope、旧命令不接受 Agent-only 不适用 flags；错误 stderr/nonzero exit。
同步现有 README/CLI 子命令集合测试，不能出现 CLI 新命令已实现但 docs contract suite 必然失败的中间状态。旧 provider/model 小节的精确示例集合保持不变，Agent 示例放独立小节。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/cli/test_cli.py::test_agent_cli_builds_independent_request -v
```

Expected: FAIL：argparse 当前不识别 agent-search；将 SystemExit 以预期命令缺失 assertion 表达。

- [x] **Step 3: Describe the minimal implementation**

```text
新增 agent-search parser：prompt、--scope(web|paper|all, default web)、
--provider NAME、--model NAME。
_request_from_args 新分支复用 prompt.strip 与 _normalize_selector；
构造 AgentSearchRequest，不把调用改写为 llm-search。
复用 run_command 的 socket/send、SuccessResponse/stdout、ErrorResponse/stderr 路径。
README 增加独立章节及不带 --model 的最小 agent-search 示例；注明该 flag 是显式配置模型的筛选器，不会覆盖 Harness 默认设置；
文档测试 regex/预期命令集合增加 agent-search，保留旧命令与旧选择器断言。
完整配置/限制文档由 Task 23 补齐。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/cli/test_cli.py tests/docs/test_documented_config.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

CLI 继续是薄客户端。不要在 CLI 执行 agy、读 provider 配置或保存 request state。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/cli/test_cli.py tests/docs/test_documented_config.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 20: 接入 daemon 分发、请求 ID 和 shutdown 清理

**依赖：** Tasks 8、16-18；CLI Task 19 已完成。

**Files:**

- Modify: `src/agent_search_gateway/daemon.py:15-36,43-56,98-107,324-438`
- Modify/Test: `tests/daemon/test_daemon_dispatch.py`
- Modify/Test: `tests/daemon/test_daemon_request_ids.py`
- Modify/Test: `tests/daemon/test_daemon_shutdown.py`
- Modify/Test: `tests/daemon/test_daemon_debug.py`（含 LifecycleSearch 测试替身）
- Create/Test: `tests/daemon/test_agent_shutdown.py`
- Test support: `tests/support/agent_cli.py`, `tests/support/agent_search.py`

- [x] **Step 1: Write the failing test**

首个用例 `test_daemon_dispatches_agent_request_with_selectors_and_reserved_id`：真实 Unix socket，recording runtime 接收全部 Agent 参数与 reserved request_id，命令日志为 agent-search；固定 ID 工厂验证 agent 文件碰撞会重选。
加入并发请求不同 selector 的独立结果、后续无 selector 调用无污染、无配置/坏选择直接返回 typed errors。
专用 shutdown 测试用真实 fake-CLI runner，在 ready 后通过已有 shutdown_waiter seam 触发原10秒grace到期路径；stop 后无存活进程、gate可用、cwd删除、runtime关闭一次、socket删除。新增请求在 shutting_down 时仍被拒绝。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/daemon/test_daemon_dispatch.py::test_daemon_dispatches_agent_request_with_selectors_and_reserved_id -v
```

Expected: FAIL：daemon 当前把 Agent request 当未知请求，或没有正确转发参数。

- [x] **Step 3: Describe the minimal implementation**

```text
_SearchOrchestratorLike 新增与真实 agent_search 一致的方法。
_command_name 明确 Agent -> "agent-search"。
_dispatch 业务请求识别和 is_search reservation 都加入 AgentSearchRequest；
_dispatch_business/_invoke_workflow 类型 union 同步更新。
_invoke_workflow 在 URLFetch fallback 之前分派到 runtime.search_orchestrator.agent_search。
更新四个既有 daemon test 文件中所有 Search protocol 替身，保持 strict mypy。
复用 _shutdown_coordinator 的10秒grace→cancel→gather；
runner cleanup 由任务取消触发，不添加第二套 daemon process registry。
若测试证明收束链存在缺口，只在已有取消/await链最小修补，不修改 grace 和旧 stop 语义。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/daemon -q
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

统一新增类型分支，防止 _command_name 或 _invoke_workflow 默认落到 url-fetch。明确 shutdown readiness 只有任务 cleanup 完成后才置位；保留旧错误净化流程。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/daemon -q
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 21: 完成真实 Unix socket 的离线端到端验收

**依赖：** Tasks 17-20。

**Files:**

- Create/Test: `tests/acceptance/test_agent_search_workflows.py`
- Test support: `tests/support/agent_cli.py`, `tests/support/agent_search.py`
- Reuse: `tests/acceptance/test_gateway_workflows.py::_running_daemon`
- Test: `tests/acceptance/test_gateway_workflows.py`
- Docs: `docs/designs/testings/20261022-agent-search-antigravity.md` §7.2、§9

- [x] **Step 1: Write the failing test**

新增 `test_agent_cli_socket_to_fake_cli_and_followup_fetch`：有效 TOML 经真实 resolve_config/Runtime.build；command 为假 agy；所有 HTTP 走 MockTransport；真实 CLI run_command → send_request → Unix socket → daemon → shared orchestrator → subprocess → parser/store/writer。
断言 stdout 只有实际绝对 agent JSONL 路径，Web/Paper/All 三类 schema 正确、paper 来源为 agent:*。从结果取得 landing URL 后执行原 url-fetch，实际使用同一 store 和既有 fetch LLM 阶段；PDF-only URL 不能因 metadata 自动 admission。
首个验收使用省略 model 的 Agent entry，假 CLI 在收到 --model 时拒绝并在缺省时产出默认模型标记；断言标记出现在真实结果中，证明默认路径贯穿配置、socket、runtime 和 subprocess。独立场景覆盖显式 model 配置并用 provider/model 选择、default-only 配置加 model selector 无匹配且零 spawn、不选调用未触发、重复默认 entry 独立、并发与后续无 selector 请求、部分成功、全失败没有新文件、只有 Agent search 的 runtime 仍需有效 fetch。
所有断言基于返回文件/HTTP边界/进程观察，不替换 parser、finalization、URLStore 或 writer。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/acceptance/test_agent_search_workflows.py::test_agent_cli_socket_to_fake_cli_and_followup_fetch -v
```

Expected: 若任一 CLI/codec/dispatch/runtime 接缝遗漏，FAIL 在对应真实结果或 admission 断言。若前序实现已完整使其直接 PASS，记录为集成验收证据；本任务不为制造 RED 回退正确实现。

- [x] **Step 3: Describe the minimal implementation**

```text
先运行完整真实离线链路。
若发现实际缺口，保留该失败断言，定位到前述相应生产文件并只修补缺失连接；
不得以 fake SuccessResponse、预写 JSONL 或绕过真实 subprocess 让验收变绿。
使用临时 config/socket/results，HTTP MockTransport 仅隔离外部网络。
确保 daemon/process fixture 在失败时也执行清理，测试不会修改真实用户 config/cache。
本任务不增加新产品行为，验证前序任务组合。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/acceptance/test_agent_search_workflows.py tests/acceptance/test_gateway_workflows.py tests/daemon/test_agent_shutdown.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

只提取被多个验收场景实际复用的 fixture。保留旧 acceptance suite，不合并/删减其对旧命令的精确 assertions。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/acceptance/test_agent_search_workflows.py tests/acceptance/test_gateway_workflows.py tests/daemon/test_agent_shutdown.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 22: 完成 Agent 安全诊断与 doctor 无网络回归

**依赖：** Tasks 5、8-17、20。

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py`（共享 provider/branch logging；原 307-324、576-591）
- Modify if needed: `src/agent_search_gateway/providers/agent/antigravity_cli.py`（固定 reason 映射）
- Create/Test: `tests/observability/test_agent_event_logging.py`
- Test: `tests/observability/test_provider_event_logging.py`, `tests/observability/test_redaction.py`
- Test: `tests/doctor/test_config.py`, `tests/doctor/test_no_network.py`
- Inspect only: `src/agent_search_gateway/doctor.py:111-153`

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_failure_logs_allowlisted_reason_without_payload`：真实失败 runner（stderr、JSON error、prompt、response、env 值各含不同哨兵）经共享搜索路径触发 provider_failed；断言 provider/model/stage/elapsed_ms 和固定 reason 完整，全部哨兵不出现在 DEBUG/stderr/用户错误中。
分别覆盖成功、invalid_json、nonzero_exit、terminal_status_failure、invalid_markdown、timeout、output_limit、取消、未知 RuntimeError、恶意 reason 值；Agent 两个 stage 名分别正确，没有 llm_stage_*。
默认模型路径的 Agent 事件传入 model=None，断言已有 formatter 输出 model=null；显式 pin 记录配置值，不读取/伪造实际默认模型。补充 doctor：省略 model 的有效 Agent config + 不存在 command 仍只进行本地检查；显式 model 空白/错误类型等无效 Agent TOML 返回 configuration invalid；用失败哨兵禁止 subprocess/HTTP/远程模型发现。既有 LLM事件精确 assertions 不变。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/observability/test_agent_event_logging.py::test_agent_failure_logs_allowlisted_reason_without_payload -v
```

Expected: FAIL：共享日志尚未记录完整 Agent reason/model，或意外把不可信内容放入日志；doctor 既有本地校验用例可直接作为回归通过。

- [x] **Step 3: Describe the minimal implementation**

```text
Agent provider_started/completed/failed 在 shared pipeline 发出，
stage 仅 agent_search 或 agent_paper_search；使用 invocation.provider/model 和 monotonic elapsed；model=None 由现有 formatter 渲染 null，不新建默认名称或模型发现逻辑。
从 GatewayError.reason 的固定 allowlist 取失败分类；ParserFailure -> invalid_markdown；
未知异常/非白名单 reason -> execution_failure；取消记录固定 cancelled 后重抛。
不打印 exc/str(exc)/repr(exc)/args/traceback payload，也不转存 CLI metadata。
_log_provider_failure 的 Agent 新字段按需增加；旧 LLM/keyword event payload 不额外变化。
doctor 继续调用 resolve_config；不新增 agy availability/auth/network probe。
除非回归暴露实际问题，不修改 doctor.py。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/observability tests/doctor tests/unit/test_llm_stages.py tests/unit/test_llm_paper_stage.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

固定分类只维护一处 allowlist/映射；不要依赖动态字符串净化 raw stderr。把需要诊断的信息限制在已有安全元数据。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/observability tests/doctor tests/unit/test_llm_stages.py tests/unit/test_llm_paper_stage.py -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

### Task 23: 补齐公共文档、示例配置与最终质量门

**依赖：** Tasks 1-22。

**Files:**

- Modify: `README.md`
- Modify: `config.example.toml`
- Modify: `docs/public-interface.md`
- Modify: `CHANGELOG.md`（Unreleased）
- Modify/Test: `tests/docs/test_documented_config.py`
- Test: `tests/docs/test_installation_contract.py`, `tests/docs/test_open_source_contract.py`
- Run unchanged: `scripts/build_docs.py`
- Docs: 三份 `20261022-agent-search-antigravity.md` spec

- [x] **Step 1: Write the failing test**

首个用例 `test_agent_example_config_matches_documented_command`：load_toml(config.example.toml) 经真实 resolve_config + stub LLM secrets，断言合法的 antigravity_main 配置和 model=None 的 Agent invocation；再为同一 entry 填入显式 model 验证 pin 路径。README 默认示例不带 --model，pin 示例须说明先配置同名 model，再通过 build_parser 产生对应 request 参数；不运行 agy 或解析实时模型列表。
再验证公共 policy 与 Unreleased 覆盖 agent-search、scope/provider/model、agent-*、agent: provenance，以及 fetch mandatory/search optional。保留原命令/配置/LLM示例 assertions，不用宽松 substring 替代原明确命令集合。

- [x] **Step 2: Run test to verify it fails**

Run:

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/docs/test_documented_config.py::test_agent_example_config_matches_documented_command -v
```

Expected: FAIL：当前 config.example/public documentation 未包含完整 Agent 契约。

- [x] **Step 3: Describe the minimal implementation**

```text
README/示例配置写清：
    agent_providers alias 与 harness="antigravity" 的区别；独立 search_agent.providers；
    command/max_concurrency/timeout_seconds/pass_env 类型、默认值和限制；
    fetch_llm 四阶段仍必需，search_llm/search_agent 可独立为空；
    agy 由用户安装/登录/配置默认模型；search_agent 的 model 可省略，不从 LLM 继承；
    需要固定模型时才配置非空 model，并由用户从安装版本的 agy models 选择，示例 slug 不是可用性保证；
    Gateway 的 --model 只筛选显式配置项，不覆盖缺省 entry；默认/显式两路径均传 JSON 输出和显式 timeout；
    Web/Paper/All 命令、精确 selectors、无匹配不 fallback、重复 entry 独立执行；
    agent 文件名与 schema、source agent:、mixed顺序、空成功/部分成功；
    一次调用新会话、all两次调用可能排队、三层deadline、输出上限、无整Agent retry；
    exit 0/soft-denial 不证明工具执行；无源访问保证；
    不自动批准权限、不管理 Harness tools/MCP；
    argv -p 的本地可见性、临时 cwd 和 env allowlist 的局限（不是 sandbox）。
在 config.example 给出省略 model 的合法可解析 Agent 示例，把可选 model pin 写成注释；不要为演示两种模式默认添加第二个 invocation 而意外执行两次。注释指出未配置/清空 search_agent 可关闭 Agent 搜索。
public-interface 将新 CLI/TOML/JSONL 纳入现有公共合约，内部 Python/socket 仍非稳定 SDK。
CHANGELOG 的 Unreleased/Added 记录功能；不变更版本号。
保持自动测试离线；真实 smoke 仅在后续已有明确运行授权时执行。
```

- [x] **Step 4: Run test to verify it passes (and full suite)**

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/docs -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
```

Expected: 目标行为及全部普通测试通过；没有真实 provider/Agent 调用。

- [x] **Step 5: Refactor (keep tests green)**

整理文档重复解释，核对每条示例确实被 CLI/parser/config 接受。执行下方最终质量门；按构建流程生成 site/，不手改生成文件，不提交运行时/cache/认证文件。

如进行了上述重构，执行：

```bash
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/docs -q
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run python scripts/build_docs.py
```

Expected: 对应回归与质量检查全部通过；既有行为断言保持。

## 最终验收与真实 smoke

完成全部实现任务后，质量门使用现有 CI 对应命令，记录实际输出；不要把本计划中的 Expected 当成已通过证据：

```bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
uv run python scripts/build_docs.py
```

期望：格式、lint、类型检查、普通测试和文档构建均成功；原 opt-in live tests 仍跳过；新增 Agent 测试不需要真实 agy、网络或凭据。原 871 个通过用例是基线参照，新增后数量应增长，但不硬编码新的总数。严格 mypy 必须覆盖新增 typed callables、Agent request union 和全部 daemon 测试替身。

仅在后续实施阶段已有真实运行授权、且操作者已安装并认证 agy 时进行下列 smoke。本次计划编写不运行它们：

1. 用安装版本的 `agy --help` 确认可用参数，并确认用户已在 Antigravity 保存默认模型；不改 Harness 权限或 MCP/tool 配置。只有测试显式 pin 时才需用 `agy models` 确认实际 slug。
2. 在有效 mandatory fetch_llm 配置中，加入 spec 对应 Agent alias，先省略 search_agent entry 的 model。
3. 在单独终端运行 `agent-search-gateway start --debug`；另一终端分别运行：

```bash
agent-search-gateway agent-search "Find official documentation on Python asyncio cancellation" --provider antigravity_main --scope web
agent-search-gateway agent-search "Find academic papers on retrieval augmented generation" --provider antigravity_main --scope paper
agent-search-gateway agent-search "Find papers and reference sites for agent tool safety" --provider antigravity_main --scope all
```

上述三条先验证默认模型路径。再把同一 entry 的 model 显式设为已核实的 slug，按现有流程重启 daemon 使新配置生效，单独执行 `agent-search-gateway agent-search "Find official documentation on Python asyncio cancellation" --provider antigravity_main --model "<actual-model-slug>" --scope web`；替换 placeholder，且 selector 必须与配置值一致。不得靠该 flag 为未配置 model 的 entry 临时注入模型。这些是待执行示例，不是本次已验证的账户/模型事实。

只检查新返回的结果文件；按当前环境约束可通过 bash 读取 `~/.cache/agent-search-gateway-cli/` 下这些明确路径。确认 Web 两键、Paper agent:* 来源、mixed type/顺序；任选 admitted landing URL 验证原 url-fetch。以 `stop` 和一个有界取消场景检查无存活 Agent 进程，不输出认证值或原始 transcript。记录 soft-denial 时只说明终态/格式结果，不能据 SUCCESS 推断工具或源访问真实发生。

## Spec 覆盖映射

| 已批准要求 | 对应任务 / 验收 |
|---|---|
| Architecture §1：仅 Antigravity、最小改动、既有 fetch 必需 | 1-2、5-10、17、23 |
| Architecture §2-3：一个 SearchOrchestrator，共享文本搜索，保留 LLMStages | 10-16；旧 LLM scope/selector/bytes 回归 |
| Agent/LLM 独立配置，缺省可空，同名 alias 无冲突 | 1-2、12、17 |
| Agent model 可省略，None 贯穿类型边界，省略 --model；显式 pin 精确传递 | 1-2、6、10、17、21-23 |
| 模型筛选排除默认 entry，provider-only/无 selector 保留；不探测/覆盖 Harness 默认 | 12、14-15、17、21-23 |
| 两种模型路径均保留 JSON 输出及显式 CLI timeout，日志默认 model=null | 6、8、22-23 |
| 单请求 selectors 精确匹配、错误优先级、无隐式 fallback | 12、14-15、18-21 |
| 保留重复 invocation 与配置顺序 | 1、11-12、14-15、17、21 |
| 六个既有 LLMStages 方法及 llm_stage_* 保持 | 10-12、14-15、22 |
| Provider 家族目录与 OpenAI 原 HTTP 行为 | 10 的 Refactor；原 OpenAI 全部测试 |
| 完整 messages 与唯一 -p argv，无 shell/session resume/自动批准 | 6-7、10 |
| 新 cwd、受控环境、auth cache、显式 pass_env | 2、6-8、22-23 |
| exit code / status / response 联合判断，soft-denial | 5-6、11、22-23；仅文本/语法，不承诺工具证据 |
| 缺 executable、权限/认证/模型失败 | 5、8、17、22；不静默切换 provider |
| 严格 Web/Paper parser，无 regex/fallback JSON grammar | 5、11、13-15 |
| Agent 论文来源与 generated rank，LLM 旧优先级 | 13-14、17 |
| DOI/arXiv/native identity、OA enrichment 与 landing admission | 13-14、21 |
| All 双分支并发、quota=1 排队、独立会话 | 4、6、15、17 |
| 成功空 list、单 provider/单分支幸存、双失败无文件 | 11、14-16、21 |
| 无整 Agent retry；重复 entries 是独立调用 | 8、12、15、17 |
| 进程硬限时、workflow deadline、socket timeout、旧请求300秒 | 8、16、18 |
| 子/后代进程清理、排队取消、daemon10秒grace链 | 8-9、16、20-21 |
| stdout/stderr byte上限、并行排空、不接受截断成功 | 6、9 |
| 固定分类、无 prompt/response/stderr/env/异常原文日志 | 5、7-9、22 |
| Agent filenames，四种 ID collision，exclusive creation/回滚 | 3、20-21 |
| CLI path-only stdout、typed socket、legacy bytes | 18-21 |
| Runtime.aclose 原 HTTP 关闭行为，active process 由 task 管 | 8、17、20 |
| doctor 只本地 resolve_config，无 Agent 探测/登录/模型发现 | 2、17、22 |
| README/config/policy/changelog 与离线 CI | 19、23、最终质量门 |
| 不改现有 URL fetch 安全/清洗/不可用逻辑 | 10-11、21；原 keyword/fetch/scheduler 回归 |
| Error spec §2 全矩阵 / §6 非回归 | 2-9、11-16、18-22 |
| Testing spec §3-9 所有层次与 acceptance gates | 1-23；最终质量门/单独 opt-in smoke |

## 类型与实现边界自查

| 接缝 | 唯一约定 |
|---|---|
| config → runtime → stages → runner | AgentInvocation(provider, model: str \| None = None)；AgentProviderConfig.timeout_seconds；AgentTextRunner(model: str \| None, messages) -> Awaitable[str]；None 原样传递 |
| runtime → runner 配置绑定 | command/timeout_seconds/pass_env/quota 为 keyword-only；alias gate 在绑定时确定 |
| request → codec → CLI → daemon → orchestrator | AgentSearchRequest(prompt, scope, provider, model)；wire type agent_search；method agent_search(..., request_id=...) |
| old LLM scope | LLMSearchScope 保留兼容 alias；LLMInvocation 的 model 仍为必需 str，extra_body 不丢失 |
| shared helper | typed tuple + 对应同类型 generator；不用 Agent/LLM union 的强转代替类型配对 |
| parser → aggregator | source_kind="agent" 显式传递；默认 llm；完整 agent:<alias> 参与 sources/citation_counts |
| writer → request registry | ResultKind 四种；PaperResultKind 三种；mixed 的 TextSearchKind 默认 llm |
| error → provider log → daemon | 唯一新增 error code 为 NO_AGENT_SEARCH_PROVIDERS；分类复用 reason，经过 allowlist；外部取消重抛 |
| lifecycle → quota | 先 acquire，再 spawn；清理/收束/删除 cwd 完成后 release；排队取消不能 spawn |
| time budgets | 300（max540）+5 每进程；2秒TERM grace；1140 workflow；1200 Agent socket；旧 socket300 |

### 完成定义

实现交付必须满足：所有任务复选框都有对应实际验证；三份 spec 的上述映射无遗漏；旧命令/错误/JSONL 字节不回归；新的 Agent Web/Paper/All 可经过真实本地 socket 离线验收；异常和 shutdown 后没有存活 subprocess；普通测试不依赖真实 Agent 账户；公共文档与配置可实际解析。

### 计划自查结果（初版 2026-10-22；修订 2026-10-23）

已逐节对照 Architecture §1-7、Error handling §1-7、Testing §1-9，覆盖映射中没有待补的实现要求。已核对文件路径、任务依赖、类型名、callable 参数、source_kind 默认值、writer kind 和三层 timeout。

自查修正：把 SearchScope/LLMSearchScope alias 的定义提前到 Task 1；明确 fetch 必需不改变既有配置继承；固定 command 单 argv 校验及终态 status/response 顺序；补齐直接调用输入优先级和整 Agent 不重试测试。接口目录使用伪代码签名标记，避免被误当成可直接执行的完整 Python。

2026-10-23 修订自查：同步三份 spec 与 Tasks 1-2、6-7、10、12、17、19、21-23；补齐省略/显式非法 model 的区别、optional callable 类型、条件 argv、默认/显式模型端到端验收、model=null 日志及默认 smoke 示例。Gateway 的 --model 筛选语义、JSON envelope 和三层 timeout 保持不变。

计划修订时，23 个实现任务及其 115 个执行步骤尚未执行。后续实施已在本工作树完成并逐项勾选；实际质量门和真实环境限制见上方“实施与验收记录”，不以历史 Expected 代替实测。
