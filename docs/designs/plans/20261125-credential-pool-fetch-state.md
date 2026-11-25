# 凭证池、显式 Fetch 顺序与延迟正文处理 Implementation Plan

**Goal:** 为 Web、Academic、OA 与 LLM 的 HTTP Provider 引入独立限流的多凭证池，明确 Fetch 优先级，移除隐式限流，并通过 URL 状态机按需验证和复用正文。

**Architecture:** `docs/designs/architectures/20261125-credential-pool-fetch-state.md`

**Error handling:** `docs/designs/error-handlings/20261125-credential-pool-fetch-state.md`

**Testing:** `docs/designs/testings/20261125-credential-pool-fetch-state.md`

---

## 执行上下文

- 日期：2026-11-25。
- 工作树：`/home/agent/agent-search-gateway/.worktrees/design-credential-pool-fetch-state-20261125`。
- 分支：`design-credential-pool-fetch-state-20261125`。
- 规划基线：`7442a5a927e83e8180ed20b94a554a84584128f6`；该提交包含三份 spec，生产代码基线为 `8da7aeb`（v2.0.0）。
- 本文件最初提交为待执行计划；现按用户授权实施。复选框表示已完成的步骤，实际验证证据与剩余范围见文末“实施记录”。
- 已加载全局指令。主仓库与任务工作树根目录的 AGENTS.md 均不存在；任务工作树中未发现更深层 AGENTS.md。
- 复用指定工作树。实施期间只修改任务文件；不改写用户实际配置、不切换主仓库分支、不停止共享 daemon。
- 三份 spec 的评审阶段说明保留为历史文字；本计划以这些已提交的设计和用户当前的写计划请求为依据，不重新启动 brainstorming 或发布流程。

## 代码核对与实施原则

| 当前事实及定位（行号用于定位，实施时以符号为准） | 本次处理 |
|---|---|
| `config.py` 的 Web/Academic 默认并发为 3、LLM 为 2、Agent 为 1；`ProviderQuotaManager` 按 provider 管理限制 | 删除默认值与 HTTP provider 聚合 gate；HTTP 限制归每组 credential，Agent 限制归 alias。当前的 default_max_concurrency 是各 provider 的默认值，并不是一个全 Gateway semaphore。 |
| `runtime.py:_create_http_client` 的代理路径调用无参 factory，直连路径传 trust_env/SSL context | 两条路径都显式传无限活动连接数的 HTTPX limits；测试工厂同步迁移，禁止无参重试丢失 limits。 |
| `scheduler/fetch.py:fetch_until_accepted` 顺序选择当前可用 provider；原 lease 包住网络和 Judge | 增加 fetch_order；池内选凭证；网络结束释放 Web lease，随后独立 Judge。 |
| `orchestrators/search.py:_stage_keyword_hit` 对搜索自带正文执行 cheap_check/Judge；不是完整 Clean/Safety pipeline | Search 移除所有正文 stage，保留自然正文，存为 NEED_JUDGE。 |
| `url_store.py:admit/merge_body` 分别 first-nonempty 合并 raw/content；`FetchOrchestrator` 每次执行 Safety | 同源正文对原子提交，URLStatus 替代 available，READY 复用已完成的 Safety。 |
| `OpenAIChatCompletionsClient.complete_text/complete_json` 各自取 capacity；本地校验也可能抛 LLM_STAGE_FAILED | 容量归 pool；本地准备在选凭证前执行，防止本地输入错误被当成凭证失败。 |
| `Runtime.build` 同步构造且部分 client 在 adapter 构造成功后才进入关闭列表 | 改为可异步回滚的构造，client 一创建即登记唯一关闭责任。 |

### 固定边界

1. 一个逻辑 HTTP provider 一个共享 pool，每个 credential 一个独立 adapter/client/executor 及可选 gates。Web search/fetch、同一 LLM alias 的所有模型调用和正文 stages 共享各自的池。
2. 单次逻辑调用只选一组 credential 开始；其内部 HTTP retry、并行分支和已有 LLM protocol retry 不换组。最终 provider ExecutionFailure 才尝试下一组，本次每组至多一轮。正常空结果、OA no-match、有效 partial success 都是成功。
3. 不增加冷却、熔断、余额查询、跨请求失败历史、加权轮询、模型级池或 Agent 多账户。策略名称固定为 `round_robin` / `fill_first`。
4. 三种限制独立可选；没有配置就没有对应 gate，不用大整数模拟无限制。max_concurrency 仍计算逻辑 adapter operation，不是每条内部物理 HTTP 请求；rate 按每次真实 HTTP attempt 计算。
5. CLI、Socket、JSONL、逻辑 provider 身份、过滤、LLM model 和 Agent CLI 认证保持原合同。配置 schema 是有意的 breaking change，不保留双格式兼容。
6. 所有正文和状态更新经过 URLStore。只有 READY 正文能返回或送入 Focus Summary；Focus 不缓存，失败不降级返回全文。

### 统一类型与内部接口

下表是本计划的命名合同；实施可做局部重命名，但必须一次更新所有调用者、测试和后续任务记录，不同时维护两套 API。

| 位置 | 类型或方法 | 合同 |
|---|---|---|
| `config.py` | `CredentialStrategy = Literal["round_robin", "fill_first"]` | 默认 round_robin；不接受 fill_out 等别名。 |
| `config.py` | `ResolvedCredential` | frozen/slots；只读 effective options、api_key_env/api_key、contact_email_env/contact_email、max_concurrency: int \| None、rate_policy: ResolvedRatePolicy；认证值仍为 SecretValue，含 endpoint 的 options 不进入自动 repr。 |
| `config.py` | 四类现有 provider config | 保留逻辑 name、enable flags、use_proxy 和 LLM protocol/api_endpoint 等类别字段；新增 credentials 元组和 credential_strategy；端点、认证及 HTTP limits 只存在成员中。 |
| `config.py` | `ResolvedWebProviderGroup.fetch_order: tuple[str, ...]` | 原配置顺序中的逻辑 provider 名称；排序不作过滤。 |
| `credential_pool.py`（新） | `CredentialMember[A]` | adapter + capacity: CapacityGate \| None + rate: RateGate \| None；成员 index 来自不可变数组位置。 |
| `credential_pool.py`（新） | `CredentialReservation[A]` | index、member、可选 lease；所有权只转交一次，幂等释放；退出、交接取消都释放。 |
| `rate_limit.py` | `SelectionBudget` | remaining_seconds、waited_seconds；只累计选择与首次 permit 的实际阻塞等待，不含正在执行的 HTTP、Judge、backoff 或 Retry-After。每次逻辑调用/每个 scheduler 候选独立持有。 |
| `credential_pool.py`（新） | `try_reserve(attempted) / wait_available(attempted, budget) / invoke(operation, *, first=None, budget=None)` | try-reserve 原子选择并推进游标；wait 只通知/观察，不预占 lease、不扣 rate；invoke 消费首次 reservation，request-local attempted，不新增 retry loop。 |
| `providers/pooled.py`（新） | `PooledWebProvider / PooledAcademicProvider / PooledOAResolver / PooledLLMClient` | 四类薄 façade，保持现有方法签名和 name；调度算法只在 pool。Web 能力仍取 registry，不能因 façade 具有两个方法就把 search-only provider 当作 fetch-capable。 |
| `providers/pooled.py` | `try_reserve_fetch / wait_fetch_available / fetch_reserved` | 仅供 FetchScheduler 的内部协议；首次 reservation 直接交给 invoke，不再次选择凭证或取 capacity。 |
| `providers/llm/openai_chat.py` | `prepare_request(invocation, messages) -> PreparedInput`；`complete_text_prepared / complete_json_prepared` | 准备函数在 pool 外只运行一次；所选物理 client 用自己的 endpoint/key 发送同一 PreparedInput；直接 complete_* 入口仍先 prepare 再调用 prepared 入口。 |
| `runtime.py` | `async Runtime.build(...)` | 成功返回完整 Runtime；构造失败先完整回收再抛原错。生产 daemon await 它；既有同步 fake runtime_factory 注入继续可用。 |
| `models.py` | `URLStatus(StrEnum)`、五字段 URLRecord | NEED_FETCH、NEED_JUDGE、NEED_CLEAN、NEED_SAFETY、READY、UNAVAILABLE；status 替换 available。 |
| `url_store.py` | `admit(url, abstract)`、`serialization(url)`、`async admit_candidate(url, abstract, raw_content, content)` | metadata admission 保持同步；正文 admission 在同一 per-URL 锁内重读并成对写入。 |
| `url_store.py` | `accept_candidate / reject_candidate / set_clean_content / mark_ready / mark_unavailable` | 同步、原子、验证状态前置条件；Fetch 调用者已持 serialization 锁，方法内部不重入取锁。 |

预算实现复用现有 `initial_rate_budget / consume_initial_rate_budget` 的 task + gate 绑定，只扩展必要的 SelectionBudget 交接与记账。并行 availability waiter 不重复扣同一预算；首次 executor permit 的等待也只能记一次。新 spec 要求 capacity/rate 全部暂不可用时有选择等待预算，因此旧的“纯 capacity 等待永不耗选择预算”断言需要迁移；已开始的 operation 耗时仍不得变成新整体 deadline。

两个容易混淆的旧合同明确保留：

- Search 的 content-only 自然候选合法；Web `URLFetchCandidate` 仍执行 scheduler 原结构校验：空字符串 raw 是结构执行失败，空白 raw 是 cheap-check 语义拒绝。不要为 Search admission 放宽所有 vendor Fetch contract。
- 按 Testing §6.2 的具体约定保留 singleflight：follower 取消不影响 leader/其他 follower；leader 取消传播至同 key 的共享 future。不同 URL 不受影响。不要引入共享任务所有权转移、最后等待者计数或新的取消模型。Focus 保留在原 per-URL 串行范围内。

## 文件结构与责任

| 文件 | 责任 |
|---|---|
| `src/agent_search_gateway/config.py` | 共用 credential 值对象、严格新 schema、各类配置解析、迁移错误、可选 limits、fetch_order。 |
| `src/agent_search_gateway/providers/registry.py`、`providers/defaults.py`、`providers/academic/registry.py`、`providers/academic/defaults.py` | 在注册边界区分已支持的 endpoint/auth/业务参数；逐成员复用现有 validators；不引入任意 kwargs 透传。 |
| `src/agent_search_gateway/credential_pool.py`（新） | 选择、reservation、单轮 fallback、有限等待和取消清理。 |
| `src/agent_search_gateway/providers/pooled.py`（新）、`providers/contracts.py` | 四类 façade、内部 Fetch availability 协议；业务协议与资源所有权分开。 |
| `src/agent_search_gateway/concurrency.py`、`rate_limit.py` | 现有 capacity、rate、URL locks/singleflight；移除 HTTP provider 聚合 gate 用法，Agent 的可选 gate；共用等待预算。 |
| `src/agent_search_gateway/providers/http.py`、`providers/llm/openai_chat.py` | 物理 attempt 限速、首次预算交接、已有重试与 LLM 准备；不重复持容量。 |
| `src/agent_search_gateway/runtime.py`、`daemon.py` | 多成员装配、所有 secrets 提前注册、HTTPX limits、异步回滚和 close-once。 |
| `src/agent_search_gateway/providers/agent/antigravity_cli.py` | 仅 invocation 的可选 capacity/rate，保持 prompt、spawn、timeout/reap。 |
| `src/agent_search_gateway/scheduler/fetch.py` | 有序逻辑 provider、pool reservation、网络后 Judge、原 FetchOutcome。 |
| `src/agent_search_gateway/models.py`、`url_store.py` | 六状态及同源正文原子更新。 |
| `src/agent_search_gateway/orchestrators/search.py`、`orchestrators/paper.py`、`orchestrators/fetch.py` | 去重复 quota、Search 延迟正文处理、Fetch 状态恢复；学术聚合/OA 逻辑继续复用。 |
| `src/agent_search_gateway/providers/web/tavily.py`、`providers/web/exa.py` | 仅关闭主动正文请求；其他 Web adapters 逐一审计，保留已正确的代码。 |
| `src/agent_search_gateway/doctor.py`、`observability.py` | 逐凭证诊断、安全 identity、实际 attempt 与 URL 状态事件。 |
| `tests/runtime/test_credential_pool.py`、`tests/runtime/test_pooled_providers.py`、`tests/runtime/test_runtime_resources.py`（新） | 池行为、四类 façade、装配/回滚/连接池。优先复用现有 controlled/fakes，避免继续膨胀大测试文件。 |
| `tests/acceptance/test_credential_pool_fetch_state.py`（新） | 真实 Runtime + mock upstream + CLI/daemon/socket 的组合验收。 |
| 现有 `tests/unit`、`runtime`、`scheduler`、`orchestrators`、`providers`、`doctor`、`observability`、`docs`、`cli`、`daemon`、`support`、`integration` | 具体测试文件见各任务；旧 fixture 和 helper 同步迁移，不删除有价值的断言。 |
| `config.example.toml`、`config.youcom-free.fragment.toml`、`README.md`、`docs/public-interface.md`、`docs/provider-rate-limits.md`、`docs/youcom-free-mcp.md`、`CHANGELOG.md` | 当前生效的配置示例、迁移规则、可观察行为与 Unreleased；历史设计文件不批量改写。 |

