# Architecture: Provider 限速、可追踪日志、OpenAlex 鉴权和出站裁剪

- 日期：2026-11-18
- 状态：架构方案已由用户确认，2026-11-19 已完成实现；验证结果与保留限制见对应实施计划第 6 节
- 基线：edcc627，独立 worktree：patch-rate-limits-logging-openalex-input-caps-20261118

## 1. Scope & Assumptions

### In Scope

1. 为 Web、Academic、OA Resolver 共 31 个已注册非 LLM/Agent 服务研究免费层级的请求速率与技术并发要求；对有官方固定值者提供配置示例，未知者留空。
2. 在现有 max_concurrency 之外增加单个提供商级的多窗口 rate_limits 和最短发送间隔 min_interval_seconds；同一 Web 提供商的 Search/Fetch 共用，不分开计数。
3. 为每一行日志添加 UTC 毫秒时间戳，并保留现有 request_id、elapsed_ms 和单行键值格式。
4. OpenAlex 启用时要求 API Key，放在 HTTP Authorization Bearer Header，不再匿名回退；可选联系邮箱保持可用。
5. LLM 与 Agent 各自使用可配置的 UTF-8 字节上限，出站前仅截断长正文，保留系统消息、模板和 Focus 指令。
6. 仅在 debug 模式，每一次上游 HTTP 尝试（含重试）及 Agent CLI 调用各输出一条双向 Body 安全预览事件，Request 与 Response 分别上限 8 KiB，记录原始/发送字节数及截断标记。

### Out of Scope

- LLM/Agent 提供商的 RPM/TPM、模型 token 计算、额外模型摘要调用。
- 厂商 credits、付费额度、每月使用量、每日美元预算等费用相关配额。
- 每端点单独限速；跨机器或进程共享一个 API Key 时的全局协调。
- 将完整 Body 或被裁掉的正文保存到日志；更改业务结果的响应解析规则。
- 尚未获用户同意前实现代码补丁。

### Assumptions

- 所有时间单位均为秒；速率窗口的请求数量为正整数，时间长度为有限正数。
- 没配 rate_limits 仅表示本地未配置固定阈值，不意味着服务端不限速；现有 429 + Retry-After 行为保持。
- 重试算新的实际 HTTP 请求，必须再次取得速率许可。
- 免费套餐速率以官方 2026-11-18 公开文档为准；账户、版本、认证模式可能有差异。

## 2. Architecture Summary

沿用 Provider Registry → config.py → Runtime → Orchestrator → HttpJsonExecutor 的现有结构：

- 配置层：为 Web/Academic/OA 的解析结果增加不可变 RatePolicy，包含 windows（请求数+秒数）和可选 min_interval_seconds。配置字段属于共享字段，不传给具体 Adapter 的 options。
- 配额层：新增异步 RateGate。每个非 LLM/Agent provider 一实例，同一 Web provider 的 Search 和 Fetch 使用同一个实例。保留 CapacityGate，因为并发量不等于一段时间内的请求数。
- HTTP 层：HttpJsonExecutor 在每次真正发送请求前以 monotonic 时钟取得 RateGate 许可；网络失败、429 和 Retry-After 重试都重新限速。
- 调度层：FetchScheduler 可使用 RateGate 非预约式剩余等待时间优先选择可用候选，但最终由 HTTP Executor 原子预约，防止竞态。
- 观测层：KeyValueFormatter 用 LogRecord.created 添加 ISO 8601 UTC 毫秒 time 字段；HTTP/Agent 的 debug-only BodyPreview 帮助函数做脱敏、UTF-8 安全裁剪、统计与单行渲染。
- LLM/Agent 出站层：LLM 按真实编码后的 JSON Request Body 总字节限额；Agent 按单个 CLI prompt 参数字节限额。长正文执行 80% 前缀 + 20% 后缀 + 显式标记的裁剪。
- OpenAlex：Registry 的 authentication 从 none 变为 required；Adapter 接收 SecretValue 并以 Bearer Header 发送。

## 3. Design Decisions

### 3.1 RateGate 与并发门限并存

一个 RateGate 持有多个独立滑动窗口的时间戳队列和最后一次发送时刻。每个 attempt 在 asyncio Lock 内同时检查全部窗口和最小间隔：可用则原子登记一次，不可用则计算最早可发送时间、解锁等待、再检查。取消等待不会消耗许可。使用 monotonic，而非墙上时钟。

理由：能表达 QPS、RPM、RPH、多个窗口并存以及 arXiv 每三秒间隔；滑动窗口避免固定窗口边界一次性突发。计全部尝试会比服务商仅计成功请求的规则更保守。

