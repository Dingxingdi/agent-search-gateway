# Retry-After, Zhihu, and Yuanbao Search Implementation Plan

**Goal:** 在现有共享 HTTP 重试路径遵循有效的 Retry-After，为关键词搜索新增知乎、元宝两个独立的 search-only provider，并按 spec 补充 Bright Data 已知限制说明。

**Architecture:** [docs/designs/architectures/20261116-zhihu-yuanbao-retry-after.md](../architectures/20261116-zhihu-yuanbao-retry-after.md)

**Error handling:** [docs/designs/error-handlings/20261116-zhihu-yuanbao-retry-after.md](../error-handlings/20261116-zhihu-yuanbao-retry-after.md)

**Testing:** [docs/designs/testings/20261116-zhihu-yuanbao-retry-after.md](../testings/20261116-zhihu-yuanbao-retry-after.md)

---

## 0. 执行上下文

- 日期：2026-11-16；工作树：`/home/agent/agent-search-gateway/.worktrees/zhihu-yuanbao-retry-after`。
- 分支：`design/20261116-zhihu-yuanbao-retry-after`；spec 基线：`8c83718`；生产代码基线：`eeeff82 / v0.2.2`。
- 本文件所有相对路径和命令 cwd 均指该工作树。继续使用该工作树，不创建第二个工作树。
- 本轮只编写、自审并提交本计划。下列复选框属于后续实施，不能将计划中的预期结果报告为本轮已执行的测试结果。
- 按 `writing-plans` 及其要求的 `test-driven-development`、`references/testing-anti-patterns.md` 组织工作。每次只选一个行为完成 RED → 核实 RED → GREEN → 核实 GREEN → REFACTOR，再进行下一个行为。
- 新 provider 的首个行为测试经现有 registry/factory 入口发起搜索，让“未知 provider、无法完成请求”成为初始失败；不以顶层导入不存在模块导致的 collection error 充当有效 RED。随后才直接导入已创建的 adapter 做其余单元测试。
- 已有能力的兼容性测试如果起初就通过，记录为回归保护；不人为破坏实现制造 RED。资料核对、文档维护、最终验收不需要虚构失败测试。
- 实施前重新加载适用指令并记录工作树差异。修改已有文件使用最新 SHA-256 并协调并发改动；仅暂存本任务文件。
- 按 spec 保持最小改动：不调整重试次数、提供商并发预算、请求超时、公共配置 schema、CLI/socket/JSONL 协议、URLStore、业务编排器、Academic adapter 或 Bright Data adapter。
- Bright Data 的本次处理是 README 已知限制：偶发 HTTP 200 后 JSON 解析失败，原因未确认；不把它宣称为已修复，不推定为先前 captcha/header 问题，也不新增解析重试。
- DeepSeek（issue #98）和通用提供商参数覆盖（issue #24）继续延期。不给这两个新 adapter 增加 Fetch、SDK、backend 开关、额外请求选项框架或业务错误自动重试。
- 完成本计划的实现与验收后，进入项目已有审阅、版本决策及发布流程。本计划不自动选版本、打 tag、推送或发布。

## 1. 已核实的实现路径与文件边界

现有路径为 CLI → daemon → SearchOrchestrator → registry 装配的关键词 adapter → HttpJsonExecutor → retry_async。新 provider 只生产 KeywordSearchHit；现有流水线继续管理 quota、Google goto 解包、URL 规范化/去重、准入、正文状态和输出。

| 文件 / 位置 | 已有责任与本次处理 |
| --- | --- |
| `src/agent_search_gateway/retry.py:17-50` | 已有 adjust_delay、before_attempt、on_retry 和 sleep 注入。只取消调整结果的第二次上限裁剪；保留本地退避上限、有限性保护和取消传播。 |
| `src/agent_search_gateway/providers/http.py:23-32,177-393,469-482` | HTTP 状态判断、响应诊断及重试所有者。新增 Retry-After 解析、内部异常的已解析秒数、延迟来源字段；现有 response inspector 优先级不变。 |
| `src/agent_search_gateway/providers/web/common.py:17-28,48-121` | JsonRequester、endpoint、failure、字段验证和安全错误提取。协议没有 params 参数；知乎按 Brave 的方式在 URL 中 urlencode Query，保持此文件不变。 |
| `src/agent_search_gateway/providers/diagnostics.py` | 复用 CandidateCounts、safe_diagnostic_fields、best_effort_diagnostics、safe_failure_reason、diagnostic_fields_from_failure；不修改。 |
| `src/agent_search_gateway/observability.py:24-52` | 已有 decode_error、invalid_record_shape、invalid_record、missing_url 等原因及有界诊断文本；不修改。 |
| `src/agent_search_gateway/providers/defaults.py` | 追加两个 WebProviderRegistration；复用 search=True、fetch=False、allowed_config_keys 和默认 requires_api_key=True。 |
| `src/agent_search_gateway/config.py:72-148` | 现有 resolver 已验证凭据、未知选项和 Fetch 能力。Web api_url 的默认值由新 adapter 构造函数负责；不修改 resolver。 |
| `src/agent_search_gateway/runtime.py:211-271` | 已注入 name、http_executor、secret 和选项，且按能力组装；不修改。 |
| `tests/unit/test_retry.py:246-284` | 旧测试明确要求调整值 5.0 被裁成 0.1；更新为新契约，并保留 NaN/inf/负值保护。 |
| `tests/providers/test_registry.py:82-122`、`tests/runtime/test_runtime_assembly.py:119-145` | 有精确注册顺序断言；新增 provider 时维护这些断言，不删除旧 provider 验证。 |
| `tests/acceptance/test_gateway_workflows.py:559-695` | 已有真实 adapter + MockTransport + SearchOrchestrator + URLStore + Fetch 的集成例子，可直接沿用组装方式。 |
| `src/agent_search_gateway/cli.py:145-156`、`daemon.py:128-196` | run_command 接受显式 RuntimePaths；ForegroundDaemon 有 ready/stopped 和独立 socket，供隔离验收复用。 |

### 1.1 文件清单

生产代码仅新增：

- `src/agent_search_gateway/providers/web/yuanbao.py`
- `src/agent_search_gateway/providers/web/zhihu.py`

生产代码仅修改：

- `src/agent_search_gateway/retry.py`
- `src/agent_search_gateway/providers/http.py`
- `src/agent_search_gateway/providers/defaults.py`

文档修改：`README.md`、`config.example.toml`。不编辑历史 spec、发布元数据或自动生成的站点内容。

新增测试：

- `tests/providers/web/test_yuanbao.py`
- `tests/providers/web/test_zhihu.py`

新增脱敏 fixture：