## 执行顺序与 TDD 约定

- Tasks 1–4 固定配置合同；5–9 完成限流、pool 与 façade；10–13 完成装配、Agent 和 Fetch 调度；14–18 完成 URL 状态和 Search/Fetch；19–22 完成诊断、验收及文档；23 为最终检查。
- 配置和运行时是同一次 schema 切换，Tasks 1–13 构成集成检查点。每个任务同时迁移其触及的调用者与 fixture；不能把生产兼容旧 TOML 或自动转换旧测试配置当作过渡方案。组件 RED/GREEN 不等于整个改造已完成，Task 23 前不得声称全量通过。
- 每个场景都做独立小循环：一次一个失败测试，确认行为断言失败，最小实现，通过，然后整理。表中多场景不是要求先写全部实现。
- 新模块先给最小可导入接口声明；RED 必须是缺少行为导致的断言失败，不能把 ImportError、语法错误或错拼测试名当作 RED。原行为回归可直接为绿，不人为破坏。
- 各任务 Step 4 运行目标测试与所在组件的完整回归；跨组件尚未迁完的失败必须记录并在相应任务解决，不改成 skip。两个集成检查点为 Task 13、Task 21；最终全工程检查在 Task 23。
- 使用现有 pytest、pytest-asyncio、HTTPX MockTransport、ManualClock、asyncio.Event/barrier 和假 Agent CLI。测试观察真实组件的出站/结果/状态，不只验证 mock 被调用；不加只供测试使用的生产接口。
- 命令全部在目标工作树执行。实施开始按 CONTRIBUTING 执行 `uv sync --locked --all-groups`；保留 lockfile。全量测试显式去掉 `WEB_SEARCH_RUN_INTEGRATION`，防止继承终端开关。API 密钥只用合成环境值。
- 本轮计划无需实际 smoke。实施阶段用户已授权针对性 smoke，可使用隔离配置和 `--provider`；不要求重复授权既有范围。真实多凭证不足时用确定性 mock 验证切换，不制造真实 429/耗尽额度，不自动修改运行配置。

---

### Task 1: 定义统一 credential 与独立可选 limits

**Files:**

- Modify: `src/agent_search_gateway/config.py`（ResolvedRatePolicy、_resolve_rate_policy、配置 dataclasses 和字段校验 helpers）
- Test: `tests/unit/test_config_web_providers.py`
- Test: `tests/unit/test_config_academic_providers.py`
- Test: `tests/unit/test_config_llm.py`
- Test: `tests/unit/test_config_agent.py`

- [x] **Step 1: 写失败测试**

逐项参数化：三个 limits 均缺省、仅 capacity、仅 window、仅 interval、任意组合、空 windows。断言缺省 capacity 为 None、无 rate 规则，而不是 1/2/3。另写 `test_credential_strategy_defaults_and_rejects_invalid_values`，逐一断言缺省→round_robin、round_robin/fill_first 合法、fill_out/其他字符串/非字符串非法；四类 HTTP 入口在 Tasks 2–4 分别复用这组参数。再测 0/负数/bool/小数冒充整数、NaN/Infinity、未知窗口字段、错误容器。新增 `test_credential_limits_are_independently_optional`、`test_invalid_explicit_limits_report_field_path`，错误只给字段路径，不回显秘密或 endpoint。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py -k "credential_limits or invalid_explicit_limits or credential_strategy" -v
~~~

预期：旧数据模型或默认值与新断言不同；先修正测试导入问题，再观察具体行为失败。

- [x] **Step 3: 最小实现**

~~~text
定义 CredentialStrategy 与 frozen ResolvedCredential
_resolve_credential_strategy：缺省 round_robin；只接受两个合法字符串，其他值给字段路径错误
max_concurrency 缺失 -> None；存在 -> 复用严格正整数校验
rate_limits 缺失/[] -> 无窗口；interval 独立解析
保留 ResolvedRatePolicy，无限流时不从 registry/example 复制推荐值
effective options 只读且隐藏 repr；secret 使用 SecretValue
~~~

新增共用 `_resolve_credential_strategy` 与 limits parser 供 Tasks 2–4 调用；本任务先验证这些公共 helpers/值对象，各类别入口在对应任务切换，不先增加兼容旧 schema 的入口。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py -k "credential_limits or invalid_explicit_limits or credential_strategy" -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py -q
~~~

预期：新公共校验的目标测试通过；各类别的有意 schema 变化随 Tasks 2–4 更新，而不是放松数值校验。

- [x] **Step 5: 整理并保持 GREEN**

提取重复字段路径与 optional numeric 校验，避免各类别复制策略/限流默认值；重跑上列组件命令。

### Task 2: Web credentials、专属选项与 fetch_order

**Files:**

- Modify: `src/agent_search_gateway/config.py`（_resolve_one_web_provider、resolve_web_provider_config）
- Modify: `src/agent_search_gateway/providers/registry.py`
- Modify: `src/agent_search_gateway/providers/defaults.py`
- Test: `tests/unit/test_config_web_providers.py`
- Test: `tests/providers/test_registry.py`

- [x] **Step 1: 写失败测试**

`test_web_credentials_preserve_order_and_validate_every_member` 使用两个不同 endpoint/env；第二项无效必须整体失败。覆盖单项、无 key 的 Jina/open-webSearch、You.com REST 必需 key、官方免费 MCP 禁止 key、TinyFish 双端点、BrightData zone 公共值与成员覆盖。对 You.com 再测 REST 无 key、官方免费 MCP 带 key 这两种反向组合必须拒绝，每个成员独立调用原 validator。provider 业务参数保留原层级，credential 不接受任意参数。各 Web 策略输入按 Task 1 的缺省/合法/非法矩阵参数化，不仅测试已构造 pool。

`test_fetch_order_prioritizes_then_appends_enabled_providers` 覆盖缺省、空、完整、部分排序、合法未启用项；重复/未知/search-only/非字符串/非数组报错。另测旧顶层 endpoint/auth/limits、default_max_concurrency 及新旧混用全部明确报迁移错误。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py -k "credentials or fetch_order or legacy or credential_strategy" -v
~~~

预期：当前解析器不识别 credentials/fetch_order 或仍接受旧字段。

- [x] **Step 3: 最小实现**

~~~text
先识别 web group 的 fetch_order 保留键，不把它当 provider
依据 registration 分离 provider 业务选项与 credential endpoint/auth/limits
provider 的 strategy 用共用 _resolve_credential_strategy 解析
启用项要求非空有序 credentials；每项解析 env，合成 effective options
只允许已注册 endpoint；TinyFish 双 URL；BrightData 仅 zone 可覆盖
逐项调用原 registration.validate_config 与已有专属校验
解析 fetch_order，核对 registry capability；保留声明顺序供 Runtime 排列
任何已删除字段出现 -> 迁移错误，不自动搬运
~~~

保留既有 endpoint 默认与认证约束，不发明 base_url 字段。禁用项不解析真实 env/建池；仍拒绝明示的已删除旧字段，其他禁用校验遵循现有边界。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py tests/providers/test_registry.py -v
~~~

预期：每组配置独立验证，fetch_order 不改变 enable_search/enable_fetch 或搜索列表。

- [x] **Step 5: 整理并保持 GREEN**

复用 registry allowlist/validator，不新增 vendor 认证调度；删除 Web 旧 default_max_concurrency 字段与默认值。重跑上列命令。

### Task 3: Academic 与 OA 的统一 credentials

**Files:**

- Modify: `src/agent_search_gateway/config.py`（resolve_academic_provider_config、resolve_oa_resolver_config）
- Modify: `src/agent_search_gateway/providers/academic/registry.py`
- Modify: `src/agent_search_gateway/providers/academic/defaults.py`
- Test: `tests/unit/test_config_academic_providers.py`
- Test: `tests/providers/academic/test_registry_defaults.py`

- [x] **Step 1: 写失败测试**

`test_academic_and_oa_validate_all_credentials` 覆盖 arXiv 无认证、OpenAlex 必需 key/可选 email、其他既有 optional auth、Unpaywall 必需 contact email。一个 OA resolver 两组凭证有效；两个 enabled resolver 仍失败。第二项缺 env 或无效 options 不可被第一项掩盖；禁用项不要求环境值。旧格式/default_max_concurrency 显式拒绝。Academic 和 OA 各用最小合法配置参数化 Task 1 的 strategy 矩阵，非法值返回各自完整配置路径。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_academic_providers.py -k "credentials or legacy or optional_limits or credential_strategy" -v
~~~

预期：旧单凭证结构/default=3 与新断言不同。

- [x] **Step 3: 最小实现**

~~~text
保留 Academic/OA registration required/optional/none 认证规则
各逻辑 provider 共用 _resolve_credential_strategy；每 credential 分别解析 api_key_env/contact_email_env 和 effective options
arXiv 搜索业务参数留在 provider，原 query validator 逐 effective config 复用
OA 仍仅允许一个 enabled resolver；该 resolver 内可以多个 credential
删除 Academic 默认并发；OA 的 optional capacity 放入 credential
~~~

不改变论文聚合、DOI 规范化或 OA best-effort 边界。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_academic_providers.py tests/providers/academic/test_registry_defaults.py -v
~~~

预期：所有认证模式和旧业务字段校验通过，缺省无限流。

- [x] **Step 5: 整理并保持 GREEN**

用同一个 credential/limits parser，保留各 registry 的厂商差异；同步更新直接位置参数构造的 resolved dataclass 测试，重跑上列命令。

### Task 4: LLM 与 Agent 配置移除默认限制

**Files:**

- Modify: `src/agent_search_gateway/config.py`（_resolve_referenced_llm_providers、resolve_agent_config、ResolvedLLMConfig、AgentProviderConfig）
- Test: `tests/unit/test_config_llm.py`
- Test: `tests/unit/test_config_agent.py`
- Test: `tests/unit/test_search_request_selectors.py`

- [x] **Step 1: 写失败测试**

`test_referenced_llm_alias_resolves_ordered_credentials` 保留 alias/protocol/api_endpoint/model，验证各成员独立 endpoint/key/limits；未引用 alias 不读取 env，已引用空 credentials 失败。`test_agent_limits_are_optional_without_credentials` 测 Agent 无配置不限流、有配置严格校验，拒绝 HTTP credentials/strategy。覆盖 LLM/Agent 各处 default_max_concurrency 和 HTTP alias 顶层旧字段拒绝；原模型与 provider 过滤不变。LLM alias 同样参数化共用 strategy 的缺省、两个合法值及非法字符串/类型；Agent 明确拒绝 strategy。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_llm.py tests/unit/test_config_agent.py -k "credentials or optional or legacy or credential_strategy" -v
~~~

预期：LLM 旧 secret/api_url 结构、默认 cap=2 和 Agent cap=1 暴露差异。

- [x] **Step 3: 最小实现**

~~~text
LLM provider 保留协议/endpoint类型，模型在原 invocation 层
每个被引用 alias 解析 credentials，并调用共用 _resolve_credential_strategy
同 alias 全部 invocation 引用同一 config，不创建 model 级池
Agent 保留 harness/command/pass_env/timeout，新增 rate_policy
Agent max_concurrency 缺省 None，禁止 HTTP pool 字段
删除所有类别 default_max_concurrency，不做旧配置转换
~~~

更新 direct dataclass 构造测试；只改本次改变的内部结构，不兼容旧位置参数。

- [x] **Step 4: 验证 GREEN 与配置检查点**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py tests/unit/test_search_request_selectors.py -v
~~~

预期：四类 HTTP/Agent 的配置合同全部通过；业务过滤、输入限制和 RetryPolicy 未变。

- [x] **Step 5: 整理并保持 GREEN**

检查配置定义及 parser 中不再有隐式 1/2/3 或 fallback default cap；保留它们仅作为“旧字段拒绝”的测试输入和迁移说明。重跑上列命令。

