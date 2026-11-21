# Testing: 百度千帆双分支、open-webSearch Doctor 与知乎配置

- 日期：2026-11-21
- 状态：Draft for user review — 仅测试设计，尚未实施或运行测试
- 基线：e712f10 (v1.0.0)
- 同组设计：[架构](../architectures/20261121-baiduqianfan-doctor-zhihu.md) / [错误处理](../error-handlings/20261121-baiduqianfan-doctor-zhihu.md)
- 约束：最小修改与最小侵入。复用知乎、open-webSearch、HTTP executor、Doctor 等现有测试基础设施。

## 1. 策略与验证层次

使用现有 pytest、httpx.MockTransport、RecordingJsonExecutor、ManualClock、pytest caplog、受控 fixtures；禁止新增第二套 HTTP 模拟系统或测试框架。确定性离线测试必须是主要验收依据；真实 API 冒烟测试只能显式 opt-in，不作为普通 CI 的通过前提。

测试只能使用假的密钥和非敏感示例 URL。不同 Baidu API 的输入/响应 shape 使用**独立**、来自官方文档或最小脱敏样本的 fixture。若官方响应与设计中的字段路径假设存在差异，应先更新设计与 fixtures 再实现解析器，不可凭猜测无限递归找 URL。日志断言遵循项目已存在的脱敏与截断 body preview 行为，不凭空新增“任何 debug 日志都不能包含查询”的限制。

| 类型 | 候选测试位置 | 验证重点 |
|---|---|---|
| Baidu 适配器 | tests/providers/web/test_baiduqianfan.py（新增） | 双路请求、并行性、字段、部分失败、取消 |
| Baidu fixtures | tests/fixtures/providers/baiduqianfan/（新增） | Web Search / Agentic 各自成功、合法空结果、多模态、业务错误、非法数据 |
| Registry / config | tests/providers/test_registry.py、tests/unit/test_config_web_providers.py | 一个提供商、Search-only、环境变量、选项和 rate_limits |
| Runtime / gate | tests/runtime/test_new_web_runtime_assembly.py、tests/runtime/test_rate_gate.py 或 adapter 定向测试 | 共用一个 HttpJsonExecutor 和 RateGate、物理请求计数 |
| Search workflow | tests/orchestrators/test_keyword_search_pipeline.py、test_keyword_provider_filtering.py | 选择提供商、规范化、去重、原文处理 |
| Doctor | tests/doctor/test_open_websearch_health.py（新增）、test_no_network.py、test_config.py、test_socket.py | 仅已启用时 GET /health，失败/超时/非法响应及状态聚合 |
| 日志、文档、回归 | tests/observability/、tests/docs/test_documented_config.py、tests/acceptance/ | 安全诊断、示例配置、公共结果协议保持不变 |
| 可选 live | tests/integration/test_live_baiduqianfan.py（新增） | 一次真实双分支调用；仅经显式启用 |

## 2. Baidu 请求及成功结果测试

优先用真实 HttpJsonExecutor 搭配 MockTransport，直接检查发出的 HTTP 请求；模拟任务竞争和取消则借鉴 tests/providers/web/test_zhihu.py 的 asyncio.Event 模式。

