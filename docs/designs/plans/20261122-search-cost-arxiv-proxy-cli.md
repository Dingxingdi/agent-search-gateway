# 搜索成本、arXiv 查询、HTTP Provider 代理与 CLI 等待 Implementation Plan

**Goal:** 在现有工作流和协议内，取消 Firecrawl Search 的隐式抓取、配置化 arXiv 字段查询、按 HTTP Provider 选择环境代理或直连，并统一 CLI 默认无限等待响应。

**Architecture:** `docs/designs/architectures/20261122-search-cost-arxiv-proxy-cli.md`

**Error handling:** `docs/designs/error-handlings/20261122-search-cost-arxiv-proxy-cli.md`

**Testing:** `docs/designs/testings/20261122-search-cost-arxiv-proxy-cli.md`

---

## 执行上下文

- 日期：2026-11-22。
- 目标工作树：`/home/agent/agent-search-gateway/.worktrees/design-firecrawl-arxiv-provider-proxy-cli-20261122`。
- 分支：`design-firecrawl-arxiv-provider-proxy-cli-20261122`。
- 规划基线：`fccbef763cbc6f1aff1f3ae6cb886417348d8bfe`；其父提交 `e712f10` 为 v1.0.0。
- 本文件是实施计划。所有任务复选框表示未来实施工作；当前没有实现生产代码或运行这些测试。
- 已加载全局指令；主仓库和工作树的根 `AGENTS.md` 不存在，工作树中未发现更深层 `AGENTS.md/CLAUDE.md/GEMINI.md`。
- 延用这个工作树，不新建工作树，不切换主仓库分支，不修改实际用户配置或重启共享守护进程。
- 三份 spec 中的“待审核、未提交”是遗留状态文字；本计划依据用户明确要求继续编写，不据此重新发起设计确认，也不顺手修改 spec。

### 已核对的最近调用证据

读取范围为 `~/.cache/agent-search-gateway-cli/logs/debug.log` 及 `.1/.2/.3`。以下时间按日志 UTC 保留；不复制查询正文、凭据或完整 URL。

| 证据 | 已观察事实 | 对实施的含义 |
|---|---|---|
| `debug.log:5495–5912`，request `3d21ef4a` | 最新启动的请求为 2026-11-20 09:31:04Z 的 paper-search；先等待 arXiv quota；HTTP 尝试依次为 429、429、429、200；得到 10 个结果，Unpaywall 两次 resolved、一次 no_match；09:46:23Z 写出结果。workflow 的 `elapsed_ms=888711`。 | 后台能够在超过 600 秒后正常完成；保留 quota、Retry-After/重试、OA enrichment，消除客户端默认 600 秒上限。日志耗时和墙钟时间分别记录，不混算。 |
| `debug.log.1:2572–2822`，request `446966ad` | 最新启动的 keyword-search 中，Firecrawl 请求预览含 `scrapeOptions={"formats":["markdown","rawHtml"]}`；该次 Search 收到 402，第一次尝试即失败。 | 隐式抓取是已证实的出站行为；402 按现有策略不重试。不能把这条失败请求认定为用户所见的 200-credit 账单。 |
| `debug.log.3:10639` 起及 `debug.log:99` 起 | 可见知乎 ConnectError 与后续 PoolTimeout；末尾仍有旧请求的两个搜索分支重试，部分 PoolTimeout 的 `elapsed_ms` 约为 1,800,000。 | 分清建连失败与等待池槽位超时；配置直连以绕开可疑代理路径，保留现有错误分类。 |
| `debug.log:6159–6182` | 最新日志事件时间为 2026-11-20 14:20:05Z，属于更早的知乎请求；它不是最新启动的工作流。 | 不把日志最后一行误当作最新请求的起点，也不凭历史日志声称这些进程今天仍在运行。 |

代码和历史日志证明 Firecrawl Search 请求了正文；官方当前文档说明 PDF 解析可能按页计费，但历史 200-credit 单笔仍缺少供应商账单关联。知乎代理/TLS 失败导致连接池长期占用是 spec 记录的诊断假设；本轮没有复现或修补上游 HTTPX/httpcore，也没有历史 daemon 环境快照来把全部 ConnectError 归因于代理。

## 实施约束和确定的接口

### 变更边界

生产变更集中在七个现有文件：`config.py`、`runtime.py`、`providers/defaults.py`、`providers/web/firecrawl.py`、`providers/academic/defaults.py`、`providers/academic/arxiv.py`、`protocol.py`。

不修改 `providers/http.py`、`providers/web/zhihu.py`、其他 Adapter、`orchestrators/*.py`、`scheduler/fetch.py`、`daemon.py` 或 `cli.py` 的生产行为。不增加 CLI 参数、取消协议、查询扩展轮次、连接池重建、代理回退、费用预算或账单追踪。外部 Agent CLI 的 `pass_env` 与进程行为保持。

### 类型与默认值

| 范围 | 字段/接口 | 约定 |
|---|---|---|
| Web、Academic、OA、被引用的 LLM | `use_proxy: bool = True` | 省略/true 保留 HTTPX 环境代理及 NO_PROXY；false 直连；只接受真正的 bool。 |
| 配置数据类 | `ResolvedWebProviderConfig`、`ResolvedAcademicProviderConfig`、`ResolvedOAResolverConfig`、`LLMProviderConfig` | 新字段加在现有字段末尾并提供默认值，保留旧位置参数构造；保持 frozen。 |
| Web/Academic/OA options | `use_proxy` 为 shared key | 从 options 排除；不传给 Adapter、LLM extra_body 或请求体。 |
| HTTP 工厂 | `HttpClientFactory = Callable[..., httpx.AsyncClient]` | true 路径仍调用 `factory()`，兼容已有零参数测试工厂；false 必须支持 `trust_env=False, verify=SSLContext`；不捕获 TypeError 后回退到默认代理。 |
| Firecrawl | `search_limit=10`、`search_highlights=True`、`scrape_formats=("markdown",)`、`scrape_parsers=()` | TOML 接受数组，Adapter 保存 tuple，出站转 list；默认 highlights 显式发 true，固定与当前上游默认一致的行为。 |
| arXiv | `query_fields=("ti","abs")`、`max_results=10`、`sort_by="relevance"`、`sort_order="descending"` | 字段顺序保留；重复字段报错，不静默去重；默认值由构造函数提供。 |
| CLI | `send_request(..., response_timeout_seconds: float | None = None)` | 签名保持；None 始终表示不设响应等待截止时间，显式数值保持现有语义。 |

Provider 专属配置只校验用户显式给出的值，不要求 `Resolved.options` 填满默认值。错误使用已有 `ConfigFailure(CONFIG_ERROR)`，消息给出完整字段路径，不回显原值。

### arXiv 特殊字符的实施前验证门

普通词、普通完整短语及 ti/abs 的 AND/OR 组合有明确官方语法依据。**任意嵌入双引号/反斜线的无损字面化尚没有足够的全链路证据。**

已核对 arXiv 官方仓库 commit `f53ed1707d80a877e72ec989b2156c2e7bb01d87`：

- `classic_search_query.py` 的前置 tokenizer 用不识别 escaped quote 的双引号模式拆词；其 `remove_charactors` 只将 CR/LF 换为空格。
- 后续 `query_parser.py` 使用 Lark ESCAPED_STRING，但不能据此推断前置处理也支持相同 escaping。
- 此仓库版本不等于已确认的线上部署版本。
- URL percent encoding 与查询内部转义是两件事。MockTransport 只证明发出的字符串，不证明上游会按同一语法树解析。

Task 1 先产出有证据的特殊字符策略。不能通过删除引号/反斜线、改成全角字符或只用 `json.dumps` 来悄悄降低 spec 的要求。若无损与宽容处理无法同时得到上游支持，应把具体反例及最小 spec 调整交给用户决定；**只暂停受影响的查询字面化任务，其他独立任务继续**。这项门是已发现的技术约束，不是要求再次批准整个方案。

## 文件结构与责任