### Task 5: 可选 gate 与共用选择等待预算

**Files:**

- Modify: `src/agent_search_gateway/concurrency.py`（CapacityGate/CapacityLease、ProviderQuotaManager 的迁移边界）
- Modify: `src/agent_search_gateway/rate_limit.py`
- Test: `tests/runtime/test_quota_manager.py`
- Test: `tests/runtime/test_rate_gate.py`
- Read: `tests/support/controlled.py`

- [x] **Step 1: 写失败测试**

`test_omitted_capacity_creates_no_waiting_gate`、`test_selection_budget_is_not_reset_by_notifications`、`test_first_permit_charges_shared_budget_once`。用 ManualClock/Event 测 capacity-only、rate-only、二者同时、仅 interval、空窗口，确保 peek 不消费许可，真实发送失败不退回已消费许可。预算为 0 时允许当下已就绪的一次检查；超期迟唤醒不额外放行。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_quota_manager.py tests/runtime/test_rate_gate.py -k "omitted or selection_budget or first_permit" -v
~~~

预期：旧 quota 必须有 finite limit，或现有 immutable 初次预算尚无跨层一次记账。

- [x] **Step 3: 最小实现**

~~~text
未配置 capacity -> None；未配置 windows/interval -> None
复用 CapacityGate.try_lease/release 与 RateGate.next_available_in_seconds/permit
SelectionBudget 记录实际等待，单个协调者在一次 wait 的 finally 记账
initial_rate_budget 继续绑定当前 task 与精确 gate
consume 只交接一次，executor 首次 permit 等待/超时回报该预算一次
独立候选/调用使用独立 budget；HTTP retry/backoff/Judge 不计入选择预算
~~~

仅保留 Agent 所需的 alias gate 管理；HTTP provider 旧聚合 gate 的调用者在 Tasks 9/13 全部移除。不修改 PerKeyLockPool 或 SingleflightGroup 算法。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_quota_manager.py tests/runtime/test_rate_gate.py tests/runtime/test_singleflight.py -v
~~~

预期：许可计数、可选语义、精确边界均通过，URL lock/singleflight 回归保持。

- [x] **Step 5: 整理并保持 GREEN**

不要创建“无限 semaphore”或对无规则成员发空轮询通知；避免 rate_limit 导入 credential_pool 造成循环依赖。重跑上列命令。

### Task 6: CredentialPool 选择与 reservation

**Files:**

- Create: `src/agent_search_gateway/credential_pool.py`
- Create: `tests/runtime/test_credential_pool.py`
- Test support: `tests/support/controlled.py`（仅需新受控行为时扩展）

- [x] **Step 1: 写失败测试**

分别写 `test_round_robin_advances_at_selection`、`test_round_robin_keeps_array_position_when_member_is_blocked`、`test_fill_first_returns_to_first_ready_member`、`test_unlimited_128_operations_enter_before_release`。A/B/C 用 Event 阻塞完成；断言 RR A/B/C/A，完成逆序不改变下一选择；FF A 常可用时连续 A，A 受限时选 B，恢复后再 A。128 个独立工作在任何一个释放前全部进入无 gate 成员。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_credential_pool.py -k "round_robin or fill_first or unlimited_128" -v
~~~

预期：最小接口尚不能保证选择顺序、容量原子性或真正无限制；不是模块不存在的 collection error。

- [x] **Step 3: 最小实现**

~~~text
pool 持固定成员 tuple、strategy、共享 RR cursor、短选择锁
try_reserve:
  按固定 index 的 RR 或从 0 开始的 FF 顺序扫描
  跳过 request-local attempted、rate 当前不可用、capacity 无槽位
  原子取得可选 lease；选择时推进 RR cursor；返回 reservation
  无 ready -> None
reservation.release 幂等；短临界区不等待网络/permit
~~~

Pool 不读 URLStore、认证格式或 vendor 响应；只接收类型化 operation。安全 identity 为 namespace/name/index，index 不变成逻辑 provider 名。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_credential_pool.py -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_quota_manager.py tests/runtime/test_rate_gate.py tests/runtime/test_credential_pool.py -q
~~~

预期：无隐藏 cap、各成员 capacity/rate 独立、游标不受完成顺序影响。

- [x] **Step 5: 整理并保持 GREEN**

仅保留实际需要的 member/reservation 泛型，避免动态修改共享 adapter 的 URL/header；重跑上列命令。

### Task 7: 单轮 fallback、有界等待与取消清理

**Files:**

- Modify: `src/agent_search_gateway/credential_pool.py`
- Modify: `src/agent_search_gateway/rate_limit.py`（仅补预算交接所需行为）
- Test: `tests/runtime/test_credential_pool.py`
- Read: `src/agent_search_gateway/retry.py`

- [x] **Step 1: 写失败测试**

逐一验证：

| 建议测试名 | 设置/行动/断言 |
|---|---|
| `test_terminal_execution_failure_tries_each_member_once` | A/B 最终失败、C 成功；每组 operation 一次，成功立即止步；全失败无第二轮。 |
| `test_empty_result_and_no_match_do_not_fallback` | 返回 []、None 或有效 partial result；下一成员零调用。 |
| `test_non_provider_failure_does_not_fallback` | InputFailure、ConfigFailure、取消、程序异常直接传播；不得用宽泛 except Exception 吞掉。 |
| `test_failed_member_has_no_memory_in_next_call` | X 中 A 失败，新调用 Y 仍可按策略选 A；并发调用各有 attempted。 |
| `test_waits_for_unattempted_ready_member_without_spinning` | 全受限等待；rate 已耗尽但 capacity 空闲不能形成立即唤醒循环；反复通知仍超时。 |
| `test_cancelled_selection_or_handoff_releases_every_lease` | 在等待、reserve 后/operation 前、HTTP 后返回前注入取消；不再启动 fallback，后续调用可用。 |

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_credential_pool.py -k "terminal or no_match or non_provider or memory or waits or cancelled" -v
~~~

预期：缺少单轮 attempted、等待预算或交接清理时，观察多轮调用、错误切换或 lease 泄漏。

- [x] **Step 3: 最小实现**

~~~text
invoke 创建一次 attempted 和 SelectionBudget，或接收 scheduler 提供的 first/budget
循环:
  有 first -> 原样消费，不再选择；否则 try_reserve
  无未尝试项 -> 以原逻辑 provider 错误结束
  有未尝试但均 blocked -> wait_available 后重检，预算不重置
  attempted.add(index)
  try: await operation(member.adapter)；成功立即 return
  except ExecutionFailure: 留存安全错误，允许下一成员
  finally: release reservation
wait_available:
  注册 waiter 前先非消费重检；已有 ready 成员立即返回，不预占、不推进游标
  仅为 rate-ready/capacity-full 建 capacity waiter
  为 rate-blocked 建最早 timer，并始终有预算超时
  所有 loser waiter 在 finally cancel 且 await，通知后重新按策略选择
~~~

保留最后的 typed failure/error code；无成员执行但选择超时时，用 façade/Runtime 提供的既有错误类别构造安全错误（LLM_STAGE_FAILED 或 ALL_PROVIDERS_FAILED），不新增公开 credential code。既有 RetryPolicy 继续在 adapter 内，pool 不调用 retry_async 再套一轮。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_credential_pool.py tests/runtime/test_rate_gate.py tests/unit/test_retry.py -v
~~~

预期：有限遍历、虚假唤醒不重置、取消立即传播、所有 waiter/lease 回收。

- [x] **Step 5: 整理并保持 GREEN**

检查释放路径不因日志异常或正常 return 漏掉；等待不持选择锁，取消不记录为一次可恢复凭证故障。重跑上列命令。

### Task 8: 物理 HTTP 限速、LLM 池外准备与原重试

**Files:**

- Modify: `src/agent_search_gateway/providers/http.py`（_request_response、_wait_for_rate）
- Modify: `src/agent_search_gateway/providers/llm/openai_chat.py`（complete_*、_prepare_body、_request_once）
- Test: `tests/providers/test_http_executor.py`
- Test: `tests/providers/test_bounded_http_executor.py`
- Test: `tests/providers/llm/test_openai_chat.py`
- Test: `tests/runtime/test_credential_pool.py`

- [x] **Step 1: 写失败测试**

`test_http_retries_stay_on_member_before_fallback` 通过真实 executor + MockTransport 观察 A/A/A/B，而非 mock 自带的重试。401/402/403 保持原一次请求后失败；408/429/5xx、网络错误与 Retry-After 维持原次数/时序。内部并行请求各自计 rate。

`test_llm_prepare_failure_sends_no_http` 覆盖 alias mismatch、reserved extra_body 和 input_budget_unfit，先在真实物理 client/prepare_request 验证原 LLM_STAGE_FAILED、零 send；真实 façade 的零 cursor 推进断言放 Task 9。`test_llm_retry_reuses_prepared_body_with_selected_auth` 验证 HTTP/protocol retry 同凭证、切换后的 body 字节/model 一致，而 endpoint/auth 独立。本任务验证 prepared 入口本身不再获取容量；cap=1 的真实 façade+pool 防死锁验收放 Task 9。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/llm/test_openai_chat.py -k "prepare_failure or prepared_body" -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/test_http_executor.py tests/runtime/test_credential_pool.py -k "member_before_fallback or initial_rate or retry_after" -v
~~~

预期：旧 LLM client 重复持容量/准备发生在成员内，或首次 rate 预算被重新赋满。

- [x] **Step 3: 最小实现**

~~~text
_prepare_body -> prepare_request，仍用真实 HTTPX JSON 序列化和既有输入裁剪
提取 complete_text_prepared/complete_json_prepared，保留各自协议 retry
直接 complete_*：prepare 一次 -> prepared 入口
prepared 入口不持 pool 的 capacity；_request_once 用当前实例 endpoint/secret
每个 HTTP attempt 发送前仅在该成员 RateGate.permit 处消费许可
首次匹配的 task/gate 消费 selection budget；其他 retry 保留原 per-attempt 预算
本地 rate timeout 无 HTTP send/伪造 attempt；已发 attempt 不退回 rate
~~~

不将 Search markdown parser、LLMStages 的决策解析或 input error 包进凭证 fallback。输入正文 cap、UTF-8 截断和 DEBUG body preview 保持原路径。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/test_http_executor.py tests/providers/test_bounded_http_executor.py tests/providers/llm/test_openai_chat.py tests/runtime/test_credential_pool.py tests/unit/test_retry.py tests/unit/test_input_limits.py -v
~~~

预期：发送顺序/速率可证明，所有原重试与输入上限断言通过。使用假时间，禁止真实长睡眠或真实额度故障。

- [x] **Step 5: 整理并保持 GREEN**

保留原 PreparedInput，避免新增请求 envelope 或第二种 JSON 序列化。初次预算保持 owner-task 隔离，不能为“修好”测试取消该保护；重跑上列命令。

### Task 9: 四类 typed façade 与统一逻辑身份

**Files:**

- Create: `src/agent_search_gateway/providers/pooled.py`
- Modify: `src/agent_search_gateway/providers/contracts.py`
- Modify: `src/agent_search_gateway/orchestrators/search.py`（_run_keyword_pipeline 的旧外层 quota）
- Modify: `src/agent_search_gateway/orchestrators/paper.py`（_run_provider 的旧外层 quota）
- Create: `tests/runtime/test_pooled_providers.py`
- Test: `tests/orchestrators/test_keyword_provider_filtering.py`
- Test: `tests/orchestrators/test_paper_provider_filtering.py`
- Test: `tests/orchestrators/test_llm_provider_model_filtering.py`

- [x] **Step 1: 写失败测试**

`test_web_search_and_fetch_share_one_pool` 用同一成员 Search 占满 capacity/rate，Fetch 只能选其他可用成员；无 provider 聚合 gate。`test_typed_facades_preserve_success_and_failure_contracts` 参数化 Web/Academic/OA/LLM，正常空值不切换；逻辑 name 不含 index。验证 LLM Search 与 judge/clean/safety/focus 用同 alias pool，model/filter 不变。新增 `test_llm_prepare_failure_never_selects_credential`，把 Task 8 的本地准备失败接到真实 PooledLLMClient；失败后的下一次合法调用仍选 A，证明零游标推进。再以 cap=1 验证 client+pool 不双重获取容量。prepared 方法明确接收 invocation、messages、prepared 三个参数，保留原模型/消息统计日志。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_pooled_providers.py -v
~~~

预期：缺少共享池、薄协议适配或操作层 lease 迁移时，观察独立计数/重复取容量等行为失败。

- [x] **Step 3: 最小实现**