- `tests/fixtures/providers/yuanbao/search.json`
- `tests/fixtures/providers/yuanbao/error.json`
- `tests/fixtures/providers/yuanbao/README.md`
- `tests/fixtures/providers/zhihu/zhihu_search.json`
- `tests/fixtures/providers/zhihu/global_search.json`
- `tests/fixtures/providers/zhihu/zhihu_search_empty.json`
- `tests/fixtures/providers/zhihu/global_search_empty.json`
- `tests/fixtures/providers/zhihu/README.md`

扩展现有测试：`tests/unit/test_retry.py`、`tests/providers/test_http_executor.py`、`tests/providers/academic/test_semantic_scholar.py`、`tests/providers/academic/test_arxiv.py`、`tests/providers/test_registry.py`、`tests/unit/test_config_web_providers.py`、`tests/runtime/test_runtime_assembly.py`、`tests/acceptance/test_gateway_workflows.py`、`tests/docs/test_documented_config.py`。优先把新 provider 的集成终态断言放在同一 acceptance 用例，避免跨文件复制装配。

## 2. 实施前锁定的内部契约

### 2.1 Retry-After

- 小型私有纯函数：`_parse_retry_after(value: str | None, *, now: float) -> float | None`，放在 `providers/http.py`；now 是当前 UTC Unix 秒数，调用处传 `time.time()`，测试固定该值。不增加公共配置或 executor 构造参数。
- 只接受整数 delta-seconds 或 HTTP-date。整数必须为 ASCII 非负十进制；0 有效；空值、负数、小数、NaN、Infinity、无法表示为有限 float 的值和坏日期返回 None。有效过去日期返回 0；日期时区按 HTTP 规则统一到 UTC，不依赖机器本地时区。
- 使用标准库日期解析，捕获解析/范围转换的普通失败；不把原始 header 存入异常或日志，不自定“最大允许等待秒数”。
- `_RetryableStatus` 增加 `retry_after_seconds: float | None = None`。只存验证后的秒数；其原有 status_code、diagnostics 继续保留。
- 只有原有可重试 HTTP 状态路径解析 header：408、429、现有 5xx 判断。不要扩大可重试范围或把 HTTP 200 业务错误改写成 HTTP 429。
- 有效服务端时长直接替代本地退避，包含 0、短于 base 的值和超过 max_delay 的值。判断使用 `is not None`；不能使用 truthiness 或 `max(backoff, retry_after)`。
- retry_async 初始本地退避仍先受 max_delay_seconds 限制；adjust_delay 返回的有限数仅下限裁为 0，不再二次上限裁剪。调整异常/非有限结果回退本地值。
- HttpJsonExecutor 始终传现有 _adjust_retry_delay：先处理带时长的 _RetryableStatus，再保留 ProviderResponseFailure 的 `random.uniform(0.8, 1.0)` 抖动。抖动仍基于已封顶的本地退避，不抖动服务端指令。
- 现有 http_retrying 事件增加固定 `delay_source=retry_after|backoff`，由事件所有者直接传递，不扩充通用诊断字段白名单。保留各分支原日志级别。
- `delay_ms` 必须对应实际传给 sleep 的时长。超大但有限的秒数不能因 `int(delay * 1000)` 溢出而丢失整个日志；测试驱动使用不会产生浮点无穷的转换，不以压短等待解决日志问题。

### 2.2 Adapter、URL 与结果

两个构造函数均采用现有 keyword-only 方式：

~~~python
def __init__(
    self,
    *,
    name: str,
    secret: SecretValue,
    http_executor: JsonRequester,
    api_url: str = OFFICIAL_BASE_URL,
) -> None: ...

async def search(self, query: str) -> list[KeywordSearchHit]: ...
~~~

- `api_url` 在这两个新 adapter 中都表示基础 URL：知乎默认 `https://developer.zhihu.com`，元宝默认 `https://api.wsa.cloud.tencent.com`。用现有 endpoint helper 追加固定路径。配置示例不重复写 /SearchPro 或 /api/v1/content 路径；测试覆盖自定义基础路径和末尾斜杠。
- 元宝请求固定 POST /SearchPro，Bearer service API key，JSON body 恰为 `{"Query": query}`；不发送 TC3 签名字段或未开放的 vendor options。
- 知乎两个分支标识及 HTTP/candidate stage 固定为 `zhihu_search`、`global_search`，路径分别为 /api/v1/content/zhihu_search、/api/v1/content/global_search；Query 使用 urlencode 保留原始查询语义，不能手工拼未转义文本。
- 知乎请求固定带 Content-Type: application/json；Authorization 和 Unix 秒级 X-Request-Timestamp 在每次分支调用共享 executor 前生成，不能在 adapter 构造时缓存时间戳。时钟测试 monkeypatch 本模块使用的 time.time；不新增公共 clock 配置。Task 1 核对官方时间戳语义，不擅自设计签名刷新协议。
- 新 adapter 使用模块 logger，复用现有安全诊断。正常结果仅赋值 url、title、snippet，始终保留 `raw_content == content == ""`。
- 元宝将非空白 content 作为 snippet，否则取 passage；它们都是摘要。可选字符串缺失按现有 optional_string 处理；错误类型的记录按已有原因拒绝，不对任意对象调用 str。
- URL 的非空/类型要求在 adapter 检查；规范化、scheme、重定向、去重和准入继续由现有流水线处理。adapter 不引入 URL 集合或第二套 normalizer。

### 2.3 Envelope、分支失败与诊断

- 元宝先验证根对象与 Response；只要 Response 中出现 Error，就不能走成功路径，即使同时存在 Pages。Code/Message 是大写字段，先在本模块显式翻译给 selected_error_fields 的小写 code/message 输入。Error 自身结构异常同样产生已有 typed failure，不能变成零结果。
- Response/Pages envelope 无效属于整个 adapter 失败；Pages=[] 是成功空结果。
- Pages 的每条数据必须先是字符串，再单独 json.loads 成对象。坏 JSON → decode_error；非字符串/非对象 → invalid_record_shape；缺 URL → missing_url；其他已有字段错误 → invalid_record。这些原因已在现有白名单中，无需改 common/diagnostics/observability。
- 每个有效 Pages envelope 和每个有效知乎分支使用独立 CandidateCounts；received 包括被拒绝条目。计数结束满足 received = accepted + rejected，unprocessed=0；成功空列表也记录零计数。
- 知乎并发启动两个分支，按固定“站内、全网”顺序合并成功结果，避免完成时序决定结果优先级。一个分支成功（包括 []）就保留该成功；两支普通失败才抛一个已有 ExecutionFailure。
- 使用两条本地 task：分支普通 Exception 转成带固定 branch 的结果值；CancelledError 不转换。外层 gather 正常合并；捕获取消后取消尚未结束的 sibling 并 await 清理，再重新抛原取消。不能只用 gather(return_exceptions=True) 后把 CancelledError 当普通失败吞掉。
- 恰有一支失败时，adapter 发一次 provider_partial_failure，包含 provider、stage=search、固定 branch、error_type、safe_failure_reason 和 diagnostic_fields_from_failure；禁止 str(exc)、完整 traceback 或原 payload。
- 双失败保留固定顺序首个失败的安全诊断与异常 cause；抛出的汇总消息使用固定本地文本。adapter 不发送 provider_completed/provider_failed，终态仍由 SearchOrchestrator 唯一负责。
- response_diagnostics callback 只识别自己的已知 envelope，并经 best_effort_diagnostics / safe_diagnostic_fields；不作为重试 inspector，不让诊断失败改变业务结果。HTTP response metadata 继续由 executor 负责。

