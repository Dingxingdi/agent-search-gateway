# Request-Scoped Provider and Model Filtering Implementation Plan

**Goal:** 为三个 search 命令添加请求级 `--provider`，为 `llm-search` 添加可独立使用的 `--model`，在执行前筛选已装配的 provider/invocation，并保持无筛选请求的既有行为。

**Architecture:** `docs/designs/architectures/20260923-request-provider-model-filtering.md`

**Error handling:** `docs/designs/error-handlings/20260923-request-provider-model-filtering.md`

**Testing:** `docs/designs/testings/20260923-request-provider-model-filtering.md`

---

## 执行上下文

工作树：`/home/agent/agent-search-gateway/.worktrees/request-provider-model-filtering`。分支：`request-provider-model-filtering`。下文所有仓库相对路径、行号和命令均以此工作树为基准；行号为编写计划时的位置，实施时优先按符号定位。

本计划只安排实现，不包含已经执行的实现或测试结果。三个现有 spec 是输入，不修改其设计内容；保留工作树中的其他既有改动。不新建工作树，不在主工作区实施，不创建 `.ai-bridge` 交接文件。已确认的 spec 与本计划按对应技能要求提交。

### 计划编写检查单

- [x] 阅读 architecture、error-handling、testing 三份 spec。
- [x] 核对 CLI、模型、协议、daemon、orchestrator、runtime 和测试/文档结构。
- [x] 拆分有依赖顺序的测试驱动任务。
- [x] 写出文件路径、测试场景、失败预期、最小实现和验证命令。
- [x] 对照 spec 检查覆盖范围与类型/签名一致性；见文末自查表。

## 已确认的代码路径与改动边界

当前数据流：`build_parser()` → `_request_from_args()` → frozen request dataclass → `encode_request()` / `decode_request_frame()` → `ForegroundDaemon._invoke_workflow()` → orchestrator → provider/stages → 原有聚合和 ResultWriter。

| 文件 / 当前定位 | 本次职责 |
|---|---|
| `src/agent_search_gateway/models.py:91-108` | 三种 search request 增加可选 selector，保留字段顺序、默认值和不可变性 |
| `src/agent_search_gateway/protocol.py:40-70,160-172` | search 帧增加严格类型的可选字段，保留旧帧及默认 scope 的编码行为 |
| `src/agent_search_gateway/cli.py:47-93` | 只在对应子命令注册 flag；trim、拒绝显式空值并构造 request |
| `src/agent_search_gateway/orchestrators/search.py:67-242` | keyword 请求局部筛选；LLM invocation 选择及两个 scope 分支的显式透传 |
| `src/agent_search_gateway/orchestrators/paper.py:61-105` | academic provider 请求局部筛选，后续 finalization 不变 |
| `src/agent_search_gateway/daemon.py:43-56,405-428` | 更新结构化类型协议和调用参数，不保存 selector 状态 |
| `README.md:104-147,197-235` | 用法、命名空间、错误边界和 DEBUG smoke 说明 |
| `docs/public-interface.md:5-16` | 明确 flags / 匹配语义属于公共 CLI 合约 |
| `CHANGELOG.md:9-22` | 在 Unreleased 新增 Added 条目，不改版本号 |

生产代码只需修改上述六个 `.py` 文件。不新增通用 selector 服务，不修改 `config.py`、`runtime.py`、provider adapter、`llm/stages.py`、`orchestrators/fetch.py`、scheduler、结果 schema 或配置 schema。

测试优先复用 `tests/support/fakes.py` 中的 `FakeKeywordSearchProvider`、`FakeAcademicSearchProvider`、`FakeOAResolver`、`FakeLLMClient`，以及真实 `LLMStages`、`ProviderQuotaManager`、`URLStore`、`ResultWriter`。复杂筛选用例放在新的聚焦测试文件，不继续扩大现有 pipeline 大测试。

`docs/site/index.html` 当前是文档链接入口，不含需要同步的 search 命令示例；`scripts/build_docs.py` 生成 `site/`，不要手工修改生成文件。

## 实施时统一遵守的合约

### 数据与类型

请求字段顺序固定为：

```text
KeywordSearchRequest(query: str, provider: str | None = None)
PaperSearchRequest(query: str, provider: str | None = None)
LLMSearchRequest(prompt: str, scope: LLMSearchScope = "web",
                 provider: str | None = None, model: str | None = None)
```

保留 `LLMSearchRequest("prompt", "paper")` 的位置参数含义。三个类仍为 `@dataclass(frozen=True, slots=True)`，不在 dataclass 中查询配置、执行选择或做网络操作。

实现与 daemon Protocol 的最终方法签名必须一致：

```python
async def keyword_search(self, query: str, *, request_id: str,
                         provider: str | None = None) -> str: ...
async def paper_search(self, query: str, *, request_id: str,
                       provider: str | None = None) -> str: ...
async def llm_search(self, prompt: str, *, request_id: str,
                     scope: LLMSearchScope = "web",
                     provider: str | None = None,
                     model: str | None = None) -> str: ...

def _select_llm_invocations(self, *, provider: str | None,
                            model: str | None) -> tuple[LLMInvocation, ...]: ...
async def _llm_web_records(self, prompt: str, *,
                           invocations: tuple[LLMInvocation, ...]) -> list[SearchRecord]: ...
async def _llm_paper_records(self, prompt: str, *,
                             invocations: tuple[LLMInvocation, ...]) -> list[PaperRecord]: ...
```

