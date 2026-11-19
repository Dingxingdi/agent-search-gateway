# Provider 限速、可追踪日志、OpenAlex 鉴权与输入裁剪 Implementation Plan

**Goal:** 在现有共享 HTTP、配额与 Prompt 边界实现提供商多窗口限速、UTC 日志时间、OpenAlex 必需 Key、LLM/Agent 出站字节预算及 debug 双向安全 Body 预览，并交付全部 31 个非 LLM/Agent 服务的来源与配置说明。

**Architecture:** [docs/designs/architectures/20261118-provider-rate-observability-openalex-input-truncation.md](../architectures/20261118-provider-rate-observability-openalex-input-truncation.md)

**Error handling:** [docs/designs/error-handlings/20261118-provider-rate-observability-openalex-input-truncation.md](../error-handlings/20261118-provider-rate-observability-openalex-input-truncation.md)

**Testing:** [docs/designs/testings/20261118-provider-rate-observability-openalex-input-truncation.md](../testings/20261118-provider-rate-observability-openalex-input-truncation.md)

---

## 0. 执行上下文

- 工作树：`/home/agent/agent-search-gateway/.worktrees/patch-rate-limits-logging-openalex-input-caps-20261118`。
- 分支：`patch-rate-limits-logging-openalex-input-caps-20261118`。计划基线为设计提交 `3d5c481`；代码基线为 `edcc627`（v0.4.0）。
- 规划日期：2026-11-18；实施日期：2026-11-19。Task 1–16 已完成，任务步骤中的 PASS/FAIL 描述预期验证，实际执行结果与保留限制见第 6 节。OpenRouter 大输入补测的最新结果见第 6.5 节末尾。
- 已读取全局 `/home/agent/.codex/AGENTS.md`；项目根、工作树及其已检查子目录未发现 AGENTS.md、CLAUDE.md、GEMINI.md。后续实施若新增目录级指令，先加载再操作。
- 沿用本工作树，不切换或重置其他分支，不修改用户实际配置、环境、运行中的 daemon、其他项目或共享交接文件。
- 依据 `writing-plans`、其要求的 `test-driven-development` 及测试反模式参考组织任务；官方资料核验按 `documentation-lookup` 进行。当前没有 Context7 resolve/query 工具，因此使用官方网页及官方仓库核验。
- 本计划的提交不代表功能已经实现。实施后仅更新任务复选框和真实验证记录，不把未跑、SKIP 或来源调查写成实测 PASS。

### 0.1 保留的设计决策

1. 同一非 LLM/Agent provider 的所有实际 HTTP 请求共用一份速率状态；同一 Web provider 的 Search、Fetch、内部多请求和重试都计数。LLM/Agent 不增加 RPM/TPM。
2. `max_concurrency` 与 `rate_limits`、`min_interval_seconds` 分别约束同时在途数、时间窗口请求数和最短间隔。速率窗口采用 monotonic 滑动窗口；不把 RPM 简化为固定 sleep。
3. 新字段缺省保持不限本地固定速率的旧行为。厂商推荐值只进入文档和示例，不写成 registry/runtime 内置默认；不做账单、credits、月额度管理。
4. 每次 RateGate 等待使用现有 `retry.request_timeout_seconds` 作为上限；HTTP timeout 另起计时。等待超时不重试本地等待、不伪造已发送的 HTTP 事件。
5. LLM 默认 `131072` bytes（128 KiB），以最终 JSON HTTP body 计算；Agent 默认 `98304` bytes（96 KiB），以完整单个 `-p` 参数计算。只裁明确标注的正文，保留 system、模板、Focus 和角色标记。
6. 正文以剩余预算的 80% 头、20% 尾裁剪，省略标记包含在预算内。字节 cap 不等于 token cap；不增加摘要调用或缩小重发循环。
7. 每条格式化日志加入来自 `LogRecord.created` 的 UTC 毫秒 `time`；耗时继续由 monotonic 计算。
8. 只有 DEBUG 准备/输出 `upstream_body_preview`。每个实际 HTTP attempt 和每个尝试 spawn 的 Agent 调用分别一条；两侧各最多 8192 UTF-8 bytes，含省略标记，先脱敏后裁剪。同时验证预览文本本身和 JSON 转义后字段值的字节预算；整行含两侧与元数据可大于 8 KiB。
9. OpenAlex enabled 时必须解析 Key，使用 `Authorization: Bearer`；disabled 不要求 Key，401/403 不匿名回退。联系邮箱仍可选。项目主动要求 Key，不声称官方彻底禁止匿名。
10. 保持现有公开 ErrorCode、CLI、stdout、结果模式、URL admission、Retry-After、总 attempt 上限、Agent 取消与清理契约。OpenAlex 鉴权迁移明确作为已有配置的不兼容变化记录。

### 0.2 已核对的调用链与特殊路径

- `config.py` 从 provider 表分离共享字段和 adapter options；`Runtime.build` 按 registry 创建 adapter 及 `HttpJsonExecutor`。
- Web 双能力 provider 使用同一个 adapter；`ProviderQuotaManager` 已共享其 Search/Fetch concurrency。新增 rate 必须共享同一对象，不能分别构建两个相同配置的 gate。
- `HttpJsonExecutor._request_response` 是所有 HTTP 的共同 attempt 边界；`request_json` 在该边界返回后解码。因此预览写在共享发送边界即可覆盖 HTTP 200 非法 JSON。
- Zhihu 一次逻辑 Search 会产生两个真实 HTTP 分支；它们与 retry 都要经过同一 provider RateGate，不能只对一次 adapter.search 计数。
- `FetchScheduler` 在候选选择阶段持有并发租约；rate 查询只给建议，最终预约仍属于 HTTP Executor。
- Academic Search 与 Unpaywall enrichment 独立；OA 失败继续保留已发现论文。
- `ChatMessage = Mapping[str, str]`；LLM 和 Agent 接口使用 `Sequence[ChatMessage]`。六个现有 Prompt builder 可显式标注可裁正文，保持 Mapping 协议和现有 stage 调用方式。
- Agent `run_antigravity` 自己拥有 spawn、输出上限、deadline 和进程组清理。预览不得绕过这些边界，也不得把预览裁剪后的 stdout 交给业务解析。
- `url-fetch` 没有 `--provider`；单家 Fetch 验收通过隔离配置或 adapter 完成。正常 CLI Fetch 必须先在同一 Runtime 中录入 URLStore。

## 1. 文件职责与统一接口

以下精确路径以工作树根目录为基准；现有文件按符号定位，避免后续行号变化造成误改。

| 文件 | 操作与职责 |
|---|---|
| `src/agent_search_gateway/config.py` | 增加不可变速率/输入配置；严格校验并从 adapter options 中移除共享字段 |
| `src/agent_search_gateway/rate_limit.py` | 新增；异步滑动窗口、原子预约、可取消且有期限的等待、非预约 ready-in 查询 |
| `src/agent_search_gateway/concurrency.py` | 在现有 ProviderQuotaManager 中持有按命名空间区分的 RateGate；保留 CapacityGate |
| `src/agent_search_gateway/runtime.py` | 每个启用 provider 创建并注入 gate；传入 LLM/Agent 输入上限；OA gate 单独注册 |
| `src/agent_search_gateway/providers/http.py` | 每次发送前限速；接受已测量的 JSON bytes；一次 attempt 一个双向预览 |
| `src/agent_search_gateway/scheduler/fetch.py` | rate + capacity 候选选择、最早唤醒、等待预算与取消清理 |
| `src/agent_search_gateway/providers/academic/defaults.py` | OpenAlex authentication 改 required |
| `src/agent_search_gateway/providers/academic/openalex.py` | 增加 SecretValue 入参并用 Bearer Header 发送 |
| `src/agent_search_gateway/input_limits.py` | 新增；UTF-8 首尾裁剪、可裁消息、最终编码预算与元数据 |
| `src/agent_search_gateway/llm/prompts.py` | 六个 builder 标注 protected prefix/body/suffix，保持文本内容与 Mapping 兼容 |
| `src/agent_search_gateway/providers/llm/openai_chat.py` | 用实际 httpx JSON 编码器准备一次 body，供两层 retry 重用 |
| `src/agent_search_gateway/providers/agent/antigravity_cli.py` | 完整 prompt cap、可靠的系统边界预检、失败/取消时保留有界 stdout 预览 |
| `src/agent_search_gateway/observability.py` | UTC formatter、安全共享 redactor、lazy preview 入口及安全降级 |
| `src/agent_search_gateway/body_preview.py` | 新增；有界 Body 脱敏/预览纯逻辑；不导入 Runtime 或发网络请求 |
| `src/agent_search_gateway/providers/diagnostics.py` | allowlist 新增安全原因，维持现有 provider 失败归类 |
| `config.example.toml`、`README.md`、`docs/public-interface.md`、`CHANGELOG.md` | 输入配置、限流、OpenAlex 迁移、debug Body 行为及兼容性说明 |
| `docs/provider-rate-limits.md` | 新增；31 服务官方来源、产品/认证条件、数值/未知说明的用户参考 |
| `tests/support/controlled.py`、`tests/support/http.py`、`tests/support/logging.py`、`tests/support/agent_cli.py` | 只在现有支撑无法覆盖时扩展可控时钟、MockTransport、日志解析和假 CLI；不建第二套框架 |

`retry.py`、`academic/enrichment.py`、`llm/stages.py`、`providers/contracts.py` 是重点回归观察点；只有实际接线需要时才最小修改，不为本次重构这些模块。

### 1.1 配置与 RateGate 合同

~~~python
# config.py；均 frozen=True
ResolvedRateWindow(requests: int, period_seconds: float)
ResolvedRatePolicy(
    windows: tuple[ResolvedRateWindow, ...] = (),
    min_interval_seconds: float | None = None,
)
ResolvedInputLimits(
    llm_request_body_bytes: int = 131072,
    agent_prompt_bytes: int = 98304,
)

# 在 Web / Academic / OA resolved provider 对象末尾增加默认字段
rate_policy: ResolvedRatePolicy

# ResolvedConfig 末尾增加有默认值的字段
input_limits: ResolvedInputLimits
~~~

新默认字段必须保留旧测试及内部调用按既有参数构建 resolved 对象的能力。不可变对象可共享默认值，不能共享可变时间戳队列。

~~~python
# rate_limit.py；clock/sleep 沿用项目注入风格
RateGate(policy, *, monotonic=time.monotonic, sleep=asyncio.sleep)
RateGate.next_available_in_seconds() -> float  # 同步、非预约、返回非负估计
await RateGate.permit(*, timeout_seconds: float) -> float  # 成功返回等待秒数

# concurrency.py；现有 get_* capacity 方法保留，register_rate 为新增方法
ProviderQuotaManager.register_rate(
    namespace: Literal["web", "academic", "oa"],
    name: str,
    gate: RateGate,
) -> None
ProviderQuotaManager.get_rate(
    namespace: Literal["web", "academic", "oa"],
    name: str,
) -> RateGate | None
~~~

`permit` 只在成功原子登记后返回；内部使用专用 `RateLimitWaitTimeout`（只含安全时间数值），由 HTTP 边界翻译成 `ExecutionFailure(ALL_PROVIDERS_FAILED, reason="rate_limit_wait_timeout")`。无 policy 可快速返回 0。直接构建旧 executor/scheduler 的测试允许没有 gate。

Fetch 首发传递统一使用 `InitialRateBudget(remaining_seconds: float, waited_seconds: float)`、`initial_rate_budget(gate, *, remaining_seconds, waited_seconds)` 上下文管理器，以及 `consume_initial_rate_budget(gate) -> InitialRateBudget | None`。只在同一 task、同一 gate 的首个 executor 进入许可等待时消费一次；ContextVar scope 退出后 reset。remaining_seconds 是实际 rate 等待的余额，不是绝对时刻；waited_seconds 是调度器已发生的 rate 等待，不含纯 capacity 等待或另一 provider 的执行。详情见 Task 6。

### 1.2 输入与 HTTP 合同

~~~python
# input_limits.py
TextTruncation(text: str, original_bytes: int, sent_bytes: int, truncated: bool)
BodySelection(message_index: int, head_end: int, tail_start: int)
PreparedPreview(content: bytes | None)
PreparedInput(
    content: bytes,
    original_bytes: int,
    input_truncated: bool,
    preview_body: PreparedPreview | None = None,
)

truncate_utf8_head_tail(text: str, max_bytes: int) -> TextTruncation

# 只读 Mapping，外部键仍只有 role/content；内部元数据绝不进入 HTTP JSON
TruncatableMessage(protected_prefix: str, body: str, protected_suffix: str = "")

PreviewProjector = Callable[
    [Sequence[ChatMessage], Sequence[ChatMessage], tuple[BodySelection, ...]],
    Sequence[ChatMessage] | None,
]

fit_messages_to_budget(
    messages: Sequence[ChatMessage],
    *,
    max_bytes: int,
    encode: Callable[[Sequence[ChatMessage]], bytes],
    preview_projector: PreviewProjector | None = None,
) -> PreparedInput
~~~

统一 helper 只裁 `TruncatableMessage.body`。普通 Mapping、system 及额外字段均受保护；未超限时保持编码完全一致，普通 Mapping 超限而没有可裁段则明确失败。Gateway 六个 builder 全部标注正文，故正常工作流不依赖从文本猜边界。

`fit_messages_to_budget` 先编码原输入，保留其总字节数；超限时从原始可裁正文构造候选，预留 marker，确定性分配正文预算，先验证最小可行候选，再用真实 encoder 寻找并保留满足 cap 的候选；不要求未经证明的全局最大。当前各 builder 只有一段正文；多个已标注消息也必须共享总预算，不能每段单独取满上限。所有候选都从原文生成，不能反复裁已经带 marker 的候选。

BodySelection 只描述被裁正文在原字符串中的 Unicode 索引，head_end 为头部保留区间右开边界，tail_start 为尾部保留区间左边界；只在准备输入时短暂使用。Task 11 的 DEBUG 专用 preview_projector 接收原 messages、实际选中 candidate messages 和这些索引，把原文敏感跨度投影到同一保留区间，再交同一 encode 生成日志专用视图。不能先把原文替换成不同长度字符串再按旧索引切，也不能搜索省略 marker 猜截断位置。普通模式不传 projector、不额外扫描或编码预览。

PreparedInput.content 始终是唯一实际传输体，所有尺寸/截断统计来自它；日志视图不能再次参与 fit 或发送。preview_body=None 表示未准备特殊视图，PreparedPreview(content=None) 表示安全准备失败、必须省略这一侧正文。输入已截断而特殊视图缺失时也省略请求预览，覆盖准备时 DEBUG 关闭而发送时开启的情况。两种对象的正文成员设 repr=False，不直接写入 LogRecord；只保留已选中内容的安全视图，不保留原 messages、被裁中段或捕获原文的 callback。

~~~python
# HttpJsonExecutor.__init__ 新增可选关键字
rate_gate: RateGate | None = None

# request_json / request_text / _request_response 新增可选关键字
prepared_body: PreparedInput | None = None
protocol_attempt: int | None = None
~~~

`prepared_body` 与非空 `json_body` 互斥；预先编码的 JSON 以 `content=prepared_body.content` 发送，并保持 `Content-Type: application/json`。其他 provider 继续通过 `json_body=` 构建。每次 attempt 使用 `AsyncClient.build_request` 保留 client 默认配置、动态 Header、params 与显式 timeout；最终发送同一个已构造 request，并以其 `content` 为日志和测试依据。

原字节数来自裁剪前的完整 LLM JSON / Agent prompt；实际发送字节数来自最终 bytes。普通 HTTP 两者相等；GET 无 body 的两者均为 0，preview 为 null。不把这些元数据作为 messages 的隐藏键或额外 JSON 字段发送。

### 1.3 预览事件合同

固定事件名 `upstream_body_preview`；transport 为 `http` 或 `agent_cli`。保留 request_id 关联，字段如下：