| 文件 | 责任/定位 |
|---|---|
| `src/agent_search_gateway/config.py` | shared keys（25–45）、四类配置数据类、三个 Web 返回分支、Academic/OA 解析、被引用 LLM 解析（607–647）；Firecrawl/arXiv 专属值校验。 |
| `src/agent_search_gateway/runtime.py` | 工厂类型（51）、启动前 TLS context 准备、共享客户端 helper、四类创建点（263/316/365/419）、安全模式摘要、正常关闭回归。 |
| `src/agent_search_gateway/providers/defaults.py` | 仅 Firecrawl registration 的配置白名单（40–45）。 |
| `src/agent_search_gateway/providers/web/firecrawl.py` | 构造选项（72–83）、Search 负载/映射（89–147）、Fetch 负载（149–158）；复用 `_page_body`（33–48）。 |
| `src/agent_search_gateway/providers/academic/defaults.py` | 仅 arXiv 扩大允许选项；共享 `_ALLOWED_OPTIONS` 不扩大。 |
| `src/agent_search_gateway/providers/academic/arxiv.py` | 私有纯 scanner/compiler、构造选项（60–67）、GET params（69–81）；Atom 映射保持。 |
| `src/agent_search_gateway/protocol.py` | 删除旧 600 秒常量/注释及 send_request 的 None→600 特判（29–31、271–272）。 |
| `tests/runtime/test_provider_proxy.py`（新增） | 代理模式、SSL、工厂注入、四类隔离及本地代理路由测试，避免继续扩大已有 756 行 runtime 测试文件。 |
| `tests/acceptance/test_search_cost_arxiv_proxy_cli.py`（新增） | 真实 Runtime/Adapter + mock 上游 + 本地 daemon/socket 的组合验收，避免继续扩大已有大型 acceptance 文件。 |
| `tests/unit/test_config_web_providers.py`、`test_config_academic_providers.py`、`test_config_llm.py` | 严格配置矩阵、disabled/unreferenced 和旧构造兼容。 |
| `tests/providers/web/test_firecrawl.py`、`tests/providers/academic/test_arxiv.py`、`tests/providers/academic/test_registry_defaults.py` | 负载、查询、正文、错误与旧断言更新。 |
| `tests/docs/test_documented_config.py` | 示例真实可解析/可组装，以及支持构造参数的文档测试工厂。 |
| `config.example.toml`、`README.md`、`docs/public-interface.md`、`CHANGELOG.md` | 用户选项、默认变化、费用/中断语义与 Unreleased。 |

所有行号仅帮助定位，实施时按符号确认。只有两份聚焦的新测试文件；不拆分现有生产架构，不引入通用配置框架。

## 执行顺序与测试约定

1. Task 1 处理 arXiv 字面化证据；Task 2–5 完成共用代理能力。
2. Task 6–8 完成 Firecrawl；Task 9–11 完成 arXiv；Task 12 完成 CLI。
3. Task 13 组合验收；Task 14 文档与示例；Task 15 最终检查。

各实现任务中的测试表表示若干独立的小循环：**每次选择一个行为，写测试→观察合理失败→最小实现→通过→整理**，不要先写全部实现。已有正确行为的回归测试允许一开始就通过，记录为回归基线，不人为制造失败。新增纯 helper 不存在导致的 import error 不是合格 RED：先用真实 Provider 出站行为暴露差异，或提供最小可导入声明后再观察行为断言失败。

所有命令在目标工作树执行。采用项目已有 pytest、pytest-asyncio、HTTPX MockTransport、临时 Unix socket；本地 TCP 代理仅监听 loopback。测试使用合成 Key 和例示域名，不读取实际用户配置。实现阶段先 `uv sync --locked --all-groups`；保留 `uv.lock`。普通完整测试使用 `env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest -v`，避免用户终端已设置集成测试开关时误调用真实服务。

---

### Task 1: 固定 arXiv 字面化的上游依据与反例

**Files:**

- Read: `src/agent_search_gateway/providers/academic/arxiv.py`
- Test: `tests/providers/academic/test_arxiv.py`
- Docs: `docs/designs/plans/20261122-search-cost-arxiv-proxy-cli.md` 的“特殊字符验证门”及 Task 10
- Docs: 本文末的 arXiv 官方手册及固定版本源码链接

- [ ] **Step 1: 写出失败场景及验证输入**

先列输入/预期 token 表：完整短语、短语内多空格、未闭合/连续引号、独立 AND/OR/ANDNOT/NOT、`au:example`、括号、百分号、Unicode、连字符、字面反斜线与嵌入双引号组合。要求每个输入词/短语只成为一个 literal，用户不能增加字段或布尔节点。保留 spec 中短语内部字符的要求。

- [ ] **Step 2: 验证证据，不用模拟服务替代语法证明**

阅读官方手册 §5.1 与完整前置处理/解析链。若使用本地重现，在临时验证目录保留输入、生成查询、实际解析出的词项/字段/操作符及结果；不向生产代码引入 Lark 或上游服务依赖。用上述反例检验候选 serializer，指出到底在哪一层失败。普通 HTTP 200、响应回显查询或自编 parser 的通过都不足以证明词项未被改写。

必要的实际 arXiv 验证仅使用公开 API、合成短查询及 `max_results=1`，遵守至少三秒间隔；不使用共享 daemon、不运行全 Provider smoke、不把真实请求作为默认测试依赖。当前用户已授权针对性 smoke，无需仅因该可选步骤重复请求许可；若要改变 spec 则仍需明确决策。

- [ ] **Step 3: 定义最小可实现策略**

~~~text
verified_literal_policy:
    定义 quote 开闭/嵌入 quote/backslash/未闭合 quote 的确定行为
    serializer 必须生成单个词项
    compiler 单独负责 ti/abs、OR、AND、括号
    HTTPX 只负责一次 URL 编码
~~~

策略确定后把明确输入/输出及证据写到 Task 10 测试中。若上游无法支持 spec 的无损要求，记录反例与最小可选归一策略，提交这一具体差异供确认；不得静默删除字符，Task 10/11/13 的 arXiv 验收保持未完成。

- [ ] **Step 4: 核实普通查询路径可独立进行**

运行现有基线：

~~~bash
uv run pytest tests/providers/academic/test_arxiv.py -q
~~~

预期：原有映射、HTTP 重试和 XML 诊断测试通过。此处是证据/基线任务，没有生产变更，不宣称已经证明新 compiler 正确。

- [ ] **Step 5: 整理验证结论**

只保留生成算法所必需的规则与具体反例。生产测试使用固定用例，不抓取当前网页或依赖联网；不把研究用 parser 复制成新的运行时系统。

### Task 2: 四类 HTTP Provider 解析 use_proxy

**Files:**

- Modify: `src/agent_search_gateway/config.py`（shared keys、四个数据类、各解析分支）
- Test: `tests/unit/test_config_web_providers.py`
- Test: `tests/unit/test_config_academic_providers.py`
- Test: `tests/unit/test_config_llm.py`

- [ ] **Step 1: Write the failing test**

在各现有测试文件增加 `test_*_provider_use_proxy_defaults_true`、`test_*_provider_use_proxy_is_strict_boolean` 等单行为用例。四类均覆盖省略/true/false；拒绝 `"false"`、0、1、None、空数组/对象。输入 None 通过直接字典构造，TOML 本身没有 null。

检查 options 不含 use_proxy；LLMInvocation.extra_body 不含它。Web 的禁用、无 Key、带 Key 三个返回分支全部覆盖；Academic 禁用及 OA 未启用仍验证显式字段类型但不强求凭据；未被引用的 LLM 表继续按既有策略忽略。用已有旧构造兼容测试证明末尾默认字段不会挤占 rate_policy 的位置。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py -k use_proxy -q
~~~

预期：当前 Web/Academic/OA 把 use_proxy 当未知选项，LLM 尚无对应字段或未拒绝非法类型；失败应定位新行为，不是配置缺少测试 Key。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
_resolve_use_proxy(table, label) -> bool:
    value = table.get("use_proxy", True)
    require type(value) is bool; otherwise ConfigFailure with label.use_proxy
    return value

append use_proxy=True to four frozen configuration classes
add use_proxy to Web and Academic/OA shared keys
parse once before disabled early returns, pass to all returned configurations
parse referenced LLM provider table only
keep use_proxy outside options and invocation extra_body
~~~

