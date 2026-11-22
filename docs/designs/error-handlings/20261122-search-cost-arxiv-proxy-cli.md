# Error Handling: 搜索成本、arXiv 查询、Provider 代理与 CLI 等待

- 日期：2026-11-22
- 状态：设计稿，待用户审核；未实施、未提交
- 架构依据：../architectures/20261122-search-cost-arxiv-proxy-cli.md
- 原则：**配置错误尽早失败，外部调用按现有重试策略，日志安全且可诊断，不新增全局异常处理框架。**

## 1. 故障分类和边界

| 故障场景 | 捕获位置 | 预期对外行为 | 日志与安全要求 | 重试 |
|---|---|---|---|---|
| use_proxy 非布尔值（如 "false"、1） | config.py 配置解析 | ConfigFailure(CONFIG_ERROR)，指出字段路径，不泄漏值 | 不记录代理凭据 | 不启动 |
| Firecrawl search_limit 非法、scrape_formats/parsers 类型或选项非法 | Firecrawl 配置解析/Adapter 初始化前 | ConfigFailure(CONFIG_ERROR) | 明确 Provider 和字段 | 不启动 |
| arXiv query_fields 空、重复、非法；max_results、sort_by/order 不合法 | config.py 校验 + ArxivProvider 构造 | ConfigFailure(CONFIG_ERROR) | 明确字段，不回显用户复杂查询 | 不启动 |
| use_proxy=false 时环境证书路径无效 | Runtime 的安全 SSL Context 构建 | ConfigFailure(CONFIG_ERROR) 或现有受控启动异常；不得静默降级为 verify=False | 路径按现有安全日志策略处理 | 不启动 |
| 代理不可达或目标不可达导致 ConnectError | HttpJsonExecutor | 沿用现有 transport failure 和重试；故障归属当前 Provider | 保留 provider/stage/attempt/transport_type | 按原 RetryPolicy |
| PoolTimeout（包括代理问题导致） | HttpJsonExecutor | 沿用现有超时和重试路径 | 与 ConnectError 区分；不臆测根因 | 按原 RetryPolicy |
| Firecrawl Search 未返回正文 | FirecrawlAdapter → Keyword Pipeline | **正常元数据结果**；URL/标题/摘要仍可采纳 | 不视为 body_rejected 的业务失败 | 无新重试 |
| Firecrawl Scrape 只有 base64/无文本 | FirecrawlAdapter.fetch | 沿用“无可用正文”的 Provider 失败或调度备用路径 | 只记录类别/尺寸，不保存 base64/PDF | 不增加重试次数 |
| Firecrawl 返回 402/429/5xx/JSON 失败 | HttpJsonExecutor 与 Adapter | 沿用既有状态、Provider 协议错误和 Fetch fallback | HTTP 状态、阶段、受控摘要 | 按原规则 |
| arXiv HTTP 429/5xx 或无效 Atom XML | HttpJsonExecutor / ArxivProvider | 沿用 retry、protocol failure、部分 Provider 可用的聚合逻辑 | 原有拒绝统计、耗时 | 按原规则 |
| CLI 收到提前 EOF、非法 NDJSON、大帧 | protocol.send_request | 现有 ProtocolFailure | 不改变错误码/消息契约 | 不增加 |
| CLI 显式 timeout 值超过 | protocol.send_request | 仍返回现有超时协议错误并清理客户端 socket | 不要求 Daemon 中止任务 | 不增加 |
| CLI 默认无限等待但用户 Ctrl+C | CLI/asyncio | 保留当前取消/进程退出，客户端 socket 清理 | Daemon 可继续工作 | 不新增取消传播 |

## 2. Provider 代理配置错误与隔离

### 2.1 配置结构校验

- use_proxy 是 **通用 Provider 级布尔值**，不是特定 Adapter 的选项。必须出现在 Web、Academic、OA、LLM 共享配置路径，不能混进 options、extra_body 或请求载荷。
- 默认 True，保持 HTTPX 环境代理决策与 NO_PROXY；False 时直连。未知 Provider 名称与不合法配置仍按现有规则拒绝。
- 无需单独的 proxy_url、备用代理、环境变量热更新或超时后的自动代理切换。
- 不修改共享 os.environ，也不清空 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY；不得影响其他 Provider、后台作业或 Agent CLI 子进程。
- 实际 HTTP 请求使用各自长期存在的 AsyncClient；同一 Provider 的 Search/Fetch 共用代理选择和现有连接池。

### 2.2 HTTPS 信任与代理的解耦

HTTPX 的 trust_env=False 同时跳过 HTTP(S)_PROXY 和 SSL_CERT_FILE/SSL_CERT_DIR 等环境设置。因此直连时不得只传 trust_env=False 而忽略自定义 CA。

建议在 Runtime 创建直连 Client 前构造受信 SSLContext，遵循 HTTPX 已有 trust_env=True 时的 CA 选择优先级；将其作为 verify 传给客户端，并对该客户端传 trust_env=False。依赖版本 pinned 0.28.1 已验证 HTTPX 暴露 create_ssl_context(verify=True, trust_env=True)；如需兼容项目允许的更早版本，实施前须以公开 API 或等价标准 SSLContext 验证。

- 不允许 verify=False、静默更换 CA、输出 PEM 或证书目录内容。
- 环境 CA 文件损坏时拒绝创建直连客户端；不要自动回退到代理或忽略校验。
- 保留已有凭据 SecretValue 与日志脱敏；日志仅可输出 direct/env_proxy 模式，不回显环境代理 URL 或凭据。
- 工厂注入函数接收自定义构造参数时必须通过协议/测试显式处理；不能在 TypeError 时悄悄退回带代理的默认客户端。