~~~text
Web.search/fetch -> 同一个 pool.invoke(对应已验证的 adapter 方法)
Academic.search、OA.resolve -> 各自 pool.invoke
LLM.complete_*:
  prepared = alias 级 prepare_request（池外）
  pool.invoke(member.complete_*_prepared(..., prepared))
Web 内部 availability 协议转发同一 pool；fetch_reserved 接受既有 reservation
去掉 keyword/paper 的外层 HTTP provider lease，保留 fan-out/错误聚合
~~~

Web factory 返回 object 的现状在 Runtime 边界按 registry capability 检查并收窄到原 Protocol；不要用通用 __getattr__ 转发来逃避类型检查。façade 同时有 search/fetch 方法不意味着两种 capability 都启用。

运行时消费的 LLMClient 协议只保留业务方法；物理 client 直接使用时仍可 aclose。运行时物理资源关闭归 Task 10，façade 不另拥有关闭路径。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_pooled_providers.py tests/runtime/test_credential_pool.py -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_paper_provider_filtering.py tests/orchestrators/test_llm_provider_model_filtering.py tests/orchestrators/test_llm_invocation_selection.py -q
~~~

预期：默认每逻辑 provider 一次 Search，不按 credential fan-out；过滤/模型顺序不变。相关 fake/constructor 在此同步迁移。

- [x] **Step 5: 整理并保持 GREEN**

公共 pool 只保留一套算法，四类 façade 不各自实现 retry/轮询。去掉不再使用的 HTTP quota 依赖，保留 Agent 独立限制入口；重跑上列命令。

### Task 10: 公共 HTTP factory 与资源所有权基础

**Files:**

- Modify: `src/agent_search_gateway/runtime.py`（_create_http_client、私有资源 owner）
- Create: `tests/runtime/test_runtime_resources.py`
- Read: `tests/runtime/test_provider_proxy.py`、`src/agent_search_gateway/daemon.py`

- [x] **Step 1: 写失败测试**

此步仅测不依赖旧 builder 的公共 factory 和私有资源 owner；实际 Runtime/多成员/bind 回滚在 Task 11。

`test_http_client_factory_has_unlimited_active_connections` 直接调用公共 factory，捕获代理/直连的实际 kwargs，两条路径都必须有 max_connections=None、max_keepalive_connections=20。再检查真实构造 HTTPX transport 的活动连接配置（或受控 localhost 连接），不能仅凭 MockTransport 高并发作证。

`test_resource_owner_closes_registered_clients_once` 注册两个不同 client 后关闭两次，实际各 close 一次；`test_resource_owner_continues_after_close_failure` 让其中一个关闭失败，其他仍回收且日志安全。资源 owner 是 Task 11 生产装配将直接使用的组件，不是测试专用接口。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_resources.py -k "http_client_factory or resource_owner" -v
~~~

预期：旧 factory 没有 limits，或尚无统一 close-once 资源所有权。不会尝试用尚未迁移的 resolved config 调用旧 Runtime builder。

- [x] **Step 3: 最小实现**

~~~text
公共 HTTP factory 始终传 limits(None, 20)
proxy: 保留原 trust_env 环境行为；direct: trust_env=False、原 SSLContext
在 runtime.py 内定义最小私有资源 owner（如 _RuntimeResources）：
  client 创建后立即 register，先于 executor/adapter 构造
  aclose 幂等，按唯一物理 client 逐一关闭
  一项清理失败仍处理其余项，记录安全诊断
~~~

资源所有权只管理真实 client，不另为 façade 建关闭链。Task 11 将此 owner 接到异步构造、成功 Runtime 和异常回滚；本步不声称实际 Runtime/bind 清理已验证。

- [x] **Step 4: 验证 GREEN 与基础组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_resources.py -k "http_client_factory or resource_owner" -v
~~~

预期：公共 factory 的代理/TLS/limits 参数和独立 owner 的 close-once 通过。既有基于 Runtime.build 的完整 proxy/daemon 测试待 Task 11 接入并在 Task 13 集成检查点运行。

- [x] **Step 5: 整理并保持 GREEN**

只保留 register/aclose 所需状态；不添加全局 client 池、后台探测或业务 retry。重跑上列命令。记录对真实 Runtime 的待接入项，不把基础 owner 通过等同于构造失败回滚通过。

### Task 11: 多凭证 Runtime 装配、异步启动与失败回滚

**Files:**

- Modify: `src/agent_search_gateway/runtime.py`（五个现有 builder、Runtime.build、aclose）
- Modify: `src/agent_search_gateway/daemon.py`（start、_create_runtime、secret 注册、启动失败清理）
- Modify: `src/agent_search_gateway/scheduler/fetch.py`（仅构造/类型接线；选择行为在 Task 13）
- Test: `tests/runtime/test_runtime_assembly.py`
- Test: `tests/runtime/test_academic_runtime_assembly.py`
- Test: `tests/runtime/test_new_web_runtime_assembly.py`
- Test: `tests/runtime/test_optional_credentials.py`
- Test: `tests/runtime/test_request_filtering.py`
- Test: `tests/runtime/test_runtime_resources.py`
- Test: `tests/runtime/test_provider_proxy.py`
- Test: `tests/daemon/test_daemon_shutdown.py`
- Test: `tests/daemon/test_daemon_debug.py`
- Test support: `tests/runtime/conftest.py`

- [x] **Step 1: 写失败测试**

`test_runtime_builds_one_pool_per_logical_provider` 检查单/多/无 key 皆走同一流程；各成员独立 client/adapter/gates；Web Search/Fetch 共池，LLM alias 各 stage 共池，不同 namespace/alias 不混用。通过 façade 的真实 MockTransport 出站捕获各自 endpoint/auth，BrightData zone、TinyFish/You.com/Academic/OA 特例正确；不在本步调用尚未接完的 FetchScheduler 行为。

`test_runtime_sorts_fetch_only_without_changing_search_order` 验证构造出的 fetch façade 序列为显式优先项加 TOML 声明顺序，搜索列表不变。

在 Task 10 的基础测试之外新增 `test_partial_build_closes_each_allocated_client_once`：第二成员 adapter/factory、后续类别 factory 构造失败，所有已分配资源恰好关闭一次。再测 `test_cleanup_failure_preserves_original_startup_error`、`test_bind_failure_closes_all_members_once`、重复 aclose 和构造取消。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_assembly.py tests/runtime/test_academic_runtime_assembly.py -k "one_pool or credential or sorts_fetch" -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_resources.py -k "partial_build or startup_error or bind_failure" -v
~~~

预期：旧 builder 不能消费新 schema/创建各成员，或实际 Runtime 尚未接入 owner；基础 owner 测试通过不能代替本步行为证据。

- [x] **Step 3: 最小实现**

~~~text
Runtime.build 改 async，创建 Task 10 的资源 owner
resolve_config 成功后、创建资源前，向 debug redactor 登记全部成员 key/email
for 每个 active/referenced HTTP provider:
  for 每个 credential:
    可选 gates -> 公共 factory 创建 client 并立即 register -> executor -> 原 factory
    factory 只接收已验证 effective options 与显式认证参数
    按 registry capability 收窄类型，保存 CredentialMember
  建一个 pool（strategy、逻辑安全身份、原错误类别）及 typed façade
LLM alias 注入同协议/同 input limit 的 prepare_request
排序 fetch façades；构造所需的新 façade 签名/类型接线在此迁移
成功：资源 owner 移交 Runtime；异常/取消：先清理全部，再传播原错
Runtime.aclose 只委托唯一 owner，façade 不重复 close
daemon.start await _create_runtime；生产 await Runtime.build
既有同步 fake runtime_factory 注入仍直接返回 RuntimeLike
~~~

Web factory 使用 secret，Academic/OA 使用 api_key/contact_email；禁止 `**asdict(credential)`。正常关闭、构造中途失败、socket bind 失败均走同一物理资源关闭路径。清理一项失败不阻止其他项，不能覆盖主错。

HTTPX 参数由公共 factory 始终传递；同步 build 调用者改 await，测试 factory 接受 kwargs，禁止 TypeError 后回退无参调用。保留相同 URLStore、LLMStages、invocations/aggregator/writer 的装配关系。

本任务完成 HTTP façade/资源/启动接线；Agent 可选 invocation 行为在 Task 12、Scheduler 选择行为在 Task 13。不得为过渡恢复旧 HTTP provider 聚合 gate 或旧 TOML。受这两个后置行为影响的完整 runtime 测试在 Task 13 才作为全绿门槛。

- [x] **Step 4: 验证 GREEN 与本步 HTTP/资源目标**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_assembly.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_new_web_runtime_assembly.py tests/runtime/test_optional_credentials.py -k "one_pool or credential or sorts_fetch" -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_resources.py -v
~~~

预期：HTTP 实例/认证绑定、已排序 façade、正常/重复/部分构造/bind 关闭和主错保留通过。完整 `tests/runtime`、proxy 与 daemon 回归由 Task 13 接完 Agent/Scheduler 后执行；提前运行可作为诊断，但不能把依赖后续任务的失败删掉或标为通过。

- [x] **Step 5: 整理并保持 GREEN**

同步迁移 direct dataclass、旧同步 build、零参数 HTTP factory 和相关 fake。观察成员行为代替旧 `.quotas.get_*` 内省；记录 Task 12/13 的剩余集成依赖。重跑本步目标命令。