两个 records helper 的 `invocations` 是必传参数：不提供“省略时退回完整集合”的默认值。

### 输入、选择与错误

CLI trim selector，但不做大小写转换、CSV 拆分、通配或别名猜测。未传为 `None`；显式 `""` / 全空白是输入错误，不能变成无筛选。

协议只负责字段/类型校验：缺省 selector 解码为 `None`，编码时省略；显式 `null` 不接受。空字符串仍是字符串，不能在 codec 中吞掉，交给业务入口防御性拒绝。对于绕过 CLI 的非空 selector，服务端按收到的字符串精确匹配，不额外 trim；全空白值单独拒绝。

服务端保留现有 request ID、query/prompt、scope 校验顺序；原始 provider/invocation 集合为空时，先返回对应 `NO_*_PROVIDERS`，再谈筛选。集合非空时检查 selector 空值和匹配。CLI 显式空值在发 socket 前已经失败；不要让它落入服务端空 runtime 的判定。

| 条件 | 错误类型 / 消息 |
|---|---|
| 显式空 provider | `InputFailure(BAD_REQUEST, "Provider must not be empty")` |
| 显式空 model | `InputFailure(BAD_REQUEST, "Model must not be empty")` |
| keyword 不匹配 | `No enabled keyword-search provider matches '<provider>'` |
| paper 不匹配 | `No enabled paper-search provider matches '<provider>'` |
| LLM provider 不存在 | `No LLM search invocation matches provider 'P'` |
| LLM model 不存在 | `No LLM search invocation matches model 'M'` |
| LLM 两者均不存在 | `No LLM search invocation matches provider 'P' or model 'M'` |
| LLM 两者各自存在但组合不存在 | `No LLM search invocation matches provider 'P' with model 'M'` |

上述匹配错误均为 `InputFailure(ErrorCode.BAD_REQUEST, ...)`。原始集合为空仍是 `ExecutionFailure(NO_*_PROVIDERS, ...)`；合法选择之后全部执行失败仍是原有 `ALL_PROVIDERS_FAILED`。不新增错误码。

LLM 存在性只检查 `_llm_invocations`，即已解析的 search invocations。不能使用 `llm_clients`、全部 `llm_providers`、`protocol="openai"` 或 fetch-stage 配置来判断 selector 有效性。

### 副作用与兼容性

先选局部 tuple，再创建/调度 provider coroutine。绝不临时覆盖实例上的 provider/invocation 集合，也不先全部请求再筛输出。保留原对象、配置顺序、重复项、`extra_body`、quota/client 关联及现有失败隔离。

筛选失败时没有 provider 调用、quota 执行路径、结果文件、URL admission 或 OA enrichment。合法搜索之后，keyword 的 judge 等 `fetch_llm` 后处理，以及 paper 的正常 OA enrichment 仍可能执行；flags 不是“禁用所有其他网络阶段”的开关。

控制参数不追加到 query、prompt、HTTP body 或认证字段；被选中 invocation 自身已有的 `model` 和 `extra_body` 仍按原调用路径发送。成功 stdout 仍只有结果路径；失败走原 stderr/exit code。

## TDD 执行规则与环境

从指定工作树开始：

```bash
cd /home/agent/agent-search-gateway/.worktrees/request-provider-model-filtering
uv sync --locked --all-groups
uv run pytest --ignore=tests/integration -q
```

环境安装与本计划执行不是同一件事：以上命令尚未运行。依赖安装可能需要包源连接；正常测试仅使用本地 socket、fake 和 MockTransport。实现过程中显式排除 `tests/integration`，避免继承到 live opt-in 环境变量时误发请求。

每个 Task 的场景按“小循环”完成：一次新增一个行为测试 → 运行确认因缺少该行为而失败 → 最小实现 → 通过 → 再加入下一行场景。不要一次写完全部实现后补测。已存在行为的兼容性断言可能一开始就通过，不人为制造失败，也不把它们冒充 RED 证据。接口尚不存在导致的 pytest 用例失败可定位到缺失字段/参数；语法错误、导入错误、依赖缺失和零用例收集都不是有效 RED。

测试使用合法且不同的 request ID，例如 `11111111`、`22222222`、`33333333`；不能用任意文字代替八位十六进制 ID。并发测试用 Event/受控 fake 和有界 timeout，不用 sleep 猜时序。不要把 `LLMInvocation` 放入 set：它含 Mapping，测试使用列表、对象身份或 `(provider, model)` 计数比较。

---

### Task 1: 扩展不可变 search request 模型

**Files:**
- Modify: `src/agent_search_gateway/models.py:91-108`
- Create / Test: `tests/unit/test_search_request_selectors.py`
- Reference: `tests/unit/test_protocol_codec.py:20-77`

- [ ] **Step 1: Write the failing test**

先写 `test_keyword_request_has_optional_provider`：用 `dataclasses.fields()` 断言新增字段存在，再断言 `KeywordSearchRequest("query", provider="exa")` 保存值。随后逐个补充 paper 和 LLM 的行为：

