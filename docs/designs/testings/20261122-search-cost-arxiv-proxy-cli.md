# Testing: 搜索成本、arXiv 查询、Provider 代理与 CLI 等待

- 日期：2026-11-22
- 状态：设计稿，待用户审核；未实施、未提交
- 架构依据：../architectures/20261122-search-cost-arxiv-proxy-cli.md
- 错误处理依据：../error-handlings/20261122-search-cost-arxiv-proxy-cli.md

## 1. 目标与验收标准

测试必须证明：

1. Firecrawl Keyword Search **永不隐式请求正文**；返回 URL/title/snippet 的 Search 仍产生原有 JSONL 结果，且不触发只适用于正文的 judge/clean 阶段。
2. Firecrawl Fetch 按配置格式返回可用文本，默认关闭 PDF 逐页解析；遇到只返回 base64 的 PDF 响应，不当作正文。
3. arXiv 生成的字段、布尔运算和短语表达式正确编码；只产生一次 API 请求，保留全部原有响应映射与限速。
4. Web、Academic、OA Resolver、LLM 的 HTTP 客户端都能按 Provider 独立选择 env_proxy/direct，省略 use_proxy 等于现有行为；不会修改进程环境或禁用 TLS 验证。
5. CLI 默认等待时不因 600 秒而退出，显式覆盖仍有效，Socket 数据帧及现有 Daemon 行为不变。
6. 不更改全局 retry、quota、rate gate、公开 CLI 参数和现有日志敏感信息保护。

## 2. 分层测试与具体文件

### 2.1 配置与注册（纯单元测试）

目标文件：

- tests/unit/test_config_web_providers.py
- tests/unit/test_config_academic_providers.py
- tests/unit/test_config_llm.py
- tests/providers/academic/test_registry_defaults.py
- tests/runtime/test_runtime_assembly.py、test_academic_runtime_assembly.py（必要时）

用例矩阵：

| 分组 | 输入 | 断言 |
|---|---|---|
| Web、Academic、OA、LLM | 省略 use_proxy | 解析得到 true；其他配置字段完全不变 |
| 四类 Provider | use_proxy=true/false | 解析为对应 bool；不透传给 Adapter options |
| 四类 Provider | use_proxy="false"、0、1、null、数组 | 提前失败为 ConfigFailure(CONFIG_ERROR)，含字段路径 |
| Web Firecrawl | search_limit=10、search_highlights=false、scrape_formats=["markdown"]、scrape_parsers=[] | 全部可解析，Default 语义清晰 |
| Web Firecrawl | search_limit 0、101、bool；formats 空、重复、非文本格式；parsers 含未知项 | 清晰配置错误 |
| Web 其他 Provider | 只增加 use_proxy | 原有配置白名单与 Adapter 初始化成功 |
| arXiv | query_fields=["ti","abs"], max_results=10, sort_by=relevance, sort_order=descending | 可解析，默认保持 |
| arXiv | query_fields 为空/重复/含未知值，max_results=0/101，排序值非法 | 清晰配置错误 |
| 非 arXiv Academic | 传入 query_fields | 仍按原有白名单拒绝 |
| 所有分类 | 禁用 Provider、仅引用 LLM Provider、OA Resolver 未启用 | 沿用既有启用与鉴权检查行为 |

### 2.2 HTTP 客户端代理和 SSL（Runtime 层）

目标文件：tests/runtime/test_runtime_assembly.py、tests/runtime/test_academic_runtime_assembly.py，必要时增加一个纯助手单测。

使用可记录 **参数** 的注入 HttpClientFactory/MockTransport，完全不需要访问真实提供商。