## 3. 任务与验证约定

每个开发 Task 的 Step 1 表中每一行是一轮独立行为循环；不要一次把整个矩阵写完再实现。所有命令在本工作树执行，使用现有 uv 环境。需要准备依赖时：

~~~bash
uv sync --locked --all-groups
~~~

若安装需要联网，按全局指令仅对该命令恢复 CODEXPRO_TOOL_*_PROXY，不输出凭据、不修改全局代理。后续测试使用 `uv run --offline`；环境未准备好导致的导入/依赖错误不属于有效 RED。

开发 Task 的 GREEN 运行目标测试、所列完整组件测试，并按 TDD 技能执行一次完整离线 suite：

~~~bash
uv run --offline pytest -q
~~~

REFACTOR 仅有实际代码整理时复跑受影响组件及完整 suite；没有改动时保留刚得到的证据，不重复无意义检查。真实服务可用性不作为 deterministic CI 的门槛。

### Task 1: 固定外部契约、fixture 来源和实施基线

**Files:**
- Read: 三份本任务 spec；`src/agent_search_gateway/providers/web/brave.py`、`anysearch.py`、`common.py`、`tests/support/http.py`。
- Create: §1.1 列出的两组 fixture 及其 README。
- Test: `tests/unit/test_retry.py`、`tests/providers/test_http_executor.py`、`tests/providers/test_registry.py`、`tests/providers/academic/test_semantic_scholar.py`、`tests/providers/academic/test_arxiv.py`、`tests/providers/web/test_brightdata.py`。
- Docs: 本节官方链接和三份 spec。