| 场景 | 设置 / 动作 | 断言 |
|---|---|---|
| keyword / paper 默认请求 | 只传 query | provider 为 None |
| LLM provider-only、model-only、both | 分别构造 request | 两字段独立，未传字段为 None |
| 旧 LLM 位置参数 | `LLMSearchRequest("prompt", "paper")` | scope 仍为 paper |
| 不可变性 | 对实例用 `setattr` 改 selector | `FrozenInstanceError` |
| 模型不隐式规范化 | 直接构造带空白的 selector | 原字符串保存，验证由边界负责 |

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_search_request_selectors.py::test_keyword_request_has_optional_provider -v
```

预期：FAIL，dataclass 缺少 provider 字段；不是测试导入错误。

- [ ] **Step 3: Describe the minimal implementation**

```text
在 KeywordSearchRequest.query 后增加 provider=None
在 PaperSearchRequest.query 后增加 provider=None
在 LLMSearchRequest.scope 后依次增加 provider=None、model=None
保持 frozen、slots、Request union 和其他 request/response 不变
```

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/unit/test_search_request_selectors.py tests/unit/test_protocol_codec.py -v
uv run pytest --ignore=tests/integration -q
```

预期：新模型测试通过，旧协议默认请求仍通过。

- [ ] **Step 5: Refactor (keep tests green)**

合并重复测试数据，不引入 request 基类或配置依赖。再次运行：

```bash
uv run pytest tests/unit/test_search_request_selectors.py tests/unit/test_protocol_codec.py -v
```

### Task 2: 为 NDJSON search 帧添加严格可选字段

**Files:**
- Modify: `src/agent_search_gateway/protocol.py:40-70,160-172`
- Modify / Test: `tests/unit/test_protocol_codec.py`
- Regression: `tests/unit/test_socket_client.py`

- [ ] **Step 1: Write the failing test**

先写 `test_keyword_provider_round_trips`，固定编码字节：

```python
assert encode_request(KeywordSearchRequest("hello", provider="tavily")) == (
    b'{"type":"keyword_search","query":"hello","provider":"tavily"}\n'
)
```

随后逐个增加：paper round trip；LLM 四种 selector 组合 × `web|paper|all`；无 selector 的旧帧；未传字段不编码为 null；默认 web scope 继续省略。

错误测试覆盖每个允许的 selector 字段的 `null`、整数、布尔、列表、对象，以及未知字段、缺少必填字段、keyword/paper 的 model、fetch/shutdown 的 selector。预期统一为 `ErrorResponse(BAD_REQUEST, "Request fields do not match schema")`。

再锁定空字符串不会在 encode/decode 时消失：它必须到达业务校验，不能退回全量执行。扩展一个 NDJSON 分包/多帧用例，让携带 selectors 的帧也经过现有 buffering 路径。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_protocol_codec.py::test_keyword_provider_round_trips -v
```

预期：FAIL，编码缺少 provider；随后新增的有效 selector 解码测试会暴露旧 exact-key 拒绝。

- [ ] **Step 3: Describe the minimal implementation**

```text
keyword/paper: required={type,query}; optional={provider}
LLM: required={type,prompt}; optional={scope,provider,model}
验证 required <= keys <= required ∪ optional
selector key 缺省 -> None；存在且不是 str -> ValueError
保留 scope 的显式合法分支和 Literal 类型收窄
创建请求时用 provider=、model= 关键字传值
编码只在对应值 is not None 时添加 key
fetch、shutdown、response codec 和 NDJSON framing 保持原样
```

可在 protocol.py 内抽一个只读取可选字符串的小私有函数，不能放宽其他 request 的 schema。禁止用 `if value` 判断是否编码。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/unit/test_protocol_codec.py tests/unit/test_socket_client.py -v
uv run pytest --ignore=tests/integration -q
```

预期：新增合法帧通过，非法帧仍拒绝，旧帧的字节断言不变。

- [ ] **Step 5: Refactor (keep tests green)**

只消除重复的 optional-string 读取；不重写协议框架。再次运行：

```bash
uv run pytest tests/unit/test_protocol_codec.py tests/unit/test_socket_client.py -v
```

### Task 3: CLI flags、trim 与客户端提前拒绝

**Files:**
- Modify: `src/agent_search_gateway/cli.py:47-93`
- Modify / Test: `tests/cli/test_cli.py`

- [ ] **Step 1: Write the failing test**

先写 `test_keyword_cli_forwards_trimmed_provider`：真实 parser + `run_command()`，仅替换 socket client 为记录 request 的 async fake；输入 `--provider "  exa  "`，断言 request.provider 为 `exa`，query 不夹带 selector。

依次补充：paper provider；LLM provider-only/model-only/both × scope；selector 为 None 的回归；大小写原样保存；`""`、空格和 tab-only 空值。

空值断言 `code == 1`、stdout 为空、stderr 是精确的非空验证消息加换行，且 client 调用列表为空。保留空 query/prompt 原错误优先级。