| ID | 场景 | 关键断言 |
|---|---|---|
| BQ-01 | 单次 baiduqianfan.search(query) | 精确两次 POST：/v2/ai_search/web_search 与 /v2/ai_search/agentic_search，均使用配置的同一个 API 主机 |
| BQ-02 | 检查 POST headers/body | 两个分支带相同 Bearer API Key；Web Search 使用官方 messages/search_source/edition 等字段，Agentic 使用 query/count；不误混请求结构 |
| BQ-03 | 两个分支分别提供正确官方成功 fixture | 两组 URL 都转成 KeywordSearchHit；固定分支顺序汇总；无多余 HTTP 请求 |
| BQ-04 | 使用 Event 阻塞两个模拟 HTTP 请求 | 任一分支完成前两个分支都已发起，证明实际并行而不是串行 |
| BQ-05 | Web Search 包含网页、图片、视频 URL | 读取官方支持的顶层或指定嵌套 URL；有效 HTTP(S) URL 保留；媒体命中正文为空 |
| BQ-06 | Agentic 包含 web_pages、images、videos 和卡片 | 仅返回带真实 URL 的条目；没有 URL 的卡片不伪造；无媒体二进制处理 |
| BQ-07 | Agentic 网页含 snippet、content | snippet 映射摘要，content 映射 raw_content（不视作已清洗 content）；Web Search 的短节选不伪装全文 |
| BQ-08 | 中文、特殊字符、原样 query | 两个分支接收同一输入；无新语义改写、字节截断或错误 URL query 参数拼接 |
| BQ-09 | 两分支合法空结果 | 返回 []，不误报协议失败或整体失败 |
| BQ-10 | 有效与坏条目混合（缺 URL、非法 scheme、错误字段类型） | 坏条目只单条拒绝，其余保留；CandidateCounts received/accepted/rejected 正确 |
| BQ-11 | 两个分支返回重复/带追踪参数的 URL | 现有 SearchOrchestrator 和 URLStore 负责规范化去重；不要求 adapter 额外去重 |
| BQ-12 | raw_content 正文命中进入 Search -> URL Fetch | cheap_check/judge/content-clean 及既有安全检查继续生效；不会绕过正文清洗或把 URL-only 图片当成文本 |

对缺少 title/snippet 但 URL 有效的媒体结果，验证填写诚实的类型标签，使旧编排器不会因 empty_abstract 丢弃条目；不改变 KeywordSearchHit 契约。

## 3. Baidu 失败、重试及任务取消

| ID | 场景 | 关键断言 |
|---|---|---|
| BQ-13 | Web 成功、Agentic HTTP 403 | 返回 Web 命中；仅 Agentic 分支有 provider_partial_failure；逻辑 provider_completed 一次 |
| BQ-14 | Agentic 成功、Web 业务/协议失败 | 返回 Agentic 命中；partial_failure 分支标为 web_search |
| BQ-15 | 一路成功且返回 []、另一路失败 | 逻辑搜索成功返回 []，不把有效零结果当失败 |
| BQ-16 | 两路都 HTTP/协议/业务失败 | 现有 ExecutionFailure / ALL_PROVIDERS_FAILED；逻辑 provider_failed 一次；不对整个 search 二次重试 |
| BQ-17 | HTTP 200 但含明确上游错误 code | 当作业务失败而非成功空命中，错误诊断仅记录安全字段 |
| BQ-18 | 非 JSON、缺必需 envelope、错误 result list 类型 | 分支失败；另一分支若正常则可降级成功 |
| BQ-19 | HTTP 408、5xx、传输断连、超时 | 继续用现有 RetryPolicy；尝试次数上限与 Retry-After 行为不变 |
| BQ-20 | HTTP 429 + RATE_LIMIT_SEARCH_QPS | 可重试的频率限制，使用全局退避和共享 rate gate |
| BQ-21 | HTTP 429 + QUOTA_USER_DAILY_FREE 或 BILLING_INSUFFICIENT_BALANCE | 用既有 response_inspector 分类为 non-retryable；不重复发送；另一分支仍可成功 |
| BQ-22 | HTTP 429 + 未知 code | 默认仍按现有 HTTP 429 策略重试，不猜测错误类别 |
| BQ-23 | 一路抛 asyncio.CancelledError、兄弟任务阻塞 | 取消原样传播；兄弟任务被取消并 await；无错误的 partial_failure/成功终态 |
| BQ-24 | 用户取消整个搜索 | 两个子任务正确清理，无泄漏在途工作 |
| BQ-25 | 分支部分坏记录 | 各成功分支都有 provider_candidates_summary；无泄漏凭证或整份响应 |

测试需覆盖 HttpJsonExecutor 在普通 HTTP 状态检查前调用 response_inspector 的既有语义，用它区分可重试的 QPS 429 与非可重试的额度 429。不得为百度另写重试循环，也不修改公共 HTTP 429 规则。官方 error code 的准确位置必须以独立 fixture 验证。

## 4. 共享限流与配置测试