不要复用默认 False 的 `_optional_bool` 而漏改默认值，也不要更改现有 enable_search/enable_fetch 的默认。旧构造参数顺序保持，新值在解析构造处用 keyword 传入。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/unit/test_config_agent.py -q
~~~

预期：全部通过，旧启用/鉴权/速率规则不变。

- [ ] **Step 5: Refactor (keep tests green)**

只复用一个带字段路径的布尔解析 helper；不增加 Provider 基类或泛型 schema。运行同一组测试及 `uv run mypy src tests scripts`，确认旧位置构造与新增 bool 的类型一致。

### Task 3: 创建保留 CA 的直连 HTTP 客户端

**Files:**

- Modify: `src/agent_search_gateway/runtime.py`（HttpClientFactory、私有 SSL/helper）
- Create/Test: `tests/runtime/test_provider_proxy.py`
- Docs: HTTPX environment/SSL 文档及固定版本源码链接

- [ ] **Step 1: Write the failing test**

通过 Runtime.build 的现有入口，配置仅一个真实启用的 HTTP Provider 为 false，注入记录 kwargs 且返回真实 AsyncClient(MockTransport) 的工厂。断言 false 构造收到 `trust_env=False` 和真实 SSLContext；`verify_mode == ssl.CERT_REQUIRED`、`check_hostname is True`。true/省略路径仍调用零参数工厂。

按独立用例覆盖：

- SSL_CERT_FILE 指向临时有效 CA bundle；实际构建成功，所加载 CA 与文件一致。
- 仅 SSL_CERT_DIR 时采用 capath；同时设置时 FILE 优先，沿用 HTTPX 的选择次序。
- 非法/损坏 CA 文件导致受控 ConfigFailure，不创建 HTTP 客户端、不启动 daemon、不回退关闭验证。
- SSL context 构建失败时错误文本不包含代理 URL、凭据或证书内容。
- false + 仅支持零参数的工厂，受控失败且绝不再调用一次无参数工厂。
- 全部启用项都为 true、只有禁用项为 false 时，不额外预构建直连 context。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/runtime/test_provider_proxy.py -k "ssl or factory" -q
~~~

预期：旧代码始终 `factory()`，没有 direct 参数；新增行为断言失败。SSLContext 测试不得仅断言一个假对象。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
before constructing any HTTP client:
    collect enabled Web, enabled Academic, active OA, referenced LLM configurations
    if at least one use_proxy=False:
        prepare direct_ssl_context using httpx.create_ssl_context(
            verify=True, trust_env=True
        )
        map configuration/SSL construction failures to ConfigFailure
    else:
        direct_ssl_context = None

_create_http_client(factory, *, use_proxy, direct_ssl_context, label):
    if use_proxy:
        return factory()
    require a prepared verified context
    return factory(trust_env=False, verify=direct_ssl_context)
~~~

`httpx.create_ssl_context` 在已核对的 0.27.0 导出列表与锁定 0.28.1 中存在；使用顶层导出，不 import 私有 `httpx._config`。只改变工厂类型为允许 keyword options 的 Callable；明确 TypeError 为错误，不代理回退。

本任务先接通 Web 的客户端创建点以验证最小 direct 场景；其余家族在 Task 4 接通。

现有 Runtime.build 是同步方法，且没有通用的“失败后 await 关闭所有半成品”机制；不要虚构已有清理。把本次新增的 CA 失败前移到任何客户端创建前，避免该失败路径产生半成品；不要借此重写整个生命周期。正常创建的独立客户端仍由现有 Runtime.aclose 关闭。

SSL_CERT_DIR 的 CA 可在 TLS 握手时惰性加载。目录不存在或不是目录时可先作最小路径检查并受控报错；有效目录不能因 `cert_store_stats()==0` 被错误判坏。测试区分构造错误与握手验证错误，两者均不可降级或换 CA。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/runtime/test_provider_proxy.py tests/runtime/test_runtime_assembly.py -q
uv run mypy src tests scripts
~~~

预期：新的 direct/TLS 断言及原零参数工厂测试通过。测试用 monkeypatch 自动恢复环境，不改共享 os.environ。

- [ ] **Step 5: Refactor (keep tests green)**

helper 保持在 runtime.py；不加新传输模块或第三方依赖。只在有 direct 客户端时构建一次 context，可安全供独立客户端引用；不改变各连接池所有权。复跑本任务目标测试。

### Task 4: 将共享客户端工厂接入 Web、Academic、OA、LLM

**Files:**

- Modify: `src/agent_search_gateway/runtime.py`（四个 _build_* 创建点及 runtime_built）
- Test: `tests/runtime/test_provider_proxy.py`
- Test: `tests/runtime/test_runtime_assembly.py`
- Test: `tests/runtime/test_academic_runtime_assembly.py`
- Read: `src/agent_search_gateway/providers/http.py`

- [ ] **Step 1: Write the failing test**

配置混合 Runtime：Web zhihu=false、firecrawl=true，Academic arxiv=true，OA unpaywall=false，LLM primary=false/secondary=true。逐个绑定工厂返回的客户端与对应 executor/client，避免只比较没有归属的调用顺序。

再参数化四类的 false 路径，确保所有家族都覆盖。断言一个 Provider 的 Search/Fetch 使用同一 Adapter/客户端，各 Provider 客户端对象不同；禁用项无客户端。一个客户端受控 ConnectError 不替换其他实例、不改变其代理设置或重试。

正常 Runtime.aclose 连续调用两次，每个已创建客户端只关闭一次。DEBUG runtime_built 仅包含带命名空间的 Provider 名称及 use_proxy 布尔模式；合成代理凭据不进入日志。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/runtime/test_provider_proxy.py -k "families or isolation or close or logging" -q
~~~

预期：尚未接入的家族仍创建默认客户端，模式归属断言失败；保持老的 close-once 用例作为回归基线。

- [ ] **Step 3: Describe the minimal implementation**

把准备好的 direct_ssl_context 传入四个内部 builder，在原构造 HttpJsonExecutor 的位置调用同一个 helper，参数取对应 config.use_proxy。Web/Academic/OA 的 RateGate 注册、quota、retry 和 kwargs options 更新顺序保持。

LLM 直接读取 LLMProviderConfig.use_proxy，不从 invocation.extra_body 取值。日志可在已有 runtime_built 添加四个安全布尔摘要，名称按 Web/Academic/OA/LLM 区分，不读取/格式化实际代理地址。External Agent runner 完全不接这套 helper。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/runtime/test_provider_proxy.py tests/runtime/test_runtime_assembly.py tests/runtime/test_academic_runtime_assembly.py tests/runtime/test_new_web_runtime_assembly.py tests/runtime/test_agent_runtime_assembly.py -q
~~~

预期：四类覆盖、关闭、重试/RateGate 原测试全部通过。

- [ ] **Step 5: Refactor (keep tests green)**

去掉重复客户端构造分支；保留旧 HttpJsonExecutor API 和各 Adapter 签名中与代理无关的部分。复跑 `uv run pytest tests/runtime -q`，以 mypy 检查四个 builder 对 context/工厂类型的传递一致。

### Task 5: 用本地代理证明真实路由和 NO_PROXY 行为

**Files:**

- Test: `tests/runtime/test_provider_proxy.py`
- Read: `src/agent_search_gateway/runtime.py`

- [ ] **Step 1: Write the failing test**

在同一 pytest 事件循环启动两个 loopback server：目标返回 TARGET，假 HTTP proxy 返回 PROXY 并记录 request line。使用真实 HTTPX 默认 transport；不要用 MockTransport 证明代理是否生效，因为显式 transport 会绕开默认环境代理选择。

清理测试进程中 uppercase/lowercase 的 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY 及 REQUEST_METHOD，再用 monkeypatch 设置合成代理。参数化以下行为：

- use_proxy=true、目标不在 NO_PROXY：收到 PROXY；代理看到 absolute-form URL。
- use_proxy=false：收到 TARGET；代理没有收到本次请求。
- use_proxy=true 且 NO_PROXY 包含 loopback 目标：收到 TARGET。
- HTTP_PROXY 与 ALL_PROXY 的 HTTP 路由分别覆盖；HTTPS_PROXY 的构造选择由 Task 3/4 验证，必要时用受控 CONNECT server 单独测试，不访问真实 HTTPS 服务。