### Task 12: Agent 的可选 invocation 限流

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`（run_antigravity）
- Modify: `src/agent_search_gateway/runtime.py`（_build_agent_runners）
- Modify: `src/agent_search_gateway/concurrency.py`（Agent alias 可选 gate 管理）
- Test: `tests/providers/agent/test_antigravity_cli.py`
- Test: `tests/providers/agent/test_antigravity_lifecycle.py`
- Test: `tests/runtime/test_agent_runtime_assembly.py`
- Test: `tests/daemon/test_agent_shutdown.py`
- Test: `tests/unit/test_agent_stages.py`

- [x] **Step 1: 写失败测试**

`test_agent_without_limits_starts_independent_invocations`；`test_agent_rate_counts_invocation_start` 用假 CLI/spawn 边界和假时间，分别验证 alias capacity、window、interval。输入准备失败不占许可；spawn/timeout/取消都释放容量并保留原 reap。rate 等待超时不得误报 subprocess timeout，不切换 HTTP credential。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/agent/test_antigravity_cli.py tests/providers/agent/test_antigravity_lifecycle.py -k "without_limits or rate_counts or rate_wait" -v
~~~

预期：旧默认 cap=1 或缺少 invocation rate gate。

- [x] **Step 3: 最小实现**

~~~text
先验证 invocation/prompt 并沿用原输入裁剪
按 alias 获取可选 capacity
在 spawn 前获取可选 rate permit；无规则直接继续
再进入原 CLI execution timeout/shielded spawn/communicate/reap
finally 释放可选容量
rate wait failure 在 subprocess TimeoutError handler 外转换为原 Agent stage 错误
~~~

不同 model 共享同 alias gates；不统计 CLI 内部 HTTP。pass_env、认证、命令和 hard timeout 的计时起点保持原合同。

- [x] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/agent tests/runtime/test_agent_runtime_assembly.py tests/daemon/test_agent_shutdown.py tests/unit/test_agent_stages.py -v
~~~

预期：无限制和显式规则均可证明，所有 subprocess 生命周期及输入上限回归通过。

- [x] **Step 5: 整理并保持 GREEN**

复用现有 gate/错误类别，避免在 Agent runner 中增加 pool、账户或额外 retry；重跑上列命令。

### Task 13: FetchScheduler 按逻辑顺序预留可用池

**Files:**

- Modify: `src/agent_search_gateway/scheduler/fetch.py`（fetch_until_accepted、_select_available_provider、_attempt）
- Modify: `src/agent_search_gateway/providers/pooled.py`（Fetch availability 协议实现）
- Test: `tests/scheduler/test_fetch_capacity.py`
- Test: `tests/scheduler/test_fetch_outcomes.py`
- Test support: `tests/support/fakes.py`、`tests/support/controlled.py`

- [x] **Step 1: 写失败测试**

| 建议测试 | 可观察结果 |
|---|---|
| `test_fetch_uses_first_logical_provider_with_ready_credential` | A 全受限、B ready 时先 B；A 仍有一个 ready credential 则先 A；同 URL 不竞速。 |
| `test_fetch_consumes_first_reservation_without_reselection` | try-reserve 选 A1 后实际发送仍为 A1，不多推进一次 RR、不再取 capacity。 |
| `test_credential_fallback_finishes_before_next_provider` | A1 重试耗尽→A2 成功并通过 Judge，B 零调用；A 全失败才到 B。 |
| `test_rejected_body_skips_other_credentials_in_same_provider` | A1 正文被拒→B，不尝试 A2；Judge 执行异常直接向外。 |
| `test_fetch_releases_web_credential_before_judge` | Judge 被 Event 阻塞时另一 Web 工作可以取得原容量。 |
| `test_first_permit_uses_remaining_selection_budget` | 预等待后许可被竞争者抢走，executor 用剩余预算，无双扣/预算重置。 |

保留纯执行失败、实际语义失败优先的混合终止、候选结构与 cheap_check 区别；补全 capacity-only 选择等待的有界新语义。

- [x] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/scheduler/test_fetch_capacity.py tests/scheduler/test_fetch_outcomes.py -k "credential or reservation or before_judge or remaining_selection" -v
~~~

预期：旧 scheduler 仍只观察逻辑 provider gate、Judge 占用 Web lease 或重新选成员。

- [x] **Step 3: 最小实现**

~~~text
保持 request-local 逻辑 provider 候选与 semantic_failure_seen
按已排序 façade 逐个 try_reserve_fetch
全部暂不可用 -> race 各候选的非消费 wait_fetch_available，之后按优先次序重检
每候选有自己的 SelectionBudget；并行 waiter 不重复扣同一对象
候选预算耗尽 -> 记入原 FetchOutcome.failures 的执行失败，移出候选后继续扫描
同刻某候选到期就绪、另一候选超时 -> 允许就绪候选的一次重检，不提前整体失败
选定 -> 同 task await fetch_reserved(first reservation, budget)
网络 operation 已释放 lease -> 原结构校验、cheap_check、Judge
候选拒绝 -> 下个逻辑 provider；Judge exception/cancel -> 直接抛出
最终：有实际 semantic rejection -> semantic_failure，否则 execution_failure
~~~

不要 `create_task(provider.fetch(...))` 破坏 initial_rate_budget 的 task 绑定。Scheduler 退出时取消并 await 所有 loser waiter。不能把缓存候选的先前拒绝塞进 scheduler.semantic_failure_seen。知乎/百度千帆 Search 的子任务各自取得物理 permit；保留初次预算的 owner 检查，不为这些分支放宽 task 隔离。

- [x] **Step 4: 验证 GREEN 与第一个集成检查点**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/scheduler tests/runtime tests/providers tests/daemon tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py -v
~~~

预期：新配置到物理 HTTP/fetch 调度全链路通过。迁移 slow logging、late wake、精确截止、竞争、cancel/join 等既有测试；只修改 spec 明确改变的等待计费和 gate 所有权断言。

- [x] **Step 5: 整理并保持 GREEN**

删除 scheduler 中旧 provider gate 获取和重复 rate 预扣；不改变 FetchOutcome 的公开分类或 Judge 错误因果。重跑 scheduler 与运行时组件命令。

### Task 14: 五字段 URLRecord 与原子状态更新

**Files:**

- Modify: `src/agent_search_gateway/models.py`（URLRecord）
- Modify: `src/agent_search_gateway/url_store.py`（admit、merge_body、mark_unavailable）
- Test: `tests/unit/test_url_store.py`
- Read: `src/agent_search_gateway/concurrency.py`（PerKeyLockPool）

- [ ] **Step 1: 写失败测试**

`test_candidate_pair_is_atomic_and_first_abstract_wins`：A 先给 raw、B 后给 content，不能形成 A.raw+B.content。`test_metadata_admission_preserves_every_existing_status` 参数化六状态。`test_store_rejects_invalid_body_status_combinations` 验证 READY 无正文、NEED_CLEAN 有 content、NEED_JUDGE 两字段皆空和错误阶段调用不提交。

保留恰好五字段、frozen snapshot、URL/摘要输入校验。NEED_FETCH 收自然候选变 NEED_JUDGE；已有候选、READY、UNAVAILABLE 不被 Search 覆盖。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_url_store.py -v
~~~

预期：旧 available 字段及独立合并 raw/content 的断言与新状态/同源要求不同。把旧“混拼应成功”测试换成明确反例，不只删断言。

- [ ] **Step 3: 最小实现**

~~~text
URLStatus 六个固定字符串；URLRecord 第五字段 available -> status
URLStore 持 PerKeyLockPool，提供 serialization(url)
admit(url, abstract) 仅 metadata，首次合法摘要保留，既有正文/status 不动
admit_candidate 内部锁单个 URL，重读记录：
  new/NEED_FETCH + 有意义非空正文对 -> 原子写 NEED_JUDGE
  其他状态 -> 保留原候选
accept_candidate：已通过 Judge 的同源 pair -> NEED_CLEAN 或 NEED_SAFETY
reject_candidate：仅 NEED_JUDGE -> 同时清空两字段 -> NEED_FETCH
set_clean_content：NEED_CLEAN + 有效 content -> NEED_SAFETY
mark_ready：NEED_SAFETY -> READY
mark_unavailable：仅实际 Fetch 终止或 Safety 拒绝路径进入终态
~~~

转换方法是持锁 Fetch 内的同步原子提交，不再次获取锁。返回新的 immutable snapshot，不能允许 adapter 或 Search 直接写 records；不留 available 兼容 property。

- [ ] **Step 4: 验证 GREEN 与组件回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_url_store.py tests/unit/test_url_normalization.py tests/runtime/test_singleflight.py -v
~~~

预期：合法转移、正文同源、摘要和 metadata-only 不降级全部通过。

- [ ] **Step 5: 整理并保持 GREEN**

用小型私有 commit/invariant helper 消除重复，不引入 FSM 框架或任意 status setter。调用者在 Tasks 16–18 迁移，重跑上列组件命令。

### Task 15: Web Search 主动正文选项审计与最小修改

**Files:**

- Modify: `src/agent_search_gateway/providers/web/tavily.py`（search）
- Modify: `src/agent_search_gateway/providers/web/exa.py`（search）
- Test: `tests/providers/web/test_tavily.py`
- Test: `tests/providers/web/test_exa.py`
- Test: `tests/providers/web/test_firecrawl.py`
- Test: `tests/providers/web/test_baiduqianfan.py`
- Test: `tests/providers/web/test_scrapegraphai.py`
- Read: `src/agent_search_gateway/providers/defaults.py` 和本文“Web Search 审计矩阵”
- Fixtures: `tests/fixtures/providers/tavily/search.json`、`tests/fixtures/providers/exa/search.json`（现有搜索响应 fixture；按需要增加无正文/自然正文变体）

- [ ] **Step 1: 写失败测试**

`test_tavily_search_does_not_request_raw_body` 捕获出站 JSON；`test_exa_search_requests_highlights_without_full_text` 保留 highlights 且无 text=true。各测一次没有正文的有效响应和一次自然带正文的响应：摘要/标题/URL 保留，自然 raw/text 仍映射进 KeywordSearchHit。

Firecrawl 继续无 scrapeOptions/隐式 Scrape。BaiduQianfan Agentic 网页和 ScrapeGraphAI 的自然正文不删除；媒体、正常 partial success、摘要 fallback 不变。Fetch 的 /extract、/contents 和相关正文参数保持原断言。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/web/test_tavily.py tests/providers/web/test_exa.py -k "does_not_request_raw_body or without_full_text" -v
~~~

预期：旧 Tavily include_raw_content=markdown、Exa contents.text=true 与断言不符。

- [ ] **Step 3: 最小实现**

~~~text
Tavily.search 删除主动 include_raw_content 请求项
Exa.search 删除 contents.text，保留 contents.highlights
不删除自然返回 raw_content/text 的 response mapping
Exa 摘要继续 highlights -> summary -> title；无 text 仍有合法搜索结果
其他 20 个 search-capable adapter 依据审计矩阵维持已正确请求/映射
~~~

不要机械删除叫 content 的摘要字段，也不修改只属于 Fetch 的 includeRawContent/full_content。遇到本地契约无法确定的选项再查厂商一手文档；不得凭名称发明关闭参数或计费结论。

- [ ] **Step 4: 验证 GREEN 与 Web 回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/web/test_tavily.py tests/providers/web/test_exa.py tests/providers/web/test_firecrawl.py tests/providers/web/test_baiduqianfan.py tests/providers/web/test_scrapegraphai.py -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/web -q
~~~

预期：22 个 Search adapter 的既有契约不退化，两项主动正文请求已关闭。

- [ ] **Step 5: 整理并保持 GREEN**

审计记录注明不需改的 provider，保留真实 response fixture 结构。不把“没有主动正文请求”写成“没有供应商费用”。重跑 Web 回归。

### Task 16: Keyword Search 零正文 stage 与延迟 admission

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/search.py`（keyword_search、_run_keyword_pipeline、_stage_keyword_hit）
- Test: `tests/orchestrators/test_keyword_search_pipeline.py`
- Test: `tests/orchestrators/test_keyword_search_state.py`
- Test: `tests/orchestrators/test_keyword_provider_filtering.py`
- Test support: `tests/support/fakes.py`
- Regression: `tests/orchestrators/test_paper_search_pipeline.py`、`tests/orchestrators/test_llm_search.py`、`tests/orchestrators/test_agent_search.py`

- [ ] **Step 1: 写失败测试**

`test_keyword_search_caches_natural_body_without_body_stages` 参数化 raw-only/content-only/raw+content；四个 LLM stage 均为 0，cheap_check 也不预跑。`test_keyword_admission_uses_declared_provider_order` 控制 provider 完成逆序，gather 后仍按原 provider/hit 顺序选择第一份候选。metadata-only/空白正文进入 NEED_FETCH，JSONL 仍只有 url/abstract。

覆盖 redirect、无效结构/URL/摘要、重复 URL、部分 provider 失败/合法空结果成功、全失败错误、writer 失败保留已提交 admission、READY/UNAVAILABLE 不被覆盖。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_keyword_search_state.py -k "without_body_stages or declared_provider_order" -v
~~~

预期：旧 Search 调用了 Judge，或旧分字段写入改变了正文对。

- [ ] **Step 3: 最小实现**

~~~text
保留 provider fan-out 和 gather 的输入顺序
_stage_keyword_hit 只做结构、URL/redirect、摘要校验，保留自然候选
移除预读 available、cheap_check/Judge 和仅为 Judge 设的失败计数分支
批量 admission 一次处理一个 URL：
  有自然正文 -> await store.admit_candidate（内部重读并锁单 URL）
  无正文 -> store.admit
沿原顺序去重并写 JSONL
~~~

不持全局锁或多个 URL 锁。LLM/Agent 本身的搜索调用、学术聚合及 OA enrichment 不是正文 stage，继续原流程；其 metadata-only admission 不改为不必要的异步锁路径。

- [ ] **Step 4: 验证 GREEN 与搜索回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_keyword_provider_filtering.py -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_paper_search_pipeline.py tests/orchestrators/test_llm_search.py tests/orchestrators/test_agent_search.py tests/academic -q
~~~

预期：Search 完全没有正文 stage，所有合法结果/JSONL/过滤和 OA best effort 通过。

- [ ] **Step 5: 整理并保持 GREEN**

旧“Search Judge 失败/部分失败”的测试改为实际新行为断言；取消测试改在 provider/正文 admission 锁/writer 等仍存在的边界，不保留假 Judge 等待。重跑上列命令。

### Task 17: Fetch 按状态推进并复用 READY

**Files:**

- Modify: `src/agent_search_gateway/orchestrators/fetch.py`（url_fetch、_prepare_content）
- Modify: `src/agent_search_gateway/url_store.py`（仅补必要状态前置条件）
- Test: `tests/orchestrators/test_url_fetch_flow.py`
- Test: `tests/orchestrators/test_url_fetch_admission.py`
- Read: `src/agent_search_gateway/llm/stages.py`（cheap_check、judge、content_clean、safety、focus_summary）

- [ ] **Step 1: 写失败测试**

按状态逐项增加 `test_fetch_resumes_from_url_status`；每个例子断言精确调用顺序/次数与正文对。

| 起始状态/事件 | 期待行为 |
|---|---|
| NEED_JUDGE raw-only 通过 | Judge→Clean→Safety，无 Fetch。 |
| NEED_JUDGE 有 content 通过 | Judge→Safety，无 Fetch/Clean。 |
| 缓存 cheap/Judge 拒绝 | 同时清空两字段→NEED_FETCH→实际 Fetch；不继承旧候选作为 semantic_failure。 |
| NEED_FETCH scheduler accepted | 已经过 Judge；只进入 Clean（raw-only）或 Safety（有 content）。 |
| NEED_FETCH 无 provider/纯执行失败 | 原错误，保留 NEED_FETCH；可后续恢复。 |
| 实际 fetch 最终 semantic_failure | UNAVAILABLE，原 unavailable 文本。 |
| NEED_CLEAN/NEED_SAFETY | 只执行尚未完成阶段；Safety 拒绝 terminal。 |
| READY 全文/带 focus | 全文直接返回；focus 只调用 summary；独立下一请求不缓存 summary。 |
| UNAVAILABLE | 零上游调用；Search 不恢复。 |