| ID | 场景 | 关键断言 |
|---|---|---|
| RATE-01 | 一次双分支搜索，示例 3 requests / 1s 限流 | 两个实际 HTTP POST 分别获取 permit，不能把整个逻辑搜索只计数一次 |
| RATE-02 | 同一时间发起两次双分支搜索（共四次 POST） | 使用 ManualClock，前三次可发送，第四次必须等待窗口放行，不依赖真实时间 |
| RATE-03 | 某分支重试 HTTP 429 | 物理重试再次计入 gate；等待超过当前请求预算时沿用 rate_limit_wait_timeout |
| RATE-04 | 配置删掉 rate_limits | 沿用既有无限频率配置语义，不能增加隐含限制 |
| RATE-05 | 用户提交非法 rate_limits 或不支持的 Fetch/选项 | 现有 resolve_config 拒绝；不扩展配置解析器 |

注册和配置还要验证：registry 仅有一个 baiduqianfan，Search=True、Fetch=False，factory/允许配置键正确；enable_search=true 时必须有非空 BAIDUQIANFAN_API_KEY 所指凭证，未启用时不强制提供；未知键、enable_fetch=true 被现有配置机制拒绝。Runtime 注入一个共享 executor 和 RateGate，两个分支使用同一个服务限流预算。

## 5. open-webSearch Doctor 测试

新的测试优先放在 tests/doctor/test_open_websearch_health.py。用可注入异步探针、MockTransport 或本地受控 HTTP 服务模拟 /health，禁用真实网络，**不启动或终止用户的 daemon**。

| ID | 条件 | 关键断言 |
|---|---|---|
| DOC-01 | 未配置 open_websearch | 不创建 HTTP Client、不探测网络；既有 config、目录、Socket 检查保留 |
| DOC-02 | 配置存在但 enable_search=false、enable_fetch=false | 仍完全离线；现有 test_no_network 保持通过 |
| DOC-03 | 仅 Search 启用 | 正好一次 GET <api_url>/health，成功时新增 [ok] |
| DOC-04 | 仅 Fetch 启用 | 同样执行一次 GET /health |
| DOC-05 | Search 和 Fetch 都启用 | 只检查同一个 daemon 一次，而不是每功能一次 |
| DOC-06 | 自定义 api_url、末尾带斜杠 | URL 拼接正确，不重复斜杠也不错误丢失服务根路径 |
| DOC-07 | HTTP 200 且 status=ok、data.daemon=running | 输出 [ok]；不声称所有内部搜索引擎均 ready |
| DOC-08 | 连接拒绝、DNS/网络失败 | 输出 [fail]，doctor exit_code=1；即使网关自己 Unix Socket 未启动（原有 [info]）也不能遮蔽该失败 |
| DOC-09 | 连接/读取超时 | 有界超时（设计 2 秒），输出 [fail]，不重试、不启动服务 |
| DOC-10 | HTTP 301/302/307、401、404、500 | 全部判 [fail]；不跟随重定向、无业务搜索请求 |
| DOC-11 | HTTP 200 但空 body、非 JSON、错误 status、daemon 非 running | [fail] invalid_health_response，而非假成功 |
| DOC-12 | 配置文件缺失/非法、Key 缺失或 Provider 配置无效 | 沿用配置检查失败；不执行健康探测；其他本地检查仍报告 |
| DOC-13 | 响应 body、异常消息或 api_url 中包含秘密/查询参数 | 日志和输出只保留静态安全错误类别/HTTP 状态；不打印任何原始 token、异常堆栈、body |
| DOC-14 | MockTransport 检查发出的请求 | 只有无凭证、无搜索 body 的 GET /health；不跟随重定向 |
| DOC-15 | 健康请求被取消 | CancelledError 传播，不伪造 OK/FAIL 或遗留额外异步任务 |

必须明确区分网关自身 Unix Socket 的 daemon 状态与独立 open-webSearch HTTP daemon 状态。原有 DoctorReport/DoctorStatus/退出码和 CLI 参数均不能变化。普通云端 Web/Academic/LLM Provider 不访问网络；Agent 命令检查保持当前静态行为。

## 6. 知乎、文档与全系统回归