参数化所有不支持的位置：`url-fetch`、`start`、`stop`、`doctor` 均不接受 provider/model；keyword/paper 不接受 model。argparse 应 `SystemExit(2)`。不新增全局 flags、多值解析或动态 provider choices。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/cli/test_cli.py::test_keyword_cli_forwards_trimmed_provider -v
```

预期：FAIL，当前 parser 不识别该合法新 flag。加入支持后再单独观察空值行为的 RED，不把 parser 失败当作空值校验已测到。

- [ ] **Step 3: Describe the minimal implementation**

```text
keyword、paper、llm 子 parser 各注册 --provider，默认 None
只在 llm 子 parser 注册 --model，默认 None
小型 CLI 私有规范化函数：None 原样返回；trim 后空则抛对应 InputFailure
在现有 query/prompt 校验之后规范化 selectors
按请求类型构造 dataclass；LLM 保留 args.scope
使用现有 run_command 的 GatewayError -> stderr/EXIT_ERROR 路径
```

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/cli/test_cli.py tests/unit/test_search_request_selectors.py -v
uv run pytest --ignore=tests/integration -q
```

预期：支持的 flags 正常构造 request；空值不连接 daemon；不支持的位置仍是 argparse 错误。

- [ ] **Step 5: Refactor (keep tests green)**

仅在 CLI 内共用 trim/blank 逻辑；不将 selector 拼进 query/prompt，不改变 stdout 渲染。再次运行：

```bash
uv run pytest tests/cli/test_cli.py -v
```

### Task 4: Keyword provider 的执行前筛选

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/search.py:67-84`
- Create / Test: `tests/orchestrators/test_keyword_provider_filtering.py`
- Modify / Test: `tests/runtime/test_runtime_assembly.py`
- Reference: `tests/support/fakes.py:16-32`
- Regression: `tests/orchestrators/test_keyword_search_pipeline.py`, `tests/orchestrators/test_keyword_search_state.py`

- [ ] **Step 1: Write the failing test**

先写 `test_keyword_filter_calls_only_selected_provider`：装配 tavily/exa/brave 三个 fake，各返回不同 URL，调用 provider=exa；只 exa 的 calls 有原 query，JSONL 及 URLStore 仅出现它的结果。

随后逐个覆盖：None 全量；未知名、大小写不符、显式空白；空 runtime 的原 `NO_KEYWORD_SEARCH_PROVIDERS`；选中的 provider 全失败而未选中的健康 provider 不得补跑；成功空结果仍写空文件；筛选后再无筛选仍调用全部。

负例同时检查结果目录快照不变、provider calls 不变、未 admission URL。复用 runtime 测试的 `_config()` / `_environment()`，用 MockTransport 验证 fetch-only 的 tinyfish 和 disabled 的 firecrawl 不能被 keyword selector 选中。

加一项真实 stages 回归：选中 keyword provider 的有效正文仍进入已配置的 judge，即使 judge 使用另一 LLM alias。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/orchestrators/test_keyword_provider_filtering.py::test_keyword_filter_calls_only_selected_provider -v
```

预期：FAIL，入口尚不接受 provider；加上参数后，错误的全量 fan-out 会被其他 provider 的调用断言捕获。

- [ ] **Step 3: Describe the minimal implementation**

```text
给 keyword_search 添加 keyword-only provider=None
保持 request_id/query/原始空集合检查
拒绝显式空 selector
selected = 原始 tuple，或按 candidate.name == provider 保序筛选的新 tuple
selected 为空 -> 精确 keyword BAD_REQUEST；此时还未创建 provider 工作
仅把 gather 的迭代集合改为 selected
后续 pipeline、quota、聚合、store、writer、日志和失败判断不改
```

使用 `candidate` 等局部名区分 selector 字符串与 provider 实例。不修改 `_keyword_providers`。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py tests/runtime/test_runtime_assembly.py -v
uv run pytest --ignore=tests/integration -q
```

预期：只有选中的 discovery pipeline 执行；fetch-only/disabled 的拒绝不触发 HTTP。

- [ ] **Step 5: Refactor (keep tests green)**

保持过滤靠近 fan-out；不引入跨领域 selector 抽象。再次运行：

```bash
uv run pytest tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py -v
```

### Task 5: Paper provider 的执行前筛选

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/paper.py:61-75`
- Create / Test: `tests/orchestrators/test_paper_provider_filtering.py`
- Reference: `tests/support/fakes.py:35-70`
- Regression: `tests/orchestrators/test_paper_search_pipeline.py`, `tests/academic/test_oa_enrichment.py`

- [ ] **Step 1: Write the failing test**

先写 `test_paper_filter_calls_only_selected_discovery_provider`：arxiv/openalex/crossref 返回可区分的 PaperSearchHit，provider=openalex 只调用该 fake，并使用原 PaperAggregator / ResultWriter 生成结果。

逐个增加 None、未知、大小写、空白、空 runtime、有效选择后全失败、成功空结果、下一请求无筛选等场景。