重点独立测试 `test_cached_rejection_then_fetch_execution_failure_is_recoverable` 和 `test_ready_focus_failure_does_not_fallback_to_full_content`。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_admission.py -k "url_status or cached_rejection or ready_focus" -v
~~~

预期：旧实现按字段跳过 Judge、每次重做 Safety 或无法在中间状态恢复。

- [ ] **Step 3: 最小实现**

~~~text
保持 normalize URL/focus 与原 singleflight
取得 store.serialization(url)，重新读取 admission/status
循环直到 READY 或 UNAVAILABLE：
  NEED_JUDGE -> content or raw -> cheap/Judge -> accept 或 reject_candidate
  NEED_FETCH -> scheduler outcome -> accept_candidate / mark_unavailable / raise
  NEED_CLEAN -> content_clean -> set_clean_content
  NEED_SAFETY -> safety -> mark_ready 或 mark_unavailable
每个 stage 成功立即同步原子提交；异常/取消不提交未完成阶段
READY 才返回全文或在原 per-URL 锁内做请求级 focus_summary
~~~

不重复 Judge scheduler accepted 候选。缓存拒绝后无 provider 的错误仍为 NO_URL_FETCH_PROVIDERS；纯执行失败为原执行错误。空 Clean 输出沿 LLMStages 既有校验失败，不强行写 READY。

- [ ] **Step 4: 验证 GREEN 与 Fetch 回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_admission.py tests/scheduler/test_fetch_outcomes.py tests/unit/test_llm_stages.py tests/unit/test_llm_judge_stage.py -v
~~~

预期：六状态、原输入错误、FetchOutcome 与 LLM stage 错误边界通过。

- [ ] **Step 5: 整理并保持 GREEN**

删除私有重复 URL 锁与按 raw/content 猜状态的旧分支；不增加刷新、TTL、Focus 缓存或无 admission 抓取。重跑上列命令。

### Task 18: Search/Fetch 交错、阶段失败与既有 singleflight

**Files:**

- Test: `tests/orchestrators/test_keyword_search_state.py`
- Test: `tests/orchestrators/test_url_fetch_flow.py`
- Test: `tests/orchestrators/test_url_fetch_singleflight.py`
- Test: `tests/runtime/test_singleflight.py`
- Test: `tests/unit/test_url_store.py`
- Modify only if a failing test requires: `src/agent_search_gateway/url_store.py`、`orchestrators/fetch.py`、`orchestrators/search.py`
- Read: `src/agent_search_gateway/concurrency.py`

- [ ] **Step 1: 写失败测试**

`test_search_body_waits_then_rereads_committed_fetch_state` 在 Judge/Clean/Safety 各 await 阻塞期间让 Search 返回同 URL 的另一份正文，释放后既不倒退 READY，也不混拼。再测缓存拒绝后换正文的交错，以及两个 Search 同时 admission。不同 URL 可并发。

`test_stage_failure_preserves_last_committed_status` 在各 stage 的提交前/后注入 execution failure 或取消；下一次仅做未完成 stage。包括 Safety 失败留 NEED_SAFETY、Focus 失败留 READY。保留 follower/leader 不同取消语义、同 URL 不同 focus 串行、不同 URL 同时 focus、同 focus 后续独立请求重新 summary。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_url_fetch_flow.py -k "waits_then_rereads or last_committed_status" -v
~~~

预期：若仍用等待前 snapshot、不同锁或非原子提交，会出现旧正文覆盖/阶段重跑；原本正确的 singleflight 用例直接作为绿色回归。

- [ ] **Step 3: 最小实现**

~~~text
所有正文 admission 与 Fetch 状态推进共用 URLStore.serialization
取锁后读取；一次只锁一个 URL；stage 返回与状态提交之间不额外 await
无效输入/取消不写半个候选，不产生虚假 READY/UNAVAILABLE
锁和 singleflight 清理沿现有 finally/identity 规则
~~~

按 Testing §6.2 保留 leader cancel→共享结果取消，follower cancel→不影响 leader；不以此任务重构 singleflight 所有权。

- [ ] **Step 4: 验证 GREEN 与并发回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_url_store.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_singleflight.py tests/runtime/test_singleflight.py -v
~~~

预期：所有交错都由 Event/Barrier 确定，不依赖真实 sleep；取消后无残留 waiter，后续请求可正常完成。

- [ ] **Step 5: 整理并保持 GREEN**

只为实际发现的共享锁或状态 bug 修生产代码；不把回归任务扩为新并发架构。复用受控 fakes，重跑上列命令。

### Task 19: Doctor 逐凭证配置与适用健康检查

**Files:**

- Modify: `src/agent_search_gateway/doctor.py`（_DoctorConfiguration、_configuration_checks、run_doctor）
- Test: `tests/doctor/test_config.py`
- Test: `tests/doctor/test_open_websearch_health.py`
- Test: `tests/doctor/test_rendering.py`
- Test: `tests/doctor/test_no_network.py`
- Test support: `tests/doctor/_support.py`

- [ ] **Step 1: 写失败测试**

`test_doctor_reports_invalid_second_credential` 使第二组缺 env/选项非法，必须 FAIL 并定位 credentials[1]，不能只报告第一组有效。`test_doctor_checks_each_open_websearch_endpoint` 两个健康端点一好一坏，分别检查且最终退出失败。无认证 provider 不虚构 API key；未启用/未引用者无请求。

使用合成秘密哨兵检查配置错误/渲染/健康失败不输出 key、email、认证 URL 或异常原文；保留 filesystem/socket/exit-code 断言。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/doctor/test_config.py tests/doctor/test_open_websearch_health.py -k "credential or each_open_websearch" -v
~~~

预期：旧 Doctor 仍直接访问 provider.api_key_env/options，只检查一个 endpoint。

- [ ] **Step 3: 最小实现**

~~~text
沿用 resolve_config 的完整逐成员校验
遍历所有 active/referenced provider.credentials，报告安全 namespace/name/index
env 引用去重仅用于“已配置”信息，不用来合并额度或隐藏某成员
open-webSearch 逐 credential 调用原 health helper，保留 timeout/body/redirect/trust_env 边界
汇总原 DoctorCheck/exit code，某成员失败不被其他成功抵消
~~~

对不具有既有 health check 的 provider 只做原配置诊断；不请求付费 Search/Fetch、余额或 model 列表。

- [ ] **Step 4: 验证 GREEN 与 Doctor 回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/doctor -v
~~~

预期：逐成员与原非网络/文件系统/socket 契约全部通过，输出安全。

- [ ] **Step 5: 整理并保持 GREEN**

复用既有 health URL、超时和脱敏 helpers；不增加定时探测。重跑上列命令。

### Task 20: 凭证与状态日志的安全关联

**Files:**

- Modify: `src/agent_search_gateway/observability.py`
- Modify: `src/agent_search_gateway/concurrency.py`、`src/agent_search_gateway/rate_limit.py`（成员 gate 的固定安全 identity）
- Modify: `src/agent_search_gateway/runtime.py`（创建 gate 时绑定 identity）
- Modify: `src/agent_search_gateway/credential_pool.py`
- Modify: `src/agent_search_gateway/url_store.py`
- Modify: `src/agent_search_gateway/daemon.py`（所有成员 secret 注册回归）
- Modify as required: `src/agent_search_gateway/providers/http.py`、`providers/llm/openai_chat.py`
- Test: `tests/observability/test_provider_event_logging.py`
- Test: `tests/observability/test_provider_diagnostics.py`
- Test: `tests/observability/test_redaction.py`
- Test: `tests/observability/test_agent_event_logging.py`
- Test: `tests/unit/test_observability_logging.py`

- [ ] **Step 1: 写失败测试**

`test_concurrent_credential_attempts_have_safe_independent_identity` 并发 A/B、内部并行分支与 retry，能够区分 namespace/provider/index，provider 字段仍是逻辑名。另写 `test_gate_identity_is_known_before_selection_and_after_release`：两个成员竞争时，尚未选出 operation 的 wait/acquire 和 operation 结束后的 release 也具有正确 index，不能依赖只在 operation 内设置的 ContextVar。`test_pool_fallback_and_exhaustion_are_sanitized` 覆盖 switch、wait、exhaustion、cleanup failure；`test_url_state_events_follow_atomic_commits` 只在真实提交后记录转换，取消不伪造成功。

将各凭证 key/email、认证 URL/headers、上游错误/body 都放合成哨兵，覆盖配置、运行时错误、Doctor、清理和 DEBUG preview。日志失败不能改变业务结果。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/observability/test_provider_event_logging.py tests/observability/test_redaction.py -k "credential or pool_fallback or state_events" -v
~~~

预期：旧事件无法区分成员、只注册一组 secret，或 context 在并发/fallback 后串用。

- [ ] **Step 3: 最小实现**

~~~text
Runtime 为每个成员 gate 绑定固定 namespace/name/index，gate 的 peek/try/wait/acquire/release 日志使用该 identity
在现有 observability 增加 scoped credential context（namespace/name/index）
pool 进入成员 operation 前 set，finally reset；HTTP/LLM子任务自然继承不可变 identity
公共 log_event/HTTP event 路径附安全字段，不逐个 vendor adapter 增参数
记录 selected、fallback、exhausted、capacity/rate wait、cancel 与已提交 URL 状态
保留 request ID、UTC 时间、DEBUG preview 预算和脱敏器
所有原始 secret 在 Runtime 分配资源前登记；错误消息不格式化 credentials/options
~~~

HTTP stage/重试编号继续原含义；不根据 index 新造 provider 名，不输出正文作为状态事件。日志 sink 故障及诊断异常不能遮蔽执行主错。

- [ ] **Step 4: 验证 GREEN 与日志/输入回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/observability tests/unit/test_observability_logging.py tests/unit/test_input_limits.py tests/doctor -v
~~~

预期：成员关联、状态提交、所有脱敏/UTC/body cap 回归通过，普通结果格式无新字段。

- [ ] **Step 5: 整理并保持 GREEN**

用固定 gate identity 加 scoped operation context 避免为 25 个 Web adapters 写相同日志 plumbing；检查选择前、释放后及 early return/异常/取消的标识都正确，context 始终 reset。重跑上列命令。

### Task 21: 真实 Runtime 的离线端到端验收

**Files:**

- Create: `tests/acceptance/test_credential_pool_fetch_state.py`
- Modify: `tests/acceptance/test_gateway_workflows.py`
- Modify: `tests/acceptance/test_agent_search_workflows.py`
- Modify: `tests/acceptance/test_search_cost_arxiv_proxy_cli.py`
- Modify: `tests/support/acceptance.py`
- Modify: `tests/support/agent_search.py`
- Regression: `tests/cli/test_cli.py`、`tests/daemon/test_daemon_dispatch.py`、`tests/daemon/test_daemon_request_ids.py`、`tests/daemon/test_daemon_shutdown.py`、`tests/daemon/test_agent_shutdown.py`

- [ ] **Step 1: 写失败测试**

使用生产 resolve_config/Runtime、实际 adapter/executor、MockTransport、临时 socket 和既有 CLI harness；只 mock 最底层外部服务，不把整个 Runtime 替换成“总是成功”。

至少八个独立场景：

1. 显式 fetch_order：A1 重试耗尽→A2 accepted，后续 provider 零调用。
2. 省略所有 limits：受控并发独立调用无默认 cap；与 Task 10 的真实 HTTPX 配置证据配对。
3. 两成员独立窗口：RR/FF 的真实发送序列及恢复，retry 计数正确。
4. 自然正文：Keyword Search 零 LLM；首次 Fetch Judge/按需 Clean/Safety；再次全文零 stage；focus 只 summary。
5. 缓存拒绝：清空旧对、实际 Fetch 换同源新正文；纯执行失败时仍 NEED_FETCH。
6. Web/Academic/OA/LLM 各自池及同 alias 共享；Agent 仅 CLI gate；现有 provider/model/scope 过滤。
7. retry/选择等待/正文阶段取消与 daemon stop：lease/waiter/client/子进程回收，后续独立调用能执行。
8. 旧配置迁移错误、合成的新 schema 配置可解析/Doctor、安全输出，保持 stdout/stderr、JSONL、错误 envelope 和 unavailable 文本。仓库真实示例文件的解析/Doctor 验收在 Task 22 迁移完成后执行。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/acceptance/test_credential_pool_fetch_state.py -v
~~~