| 字段 | 类型/含义 |
|---|---|
| `provider`、`stage`、`endpoint`、`attempt`、`transport` | 现有安全关联信息；endpoint 去 userinfo/query/fragment，Agent 无 HTTP endpoint 用 null |
| `protocol_attempt` | 可选 int；LLM 外层协议重试编号，与原 HTTP attempt 配对区分同一逻辑调用内的发送；其他 transport 不填 |
| `request_body_preview` | string 或 null；实际发送内容经脱敏和 8 KiB 预览 |
| `request_body_original_bytes` | int；输入裁剪前完整传输体字节数 |
| `request_body_sent_bytes` | int；HTTP 实际 request.content / 已准备 Agent prompt 的长度 |
| `request_input_truncated` | bool；输入本身被裁剪 |
| `request_preview_truncated` | bool；仅日志预览被二次裁剪 |
| `response_body_preview` | string 或 null；HTTP 原始响应 / Agent 已捕获 stdout envelope |
| `response_body_bytes` | int；HTTP response.content 长度；Agent 已捕获 stdout 的原始字节数 |
| `response_preview_truncated` | bool；响应预览被裁剪或因捕获上限已不完整 |

Agent spawn 失败时 `request_body_sent_bytes` 表示准备交给 CLI 的 prompt 长度，沿用现有 `agent_process_diagnostic` 的 spawn failure 标记，不把它描述为远端已接收。Agent 原诊断中的 stdout/stderr 总计数继续保留，用于区分已观察字节与安全捕获的字节。

## 2. 官方来源复核与示例输入（31/31）

初次调查 2026-11-18；官方来源复核 2026-11-19。下表用于 Task 15 的文档/示例更新；没有数值不代表无限。所有推荐策略仅为匹配列示产品/认证条件的本地示例，实际账号更低时仍以账号规则为准。并发只保留原配置或明确技术并发建议；不从费用配额换算 RPM。

`N/T` 表示 `{ requests = N, period_seconds = T }`，T 单位秒；`I=x` 表示 `min_interval_seconds=x`；`C=x` 表示 `max_concurrency=x`。