OA 专项：无效 selector 不调用 resolver，不写文件、不 admission；有效选择后 selected DOI 仍正常 enrichment；`unpaywall` 即使被配置为 resolver，也不是可选 discovery provider。确认未选 provider 的命中不会进入 paper sources。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/orchestrators/test_paper_provider_filtering.py::test_paper_filter_calls_only_selected_discovery_provider -v
```

预期：FAIL，paper_search 缺少 provider 参数；之后用调用列表验证不是过滤输出。

- [ ] **Step 3: Describe the minimal implementation**

```text
paper_search 增加 keyword-only provider=None
保持 request_id/query/原始 providers 为空的既有错误
验证空白与精确 provider.name 匹配
用请求局部 selected 替代 gather 中的 self.providers
不修改 self.providers、aggregator、resolver 或 finalize_paper_hits
```

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/orchestrators/test_paper_provider_filtering.py tests/orchestrators/test_paper_search_pipeline.py tests/academic/test_oa_enrichment.py -v
uv run pytest --ignore=tests/integration -q
```

预期：只过滤 discovery；正常 OA、错误分类及 paper schema 不变。

- [ ] **Step 5: Refactor (keep tests green)**

保留独立 academic 类型及现有 finalization，不合并 keyword/paper 实现。再次运行：

```bash
uv run pytest tests/orchestrators/test_paper_provider_filtering.py tests/orchestrators/test_paper_search_pipeline.py -v
```

### Task 6: LLM invocation 选择函数与完整诊断矩阵

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/search.py`，在 llm_search 附近新增 `_select_llm_invocations`
- Create / Test: `tests/orchestrators/test_llm_invocation_selection.py`
- Reference: `src/agent_search_gateway/models.py:70-74`

- [ ] **Step 1: Write the failing test**

先写 `test_llm_provider_selection_keeps_all_models`。构造真实 orchestrator，配置以下原始 invocation 对象，直接测试小型选择函数；本 Task 不执行网络/流水线，Task 7 再验证公共入口和副作用。

```text
A1 = LLMInvocation("openai_main", "gpt-5")
A2 = LLMInvocation("openai_main", "gpt-5-mini")
B1 = LLMInvocation("deepseek_main", "deepseek-v3")
C1 = LLMInvocation("azure_main", "gpt-5")
```

| provider | model | 期望选择，保持原顺序 |
|---|---|---|
| None | None | A1, A2, B1, C1 |
| openai_main | None | A1, A2 |
| None | gpt-5 | A1, C1 |
| openai_main | gpt-5 | A1 |

增加重复 pair：同一 pair 的两个 invocation 都保留；用 `is` 检查原对象以及各自 `extra_body` 未被重新构造或修改。独立验证大小写和非空但带前后空白的 socket 原值不做模糊匹配。

逐项新增六个错误测试，每项同时核对 code 和完整消息：provider-only missing、model-only missing、both missing、provider missing/model exists、provider exists/model missing、两者分别存在但 pair missing。另测空值、原始 tuple 为空及多个匹配不报歧义。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/orchestrators/test_llm_invocation_selection.py::test_llm_provider_selection_keeps_all_models -v
```

预期：FAIL，选择方法尚不存在；后续诊断测试应分别观察缺少对应判断的 RED。

- [ ] **Step 3: Describe the minimal implementation**

```text
invocations = self._llm_invocations
原始集合为空 -> 原 NO_LLM_SEARCH_PROVIDERS
拒绝显式空 provider/model
无 selector -> invocations
只有 provider -> 保序筛选 provider；为空报 provider-only 错误
只有 model -> 保序筛选 model；为空报 model-only 错误
两者都有：
    分别从完整 invocations 求 provider_matches 与 model_matches
    两者均空 -> both-missing
    仅 provider_matches 空 -> provider-missing
    仅 model_matches 空 -> model-missing
    再从 provider_matches 筛 model
    交集为空 -> pair-missing
返回原 invocation 对象组成的 tuple
```