每次先加入一个验收场景，确认其缺少的集成行为失败；已有正确路径作为回归。不能只检查 fixture 预设结果。

- [ ] **Step 3: 最小实现或集成修正**

~~~text
新 schema + 合成 environ -> await Runtime.build
HTTP factory 接受并保留实际 kwargs，transport 记录 endpoint/auth/body/time
以 CLI/daemon 原消息驱动 Search/Fetch
观察实际出站、JSONL/返回正文、store状态与关闭计数
只修未贯通的装配/协议边界，不另写替代 runtime
~~~

同步迁移旧 AcceptanceRuntime/fakes 的 quota、available、Safety 次数断言。Firecrawl/arXiv/proxy/CLI 超时的既有验收继续通过，不增加新的断线 watcher 或请求总 deadline。

- [ ] **Step 4: 验证 GREEN 与第二个集成检查点**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/acceptance/test_credential_pool_fetch_state.py tests/acceptance/test_gateway_workflows.py tests/acceptance/test_agent_search_workflows.py tests/acceptance/test_search_cost_arxiv_proxy_cli.py tests/cli tests/daemon -v
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime tests/scheduler tests/orchestrators tests/academic -q
~~~

预期：基于合成配置的八类验收及旧协议、过滤、Agent 生命周期通过；明确记录通过/失败/跳过数量。依赖仓库真实 config.example.toml 的 test_debug_and_doctor.py 门槛随 Task 22 执行，Task 23 再运行全部 acceptance。

- [ ] **Step 5: 整理并保持 GREEN**

复用现有 _running_daemon/CLI harness，只为新场景添加局部 fixture。避免 mock 高层组件掩盖真实构造/关闭问题，重跑上列命令。

### Task 22: 当前文档、示例与遗漏 fixture 迁移

**Files:**

- Modify: `config.example.toml`
- Modify: `config.youcom-free.fragment.toml`
- Modify: `README.md`
- Modify: `docs/public-interface.md`
- Modify: `docs/provider-rate-limits.md`
- Modify: `docs/youcom-free-mcp.md`
- Modify: `CHANGELOG.md`
- Test: `tests/docs/test_documented_config.py`
- Test: `tests/docs/test_installation_contract.py`
- Test: `tests/docs/test_open_source_contract.py`
- Test: `tests/acceptance/test_debug_and_doctor.py`
- Migrate affected fixtures/call sites: `tests/runtime/conftest.py`、`tests/doctor/_support.py`、`tests/support/fakes.py`、`tests/support/acceptance.py`、`tests/support/agent_search.py`
- Migrate affected live helpers without running them: `tests/integration/test_live_tavily_and_openai.py`、`tests/integration/test_live_parallel.py`、`tests/integration/test_live_new_web_providers.py`、`tests/integration/test_live_baiduqianfan.py`、`tests/integration/test_live_rate_limits_and_inputs.py`
- Regression for those helpers: `tests/unit/test_live_smoke_validation.py`、`tests/unit/test_live_baiduqianfan_validation.py`、`tests/unit/test_live_preview_budget_validation.py`

- [ ] **Step 1: 更新实际配置合同测试并观察失败**

`test_example_config_resolves_each_credential_with_stub_secrets` 让现有 _stub_environment 进入 credentials 数组，验证所有启用/禁用模板可在合成环境按规则启用。You.com 免费 MCP fragment 单独解析且不配置 key；REST 示例明确要求 key。对迁完的实际 config.example.toml/fragment 用合成环境运行 Doctor，适用 health 由本地 mock 提供，不收费联网。加第二成员与无 limits 示例，断言 parser 不继承示例推荐值。

原文档测试中 provider.rate_policy/max_concurrency/secret 访问迁到成员；旧“默认 3”“知乎继承默认并发”等断言改为显式值或缺省无限制。不要用字面字符串覆盖率代替真实解析/运行时出站检查。

- [ ] **Step 2: 验证 RED**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/docs/test_documented_config.py -v
~~~

预期：旧 TOML 或 _stub_environment 仍使用顶层 endpoint/key/limits，明确报迁移错误。

- [ ] **Step 3: 最小文档和 fixture 迁移**

~~~text
所有当前 HTTP 示例改为 [[namespace.provider.credentials]]
endpoint/auth/limits 放 credential，业务配置保留 provider
LLM model/invocation 层不变；Agent 保持 CLI 表且 limits 独立可选
展示 fetch_order 的追加规则、RR/FF、同 key 先重试再有限切换
说明缺省不限流、HTTPX活跃连接无 cap、rate与逻辑capacity计数单位
说明 Search延迟验证、URL不可任意抓取、READY复用、Focus不缓存
明确拒绝旧格式及如何手动保留原并发值；不自动修改用户实际配置
CHANGELOG Unreleased 标注配置 breaking change，不决定新版本号/执行发布
~~~

TOML 注意父子表归属：在首次显式声明的 provider 表中先写完公共 options，再声明 credentials 数组；不能重复声明已经显式定义的同一父表。后续业务子表使用完整路径；不能误把 enable/model/search 参数放进最后一个 credential。HTTP 推荐值仍是示例数据，不在此任务重新研究或改写上游价格/配额事实。

所有跨文件 fixture 迁移在其关联实现任务已做；这里做剩余审计，不能将旧配置自动转换 helper 用于生产或隐藏 legacy rejection。历史 docs/designs 文档不改成新 schema。

- [ ] **Step 4: 验证 GREEN 与文档/辅助入口回归**

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/docs tests/acceptance/test_debug_and_doctor.py tests/unit/test_live_smoke_validation.py tests/unit/test_live_baiduqianfan_validation.py tests/unit/test_live_preview_budget_validation.py -v
uv run python scripts/build_docs.py
~~~

预期：当前示例全部可解析且生成文档成功，live helper 的离线测试通过；不执行 live 测试。build_docs 产生的 site 文件按既有生成物规则处理，不混入无关改动。

- [ ] **Step 5: 整理并保持 GREEN**

针对性搜索 default_max_concurrency、旧 available/merge_body、provider 顶层旧字段、Runtime.build 同步调用和无参 HTTP factory。历史 spec、明确拒绝旧字段的测试及迁移说明可保留；当前运行路径必须清除。重跑文档和相关 helper 测试。

### Task 23: 最终自审、全量检查与实施记录

**Files:**

- Read: `CONTRIBUTING.md`、`.github/workflows/ci.yml`
- Verify: 本计划涉及的全部源码/测试/当前文档
- Update after implementation: `docs/designs/plans/20261125-credential-pool-fetch-state.md`（实际完成情况与证据）

- [ ] **Step 1: 按覆盖矩阵检查剩余失败风险**

逐条核对下文覆盖表和三份 spec。缺少关键行为时先加一个能失败的测试，再补最小实现；若无缺口，不为验证文档或凑测试数新建镜像测试。特别核对新 schema、无默认 cap、单轮 fallback、取消、close-once、Search 零正文 stages、Safety 复用和已确认的错误终止边界。

- [ ] **Step 2: 核实失败属于缺失行为而非测试基础设施**

运行任何新增缺口测试的确切 node，记录预期失败。缺环境/ImportError/错误 fixture 不是合格 RED；修正后再检查。已有回归全部绿色则直接进入最终检查，不故意制造失败。

- [ ] **Step 3: 只完成必要修正**

补齐有证据的类型、fixture 或集成问题；不借最终整理添加默认 limiter、凭证冷却、Agent 账户、刷新缓存、额外 retry 或总 deadline。代码和文档的字段/方法名称全篇统一。

- [ ] **Step 4: 执行项目要求的完整检查**

~~~bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest -v
uv run python scripts/build_docs.py
~~~

上述命令以 CONTRIBUTING/CI 为准，mypy 覆盖 src/tests/scripts，不能只跑 spec 中较窄的 src。本次无依赖/打包配置变更时不额外改 lockfile；CI 既有 Python 3.11/3.12/3.13/3.14 与 macOS job 保持。平台未实际运行不能宣称已通过。发布、版本号调整、PR 和合并由后续对应任务处理。

预期：格式、lint、mypy、全部离线测试和 docs build 通过；live integration 的默认 skip 如实记录，不把 skip 算成真实 API 成功。

可选 smoke 只在隔离临时配置/独立 socket 下运行少量已授权 provider。缺多组真实凭证不阻塞 mock 验收；记录真实尝试数与服务错误，不用 smoke 替代调度/并发证明。

- [ ] **Step 5: 收敛实施记录**

检查 diff 只含本任务文件；更新每项实际完成状态、命令和通过/失败/跳过数。若存在未通过检查，明确列出原因与剩余步骤，不能全部勾选。最终提交实现遵循后续执行技能/用户指令；本写计划任务仅提交本计划并停止。

---

## Web Search 审计矩阵

审计依据为当前 22 个 search-capable adapter 的实际请求和 response mapping；实施时按 registry 再核对集合。fetch-only 的 Jina、ScrapingAnt、ZenRows 不在 Search 请求修改范围。

| Provider | 当前摘要/自然正文来源 | 实施动作与现有测试 |
|---|---|---|
| tavily | content 为摘要，raw_content 为自然 raw；当前主动要求 markdown raw | 删除 Search include_raw_content；保留自然 raw，Fetch /extract 不动。`tests/providers/web/test_tavily.py`。 |
| exa | highlights→summary→title 为摘要；text 映射 raw+content | 删除 Search contents.text，保留 highlights/自然 text；Fetch /contents 不动。`tests/providers/web/test_exa.py`。 |
| baiduqianfan | 普通搜索摘要；Agentic 网页 content 为 raw；媒体不是网页正文 | 保留双分支、partial success、网页正文与媒体摘要。`tests/providers/web/test_baiduqianfan.py`。 |
| scrapegraphai | query-only；原生 content 为 raw+content，摘要可回退 title | 保留自然正文。`tests/providers/web/test_scrapegraphai.py`。 |
| firecrawl | description/highlights；既有 metadata-only | 继续无 scrapeOptions/隐式 Scrape。`tests/providers/web/test_firecrawl.py`。 |
| brave | description | 请求/映射保持。`tests/providers/web/test_brave.py`。 |
| brightdata | 搜索引擎结果 description；format=raw 不是目标网页全文开关 | 保留请求语义；zone 只由 credential effective config 绑定。`tests/providers/web/test_brightdata.py`。 |
| open_websearch | description | 保留 engines 与 metadata。`tests/providers/web/test_open_websearch.py`。 |
| decodo | Google parse 的 desc | 保留解析。`tests/providers/web/test_decodo.py`。 |
| parallel | excerpts；Search 原有 search_fetch_policy | 不触碰仅 Extract 使用的 full_content。`tests/providers/web/test_parallel.py`。 |
| youcom | REST snippets；MCP highlights→snippets | 保留 REST 必需 key、官方免费 MCP 禁止 key 的各自规则；不添加正文请求。`tests/providers/web/test_youcom.py`。 |
| doubao | Summary 或 Snippet | 保留摘要。`tests/providers/web/test_doubao.py`。 |
| keenable | snippet | 保留摘要。`tests/providers/web/test_keenable.py`。 |
| anysearch | keyword_hits_from_results 的 snippet | 保留公共映射。`tests/providers/web/test_anysearch.py`。 |
| tinyfish | keyword_hits_from_results 的 snippet | 保留公共映射与双端点。`tests/providers/web/test_tinyfish.py`。 |
| scrape_do | organic_search_hits 的 snippet | 保留搜索包装。`tests/providers/web/test_scrape_do.py`。 |
| scraperapi | organic_search_hits 的 snippet | 保留搜索包装。`tests/providers/web/test_scraperapi.py`。 |
| scrapingdog | organic_search_hits 的 snippet | 保留搜索包装。`tests/providers/web/test_scrapingdog.py`。 |
| serpapi | organic_search_hits 的 snippet | 保留搜索包装。`tests/providers/web/test_serpapi.py`。 |
| linkup | standard/searchResults 的 content 为摘要 | 不能删除摘要；includeRawContent 属于 Fetch。`tests/providers/web/test_linkup.py`。 |
| zhihu | 双分支 ContentText 为摘要 | 保留双分支与速率计数，不能因字段名误删摘要。`tests/providers/web/test_zhihu.py`。 |
| yuanbao | page.content/passage 为摘要 | 保留摘要映射。`tests/providers/web/test_yuanbao.py`。 |