拒绝方案：完全删除并发限制；分别限制 Search/Fetch；仅将 RPM 除以 60 后按固定 sleep；把第三方套餐限制写死进核心。并发阈值继续约束慢请求的同时在途数，RateGate 约束发送频率。

### 3.2 配置方案

推荐如下，真实 Key 只由环境变量提供，以下示例不含密钥值：

- Web provider：rate_limits = [{ requests = 100, period_seconds = 60 }]，可选 min_interval_seconds。
- Academic provider：相同字段。arXiv 可写 min_interval_seconds = 3、max_concurrency = 1。
- OA Resolver：相同字段，保留现有 OA 解析与启用行为。
- input_limits.llm_request_body_bytes 缺省 131072；input_limits.agent_prompt_bytes 缺省 98304。
- OpenAlex 的 api_key_env 指向用户自设的环境变量；启用但缺失时在配置解析阶段失败。

每个窗口只允许 requests（正整数）和 period_seconds（有限正数秒）两个字段；数组可为空/缺省。min_interval_seconds 缺省表示不启用，显式设置必须为有限正数。Web/Academic 现有 max_concurrency 保留。

正式示例配置将只写来源明确、与认证方式匹配的推荐值；对官方未公布固定数值的提供商记录原因而不造值。通过文档更新示例，不静默重写用户本机的配置文件。

### 3.3 时间戳与双向 Body 预览

使用原 LogRecord.created 生成 time=2026-11-18T08:21:30.124Z，放在日志级别后；不改变 elapsed_ms（monotonic 耗时）。

每个 HTTP attempt 结束时最多输出一条 upstream_body_preview DEBUG 事件，带 provider、stage、endpoint（不包含 URL query）、attempt、transport。字段：

- request_body_preview：HTTP JSON 或文本实际发送体的脱敏首尾预览；GET 无 body 为 null。
- request_body_original_bytes：LLM/Agent 内容输入截断前的尺寸（常规 HTTP 请求则与 sent 相同）。
- request_body_sent_bytes：实际发出的 JSON/文本或 Agent prompt 参数的 UTF-8 字节数。
- request_input_truncated：输入裁剪的布尔标记。
- request_preview_truncated：日志二次 8 KiB 裁剪标记。
- response_body_preview、response_body_bytes、response_preview_truncated：以原始响应内容计算；网络失败且没有响应时明确为 null、0、false。

Agent CLI 使用相同事件与字段，transport 标为 agent_cli；response 记录已捕获的 stdout 终端 envelope（不记录 stderr 全文、环境变量或 argv）。HTTP 200 但 JSON 解析失败时仍记录响应预览，日志不会因解析失败而缺失。

敏感字段名（例如认证、Cookie、Token 等）先按结构化规则遮蔽，再做 UTF-8 字节裁剪，最后套用现有 SecretRedactor 全行兜底。只在 logger.isEnabledFor(DEBUG) 为真时准备/序列化预览。预览不会重新暴露被输入裁剪掉的正文；多行原文由键值 Formatter 的 JSON 字符串转义保持物理单行。原有 5 MiB 文件和 3 个备份轮转保持。

权衡：debug 日志会带有搜索词和网页片段，仍需按敏感调试日志管理；默认模式不记录 Body。

### 3.4 出站截断保证

共享纯函数对 UTF-8 文本按字节预算截取头 80%、尾 20%，预留明确的“内容已截断”标记所占字节，不截断任意 Unicode 代码点。

LLM Stage/Prompt 层把 system、必要模板、Focus 标注为不可压缩片段，网页/检索长正文标注为可压缩片段。LLM Client 以真实 JSON 序列化开销（模型、messages、extra_body 一并计算）得出正文预算，裁剪后再次序列化验证整体 ≤ 131072 默认上限。

Agent Runner 以最终单个 -p 参数的 UTF-8 字节计算预算，保留角色标题、system 内容，仅压缩可变正文，裁剪后验证 ≤ 98304 默认上限。其余命令行参数长度独立于 prompt cap。

若不可压缩字段本身超过总预算，在发请求前给明确错误，而非静默删掉系统/Focus 或尝试超长 argv。Byte cap 不能保证模型 token 限额，允许配置调小。

拒绝方案：只按字符数切、保留开头丢掉尾部、默认保存整段原始正文、改变系统/Focus 指令。

### 3.5 OpenAlex 强制鉴权

修改 Academic Registry 的 OpenAlex authentication=required，在 config.py 已有 required 凭据解析路径早期验证。OpenAlexProvider 增加 api_key: SecretValue 入参，用 Authorization Bearer 发送；禁止拼进 URL、日志和异常消息。contact_email_env 仍为 optional。被禁用的 OpenAlex 不要求环境变量。