- [ ] **Step 2: Run test to verify the expected distinction**

~~~bash
uv run pytest tests/runtime/test_provider_proxy.py -k routing -q
~~~

旧实现 false 与 true 都会走代理，此为独立可观察的 RED。若 Task 3/4 已实施，这组用例会直接通过，记录为集成回归，不改坏代码制造红灯。

- [ ] **Step 3: Describe the minimal implementation**

复用 Runtime 的真实配置路径与 helper，不新增生产逻辑。给本地 server/task 设置短的测试保护 deadline，并在 finally 中关闭 listener、writer 和 client；所有目标仍为本机。以测试看到的响应来源证明隔离，同时检查环境在单个用例结束后恢复。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/runtime/test_provider_proxy.py -q
~~~

预期：路由用例确定、无需公网、无泄漏 pending task。若失败，仅修正本次 helper 的参数传递或测试环境隔离。

- [ ] **Step 5: Refactor (keep tests green)**

仅在该新测试文件内提取重复 loopback fixture，不给生产对象增加测试专用属性；复跑本文件，不扩展成通用代理测试框架。

### Task 6: 注册并验证 Firecrawl 的四项配置

**Files:**

- Modify: `src/agent_search_gateway/providers/defaults.py`（Firecrawl registration）
- Modify: `src/agent_search_gateway/config.py`（_validate_options）
- Modify: `src/agent_search_gateway/providers/web/firecrawl.py`（构造函数）
- Test: `tests/unit/test_config_web_providers.py`
- Test: `tests/providers/web/test_firecrawl.py`

- [ ] **Step 1: Write the failing test**

新增 `test_firecrawl_options_are_validated_before_runtime` 及分字段参数化用例，使用真实 registry 和 resolve_web_provider_config；对配置合法性断言 ConfigFailure.code 和完整字段路径。

| 字段 | 接受 | 拒绝 |
|---|---|---|
| search_limit | 1、10、100 | 0、101、bool、float、str、None |
| search_highlights | True、False | 数字、字符串、None、容器 |
| scrape_formats | ["markdown"]、["rawHtml"]、两者且无重复 | 空列表、整体字符串、未知项、重复、非字符串元素/嵌套列表或对象 |
| scrape_parsers | []、["pdf"]、["image"]、两者且无重复 | 整体字符串、未知项、重复、非字符串/对象 parser |

另从解析后的合法配置经真实 registration.factory 构造 Adapter，证明新增 kwargs 可被接受；构造默认值通过后续实际出站参数验证。

Firecrawl 禁用时仍遵循原有“先验证已注册 options，再跳过凭据”的时机。其他 Web Provider 传这些新键仍失败；use_proxy 仍独立于 options。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/unit/test_config_web_providers.py -k firecrawl -q
~~~

预期：合法字段被旧白名单拒绝；白名单扩展后非法值用例暴露缺失验证。

- [ ] **Step 3: Describe the minimal implementation**

只扩大 Firecrawl 的 allowed_config_keys。_validate_options 在已有未知键检查之后调用 Firecrawl 专属验证；search_limit 复用 _positive_int 后限制 <=100；选择数组先检查列表/元素类型再去重，避免 unhashable 值抛出未转换 TypeError。

构造函数新增：

~~~python
search_limit: int = 10
search_highlights: bool = True
scrape_formats: Sequence[str] = ("markdown",)
scrape_parsers: Sequence[str] = ()
~~~

配置解析负责 TOML 值的严格校验，Adapter 只保存默认/已验证值并把序列复制为 tuple。不要把这四项提升成所有 Web 配置的数据类字段，不透传任意 Firecrawl JSON。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/unit/test_config_web_providers.py tests/providers/test_registry.py tests/providers/web/test_firecrawl.py -q
~~~

预期：配置用例和现有 Adapter 行为测试通过；本任务还没有改变 Search/Fetch 负载。

- [ ] **Step 5: Refactor (keep tests green)**

如与 Task 9 重复，可复用一个小的 choice-list 校验 helper，但不抽象成 schema 框架；保留 Provider 专属标签与允许值。运行本组测试及 mypy。

### Task 7: Firecrawl Search 永远只请求和映射元数据

**Files:**

- Modify: `src/agent_search_gateway/providers/web/firecrawl.py`（search）
- Test: `tests/providers/web/test_firecrawl.py`
- Test: `tests/providers/web/test_search_result_tolerance.py`
- Read: `tests/fixtures/providers/firecrawl/search.json`
- Read: `src/agent_search_gateway/orchestrators/search.py`（_stage_keyword_hit）

- [ ] **Step 1: Write the failing test**

更新现有 `test_firecrawl_adapter_maps_v2_search_and_scrape_payloads` 的 Search 断言：原 38–46 行正文改为空；原 54–58 行的 scrapeOptions 改为元数据参数。此任务暂时保留旧 Fetch 负载断言，交 Task 8 修改。

新增独立用例 `test_firecrawl_search_never_requests_or_maps_body`，覆盖：

- 默认与自定义 limit/highlights；每次只有一个 /v2/search 出站请求。
- 请求键不存在 scrapeOptions，既不传 null 也不传空对象。
- 只有 URL/title/description 的响应正常返回。
- 返回意外 markdown/rawHtml 字符串、对象或数组时，一样返回有效元数据且正文为空。
- 原 URL/摘要映射、无效结果计数、空列表和 success=false 语义保持。

继续使用含正文的原 fixture 作为“意外正文被忽略”证据；元数据最小响应可直接构造字典，不必额外创建 fixture 文件。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_firecrawl.py -k search -q
~~~

预期：旧实现存在 scrapeOptions/非空正文，或因非字符串正文误拒绝有效结果。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
search_body = {
    query,
    sources: ["web"],
    limit: configured search_limit,
    highlights: configured search_highlights
}
one request_json("POST", "/v2/search", search_body)
for each existing valid web result:
    read URL, title, description only
    create KeywordSearchHit with raw_content="" and content=""
preserve counts and existing error diagnostics
~~~

删除读取/校验 Search 正文字段的代码，而不只是最后把正文覆盖为空。_stage_keyword_hit 在无正文时已经返回可采纳的 _StagedKeyword，发生 body_skipped/no_body，先于 cheap_check 和 judge；无需改通用 Pipeline。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_firecrawl.py tests/providers/web/test_search_result_tolerance.py tests/orchestrators/test_keyword_search_pipeline.py tests/orchestrators/test_keyword_search_state.py -q
~~~

预期：Search 元数据正常采纳，其他 Provider 的正文路径保持；剩余 Fetch 旧行为仍由下一任务处理。

- [ ] **Step 5: Refactor (keep tests green)**

Search 与 Fetch 的正文解析各自清晰；只让 Fetch 调用 _page_body，不把 Firecrawl 特例放进通用 Orchestrator。复跑上述组。

### Task 8: Firecrawl Fetch 显式 formats/parsers，保留空正文与错误路径

**Files:**

- Modify: `src/agent_search_gateway/providers/web/firecrawl.py`（fetch 负载）
- Test: `tests/providers/web/test_firecrawl.py`
- Read: `tests/fixtures/providers/firecrawl/scrape.json`
- Read: `src/agent_search_gateway/providers/http.py`、`src/agent_search_gateway/scheduler/fetch.py`
- Regression: `tests/providers/test_http_executor.py`、`tests/unit/test_retry.py`

- [ ] **Step 1: Write the failing test**

更新现有组合测试的 Fetch 负载断言（原 59–62 行），默认必须显式发送：

~~~json
{"url":"https://example.com/article","formats":["markdown"],"parsers":[]}
~~~

新增配置参数用例，验证 markdown/rawHtml 的合法组合、pdf/image parser 列表只影响 Fetch。保留 rawHtml-only、markdown-only 与两者存在时的既有正文映射。

给 `test_fetch_reports_safe_response_diagnostics` 现有空对象案例追加 rawBase64-only：应继续报 `page body is empty`，semantic_result=empty、response_kind=object。不把 base64 当文本，不在异常消息/新增诊断中附加其内容；既有受控 DEBUG preview 不扩容。

在真实 HttpJsonExecutor + MockTransport 上补足或复用以下行为，不改变策略：