- 带 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY 环境，省略 use_proxy 时按现有默认创建客户端。
- 同时配置 Web zhihu=false、firecrawl=true、Academic arxiv=true、OA unpaywall=false、LLM openrouter=true：逐个断言仅对应 Provider 的代理模式发生改变。
- false 的客户端实际绕过模拟 HTTP 代理；true 的客户端仍遵从环境代理 / NO_PROXY。测试使用本地假代理和受控目标地址，不发送真实 API。
- false 时透传 verify=SSLContext，context.verify_mode 仍为 CERT_REQUIRED、check_hostname 有效；测试 SSL_CERT_FILE 和 SSL_CERT_DIR 的有效/错误场景，确保 CA 配置不被静默丢弃。
- 构造 factory 接受参数的兼容路径：true 继续支持不带参数的原注入场景；false 必须以明确参数方式构造，不允许捕获 TypeError 然后退回默认代理。
- 分别持有两个 Provider 的客户端，在一个连接失败时，另一个的代理选择、连接对象与重试设置不改变。
- Daemon 关闭运行时后所有 HTTP Executor 的 aclose 仍按原有流程执行；重试、RateGate、SecretRedactor 不受影响。

### 2.3 Firecrawl Adapter 与业务 Pipeline

目标文件：

- tests/providers/web/test_firecrawl.py
- tests/orchestrators/ 或相关关键词编排测试
- tests/fixtures/providers/firecrawl/（在需要时新增元数据 Search fixture）

示例（仅示意载荷，不打真实 Firecrawl API）：

~~~json
{
  "query": "graph transformers",
  "sources": ["web"],
  "limit": 10
}
~~~

断言：

1. Search 载荷**不存在 scrapeOptions 字段**；即使开启/关闭 highlights，也只能影响元数据搜索参数。不得发送隐藏的第二次 scrape。
2. Search 响应包含预料之外的 markdown/rawHtml 时，KeywordSearchHit 的 raw_content/content 仍为空；URL、title、description 和拒绝计数映射正常。
3. 运行 Keyword Pipeline：没有正文时保留 URL 记录；cheap_check/LLM judge 不因 Firecrawl 调用；其他 Provider 有正文时保留现有 judge 行为。
4. Fetch 默认载荷含 formats=["markdown"], parsers=[]；在配置 ["markdown","rawHtml"] / ["pdf"] 时只在 Fetch 端按要求改变；无额外 fetch URL/重试轮次。
5. 只返回原始 PDF base64、无 markdown/rawHtml 时适配器返回既有“无可用文本”语义，Scheduler 能尝试下一个允许的 Fetch Provider；不把 base64 传给 LLM。
6. 402、429、5xx、invalid JSON、success=false、空 Web 列表和不合法结果的现有错误/日志断言继续通过。

费用性质的验收约束：测试断言**没有隐式 Scrape 的出站调用**、PDF parser 显式禁用，而不是伪造“0 credits”保证；真实账单只有厂商能够证实。

### 2.4 arXiv 纯查询编译与 Adapter

目标文件：tests/providers/academic/test_arxiv.py

| 输入 query | 预期 search_query（默认字段） |
|---|---|
| graph | (ti:graph OR abs:graph) |
| graph transformers | (ti:graph OR abs:graph) AND (ti:transformers OR abs:transformers) |
| "graph neural networks" | (ti:"graph neural networks" OR abs:"graph neural networks") |
| "graph neural networks" molecular | (ti:"graph neural networks" OR abs:"graph neural networks") AND (ti:molecular OR abs:molecular) |
| graph transformers，query_fields=["ti"] | (ti:graph) AND (ti:transformers) |

补充检验：

- 含额外空格、Unicode、连字符、引用的原始 Boolean 词、引号不匹配、冒号、括号、百分号的输入必须按字面处理，不得被当作 API 布尔操作符注入。
- 明确引号短语保持原样，不意外去除其内部空格/转义。
- query_fields 自定义顺序保持、输出 deterministic；HTTPX params 自动 URL 编码，避免手工重复 percent encoding。
- 默认 max_results=10、sortBy=relevance、sortOrder=descending；合法配置值能映射到 arXiv 参数，非法值在创建 Provider 前拒绝。
- 所有用例只产生 1 个受控模拟 GET 请求；原有 Atom XML、URL/DOI、作者、主题、日期及候选数测试全部保留。
- 仍通过 Academic RateGate，原有 429 与 Retry-After 测试通过，不新增自动放宽第二轮查询。

### 2.5 CLI 默认等待与 socket 协议

目标文件：

- tests/unit/test_socket_client.py
- tests/cli/test_cli.py
- tests/daemon/ 下现有连接生命周期测试
- tests/unit/test_protocol_codec.py（回归）