- config.example.toml 的 zhihu 表仅新增说明注释，没有 rate_limits/min_interval_seconds；默认 max_concurrency、原有 HTTP 429/Retry-After 与双分支实现不变。
- tests/providers/web/test_zhihu.py、test_open_websearch.py、tests/providers/test_http_executor.py 按原基线通过，不修改既有适配器。
- tests/docs/test_documented_config.py 对新增 TOML 及文档参数和实际 registry 进行校验；如有“提供商总数”静态断言，只按新增 baiduqianfan 的事实更新，不跳过测试。
- tests/orchestrators 的 provider filtering、URL 正规化、去重、URLStore admission 和 raw_content staging 回归通过，JSONL 结果协议不变。
- 日志复用现有 http_attempt_started/completed、provider_response_diagnostic、http_retrying、http_failed、provider_candidates_summary、provider_partial_failure；任何一次逻辑搜索最多一个 provider_completed/provider_failed 终态。
- DEBUG 日志遵循当前安全、截断的请求/响应 body preview 规则，不新增绕过敏感信息过滤的 raw payload 日志。

## 7. 实施阶段验证顺序

1. **基线**：先运行已有 Zhihu、open_websearch、HTTP、doctor、registry/config 的相关测试；记录无关基线失败。
2. **红灯先行**：新增请求协议、双分支容错、Doctor 未启动/已启动、共享 RateGate 的失败测试，再逐项做最小实现。
3. **错误覆盖**：补齐 QPS vs 日额度 429、HTTP 200 业务错误、JSON schema、取消传播、图/视频 URL、正文映射、脱敏断言。
4. **定向回归**：运行 tests/providers/web、tests/doctor、tests/providers/test_http_executor.py、tests/runtime、tests/orchestrators、tests/docs 等涉及文件；确保旧行为未变。
5. **全量本地验证**：按当前 pyproject.toml 的项目工具使用完整 pytest、ruff、mypy（如果在项目配置中启用）。将预存失败和新引入失败区分记录。
6. **可选真实 API 冒烟**：仅当 WEB_SEARCH_RUN_INTEGRATION=1 且环境含 BAIDUQIANFAN_API_KEY、确认测试可能消耗免费额度或产生费用时，执行一次指定 baiduqianfan 的短查询。只收集安全的状态/计数/耗时，不输出 API Key/正文，不默认调用全部提供商。
7. **可选 daemon 实测**：已运行的 open-webSearch 应报告 healthy；已知未运行地址应报告 fail。该检查已有确定性离线单测，不以人为管理进程作为必需条件。
8. **实施与发布**：待三份设计文档经用户确认并提交后另行编写实施计划；当前不实施、不发 PR、不发布版本。

推荐实现阶段命令示意（本次设计阶段不执行）：

~~~bash
uv run pytest tests/providers/web/test_baiduqianfan.py tests/doctor/test_open_websearch_health.py -q
uv run pytest tests/providers/web/test_zhihu.py tests/providers/web/test_open_websearch.py tests/doctor -q
uv run pytest -q
uv run ruff check .
uv run mypy src
# 显式启用、仅测试一项，可能消耗额度
WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_baiduqianfan.py -q
~~~

## 8. Definition of Done

- [ ] 一个 baiduqianfan Provider 每次搜索调用两个独立官方 API；测试证明并行而不是串行。
- [ ] 单分支成功/失败、双失败、合法零结果、错误 envelope、任务取消及坏记录均有覆盖。
- [ ] 有效网页、图片和视频 URL 保留；Agentic 原始正文不绕过既有清洗与审查；去重与存储协议不变。
- [ ] 所有物理 HTTP 尝试/重试使用同一 RateGate；429 能区分可重试 QPS 和不可重试额度/账单问题。
- [ ] 已启用但未启动的 open-webSearch 被 doctor 识别为 FAIL；未启用时 doctor 不产生网络请求。
- [ ] 知乎保留未设置固定速率的事实，不引入虚构 QPS；现有相关测试全部通过。
- [ ] 所有新老测试和质量门结果有可复核记录；可选 live 与离线测试结果分开叙述；日志无未脱敏密钥。
- [ ] 用户确认本测试设计之后才提交三份设计规范；业务代码改动另行安排。

## Review Gate

待用户审阅这份测试设计后，三份 specs 获得统一批准再提交设计文档。本阶段不会修改业务代码。