| 模拟响应 | 当前应保留的行为 |
|---|---|
| 402 | `HTTP attempts == 1`；精确断言 `HttpStatusFailure`、`code=ErrorCode.ALL_PROVIDERS_FAILED`、`status_code=402` |
| 429 后 200 | 遵从现有 Retry-After，再成功；Adapter 不加第二层重试 |
| 连续 5xx | 到配置 max_attempts 后现有 failure |
| 200 + invalid JSON | `ProtocolFailure(PROTOCOL_ERROR)` 且 `HTTP attempts == 1`；JSON 解码在共享重试之外，不新增请求 |
| 200 + success=false | 现有 Provider 业务失败；不隐式新增请求 |
| 空 Web 列表/不合法条目 | 现有空结果、拒绝计数与容错 |

这里断言的是 Adapter/Executor 边界的具体异常；Scheduler 边界另行断言 `execution_failure` outcome 和继续下一个候选，不把两个层级的异常/结果混为可任选的契约。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/web/test_firecrawl.py -q
~~~

预期：负载测试因旧 formats 和缺少 parsers 失败。base64-only 和已有错误分类可能直接通过，是回归保护，不要求改坏正确逻辑。

- [ ] **Step 3: Describe the minimal implementation**

~~~text
fetch_body = {
    url,
    formats: list(self._scrape_formats),
    parsers: list(self._scrape_parsers)
}
one existing /v2/scrape executor call
reuse _parse_fetch -> _page_body -> URLFetchCandidate
if no markdown/rawHtml:
    preserve existing empty-body failure
~~~

_page_body 已经忽略 rawBase64；不新增 PDF/base64 解码。HTTP 错误分类继续由共享 executor 决定。Scheduler 的 execution failure 会继续尝试下一可用候选；不能把空正文改成成功或直接把 PDF 交给 LLM。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/web/test_firecrawl.py tests/providers/test_http_executor.py tests/unit/test_retry.py tests/scheduler/test_fetch_outcomes.py -q
~~~

预期：新请求参数及既有错误/日志测试通过。只断言无隐式 scrape、parser 配置明确；不从 mock 推导 credits 数字。

- [ ] **Step 5: Refactor (keep tests green)**

重复的受控响应/请求记录使用文件内 helper，真实 HTTP 请求只替换 transport，保留 executor/retry/diagnostics。确认没有生产改动落到 providers/http.py 或 scheduler/fetch.py。

### Task 9: 注册 arXiv 查询选项并严格校验

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/defaults.py`
- Modify: `src/agent_search_gateway/config.py`（_validate_academic_options）
- Modify: `src/agent_search_gateway/providers/academic/arxiv.py`（构造函数）
- Test: `tests/unit/test_config_academic_providers.py`
- Test: `tests/providers/academic/test_registry_defaults.py`
- Test: `tests/providers/academic/test_arxiv.py`

- [ ] **Step 1: Write the failing test**

新增 `test_config_accepts_arxiv_search_options`、`test_config_rejects_invalid_arxiv_search_options`、`test_arxiv_options_are_not_shared_with_other_providers`。矩阵：

- query_fields 接受 ["ti"]、["abs"]、["abs","ti"]；拒绝空/重复、au/cat、整体字符串、非字符串元素和嵌套容器。
- max_results 接受 1/10/100；拒绝 0/101、bool、float、str、None。
- sort_by 仅 relevance/submittedDate/lastUpdatedDate；sort_order 仅 ascending/descending；拒绝大小写变体与非字符串。
- 已禁用 arXiv 维持已有 options 验证时机。
- 非 arXiv Academic 和 OA 仍拒绝这些键。
- 省略选项时构造默认依次为 ti+abs、10、relevance、descending。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/unit/test_config_academic_providers.py tests/providers/academic/test_registry_defaults.py -k arxiv -q
~~~

预期：旧白名单拒绝合法新键；添加白名单后值校验用例仍能暴露错误。

- [ ] **Step 3: Describe the minimal implementation**

arXiv registration 使用原 `_ALLOWED_OPTIONS` 与四个新键的并集，其他 registration 不变。_validate_academic_options 在未知/保留键检查后，仅对 name=arxiv 校验对应字段。重复字段报错，保留合法输入顺序。

ArxivProvider 构造函数新增 Sequence[str]/int/str/str 参数及表中默认值；query_fields 保存 tuple。使用已有 _positive_int 再限制 <=100；复用 Task 6 的小型数组验证逻辑即可。不引入 author/category 或 CLI 专属字段。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/unit/test_config_academic_providers.py tests/providers/academic/test_registry_defaults.py tests/providers/academic/test_arxiv.py tests/runtime/test_academic_runtime_assembly.py -q
~~~

预期：配置及构造成功，现有旧查询行为尚未因本任务改变。

- [ ] **Step 5: Refactor (keep tests green)**

确认四个 option 名、构造参数名、私有保存字段在后续任务一致。配置值不泄漏至公共 PaperSearchRequest；运行 mypy 和本组测试。

### Task 10: 编译普通词、短语及经验证的安全字面量

**Depends on:** 10A（普通词、完整短语、字段组合）仅依赖 Task 9；10B（特殊字符 scanner/serializer）依赖 Task 1 的验证结论和 10A。

本任务按 10A、10B 分别执行下面的小循环。Task 1 尚未给出特殊字符结论时，仍可完成 10A 并推进 Task 11 的结果数、排序和普通查询接通；Task 10 整体及完整 arXiv 验收必须等 10B 通过后才能标记完成。不得将暂未验证的特殊字符输入作为已支持能力交付。

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/arxiv.py`
- Test: `tests/providers/academic/test_arxiv.py`
- Docs: arXiv 官方用户手册 §5.1；Task 1 验证结论

- [ ] **Step 1: Write the failing test**

通过已有 ArxivProvider.search 和受控 HTTP 回应，先对出站 search_query 写行为断言，避免 helper 尚不存在造成 import error。将下列场景拆成独立参数化用例：

| query / 配置 | 解码后的 search_query |
|---|---|
| graph | (ti:graph OR abs:graph) |
| graph transformers | (ti:graph OR abs:graph) AND (ti:transformers OR abs:transformers) |
| "graph neural networks" | (ti:"graph neural networks" OR abs:"graph neural networks") |
| "graph neural networks" molecular | (ti:"graph neural networks" OR abs:"graph neural networks") AND (ti:molecular OR abs:molecular) |
| graph transformers / fields=["ti"] | (ti:graph) AND (ti:transformers) |

**10A：** 上表普通词/完整短语用例，加上 phrase 内多空格、字段自定义顺序和 Unicode 的确定性测试，可先独立实现和验证。

**10B：** 按 Task 1 的结论加入特殊字符矩阵：AND/OR/ANDNOT/NOT、冒号、括号、百分号、连字符、反斜线、连续/未闭合/嵌入引号。除了字符串快照，还验证用户不能增加语法节点；未通过验证门时不将这些用例静默跳过并宣称本任务完成。

空白/空短语导致无有效词项时，应在 HTTP 调用之前走现有 provider ProtocolFailure，不能发 `all:`、空 search_query 或通配搜索。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/academic/test_arxiv.py -k "query_compiler or literal or quote or unsearchable" -q
~~~

预期：现有 `all:{query}` 负载与字段组合不符，或特殊输入被透传为语法。不接受拼写/import 错误作为 RED。

- [ ] **Step 3: Describe the minimal implementation**

在 arxiv.py 内新增私有纯函数，固定接口：

~~~python
def _compile_query(query: str, fields: tuple[str, ...]) -> str:
    ...
~~~

~~~text
tokens = scan ordinary terms and complete double-quoted phrases
    outside phrases: whitespace delimits terms
    inside phrases: preserve original internal whitespace
    apply the verified unmatched/embedded-quote policy from Task 1
    do not use shell parsing or globally collapse whitespace

for token in tokens:
    literal = serialize one term according to verified policy
    alternatives = [field + ":" + literal for field in fields]
    groups.append("(" + " OR ".join(alternatives) + ")")

if no effective group:
    raise protocol_failure("arxiv", safe no-searchable-terms message)
return " AND ".join(groups)
~~~