不通过真实等待 600 秒来测试：替换 asyncio.wait_for 为记录 timeout 的受控函数或使用 fake reader/writer，确认发送普通业务请求时 timeout=None，agent-search 同样 None；显式 response_timeout_seconds（如 0.01）仍按有限值执行原有超时分支。

测试矩阵：

- keyword-search、paper-search、url-fetch、llm-search、agent-search 五种请求均不再收到隐式 600 秒等待限制。
- 显式 timeout 触发 ProtocolFailure，finally 中关闭客户端 socket。
- 收到成功响应、ErrorResponse、提前 EOF、无换行、非法 JSON、超大帧时都维持现有行为。
- 客户端取消/退出后 Daemon 无新增 cancellation watcher、无强制取消；业务工作流完成仍可以写入结果，符合用户明确接受的异常情况。
- CLI 改动仅涉及客户端等待；Daemon 的 start/stop、graceful shutdown、request_id 与 NDJSON 兼容性全部保留。

## 3. 端到端场景（模拟网络，非收费）

1. **知乎直连、其他走代理**：通过配置解析构造 Runtime，用本地 MockTransport + 本地 HTTP 代理模型确认 zhihu 的 direct，Firecrawl 的 env_proxy，Academic/LLM/OA 混合设置也互不污染。
2. **先 Keyword 后 Fetch**：Firecrawl 返回元数据→JSONL 中包含 URL→url-fetch 触发显式 scrape→正常正文或备用 Provider→继续现有安全检查与内容处理。
3. **arXiv 检索**：CLI paper-search "graph transformers" 选定 arxiv→单次 GET 编译查询→Atom 解析→聚合→写出文件，数量和排序与配置一致。
4. **长执行 CLI**：模拟工作流耗时超过旧 600 秒但最终有合法响应，验证没有隐式超时并能读到结果；使用虚拟时间而不是等待 10 分钟。
5. **回归旧配置**：在未配置 use_proxy、未增加 arXiv 额外参数时，HTTP Provider 与既有请求/响应的兼容性测试无回退。

## 4. 执行建议与验收命令

实现阶段先运行目标测试，再运行全套 lint、typecheck、pytest：

~~~bash
uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/providers/web/test_firecrawl.py tests/providers/academic/test_arxiv.py tests/unit/test_socket_client.py
uv run pytest tests/runtime tests/cli tests/daemon tests/providers
uv run ruff check .
uv run mypy src
uv run pytest
~~~

执行后应在实施报告中写明完整命令、通过/失败数量、如有失败的原因与需要排除的环境问题。不要把“未运行”“测试定义存在”表述为通过。

### 真实 API Smoke Tests（仅在明确授权并确保可用环境时）

- 可选：以 --provider=arxiv 对一个短查询进行一次低频检索，遵守 arXiv 间隔和当前重试配置。用户现有环境可能设置多次尝试、每次 1800 秒，请先核对运行配置。
- 可选：以 --provider=zhihu 在 use_proxy=false 下执行单次搜索，同时记录连接是否成功；避免拿实际 API Key 做大规模重试。
- Firecrawl 可能收费：不自动发起真实 Scrape；如确认执行一次 Search，必须确认无 scrapeOptions 和搜索结果数量，仍可能产生 Search 基础费用。
- 保证任何测试不输出 API Key、代理凭据或原始 PDF base64。

## 5. 退出标准

- 所有配置分组与默认行为都有测试；不支持的 Provider 参数不会被默许。
- Firecrawl 没有隐式抓取；PDF 可能发生的固定费用和无文本响应都在文档中明确说明。
- arXiv 的关键词 AND/字段 OR 结果确定、可测试，并保留现有 Provider 结果契约。
- 代理 direct/env_proxy 各类 Provider 都可配置，TLS CA 信任未退化，且原有客户端工厂测试可注入。
- CLI 默认无 600 秒超时，错误/显式超时/socket 资源清理回归通过。
- 不修改其他 Adapter、不增加外部 Agent CLI 的代理能力、不新增连接池自愈和 Daemon 取消协议；未经实现不能宣称以上验收完成。