这是唯一有意不兼容的原配置路径：之前能匿名运行的启用项，现在必须填写 api_key_env 并设置真实环境变量。

### 3.6 官方免费层级速率调查：31/31 服务

速率指**公开的请求频率/技术并发**，不是赠送 credits、月度可用调用次数或计费日预算。表中“建议”是将来写入 config.example.toml 的示例，不是程序内置强制默认。

**2026-11-19 调查复核注记。** 下表保留初查记录，配置请以[最新 31 项来源表](../../provider-rate-limits.md)的端点、认证及套餐条件为准。Linkup、AnySearch、Keenable 分别核实到组织 Key、注册 Free、认证组织的 10 QPS；Bright Data 未充值 Free Tier 的适用 zone 共享 1000 RPM；元宝未知版本保守 5 QPS。Firecrawl 的 2 个并发浏览器是服务端 worker，Exa 普通 Search 与 Contents 分别为 10 和 100 QPS，Scrape.do 同步 Free 为 5 并发。Brave 与 Parallel 的数值分别适用于 Search Prepaid 与 keyed Build API，Decodo 的 10 RPS 要核对旧 Basic 接口资格。SerpApi 发布的是小时吞吐量。

TinyFish Fetch 另有每日 1000 URL 的技术上限，日重置算法未经核实，本地采用滚动 24 小时并同时保留分钟、小时窗口。豆包默认账号 Search 为 10 QPS，免费-only 账号和 Fetch 的固定阈值未公开核实。OpenAlex 超过 5 个 Boolean operator 的查询限 2 RPS 且不可突发，因此示例另设 0.5 秒间隔；宽 topic/concept OR 和 semantic search 的 1 RPS 不属于当前 adapter。启用时必须提供 Key 是本项目的决定，官方仍允许部分基础匿名查询。dblp 的 2 秒间隔是礼貌建议，CORE 的 1/10 秒是本地保守选择，均未核实为当前端点固定硬阈值。全表来源覆盖不代表全服务实测成功。

#### Web：24 个