只有 compiler 生成字段、括号和连接符。不要使用默认 shlex.split；不要复制 _clean 的全串空白折叠。将 search 的 search_query 接到 _compile_query；传 HTTPX params 原串，不手工 quote_plus、不二次 percent encode。

- [ ] **Step 4: Run test to verify it passes**

更新旧查询结构断言：

- `test_arxiv_honors_shared_retry_after` 的旧 `all:graph transformers`。
- `test_arxiv_maps_atom_feed_and_isolates_malformed_entry` 中相同旧值。

~~~bash
uv run pytest tests/providers/academic/test_arxiv.py -q
~~~

预期：查询、短语、字面化、错误与所有 Atom 断言通过。若 Task 1 的特殊字符门尚未通过，本任务不能被标为完成；普通词子用例通过不能替代全部验收。

- [ ] **Step 5: Refactor (keep tests green)**

scanner/serializer/compiler 只在同一 Provider 文件内分成少量私有函数，必要时 token 用明确的不可变 tuple/dataclass；不引入查询 DSL、全局 parser 或 LLM 重写。复跑本文件，确认字段/词序输出 deterministic。

### Task 11: arXiv 参数接通并保留一次逻辑检索及全部响应契约

**Depends on:** Task 9、Task 10A。结果数、排序、普通查询接通与现有响应契约可独立推进；涉及特殊字符编码的最终验收另依赖 Task 10B，不能用普通查询通过替代。

**Files:**

- Modify: `src/agent_search_gateway/providers/academic/arxiv.py`（search params）
- Test: `tests/providers/academic/test_arxiv.py`
- Test: `tests/runtime/test_academic_runtime_assembly.py`
- Regression: `tests/providers/test_http_executor.py`、`tests/runtime/test_rate_gate.py`

- [ ] **Step 1: Write the failing test**

新增 `test_arxiv_uses_configured_search_options`：query_fields=["abs"]、max_results=25、sort_by=submittedDate、sort_order=ascending，检查完整 GET params 与默认值差异。Runtime 测试从真实 TOML 字典解析到 Adapter，再发一次模拟请求，避免只测直接构造。

继续检查 `request.url.params["search_query"]` 与原始编译串一致；特殊 Unicode/百分号没有双重编码。成功且无需重试时只一条 GET；结果不足不发第二轮宽松查询。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/providers/academic/test_arxiv.py tests/runtime/test_academic_runtime_assembly.py -k "configured_search_options or arxiv_options" -q
~~~

预期：旧硬编码 max_results/sortBy/sortOrder 与配置值不符。

- [ ] **Step 3: Describe the minimal implementation**

保持一次 `self._executor.request_text("GET", ...)`，把四项参数从 self._query_fields/_max_results/_sort_by/_sort_order 接入。stage="paper_search"、response_diagnostics、_parse_response 和 _map_entry 保持。

“一次逻辑检索”不等于“禁止共享 executor 重试”。`test_arxiv_honors_shared_retry_after` 的两次 HTTP 尝试断言必须保留；`test_arxiv_http_failure_propagates_without_adapter_retry` 仍证明没有 Adapter 层第二重 retry。保留 Atom XML/非法 envelope、作者、DOI/arXiv ID、URL/PDF URL、主题、日期、候选计数等现有断言。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/providers/academic/test_arxiv.py tests/providers/academic/test_registry_defaults.py tests/runtime/test_academic_runtime_assembly.py tests/providers/test_http_executor.py tests/runtime/test_rate_gate.py tests/academic -q
~~~

预期：新参数生效；429/Retry-After 与 RateGate、原 PaperSearchHit 映射全部保持。

- [ ] **Step 5: Refactor (keep tests green)**

不对结果做额外客户端截断/排序，不加分页或检索放宽；结果数/排序由同一 arXiv API 请求参数控制。检查生产 diff 仅在查询和构造路径，复跑目标组。

### Task 12: CLI 默认无限等待，同时保留显式超时和清理

**Files:**

- Modify: `src/agent_search_gateway/protocol.py`（29–31、send_request）
- Test: `tests/unit/test_socket_client.py`
- Regression: `tests/unit/test_protocol_codec.py`、`tests/cli/test_cli.py`、`tests/daemon/`
- Read: `src/agent_search_gateway/cli.py`、`src/agent_search_gateway/daemon.py`

- [ ] **Step 1: Write the failing test**

将现有 `test_socket_timeout_depends_on_request_type_and_honors_override` 改名为 `test_socket_client_defaults_to_unbounded_wait_and_honors_override`。保留六种 request（五种业务加 shutdown）与 None/0.01/42.0 的覆盖；额外区分省略参数与显式 None。

原断言：

~~~python
assert seen == [override if override is not None else (None if kind == "agent" else 600.0)]
~~~

改为：

~~~python
assert seen == [override]
~~~

保持其受控 wait_for recorder，以保存的真实 wait_for 给测试套短保护时间，不能真的等待 600 秒。独立回归用例验证显式 0.01 超时仍抛 ProtocolFailure、客户端 writer 关闭且服务端读到 EOF；把已有 Agent 专属 EOF/取消用例参数化到五种业务。合法 ErrorResponse、成功响应、缺换行、非法 JSON/UTF-8、超大帧保留既有协议测试。

- [ ] **Step 2: Run test to verify it fails**

~~~bash
uv run pytest tests/unit/test_socket_client.py::test_socket_client_defaults_to_unbounded_wait_and_honors_override -q
~~~

预期：旧代码对非 Agent 的 None 记录 600.0；显式数值和 Agent 原样通过。

- [ ] **Step 3: Describe the minimal implementation**

删除 _RESPONSE_TIMEOUT_SECONDS 及解释旧策略的注释，删除 send_request 中 None+非 Agent 的赋值分支。保留：

~~~python
line = await asyncio.wait_for(reader.readline(), timeout=response_timeout_seconds)
~~~

wait_for(..., None) 已有无限等待语义，无需再写一套分支。None 的统一语义也适用于 ShutdownRequest，不新增 shutdown 特判。

保留建连、write/drain、frame 解析、TimeoutError/ValueError 转换和 finally 的 close/wait_closed。显式 timeout 只约束 readline，不扩展为整次工作流 deadline。不要删除其他编码/解析处仍使用的 AgentSearchRequest import。

cli.py 的 SocketClient 仍为双位置参数 Callable；run_command 不新增 timeout keyword，以免破坏注入替身。daemon.py 不增加断开 watcher，后台可继续完成并写结果；daemon 自己的 shutdown grace 和 Agent deadline 不变。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/unit/test_socket_client.py tests/unit/test_protocol_codec.py tests/cli/test_cli.py tests/daemon -q
~~~

预期：默认超时、显式数值、EOF、取消、资源清理与 request_id/停机回归全部通过。取消/EOF 等既有正确行为的新覆盖可以直接通过。

- [ ] **Step 5: Refactor (keep tests green)**

测试中的 asyncio.wait_for monkeypatch 仅用于参数观察；事件等待和清理使用保存的真实函数，避免 Awaitable[bytes] recorder 被误用于 Event.wait() 的 bool。清理服务端 task/listener，复跑目标组，不引入 fake 全局时钟或十分钟测试。

### Task 13: 串起真实 Adapter、Runtime、CLI 与 fallback 的离线验收

**Files:**

- Create/Test: `tests/acceptance/test_search_cost_arxiv_proxy_cli.py`
- Read: `tests/acceptance/test_gateway_workflows.py`（现有受控 Runtime/daemon 测试模式）
- Read: `tests/support/fakes.py`、`tests/support/controlled.py`
- Regression: `tests/orchestrators/test_keyword_search_state.py`、`tests/orchestrators/test_url_fetch_flow.py`、`tests/scheduler/test_fetch_outcomes.py`

- [ ] **Step 1: Write the failing tests as separate scenarios**

新增三个聚焦场景，每个场景用真实配置解析、Runtime、Adapter、Store/ResultWriter，只有上游 HTTP 响应用 MockTransport。不要以 FakeKeywordSearchProvider 替代 FirecrawlAdapter 来“证明”出站成本行为。

**A. test_firecrawl_metadata_search_then_explicit_fetch**