对应 production 文件均位于 `src/agent_search_gateway/providers/web/`。本计划不要求为未改变的正确请求各写一套重复测试，运行既有回归并补真正缺失的断言即可。

## Spec 覆盖与接口一致性检查

| Spec 要求 | 对应任务 |
|---|---|
| Architecture D1、§4：四类别共享 pool、逻辑 provider 身份、factory 复用 | 6–11、21 |
| D2/D9、§6.1：统一 schema、特殊 endpoint/auth/zone、旧格式拒绝、手工迁移 | 1–4、11、19、22 |
| D3：全部限制独立可选、无默认 cap、HTTPX 活跃连接无限制、Agent invocation | 1、4–5、8、10–12、21 |
| D4：RR/FF、稳定游标、原凭证重试、有限 fallback、成功空值、无失败记忆 | 6–9、21 |
| Error §3.2 / Testing §4.3：等待预算、物理计数、Retry-After、竞争、取消 | 5–8、12–13、21 |
| D5 / Error §4.2：fetch_order、ready pool、lease 交接、Judge 错误及混合 outcome | 2、11、13、17、21 |
| D6 / Error §5 / Testing §6：五字段六状态、同源正文、状态恢复、READY/Safety、缓存拒绝可恢复 | 14、16–18、21 |
| D7 / Testing §5.1–5.2：22 adapters 审计、Search 零正文 stage、自然正文、metadata-only/OA | 9、15–16、21 |
| D8 / Error §2/6 / Testing §7：Doctor、脱敏、attempt identity、输入 cap、原错误和资源生命周期 | 8、10–12、18–21 |
| Testing §8：八类真实装配离线验收 | 21 |
| Testing §9 / CONTRIBUTING / CI：完整回归、命令证据、live skip 如实记录 | 22–23 |

规划自检要点：

- 同一 `ResolvedCredential` 表达四类成员；Web secret 与 Academic/OA api_key 参数在 builder 显式映射，业务字段不泄漏进 vendor kwargs。
- 一个 pool 负责 selection/fallback；capacity 只在 member operation 持有，rate 只在真实 attempt/spawn 边界消费。
- Scheduler 首次 reservation 不被再次轮询；SelectionBudget 的 owner/候选与 initial_rate_budget 的 task/gate 一致，既不双扣也不重置。
- LLM 的 input preparation 在 pool 外，protocol retry 在选中的物理 client 内，上层 parser/Judge/Safety 决策在 pool 外；prepared 方法保留 invocation/messages 以维持日志。
- URLStatus 的六个名称及 URLStore 各方法在所有任务一致；Search content-only 与 vendor Fetch raw contract 分开验证。
- Search 正文与 Fetch 共用 store 的 per-URL 锁，转换函数同步提交、不重入锁；metadata-only 不改变既有状态。
- 保留既有 singleflight leader/follower 区别；不把通用取消描述扩展成新所有权模型。
- 所有新行为均有对应 task 和验收位置；实施复选框与文末实际命令、结果共同记录完成状态。

## 实施记录（进行中）

2026-11-25：使用 subagent-driven-development，逐任务派发独立实现子代理并完成自查；复用本工作树。`uv sync --locked --all-groups` 成功，当前验证环境为 Linux / Python 3.12.3。真实 API 集成开关在离线测试中显式移除。

| Task | 状态 | 已执行验证 |
|---|---|---|
| 1 | 完成 | 公共策略/limits 目标 51 通过；当时四类配置组件 1,162 通过；相关 Ruff / mypy 通过。 |
| 2 | 完成 | Web 配置 + registry 700 通过；相邻 Academic/LLM/Agent 配置 562 通过；相关 Ruff / mypy 通过。 |
| 3 | 完成 | Academic/OA 565 通过；合并相邻配置/registry 1,405 通过；相关 Ruff / mypy 通过。 |
| 4 | 完成 | 四类配置及请求筛选检查点 1,648 通过、0 失败、0 跳过；相关 Ruff 通过；四文件 `mypy --follow-imports=silent` 通过。 |
| 5 | 完成 | quota/rate/singleflight 46 通过；HTTP executor 236 通过；Ruff 通过。 |
| 6 | 完成 | pool 10 通过；合并 pool/quota/rate 52 通过；相关 Ruff / 严格 mypy 通过。 |
| 7 | 完成 | pool/rate/retry 92 通过（含 pool 35）；复现并修复成功交付前待生效取消；相关 Ruff / 严格 mypy 通过。 |
| 8 | 完成 | executor/LLM/pool/retry/input 376 通过；LLM Search 及 gated live 文件 4 通过、8 个原有 live skip；7 文件 Ruff 通过。 |
| 9 | 完成 | pool/façade 66 通过；过滤/调用选择 115 通过；20 文件 Ruff、4 生产文件 scoped mypy 通过。扩大诊断 329 通过、5 项后续配置/Doctor 迁移失败。 |
| 10 | 完成 | 公共 HTTP factory/资源 owner 7 项 RED→GREEN；真实 HTTPX 直连/环境代理 transport 的活动连接上限均为无限制表示，keepalive 为 20；2 文件 Ruff 通过。 |
| 11 | 完成 | HTTP 装配目标 16 通过、76 deselected；资源及相关启动/Academic/optional 26 通过（含资源 15）；扩大回归 291 通过、7 个明确 Task 12/13 依赖 deselected。19 文件 Ruff lint/format 通过；三个生产模块 mypy 仅余 Task 12 的 quota=None 参数类型问题。 |
| 12 | 完成 | Agent 111 通过；Task 11 两项移交用例 2 通过；quota/pool 51 通过；5 文件 Ruff lint/format 和 3 生产模块 mypy 通过，消除 Task 11 的类型依赖。 |
| 13 | 完成 | 首个完整集成检查点 4,008 通过、0 失败；Scheduler 37 通过；迁移的 URL Fetch 编排器及 gateway workflow 71 通过；22 文件 Ruff lint/format、6 生产模块 mypy 通过。 |
| 14–23 | 待实施 | URL 状态机、Search 延迟处理、诊断、文档和全量验收尚未完成。 |

配置检查点实际命令：

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py tests/unit/test_search_request_selectors.py -q
~~~

相同选择的 `-v` 曾超过 LocalHost 120 KB 输出上限并以 143 中断；上列 `-q` 全部执行通过。后续大套件采用相同测试选择的紧凑输出，保留实际退出码与数量，不将输出中断当成断言失败或通过。

当前为 Task 1–13 的第一个完整集成检查点，覆盖 Scheduler、Runtime、providers、daemon 和四类配置，不能据此声称全仓库已通过。Runtime 多凭证装配、异步启动、40 个测试构造调用点和代理/认证 fixture 已迁移。Python 代码已清除旧 HTTP 聚合 quota API、InitialRateBudget 及 initial_rate_budget 标量入口，统一使用 SelectionBudget。Agent 保留 alias 级可选 gate；URL 状态机和 Doctor 尚待后续任务。未修改用户实际配置、共享 daemon 或主仓库分支。

Task 11 的 7 个明确后置依赖保留原测试，未标记 skip：Task 12 的 `test_runtime_binds_each_alias_and_does_not_retry_or_invoke_unselected_alias` 与 `test_runtime_exhausted_web_rate_allows_same_named_llm_and_agent` 需要 Agent runner 支持无 gate；Task 13 的 `test_new_web_provider_search_and_fetch_contend_for_runtime_quota` 四个参数分支（doubao、youcom、keenable、open_websearch）及 `test_youcom_mcp_search_and_fetch_share_concurrency` 需要 Scheduler 改用池预留接口。这 7 项仅在 Task 11 扩大诊断命令中 deselect，已在 Tasks 12–13 修复并在 Task 13 集成门槛中全部通过。

Task 11 已复现并修复部分构造、反复取消、并发关闭和 socket bind 失败时的资源清理；`join_cleanup(task)` 屏蔽等待者取消并等待真实清理完成，随后传播取消。多个 Runtime.aclose 调用等待同一个关闭任务，清理或日志错误不掩盖原始启动错误。所有 HTTP 成员 key/email 在资源分配前注册脱敏。TinyFish、BrightData、You.com REST/MCP、Academic/OA 和 LLM 的成员 endpoint/auth/options 以真实适配器 + MockTransport 出站验证。

Task 7 控制者复核发现：未限流的 operation 请求取消后同步成功返回，调用方仍可能接收结果。实现子代理先复现，再在同一 task 内交付结果前加入取消检查；回归证明无结果写入、无下一个成员调用且容量释放。Task 8/9 验证真实 executor/LLM 的内部重试保持所选凭证、准备字节复用以及池外准备失败时零发送/零游标推进。

组件检查点实际命令：

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_credential_pool.py tests/runtime/test_rate_gate.py tests/unit/test_retry.py -q
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/test_http_executor.py tests/providers/test_bounded_http_executor.py tests/providers/llm/test_openai_chat.py tests/runtime/test_credential_pool.py tests/unit/test_retry.py tests/unit/test_input_limits.py -q --tb=short
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_pooled_providers.py tests/runtime/test_credential_pool.py -q
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_paper_provider_filtering.py tests/orchestrators/test_llm_provider_model_filtering.py tests/orchestrators/test_llm_invocation_selection.py -q
~~~

Task 12 在受控 condition 竞争下复现重复取消使容量计数 in_use 停留为 1，使用 join_cleanup 完成释放后再传播取消，相关回归通过。输入校验/UTF-8 裁剪先于可选 gates；rate 等待复用 retry.request_timeout_seconds，CLI 执行 timeout 仍从取得许可后的原边界计时。

Task 13 先迁移接口后得到 32 通过 / 4 失败的实际调度行为 RED，修复首次 waiter 迟调度与清理取消边界。最终自检再次复现“超时已发生但 observer 仍在清理，loser 取消掩盖超时后调用过期凭证”的交错；ManualClock 回归现在断言两个候选超时、发送为零，同时保留精确截止就绪的重检机会。首轮大集合的 7 项 arXiv/Firecrawl 旧配置 fixture 已迁移，最终 4,008 全绿。

Runtime 与 Agent 实际验证命令（对应上表历史检查点）：

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_assembly.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_new_web_runtime_assembly.py tests/runtime/test_optional_credentials.py -k 'one_pool or credential or sorts_fetch' -q --tb=line
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_runtime_resources.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_optional_credentials.py tests/daemon/test_daemon_debug.py tests/daemon/test_daemon_shutdown.py -q --tb=line
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime tests/daemon/test_daemon_debug.py tests/daemon/test_daemon_shutdown.py tests/daemon/test_agent_shutdown.py tests/providers/academic/test_arxiv.py::test_runtime_arxiv_options_reach_real_http_request -k 'not binds_each_alias_and_does_not_retry and not search_and_fetch_contend_for_runtime_quota and not mcp_search_and_fetch_share_concurrency and not exhausted_web_rate_allows_same_named_llm_and_agent' -q --tb=line
uv run mypy src/agent_search_gateway/runtime.py src/agent_search_gateway/daemon.py src/agent_search_gateway/concurrency.py
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/providers/agent tests/runtime/test_agent_runtime_assembly.py tests/daemon/test_agent_shutdown.py tests/unit/test_agent_stages.py -q
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_agent_runtime_assembly.py::test_runtime_binds_each_alias_and_does_not_retry_or_invoke_unselected_alias tests/runtime/test_runtime_assembly.py::test_runtime_exhausted_web_rate_allows_same_named_llm_and_agent -q
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/runtime/test_quota_manager.py tests/runtime/test_credential_pool.py -q
uv run mypy src/agent_search_gateway/providers/agent/antigravity_cli.py src/agent_search_gateway/runtime.py src/agent_search_gateway/concurrency.py
~~~

Task 13 集成检查点实际命令，4,008 passed in 16.33s：

~~~bash
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest tests/scheduler tests/runtime tests/providers tests/daemon tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py -q --tb=short
~~~

## 计划编写与自检记录

- [x] 阅读架构、错误处理、测试三份设计。
- [x] 核对现有文件、关键符号、调用关系和测试入口。
- [x] 分解为 23 个任务，明确配置/运行时切换及两个集成检查点。
- [x] 写入实施计划，提供各任务的 RED/GREEN、最小实现、回归命令。
- [x] 对照 spec 检查覆盖与类型一致性，并完成独立只读审阅。

已修正审阅发现的 Runtime/LLM/真实示例验收依赖、选择前 gate 身份、四类策略解析、You.com 模式认证、可用性重检及单候选超时、TOML 父表和实际符号名问题。此段仅记录最初计划评审；实施完成状态和真实验证结果见上方实施记录。