| 服务 | 官方免费/公开请求限制 | 建议策略 | 官方来源 |
|---|---|---|---|
| tavily | 开发 Key 100 RPM | 100/60s | [帮助中心](https://help.tavily.com/articles/3240802908-rate-limits) |
| firecrawl | Search 与 Scrape 10 RPM | 10/60s | [官方 Rate Limits](https://github.com/firecrawl/firecrawl-docs/blob/main/rate-limits.mdx) |
| exa | 免费 10 QPS | 10/1s | [开发文档](https://exa.ai/docs/admin/billing) |
| linkup | 未确认统一固定免费 RPM | 暂不填 | [官网](https://www.linkup.so/pricing) |
| brave | Search 50 RPS 容量；实际 Header 可能提示更低限制 | 50/1s | [官网](https://api-dashboard.search.brave.com/documentation/pricing) |
| anysearch | 认证账户阈值动态，未确认固定数值 | 暂不填 | [文档](https://www.anysearch.com/docs/auth) |
| tinyfish | Search 30 RPM + 500 RPH；Fetch 150 URLs/min，采用严格的 Search 窗口 | 30/60s + 500/3600s | [官网](https://www.tinyfish.ai/pricing) |
| parallel | Search/Extract 600 RPM | 600/60s | [官网](https://parallel.ai/pricing) |
| brightdata | 未找到与本账户匹配的固定免费 RPM | 暂不填 | [SERP API](https://brightdata.com/products/serp-api) |
| scrape_do | 套餐、并发能力可变；没有确认固定免费值 | 暂不填 | [文档](https://scrape.do/documentation/async-api/) |
| zenrows | 免费计划 5 并发 | max_concurrency=5 | [官网](https://www.zenrows.com/pricing) |
| decodo | 免费 Web Data API 10 RPS | 10/1s | [官网](https://decodo.com/scraping) |
| scrapingdog | 免费 1 并发 | max_concurrency=1 | [官网](https://www.scrapingdog.com/pricing/) |
| scrapegraphai | 免费 10 RPM | 10/60s | [官方文章](https://scrapegraphai.com/blog/scrapegraphai-pricing) |
| scraperapi | 免费计划 5 并发 | max_concurrency=5 | [FAQ](https://docs.scraperapi.com/resources/faq/plans-and-billing) |
| scrapingant | 免费 1 并发 | max_concurrency=1 | [文档](https://docs.scrapingant.com/errors) |
| serpapi | 免费最多 50 次成功搜索/小时 | 50/3600s（本地更保守地计全部尝试） | [官网](https://serpapi.com/pricing) |
| jina | 当前无 Key Reader 20 RPM；有免费 Key 可达 500 RPM | 20/60s | [Reader](https://jina.ai/reader/) |
| yuanbao | 官方未公布对所有账户一致的固定速率 | 暂不填 | [腾讯文档](https://cloud.tencent.com/document/product/1806/121821) |
| zhihu | 有频率限制，未找到统一公开免费值 | 暂不填 | [开放平台](https://developer.zhihu.com/docs?key=quota) |
| doubao | 火山引擎联网搜索默认 10 QPS；需核对当前 API 服务配置 | 10/1s | [文档](https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh) |
| youcom | Search/Contents 各 10 RPS | 共用 10/1s | [文档](https://you.com/docs/rate-limits) |
| keenable | keyless 公开入口 10 RPS + 1000/hour/IP；当前项目用 Key，无法验证 Key 下免费统一限制 | 暂不填 | [官方项目](https://github.com/keenableai/dsh-keenable) |
| open_websearch | 自托管聚合，本身没有统一外部套餐阈值 | 暂不填 | [项目](https://github.com/Aas-ee/open-webSearch) |

#### Academic：6 个；OA Resolver：1 个

| 服务 | 官方免费/公开请求限制 | 建议策略 | 官方来源 |
|---|---|---|---|
| arxiv | 1 次/3 秒，单连接 | min_interval_seconds=3，max_concurrency=1 | [API ToU](https://info.arxiv.org/help/api/tou.html) |
| semantic_scholar | Key 1 RPS；匿名为共享池 | 保守 1/1s | [官方 API](https://webflow.semanticscholar.org/product/api) |
| openalex | 免费 Key 普通 works 请求 30 RPS | 30/1s，Key 必需 | [鉴权/限额](https://help.openalex.org/api/authentication/) |
| dblp | 无公开固定数值，要求礼貌访问 | 暂不填 | [FAQ](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html) |
| crossref | 公共 list 1 RPS/1 并发；polite（含邮箱）list 3 RPS/3 并发 | 默认 1/1s 与并发 1 | [官方调整说明](https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/) |
| core | 10 秒 5 single 或 1 batch，Search 分类待确认 | 暂按最保守 1/10s | [CORE API](https://core.ac.uk/services/api) |
| unpaywall | 100000 次/日，非付费额度，属技术上限 | 可选 100000/86400s | [API](https://unpaywall.org/api) |

备注：上表不覆盖 LLM/Agent；Search/Fetch 仅用一个提供商的更严格策略。仅有 concurrency 约束的继续用 max_concurrency，不用 RPM 替代。免费额度/账单额度不进入 RateGate。没有公开确定值的不猜测，不代表不存在限流。厂商限速可能随时间和账户不同而改变，样例与官方参考链接应一起更新。

## 4. Component Catalog

| Component | Purpose | Responsibilities | Interfaces | Dependencies | State? | Data Flow |
|---|---|---|---|---|---|---|
| config.py / ResolvedRatePolicy | 配置解释 | 字段校验、Input cap、OpenAlex Key | resolve_config, ResolvedConfig | registries, environ | 否 | 启动 |
| rate_limit.py / RateGate | Provider 速率控制 | 多窗口、间隔、预约、可取消等待 | permit, next_available_in_seconds | asyncio, monotonic | 是 | HTTP attempt |
| concurrency.py / ProviderQuotaManager | 配额聚合 | 原 CapacityGate 和 RateGate 引用 | get_web, get_academic, get_rate | RateGate | 是 | 调度 |
| runtime.py | 装配 | 同一 Web 共用 Gate，注入 HTTP Executor | Runtime.build | Config, Registries | 是 | 初始化 |
| providers/http.py / HttpJsonExecutor | 出站 HTTP | 限速、重试、Body preview | request_json, request_text | RateGate, httpx | 是 | 上游调用 |
| scheduler/fetch.py | 候选选择 | 优先可发请求者，取消等待 | FetchScheduler | Quotas | 是 | Fetch |
| observability.py | 日志 | UTC time、Body 安全预览和轮转 | KeyValueFormatter, log_event | logging, Redactor | 是 | 输出日志 |
| llm/stages.py + llm/prompts.py | Prompt 组织 | 保护指令与正文分区 | LLMStages | ChatMessage | 否 | LLM 输入 |
| providers/llm/openai_chat.py | LLM Body | 真实 JSON 总字节预算 | complete_text, complete_json | Http Executor | 否 | LLM 出站 |
| providers/agent/antigravity_cli.py | Agent Prompt | argv prompt 字节预算、stdout preview | run_antigravity | subprocess | 是 | Agent 出站 |
| providers/academic/openalex.py | OpenAlex Key | Bearer Header 和 search | OpenAlexProvider.search | SecretValue, HTTP | 否 | Paper Search |
| config.example.toml | 运维示例 | 推荐免费层速率与明确未知状态 | TOML | 官方文档 | 否 | 配置 |

## 5. Data Flow

### 5.1 启动

1. 加载 TOML，验证 provider 级 rate_limits 数组、min_interval_seconds、原 max_concurrency、输入 byte caps。
2. 如果 OpenAlex enabled：在配置解析期间检查有无 API Key 环境变量，缺失即 ConfigFailure，不连接匿名 API。
3. Runtime 为每个启用的 Web/Academic/OA 创建 Gate；Web Search 与 Fetch 共同引用同一个 RateGate。
4. LLM/Agent 仍然只受原有并发 Gate 控制；传入两个独立字节预算。

### 5.2 HTTP 调用与重试

1. Orchestrator 选择 provider 并按原逻辑占用 concurrency 许可。
2. RetryController 开始新 attempt；HTTP Executor 在 client.request 之前 await rate_gate.permit()。
3. RateGate 在锁内检查全部窗口+间隔，成功时立即预约，否则释放锁异步等待。取消等待不计数；刚预约后失败也计数。
4. HTTP Executor 按原策略完成 HTTP 状态检查、Response 诊断、重试等待。
5. 每次 attempt 最终发出一条双向 Body 预览（无响应则 null）；后备 provider 处理保持原逻辑。
6. FetchScheduler 可检查非预约等待时间，避免选中一个已到长周期限额的 provider 而忽略其他可用者。

### 5.3 LLM / Agent

1. Prompt Builder 产生受保护的 system/template/focus 以及可截断用户正文。
2. LLM 按实际 JSON 序列化开销计算剩余预算；Agent 按 -p prompt 编码字节计算。
3. 若超限：按 Unicode 字符边界保留正文头尾和标记，再编码并验证总字节数；不可压缩内容单独超限则预发请求失败。
4. 成功发送/调用后，debug 记录实际已截断内容的 8KiB 预览，以及原始/实际尺寸、输入裁剪标志、预览裁剪标志。
5. 任何 transport/解析/子进程失败继续沿现有上游异常路径返回；不将 Response 截断后交给业务。

### 5.4 日志

1. debug 未开启：跳过 Body 序列化和预览准备。
2. debug 开启：结构化脱敏 → UTF-8 头尾预览 → Formatter JSON 逃逸 → 最终 SecretRedactor。
3. Formatter 从 LogRecord.created 生成每行 UTC 毫秒 time；原有 request/elapsed_ms、同一行键值和轮转保持。

## 6. Interfaces & Contracts

- rate_limits：provider 共享配置中的可选数组，元素含 requests（整数）和 period_seconds（正数，单位秒）；支持多个窗口共存。
- min_interval_seconds：provider 共享配置中的可选有限正数秒；不存在即不启用。
- max_concurrency：独立生效，与上述速率字段不互相覆盖。
- input_limits.llm_request_body_bytes：正整数，缺省 131072；对真实 LLM HTTP JSON Body 生效。
- input_limits.agent_prompt_bytes：正整数，缺省 98304；对 Agent CLI 单个 prompt 参数生效。
- OpenAlex：启用时 api_key_env 必需，API Key 只从环境变量读取；Authorization Bearer，不拼接查询串。
- ResolvedRateWindow 与 ResolvedRatePolicy：不可变配置对象；解析时验证，执行时不再检查类型。
- RateGate.permit：协程，完成全部条件的原子预约；取消安全。RateGate.next_available_in_seconds：只读建议，不保证预约。
- HttpJsonExecutor 新增可选 rate_gate 入参；不传时保持现有调用行为，兼容测试直接构造实例的情况。
- truncate_utf8_head_tail：返回裁剪后文本、原/后字节数和布尔标志；正文总额必须为最终实际发送预算。
- upstream_body_preview：每个实际 attempt 一条 DEBUG 事件；Request 与 Response 原始预览字段可为 null，采用两个不同的 truncated 标志区分请求本身裁剪和日志预览裁剪。
- 兼容性：除 enabled OpenAlex Key 要求外旧配置仍有效；不修改 Search/Fetch/Paper 的对外结果模式或重试总次数。

架构、Error Handling 与 Testing 设计均已确认并实施；LLM 默认 128 KiB、Agent 默认 96 KiB。对应实施计划保留各任务的测试证据、真实上游验证结果及限制。