1. 配置 Firecrawl 搜索/抓取，返回只有 URL/title/description 的 Search 响应；保留一个含意外正文的变体。
2. keyword-search 后 JSONL 有原 schema 的 URL/abstract，Store 中无正文，只有 /v2/search 请求，没有 /v2/scrape、judge/clean 调用。对真实 `cheap_check` 加保留原函数行为的 spy，两个无正文变体都精确断言调用次数为零；LLM 请求记录不能替代这项本地阶段断言。
3. 显式 url-fetch 后才出现 /v2/scrape，负载含默认 formats/parsers；正常正文通过既有安全/清洗路径并缓存。
4. 再次 fetch 复用缓存，不增加上游 scrape。
5. 用现有正文 Provider 的状态回归测试证明它仍执行 cheap_check/judge；复用已有精确断言，缺少时只补必要计数，不全局关闭正文判定。

**B. test_firecrawl_base64_only_fetch_uses_existing_fallback**

1. 配置顺序 Firecrawl 在前，另一个合法 Fetch Provider（例如 Tavily）在后，二者容量/速率立即可用；本场景使用独立 Store，不依赖 A 的状态。
2. 先执行仅选择 Firecrawl 的真实元数据 keyword-search，将目标规范化 URL 准入并写入 Store；确认该 URL 无缓存正文且未被标为 unavailable，再发起显式 url-fetch。
3. Firecrawl Scrape 返回 rawBase64-only；下一 Fetch Provider 返回可用正文。
4. 精确断言调用顺序为 Search → Firecrawl Scrape → fallback Fetch，三者各一次；按现有 scheduler 选择顺序回退并得到正确结果。
5. base64 不进入任何 LLM 请求/成功正文；失败候选不重新触发 Search，也不重复同一 Scrape。

**C. test_arxiv_cli_uses_compiled_query_and_writes_paper_rows**

1. 临时路径运行真实 ForegroundDaemon/Runtime，CLI paper-search 仅选 arxiv，mock Atom + 可选 OA 回应。
2. 检查单次逻辑检索参数、原 paper JSONL schema/候选数、其他 Provider 未调用。
3. 对多关键字和引号短语使用 Task 10 的确定期望；字段/排序自定义使用 Task 11 配置。
4. 这不是检索相关性评分；只证明官方语法与已有输出契约正确接通。

- [ ] **Step 2: Run test to verify meaningful behavior**

~~~bash
uv run pytest tests/acceptance/test_search_cost_arxiv_proxy_cli.py -q
~~~

若这些场景在 Task 7/8/10/11 前运行，预期暴露隐式正文、错误参数或不支持配置；若前序已完成，应直接通过作为组合验收。不要为了让它们先红而回退共享文件或修改其他 Agent 的代码。

- [ ] **Step 3: Describe the minimal implementation**

只组织测试 fixture，不改生产 Pipeline。mock HTTP 仍经过真实 HttpJsonExecutor；LLM 端使用完整的受控 OpenAI response envelope。用实际 JSONL/Store/请求记录断言最终行为。

再以现有 daemon 测试模式加入 `test_client_disconnect_does_not_cancel_running_workflow`：工作流进入受控等待后关闭客户端端点，释放工作流，让 ResultWriter 完成；验证工作流未被取消且结果文件写出。关闭测试 daemon 时复用既有 stop/cleanup，不操作用户 daemon。

长执行的超时回归由 Task 12 的参数观察证明旧 600 秒限制已消除；本测试只控制“响应稍后完成”及结果返回，不真实等待十分钟、不把测试保护 deadline 当作产品超时。

- [ ] **Step 4: Run test to verify it passes**

~~~bash
uv run pytest tests/acceptance/test_search_cost_arxiv_proxy_cli.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_url_fetch_flow.py tests/scheduler/test_fetch_outcomes.py tests/cli tests/daemon -q
~~~

预期：真实配置/Adapter 到输出闭环全部通过，后台断线行为与 spec 一致；临时 socket、客户端、HTTP executor、server task 均关闭。

- [ ] **Step 5: Refactor (keep tests green)**

仅在新测试文件提取小型配置/响应 helper。保留有意义的跨模块行为断言，不添加只检查私有字段存在、重复 mock 调用或 README 文本的测试。复跑该文件后进入文档任务。

### Task 14: 更新配置示例、用户文档和 Unreleased

**Files:**

- Modify: `config.example.toml`
- Modify: `README.md`
- Modify: `docs/public-interface.md`
- Modify: `CHANGELOG.md`
- Modify/Test: `tests/docs/test_documented_config.py`
- Regression: `tests/docs/test_installation_contract.py`、`tests/docs/test_open_source_contract.py`

- [ ] **Step 1: 固定可执行的示例契约**

复用 tests/docs/test_documented_config.py 的 load_toml、_stub_environment 和真实 resolve_config/Runtime 方式，扩展已有配置测试以验证四类 use_proxy、Firecrawl/arXiv 选项能进入实际运行时；不要新增全文逐句匹配的文档测试。

将示例知乎配置设为 use_proxy=false 后，基于示例构造 Runtime 的测试工厂必须支持 trust_env/verify。当前该文件约 467、707 行有零参数工厂，逐处核对是否仍使用含知乎的完整配置；需要时加具有明确类型的 keyword 参数并传给 AsyncClient(MockTransport)。默认 true 的其他旧零参数测试不需要批量重写。

- [ ] **Step 2: Run existing checks and observe configuration gaps**

~~~bash
uv run pytest tests/docs/test_documented_config.py -q
~~~

纯文档修改无需人为制造 RED；新增示例运行时行为断言若先写，应因缺少示例字段或无法接受 direct 工厂参数而失败，不能放宽全局测试断言掩盖问题。

- [ ] **Step 3: 更新具体内容**

config.example.toml：

- Firecrawl 表（原 14 行起）：use_proxy=true、search_limit=10、search_highlights=true、scrape_formats=["markdown"]、scrape_parsers=[]。
- 知乎表：use_proxy=false；保持原两分支、限流/并发和重试配置。
- arXiv 表（原 193 行起）：use_proxy=true 及四项查询字段；保留 max_concurrency=1、min_interval_seconds=3。
- LLM 与 OA 表各给 use_proxy 示例；其他 HTTP Provider 用公共说明表明同样支持，无需逐个复制。
- 删除原 300 行附近“Other socket waits remain 600 seconds”的陈旧注释；不添加新的 CLI timeout 配置键。

README.md：

- 配置章节解释 Provider 代理两态、NO_PROXY、证书保留、修改配置需要正常重启后生效；不会自动重写已有用户配置。
- 加 Firecrawl 元数据 Search/显式 Fetch 的选项说明。当前供应商文档说明 `parsers=[]` 遇到 PDF 可返回 base64，并按整份 PDF 收固定费用（当前为 1 credit）；开启 PDF parser 则可能按页收费。注明这是当前供应商规则，不是 Gateway 对最终账单的保证。仅 base64 不是可用正文，可能 fallback；不承诺零费用。
- academic 章节给字段 AND/OR 例子和四项 arXiv 白名单；说明长自然语言 AND 查询可能降低召回，不增加 LLM 改写或第二轮查询，不声称官方保证相关性改善。
- 更新原 396 行“Other socket response wait | 600 seconds”及 399–405 行附近解释；所有业务默认等响应，显式内部超时仍有效，中断 CLI 不保证后台任务停止。Agent 自身期限和 Provider HTTP timeout 不变。

docs/public-interface.md：

- 更新原 75–76 行非 Agent 600 秒说明；说明 use_proxy 是 HTTP Provider 配置且不作用于外部 Agent subprocess。
- 公共命令/输出模型不变，不把内部工厂或私有 compiler 提升为公开 API。

CHANGELOG.md：

- 在 Unreleased 简短列出 Firecrawl 默认成本行为、arXiv 查询/选项、Provider 代理、CLI 默认等待四项；不升版本、不发布、不称已修复上游池泄漏。

- [ ] **Step 4: Run existing checks**

~~~bash
uv run pytest tests/docs -q
uv run python scripts/build_docs.py
~~~

预期：示例可解析/可组装，现有安装和公开接口契约保持，文档构建成功。检查构建结果只含预期文档产物，不盲目提交其他生成变化。

- [ ] **Step 5: 人工一致性检查**