不要先限定 provider 后才判 model 是否全局存在，否则会把“pair 不存在”误报为“model 不存在”。不去重，不查 transport/client/fetch-stage 名称。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/orchestrators/test_llm_invocation_selection.py tests/orchestrators/test_llm_search.py -v
uv run pytest --ignore=tests/integration -q
```

预期：选择矩阵和错误矩阵通过；旧入口尚未接入新选择逻辑，其已有行为不变。

- [ ] **Step 5: Refactor (keep tests green)**

只收敛局部选择和诊断重复；保留清晰的四个双参数错误分支。再次运行：

```bash
uv run pytest tests/orchestrators/test_llm_invocation_selection.py -v
```

### Task 7: 将同一选择结果贯穿 LLM web / paper / all

**Files:**
- Modify: `src/agent_search_gateway/orchestrators/search.py:129-242`
- Create / Test: `tests/orchestrators/test_llm_provider_model_filtering.py`
- Modify / Test: `tests/runtime/test_runtime_assembly.py`
- Reference: `tests/orchestrators/test_scoped_llm_search.py:18-110`, `tests/support/fakes.py:92-130`
- Regression: `tests/orchestrators/test_llm_search.py`, `tests/orchestrators/test_scoped_llm_search.py`, `tests/observability/test_provider_event_logging.py`

- [ ] **Step 1: Write the failing test**

先写 `test_all_scope_uses_same_selected_invocations_in_both_branches`：使用 Task 6 的四项 invocation；局部 recording client 按现有 system grammar 返回合法 web/paper Markdown，记录 `(invocation, branch, messages)`；调用 provider=openai_main、model=gpt-5、scope=all。

断言 web 和 paper 各只执行 A1 一次。不要把整个 `_llm_web_records` / `_llm_paper_records` mock 掉，否则无法发现 helper 仍读取完整 tuple 的问题。

逐个扩展四种 selector 组合 × 三种 scope，以及重复 pair；验证真实 JSONL schema、顺序和原 invocation 对象。把 Task 6 六种 mismatch 通过公开 llm_search 再跑一遍：所有 client 调用、result 文件、URLStore admission、resolver 调用均为零变化。

另外逐项加入：

- 每种 scope 下，合法 selected 全失败仍为原 scope-specific `ALL_PROVIDERS_FAILED`；健康但未选 invocation 不能救场。
- selected 部分成功、成功空结果，以及 all 中只有一条分支成功的语义不变。
- filtered → unfiltered 连续请求；两个不同 selector 的并发请求；入口完成或取消后 `_llm_invocations` 保持原 tuple。
- 用 Event 阻塞选中的调用，取消外层请求 task，断言 `CancelledError` 传播且有界清理；不重新定义 provider 自发异常的旧语义。
- 传给 stage/client 的用户 prompt、原 invocation.model / extra_body 不变，没有新增 provider/model 控制字段；DEBUG 的 search-stage 事件只来自 selected invocation。

Runtime 命名空间测试直接扩展现有 `_config()`：primary 已有 client、用于 global/fetch 阶段，但 search 只有 secondary/search。primary、global model 和 transport 名 openai 均不能因此成为有效 search selector。用 MockTransport 记录 HTTP，验证匹配失败零请求；成功只到 secondary。搜索前后 `runtime.llm_clients`、web/fetch provider、quota 对象及数量不变，finally 关闭 runtime。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/orchestrators/test_llm_provider_model_filtering.py::test_all_scope_uses_same_selected_invocations_in_both_branches -v
```

预期：FAIL，llm_search 尚无 selector 参数；仅接入 web 的不完整实现也必须被此用例抓住。

- [ ] **Step 3: Describe the minimal implementation**

```text
llm_search 增加 provider=None、model=None
在 request_id/prompt/scope 校验后调用 _select_llm_invocations
把原入口的空 invocation 检查收敛到该 helper，保持错误顺序和消息
selected 在当前请求中只计算一次
web -> _llm_web_records(prompt, invocations=selected)
paper -> _llm_paper_records(prompt, invocations=selected)
all -> 同一个 selected 传入两个并发分支
两个 records helper 改成必传 tuple 参数，并只遍历参数
其余 stage、聚合、writer、失败隔离、日志和取消代码保持原样
```

尤其不修改默认 `_paper_aggregator` 的 provider 优先顺序，不给 request 字段创建新的 LLMInvocation，不修改 `Runtime.build()`。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/orchestrators/test_llm_invocation_selection.py tests/orchestrators/test_llm_provider_model_filtering.py tests/orchestrators/test_llm_search.py tests/orchestrators/test_scoped_llm_search.py tests/runtime/test_runtime_assembly.py tests/observability/test_provider_event_logging.py -v
uv run pytest --ignore=tests/integration -q
```

预期：三个 scope 共享选择规则；默认输出回归通过，取消与 runtime 装配语义未变化。

- [ ] **Step 5: Refactor (keep tests green)**

确认两个 fan-out helper 均无读取完整实例 tuple 的回退；不以强制 cast 或忽略 mypy 掩盖签名不一致。再次运行：

```bash
uv run pytest tests/orchestrators/test_llm_provider_model_filtering.py tests/orchestrators/test_scoped_llm_search.py tests/runtime/test_runtime_assembly.py -v
```

### Task 8: Daemon 透传、类型协议与真实 socket 验收

**Files:**
- Modify: `src/agent_search_gateway/daemon.py:43-56,405-428`
- Modify / Test: `tests/daemon/test_daemon_dispatch.py`
- Modify / Test: `tests/daemon/test_daemon_debug.py`（包括函数内的 fake 类）
- Modify / Test: `tests/daemon/test_daemon_request_ids.py`
- Modify / Test: `tests/daemon/test_daemon_shutdown.py`
- Modify: `tests/support/acceptance.py`
- Modify / Test: `tests/acceptance/test_gateway_workflows.py`

- [ ] **Step 1: Write the failing test**

先写 `test_daemon_forwards_keyword_provider`：更新 dispatch fake 使其显式接受并记录 provider，发送带 provider 的 typed request，断言收到 selector 和有效 request ID。当前 daemon 会丢 selector，形成真实 RED。

逐项补充 paper，以及 LLM provider-only/model-only/both × scope；有 selector 后无 selector 的下一请求必须收到 None。并发发不同 selector，按 query/request ID 核对每次调用，不依赖跨 task 的启动顺序。

在 `tests/support/acceptance.py` 新增独立 `FilteringAcceptanceRuntime` / `build_filtering_acceptance_runtime(paths)`，不改变旧 `AcceptanceRuntime` 的默认结果。该 fixture 使用真实三个 orchestrator、stages、store/writer，暴露多个 fake keyword/academic providers 和多个 search invocation 的调用记录；复用现有 FakeLLMClient 返回合法 web Markdown即可，不增加通用可配置运行框架。

在既有 acceptance 文件中复用 `_running_daemon()` 的有界启动/清理，通过 `build_parser()` → `run_command()` → 默认 socket client 验证：

| 场景 | 关键断言 |
|---|---|
| keyword 选 beta，再无 flag | 第一次只有 beta，第二次全部；stdout 每次仅绝对路径加换行 |
| paper 选一项 | 只有该 discovery provider，paper schema 不变 |
| LLM provider+model | 只调用匹配 invocation |
| LLM 两者各自存在但 pair 不存在 | exit 1，stdout 为空，stderr 为精确 pair 消息加换行；调用/文件/store 均不变 |
| socket 直接发送空白 selector | 业务输入错误，不退回全量 |
| 原始非法 schema 帧 | BAD_REQUEST；没有 orchestrator 调用 |

注意 CLI 当前打印的是 `response.message`，不自动打印 `BAD_REQUEST` 枚举名；不要让验收测试要求不存在的错误码前缀。保持原 unexpected-error 脱敏、DEBUG、request ID 和 shutdown 回归。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/daemon/test_daemon_dispatch.py::test_daemon_forwards_keyword_provider -v
```