| # | 类型 / 名称 | 官方事实与当前模式 | 示例策略 / 不确定性 | 官方来源 |
|---|---|---|---|---|
| 1 | Web / tavily | Development Key 100 RPM | 100/60 | [Rate Limits](https://help.tavily.com/articles/3240802908-rate-limits) |
| 2 | Web / firecrawl | 已认证 Free search/scrape 各 10 RPM；另有 2 个并发浏览器、超出页面任务进入服务端队列 | 共享 10/60；浏览器 worker 不直接等同在途 HTTP 请求数，见下方说明 | [官方文档仓库](https://github.com/firecrawl/firecrawl-docs/blob/main/rate-limits.mdx) |
| 3 | Web / exa | Free 普通 /search 10 QPS、/contents 100 QPS；当前两者均使用，未选择 deep search | 共享取严格的 10/1；deep 的 5 QPS 不用于当前模式 | [Billing](https://exa.ai/docs/admin/billing) |
| 4 | Web / linkup | 当前 Key 模式 Search/Fetch 每组织 10 QPS；x402 才按 IP 计数 | 共享 10/1，不从赠送查询数推算 | [Rate Limits](https://docs.linkup.so/pages/documentation/platform/rate-limits.md) |
| 5 | Web / brave | Search Prepaid 产品（含免费 credits）50 RPS | 50/1；非 Answers，非旧 Free 方案保证 | [Pricing](https://api-dashboard.search.brave.com/documentation/pricing) |
| 6 | Web / anysearch | 注册 Free 最高 10 QPS；Key 认证不代表 Pro；Pro 最高 20 QPS | Free 示例 10/1；每日免费 calls 属可消耗额度，不纳入 gate | [Pricing](https://www.anysearch.com/pricing)、[Auth](https://www.anysearch.com/docs/auth) |
| 7 | Web / tinyfish | Search 30/min + 500/hour；Fetch 150 URLs/min + 1000/day；当前一次 Fetch 一个 URL | 共享 30/60 + 500/3600；日上限的处理见下方来源差异说明 | [Pricing](https://www.tinyfish.ai/pricing) |
| 8 | Web / parallel | 当前 keyed Build API search/extract 各 600/min；免费 Launch MCP 为 variable | 条件化 600/60，仅适用于 Build API（含免费使用量） | [Pricing](https://parallel.ai/pricing) |
| 9 | Web / brightdata | 未充值 Free Tier 的 SERP/Web Unlocker/Proxy 默认共享 1000 RPM；充值后移除此限制 | 当前 Search/Fetch zone 对应 SERP/Web Unlocker 时共享 1000/60，保留账号/zone 条件 | [Free Tier](https://docs.brightdata.com/general/account/billing-and-pricing/free-tier) |
| 10 | Web / scrape_do | Free 主同步 API 5 并发；当前不是 Async 队列 API | C=5；无 RPM 数字 | [Pricing](https://scrape.do/pricing/) |
| 11 | Web / zenrows | Free 5 并发 | C=5 | [Pricing](https://www.zenrows.com/pricing) |
| 12 | Web / decodo | 免费 10 req/s；当前是 Basic 的旧 /v2/scrape 兼容接口 | 10/1，明确旧产品/账号条件，不改 endpoint | [旧集成说明](https://decodo.com/Scraping/web)、[旧端点示例](https://decodo.com/scraping/web/rss-scraper-api) |
| 13 | Web / scrapingdog | Free 1 并发 | C=1 | [Pricing](https://www.scrapingdog.com/pricing/) |
| 14 | Web / scrapegraphai | Free 10 requests/min | 10/60；不套 crawl 并发 | [Pricing](https://scrapegraphai.com/pricing) |
| 15 | Web / scraperapi | Free 5 concurrent connections | C=5 | [FAQ](https://docs.scraperapi.com/resources/faq/plans-and-billing) |
| 16 | Web / scrapingant | Free 单 active request | C=1 | [v2 Errors](https://docs.scrapingant.com/errors) |
| 17 | Web / serpapi | Free throughput 50/hour | 50/3600，本地全部尝试计数；不宣称官方小时只计成功 | [Pricing](https://serpapi.com/pricing) |
| 18 | Web / jina | 当前无 Key Reader 20 RPM；Key 模式是另一档 | 20/60，维持当前匿名 Reader | [Reader](https://jina.ai/reader/) |
| 19 | Web / yuanbao | 产品 FAQ：极速/标准 20 QPS、旗舰 5 QPS；当前服务 Key 兼容请求只发 Query，版本由已开通账号决定 | 账号版本未知时保守 5/1；确认极速/标准可用 20/1；不新增版本配置 | [产品 FAQ](https://cloud.tencent.com/document/product/1806/121804) |
| 20 | Web / zhihu | 官方有账号配额和超频错误说明，未确认统一公开请求频率 | 固定频率留空；限免日额度不纳入 gate | [Quota](https://developer.zhihu.com/docs?key=quota) |
| 21 | Web / doubao | Custom Search 默认账号 10 QPS；Fetch 数值按控制台 | 10/1 只作为已知 Search 约束的共享示例；不能保证覆盖未知 Fetch 更低值 | [Custom](https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh)、[Fetch](https://docs.volcengine.com/docs/networked-search/doubao-search-web-analysis-fetch?lang=zh) |
| 22 | Web / youcom | Self-serve search/contents 各 10 RPS | 共享 10/1 | [Rate limits](https://you.com/docs/rate-limits) |
| 23 | Web / keenable | 认证组织默认 10 RPS，组织所有 Key 与 Search/Fetch 共享；Key 解除小时上限 | 共享 10/1，不添加匿名入口的 1000/hour/IP | [Rate limits](https://docs.keenable.ai/rate-limits) |
| 24 | Web / open_websearch | 自托管；外部引擎策略不同 | 留空，不增加子引擎调度 | [官方项目](https://github.com/Aas-ee/open-webSearch) |
| 25 | Academic / arxiv | legacy API 每 3 秒一次、单连接 | I=3，C=1 | [API ToU](https://info.arxiv.org/help/api/tou.html) |
| 26 | Academic / semantic_scholar | Key 初始 1 RPS；匿名共享池无独享保证 | 1/1；标清 Key 模式，匿名时仅为本地保守值 | [API](https://webflow.semanticscholar.org/product/api) |
| 27 | Academic / openalex | Free Key 普通请求 30 RPS；>5 Boolean operators 搜索 2 RPS 且不允许突发 | 普通 30/1；复杂查询需 I=0.5，见下方说明 | [Authentication](https://help.openalex.org/api/authentication/) |
| 28 | Academic / dblp | 未公开固定硬阈值；官方建议相邻请求间隔 1–2 秒并遵守 Retry-After | 固定窗口留空；可另注 I=2 的礼貌访问建议，不写成官方硬上限 | [Search API](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html)、[爬取建议](https://dblp.org/faq/Am%2BI%2Ballowed%2Bto%2Bcrawl%2Bthe%2Bdblp%2Bwebsite.html) |
| 29 | Academic / crossref | Public /works list 1 RPS/1并发；有 mailto 的 polite 3 RPS/3并发 | 保守 1/1，C=1 | [官方调整](https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/) |
| 30 | Academic / core | 10秒5 single 或1 batch；/search/works 分类仍待确认 | 1/10 是本地最保守示例，不声称该端点官方确定值 | [API](https://core.ac.uk/services/api) |
| 31 | OA / unpaywall | /v2/:doi，100000 calls/day 技术要求，email 必需 | 可选 100000/86400；保留 disabled 状态 | [API](https://unpaywall.org/api)、[新版说明](https://data.unpaywall.org/products/api) |

### 2.1 相对原 spec 调查表的来源差异处理

本轮不改已提交的三份 spec；Task 15 在用户文档和配置说明中落实来源纠正，并在架构调查节注明复核结果，不改产品架构。

- **Linkup / Keenable / AnySearch / Bright Data / 元宝：**2026-11-19 补查官方限流专页、免费层说明及产品 FAQ，五项由“未知”纠正为上表的条件化确定值。Task 15 必须同步来源表和现有 provider 的示例字段，不再将它们归入未知集合。元宝请求 Mode 是返回类型，不是极速/标准/旗舰选择器；不新增未设计的产品版本参数。
- **Firecrawl / Exa：**Firecrawl 的 2 个浏览器表示服务端并行处理页面的容量，超出任务排队，不能声称本地 max_concurrency=2 精确管理其内部 worker；本次保留请求级 CapacityGate，不新增远端 job 配额控制。Exa /contents 的 100 QPS 与普通 /search 的 10 QPS 合并为同 provider 10/1。
- **Scrape.do：**补充已核实的同步 API 免费 C=5；Async 的独立 C=2 不适用于现有 adapter。
- **TinyFish：**原表遗漏 Fetch 的 1000 URLs/day。官方明确 TinySearch/TinyFetch 不扣 Wallet，且把这些约束描述为免费检索的 rate/daily limits。因此完整调查必须列出；本计划把它作为请求次数上限，可用第三个共享窗口 `1000/86400` 保守覆盖，和 Unpaywall 的日请求窗口同类，不引入付费余额管理。固定日重置与本地滑动日窗口不同，本地更保守。这项是配置示例补全，不能说原先 500/hour 已涵盖日上限。[官方说明](https://www.tinyfish.ai/pricing)
- **Parallel / Brave / Decodo：**数值须绑定当前 API 产品与认证条件。附有免费用量的 API 方案与完全免费的 MCP 方案不混写；不将赠送调用额度、credits、月度使用量或美元等费用配额加入 RateGate；官方明确的请求频率、技术并发及日请求上限按本节处理。
- **Doubao：**明确 Fetch 的固定值未公开核实。同时启用 Search/Fetch 须使用可调用 Fetch 的按量后付费 API Key，Search 的订阅套餐 Key 不支持 Fetch。若同一账号控制台给出更低值，用户应调整同一共享策略；本次不增加账号探测或每端点 gate。示例注释标明此证据限制，不能宣称对未知端点完全保证。
- **OpenAlex：**Gateway 将原 query 送入 works search，可能包含复杂 Boolean。完整说明保留普通查询 30/1，并提供 `min_interval_seconds=0.5` 的共享保守配置来覆盖当前接口可表达的 >5 operator 查询；示例默认采用该间隔以符合“采用更严格要求”的既定规则。不调用 semantic search、不添加宽 topic filter，也不将其专属限制引入当前 adapter；不处理新账号美元预算节奏。[官方请求限制](https://help.openalex.org/api/authentication/)
- **SerpApi / CORE：**分别区分 hour throughput 与成功计费统计、本地保守值与端点确定值。知乎、dblp、自托管 open_websearch 仍没有可确认的统一固定请求频率；豆包 Fetch 和 CORE /search/works 分类继续明确证据边界。

以上调查覆盖 31 个服务，不等于 31 家真实调用都已测试，也不等于每家都有可公开确认的固定免费数值。

## 3. 实施方式与依赖顺序

执行顺序为 Task 1–16。共享核心文件按序修改，不并行抢改 config/runtime/http/observability。每个 Task 的用例表是多个小循环：先完成第一行的 RED → GREEN → REFACTOR，再处理下一行；不要先写整组实现再补测试。

每次 RED 必须确认失败来自目标行为缺失。新增模块/符号若使测试无法收集，可先补最小声明骨架让测试运行，再重新确认行为断言失败；导入错误、语法错误、环境失败不能充当有效 RED。各任务列出的命令是定位行为的快速验证。按已加载的 TDD 要求，每个 GREEN（Step 4）及 REFACTOR（Step 5）完成时，相关测试通过后还要运行完整离线 suite：`env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -v`，预期全部离线用例 PASS、live 用例 SKIP。此处统一列一次以免每项重复命令；Task 16 另保留最终格式、类型、文档、构建和完整回归门槛。

所有测试环境使用合成文本、假密钥、tmp_path、MockTransport、现有假 CLI。不要让测试依赖用户真实配置或实际时钟等待一分钟/一小时。


### Task 1: 解析并隔离提供商级速率配置

**Files:**

- Modify: `src/agent_search_gateway/config.py`（`_WEB_SHARED_KEYS`、`_ACADEMIC_SHARED_KEYS`、三个 resolved provider 类型与解析函数）
- Test: `tests/unit/test_config_web_providers.py`
- Test: `tests/unit/test_config_academic_providers.py`
- Test: `tests/runtime/test_new_web_runtime_assembly.py`

- [x] **Step 1: Write the failing test**

先写 `test_web_provider_resolves_multiple_rate_windows_without_adapter_options`：以已有有效 Web 配置加入两个窗口和间隔，调用真实 resolver，断言得到 immutable policy，options 保持原 adapter 字段。依次追加：

| 场景 | 预期 |
|---|---|
| Web / Academic / OA 缺省、空数组、单/多窗口、仅 interval | 缺省为空 policy；正常值保持原顺序与数值 |
| 非数组、窗口非对象、缺必需键、未知键、嵌套错 | CONFIG_ERROR，错误含安全字段路径 |
| requests 为 0、负数、浮点、bool、字符串 | 拒绝；bool 不能当 int |
| 秒数为 0、负数、bool、NaN、Inf、无法表示的巨大数或字符串 | 拒绝；采用已有 finite-positive 语义 |
| enabled/disabled、未知 disabled provider | 保留既有启用和未知项处理，不意外要求禁用项密钥 |
| provider 自带 options 与共享新键共存 | 新键不传给 factory，不修改 registry.allowed_config_keys |
| 原构造调用未传 rate_policy | 仍可构建，旧配置保持无固定本地速率 |

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_web_providers.py -k rate -v
~~~

预期：断言速率 policy 尚未解析，或现有 options 校验拒绝新共享键；确认不是 fixture 缺 Key 造成的失败。

- [x] **Step 3: Describe the minimal implementation**

~~~text
新增 frozen ResolvedRateWindow / ResolvedRatePolicy
共享解析函数取得 provider.rate_limits，逐项验证两键且没有未知键
复用 _positive_int / _positive_float 的 bool 与有限数规则；对新 rate 秒数字段额外捕获极大整数触发的 OverflowError，转安全 ConfigFailure
把窗口存为 tuple；min_interval_seconds 缺省为 None
Web / Academic / OA resolved 对象末尾追加默认 rate_policy
先消费共享字段，再生成 adapter options
沿用现有 disabled 分支与凭据检查顺序
~~~

错误不插入原始 table、窗口对象或环境值。仅给字段路径、需要的类型/范围。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/runtime/test_new_web_runtime_assembly.py -v
~~~

预期：新用例与三个文件已有用例全部 PASS；不同 adapter 不收到额外 kwargs。

- [x] **Step 5: Refactor (keep tests green)**

仅抽取三个配置组的重复 rate 解析；不重写整个 config 模块。重跑 Step 4，确认旧错误行为仍在。

### Task 2: OpenAlex required Key 与 Bearer 传输

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/defaults.py`
- Modify: `src/agent_search_gateway/providers/academic/openalex.py`
- Test: `tests/providers/academic/test_openalex.py`
- Test: `tests/providers/academic/test_registry_defaults.py`
- Test: `tests/unit/test_config_academic_providers.py`
- Test: `tests/runtime/test_academic_runtime_assembly.py`
- Test: `tests/runtime/test_runtime_assembly.py`
- Test: `tests/orchestrators/test_paper_search_pipeline.py`
- Docs: `config.example.toml`（本任务仅添加使 enabled OpenAlex 可解析的环境变量名称，完整文档在 Task 15）

- [x] **Step 1: Write the failing test**

先写 `test_enabled_openalex_requires_api_key_environment_name`。用此前可启动的匿名 OpenAlex 配置，预期 CONFIG_ERROR 且 HTTP 调用数为零。随后分别覆盖缺失/空环境值、disabled 无 Key、合法 Key 和可选邮箱。

Adapter 用 MockTransport 捕获真实请求，断言：

- `Authorization` 为假 Key 的 Bearer；URL 中无 Key，原 `search`、`per_page=10`、可选 `mailto` 保留。
- 401、403 各只发一次，绝不匿名重发；429 保持既有 retry，Task 5 再联合 gate 验证。
- 成功 fixture 的论文映射不变；非法 JSON/数据维持已有 ProtocolFailure。
- 假 Key 不出现在异常、repr 或最终日志。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_academic_providers.py -k openalex -v
~~~

预期：匿名 enabled 配置当前未拒绝；不是网络或环境依赖失败。

- [x] **Step 3: Describe the minimal implementation**

~~~text
将 OpenAlex registration.authentication 改成 required
复用 config 的 required SecretValue 路径；保留 contact_email optional
OpenAlexProvider.__init__ 增加 keyword-only required api_key: SecretValue
仅在 Header 构造处调用 api_key.reveal() 取得真实值并发送 Bearer；不能把 SecretValue.__str__ 的 <redacted> 当 Key
params 仍只含搜索/页大小/可选邮箱
沿用 runtime 对 academic factory 已有的 api_key 注入，不写 OpenAlex 特判
更新已有匿名构造 fixture 为假 SecretValue / 假 environ
~~~

必须迁移 registry exact tuple 断言，以及 runtime_assembly、academic_runtime_assembly、paper_search_pipeline 中的 enabled OpenAlex fixture；不通过禁用 OpenAlex 规避它们原本验证的行为。示例使用 `api_key_env` 引用名为 `OPENALEX_API_KEY` 的环境变量，不读取或写入真实 Key。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/academic/test_openalex.py tests/providers/academic/test_registry_defaults.py tests/unit/test_config_academic_providers.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_runtime_assembly.py tests/orchestrators/test_paper_search_pipeline.py tests/docs/test_documented_config.py -v
~~~

预期：required/disabled 分支和全部原映射 PASS；其余 Academic 成功结果不被单家认证失败删除。

- [x] **Step 5: Refactor (keep tests green)**

仅合并重复的假 Key fixture；继续通过既有 SecretValue 注册脱敏。重跑 Step 4，不改公开错误码。

### Task 3: 配置独立输入上限与安全失败原因

**Files:**

- Modify: `src/agent_search_gateway/config.py`（`ResolvedConfig`、`resolve_config`）
- Modify: `src/agent_search_gateway/providers/diagnostics.py`（`_FAILURE_REASONS`）
- Test: `tests/unit/test_config_llm.py`
- Test: `tests/unit/test_config_agent.py`
- Test: `tests/observability/test_provider_diagnostics.py`

- [x] **Step 1: Write the failing test**

先写 `test_input_limits_default_to_128_kib_llm_and_96_kib_agent`，调用真实 `resolve_config` 后读取默认输入预算。追加分别覆盖自定义一个字段、两个字段、非对象/未知键及 bool、0、负值、非整数。

用真实 `ExecutionFailure(reason=...)` 检查 `safe_failure_reason` 保留 `input_budget_unfit` 和 `rate_limit_wait_timeout`，而未知 reason 仍被现有兜底规则过滤。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_llm.py -k input_limits -v
~~~

预期：配置尚未解析预算，行为断言失败。

- [x] **Step 3: Describe the minimal implementation**

~~~text
新增 ResolvedInputLimits，两个正整数默认值固定为 131072 / 98304
在 ResolvedConfig 末尾追加默认 input_limits，保留旧构造方式
resolve_config 仅消费 input_limits 中两个约定键，未知键 ConfigFailure
不向 llm provider.extra_body 或 agent provider 配置透传这些键
在既有安全原因 allowlist 添加两个固定字符串
~~~

数值配置错误不要默默回退默认。输入拒绝使用已有 `ExecutionFailure`；不新建公开 ErrorCode。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_config_llm.py tests/unit/test_config_agent.py tests/observability/test_provider_diagnostics.py tests/doctor/test_config.py -v
~~~

预期：默认、自定义和非法值 PASS，doctor 仍是无网络配置检查。

- [x] **Step 5: Refactor (keep tests green)**

复用已有正整数校验和安全 reason 机制；重跑 Step 4。不要引入模型 token 配置。

### Task 4: 实现可控时钟下的 RateGate

**Files:**

- Create: `src/agent_search_gateway/rate_limit.py`
- Create: `tests/runtime/test_rate_gate.py`
- Modify if needed: `tests/support/controlled.py`

- [x] **Step 1: Write the failing test**

先写 `test_rate_gate_waits_for_oldest_request_in_sliding_window`：注入可控 monotonic/sleep，1/1s 在 t=0 成功一次，第二个 coroutine 在 t<1 不完成，t=1 后完成。断言真实许可时间而不是“sleep mock 被调用过”。

按下面顺序每行独立完成一个 TDD 循环：

| 用例 | 关键断言 |
|---|---|
| 10/1s、100/60s 的容量和边界 | 不提前放行，不出现固定窗口边缘突发 |
| 30/60s + 500/3600s、第三个 1000/86400s | 所有窗口同时满足；时间队列按各窗口独立过期 |
| 仅 min_interval=3，及 interval 与窗口并存 | 发送间隔和所有窗口均满足 |
| 20+ 个 coroutine 同时竞争 1/1s | 每个成功许可唯一登记，不能双花；不假定 FIFO |
| 空 policy、不同 provider gate | 空 policy 无额外等待，A 不阻塞 B |
| 等待中取消 / 许可后取消 | 前者不登记，后者保留计数；无悬挂 waiter |
| timeout=.5，但最早许可需 3600s | .5 到期专用超时，许可数不增加 |
| 多次 next_available 查询 | 不登记、不推迟未来许可，返回非负值 |
| 到期边界、早/迟唤醒、clock 从 0 开始 | 重查条件，last_sent 用 None 区分首次；预算 .5s、窗口 1s、迟到 1.2s 才唤醒仍必须超时，不能预约 |

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_rate_gate.py -k oldest_request -v
~~~

预期：缺少滑动窗口等待行为。新增符号先满足可收集要求，再确认发送时间断言为 RED。

- [x] **Step 3: Describe the minimal implementation**

~~~text
每个窗口拥有 deque，保存成功预约的 monotonic 时刻；另有 last_sent=None
permit 开始时记录 started 与 deadline=started+timeout_seconds
循环：
  进入 asyncio.Lock
  首次 probe 可立即检查；已经等待过且 now > deadline 时先超时，不因窗口已开放而放行
  对各队列弹出 timestamp <= now-period 的过期项
  计算每个满窗口的最早释放时间及 min_interval 的剩余时间
  若全部就绪且未迟于截止：一次性向所有窗口追加 now，更新 last_sent，返回 waited_seconds
  now == deadline 时只允许无需再等的许可；仍受限则超时，不追加
  退出锁，await injected_sleep(min(需要的等待, deadline-now))
  醒来重新判断，不因早唤醒假定许可有效
next_available 仅同步读取当前 event-loop 内状态、计算估计，不预约且不清除许可
~~~

所有可变状态仅在同一 event loop 使用，permit 的读写在锁内完成；同步 ready-in 方法不 await，不宣称线程安全。等待不持锁，取消直接传播。不要给 gate 添加退款接口；每个窗口只保留其必要历史。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_rate_gate.py tests/runtime/test_quota_manager.py -v
~~~

预期：全部 PASS，没有真实分钟/小时 sleep，取消后没有 pending tasks。测试使用可控事件推进时钟，不能让多个 sleeper 叠加推动同一假时钟造成假通过。

- [x] **Step 5: Refactor (keep tests green)**

抽取同一份等待计算供 permit 和 ready-in 使用；保持只读查询无预约。重跑 Step 4。

### Task 5: Runtime 配额共享与每次 HTTP attempt 限速

**Files:**

- Modify: `src/agent_search_gateway/concurrency.py`
- Modify: `src/agent_search_gateway/runtime.py`
- Modify: `src/agent_search_gateway/providers/http.py`
- Modify if needed: `src/agent_search_gateway/academic/enrichment.py`（仅安全原因选择性透传）
- Test: `tests/runtime/test_quota_manager.py`
- Test: `tests/runtime/test_runtime_assembly.py`
- Test: `tests/runtime/test_academic_runtime_assembly.py`
- Test: `tests/runtime/test_new_web_runtime_assembly.py`
- Test: `tests/providers/test_http_executor.py`
- Test: `tests/providers/web/test_zhihu.py`
- Test: `tests/academic/test_oa_enrichment.py`

- [x] **Step 1: Write the failing test**

先写 `test_runtime_search_and_fetch_share_one_rate_gate`：真实 Runtime + MockTransport，Search 用完 1/1s 后 Fetch 不可提前发出，同时另一个 provider 能发送。

追加：

- Web、Academic、OA 各自命名空间独立；同名 LLM/Agent 完全不受 rate 影响。Runtime 时序测试仅在构造边界注入/monkeypatch clock 与 sleep，仍使用真实 RateGate，不 mock permit 的结论。
- Zhihu 两个并行 HTTP 分支受同一个 gate，不能一条逻辑 search 只计一次。
- 429 + Retry-After=2 → 200：两次请求都预约，本地 gate 许可与服务端最早时间同时满足；不把两个已重叠的等待机械相加。
- 408、5xx、TransportError、Timeout、现有 retryable HTTP-200 inspector 的每个重试均重新预约；非重试 401/403 仍一次。
- 等待窗口需一小时、timeout=.5：无 HTTP、无 http_attempt_started、无 body preview，终态 reason 为 rate_limit_wait_timeout，max_attempts 不被本地等待重新启动。
- 许可前取消不发送，许可后 transport 取消保留计数；原 retry sleep 取消行为不变。
- 等待事件与网络事件单独计时；等待/获准分别断言 estimated_wait_ms/waited_ms，http_attempt_started 位于真正 send 前。动态 Header factory 在限速放行后才调用，确保 Zhihu X-Request-Timestamp 不因排队过期。
- Task 6 到位后，scheduler 已等待 .4s、executor 再等待 .1s 时，获准/超时诊断累计约 .5s，HTTP elapsed_ms 不含排队。period_seconds=1e308 的诊断不得因秒数乘 1000 溢出而替代 rate_limit_wait_timeout。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_runtime_assembly.py -k share_one_rate_gate -v
~~~

预期：Fetch 当前可立即发送，时序断言失败。

- [x] **Step 3: Describe the minimal implementation**

~~~text
Runtime 为每个启用 Web/Academic/OA provider 建一个 RateGate
通过 register_rate(namespace,name,gate) 保存引用并注入该 provider 的 executor
Web 双能力 adapter 继续只建一次；LLM executor 不传 rate_gate

HttpJsonExecutor 的 before_attempt 仅更新当前 attempt 序号
将原 client.request 等价拆成 client.build_request + client.send，保留原 json_body 通道
operation 先构建本次 body 与静态 Header；动态 Header factory 暂不执行
operation 在每次发送前：
  消费匹配 gate 的 InitialRateBudget；没有时用完整 request_timeout_seconds 与 waited_seconds=0
  记录本段 rate 等待开始；必要时写 rate_limit_waiting 安全元数据
  await gate.permit(timeout_seconds=remaining_seconds)
  超时转非 retryable ExecutionFailure，rate_limit_wait_timeout 记录先前与本段实际等待之和
  成功写 rate_limit_acquired，waited_ms 同样采用两处实际 rate 等待累计值
  此时才调用动态 header factory 并更新本次 request 的 headers
  重新设 attempt_started=monotonic，写 http_attempt_started
  执行原 HTTP 请求（真正网络 timeout 使用完整 request_timeout_seconds）
按既有 inspector/status/retry 路径处理响应
~~~

不能把异步 gate 放进 `retry_async.before_attempt`：该回调同步且异常会被压制。local gate timeout 不能归入 transport failure，不能由 retry controller 再等一次。保留动态 headers、allowlisted diagnostics、Retry-After、jitter 和状态分类。日志秒转毫秒提取复用当前 on_retry 对超大有限数值的安全分支（先转整数再乘），不能对 1e308 直接 int(seconds * 1000)；estimated_wait_ms、累计 waited_ms 与 timeout 诊断采用同一规则。

`enrich_paper_records` 继续捕获 resolver 的 ExecutionFailure 并保留论文；如果旧日志固定 `resolver_failed`，仅为新的安全 rate reason 透传 allowlist 值，不扩大异常文本输出。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime tests/providers/test_http_executor.py tests/providers/web/test_zhihu.py tests/providers/academic/test_openalex.py tests/academic/test_oa_enrichment.py tests/unit/test_retry.py -v
~~~

预期：每次真实 HTTP 尝试计数，原 attempt 上限与错误分类 PASS；直接构建无 rate_gate executor 的旧测试不变。

- [x] **Step 5: Refactor (keep tests green)**

仅抽取等待诊断的重复代码；不新增 adapter 自有 retry 或 rate loop。重跑 Step 4。

### Task 6: FetchScheduler 同时考虑速率与并发，且等待有界

**Files:**

- Modify: `src/agent_search_gateway/scheduler/fetch.py`
- Modify: `src/agent_search_gateway/rate_limit.py`（首发等待余额的 task-local scope）
- Modify: `src/agent_search_gateway/providers/http.py`（消费首发余额并累计诊断）
- Modify: `src/agent_search_gateway/runtime.py`（注入现有 request timeout）
- Test: `tests/scheduler/test_fetch_capacity.py`
- Test: `tests/scheduler/test_fetch_outcomes.py`
- Test: `tests/orchestrators/test_url_fetch_singleflight.py`
- Test: `tests/orchestrators/test_url_fetch_admission.py`

- [x] **Step 1: Write the failing test**

先写 `test_fetch_prefers_rate_ready_provider_over_earlier_limited_provider`：A 有并发空位但需等 3600s，B 立即可发；B 成功时 A 的 HTTP 调用为零。随后逐项完成小 TDD 循环：

| 场景 | 预期 |
|---|---|
| A rate ready 但 capacity 满，B capacity 空但 rate 等待 | 等 A capacity 事件或 B rate timer 的较早者，不被 B 空闲 capacity 反复唤醒 |
| 所有候选实际等 rate 超过 .5s（含 event loop 迟唤醒） | 先判该段等待超时再看当前 ready；候选耗尽明确结束，零 HTTP/preview，failures 保留 rate_limit_wait_timeout |
| scheduler 已实际等 .4s，剩余 .1s 内 ready 被竞争者抢走 | 首发 HTTP permit 只余 .1s，不能再得到完整 .5s |
| A 在 t=.1 被观察为 rate ready，之后只等 capacity 到 t=.8；预算 .5 | 只扣 .1s，仍可发送，不能套首次受限以来的永久截止 |
| A 受限而 B 立即选中，B 执行 HTTP/judge .8s 后失败，A 此时 ready | A 不因 B 的执行耗时报 rate timeout |
| 先有 cheap_check/judge 语义拒绝，其余候选全 rate timeout | 明确耗尽并保留原 semantic_failure 优先终态，同时保留限流 failures |
| judge 调用抛 ExecutionFailure，与 judge 返回拒绝分别测试 | 前者沿原边界向上传播，后者继续 fallback，不合并为一种失败 |
| 取消、普通 provider 失败、候选耗尽 | 所有内部 wait tasks cancel 后 await，CapacityLease 一次释放，无空集合忙循环 |
| scheduler 等 .4s + executor 等 .1s | rate_limit_acquired/timeout 累计约 500ms，HTTP 网络 elapsed_ms 单独计时 |
| 新用户请求再尝试之前失败 provider；纯等待 capacity | 失败集合/余额仅属本次 fetch，原纯并发等待语义保持 |

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/scheduler/test_fetch_capacity.py -k rate_ready_provider -v
~~~

预期：当前按 capacity 的选择先进入 A，无法立即通过 B 完成。其他时间线的 RED 分别证明实际多扣等待、候选耗尽不收尾或错误分类错误。

- [x] **Step 3: Describe the minimal implementation**

~~~text
每个本次 fetch 候选保存 remaining_seconds=T、waited_seconds=0
每轮查询非预约 ready-in，按配置顺序给 rate ready 候选尝试 capacity lease
有可选者立即返回 provider、lease、它的 InitialRateBudget，不为其他候选启动计时
没有可选者时：
  remaining 已空则明确返回耗尽；外层合并 selector 收集的 failures 后 break
  快照 blocked={当前 ready-in>0 的候选} 和本轮实际挂起起点 s
  对 blocked 创建最早 rate-ready/余额截止 timer，写安全 rate_limit_waiting
  只对 rate ready 但 capacity 满者创建 capacity 状态 waiter
  await FIRST_COMPLETED；所有退出路径 finally cancel 并 gather 全部内部任务
  醒来 t：仅对本轮 blocked 扣 t-s，累计 waited_seconds
  先淘汰 t>s+本轮余额者，即使迟唤醒时已 ready；恰好截止仍受限也淘汰
  生成带 reason 的 ExecutionFailure、写累计等待诊断、从本次 remaining 移除
  重新评估；观察到 ready 后的纯 capacity 等待不再扣 rate 余额
选中 provider.fetch/judge 执行期间，所有未选候选均没有活动中的 rate 等待片段
outer 保留原 accepted / semantic_failure_seen / execution_failure 汇总顺序
~~~

内部 selector 返回协议必须显式表达“选中”或“耗尽”，并携带本轮收集的 failures，可用小结果类型或等价明确元组。不能仍按旧接口无条件解包 `(provider, lease)`，也不能移除最后候选后调用空列表 waiter 或 `asyncio.wait([])`。现有 `wait_until_any_web_available` 对空集合立即返回、取消分支只 cancel 子任务；本任务直接为需要的 CapacityGate 建可用性 waiter，并在同一 finally 中 cancel+gather，避免未清理的嵌套等待者。

首发预算只采用 §1.1 的 `initial_rate_budget(gate, *, remaining_seconds, waited_seconds)` 与 `consume_initial_rate_budget(gate)`。取得 lease 后包住该 provider 调用；executor 进入匹配 gate 的首个许可等待时消费一次，以与 gate 同源的 monotonic 建立本段截止。传秒数余额，不传先前已暂停的绝对 deadline。余额为零时只允许无需等待的原子许可，不能重给完整 T。后续实际 HTTP retry 使用新的完整等待预算；Retry-After 不计入本地 rate 等待。scope finally reset，不在共享 gate 上存请求余额。

scheduler 的 `rate_limit_waiting` 带 provider、stage=fetch、预备 attempt=1、安全 estimated_wait_ms。获准事件由 executor 唯一输出，waited_ms 为 scheduler 与本段 executor 等待之和，不能把两条累计值重复相加。scheduler 直接淘汰的候选没有 executor，须自行写累计 rate_limit_wait_timeout。数字转换复用 Task 5 的安全毫秒逻辑；没有实际发送不写 http_attempt_started 或 Body preview。

无 rate gate 时 ready-in=0，不增加全 workflow timeout。取消原样传播；cheap_check/judge 语义拒绝继续 fallback，judge 执行异常按当前代码上抛。计时器用可注入 monotonic/sleep，不真实等待一分钟。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/scheduler tests/orchestrators/test_url_fetch_flow.py tests/orchestrators/test_url_fetch_singleflight.py tests/orchestrators/test_url_fetch_admission.py tests/providers/test_http_executor.py -v
~~~

预期：实际限流等待有界，全候选耗尽明确结束；无空转、lease 泄漏、后台 waiter、URL 准入或 judge 分类回归。

- [x] **Step 5: Refactor (keep tests green)**

只合并等待任务创建/清理与余额结算；scope 限于首发 rate，不扩展成通用 workflow deadline 框架。重跑 Step 4。


### Task 7: UTF-8 首尾裁剪与显式 Prompt 分段

**Files:**

- Create: `src/agent_search_gateway/input_limits.py`
- Modify: `src/agent_search_gateway/llm/prompts.py`
- Create: `tests/unit/test_input_limits.py`
- Test: `tests/unit/test_llm_stages.py`
- Test: `tests/unit/test_llm_judge_stage.py`
- Test: `tests/unit/test_llm_paper_stage.py`
- Test: `tests/unit/test_agent_stages.py`

- [x] **Step 1: Write the failing test**

先写 `test_truncate_utf8_preserves_head_tail_and_counts_marker`：ASCII 正文开头/中间/尾部放不同哨兵，设小预算，断言头尾保留、中间消失、marker 一次、字节数不超限且 metadata 正确。

依次补充：

- 未超限及刚好等于预算原样返回；少 1 byte 时确定性裁剪。
- 中文、emoji、非 BMP、组合字符、换行和反斜杠；不拆 Unicode code point，不要求额外做 grapheme 归一化。
- 预算小于 marker、不可编码 surrogate、保护片段超限均返回安全内部 `InputBudgetUnfit`，不泄漏原文。
- 原文自带相同 marker 不影响 `truncated` 的计算。
- 合成 2 MiB 正文和多个可裁消息，共享总预算；含一个短转义段与一个长正文的边界用例，不能因 marker 切换的非单调长度误判不可适配；有限编码次数，无递归无限收缩。
- 六个 builder 对未裁输入 `dict(message)` 的 role/content 与现有文本完全相同。
- Focus 值本身、固定 `Candidate content:`、`Final content:`、`Page content:` 等模板被保护；正文包含这些字符串不会改变分段。
- 普通 Mapping 的额外键原样保留；未标注内容不被猜测性裁剪。
- 被裁正文的 BodySelection 使用原正文 Unicode 索引，与实际保留头尾准确对应；原文同名 marker 不构成切口。日志视图接口按 §1.2 保留默认 None，敏感跨度投影在 Task 11 的安全测试后接入。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_input_limits.py -k preserves_head_tail -v
~~~

预期：裁剪/字节预算行为尚未实现，正确断言 RED。

- [x] **Step 3: Describe the minimal implementation**

~~~text
truncate_utf8_head_tail：
  先严格编码并记原 bytes，未超则原样返回
  从总预算扣掉固定 marker bytes
  按 80%/20% 分配 head/tail，在合法 code point 边界取字节
  连接 head+marker+tail，复验 <= cap；返回真实 metadata

TruncatableMessage：
  只读 Mapping，role 固定 user，content 渲染 protected_prefix + body + suffix
  仅内部属性保存分段；dict(message) 不包含元数据

fit_messages_to_budget：
  用调用方 encoder 得到 original bytes
  若在预算内返回相同 bytes
  只有明确标注的正文参与分配；其余字段完整保留
  空段或原编码不比 marker 更贵的短段先保持原样，其他段预留 marker
  先编码并验证最小可行候选，确实不适配才 InputBudgetUnfit
  从原文确定性分配预算，以相同 encoder 验证每个候选并保存最近可行值
  多段从 marker 切回原文可能非单调；不能用未经证明的二分谓词宣告无解
  搜索次数有界，找不到更好候选时返回已验证可行值
  固定最终候选及 BodySelection，记录真实 bytes / original_bytes / input_truncated
  Task 11 接入的可选 projector 只作用于日志视图，不再次 fit、不改变 transport bytes
  返回 PreparedInput，不携带原 messages、被裁原文或 callback
~~~

`llm_search_messages`、`llm_paper_search_messages` 保留已有 `.strip()`；Focus 保留已有标准化，不额外改变语义。helper 不根据 model 猜 token，也不发送 HTTP。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_input_limits.py tests/unit/test_llm_stages.py tests/unit/test_llm_judge_stage.py tests/unit/test_llm_paper_stage.py tests/unit/test_agent_stages.py -v
~~~

预期：所有原 Prompt/Mapping 契约 PASS，head/tail 与保护段在边界用例中不丢失。

- [x] **Step 5: Refactor (keep tests green)**

保持 helper 纯函数及两种 transport 所需的最小共同接口；不引入通用模板语言。重跑 Step 4。

### Task 8: LLM 真实 JSON body 的预发送预算

**Files:**

- Modify: `src/agent_search_gateway/providers/llm/openai_chat.py`
- Modify: `src/agent_search_gateway/providers/http.py`
- Modify: `src/agent_search_gateway/runtime.py`（`_build_llm_clients`）
- Test: `tests/providers/llm/test_openai_chat.py`
- Test: `tests/providers/test_http_executor.py`
- Test: `tests/runtime/test_runtime_assembly.py`

- [x] **Step 1: Write the failing test**

先写 `test_llm_sends_serialized_json_within_configured_body_limit`。真实 client + MockTransport，构造超过默认 128 KiB 的中文/emoji/JSON 特殊字符正文，捕获最终 `request.content` 并断言长度、可解析 JSON、头尾与 system 完整。

随后覆盖六种 stage、较小自定义 cap、exact boundary、小输入字节完全一致、Focus 保护、巨大 `model`/`extra_body`/system 的预发送拒绝。通过实际 HTTP request 断言元数据没有被序列化。

补充重试行为：

- protocol retry 与 HTTP retry 均复用同一个已准备 body，字节完全一致，不二次裁剪。
- 同一 complete_* 模拟 HTTP 429 → HTTP 200 但协议内容非法 → 下一协议轮 HTTP 200 合法；本任务先断言三次真实传输的 prepared bytes 完全一致。protocol_attempt 编号和 preview 配对由 Task 12 在日志功能存在后新增测试，不能在本任务要求尚未实施的预览事件。
- 已在 byte cap 内但服务端返回 context-length HTTP 400，仍按原失败分类，仅一次，不自动缩短重发。
- `prepared_body` 与 `json_body` 同时指定是明确的内部参数错误。
- 原普通 Web JSON 路径和 callable headers 每次尝试刷新仍通过。
- 输入无法压缩时 HTTP 与 quota 内操作均未开始，reason 为 `input_budget_unfit`，没有假 preview。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/llm/test_openai_chat.py -k serialized_json_within -v
~~~

预期：当前真实 body 超出预算，长度断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
_validate_invocation 仍先检查 reserved extra_body keys
每次 encoder 用本轮 candidate_messages 构建完整 payload：
  {"model": invocation.model, "messages": [dict(m) for m in candidate_messages], **invocation.extra_body}
encoder 使用 httpx.Request("POST", endpoint, json=payload).content
在 quota / protocol retry 之前调用 fit_messages_to_budget 准备一次 PreparedInput
complete_text / complete_json 与 _request_once 共用 prepared_body
Executor 用 client.build_request 生成实际请求：
  普通请求沿 json=json_body
  已编码请求沿 content=prepared_body.content 且保留 application/json
  保留显式 timeout 与 client 默认配置；动态 header factory 延迟到 rate permit 后再更新 headers
真正发送 prepared bytes，不再用另一套 JSON encoder 重建
InputBudgetUnfit 写一次无正文 input_rejected（仅安全 bytes/limit/reason），
然后转 ExecutionFailure(LLM_STAGE_FAILED, reason=input_budget_unfit)；零 HTTP preview、零重试
~~~

不能用 `len(messages_content)`、单独 `json.dumps` 的不同参数或估算引号/escape 的开销替代实际编码。转换编码异常时给固定安全信息，不把可能包含正文的原异常 repr/链作为普通日志内容。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/llm/test_openai_chat.py tests/providers/test_http_executor.py tests/runtime/test_runtime_assembly.py tests/orchestrators/test_llm_search.py tests/orchestrators/test_scoped_llm_search.py -v
~~~

预期：MockTransport 看到的真实字节全部在 cap 内；原 response parser、模型筛选及 retry 次数保持。

- [x] **Step 5: Refactor (keep tests green)**

只共享 text/json 的 body preparation；维持两层 retry 的原职责。重跑 Step 4。

### Task 9: Agent 单个 prompt 参数预算与系统上界

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Modify: `src/agent_search_gateway/runtime.py`（`_build_agent_runners`）
- Test: `tests/providers/agent/test_antigravity_cli.py`
- Test: `tests/providers/agent/test_antigravity_lifecycle.py`
- Test: `tests/runtime/test_agent_runtime_assembly.py`
- Test: `tests/unit/test_agent_stages.py`

- [x] **Step 1: Write the failing test**

先写 `test_agent_caps_complete_prompt_argument_before_spawn`：用现有假 CLI 或受控 spawn 边界捕获 argv，输入 120 KiB 混合正文，取 `-p` 后完整参数，断言 ≤98304 bytes、system/role/头尾保留。

追加自定义小 cap、保护段太大不 spawn、最终 prompt 含 NUL 不 spawn、用户 cap 超过可确定系统单参数能力、系统能力无法可靠推导、E2BIG、普通 spawn error、timeout 和取消。禁止将实际 prompt 放进 pytest 参数 id/诊断 print。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_cli.py -k caps_complete_prompt -v
~~~

预期：当前 `-p` 参数超限，实际 argv 边界断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
为 run_antigravity 增加兼容默认值的 prompt_max_bytes 关键字参数
Runtime._build_agent_runners 接收 ResolvedInputLimits 并绑定对应值
encoder 完整渲染原 [ROLE] 标签与分隔符后 UTF-8 编码
fit_messages_to_budget 得到 PreparedInput，decode 回单个 prompt 参数
在创建 subprocess 前验证受保护内容、最终参数长度及 argv 不允许的 NUL
不可适配转 ALL_PROVIDERS_FAILED + input_budget_unfit，写安全 input_rejected；不暴露 prompt 或底层 ValueError 原文
~~~

在明确 Linux 且 `SC_PAGE_SIZE` 可用时可检查单参数 `32 * page_size - 1` 的已知上界（留 NUL）；`SC_ARG_MAX` 是整体 argv/env 上界，不能当作单参数值。平台不确定时不编造统一 128 KiB 常数。用户设置 cap 超过可靠系统上界时额外检查最终实际 prompt；若实际参数也超出该上界，清楚拒绝对应调用，不静默换 cap。实际参数仍在系统上界内则允许继续。真实 E2BIG 继续分类 `argument_limit`，不重试；其他 argv/env 仍可能触发 OS 限制。

原过程 deadline、print timeout、process group、stdout/stderr 上限、临时 cwd、env allowlist 和 quota finally 保持。预览在 Task 13 添加。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent tests/runtime/test_agent_runtime_assembly.py tests/unit/test_agent_stages.py tests/orchestrators/test_agent_search_deadline.py tests/daemon/test_agent_shutdown.py -v
~~~

预期：无越界 prompt，无新增 retry；无因预检失败泄漏的 quota 或孤儿进程，原 double-cancel 用例通过。

- [x] **Step 5: Refactor (keep tests green)**

仅将既有 prompt 渲染提取为共享 encoder；保留原消息顺序。重跑 Step 4。

### Task 10: 为所有格式化日志添加 UTC 毫秒时间

**Files:**

- Modify: `src/agent_search_gateway/observability.py`（`KeyValueFormatter.format`）
- Test: `tests/unit/test_observability_logging.py`
- Test: `tests/daemon/test_daemon_debug.py`
- Test: `tests/acceptance/test_debug_and_doctor.py`

- [x] **Step 1: Write the failing test**

先写 `test_formatter_uses_record_created_in_utc_milliseconds`：固定 LogRecord.created，设置与当前时区不同的环境，断言形如：

~~~text
DEBUG time=2026-11-18T08:21:30.124Z request=0123abcd event=...
~~~

分别覆盖普通非 gateway_event 日志、毫秒/跨日边界、request_id、单物理行、原 elapsed_ms、不合法 created 的安全降级。已有两个写死 `DEBUG request=` 的测试更新预期顺序，不只检查字符串包含 time。

补 `test_rotation_preserves_timestamp_and_request_id_at_default_boundary`：tmp_path 使用生产 5 MiB / 3 backups 设置，在边界前写入固定 created/request_id 的哨兵事件；有界预填充使下一条固定记录触发 rollover。分别解析 `.1` 与当前文件的对应事件，验证两侧 UTC 毫秒、request_id、单物理行完整。现有 220 字节测试只验证备份存在，不能作为此项证据；不要用数万次日志调用制造边界。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_observability_logging.py -k record_created -v
~~~

预期：当前 formatter 缺少 time。

- [x] **Step 3: Describe the minimal implementation**

~~~text
以 datetime.fromtimestamp(record.created, UTC) 生成 ISO8601 毫秒，+00:00 转 Z
parts 依次为 level、time、request，再接已有 event/fields
time 由 formatter 保留，外部 gateway_fields 不能覆盖或产生重复 time
不读取本地时区，不使用 format 时刻替代 record.created
不改 elapsed_ms 的 monotonic 来源、field escaping、最终 redactor 与安全 handler
~~~

异常时间按现有安全 Handler 固定提示降级；不把日志格式化异常提升为业务失败。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_observability_logging.py tests/daemon/test_daemon_debug.py tests/acceptance/test_debug_and_doctor.py -v
~~~

预期：结构化和普通日志都有 time，原轮转、脱敏和 stdout 契约保持。

- [x] **Step 5: Refactor (keep tests green)**

仅提取小的时间格式化函数（确有重复才提取）；重跑 Step 4。

### Task 11: debug-only 双向 BodyPreview 安全管线

**Files:**

- Create: `src/agent_search_gateway/body_preview.py`
- Modify: `src/agent_search_gateway/observability.py`
- Modify: `src/agent_search_gateway/input_limits.py`（DEBUG 候选投影与特殊视图）
- Test: `tests/unit/test_input_limits.py`（两种实际 encoder 的投影边界）
- Create: `tests/observability/test_body_preview.py`
- Test: `tests/observability/test_redaction.py`
- Test: `tests/unit/test_observability_logging.py`
- Modify if needed: `tests/support/logging.py`

- [x] **Step 1: Write the failing test**

先写 `test_body_preview_redacts_registered_secret_before_truncating`：将已登记假 Key 放在 8192 边界附近，使未经预脱敏的裁剪会留下半截 Key，检查最终物理日志不含完整或可识别的残片。

按以下行为逐个红绿：

| 场景 | 预期 |
|---|---|
| 深层对象、数组、大小写/下划线/连字符敏感字段 | Authorization、cookie、token、api_key、password、secret、access_secret 等整个值遮蔽 |
| JSON 中字符串嵌套 URL、文本 HTML URL | 删除 URL userinfo/query/fragment；不将 URL 凭据藏在 Body 中 |
| 非 JSON 文本中的 Bearer、Cookie、Key、已登记 Secret | 预先遮蔽，最后再全行兜底 |
| 8191/8192/8193 bytes、引号/反斜杠/换行/emoji | 8 KiB 包含 marker；同时校验字段实际渲染的 JSON 转义形式不超预算 |
| 空/二进制/无效 UTF-8/不可安全脱敏文本 | 安全 null 或已验证的替换预览，保留原字节统计，不改业务响应 |
| debug=false | 不调用 body decode、结构扫描、preview encoder，事件为零 |
| helper、Formatter、Handler 或磁盘写入失败 | 业务结果不变，固定安全降级；无原文 traceback |
| 正文原含 marker 与两个截断标记 | input_truncated 与 preview_truncated 独立，不能由字符串猜状态 |
| 已登记假 secret 分别跨 LLM/Agent 的输入 head/tail 切口 | 完整值和可识别残片不进最终日志，安全正文仍可见；不只测 8 KiB 日志切口 |
| 投影失败、无安全 formatter、准备后 DEBUG 才开启 | 已截断输入请求侧为 null，实际发送/统计保持；无原文 fallback |

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/observability/test_body_preview.py -k before_truncating -v
~~~

预期：缺少安全预览/预脱敏能力；有效 RED 不得通过直接删去所有正常正文来掩盖预览功能缺失。本任务以真实 fit/encoder、preview helper 与安全 formatter 验证纯管线；HTTP/Agent 自动发事件尚未接入，其完整日志测试分别留给 Task 12/13，不依赖后续任务才能通过。

- [x] **Step 3: Describe the minimal implementation**

~~~text
observability 暴露 log_upstream_body_preview(...)：
  第一行检查 logger.isEnabledFor(DEBUG)，否则立即返回
  调用方传 lazy bytes factory 或在同一 DEBUG guard 内准备实参；stdout 复制/JSON decode 也不能提前执行
  取得该 logger 有效项目 handler/formatter 使用的 SecretRedactor
  调用 body_preview 的纯安全转换，生成有界字符串和标记
  只把已安全处理的预览和整数/布尔元数据交给 log_event
  普通异常（含 lazy bytes 获取失败）降级为 null/固定 body_preview_failed，不影响 transport
body_preview：
  对 JSON 递归处理敏感字段及字符串；对文本执行有限的凭据/URL 归一化
  使用同一个活跃 redactor 在裁剪前遮蔽已登记秘密
  80/20 头尾 + marker，按 UTF-8 和最终字段转义尺寸验证
  对不可安全处理输入 fail closed，不输出原始 fallback
输入截断的特殊日志视图：
  observability.make_input_preview_projector(logger) 仅在 DEBUG 下取得有效安全 redactor
  projector 在原正文识别已登记秘密、敏感结构、凭据/URL 跨度
  按 BodySelection 与实际保留头尾求交，在同一候选上只遮蔽交集
  保护前后缀、marker、消息顺序与保留区间保持，不恢复已丢中段
  fit 用同一个 encode 编码投影消息，保存 PreparedPreview，绝不替换实际 content
  投影/额外编码的普通异常只令 PreparedPreview.content=None，不影响请求
Formatter：
  保留最终 SecretRedactor 全行兜底，仍按 _render_string 输出单物理行
~~~

为 formatter 提供窄的只读 `redact_for_preview(text)` 方法供 helper 复用，只遍历实际生效的 logger/parent handler，并遵守 propagate=false 的停止边界；相同 redactor 不重复处理。若无已配置的安全 formatter，Body 值降级为 null，仍可输出安全计数，不能假定任意 handler 会替自己脱敏。为原正文投影提供只读 `sensitive_spans(text)`，返回敏感区间、不导出秘密注册表；先求交再替换，避免替换长度改变裁剪坐标。特殊视图仍过统一结构脱敏与 8 KiB helper，正文成员 repr=False，不假定投影识别了一切未知敏感值。不把原始 bytes、headers、argv 或环境放进 LogRecord 字段等待稍后处理。

8 KiB 使用保守计量：预览文本自身 UTF-8 ≤8192，按现有 `_render_string` 等价 JSON escaping 的字段值也 ≤8192；整行含两侧和其他元数据自然可能超过 8 KiB。marker、引号与 escape 所占空间不可漏算。不要通过裁剪 JSON 原始 bytes 再期待结构化 redaction 成功。

日志 helper 仅捕获普通 Exception；CancelledError 必须继续传播。共享安全 API 不打印 redactor 注册表，不创建新的凭据持久化文件。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/observability/test_body_preview.py tests/observability/test_redaction.py tests/unit/test_observability_logging.py tests/unit/test_input_limits.py -v
~~~

预期：正文可在 DEBUG 安全预览，秘密哨兵在全部最终日志中缺席；INFO 无预览准备成本，sink 故障不改变业务。

- [x] **Step 5: Refactor (keep tests green)**

复用现有 URL 安全化/SecretRedactor，不另造全局密钥仓库。将字节裁剪复用 Task 7 的纯函数，但预览使用自己的最终日志渲染预算。重跑 Step 4。


### Task 12: HTTP 每次实际尝试的一条双向预览

**Files:**

- Modify: `src/agent_search_gateway/providers/http.py`
- Modify: `src/agent_search_gateway/providers/llm/openai_chat.py`（日志视图与协议尝试关联）
- Test: `tests/providers/test_http_executor.py`
- Test: `tests/observability/test_provider_event_logging.py`
- Test: `tests/observability/test_provider_diagnostics.py`
- Test: `tests/observability/test_redaction.py`
- Test: `tests/providers/llm/test_openai_chat.py`
- Test: `tests/orchestrators/test_paper_search_pipeline.py`

- [x] **Step 1: Write the failing test**

先写 `test_http_preview_pairs_each_retry_with_its_own_response`：POST 429→200，启用实际安全 formatter，解析两条 upstream_body_preview，验证同一 request_id、两个 attempt、各自响应、发送体与字节数准确。在 Task 8 三次真实发送的组合基础上，本任务新增日志断言：HTTP 429 → 200 协议非法 → 下一协议轮 200 合法的 (protocol_attempt, attempt) 依次为 (1,1)、(1,2)、(2,1)，对应响应准确且全部重发字节不变；两个并发 complete_* 的外层编号不串。其他 HTTP/Agent 不填该字段。

逐项覆盖：

| 结果 | 预览数与内容 |
|---|---|
| POST JSON 成功 / GET JSON 或 text 成功 | 每次 1；GET 无 body 的 request=null、bytes=0 |
| HTTP 400/401/403、终态 5xx、HTTP 200 inspector 失败 | 每次实际尝试 1，响应仍可安全查看 |
| HTTP 200 非法 JSON / 空 body | 先有原响应预览，再走原 decode failure；不重复写 |
| TransportError / timeout / 发送期间取消 | 每次 1；无响应为 null、0、false |
| 一次真实请求后 retry 的本地 rate wait 超时 | 只保留第一次预览；不制造第二条 |
| 首次 rate wait timeout / LLM input rejection | 0 HTTP preview |
| 输入已裁 / 仅 preview 裁 / 两者都裁 / 都不裁 | 标记分别正确，原 bytes ≥ sent bytes；输入切口秘密残片经投影视图遮蔽 |
| DEBUG 关闭、helper/formatter/sink 故障 | 0 或安全降级；业务结果不变 |
| 连续 100 次小型 mock attempt | 每次一条，无跨 attempt 的 response 残留 |

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/test_http_executor.py -k preview_pairs_each_retry -v
~~~

预期：当前没有这两条成对预览事件，事件计数 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
LLM client 在 fit 调用处仅当 DEBUG 时传入 Task 11 的 make_input_preview_projector
各 complete_* 的 before_attempt 局部闭包捕获 protocol_attempt，同时保留原协议日志
该编号随本轮 _request_once 传 executor，不存共享 client 实例字段
operation 内每次独立构建 request，初始化 response=None
rate permit 已通过后、紧邻 send：
  写 http_attempt_started，记录网络开始时间
  try:
    response = await client.send(request)
  finally:
    log_upstream_body_preview(
      已截断输入使用已验证 preview_body.content，缺失/失败则 None；普通请求用实际 request.content,
      response.content 若可得否则 None,
      bytes 统计取实际 request.content，original_bytes/input_truncated 取 prepared_body 或普通默认,
      本次 provider/stage/protocol_attempt/attempt/安全 endpoint
    )
之后保留原 diagnostics、inspector、status/retry 与 completed/failed 流程
request_json 的 JSON decode 层不再次写 preview
~~~

不打印 Header、完整 Request 对象、repr(response)、原始 exceptions 或 URL query。输入裁掉的中段不再进入 helper；仅传发送内容的安全视图和统计，特殊视图缺失时不能用裸 bytes 兜底。同一 LLM 外层 protocol_attempt 在 HTTP started/completed/retrying/failed/preview 统一透传；原 attempt 含义与次数保持。原 `provider_failed` 终态仍由 owner 写一次，预览不替代它。

更新旧“整份 DEBUG 不出现任何普通正文”的断言：原事件和 INFO 仍不得携带正文，仅新增 preview 事件允许安全片段；全日志凭据、URL query、被输入裁掉的中段哨兵必须继续缺席。独立 LLM protocol logger 本身没有正文输出需求，不能顺带弱化其安全断言。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/test_http_executor.py tests/providers/llm/test_openai_chat.py tests/observability tests/orchestrators/test_paper_search_pipeline.py -v
~~~

预期：所有矩阵事件数、原字节统计和最终物理行脱敏 PASS；失败分类、Retry-After 与 attempt 上限不变。

- [x] **Step 5: Refactor (keep tests green)**

把 preview 维持在唯一发送边界，删除任何临时成功/异常分支重复 emit。重跑 Step 4。

### Task 13: Agent 失败和取消后的 stdout 预览

**Files:**

- Modify: `src/agent_search_gateway/providers/agent/antigravity_cli.py`
- Test: `tests/providers/agent/test_antigravity_lifecycle.py`
- Test: `tests/providers/agent/test_antigravity_cli.py`
- Test: `tests/observability/test_agent_event_logging.py`
- Test: `tests/observability/test_provider_diagnostics.py`
- Modify if needed: `tests/support/agent_cli.py`

- [x] **Step 1: Write the failing test**

先写 `test_agent_timeout_previews_captured_stdout_after_cleanup`：现有假 agy 先输出一个完整、可安全解析的合成 JSON envelope 再阻塞，触发 timeout，确认进程已清理、一条安全 preview 保留已捕获内容及真实字节数，并标明流尚未到 EOF。

依次覆盖 SUCCESS、非零退出、非法 envelope、空 stdout、spawn error、E2BIG、输出超限、一次/两次取消及 input preflight rejection。另将 stdout 截在 token/cookie 字符串内部，验证保留 captured bytes 而预览为 null，不把残缺 JSON 当普通文本直接打印。检查 stderr 原文、env、完整 argv、秘密哨兵从未写入日志；输入被裁中段不出现。沿用 Task 11 跨输入切口 secret 用例，不能只遮蔽完整值而遗漏残片。普通模式事件为零。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent/test_antigravity_lifecycle.py -k previews_captured_stdout -v
~~~

预期：当前 reader 异常后丢失局部 chunks，缺少所需安全 preview；原 timeout/清理本身仍正常。

- [x] **Step 3: Describe the minimal implementation**

~~~text
Agent 在 fit 调用处仅当 DEBUG 时传入 Task 11 的 make_input_preview_projector
stdout reader 将已捕获 bytes 交给调用方拥有的有界缓冲
保留 stdout 4MiB / stderr 256KiB 原硬上限与观测总计数
超限/取消不把不完整 stdout 当成功输出
已开始尝试 create_subprocess_exec 才设置 spawn_attempted
在现有 cleanup 最内 finally：
  保留 agent_process_diagnostic
  若 spawn_attempted，emit 一条 transport=agent_cli / attempt=1 的 preview
  Request 来自同一最终 prompt 的安全日志视图；已裁且视图缺失时 null；统计仍取真实 -p bytes
  Response 来自捕获 stdout
  无 stdout 则 null、0、false；部分捕获不得伪称完整终态 envelope
  超限/timeout/cancel 未到 EOF 时标记 incomplete；仅可安全处理内容给非 null preview
  不可安全脱敏的残缺 JSON 给 null，并保留原 captured bytes 与 incomplete 标志
预检拒绝在 spawn 之前，只有 input_rejected 安全诊断，零 preview
~~~

stdout 完整成功内容仍交给原 `_decode_terminal_response`；8 KiB 是日志预览限制，不是业务输出上限。已观察总字节数由原 process diagnostics 保留，response_body_bytes 按已捕获原始内容计算。若捕获因输出限制而不完整，response_preview_truncated 必须表明不完整。

不得调整原 shield、SIGTERM/SIGKILL、drain/reap、临时 cwd 清理和 quota finally 的顺序。预览同步且 best-effort，不吞 CancelledError，不增加第二次 spawn。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/agent tests/observability/test_agent_event_logging.py tests/observability/test_provider_diagnostics.py tests/daemon/test_agent_shutdown.py tests/acceptance/test_agent_search_workflows.py -v
~~~

预期：每次真实 CLI 调用或失败的 spawn 尝试一条预览，全部生命周期测试 PASS，无孤儿进程。

- [x] **Step 5: Refactor (keep tests green)**

有界缓冲只服务既有捕获与预览，不增 stderr 全文日志或新的流式协议。重跑 Step 4。

### Task 14: 工作流级验收与 opt-in live 测试入口

**Files:**

- Modify: `tests/acceptance/test_gateway_workflows.py`
- Modify: `tests/acceptance/test_agent_search_workflows.py`
- Modify: `tests/acceptance/test_debug_and_doctor.py`
- Modify: `tests/orchestrators/test_keyword_search_pipeline.py`
- Modify: `tests/orchestrators/test_paper_search_pipeline.py`
- Modify: `tests/academic/test_oa_enrichment.py`
- Create: `tests/integration/test_live_rate_limits_and_inputs.py`
- Modify if needed: `tests/support/acceptance.py`

- [x] **Step 1: Write the failing test**

优先写 `test_debug_gateway_rate_fallback_preserves_admitted_fetch_result`：本地 Runtime/daemon + Mock HTTP，Search 录入 URLStore；A 的 rate 被占、B 就绪；Fetch 经原 judge/clean/focus 返回合法结果，分别验证 Search 与 Fetch 的事件各自有稳定且不同的 request_id 及有效 time；Fetch 候选、LLM stage、HTTP preview 关联 Fetch ID。大正文触发 LLM cap，并在 preview 中仅看到最终出站体。

其他独立工作流：

- Paper Search：OpenAlex 假 Bearer Key，其他 provider 部分失败，Unpaywall 等待超时；成功论文的 DOI/URL/摘要和合并优先级保留。
- LLM/Agent Search：超过默认 cap 的合成输入，合法输出仍按原 web/paper/all schema；model/provider filtering 与空成功保留。
- DEBUG daemon 与普通 daemon：两侧预览只在前者出现；stdout 仍只输出原结果路径/最终文本。
- 显式取消实际 asyncio workflow task，以及 daemon shutdown：rate waits、retry sleeps 和 Agent 子进程均结束，既有 singleflight 不重复发请求。当前服务端不会仅因客户端关闭 socket 就自动取消已开始的 dispatch；本次不新增断连监控，也不将该行为当成已有验收契约。
- 预览 helper/sink 故障：成功结果仍成功，原业务失败仍原分类；不因日志错误改变路由。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/acceptance/test_gateway_workflows.py -k debug_gateway_rate_fallback -v
~~~

若前序任务已使新组合测试直接通过，记录它是回归验收，不伪造 RED 或改坏生产代码。只有发现未覆盖的组合缺陷才先保留其正确失败断言，再做最小修复。

- [x] **Step 3: Describe the minimal implementation**

~~~text
复用现有 acceptance 的本地 socket / Runtime 支撑
用 MockTransport 与假 CLI 驱动真实生产调用链
只修复组合验收实际暴露的接线缺口，不为测试增加生产专用开关
新增 live 模块：
  沿用 WEB_SEARCH_RUN_INTEGRATION=1 的统一 opt-in
  默认所有案例 SKIP，绝不隐式读取用户配置或触网
  缺实际凭据/CLI 时 SKIP，已配置但请求失败时 FAIL
  每个用例只一条公开查询/合成输入，输出安全计数
  真实 DEBUG 捕获使用专用非传播 logger / 内存 sink，只断言安全布尔与字节数
  不让 pytest 的捕获输出、失败断言或报告回显原 body/credentials
~~~

live 模块提供可通过 `-k` 单独执行的 `openalex`、`arxiv`、`semantic_scholar`、`unpaywall`、`llm_large_input`、`agent_large_input` 用例。OpenAlex 使用 `OPENALEX_API_KEY` 的环境值；LLM 沿用现有 live 测试的 `OPENAI_API_KEY`/`OPENAI_MODEL`；Unpaywall 邮箱使用现有示例的 `UNPAYWALL_CONTACT_EMAIL`，用户自选命令只使用已约定的临时配置，不把凭据落盘。Agent 用已安装认证的 agy；缺安装明确 SKIP，不自动安装/登录。

大输入必须来自 Task 7 的真实 Prompt builder，保证带可裁正文标注，实际默认 cap 生效。不把 live 连通性当作滑动窗口精度证明，不人为耗尽速率。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/acceptance tests/orchestrators tests/academic tests/daemon tests/integration/test_live_rate_limits_and_inputs.py -v -rs
~~~

预期：本地验收 PASS；新增 live 用例全部因未 opt-in 而 SKIP；无外部网络。失败必须能归因于真实流程，不断言 mock 自己的行为。

- [x] **Step 5: Refactor (keep tests green)**

只合并重复的合成 fixture 和安全指标输出，不扩大生产改动。重跑 Step 4。

### Task 15: 完整速率参考、示例配置与迁移文档

**Files:**

- Create: `docs/provider-rate-limits.md`
- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `docs/public-interface.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/designs/architectures/20261118-provider-rate-observability-openalex-input-truncation.md`（仅调查事实复核注记，保留已确认设计）
- Test: `tests/docs/test_documented_config.py`
- Test: `tests/docs/test_open_source_contract.py`

- [x] **Step 1: Write the failing test**

先写 `test_documented_rate_reference_covers_registered_non_llm_providers`：从三个真实 default registry 取得 Web/Academic/OA 名称集合，对照新参考文档的完整表，验证无遗漏、无 LLM/Agent 混入、有来源/模式说明。不要锁死整段 prose。

扩展示例 TOML 契约：使用已有 `_stub_environment` 为环境变量名称提供假值，经真实 `resolve_config` 解析，验证默认输入 cap、OpenAlex Key、实际存在 provider 的窗口/间隔/并发、未知值留空、共享字段不进 options。检查不能把美元、credits、月费用额度作为窗口配置。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/docs/test_documented_config.py -k "rate_reference or input_limits" -v
~~~

预期：完整文档或新示例值缺失，明确的公开配置合同失败。

- [x] **Step 3: Describe the minimal implementation**

按第 2 节的 31 行和来源差异创建 `docs/provider-rate-limits.md`。每行说明当前 endpoint/product、认证/套餐条件、官方数字或未知、本地更保守策略、来源和核验日期。

示例加入：

~~~toml
[input_limits]
llm_request_body_bytes = 131072
agent_prompt_bytes = 98304
~~~

在现有 provider 表原位加入已证实的共享速率字段；不改变 enable flags、不重复 TOML table。明确新增 Linkup/AnySearch/Keenable 的 10/1、适用免费账号的 Bright Data 1000/60、元宝版本未知时的 5/1；认证/zone/产品条件必须随注释写入，不能只复制数字。保留模板原本省略的 ZenRows、Decodo、ScrapingDog、ScrapeGraphAI 选择，它们的完整可复制配置建议放在新参考文档，不为覆盖率而突然启用更多服务。

关键样例：

~~~toml
# 在现有 [web_providers.tinyfish] 表内
rate_limits = [
  { requests = 30, period_seconds = 60 },
  { requests = 500, period_seconds = 3600 },
  { requests = 1000, period_seconds = 86400 },
]

# 在现有 [academic_providers.arxiv] 表内
max_concurrency = 1
min_interval_seconds = 3

# 在现有 [academic_providers.openalex] 表内
# api_key_env 应引用环境变量 OPENALEX_API_KEY 的名称，不能填写真实 Key
rate_limits = [{ requests = 30, period_seconds = 1 }]
# 对当前 works search 可接收的复杂 Boolean 查询使用保守共享间隔
min_interval_seconds = 0.5
~~~

这些是编辑现有表的字段片段，不是可在文件尾盲目粘贴的重复配置块。其余提供商按第 2 节落实；未知固定值不编造，Doubao 的 Fetch 不确定性在行旁保留。

README 与 public-interface 同步说明：

- provider 级窗口单位秒、Search/Fetch/重试共用、capacity 独立、local wait budget 和 fallback。
- 旧配置不自动重写；OpenAlex enabled 的唯一主动不兼容迁移与可选 email、假 Key 示例、没有匿名回退。
- LLM/Agent 默认 cap、80/20、保护指令、input_budget_unfit、byte 与 token/OS 的区别。
- 更新原“绝不记录 prompt/response/Agent stdout 内容”的过时表述：普通模式没有 Body；DEBUG 每次一条脱敏且限长的双向预览；stderr/env/argv/header/被丢中段不记录。
- `time` vs `elapsed_ms`、两个截断标志、无响应 null、无实际调用不造 preview。
- debug.log 原 5 MiB + 3 backups、仅 opt-in、CLI stdout 和结果 schema 不变。
- 31/31 是来源覆盖，不是每家都有确定阈值或都已 live PASS。

CHANGELOG 记录新增行为、OpenAlex 迁移和 debug 内容变化；不在本任务擅自 bump version、创建 tag、发布版本。现有兼容性政策要求不兼容内容在未来小版本发布说明中明确，版本决定留给发布任务。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/docs tests/doctor/test_config.py tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py -v
uv run python scripts/build_docs.py
~~~

预期：示例以假环境可真实加载，31/31 表完整，站点构建成功且入口无版本占位符。单独定向检查 README/public-interface 指向新参考文档的相对链接存在；build_docs.py 本身不证明全站无破链。不提交生成的 site 输出。

- [x] **Step 5: Refactor (keep tests green)**

README 保留使用入口和迁移步骤，完整来源集中在新参考页；避免三处维护不同数值表。重跑 Step 4，确保架构复核注记指向同一来源口径。


### Task 16: 最终离线门槛、低调用量冒烟与证据记录

**Files:**

- Verify: `.github/workflows/ci.yml`
- Verify: `pyproject.toml`
- Verify: `tests/integration/test_live_rate_limits_and_inputs.py`
- Verify: `tests/integration/test_live_tavily_and_openai.py`
- Verify: `tests/integration/test_live_parallel.py`
- Verify: `tests/integration/test_live_new_web_providers.py`
- Update evidence only: `docs/designs/plans/20261118-provider-rate-observability-openalex-input-truncation.md`

- [x] **Step 1: Write the failing test when a final check exposes a missing behavior**

本任务先运行已有验证门槛，不为可逆文档编辑或测试命令本身新增镜像测试。若暴露实际缺陷，回到所属 Task 增加一个最小失败用例；记录正确 RED，再修实现。无需为了最终验收人为制造失败。

先确认工作树/分支、仅本任务改动、没有真实配置/日志/Key 被暂存，并检查启用集和不兼容迁移说明。

- [x] **Step 2: Run the offline verification gates**

逐条运行；任一步失败都记录命令和分类，不把后续未跑项写成 PASS。

~~~bash
uv sync --locked --all-groups
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -v
uv run python scripts/build_docs.py
uv build
~~~

预期：格式、lint、strict mypy、离线测试及文档/分发构建成功；所有 live 用例按开关 SKIP。数值、签名与 fixture 均以真实工具结果为准。CI 的 Linux Python 3.11/3.12/3.13/3.14 和 macOS Python 3.12、dependency audit、secret-scan 继续作为原有门槛；本地不能冒充已执行其他平台 matrix。

构建轮子后在任务专用临时 tool 目录安装并检查两个 help；版本沿当前 `0.4.0`，若实施基线有合法版本变化，使用实际构建文件名：

~~~bash
env UV_TOOL_DIR="$PWD/.validation-rate-limits-20261118/tools" UV_TOOL_BIN_DIR="$PWD/.validation-rate-limits-20261118/bin" uv tool install --force dist/agent_search_gateway-0.4.0-py3-none-any.whl
.validation-rate-limits-20261118/bin/agent-search-gateway --help
.validation-rate-limits-20261118/bin/agent-search-gateway start --help
~~~

只使用本任务新建且已确认未被其他工作占用的目录；不要替换用户已安装工具或改全局环境。生成的 build/site/验证目录不提交。

- [x] **Step 3: Run controlled live smoke after offline checks pass**

用户已说明现有各家环境变量已设置。直接复用实际配置对应的凭据，不打印值、不自动开通产品或更改用户 daemon。计划编写阶段不执行这些命令；实现阶段用明确 opt-in 逐项运行。

先验证新鉴权：

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_rate_limits_and_inputs.py -k openalex -v -rs
~~~

再按已具备的环境逐项验证慢速 Academic 和大输入：

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_rate_limits_and_inputs.py -k arxiv -v -rs
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_rate_limits_and_inputs.py -k semantic_scholar -v -rs
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_rate_limits_and_inputs.py -k llm_large_input -v -rs
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_rate_limits_and_inputs.py -k agent_large_input -v -rs
~~~

需要 OA 时单独跑 `-k unpaywall`，其 contact email 使用现有示例的 `UNPAYWALL_CONTACT_EMAIL`。真实 Web 只选已配置代表服务执行一条 Search，必要时一条 Fetch；可使用现有 live Tavily/Parallel/新 Web provider 测试按 `-k` 缩小范围。不为证明 RPM 而压测、制造 429 或耗尽免费额度。

| 冒烟类别 | 安全验收记录 |
|---|---|
| OpenAlex | Key 模式、调用数、状态类别、合法论文映射；401/403 报 FAIL 且不匿名重试 |
| arXiv / Semantic Scholar | 调用数、间隔/等待、状态；无需把服务端偶发 429 改为本地 PASS |
| LLM 大输入 | 原 JSON bytes、发送 bytes≤131072、input_truncated、结果/失败分类 |
| Agent 大输入 | OS、最终 prompt bytes≤98304、spawn/终态类别、清理；E2BIG 不能冒充通过 |
| DEBUG HTTP/Agent | time 格式、一次调用一个 preview、两侧长度和截断标记、秘密缺席 |
| 可选 OA/Web | 实际执行范围及每家 PASS/FAIL/SKIP，未测项明确列出 |

LLM/Agent 输入使用纯合成正文且只略超过各自 cap，不使用私有网页。已有凭据但权限/余额/服务故障导致失败时如实报 FAIL；缺安装或缺必要变量才 SKIP。不能将“31 家来源已调查”写成“31 家均已实测”。

- [x] **Step 4: Record passing checks and remaining limitations**

记录实际命令、时间、Python/OS、通过/失败/跳过数、source coverage 与真实 smoke coverage；不把实际 body、环境、Header、完整 URL query 或凭据加入提交。若 smoke 暴露代码缺陷，先回到对应最小 RED 修复，再只重跑受影响检查及最终必要门槛。

源码或测试有新改动后重跑最终格式/lint/mypy/离线 suite；只补充结果文字时不用反复跑全套测试。

- [x] **Step 5: Refactor only if an actual defect or duplication remains**

完成有依据的修复后复核 diff、文档和退出标准。所有授权实施任务完成则停止实施，进入用户后续指定的 review/PR 流程；本计划不授权在实现前推送、发 PR、合并或发布。

## 4. Spec 覆盖矩阵与最终退出标准

| 规格要求 | 实施任务 | 验证证据 |
|---|---|---|
| 31 个服务、按实际认证/端点调查、未知不猜、费用排除 | 15；第 2 节来源复核 | registry ↔ 参考文档集合，TOML 真实解析 |
| 多窗口、min interval、保留 concurrency | 1、4、5 | 假时钟原子预约与 runtime quota 测试 |
| Web Search/Fetch/内部多请求/重试共用 | 5 | 实际 MockTransport 发送时序、Zhihu 双分支 |
| Academic/OA 与同名 LLM/Agent 隔离 | 5 | namespace 隔离及 OA 保留结果 |
| 长等待有限、取消不耗许可、已预约不退款 | 4、5、6 | deadline/取消用例，无悬挂任务 |
| Fetch 优先可发候选、避免忙等、不重复给等待预算 | 6 | 实际 rate 片段计时、capacity/其他候选耗时不扣、耗尽与取消测试 |
| 全日志 UTC 毫秒、request_id、elapsed_ms/轮转保留 | 10、14 | 固定 LogRecord.created、daemon、默认 5 MiB 边界两侧事件完整性 |
| OpenAlex required/disabled/Bearer/不匿名回退 | 2、5、14 | 配置零发送、真实 Mock Header、401/403 |
| 128 KiB LLM / 96 KiB Agent、保护模板/Focus、80/20 | 3、7、8、9 | 最终 request.content / 单个 -p 的字节断言 |
| 不可压缩、Unicode/JSON escape/系统上限、E2BIG | 7、8、9 | input_budget_unfit、argument_limit、不 spawn/send |
| DEBUG 每 HTTP attempt/Agent 调用一条双向 preview | 11、12、13 | success/error/retry/decode/cancel、LLM (protocol_attempt, attempt) 配对矩阵 |
| 8 KiB、先脱敏、两个截断标记、无响应 null | 11、12、13 | 8191/8192/8193、escape、秘密边界哨兵 |
| 不记录输入裁掉的中段、不输出 Header/env/argv/stderr | 8、9、11、12、13 | 跨输入 head/tail 的秘密投影、原中段缺席、最终物理日志哨兵 |
| debug=false 不准备 Body、日志失败不改业务 | 11、14 | spy 与 helper/formatter/sink 故障注入 |
| URL admission、provider fallback、论文合并、结果模式 | 6、14 | acceptance/orchestrator 原合同 |
| 离线门槛与低调用量真实 smoke 分开报告 | 14、16 | 默认 SKIP、最终命令和范围记录 |

### 4.1 类型与名称自检清单

- 所有配置消费者统一使用 `.rate_policy.windows`、`.min_interval_seconds`、`.input_limits.llm_request_body_bytes`、`.input_limits.agent_prompt_bytes`。
- 命名空间固定为 `web` / `academic` / `oa`；LLM/Agent 不登记 rate。
- `RateGate.permit(timeout_seconds=...)` 返回本段等待秒数；ready-in 同步只读。Task 6 统一用 InitialRateBudget、initial_rate_budget、consume_initial_rate_budget，传 remaining_seconds/waited_seconds；executor 从余额建立本段截止。纯 capacity/其他候选执行不扣余额，累计诊断只相加一次。
- `PreparedInput.content` 为真实 bytes，original_bytes 为完整未裁体大小，input_truncated 只反映输入变化。preview_body 三态与 PreparedPreview.content 仅供 DEBUG；BodySelection 是原正文 Unicode 索引，PreviewProjector 投影同一获选候选，不改发送/统计、不保留原文闭包。
- `TruncatableMessage` 对外只暴露 Mapping 的 role/content；`ChatMessage`、LLMClient 和 AgentTextRunner 签名保持。
- HttpJsonExecutor 的 prepared_body 与可选 protocol_attempt 在三个相关方法同名；普通 json_body 路径保持，与 prepared_body 互斥。外层编号属于每次 complete_* 局部闭包，HTTP attempt 仍每个 executor 调用从 1 开始。
- reason 固定为 `rate_limit_wait_timeout` / `input_budget_unfit`，通过现有 diagnostics 白名单；保留原 `argument_limit`。
- 所有预览事件使用第 1.3 节同一套字段，不混用 input_sent_bytes、body_size 等第二套日志名称。live 报告可展示更短列名，但必须明确来源映射。
- 新配置 dataclass 末尾字段有默认值，现有直接构造测试不会因缺新参数全部失效。

### 4.2 退出标准

以下勾选表示本计划的实现和验收步骤已完成；真实 smoke 的失败/跳过见 6.5，既有 Agent 清理测试的历史偶发失败与未确证原因见 6.1，不表示所有上游及所有平台均已验证通过。

- [x] 任务 1–15 的新行为测试和既有相关测试通过，未实现项没有勾选。
- [x] Spec 覆盖矩阵每行均有实际实现与测试证据；没有用简单计数代替传输边界验证。
- [x] RateGate、Scheduler、HTTP retry、Agent 清理无无限等待、重复预约或任务/lease 泄漏。
- [x] HTTP 与 Agent 的真实发送内容符合字节 cap；system/Focus 保留，预检失败无伪造调用日志。
- [x] DEBUG 安全预览、timestamp、secret/URL 脱敏、单行与轮转通过；普通模式无 Body 成本。
- [x] 31 服务参考表、产品条件、来源差异与配置示例一致；不含费用额度管理。
- [x] 最终离线 CI 对应门槛通过；跨平台/远程 CI 未运行部分明确标记。
- [x] Live smoke 只记录真实执行的 PASS/FAIL/SKIP，不把未测或账号问题隐藏。
- [x] 最终改动仅属于本工作树与本任务；无真实密钥、用户配置、原始调试日志或生成输出进入提交。

## 5. 规划阶段记录

本节只记录计划产物本身；不代表上述功能验收已执行。

- 已完成：读取三份 spec、核对当前文件结构/接口/测试支撑、核查 31 服务官方来源、将需求分解为 16 项可执行任务。
- 计划中的来源补全与保守示例调整已在第 2.1 节列明，实施时不得把已知不确定项写成保证。
- 规划阶段未启动上游 smoke、未修改业务代码、未更新用户配置或运行中的服务。
- 自检已完成：按第 4 节核对 spec 覆盖和类型一致性；三项只读独立审查分别核对限流/调度、输入/日志、官方来源。已修正迟唤醒期限、两次请求 ID、取消契约、动态 Header 时机、多正文预算、安全不完整输出和 live 报告边界等问题。

### 5.1 2026-11-19 复核修订

- 复核基线：计划提交 `e0def89`。沿用已有 16 项实施顺序，三份已确认 spec 不作改写。
- 根据官方专页补全 Linkup、AnySearch、Bright Data、元宝、Keenable 的速率，补齐 Exa 端点差异、Firecrawl 浏览器并发单位及 dblp 礼貌间隔；31 个注册服务全部覆盖。
- 修正 FetchScheduler 的实际限流等待余额、全候选耗尽、judge 分类、waiter 清理与累计等待诊断，复用既有 timeout 配置。
- 增加输入裁剪切口的敏感跨度投影、LLM 两层重试关联、默认 5 MiB 轮转边界完整性测试。
- 已完成三项只读复核及主代理的覆盖/接口自检；本轮只修改实施计划，未执行补丁、测试、真实 API 冒烟或服务操作。

## 6. 实施验证记录（2026-11-19）

本节仅记录已执行结果；未完成任务仍保持未勾选。完整离线 suite 使用 `env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q`，与计划 `-v` 的测试选择相同。

| Task | 状态 | 定向验证 | 完整离线 suite | 静态检查与自审 |
|---|---|---|---|---|
| 1 | 完成 | 三个相关测试文件 507 passed；已观察共享字段、结构及数值溢出的有效 RED | 3104 passed / 12 live skipped | 四文件 ruff check、format --check、strict mypy 通过；共享配置与 adapter options 隔离复核通过 |
| 2 | 完成（原写入阻塞已解除） | 原三失败用例 3 passed；计划七文件 275 passed | 3115 passed / 12 live skipped（13.47s） | 八个相关 Python 文件 ruff check、format --check、strict mypy 通过；required/Bearer、无匿名回退、配置精确引用自审通过 |
| 3 | 完成 | 四个相关测试文件 333 passed；默认/覆盖/未知键/安全原因合计 9 个有效 RED | 3150 passed / 12 live skipped（13.27s） | 五个修改文件 ruff check、format --check、strict mypy 通过；旧构造与 frozen、预算隔离和安全错误自审通过 |
| 4 | 完成 | RateGate/quota 相关 22 passed；滑窗、三窗口、间隔、期限/迟醒等取得有效 RED，25 协程竞争与取消回归通过 | 3166 passed / 12 live skipped（13.51s） | 三文件 ruff check、format --check、strict mypy 通过；锁内原子登记、锁外等待、只读查询与零余额自审通过 |
| 5 | 完成 | Runtime/HTTP/Zhihu/OA 等相关 384 passed；共享发送时序、namespace、提前 start、超时与 OA reason 的有效 RED | 3185 passed / 12 live skipped（14.09s） | 11 文件 ruff check、format --check、strict mypy 通过；每 attempt 预约、独立网络计时、Header 放行后刷新及 1e308 诊断自审通过 |
| 6 | 完成 | Scheduler/URL/HTTP/RateGate 相关 199 passed；优先候选、全部受限、预算竞态和定时器迟启动取得有效 RED，500ms 累计与 125ms 网络耗时分别验证 | 3206 passed / 12 live skipped（14.29s） | 八文件 ruff check、format --check、strict mypy 通过；候选耗尽、task/gate scope、暂停计时、retry 与 waiter/lease 清理自审通过 |
| 7 | 完成 | 输入 helper 与 stages 相关 40 passed；头尾裁剪及六 builder 取得有效 RED，2 MiB、多正文真实编码成本/Unicode 索引与投影隔离通过 | 3230 passed / 12 live skipped（13.89s） | 三文件 ruff check、format --check、strict mypy 通过；纯 helper、受保护模板、可行最小候选与无原文持久引用自审通过 |
| 8 | 完成 | LLM/HTTP/Runtime/orchestrator 相关 204 passed；256277-byte 超限、body 互斥与 Content-Type 的有效 RED；六 stage、1400-byte 配置和两层 retry 同字节通过 | 3248 passed / 12 live skipped（14.00s） | 六文件 ruff check、format --check、strict mypy 通过；真实 HTTPX 编码、quota 前拒绝、元数据隔离与无缩小重发自审通过 |
| 9 | 完成 | Agent/Runtime/stages/deadline/shutdown/诊断 136 passed；122927>98304、8424>4096、系统上界及预检原因的有效 RED，double-cancel/E2BIG 回归通过 | 3259 passed / 12 live skipped（14.45s） | 五文件 ruff check、format --check、strict mypy 通过；完整参数预算、Linux 实际上界、NUL/UTF-8 预检与原清理顺序自审通过 |
| 10 | 完成 | 日志/daemon/acceptance 相关 38 passed；缺失 time 与伪造 time 重复字段的有效 RED；UTC 跨日、五类非法 created、默认 5 MiB/3 backups 轮转两侧验证通过 | 3269 passed / 12 live skipped（14.04s） | 四文件 ruff check、format --check、strict mypy 通过；record.created 来源、level/time/request 顺序及原安全日志流程自审通过 |
| 11 | 完成（独立复核修复已回归） | 初验 117 passed；独立复核补修后 132 passed，四类凭据泄露、两类安全正文可见性及未加引号 JSON 凭据均经有效 RED→GREEN | 初验 3325 passed / 12 skipped；补修后 3340 passed / 12 skipped（14.31s） | 初验六文件 ruff check、format --check、strict mypy 通过；补修范围纳入 Task 12 最终静态检查；真实输入投影与双重 8 KiB 约束保留 |
| 12 | 完成 | HTTP/LLM/观测/相关旧合同 533 passed；2 send/0 preview、protocol 编号缺失取得 RED；组合 (1,1)/(1,2)/(2,1)、100 attempts、取消、rate timeout 和独立 logger 投影通过 | 3365 passed / 12 live skipped（15.79s） | 19 文件 ruff check、format --check、strict mypy 通过（含独立复核共享修复）；唯一 send finally、响应隔离、原全日志敏感哨兵与调用局部编号自审通过 |
| 13 | 完成（保留既有偶发测试问题） | Agent/观测/daemon/acceptance 422 passed；真实 timeout 缺预览 RED→GREEN；完整/部分捕获、清理期首字节、取消与 INFO 验证通过 | 最终原始全量 3377 passed / 12 skipped（15.72s）；一次中间回归 3376 passed / 1 failed / 12 skipped，见 6.1 | 四文件 ruff check、format --check、strict mypy 通过；stdout 有界保留、清理前 EOF 快照、唯一 emit 与原清理顺序自审通过 |
| 14 | 完成 | acceptance/orchestrators/academic/daemon 与新增 live 入口 376 passed / 6 skipped；新增 19 个本地验收、扩展论文链路，六个 opt-in 入口分别可选 | 3396 passed / 18 live skipped（16.76s） | 五文件 ruff check、format --check、strict mypy 通过；真实 Runtime/socket/HTTP/CLI、取消与 shutdown、日志故障隔离自审通过；live 的实际参数计量、预览标记和代理透传三项只读复核问题已修复；本任务未真实联网 |
| 15 | 完成 | docs/doctor/config 定向 507 passed；参考页缺失与 input_limits 缺失取得两个有效 RED；31 家覆盖、四个可选 TOML 表经实际 resolver 与假环境验证 | 3398 passed / 18 live skipped（16.57s） | 三测试文件 ruff check、format --check、strict mypy 通过；文档构建、两条相对入口和版本占位符检查通过。首次构建缺 pdoc，使用已有 docs 依赖组解决后原命令复跑通过，未改依赖/锁；九文件自审完成，保留启用状态/省略表与历史记录 |
| 16 | 完成（真实 smoke 保留三失败一跳过） | 七类最终状态 3 PASS / 3 FAIL / 1 SKIP，详见 6.5；Agent 合法单 JSON 围栏被误拒取得 RED，Agent 专属验收修正与新增 14 条持久化离线回归通过 | 最终 3412 passed / 18 live skipped（16.58s） | format 225 文件、Ruff lint、strict mypy 223 文件通过；锁定依赖、文档/分发构建、隔离安装与两 help、wheel 新模块检查通过；真实 OpenAlex/Bearer 和 Agent 98304-byte cap 验证通过。生产实现未因 smoke 改动；历史失败与未执行 CI 项均保留 |

### 6.1 已观察的验证环境问题

- 完整 `pytest -v` 两次在 LocalHost 120000-byte 输出截断附近返回 exit 143（SIGTERM），没有完整 pytest 汇总；同范围 `-q` 成功结束，因此后续完整回归采用低输出模式。这是观察与工作方式调整，不将被终止的执行记为 PASS。
- Task 1 中间两轮完整 suite 分别在既有 `test_terminal_exit_cleans_remaining_descendants` 的 `terminal_status_failure`、`nonzero_exit` 参数发生子进程存活清理断言失败（各 1 failed / 2839 passed / 12 skipped）。单独 Agent 目录 63 passed，后续最终全量 3104 passed / 12 skipped。未改无关生命周期代码；在后续 Agent 任务和最终门槛继续核查。
- Task 13 一次原始全量又命中既有 `test_terminal_exit_cleans_remaining_descendants[nonzero-nonzero_exit]` 即时存活断言（3376 passed / 1 failed / 12 skipped）。8 个新 pytest 进程共 32 例及两轮完整只读探针回归均未复现，未取得能区分瞬态状态与持续泄漏的证据；不宣称原因已确认。临时探针仅进程内生效，原清理顺序与即时断言未改。最终原始完整回归 3377 passed / 12 skipped；此后 PASS 不抹去前述失败记录，Task 16 继续必要最终门槛。

### 6.2 Task 2 曾遇的写入阻塞（已解除）

LocalHost `edit` 对三个不含真实凭据的环境变量名称引用修改返回 `INVALID_ARGUMENT: Secret-looking content is blocked from edit. Use placeholders such as [REDACTED_SECRET] in handoff files.` 受阻文件为 `config.example.toml`、`tests/runtime/test_academic_runtime_assembly.py`、`tests/runtime/test_runtime_assembly.py`。前者需补齐 OpenAlex 的 Key 环境变量名称，后两者可复用测试已有的 ENV_A 假值。没有通过 shell、字符串拼接或其他通道规避内容检查。

当时失败用例仅为 `test_runtime_instantiates_only_enabled_academic_providers_in_configured_order`、`test_runtime_assembles_all_academic_providers_resolver_and_closes_once`、`test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help`。随后用户明确授权通过 LocalHost Bash 写入同一工作树，并说明已设置 OPENALEX_API_KEY。实现者先核对三文件最新 SHA-256 和旧片段唯一性，再原位完成无凭据名称引用修改；原三失败全部转绿，Task 2 已完成。Task 3 开始实施，其余任务继续按序执行。真实上游 smoke 留在 Task 16；当前改动尚未提交。

### 6.3 实施期间的官方来源只读复核

两项独立只读复核覆盖全部 31 个非 LLM/Agent 服务，2026-11-19 的主要技术频率与第 2 节一致。Task 15 后续应纳入以下更准确的来源表述：

- TinyFish 必须保留 30/60、500/3600、1000/86400 三个共享窗口；[官方定价](https://www.tinyfish.ai/pricing)明确每日上限及不扣 Wallet，但没有核实固定日重置算法。因此不能把“固定日重置”写成已证实事实，应说明本地采用滚动 24 小时的保守窗口。
- Doubao 默认账号 Custom Search 10 QPS 与 Fetch 控制台阈值的结论成立；[官方错误码](https://docs.volcengine.com/docs/networked-search/error-description?lang=zh)另列未开通付费调用账号的免费限频而未给固定数值，不能保证所有免费-only 账号都拥有 10 QPS。
- Decodo 的旧 `/v2/scrape` Basic 认证与 10 req/s 使用条件保留；主链接使用小写 [当前产品页](https://decodo.com/scraping/web)，并可补充[官方 legacy 说明](https://help.decodo.com/docs/web-scraping-api-introduction)和[旧端点示例](https://decodo.com/scraping/web/rss-scraper-api)。
- OpenAlex 官方还为特定宽 topic/concept OR filter 和 semantic search 规定 1 RPS；当前 adapter 不发送这两类请求，不将其限值误套进当前普通/Boolean Search。鉴权仍是项目主动要求 Key，官方基础匿名查询并非全面禁用。

来源覆盖不等同于 31 家真实 API 全部实测通过；本阶段只做官方网页和仓库复核。

### 6.4 安全预览独立复核（已修复并回归）

Task 11 初验后独立静态复核发现四类凭据边界：跨嵌入 JSON 的明文凭据、URL 路径单引号、外层 URL 编辑覆盖已登记敏感跨度、表单 client_secret；另发现 JSON 字符串局部过度遮蔽和日期标签误判 JSON 两类安全正文可见性问题。Task 12 实施者先修共享 helper，再继续 HTTP 接线。前四类先以真实 LogRecord 断言复现为 9 failed / 2 passed；随后全部六类及未加引号 JSON 凭据补充用例均获有效 RED→GREEN。修复后定向 132 passed，完整离线 3340 passed / 12 skipped（14.31s）。共享修复的最终静态检查与后续传输回归一并记录于 Task 12。

### 6.5 Task 16 最终门槛与真实 smoke（2026-11-19）

本地环境为 uv 0.12.1、CPython 3.12.3、Linux 6.6.87.2-microsoft-standard-WSL2、x86_64。命令时间按执行日志的 UTC 记录；07:35–07:55 UTC 对应客户端日本时间 16:35–16:55。使用的工作树分支为 `patch-rate-limits-logging-openalex-input-caps-20261118`，HEAD 为 `8e0b3d6`。

#### 离线和安装门槛

| 实际命令或检查 | 结果 |
|---|---|
| `uv sync --locked --all-groups` | exit 0；检查 23 个包 |
| `uv run ruff format --check src tests scripts` | 最终 exit 0，225 个文件 |
| `uv run ruff check .` | 最终 exit 0 |
| `uv run mypy src tests scripts` | 最终 exit 0，strict mypy 223 个文件 |
| `env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q` | 最终 exit 0；3412 passed / 18 skipped，16.58 秒 |
| `uv run python scripts/build_docs.py` | exit 0；site 首页/API 入口存在，首页没有版本占位符 |
| `uv build` | exit 0；生成 0.4.0 wheel 和 sdist |
| 专用 UV_TOOL_DIR / UV_TOOL_BIN_DIR 中安装 wheel | exit 0；安装 0.4.0，未替换用户工具 |
| 安装后的主命令和 `start --help` | 两次 exit 0 |
| 隔离安装分发的三个新增生产模块 | `body_preview.py`、`input_limits.py`、`rate_limit.py` 均在 wheel 中 |
| 临时安装目录清理 | 核对本任务创建时的设备号、inode 与内容后清理；没有遗留安装目录 |

初次最终门槛在 07:35:53–07:37:33 UTC 完成，原始完整测试为 3398 passed / 18 skipped（16.42 秒）。之后的 smoke helper 修订仅改测试；已再次运行最终格式、lint、strict mypy 和完整离线 suite，以上表中 3412 / 18 为最后结果。生产 parser、prompt、CLI 和依赖清单没有因 smoke 改动。证据文字更新后未无目的重复全量测试。

#### 低调用量真实 smoke

所有请求均显式 opt-in，只在对应命令临时恢复代理。下表的请求/响应预览标志分别对应 `request_preview_truncated` / `response_preview_truncated`；原始/发送长度对应 `request_body_original_bytes` / `request_body_sent_bytes`，输入裁剪标志对应 `request_input_truncated`。响应字节对应 `response_body_bytes`，Agent 记录的是捕获 stdout envelope 的字节数。

| UTC 开始 | 测试项 | 实际结果 | 调用与安全证据 |
|---|---|---|---|
| 07:39:00 | OpenAlex | PASS，exit 0 | Bearer Header；query 无 Key。1 次发送，HTTP 200，非空论文均有 title/source_id；本地等待 0 ms，1 条 UTC preview，Key 未出现在日志。请求 0 B、响应 201680 B，预览标志 false/true |
| 07:39:24 | arXiv | PASS，exit 0 | 匿名，1 次发送，HTTP 200，非空论文映射；本地等待 0 ms，1 条 UTC preview。请求 0 B、响应 21510 B，预览标志 false/true |
| 07:39:29 | Semantic Scholar | FAIL，exit 1 | Key 模式，1 次发送，HTTP 429，安全类别 http_status；未重试。本地等待 0 ms，1 条 UTC preview，Key 未出现在日志；响应 174 B，预览标志 false/false |
| 07:39:34 | LLM 大输入 | SKIP，exit 0 | 缺少 OPENAI_API_KEY；0 次发送，没有真实发送字节或预览。离线真实 HTTP 编码与 cap 用例仍通过 |
| 07:39:37 | Agent 大输入初验 | FAIL，exit 1 | 1 次 spawn，102590→98304 B，首尾保留/中段移除；1 条 UTC preview，输入裁剪 true，预览标志 true/false，响应 417 B。原 helper 类别 unexpected；该轮回复形态未保留，不能倒推 |
| 07:40:15 | Unpaywall | FAIL，exit 1 | 使用已配置 contact，1 次发送尝试，未取得 HTTP 状态，安全类别 all_providers_failed。本地等待 0 ms，1 条 UTC preview，contact 未出现在日志；响应 0 B，预览标志 false/false |
| 07:41:00 | Tavily Search | FAIL，exit 1 | 1 条逻辑 Search；原用例实际发生 3 次物理发送尝试，均未取得 HTTP 状态。没有可验证响应；本轮未采集 preview |
| 07:47:36 | Agent 定位复验 | FAIL，exit 1 | 1 次 spawn，CLI exit 0、成功终态和非空 text；102590→98304 B；1 条 UTC preview，响应 361 B。完整单个 Markdown JSON 围栏内对象及布尔 ok 合格，原裸 JSON 断言误报 invalid_json；子进程回收、进程组消失 |
| 07:54:31 | Agent 修正后复验 | PASS，exit 0 | 1 passed / 5 deselected，14.56 秒。1 次 spawn，CLI 成功终态、非空 text、完整单 JSON 围栏及布尔 ok 合格；102590→98304 B，首尾保留/中段移除，输入裁剪 true。1 个诊断和 1 条 UTC preview，日志计量与实参一致；响应 378 B，预览标志 true/false，子进程回收且进程组消失 |

按七类测试的最后状态：3 PASS（OpenAlex、arXiv、Agent），3 FAIL（Semantic Scholar、Unpaywall、Tavily），1 SKIP（LLM）。按全部九次逻辑 smoke 记录计：3 PASS / 5 FAIL / 1 SKIP，包含 Agent 三次调用及前两次失败。网关共观察到 7 次 HTTP 发送尝试和 3 次 CLI spawn；不把 CLI 内部不可见的网络活动计作已知调用数。没有追加 Fetch、其他提供商或限额压力测试。

Agent 的公共输出允许 Markdown，计划要求短结构化回复而非裸 JSON。第二次调用确认了验收假阴性后，先用合法单围栏合成输入复现 RED，再仅修 Agent live helper：允许裸 JSON 或完整单个 JSON 围栏，继续严格拒绝额外 prose、多围栏、非法 JSON/NaN、非对象、缺失/非布尔 ok。新增 `tests/unit/test_live_smoke_validation.py` 的 14 条默认离线回归全部通过；LLM 路径与生产实现不变。初次 417 B 回复的精确形态未留存，不能用第二次的围栏证据解释初次回复。

Unpaywall 和 Tavily 未留存传输异常具体子类；现有证据只能确定尝试后未取得 HTTP 状态，不能归因 DNS、连接超时、TLS、服务宕机或凭据错误。Semantic Scholar 的 429 是实际失败，没有按限流“验收通过”处理。上述结果不会与 [31 服务官方来源覆盖](../../provider-rate-limits.md) 混用。

#### 最终范围和未运行门槛

- 最后工作树为 62 个已跟踪修改、9 个未跟踪任务文件，暂存区为空；仅属于本计划，没有写入实际 Key、用户配置或原始日志。示例启用状态保持不变。
- Task 16 的代码变更仅为 live 测试 helper 和新增合成回归；没有修改生产实现，没有提交、推送、发布或操作用户 daemon。
- 既有 Agent 即时存活清理断言在最终全量未复现；6.1 中的失败历史和未确证原因仍保留。
- 本机仅执行上述 CPython 3.12.3 / Linux 门槛；Ubuntu 其他 Python 版本、macOS matrix、dependency audit、GitHub event 敏感 URL 检查、TruffleHog secret scan 和远端 CI 汇总均未在本地执行，未标记为 PASS。

#### 2026-11-19 补测：OpenRouter 免费 LLM 大输入

- 上述 `07:39:34` 的 LLM SKIP 是历史记录：当时 live helper 只读取 `OPENAI_API_KEY`，而用户实际配置的是 `OPENROUTER_API_KEY`；不将 SKIP 当作验证通过。
- LocalHost 确认可读取 `OPENROUTER_API_KEY`。首次补测仅在单条 pytest 命令里将它映射到原 helper 的 `OPENAI_API_KEY`，并将 base URL 设为 `https://openrouter.ai/api`、model 设为 `openrouter/free`；`WEB_SEARCH_RUN_INTEGRATION=1` 仅对该次命令生效。一次 HTTP 请求，返回 200，模型输出满足 `ok` 布尔契约，pytest `1 passed / 5 deselected`，约 7.4 秒。
- 实际 LLM 请求由 135398 B 截断到 131072 B，头尾与省略标记保留、中段移除。1 条 DEBUG request/response 预览，原始和发送长度匹配、UTC 时间存在、环境 Key 未泄露，结果 `category=success`。不以此证明所有 OpenRouter 免费模型或所有其他模型都接受同样的输入。
- 修正 live helper：优先使用可用的 `OPENROUTER_API_KEY`、可选的 `OPENROUTER_MODEL`/`OPENROUTER_BASE_URL`，默认 `openrouter/free`；未配置 OpenRouter Key 时保留 `OPENAI_API_KEY`/`OPENAI_MODEL`/`OPENAI_BASE_URL`（或 `OPENAI_API_URL`）路径。新增 3 条离线环境选择回归，17 条 helper 单测通过；完整离线 suite `3415 passed / 18 skipped`，Ruff 检查、格式检查与 mypy（223 source files）均通过。
- 最新验收状态：LLM 大输入由历史 SKIP 更新为真实 PASS。保留旧记录便于审计；本补测未修改生产实现、用户配置、凭据文件，也未额外发送压力测试请求。

### 6.6 贡献者提交前复核（2026-11-19）

- 在本工作树完成两路只读代码审查，并复核 31 项官方速率来源；现有参考表的数值、认证和端点条件保持成立。
- 修复五项可复现边界：补齐外来 JSON/文本凭据字段遮蔽；清洗协议相对链接及跨裁剪投影；按实际编码细化正文预算，避免轻微 JSON 转义溢出造成正文减半；阻止注入 HTTP 客户端的自动重定向绕过实际发送限流；从 HTTP 完成及后续失败/重试耗时中排除新增预览处理开销。
- 输入/日志新增 8 个参数用例，HTTP 新增 5 个回归，均取得有效 RED→GREEN。原输入复现从 131073 B 裁至 131072 B；原输入/日志审查者按最终文件独立复核，37 项定向测试通过，三项发现全部关闭。HTTP 定向及相关目录 1764 项通过。
- 最终完整检查：`uv run ruff format --check src tests scripts`（225 个文件）、`uv run ruff check .`、`uv run mypy src tests scripts`（223 个文件）、`env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q`（3428 passed / 18 skipped，16.71 秒）、`uv run python scripts/build_docs.py` 均 exit 0。18 个跳过仍为显式 opt-in 的真实联网测试。
- 同步修正设计/计划的实施状态与贡献指南的 DEBUG 安全预览说明。此轮使用合成输入、MockTransport 和本地编码验证；没有追加真实上游请求，沿用第 6.5 节的真实结果与限制。按七类最新状态，OpenAlex、arXiv、Agent 和 OpenRouter LLM 为 PASS；Semantic Scholar、Unpaywall、Tavily 的三项失败记录保留。
- 提交目标已确认：贡献者 `Dingxingdi-coder/agent-search-gateway` 的当前任务分支，合入 `Dingxingdi/agent-search-gateway:main`。创建草稿 PR 后，以 GitHub 上对应提交的 CI 结果补充跨平台、依赖审计、打包及秘密扫描证据。