限定 README、public-interface 和 config.example 搜索“600”“Other socket”“Non-Agent”，核实语义而非全局替换数字；其他速率/缓存配置中的 600 保留。三份历史 spec 与旧历史设计文件不批量改写。核对默认值、费用表述、use_proxy 覆盖范围、重启/中断语义，无需为这些文字再造镜像测试。

### Task 15: 最终回归、可选定向 smoke 和实施交付

**Files:**

- Verify: 本计划列出的七个生产文件、测试、四份用户文档
- Read: `CONTRIBUTING.md`
- Update: 本计划任务状态和验证记录（只填写实际发生的结果）

- [ ] **Step 1: 确认范围和门状态**

用差异审阅确认生产文件范围；Task 1/10 字面化门必须有明确结果。未解决时报告受影响边界，不能勾选 arXiv 全部完成。确认未修改真实配置、shared daemon、其他工作树、retry 参数、连接池或取消协议。

- [ ] **Step 2: 执行定向回归**

~~~bash
uv run pytest tests/unit/test_config_web_providers.py tests/unit/test_config_academic_providers.py tests/unit/test_config_llm.py tests/providers/web/test_firecrawl.py tests/providers/academic/test_arxiv.py tests/unit/test_socket_client.py -q
uv run pytest tests/runtime tests/cli tests/daemon tests/providers tests/acceptance/test_search_cost_arxiv_proxy_cli.py -q
~~~

预期：目标测试全部通过。失败时只补充能定位剩余风险的测试，不重复无关长测试。

- [ ] **Step 3: 执行仓库规定的完整检查**

~~~bash
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
env -u WEB_SEARCH_RUN_INTEGRATION uv run pytest -v
uv run python scripts/build_docs.py
~~~

预期：format/lint/typecheck/docs 均 exit 0；pytest 全部通过、已有 opt-in live 测试按原规则跳过。类型检查范围采用 CONTRIBUTING 的 src/tests/scripts，不缩成仅 src。当前没有 packaging/依赖改动，无需额外 uv build 或改 lockfile。

- [ ] **Step 4: 需要时做定向 smoke**

普通验收足以离线完成。现有用户授权允许针对性 smoke，但此计划不要求为完成测试额外消耗 Firecrawl 额度。

如实施者选择 arXiv/知乎验证，先确认使用实施后的代码与隔离测试配置，核对超时/attempts，遵守原 rate gate；仅调用选定 Provider。命令形态：

~~~bash
agent-search-gateway paper-search "graph transformers" --provider arxiv
agent-search-gateway keyword-search "测试主题" --provider zhihu
~~~

这些命令连接当前配置的 daemon，**不能不核对版本/实例就运行**。准备独立 daemon 需要按现有测试/启动接口使用隔离路径；不得为 smoke 擅自停止共享实例。知乎 false 的成功或失败只表明该次直连结果，不作为连接池根因的证明。

默认不运行 Firecrawl 真正 Scrape，也不触发全 Provider 测试；若后续用户明确要求费用验证，再先检查出站负载与调用范围。所有报告只包含脱敏 request_id、阶段、状态和计数。

- [ ] **Step 5: 提交实施结果**

记录命令、真实通过/失败/跳过数及环境问题，区分“定义了测试”和“运行通过”。只提交本任务变更，按项目约定写清默认行为变化。此计划的创建提交只包含本文件；实施、PR、合并、发布属于后续工作。

## Spec 覆盖核对

| 需求 | 任务 | 关键验收 |
|---|---|---|
| Web/Academic/OA/LLM 的 use_proxy、严格 bool、默认兼容 | 2、4 | 四类解析、shared/options 隔离、disabled/referenced 规则、旧构造兼容 |
| 直连与 CA 环境保留、错误不降级 | 3、5 | 真实 SSLContext、FILE/DIR 优先级、构造失败前移、本地真实路由 |
| 每 Provider 独立客户端、正常关闭、安全模式日志 | 4 | 同 Provider Search/Fetch 共用，其他隔离，close once，模式不含代理 URL |
| Firecrawl 限定选项和默认值 | 6 | 严格整数/布尔/列表白名单，其他 Provider 不放开 |
| Search 无 scrapeOptions、无正文且仍采纳 URL | 7、13 | 有无/异常正文元数据响应，JSONL/Store，Search 无下游正文阶段 |
| Fetch 显式格式/parser、base64 空正文、fallback | 8、13 | 只消费文本，既有失败与候选调度，缓存 |
| Firecrawl 错误/重试/费用边界 | 8、14 | 402/429/5xx/invalid JSON/success=false，文档无零费用承诺 |
| arXiv 字段/词组 AND/OR、短语、特殊字符 | 1、10 | 官方依据与全链路验证；普通词明确快照；特殊字符门不得略过 |
| arXiv 配置、单次逻辑检索、Atom/RateGate 不变 | 9、11、13 | 精确参数、429→200 的两次 HTTP 尝试与 Retry-After 保留、无第二轮改写 |
| 五种业务 CLI 默认无限等、显式超时、协议清理 | 12、13 | None/数值矩阵、EOF/取消/ErrorResponse/帧限制、客户端关闭 |
| Daemon 断线不新增取消、停机机制不变 | 12、13 | 受控断线后工作流完成，既有 daemon/request_id 回归 |
| 示例、README、public-interface、变更记录 | 14 | 可执行配置契约、清除陈旧 600 秒描述、正确费用/代理/中断语义 |
| 不修改 retry/连接池/其他 Adapter/公开 schema | 15 | 范围审阅与完整检查 |

### 类型一致性核对

- 四个配置数据类字段一律 `use_proxy: bool`；只有 Runtime 读取，Adapter options 中不存在此键。
- 工厂外部注入接口与内部 helper 区分；旧 true 路径零参数调用，direct 路径仅支持明确 keyword 参数；没有 TypeError fallback。
- Firecrawl TOML 使用 list[str]，Adapter 内部为 tuple[str, ...]，HTTP JSON 恢复数组；search_highlights 默认 True。
- arXiv 的 `query_fields` 是有序、非空、无重复的 ti/abs tuple；`_compile_query(query, fields)` 返回 str；params 名称严格为 search_query/max_results/sortBy/sortOrder。
- `send_request` 的 None 语义统一；SocketClient 的双位置参数注入接口、Request/Response/ErrorCode 保持。
- 真实成功路径只有一轮逻辑 Search；共享 HTTP 尝试次数始终由既有 RetryPolicy 决定。

## 参考资料

以下官方资料于 2026-11-22 核对，用于界定行为，不代替历史账单或线上部署证明。

1. [Firecrawl Search](https://docs.firecrawl.dev/api-reference/endpoint/search)：可选 scrapeOptions、limit/highlights 与 Web 元数据。
2. [Firecrawl Scrape](https://docs.firecrawl.dev/api-reference/endpoint/scrape)：formats/parsers、PDF 文本解析和空 parser 的费用/响应差异。
3. [arXiv API User's Manual](https://info.arxiv.org/help/api/user-manual.html)：§3.1.1.3 排序、§5.1 字段/布尔/短语及 URL 编码。
4. [arXiv classic 查询前置处理（固定 commit）](https://github.com/arXiv/arxiv-search/blob/f53ed1707d80a877e72ec989b2156c2e7bb01d87/search/domain/classic_api/classic_search_query.py)。
5. [arXiv classic parser（固定 commit）](https://github.com/arXiv/arxiv-search/blob/f53ed1707d80a877e72ec989b2156c2e7bb01d87/search/domain/classic_api/query_parser.py)。
6. [arXiv parser tests（固定 commit）](https://github.com/arXiv/arxiv-search/blob/f53ed1707d80a877e72ec989b2156c2e7bb01d87/search/domain/classic_api/tests/test_classic_parser.py)。
7. [HTTPX environment variables](https://www.python-httpx.org/environment_variables/) 与 [SSL](https://www.python-httpx.org/advanced/ssl/)。
8. [HTTPX 0.27.0 顶层导出](https://github.com/encode/httpx/blob/0.27.0/httpx/__init__.py) 与 [0.28.1 SSL context 实现](https://github.com/encode/httpx/blob/0.28.1/httpx/_config.py)。
9. 本工作树三份 spec、`CONTRIBUTING.md`、本文“最近调用证据”中的本地日志定位。