预期：FAIL，fake 记录到 None 而非指定 provider。随后先观察 keyword socket acceptance 的未筛选调用失败，再实现透传。

- [ ] **Step 3: Describe the minimal implementation**

```text
_SearchOrchestratorLike、_PaperSearchOrchestratorLike 同步最终签名
_invoke_workflow：keyword/paper 显式传 provider=request.provider
LLM 显式传 scope、provider、model，包含 None
不增加 daemon 字段、不改 runtime 创建、锁、request ID 或错误转换
所有 daemon 测试 fake/局部子类显式增加同名可选参数，保留原返回/异常逻辑
```

已定位需要同步的 fake 位于四个 daemon 测试文件；`test_daemon_debug.py` 中除文件顶部 fake 外还有函数内定义。不要用 `**kwargs` 吞掉参数来绕过类型约束。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/daemon tests/acceptance -v
uv run mypy src tests scripts
uv run pytest --ignore=tests/integration -q
```

预期：真实 CLI/socket 链路筛选生效、无跨请求污染；类型检查无新增错误。

- [ ] **Step 5: Refactor (keep tests green)**

仅在新 acceptance fixture 中消除装配重复，不把已有单后端 fixture 扩成通用框架。再次运行：

```bash
uv run pytest tests/daemon tests/acceptance -v
uv run mypy src tests scripts
```

### Task 9: 公共文档、示例与 changelog 合约

**Files:**
- Modify / Docs: `README.md:104-147,197-235`
- Modify / Docs: `docs/public-interface.md:5-16`
- Modify / Docs: `CHANGELOG.md:9-22`
- Modify / Test: `tests/docs/test_documented_config.py`
- Reference: `scripts/build_docs.py`, `docs/site/index.html`

- [ ] **Step 1: Write the failing test**

新增 `test_readme_documents_request_scoped_provider_and_model_filters`。从 README 的新小节读取真实示例，用 `shlex.split()` 和真实 parser 检查，而不是只对测试里的硬编码命令自证。先要求以下五种单行示例存在，再逐项解析：

```bash
agent-search-gateway keyword-search "query" --provider tavily
agent-search-gateway paper-search "topic" --provider arxiv
agent-search-gateway llm-search "prompt" --provider openai_main
agent-search-gateway llm-search "prompt" --model gpt-5
agent-search-gateway llm-search "prompt" --provider openai_main --model gpt-5
```

补充 scope=paper/all 的可组合说明，以及公共接口与 Unreleased Added 中的新增 flags 记录。文档检查锁定关键语义，不用长段逐字匹配导致无意义的文案脆弱性。

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/docs/test_documented_config.py::test_readme_documents_request_scoped_provider_and_model_filters -v
```

预期：FAIL，当前 README 没有 selector 示例或说明；不是 parser 仍未支持（Task 3 已完成）。

- [ ] **Step 3: Describe the minimal implementation**

在 README 添加 request-scoped filtering 小节，遵循现有英文文档风格，并说明：

- keyword/paper 按启用的 search provider.name 匹配；LLM 按 search invocation 的配置 alias 匹配，不是协议名 openai。
- provider-only 可运行多个模型；model-only 可运行多个 alias；两者取交集；多个匹配和重复 invocation 都合法。
- 值大小写敏感，CLI trim；未传不筛选；显式空值/未匹配错误不会回退全量。给出双参数四种失败含义。
- 仅 search 命令适用，`url-fetch` 不接受这些 flags；`fetch_llm` 和正常 OA enrichment 不被筛掉。
- 请求局部生效，下一条命令不继承；runtime 仍完整装配，不绕过其他已启用 provider 的启动校验。
- 示例 alias/model 必须确实存在于本地 search 配置；示例字符串不是默认保证存在的服务。
- 安装包含本功能的新代码后，已有旧 daemon 需正常重启一次以加载新代码；之后切换 selector 不需重启。
- DEBUG 按请求 ID 和 search stage 验证选中后端，不能把合法 judge/OA 事件误判为过滤失败；成功 stdout 不加诊断文字。

在公共接口文档补充 flags/匹配行为，在 Unreleased 创建 Added 小节。不改 config.example.toml、版本号、历史 spec 或生成站点内容。