- [ ] **Step 1: 记录真实契约，暂不写生产代码。** 元宝服务密钥请求与 Pages 字符串结果已由 [服务 API KEY 文档](https://cloud.tencent.com/document/product/1806/130615)、[SearchPro 字段文档](https://cloud.tencent.com/document/product/1806/121811) 和 [错误 envelope 文档](https://cloud.tencent.com/document/api/1806/121820) 核实。fixture 保留结构、字段类型、大小写和未消费的兄弟字段，正文换成合成短文本，移除真实账户、凭据和 RequestId。
- [ ] **Step 2: 关闭知乎解析器的证据缺口。** 从 [知乎开发平台](https://developer.zhihu.com/) 的两个接口文档/官方导出或各一次受控脱敏响应，分别确认 method/path、Query、success Code 的值及类型、结果列表路径、URL/title/summary 的准确键名、空列表和业务错误语义。检索时只取得了站内请求/两者鉴权证据；quota 文档的 Code=0 不能代替搜索成功码。不得猜测 Data.Items、大小写别名或 summary 链。官方资料若与 spec 候选路径不同，记录准确差异并在实现对应解析器前解决；不擅改已提交设计。
- [ ] **Step 3: 写来源 README。** 每个 endpoint 记录来源 URL、核对日期、成功/空响应路径与准确字段映射，明确哪些 fixture 是文档结构合成、哪些是脱敏实测。不得复制整篇官方文档、返回正文或把猜测标为实测。未核实知乎 schema 时，仅阻塞 Task 9–11 的解析实现；Task 2–8 可以独立推进，但不得宣称整个 feature 已完成。
- [ ] **Step 4: 跑现有基线。**
~~~bash
uv run --offline pytest tests/unit/test_retry.py tests/providers/test_http_executor.py tests/providers/test_registry.py tests/providers/academic/test_semantic_scholar.py tests/providers/academic/test_arxiv.py tests/providers/web/test_brightdata.py -v
~~~
预期：已有测试通过；若失败，保留具体命令与失败原因，先区分环境、既有缺陷和本任务行为。
- [ ] **Step 5: 确认 fixture 与改动范围。** 不加入新网络测试开关、通用 harness 或 SDK；不运行全提供商搜索。资料不足时将缺项定位到具体 endpoint/字段，不把用户已经写入 spec 的选择重新问一遍。

### Task 2: 让现有 delay adjuster 的有效结果成为最终等待值

**Files:**
- Modify: `src/agent_search_gateway/retry.py:38-46`。
- Test: `tests/unit/test_retry.py:210-385`。

- [ ] **Step 1: Write the failing test.** 从现有 `test_retry_delay_adjuster_clamps_invalid_delays` 拆出单一行为 `test_adjusted_delay_can_exceed_local_maximum`：max_delay=0.1，operation 第一次失败第二次成功，adjuster 返回 5.0，fake sleep 和 on_retry 都必须收到 5.0。旧参数 (5.0,0.1) 随契约改为 (5.0,5.0)，不要留互相矛盾的断言。其他 NaN/inf/负数用例保留并改名明确其保护含义。
- [ ] **Step 2: Run test to verify it fails.**
~~~bash
uv run --offline pytest tests/unit/test_retry.py -k adjusted_delay_can_exceed_local_maximum -v
~~~
预期：实际 0.1 不等于期望 5.0；不是 import/fixture 错误。
- [ ] **Step 3: Minimal implementation.**
~~~text
先按现有公式计算 capped local delay
调用现有 adjust_delay
若结果 finite：delay = max(0, adjusted)
否则或 adjuster 普通失败：保留 local delay
按原次序 on_retry → sleep；保留取消和最终尝试检查
~~~
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/unit/test_retry.py -v
~~~
预期：新时长契约、普通 backoff 上限、hook 失败回退、取消与重试预算测试全通过；再执行 §3 的完整 suite。
- [ ] **Step 5: Refactor.** 仅整理必要的测试命名/重复 setup，不增加 RetryPolicy 字段或第二个调度函数。若有改动，复跑该文件及完整 suite。

### Task 3: 将 HTTP Retry-After 秒数接到共享重试路径

**Files:**
- Modify: `src/agent_search_gateway/providers/http.py:28-32,326-340,469-473`。
- Test: `tests/providers/test_http_executor.py`。

- [ ] **Step 1: Write the failing test.** 新增 `test_retry_after_delta_seconds_overrides_backoff`，用真实 HttpJsonExecutor + MockTransport 返回 429/Retry-After:5 后 200，RetryPolicy(2,0.25,2.0,30.0)，fake sleep 应仅收到 [5.0]，真实 transport 请求恰为两次。随后逐轮加入 header=0、header 短于本地 delay，以及没有 response_inspector 的 JSON/text 两种调用路径。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/test_http_executor.py -k retry_after_delta_seconds_overrides_backoff -v
~~~
预期：当前实际等待 0.25，未遵循服务端 5；目标行为缺失。
- [ ] **Step 3: Minimal implementation.** 按 §2.1 给 _RetryableStatus 保存已验证秒数，在原 retryable HTTP 状态分支提取 header；始终传现有 adjuster；adjuster 用 is not None 返回服务器秒数。先实现测试所需 delta-seconds 路径，保留 inspector 报错优先级。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/providers/test_http_executor.py tests/unit/test_retry.py -v
~~~
预期：超过 cap 的 5、有效 0 和较短服务端时长均精确生效，未指定 header 的旧用例不变；再跑完整 suite。
- [ ] **Step 5: Refactor.** 保留一个本模块私有解析 helper；不在 Academic/Web adapter 内增加 retry。复跑受影响组件和完整 suite。

### Task 4: HTTP-date、坏 header 与安全数值边界

**Files:**
- Modify: `src/agent_search_gateway/providers/http.py` 的 _parse_retry_after。
- Test: `tests/providers/test_http_executor.py`。

- [ ] **Step 1: Write the failing test.** 先写 `test_retry_after_http_date_uses_fixed_clock`：固定 Unix now，503 带未来 5 秒 HTTP-date 后成功，sleep=[5.0]。逐轮补充：
  - `test_retry_after_past_date_is_zero`：过去日期转 0，不产生负 sleep。
  - `test_invalid_retry_after_falls_back_to_backoff`：缺失/空白、坏日期、负整数、+1、小数、NaN/Infinity、指数数字、非 ASCII 数字、超出有限 float 范围的超长整数；均保持原 backoff，不抛解析异常。
  - 有效日期时区/标准旧式日期和首尾 HTTP 空白；遵守 HTTP 日期语义，避免系统本地时区影响。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/test_http_executor.py -k retry_after_http_date_uses_fixed_clock -v
~~~
预期：尚未实现日期时得到本地 delay，随后每个新增边界按实际缺失行为核实失败；已经成立的 fallback 记录为回归。
- [ ] **Step 3: Minimal implementation.** 纯 helper 接收显式 now，使用标准库解析日期、转换 UTC、求非负有限差值；无效转换返回 None。executor 测试 monkeypatch 其 time.time 并保留真实 helper/真实 transport；不要 mock 解析结果制造成功。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/providers/test_http_executor.py -k retry_after -v
uv run --offline pytest tests/providers/test_http_executor.py tests/unit/test_retry.py -v
~~~
预期：日期和整数规则一致，坏输入回退，长但可表示的合法等待不被裁短；再跑完整 suite。
- [ ] **Step 5: Refactor.** 不引入外部日期库、任意等待上限或 header 原文日志。复跑目标和完整 suite。

### Task 5: 保持尝试/终态边界并证明两个学术调用路径获益

**Files:**
- Modify: `src/agent_search_gateway/providers/http.py:208-261` 的 retry 事件字段及毫秒转换。
- Test: `tests/providers/test_http_executor.py`、`tests/providers/academic/test_semantic_scholar.py`、`tests/providers/academic/test_arxiv.py`。
- Regression: `tests/providers/web/test_brightdata.py`、`tests/observability/test_provider_event_logging.py`。
- Unchanged: `src/agent_search_gateway/providers/academic/semantic_scholar.py`、`arxiv.py`、`providers/web/brightdata.py`。

- [ ] **Step 1: Write the failing test.** `test_retry_after_logs_actual_delay_and_source` 使用 tests.support.logging.structured_test_logger，断言 retry_after/backoff 来源、实际 delay_ms、事件级别和次数。再加 `test_large_finite_retry_after_keeps_retry_event`，用可表示但乘 1000 会溢出的有限秒数及 fake sleep，要求事件仍出现。逐个补充或复用以下边界：
  - 408/429/503 耗尽恰为 max_attempts 次请求、max_attempts-1 次 sleep；最后一次不再睡眠。
  - 401/403 带 header 不重试；200 坏 JSON 不因 header 重试，继续 ProtocolFailure；保留现有坏 Atom 测试。
  - transport timeout 保持 backoff；Bright Data inspector 原 0.8–1.0 抖动不变且仍在本地 cap 内。
  - 操作中及 Retry-After sleep 中取消均传播，之后无请求；重试最终成功没有 http_failed/provider_failed。
  - 两个学术真实 executor 集成：`test_semantic_scholar_honors_shared_retry_after` 和 `test_arxiv_honors_shared_retry_after`。使用现有 search.json/search.xml fixture，429→成功，验证命中解析与 fake sleep。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/test_http_executor.py -k "retry_after_logs_actual_delay_and_source or large_finite_retry_after_keeps_retry_event" -v
~~~
预期：来源字段尚不存在；极大有限 delay 的 retry 事件因原浮点乘法消失。学术/取消/错误边界若已通过，作为兼容性证据，不要求为它们改生产代码。
- [ ] **Step 3: Minimal implementation.** retry event 直接写固定 delay_source。安全转换实际秒数为毫秒，避免先把浮点数乘成 inf；不改变 sleep 参数。学术测试实例化现有 SemanticScholarProvider(executor) 与 ArxivProvider(executor)，只新增测试，不编辑 adapter。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/unit/test_retry.py tests/providers/test_http_executor.py tests/providers/academic/test_semantic_scholar.py tests/providers/academic/test_arxiv.py tests/providers/web/test_brightdata.py tests/observability/test_provider_event_logging.py -v
~~~
预期：RT-01–RT-12 全覆盖，JSON 和 Atom 共用修复，Bright Data 行为不变；再跑完整 suite。
- [ ] **Step 5: Refactor.** 日志字段不经过需扩展的公共白名单，不存原始 header/response。只去除本次测试的明显重复；有代码整理才复跑组件和完整 suite。

### Task 6: 元宝基本搜索与 search-only 注册

**Files:**
- Create: `src/agent_search_gateway/providers/web/yuanbao.py`、`tests/providers/web/test_yuanbao.py`。
- Modify: `src/agent_search_gateway/providers/defaults.py`。
- Test: `tests/providers/test_registry.py`、`tests/runtime/test_runtime_assembly.py`。
- Fixture: `tests/fixtures/providers/yuanbao/search.json`。

- [ ] **Step 1: Write the failing test.** `test_yuanbao_search_uses_service_key_and_maps_pages` 从 build_default_registry 的元宝 factory 发起真实 executor 搜索，不顶层导入尚不存在的模块。MockTransport 检查默认 host、/SearchPro、POST、Bearer、JSON content-type、恰有 Query 且原词完整；返回 Pages JSON 字符串 fixture，断言具体 hits 和两正文槽为空。首轮失败应为 provider 尚未注册，不能完成搜索。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/web/test_yuanbao.py -k uses_service_key_and_maps_pages -v
~~~
预期：现有入口没有 yuanbao 的行为，测试不能获得期望结果；不能因导入错误停止收集。
- [ ] **Step 3: Minimal implementation.** 添加新模块、默认 base 构造参数、Bearer POST、已核实的 envelope 与单条字符串解码、url/title/snippet 映射。向 defaults 追加 yuanbao 注册，search=True、fetch=False、allowed_config_keys=frozenset({"api_url"})。更新两个精确注册顺序断言以保留所有旧项，并包含目前已实现的新项。
- [ ] **Step 4: Verify GREEN.** 逐轮加入 omitted/default api_url、显式 custom base（含路径前缀与末尾斜杠）、只有 passage、空白 content 回退、成功空 Pages 测试。
~~~bash
uv run --offline pytest tests/providers/web/test_yuanbao.py tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py -v
~~~
预期：请求使用服务密钥通道；新条目仅能 search；不新增依赖；再跑完整 suite。
- [ ] **Step 5: Refactor.** 复用 endpoint/configured_string/字段 helper；不要抽象出新通用搜索 parser，不修改 registry/config/runtime 生产文件。复跑组件和完整 suite。

### Task 7: 元宝业务错误优先于成功/空结果

**Files:**
- Modify: `src/agent_search_gateway/providers/web/yuanbao.py`。
- Test: `tests/providers/web/test_yuanbao.py`。
- Fixture: `tests/fixtures/providers/yuanbao/error.json`。

- [ ] **Step 1: Write the failing test.** `test_yuanbao_business_error_precedes_pages_validation`：HTTP 200，Response.Error 含 Code/Message，同时有 Pages=[] 或缺 Pages，必须产生已有 ExecutionFailure，安全诊断含供应商错误，HTTP 请求仅一次。随后覆盖 Error=null/坏形状、缺 Response、非对象 Response、缺/非列表 Pages。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/web/test_yuanbao.py -k business_error_precedes_pages_validation -v
~~~
预期：若错误被忽略，原实现会返回 [] 或报错时丢失 Code/Message；必须定位到该缺失行为。
- [ ] **Step 3: Minimal implementation.**
~~~text
require outer object → require Response object
if "Error" exists:
    只选择已知 Code/Message，转换为 selected_error_fields 的输入
    raise existing failure with safe provider_response diagnostics
require Pages list
继续原有成功解析
~~~
补本模块 response_diagnostics：decode/invalid_envelope/provider_response/success 的安全分类。业务判断仍在 adapter，不放到 retry inspector；RequestLimitExceeded 等业务码也不新加重试。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/providers/web/test_yuanbao.py tests/providers/test_http_executor.py -v
~~~
预期：HTTP 200 业务错误不成空成功，空 Pages 的正常成功仍成立，诊断不带完整 Error/RequestId；再跑完整 suite。
- [ ] **Step 5: Refactor.** 大写字段转换留在本模块；通用 selected_error_fields 和异常类不变。复跑组件和完整 suite。

### Task 8: 元宝坏条目隔离、准确计数与日志安全

**Files:**
- Modify: `src/agent_search_gateway/providers/web/yuanbao.py`。
- Test: `tests/providers/web/test_yuanbao.py`。
- Reuse: `tests/support/http.py`、`tests/support/logging.py`、`src/agent_search_gateway/providers/diagnostics.py`。

- [ ] **Step 1: Write the failing test.** `test_yuanbao_keeps_valid_pages_and_counts_rejections`：同一 Pages 混合正常字符串、坏 JSON、JSON 数组/标量、非字符串、空/缺 URL 和错误可选字段。断言好结果仍在、精确原因计数和没有请求级重试。随后独立增加日志 sentinel 测试、HTTP 429→成功测试和 content/passsage 的正文空槽断言。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/web/test_yuanbao.py -k keeps_valid_pages_and_counts_rejections -v
~~~
预期：缺少逐项隔离时会丢好结果或缺少准确摘要计数；若某项已成立则保留其回归断言。
- [ ] **Step 3: Minimal implementation.** CandidateCounts 以原 Pages 长度初始化；按 §2.3 分类拒绝，仅包围单条 decode/字段验证；整体 envelope 失败不能被条目 catch 吞掉。逐条 accept/reject，最终一次 adapter summary，返回成功兄弟。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/providers/web/test_yuanbao.py tests/observability/test_provider_diagnostics.py tests/observability/test_redaction.py -v
~~~
使用真实 KeyValueFormatter + SecretRedactor 的渲染输出断言测试凭据、Query sentinel、Pages 正文和 unaudited RequestId 不出现；selected Code/Message 有界且脱敏。再跑完整 suite。
- [ ] **Step 5: Refactor.** 不扩展共享 fake 以复制腾讯完整 schema；局部 fixture 负责供应商形状。保证每条只计数一次；复跑组件和完整 suite。

### Task 9: 知乎双接口基本搜索与一个 provider 注册

**Files:**
- Create: `src/agent_search_gateway/providers/web/zhihu.py`、`tests/providers/web/test_zhihu.py`。
- Modify: `src/agent_search_gateway/providers/defaults.py`。
- Test: `tests/providers/test_registry.py`、`tests/runtime/test_runtime_assembly.py`。
- Fixture: Task 1 的四份知乎 fixture 及其来源说明。
- Prerequisite: Task 1 已核实两个搜索响应契约。

- [ ] **Step 1: Write the failing test.** `test_zhihu_search_combines_both_official_endpoints` 从现有 registry factory 发起搜索。按 request.url.path 返回各自 fixture；断言两个 GET、精确路径和 Query、Bearer、Content-Type: application/json、秒级 X-Request-Timestamp、各自 stage、命中合并与正文空槽。两端都返回同一 URL 时 adapter 应保留两个候选，留给流水线去重。首轮通过“尚未注册导致无法完成搜索”观察 RED。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/web/test_zhihu.py -k combines_both_official_endpoints -v
~~~
预期：缺少知乎 provider 行为；禁止不存在模块的 collection error 或自行编造 fixture 结构。
- [ ] **Step 3: Minimal implementation.** 建立 keyword-only 构造与默认 base；endpoint + urlencode；每次分支生成鉴权/时间戳；并发两条局部 task，各按独立的确切契约验证成功 Code、结果列表、条目。按站内/全网固定顺序组合，添加唯一 zhihu 注册，不添加 zhihu_site/zhihu_global。最终注册顺序固定为原十八个 provider，随后 yuanbao、zhihu，与 Task 6 先追加元宝的顺序一致。
- [ ] **Step 4: Verify GREEN.** 逐轮加入两分支均进入后才释放的事件驱动并发测试、再次搜索时更新时间戳、中文及含 &/?/空格的查询、custom base/末尾斜杠、双空列表、标题/摘要缺失规则。测试使用按 path/stage 分派的 requester，避免共享 FIFO fake 的并发时序假设。
~~~bash
uv run --offline pytest tests/providers/web/test_zhihu.py tests/providers/test_registry.py tests/runtime/test_runtime_assembly.py -v
~~~
预期：一个逻辑 provider 发两次独立请求，精确映射、无本地去重；再跑完整 suite。
- [ ] **Step 5: Refactor.** 合并两个 branch 的相同生命周期代码；不同响应字段仍显式映射，不增加宽松别名或反射式 parser。复跑组件和完整 suite。

### Task 10: 知乎部分成功、业务失败和候选诊断

**Files:**
- Modify: `src/agent_search_gateway/providers/web/zhihu.py`。
- Test: `tests/providers/web/test_zhihu.py`。

- [ ] **Step 1: Write the failing test.** `test_zhihu_preserves_successful_branch_after_sibling_failure`，分别覆盖站内成功/全网 HTTP 耗尽、全网成功/站内 protocol 失败；成功分支也参数化为 []。断言成功结果保留、恰有一条固定 branch 的 provider_partial_failure、成功分支不被整个 search 重跑。逐轮覆盖两支失败、HTTP 200 非成功 Code、错误 envelope/缺结果列表、混合好坏条目、成功空结果与日志脱敏。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/web/test_zhihu.py -k preserves_successful_branch_after_sibling_failure -v
~~~
预期：缺少容错时抛整提供商失败，或缺少部分失败诊断；不能误把有效空列表看作失败。
- [ ] **Step 3: Minimal implementation.** 按 §2.3 收集普通分支失败，成功分支各自计数；只有两支失败才抛固定 ExecutionFailure 并带首个安全 cause/diagnostics。response_diagnostics 只选择本接口已核实的 Code/Message/结构分类。单支业务错误不成为 transport retry，普通失败不记录 raw payload 或 str(exc)。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/providers/web/test_zhihu.py tests/observability/test_provider_diagnostics.py tests/observability/test_redaction.py -v
~~~
预期：partial 仅一次；per-branch HTTP 失败/response/candidate 事件与 provider 终态职责分开，成功结果不丢；再跑完整 suite。
- [ ] **Step 5: Refactor.** 原因和 branch 使用固定枚举值；复用现有安全失败提取，不扩充 logging 系统。复跑组件和完整 suite。

### Task 11: 知乎取消传播和 sibling 清理

**Files:**
- Modify: `src/agent_search_gateway/providers/web/zhihu.py` 的两分支生命周期。
- Test: `tests/providers/web/test_zhihu.py`。

- [ ] **Step 1: Write the failing test.** `test_zhihu_branch_cancellation_cancels_and_awaits_sibling`：使用 asyncio.Event 的受控 requester，一支抛 CancelledError，另一支停在可取消等待并在 finally 置 cleanup 标志。外层必须收到 CancelledError、cleanup 已完成、没有 partial success 返回或遗留后台请求。再独立测试调用者取消整个 search，以及分支处于 Retry-After sleep 时取消。
- [ ] **Step 2: Verify RED.**
~~~bash
uv run --offline pytest tests/providers/web/test_zhihu.py -k cancellation -v
~~~
预期：缺少传播/清理时，取消被当普通分支错误、sibling 未清理或用例在有界测试等待内超时。用事件协调和小型测试 watchdog，不靠真实长 sleep。
- [ ] **Step 3: Minimal implementation.** 普通失败包装不捕获 BaseException；聚合明确识别 asyncio.CancelledError，取消仅本调用持有的未完成 branch tasks，await 它们的完成后原样重新抛出。不改变共享 executor 的取消语义。
- [ ] **Step 4: Verify GREEN.**
~~~bash
uv run --offline pytest tests/providers/web/test_zhihu.py tests/providers/test_http_executor.py tests/unit/test_retry.py -v
~~~
预期：三种取消来源都不产生后续请求或错误的成功/partial 事件；再跑完整 suite。
- [ ] **Step 5: Refactor.** 保持只两条局部 task 的直接控制流；不创建任务管理框架，不终止其他调用。复跑组件和完整 suite。

### Task 12: 完成注册、配置和 Runtime 兼容性验证

**Files:**
- Test/Modify: `tests/providers/test_registry.py`、`tests/unit/test_config_web_providers.py`、`tests/runtime/test_runtime_assembly.py`。
- Verify unchanged: `src/agent_search_gateway/providers/registry.py`、`config.py`、`runtime.py`。

- [ ] **Step 1: 写单行为回归测试。** 对两个实际注册名分别验证 search-only、factory、默认 requires_api_key 和仅 api_url 的 allowed_config_keys。通过现有 resolver 验证缺 api_key_env、环境缺值、Fetch=true、未知选项被拒绝，disabled provider 不要求凭据但仍拒绝未知选项。默认/显式 URL 最终在真实 Runtime.build → adapter 层验证；不要错误地要求 resolver 注入默认 URL。
- [ ] **Step 2: 执行并归类。**
~~~bash
uv run --offline pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py -v
~~~
这些大多验证既有能力，应已绿；失败时先区分新注册/构造不匹配与测试假设。将 test_registry.py 的 list[-10:] 改为末尾十二项（或分别断言旧十项和追加两项）；test_runtime_assembly.py 的完整十八项期望追加 yuanbao、zhihu，保持所有旧项原顺序，不能只给旧切片增加期望项或删除顺序断言。
- [ ] **Step 3: 最小修正。** 只修新 adapter 构造/注册声明和被新增条目影响的测试预期。使用已有 _CountingAsyncClient、Runtime.build 的 http_client_factory 与 RuntimePaths.from_home(tmp_path)，检验一逻辑 provider 一个 executor/client/quota、没有 Fetch entry、Runtime.aclose 正确关闭。发现必须修改 resolver/runtime 的理由时先核对 spec，不能顺手重构。
- [ ] **Step 4: 验证。**
~~~bash
uv run --offline pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/runtime/test_runtime_assembly.py tests/runtime/test_request_filtering.py -v
~~~
预期：既有能力与缺省行为稳定，无新公共参数；再跑完整 suite。
- [ ] **Step 5: 整理。** 不为本功能扩大共享 fake 协议、依赖清单或 config schema。仅必要重构后复跑测试。

### Task 13: 真实 adapter 穿过 CLI、流水线和 Fetch 状态边界

**Files:**
- Modify/Test: `tests/acceptance/test_gateway_workflows.py`。
- Regression: `tests/orchestrators/test_keyword_search_pipeline.py`、`tests/orchestrators/test_keyword_provider_filtering.py`、`tests/cli/test_cli.py`、`tests/observability/test_provider_event_logging.py`。
- Reuse unchanged: `tests/support/fakes.py`、`tests/support/acceptance.py`、`tests/support/logging.py`。

- [ ] **Step 1: 增加三个有业务结果的验收场景，逐个执行。**
  1. `test_zhihu_cli_selection_deduplicates_both_branch_urls`：实际 ZhihuAdapter + MockTransport，另一个 provider 若被调用就使测试失败；用真实 build_parser/run_command/daemon 或现有 socket 验收方式传 --provider zhihu。两分支返回等价 URL，断言恰两次 HTTP 调用、输出一条规范化后的 {url,abstract}、正常准入，无非选中 provider 请求。
  2. `test_zhihu_terminal_event_matches_branch_outcomes`：参数化单失败、单成功但空、双失败。分别断言一次 partial+一次 completed、一次 partial+一次 completed、一次 failed；筛选逻辑 provider 终态，不能把两个 http_failed 当重复终态。全选中 provider 失败时保留现有 ALL_PROVIDERS_FAILED 和不生成结果文件。
  3. `test_yuanbao_summary_does_not_skip_url_fetch`：真实元宝 adapter 的摘要进入真实 URLStore 后 raw_content/content 仍空；后续实际 FetchOrchestrator 使用现有 FakeURLFetchProvider，断言 Fetch 确实调用、正文来自 Fetch 而不是 SearchPro 摘要。
- [ ] **Step 2: 执行目标用例。**
~~~bash
uv run --offline pytest tests/acceptance/test_gateway_workflows.py -k "zhihu or yuanbao" -v
~~~
新 adapter 已正确时允许这些直接为绿，保留兼容性证明；不修改旧流水线来制造 RED。
- [ ] **Step 3: 只修新行为边界。** 沿用该文件现有真实 adapter/MockTransport/store/stages/Fetch 装配。单独组装能接收 KeywordSearchProvider 的测试对象；不要把真实 adapter 强塞入仅标注 list[FakeKeywordSearchProvider] 的旧 helper。新增生产缺陷仅回到对应 adapter/注册 Task 修复，保持编排器、store、CLI 不变。
- [ ] **Step 4: 完整组件回归。**
~~~bash
uv run --offline pytest tests/acceptance/test_gateway_workflows.py tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_provider_filtering.py tests/runtime/test_request_filtering.py tests/cli/test_cli.py tests/observability/test_provider_event_logging.py -v
~~~
预期：选择、标准化、去重、终态和正文准入全部沿旧路径工作，schema 仅 url/abstract；再跑完整 suite。
- [ ] **Step 5: 整理。** 使用现成 fake LLM 隔离 judge 等依赖，真实 transport 只用 MockTransport；不引入新网络 harness 或给生产代码加测试专用方法。

### Task 14: 配置示例与用户文档

**Files:**
- Modify: `README.md:59-106,340-387,415-447`、`config.example.toml`。
- Maintain existing tests: `tests/docs/test_documented_config.py:275-369`。

- [ ] **Step 1: 列出文档应表达的行为。** 能力表新增 Zhihu/Yuanbao（search=yes、fetch=no），说明知乎一个逻辑搜索会请求两个 API、可能消耗两份额度且允许单支成功；元宝采用 service API key，搜索摘要不等于网页正文。Retry-After 解释服务器等待优先、本地 cap 只管 backoff、最大尝试次数未变，不能承诺消灭所有 429。
- [ ] **Step 2: 维护现有配置契约测试。** 更新已有文档测试维护的 provider/capability/环境变量集合和注册顺序，不另写仅匹配警告句子的脆弱测试。示例 fixture 环境提供合成值，不读取用户凭据。
- [ ] **Step 3: 修改文档。** 两块配置均使用 enable_search=true、enable_fetch=false、仅含基础 URL 的 api_url、api_key_env；环境变量名只是示例，adapter 不硬编码名称。README 明确已有用户配置不会自动更新。Bright Data 已知限制写“偶发 HTTP 200 后 JSON 解析失败，原因尚未确认，可重试或改用其他 provider”，保留此前已实现 header 检查的说明，两者不混同。
- [ ] **Step 4: 验证可执行配置和现有回归。**
~~~bash
uv run --offline pytest tests/docs/test_documented_config.py tests/unit/test_config_web_providers.py tests/providers/web/test_brightdata.py -v
~~~
预期：示例能经现有 resolver 正确解析、Fetch 能力正确、旧 Bright Data 测试全部保持；检查文档没有真实秘密或将本次限制描述为已修复。
- [ ] **Step 5: 对齐文档。** 对照默认 base、固定路径、环境变量示例、服务端等待与源码测试语义。无需改历史 spec、CHANGELOG 版本段、pyproject、uv.lock 或生成的站点。

### Task 15: 最终验证、隔离冒烟和发布交接

**Files:**
- Verify: 本计划全部生产/测试/文档文件。
- Read: `RELEASING.md`、`CONTRIBUTING.md`、`src/agent_search_gateway/paths.py`、`cli.py`、`daemon.py`。
- Temporary only: 工作树内 `.task-smoke/zyr/run_smoke.py`、`.task-smoke/zyr/c.toml`、`.task-smoke/zyr/s.sock` 和 `.task-smoke/zyr/r/`；不提交这些临时文件/运行产物，不覆盖 .ai-bridge。

- [ ] **Step 1: 验收范围。** 确认生产 diff 仅包含 §1.1 的两新文件和三旧文件；对意外改动逐项处理。所有新增 provider 字段映射来自 Task 1 证据；没有未解释的失败、残留 task、正文污染或重复终态。
- [ ] **Step 2: 全量离线门禁。**
~~~bash
uv run --offline ruff format --check src tests scripts
uv run --offline ruff check .
uv run --offline mypy src tests scripts
uv run --offline pytest -v
uv run --offline python scripts/build_docs.py
~~~
预期：全部通过；若是已记录基线问题，列出确切证据和影响，不把未跑或失败写成通过。运行构建后检查生成文件是否属于应提交范围。
- [ ] **Step 3: 构造自有隔离运行实例。** paths.py 的默认路径来自 Path.home，并不读取 XDG 配置；只设置 XDG_* 不能隔离本项目。保持 HOME/CODEX_HOME 不变，不停止现有共享 daemon。创建本任务独占的新目录 `.task-smoke/zyr/`；若已存在则选新的任务专属后缀目录并记录实际命令，不能覆盖或清理已有内容。复制必要的配置块到 c.toml，凭据仍只通过环境变量名引用；不复制密钥值。直接使用现有字段避免 from_home 额外添加长目录后缀：
~~~python
task_dir = Path(".task-smoke/zyr").resolve()
paths = RuntimePaths(
    config_file=task_dir / "c.toml",
    socket_file=task_dir / "s.sock",
    results_dir=task_dir / "r",
)
daemon = ForegroundDaemon(
    paths, environ=os.environ, debug=True, logging_session=logging_session
)
daemon_task = asyncio.create_task(daemon.start())
~~~
绑定前检查绝对 socket 路径的编码字节长度适合该 Unix 平台。等待 daemon.ready 与 daemon_task 的启动失败二者之一，设置仅用于本地启动的有界 watchdog；若配置加载或绑定失败，立即传播该失败，不能无限等待 ready。该 watchdog 不限制搜索或 Retry-After 时长。就绪后用 build_parser().parse_args([...]) 和 run_command(args, paths, stdout=..., stderr=...) 发业务请求。try/finally 中只向相同 paths 发 stop，await daemon_task 完成，再关闭 logging_session；启动失败/驱动取消时仅取消并等待本驱动创建的 task。ForegroundDaemon 已会读取 paths.config_file、解析环境、登记日志脱敏 secret 并调用 Runtime.build，不需要新的 runtime_factory。
- [ ] **Step 4: 小型真实 smoke。** 使用工作树安装的模块和自有配置，引用用户已设置的实际 api_key_env，保持其共享配置文件不变。临时驱动命令为：
~~~bash
uv run --offline python .task-smoke/zyr/run_smoke.py
~~~
驱动的业务参数必须逐个等价于：
~~~text
keyword-search 人工智能 --provider zhihu
keyword-search 人工智能 --provider yuanbao
paper-search "machine learning" --provider semantic_scholar
paper-search "machine learning" --provider arxiv
~~~
逐次运行，不调用默认全 provider，不循环撞 429。知乎正常首轮有两次上游调用，合法 retry 时可能更多；日志仍只有一个逻辑 provider 终态。记录返回结果路径/命中数、请求 ID、选中 provider 及脱敏事件；不保存/展示密钥和原始响应。用户配置可能仍包含 fetch_llm 和 OA resolver；保留必需配置，若仅验证 direct paper 检索则在任务配置中关闭非目标 OA enrichment，明确其与 --provider 搜索过滤的差别。未遇到真实 429 不能证明 retry 无效，行为证明来自 MockTransport。
- [ ] **Step 5: 交接。** 汇总实际通过的命令、必要 smoke 结果/真实服务限制、最终改动范围与未解决项。确认所有自有运行资源释放且没有动共享实例。完成实现提交后按已有 code review/release 流程推进，版本号与发布动作留给该流程；本计划不把“已写计划”或“已有离线测试”当作已发布。

## 4. Spec 覆盖与类型自审

| Spec 要求 | 对应任务 / 证明 |
| --- | --- |
| RT-01/03/05/06：服务器秒数、零值、短值、超过本地 cap、无 header | Task 2–4 |
| RT-02/04：HTTP-date、时区、过去日期、无效/非有限输入 | Task 4 |
| RT-07–12：耗尽、非重试状态、解析错误、旧抖动、取消、日志 | Task 5、11 |
| Academic JSON 与 Atom 共同获益，adapter 无改动 | Task 5 两个真实 executor 集成 |
| ZH-01/02/06/07/08/09：双接口、鉴权、空结果、业务/结构错误和坏条目 | Task 1、9、10 |
| ZH-03/04/05/10：部分成功、双失败、取消传播 | Task 10、11、13 |
| ZH-11/12：单一 --provider、共享去重 | Task 13 |
| YB-01/02/03/06：服务密钥、字符串 Pages、摘要优先级、空结果 | Task 6 |
| YB-04/05/07/08/09/10：业务/结构错误、坏条目隔离、共享 retry、日志安全 | Task 7、8 |
| YB-11：摘要不填正文，后续 Fetch 仍执行 | Task 13 |
| 注册/config/runtime、能力限制、禁用及凭据语义 | Task 6、9、12、14 |
| Bright Data 实现不变、仅准确描述已知限制 | Task 5、14、15 |
| 现有公开协议、输出、配额、URLStore/编排器不变 | Task 12、13、15 |
| 聚合成功/失败唯一终态，安全有界日志 | Task 5、8、10、13 |
| focused/full lint/typecheck、受控真实 smoke、发布交接 | Task 15 |

类型与命名检查：

- 对外结果一律 `list[KeywordSearchHit]`，字段为 url/title/snippet/raw_content/content；供应商的 content 仅映射 snippet。
- `_parse_retry_after` 返回 float 或 None，`retry_after_seconds` 同类型；有效零值不丢失；日志 delay_source 固定两值。
- JsonRequester 的现有签名不新增 params；两个 factory 使用 name/secret/http_executor/api_url 这些已知 kwargs。
- 两 adapter 的 api_url 均为基础 URL，文档、构造默认值和测试端点保持一致；不由 config resolver 注入默认值。
- CandidateCounts 使用现有 allowlisted reason；不把 candidate_rejection_reason 的 invalid_record 与显式非对象分支的 invalid_record_shape 混为新公共类型。
- selected_error_fields 的 code/message 为小写输入，供应商大写字段只在本模块转换；日志用诊断包装而非异常字符串。
- CancelledError 是 BaseException，不能被普通分支结果逻辑吞掉；单支成功用“是否异常”判断，不能用 hits 是否非空判断。
- 实际配置测试目录是 `tests/unit/test_config_web_providers.py`，不是 testing spec 概述中的 tests/config。
- 当前唯一外部契约待证项是 Task 1 的知乎搜索响应 schema；已有明确取证步骤及实现门槛，不以猜测补齐。