### 2.3 网络失效与连接池

用户已确认此次 **不修改全局 RetryPolicy、知乎并发数、连接池重建和底层 httpcore 修复**。本地模拟显示代理隧道 TLS 失败可能导致未释放连接槽位，继而 PoolTimeout；直连能够避开这一触发路径，但并不能保证目标 API 成功。不能把“所有 ConnectError 都来自代理”或“连接槽位一定泄漏”视为已确认事实。

配置示例中知乎推荐 use_proxy=false，不对其他 Provider 强制直连。若直连仍返回 ConnectError，继续按现有 transport failure 对外报错；后续独立排障，不在本次自动切换连接方式。

## 3. Firecrawl 成本与正文异常

### 3.1 Search 元数据是成功路径

- Search 请求 **不携带 scrapeOptions**；不是“传入空 formats / null 然后依赖服务端解释”。
- 即使 Firecrawl 返回含有意外的 markdown/rawHtml 字段，Adapter 的 KeywordSearchHit.raw_content/content 也保持空，阻止对隐式正文产生下游清洗或收费假设。
- URL 无效、标题与摘要同时为空等业务校验仍按原规则运行；不能因为没有正文就拒绝所有 Search hits。
- search_limit 严格 1..100；search_highlights 仅布尔；Search 仍只使用 web 数据源，不能追加带额外计费语义的 sources。

### 3.2 Scrape 解析与 PDF 账单

- scrape_formats 非空、无重复，第一版只允许 markdown/rawHtml（现有 Fetch 能直接消费）；可显式配置两者同时取。
- scrape_parsers 是列表，默认 [] 表示不启用 pdf/image 的转换解析；只接受 pdf/image 这些已确认格式，拒绝对象或不受支持的类型，避免意外高费。
- **必须在文档中告知**：parsers=[] 遇到 PDF 时仍可能以 base64 返回并计固定费用；这不等于“完全禁止触达 PDF”或“零 credits”。
- 不可用的 PDF base64 不是文本正文；Adapter 保留既有“空正文”失败语义，FetchScheduler 可按现有规则寻找备用 Provider，而不是把 base64 填入 raw_content 交给 LLM。
- 开启 pdf parser 是用户显式选择且可能按页收费；Gateway 不保证上游账单数值，未增加费用追踪或缓存跨请求去重。
- 错误状态（如 402 额度耗尽）沿用当前 HttpJsonExecutor/Provider Adapter 和 fallback，不能因为费用不足而在隐藏位置发送更多新请求。

## 4. arXiv 语法、防注入和可观测性

- 输入仍是普通自然语言关键词，既有 Paper Search 层先验证非空。
- Provider 只将输入按 **字面词/双引号短语** 编译为字段表达式；不直接拼接用户的 AND/OR/括号为 API 语法。
- 特殊字符、冒号、括号、反斜线、引号必须做安全字面化/转义；未闭合引号或多个连续引号采用宽容的字面输入处理，不引入与其他 Academic Provider 不一致的新公开错误码。实现须保证生成的查询表达式不会包含注入的布尔结构。
- 对无有效术语的非空输入沿用已有 Provider 故障路径；不修改 PaperOrchestrator 的统一异常汇总逻辑。
- query_fields 只允许 ti/abs；非法字段、重复或空数组在启动时拒绝。max_results 1..100；sort_by 限于 relevance/submittedDate/lastUpdatedDate；sort_order 限于 ascending/descending。
- 只执行一次原有 arXiv HTTP 请求，不在 429 后增加新的松弛检索请求；保留现有 RateGate 和 Retry-After 处理。
- arXiv HTTP 200 但 Atom XML 解析失败仍按既有 ProtocolFailure 处理，不让查询构造新逻辑吞掉 Provider 错误。

## 5. CLI 等待、断开和资源清理

- 取消 600 秒 **默认等待上限**，不取消 Socket 连接、NDJSON 格式或错误处理。
- 用户/调用方显式传 response_timeout_seconds 的请求仍按该值等待；timeout=None 表示无限等待，不把该值作为“沿用 600 秒”的哨兵。
- 即使默认无限等待，connect、write、read 仍可能由于 Daemon 退出、EOF、系统错误或用户中断而终止；不把“无限等待”误写成永远不会失败。
- 现有 finally 内关闭 CLI 自己持有的 socket 端点；Daemon 在发送响应后观察 EOF，按原有逻辑关闭服务端端点。
- 当 CLI 意外退出时，Daemon 可以继续执行原工作流，可能产生 credits/结果文件，作为用户确认的有意取舍；不新增断线监测任务。
- 不改业务请求帧长度、响应帧长度、ErrorCode、ResultWriter 输出格式。

## 6. 安全日志与诊断边界

- 使用现有 SecretRedactor、http_endpoint_for_log、request_id、provider、stage 和 attempt。
- 不把 API Key、代理凭据、证书路径内容、PDF base64 或完整搜索请求新增写入普通 INFO 日志。
- 不新增全局 httpcore DEBUG 噪声或未经脱敏的原始堆栈日志；已存在的 DEBUG BodyPreview 不扩展数据量。
- 上游平台 credits 不一定与请求数一一对应，发生高额消费时需要平台明细验证；本次措施降低隐式抓取概率，不承诺 credits 上限。