- [ ] **Step 4: Run test to verify it passes (and full suite)**

```bash
uv run pytest tests/docs tests/cli/test_cli.py -v
uv run python scripts/build_docs.py
uv run pytest --ignore=tests/integration -q
```

预期：文档测试通过；构建成功并生成 `site/index.html` 与 `site/api/agent_search_gateway.html`，不手改这些产物。

- [ ] **Step 5: Refactor (keep tests green)**

避免重复长篇规则，将详细语义集中在 README，接口政策只声明兼容性边界。再次运行：

```bash
uv run pytest tests/docs -v
uv run python scripts/build_docs.py
```

---

## 完成门槛：整体验证与变更审查

以下是实施完成后执行的 gate，不代表编写计划时已经通过。

- [ ] 运行与仓库现有开发说明一致的格式、lint、类型、离线全量测试和文档构建：

```bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run pytest --ignore=tests/integration -v
uv run python scripts/build_docs.py
```

预期全部 exit 0；不预填测试数量。需要格式修复时仅格式化此次修改的文件，再重跑上述检查。确认 `WEB_SEARCH_RUN_INTEGRATION` 未开启时，还可执行仓库标准 `uv run pytest -v`，live 用例应按现有规则跳过。

- [ ] 复核三个搜索入口：selector 失败均发生在 provider 工作之前；两个 LLM records helper 均使用传入 tuple。
- [ ] 复核无 flag 默认行为、重复 invocation、稳定结果顺序、成功空文件、部分成功、quota、DEBUG 脱敏、request ID、取消和 url-fetch 回归。
- [ ] 复核最终 diff：六个预期源码文件及相关测试/公共文档；没有 runtime/config/provider/fetch 实现漂移，没有 secrets、lockfile/版本号无关变化，没有修改用户 spec。
- [ ] 使用 CodexPro 的变更审查能力检查实施工作树；按对应技能要求提交 spec、plan 和后续实现，并显式限定每次提交包含的相关文件，避免把无关工作树改动一并提交。

## 可选 live smoke（非默认 CI）

仅在实施完成、离线 gate 通过、用户明确授权真实 provider 请求且凭据配置就绪后执行。此阶段不是计划编写任务的一部分。

在一个终端正常运行新版本 `agent-search-gateway start --debug`；已有 daemon 时先正常 stop，再 start。另一个终端针对实际存在的 alias/model 运行：

```bash
agent-search-gateway keyword-search "gateway smoke test" --provider tavily
agent-search-gateway paper-search "information retrieval" --provider arxiv
agent-search-gateway llm-search "Find information retrieval resources" --provider openai_main
agent-search-gateway llm-search "Find information retrieval resources" --model gpt-5
agent-search-gateway llm-search "Find information retrieval resources" --provider openai_main --model gpt-5
```

预期：只对应 search provider/invocation 被执行；合法的下游 judge/OA 阶段不受此筛选约束。stdout 仍仅路径；真实服务失败沿用原执行错误，不以排序质量或固定命中数作为 smoke 标准。最后运行一条不带 flag 的搜索，确认恢复完整 fan-out。

## Spec 覆盖与类型自查

| 要求 / 风险 | 对应任务 |
|---|---|
| frozen request、字段独立、旧位置参数 | Task 1 |
| optional omission、null/错误类型/额外字段、旧帧/default scope、NDJSON | Task 2 |
| flags 范围、trim、空值不发请求、默认 stdout/exit | Task 3 |
| keyword 精确选择、disabled/fetch-only 排除、NO/ALL 错误、judge 保留 | Task 4 |
| academic 精确选择、resolver 非 discovery、OA 副作用边界 | Task 5 |
| LLM 四种选择、六种诊断场景、重复/顺序/extra_body、原始空集合 | Task 6 |
| web/paper/all 同一子集、零调用/文件/admission、partial/empty/failure | Task 7 |
| alias 非 protocol、仅 search namespace、runtime 完整装配 | Tasks 4、7 |
| 请求隔离、并发、取消、日志、现有 quota/client 行为 | Tasks 4、7、8 与完成 gate |
| daemon 显式透传、Protocol/fake 签名、真实 socket、异常脱敏 | Task 8 |
| README、公共接口、changelog、配置/输出/schema 不变 | Task 9 与完成 gate |
| opt-in live smoke 与默认离线隔离 | 可选 live smoke 与完成 gate |

签名自查：所有公开 search 方法的 selector 都是 keyword-only `str | None = None`；LLM scope 始终为 `LLMSearchScope`；选择 helper 返回 `tuple[LLMInvocation, ...]`，两个 branch helper 都接收同一命名 `invocations` 参数；daemon 和所有 fake 使用相同参数名。没有把 dataclass 的 `model` 误命名为 `model_name`，没有把 provider alias 当作协议。

准备阶段发现并在计划中处理的遗漏风险：daemon 测试中局部 fake 的签名、默认 web scope 的字节兼容、显式 null 的策略、LLMInvocation 不可直接用于 set、CLI 错误文本不带枚举前缀、旧 daemon 加载新代码所需的一次重启、下游 judge/OA 不在筛选范围。需求覆盖检查未发现剩余未分配项；实施与运行验证仍待执行。
