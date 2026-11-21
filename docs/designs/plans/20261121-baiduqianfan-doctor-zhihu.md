# Baidu Qianfan 双分支搜索、open-webSearch Doctor 与知乎配置 Implementation Plan

**Goal:** 在既有网关中加入使用 `BAIDUQIANFAN_API_KEY` 的单一双分支搜索提供商，识别已启用但未启动的 open-webSearch，并补齐知乎限流说明。

**Architecture:** `docs/designs/architectures/20261121-baiduqianfan-doctor-zhihu.md`

**Error handling:** `docs/designs/error-handlings/20261121-baiduqianfan-doctor-zhihu.md`

**Testing:** `docs/designs/testings/20261121-baiduqianfan-doctor-zhihu.md`

---

## 执行位置、范围和基线

- 日期：2026-11-21。
- 工作树：`/home/agent/agent-search-gateway/.worktrees/design-baiduqianfan-doctor-zhihu-20261121`。
- 分支：`design-baiduqianfan-doctor-zhihu-20261121`。
- 设计提交：`d794a70c44f0c4093958861b17fd1668f7ff4565`；业务基线：`e712f10bdb376fa63837af1365207264ce5a22e6`。
- 本计划的 15 项任务已执行完成。阶段性失败、写入阻塞的解决过程及最终验收结果见末尾实施记录。
- 继续使用该工作树；开始执行时重新加载适用指令、核对工作树和未提交变化。所有编辑以最新读取的 SHA-256 防止覆盖其他会话的修改，新增文件禁止覆盖同名文件。
- 不修改另一个 You.com 工作树，不切换主仓库分支，不覆盖 `.ai-bridge`。只提交本任务文件。
- 核心生产改动限于新增百度适配器、默认注册表和 `doctor.py`。复用现有配置解析、HTTP 执行器、RateGate、编排器、URLStore 和结果协议。
- 不新增依赖、百度 Fetch、分支开关、公共多模态类型、通用多分支框架、云端在线鉴权、Agent 命令探测或进程管理。
- 本次不发布版本、不创建 PR、不更新用户真实配置或凭据。实现阶段的 PR/发布另按用户任务处理。

## 已核对的基线调用链

`CLI -> daemon -> SearchOrchestrator -> KeywordSearchProvider.search(query) -> HttpJsonExecutor -> KeywordSearchHit -> URL 规范化/去重 -> URLStore/ResultWriter`。

`Runtime._build_web_providers`（`src/agent_search_gateway/runtime.py:222-286`）已经对每个提供商构造一个共享 executor 和 RateGate。百度两个分支和各自重试通过同一个 executor，即可自然按物理请求计数；不需要改 Runtime 生产逻辑。

适配器返回 `raw_content` 后，Search 执行 `cheap_check` 和 `judge`；后续 URL Fetch 对保留的原文执行 `content_clean` 和 `safety`。搜索阶段不把正文标记为已经清洗。媒体只有链接，不新增下载或转录步骤。

Doctor 目前只做配置解析、三个目录探测和网关 Unix socket 探测。`_configuration_checks` 已有一次 `resolve_config`；本次从这个结果取出目标，不再次读取配置，也不构造 Runtime。

## 官方协议核对与实施前澄清

核对日期为 2026-11-21，来源限于以下官方页面：

1. [Baidu Web Search API](https://cloud.baidu.com/doc/qianfan-api/s/Jmwbl4qxs)。
2. [Baidu Agentic Search API](https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk)。
3. [Baidu Search 的错误码表](https://cloud.baidu.com/doc/qianfan-api/s/bmwbqlbad)。
4. [Web Search 接入指南](https://cloud.baidu.com/doc/qianfan/s/nmweljpt3) 与 [Agentic Search 计费/限速说明](https://cloud.baidu.com/doc/qianfan/s/Ymttv7y41)。
5. [open-webSearch HTTP API](https://github.com/Aas-ee/open-webSearch/blob/main/docs/http-api.md)，补充原设计引用的 [local-http-adapter](https://github.com/Aas-ee/open-webSearch/blob/main/docs/architecture/local-http-adapter.md)。

### 必须先补清的设计细节

- **Agentic 的官方成功示例允许 `image_result.images = null` 和 `video_result.videos = null`。** Task 1 先在错误设计第 4.1 节及测试设计 BQ-09/BQ-18 相邻说明中加上这一明确例外，然后制作 fixture。仅这两个有证据的媒体列表允许 null；不把网页列表、整个容器或所有字段的 null 一概当成空结果。
- 两接口正常示例省略顶层 `code`；官方也展示数值错误码。不可凭猜测把 `0`、`"0"` 或任意其他 code 当成成功。HTTP 200 中出现明确错误码应失败；非成功状态中的未知 code 不改变既有 HTTP 策略。
- HTTP 200 的 `InternalError` 本次固定为不可重试业务失败；文档没有足以确定其可恢复性的约定，不新增推测性重试。
- 纯空结果和 Agentic 仅卡片成功是已批准的网关契约。fixture 标注为按官方 schema 构造，不能声称它们是已捕获的线上响应；空对象、错误容器类型不能因此被吞成成功。
- Agentic `CandidateCounts.received` 固定为实际遍历的网页、图片、视频数组长度之和；卡片不进入 URL 候选枚举、不递归提取 URL。仅卡片成功时命中和计数均为零。Task 3 在测试设计中补清计数口径。
- 官方限制与本项目固定请求参数分开记录。实现固定使用 Web `web=20/image=10/video=10`，Agentic `count=10`；这是本次实现选择。它们分别不超过官方上限 `50/30/10` 和 `50`，不暴露新配置项。

### 固定请求与数据映射

| 分支 | POST 路径 | JSON 请求 | 成功结果来源 |
|---|---|---|---|
| `web_search` | `/v2/ai_search/web_search` | `messages=[{role:"user",content:query}]`、`search_source="baidu_search_v2"`、`edition="standard"`、上述 `resource_type_filter` | `references` 数组 |
| `agentic_search` | `/v2/ai_search/agentic_search` | `query=query`、`count=10` | `web_page_result.web_pages`、`image_result.images`、`video_result.videos` |

两次请求使用同一 base URL 和 Bearer key；仅在发请求时 `SecretValue.reveal()`。原样发送 query，保留中文和特殊字符，不新增截断/改写、Cookie、SDK 或模型调用。媒体只接受真实 HTTP(S) URL；Web 优先顶层 URL，缺失时按类型读取指定嵌套 URL，不无限递归找链接。

## 文件结构与职责

| 文件 | 处理 | 职责 |
|---|---|---|
| `src/agent_search_gateway/providers/web/baiduqianfan.py` | 新增 | 双分支、请求参数、明确字段解析、业务错误 inspector、安全分支诊断 |
| `src/agent_search_gateway/providers/defaults.py` | 修改 | 末尾追加一个 Search-only 注册；保留现有注册顺序 |
| `src/agent_search_gateway/doctor.py` | 修改 | 私有配置结果、启用判定、独立健康探针、结果聚合 |
| `tests/providers/web/test_baiduqianfan.py` | 新增 | 请求、解析、并发/容错、取消、错误和候选日志；使用现有 fixtures/transport 工具 |
| `tests/fixtures/providers/baiduqianfan/README.md` | 新增 | 每个 fixture 的来源、字段语义和 synthesized/official-derived 标签 |
| `tests/fixtures/providers/baiduqianfan/web_success.json` | 新增 | Web 网页成功 |
| `tests/fixtures/providers/baiduqianfan/web_multimodal.json` | 新增 | Web URL-bearing 多模态及嵌套 URL |
| `tests/fixtures/providers/baiduqianfan/web_empty.json` | 新增 | `references=[]` |
| `tests/fixtures/providers/baiduqianfan/agentic_success.json` | 新增 | 网页原文和官方 nullable 空媒体 |
| `tests/fixtures/providers/baiduqianfan/agentic_multimodal.json` | 新增 | 网页、图片、视频及不扫描的卡片 |
| `tests/fixtures/providers/baiduqianfan/agentic_empty.json` | 新增 | 明确的空结果容器 |
| `tests/fixtures/providers/baiduqianfan/agentic_card_only.json` | 新增 | 仅卡片的成功契约 |
| `tests/fixtures/providers/baiduqianfan/business_errors.json` | 新增 | 顶层 code 的参数化合成业务失败样例 |
| `tests/providers/test_registry.py`、`tests/unit/test_config_web_providers.py` | 修改 | 唯一注册、Search-only、凭据、未知配置、沿用 rate policy |
| `tests/runtime/test_new_web_runtime_assembly.py`、`tests/runtime/test_rate_gate.py` | 修改 | 单一 executor/gate、四次物理请求窗口及重试预算 |
| `tests/orchestrators/test_keyword_provider_filtering.py`、`tests/orchestrators/test_keyword_search_state.py`、`tests/orchestrators/test_url_fetch_flow.py` | 修改 | 按 provider 筛选、规范化去重、原文进入既有清洗安全流程 |
| `tests/doctor/test_open_websearch_health.py` | 新增 | HTTP probe、启用条件、失败、总时限、取消、脱敏、状态顺序 |
| `tests/doctor/test_no_network.py`、`tests/doctor/test_config.py` | 修改 | 无目标仍离线、无效配置不探测、resolve 一次 |
| `tests/acceptance/test_debug_and_doctor.py` | 修改 | 真实 doctor/CLI 边界的健康成功和失败 |
| `tests/docs/test_documented_config.py`、`tests/docs/test_installation_contract.py` | 修改 | 示例配置、真实能力、参考表数量/日期、doctor 离线例外 |
| `config.example.toml`、`README.md`、`docs/provider-rate-limits.md`、`docs/public-interface.md`、`CHANGELOG.md` | 修改 | 用户可发现配置、限流/调用成本边界、doctor 行为和补丁说明 |
| `tests/integration/test_live_baiduqianfan.py` | 新增 | 显式启用的一次双分支 live smoke |
| `tests/unit/test_live_baiduqianfan_validation.py` | 新增 | smoke 不把部分成功误报为两个 API 全部通过、输出安全、缺凭据跳过 |
| `docs/designs/error-handlings/20261121-baiduqianfan-doctor-zhihu.md`、`docs/designs/testings/20261121-baiduqianfan-doctor-zhihu.md` | 仅实施时小修 | 上述 nullable 媒体及候选计数澄清，保留原设计范围 |

只读复用：`providers/web/zhihu.py`、`providers/web/doubao.py`、`providers/web/common.py`、`providers/diagnostics.py`、`providers/http.py`、`runtime.py`、`rate_limit.py`、`orchestrators/search.py`、`orchestrators/fetch.py`、`tests/support/http.py`、`tests/support/controlled.py`、`tests/doctor/_support.py`、`tests/providers/test_http_executor.py`、`tests/providers/web/test_zhihu.py`、`tests/providers/web/test_open_websearch.py`。这些文件不会仅为接入百度而增加生产逻辑。

## 固定接口与测试原则

~~~python
class BaiduQianfanAdapter:
    def __init__(
        self, *, name: str, secret: SecretValue,
        http_executor: JsonRequester,
        api_url: str = "https://qianfan.baidubce.com",
    ) -> None: ...

    async def search(self, query: str) -> list[KeywordSearchHit]: ...
~~~

- 内部采用两个明确的私有分支方法 `_search_web(query)`、`_search_agentic(query)`；阶段字符串始终是 `web_search` 和 `agentic_search`。
- 私有解析器返回 `list[KeywordSearchHit]`；`url/title/snippet/raw_content/content` 与现有 dataclass 一致。不返回 tuple/dict 替代公共结果类型。
- HTTP inspector 使用现有 `Callable[[httpx.Response], ProviderResponseFailure | None]`；headers 使用现有延迟 callable。
- Doctor 保留 `run_doctor(paths, *, environ, directory_probe, socket_probe) -> DoctorReport`。新增私有 `_DoctorConfiguration(checks: tuple[DoctorCheck, ...], open_websearch: ResolvedWebProviderConfig | None)` 和 `_probe_open_websearch_health(raw_api_url: object) -> DoctorCheck`。测试通过 monkeypatch 模块私有探针注入异步 callable，不增加公开参数。
- `ResolvedWebProviderConfig.options` 是 `Mapping[str, object]`。现有 Web 配置只校验选项名，未统一校验 URL。探针必须验证原始值，区分“未启用”“未指定 URL 使用默认”和“已指定非法 URL”。
- 每个 Task 内的测试行为逐个执行 RED → GREEN → REFACTOR；表格中的参数化行也按单个行为递增。不要一次写完全部测试再实现。
- 新模块若尚不能导入，先补只保证可收集的接口壳，再获得行为失败；导入错误、拼写错误、fixture 解析错误不计作有效 RED。接口壳不得提前实现 HTTP、解析或容错。
- `RecordingJsonExecutor` 不执行 inspector/diagnostics callback。业务码、429、Retry-After、gate 及 HTTP 日志测试必须使用真实 `HttpJsonExecutor + httpx.MockTransport`。
- 并发/取消用 `asyncio.Event`，频率窗口用既有 `ManualClock`。不用真实 sleep 猜测先后顺序，不新增第二套模拟 HTTP 系统。
- 新接线测试若第一次已经通过，明确记录为既有基础设施的回归证据，不人为破坏生产代码制造 RED；相应生产改动可以为零。
- 默认测试命令显式设置 `WEB_SEARCH_RUN_INTEGRATION=0`，仅控制当前子进程，不修改共享环境。所有 doctor 用例使用 `tmp_path` 和显式 environ；启用健康分支时必须使用私有 fake probe 或 MockTransport。

## 执行前基线（不改业务代码）

在目标工作树中运行；依赖未准备时先使用既有 lockfile，不更新依赖或锁文件：

~~~bash
uv sync --locked --all-groups
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_zhihu.py tests/providers/web/test_open_websearch.py tests/providers/test_http_executor.py tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/doctor tests/docs -q
~~~

预期：现有离线相关测试通过。若存在预存失败，记录命令、失败用例和原始基线归属；先区分其与本任务的关系，不能把后续新增失败归为“旧问题”。不运行未隔离配置的真实 `agent-search-gateway doctor`，不启动或停止用户 daemon。

---

### Task 1: 建立独立协议 fixtures 与最小双 API 请求路径

**Files:**
- Create: `src/agent_search_gateway/providers/web/baiduqianfan.py`
- Create: `tests/providers/web/test_baiduqianfan.py`
- Create: `tests/fixtures/providers/baiduqianfan/README.md`、`tests/fixtures/providers/baiduqianfan/web_success.json`、`tests/fixtures/providers/baiduqianfan/agentic_success.json`
- Modify: `docs/designs/error-handlings/20261121-baiduqianfan-doctor-zhihu.md` 第 4.1 节
- Modify: `docs/designs/testings/20261121-baiduqianfan-doctor-zhihu.md` BQ-09/BQ-18 说明
- Inspect: `src/agent_search_gateway/providers/web/zhihu.py:80-124`、`tests/support/http.py`

- [x] **Step 1: Write the failing test**

先同步官方 nullable 媒体说明，并写最小脱敏 fixture；它们使用 `example.test` URL、合成标题/原文和假的 key。新增 `test_baiduqianfan_search_posts_both_official_payloads`：真实 executor + MockTransport 按路径返回两个独立 fixture，调用一次 `search`，断言两条结果、两次 POST、准确路径、同一 Bearer、不同请求 body 和固定分支顺序。之后单独补 `test_baiduqianfan_appends_both_paths_to_configured_base` 与 `test_baiduqianfan_preserves_query_text`，覆盖默认根、自定义路径前缀及末尾斜杠、中文/特殊字符 query。

预期行为失败：尚未发送两个协议正确的请求/返回两组命中；不是模块导入失败。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k 'official_payloads or configured_base or query_text' -v
~~~

预期：正在新增的行为断言 FAIL，其余已实现的增量测试 PASS。

- [x] **Step 3: Describe the minimal implementation**

~~~text
constructor 保存 name、SecretValue、同一 JsonRequester、校验后的 base URL
search 创建 Web 和 Agentic 两个任务，按固定顺序 gather
两个分支分别使用自己的官方 JSON 请求，headers callable 在发送时取 key
Web 成功解析 references 内的网页，Agentic 成功解析 web_pages
返回 KeywordSearchHit；不写 cleaned content
本任务先支持两个成功分支；后续任务补完整容错/取消语义
~~~

使用 common 中已有 endpoint/configured_string/字段校验 helper，不注册 provider、不修改 Runtime。Agentic fixture 中官方 nullable 媒体不能导致成功网页丢失。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/web/test_zhihu.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：目标与离线全量 PASS；live 仍 SKIP。

- [x] **Step 5: Refactor (keep tests green)**

只抽取适配器内部重复的固定 headers/endpoint 代码；不提取跨 provider 框架。若发生重构，复跑 Step 4 两条命令；否则沿用刚取得的验证结果。

### Task 2: Web Search 网页、图片、视频及逐条 URL 校验

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Create: `tests/fixtures/providers/baiduqianfan/web_multimodal.json`、`tests/fixtures/providers/baiduqianfan/web_empty.json`
- Modify: `tests/fixtures/providers/baiduqianfan/README.md`
- Inspect: `src/agent_search_gateway/providers/web/doubao.py:150-173`、`src/agent_search_gateway/url_normalization.py:33-49`

- [x] **Step 1: Write the failing test**

新增 `test_baiduqianfan_maps_web_search_url_bearing_results`：fixture 同时含网页、只有 URL 的图片/视频、指定嵌套后备 URL、顶层与嵌套均有 URL 的媒体。验证顶层优先、准确媒体标签、所有媒体正文为空、Web 的 snippet/content 片段不成为 raw_content。

随后逐个加入 `test_baiduqianfan_rejects_bad_web_candidates_without_losing_siblings`：非 object、缺失/空 URL、ftp/javascript URL、非法 hostname/port、错误文本字段类型与合法项混合；合法兄弟项全部保留。`test_baiduqianfan_preserves_duplicate_urls` 断言 adapter 保留原 URL 及重复项，留给编排器去重。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k 'web_search_url or bad_web_candidates or duplicate_urls' -v
~~~

预期：媒体/坏记录边界尚未实现的断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
严格读取 references 数组
逐条验证 object 与文本字段
优先使用非空顶层 url；仅当其缺失/为空且媒体类型匹配时读 image.url 或 video.url
已给出的非法非空顶层 URL 作为坏记录拒绝，不用后备掩盖
使用 normalize_url(url) 验证，但 hit 保留原 URL
title/snippet 映射原文本；缺摘要的 URL-only 媒体使用固定诚实类型标签
Web raw_content/content 始终为空；不把 2000 字片段当全文
每条通过既有 CandidateCounts accept/reject 恰好一次
~~~

不扫描 hover_pic、icon、未知深层对象或卡片。没有合法 URL 的条目逐条忽略，不导致同级合法项失败。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/web/test_doubao.py tests/unit/test_url_normalization.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：各 URL/媒体断言 PASS，现有 URL 语义不变。

- [x] **Step 5: Refactor (keep tests green)**

复用 common 和 normalize_url 的既有错误/拒绝原因，不新增 URL 规范化实现。若调整代码，复跑 Step 4。

### Task 3: Agentic 模态、合法空结果和原文映射

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Create: `tests/fixtures/providers/baiduqianfan/agentic_multimodal.json`、`tests/fixtures/providers/baiduqianfan/agentic_empty.json`、`tests/fixtures/providers/baiduqianfan/agentic_card_only.json`
- Modify: `tests/fixtures/providers/baiduqianfan/README.md`
- Modify: `docs/designs/testings/20261121-baiduqianfan-doctor-zhihu.md` 候选计数/空模态说明

- [x] **Step 1: Write the failing test**

新增 `test_baiduqianfan_maps_agentic_modalities_and_raw_content`：网页、图片、视频、卡片同时出现；网页 content 只进入 raw_content，cleaned content 为空；媒体 URL/标签保留且不带正文。

分别新增 `test_baiduqianfan_accepts_empty_and_null_modalities` 和 `test_baiduqianfan_accepts_card_only_response`：网页空数组、图片/视频空数组或官方 null、可选模态省略、两 API 均合法空、只有卡片的成功。卡片含一个诱人的深层 URL，仍不得提取。固定计数只包含实际枚举的三类 URL 候选；卡片不计 received/rejected。

新增 `test_baiduqianfan_rejects_bad_agentic_candidates_without_losing_siblings`，按 webpage/image/video 参数化，在同一模态中混入非 object、缺 URL、非法 URL、错误文字字段，同时保留同列表与其他模态的合法项。断言合法项保留、聚合 received=accepted+rejected 且 unprocessed=0；错误网页 content 类型只拒绝该记录，媒体正文仍为空。Web 解析器的坏记录测试不能替代这一组。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k 'agentic_modalities or empty_and_null or card_only or bad_agentic_candidates' -v
~~~

预期：尚未覆盖的媒体/空结果或 raw_content 映射 FAIL；官方 nullable 成功 fixture 不得因测试写错而成为假 RED。

- [x] **Step 3: Describe the minimal implementation**

~~~text
仅枚举已知结果容器；整体必须至少包含一个可识别的有效结果容器
web_pages 必须为数组；images/videos 的官方 null 转为空枚举
可选模态未出现则跳过；出现非空错误类型则分支失败
网页字段按已有 string helper 验证，原文 content -> hit.raw_content
图片/视频只读其官方 url，缺标题摘要用准确类型标签
card_result 验证必要容器形状后忽略内容；不按卡片 type 枚举拒绝未知卡片
两个分支合法零命中返回 []，不按 bool(hits) 判断成功
~~~

明确 `{}`、`{"unexpected":[] }` 或坏容器不是“合法空结果”。只对已经证实的 images/videos null 放宽。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：原文、媒体、空数组、nullable、仅卡片全部 PASS。

- [x] **Step 5: Refactor (keep tests green)**

只有当两种解析器出现实际重复时，抽取适配器内部单候选转换 helper；保留独立 envelope 校验。若重构，复跑 Step 4。

### Task 4: 严格区分坏 envelope、业务错误与坏候选

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Create: `tests/fixtures/providers/baiduqianfan/business_errors.json`
- Modify: `tests/fixtures/providers/baiduqianfan/README.md`
- Inspect: `src/agent_search_gateway/providers/http.py:114-136,191-207,424-465`

- [x] **Step 1: Write the failing test**

新增 `test_baiduqianfan_invalid_envelope_is_a_branch_failure`，按分支参数化非 JSON、顶层数组、缺 references/全部已知容器、错误容器/非 null 列表类型。测试初期可直接调用两个私有分支边界，聚合降级由 Task 5 验证。

新增 `test_baiduqianfan_http_200_business_error_is_not_empty_success`：顶层 InvalidArgument、InternalError、数值错误码、未知非空 code、错误 code 类型，同时放入看似成功的数据，确保失败优先；正常无 code 仍成功。使用真实 executor 检查 inspector 生效，不用 RecordingJsonExecutor 代替。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k 'invalid_envelope or business_error' -v
~~~

预期：原本错误地返回 []/命中的分支现在需要失败，当前行为断言 RED。

- [x] **Step 3: Describe the minimal implementation**

~~~text
response inspector 只依据已确认的顶层 code
HTTP 200 有错误 code -> ProviderResponseFailure(retryable=False)
已识别固定 code 可进安全诊断；未知 code 仅记本地固定类别，不记原文
HTTP 200 非 JSON 留给既有 decode -> ProtocolFailure
无业务 code 时分别严格校验成功 envelope
单条坏记录仅拒绝该记录；坏 envelope 使该分支失败
其他 HTTP 状态先保持共享 executor 行为，429 特例在 Task 7 增加
~~~

不添加未经证实的 `error.code` 回退、成功 code=0 白名单、上游 message 日志或全局重试改动。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/test_http_executor.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：200 业务错误/结构错误失败，合法空和坏记录隔离仍 PASS。

- [x] **Step 5: Refactor (keep tests green)**

让两个分支复用一个百度私有错误分类函数，通过闭包绑定 stage；不修改公共 inspector 类型。若重构，复跑 Step 4。

### Task 5: 双分支并行、部分失败与确定性汇总

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Inspect: `src/agent_search_gateway/providers/web/zhihu.py:80-124`、`tests/providers/web/test_zhihu.py`

- [x] **Step 1: Write the failing test**

新增 `test_baiduqianfan_starts_both_branches_before_either_finishes`：两个分支各自设置 entered Event 并等待 release Event；只有两个 entered 都已发生才释放，证明实际并行，不依赖耗时比较。

新增 `test_baiduqianfan_preserves_successful_branch_after_sibling_failure`，分别覆盖 Web/Agentic 失败和成功分支为空/非空；失败可为 HTTP 403、200 业务错误、协议异常。新增 `test_baiduqianfan_aggregates_two_failures_using_fixed_first_branch`，交换完成顺序仍按 Web→Agentic 选择安全代表原因。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k 'starts_both or sibling_failure or two_failures' -v
~~~

预期：普通 gather 在一支失败时丢失另一支成功结果，或双失败错误语义不符合现有 provider 失败契约。

- [x] **Step 3: Describe the minimal implementation**

~~~text
仿照 Zhihu 的 run_branch：只捕获 Exception，返回 hits 或普通异常
create_task 两支，然后 gather，固定处理顺序 Web -> Agentic
成功判定为分支未抛异常；[] 也成功
至少一支成功 -> 拼接全部成功 hits，记录失败支的安全 partial_failure
两支均失败 -> ExecutionFailure(ALL_PROVIDERS_FAILED)，保留安全代表原因
不在整个 search 外层再次重试；不重复 provider_completed/provider_failed
~~~

HTTP 层各自重试与逻辑 provider 终态保持原职责。adapter 不提前去重。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/web/test_zhihu.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：并发、单支空成功、两个方向的部分失败和双失败均 PASS。

- [x] **Step 5: Refactor (keep tests green)**

仅复用知乎现有控制流模式，不抽取通用框架，不替换为改变取消/降级语义的 TaskGroup。若重构，复跑 Step 4。

### Task 6: 分支取消、调用方取消与异步清理

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Inspect: `src/agent_search_gateway/providers/web/zhihu.py:91-97`、`tests/providers/web/test_zhihu.py` 取消用例

- [x] **Step 1: Write the failing test**

逐个新增：

- `test_baiduqianfan_branch_cancellation_cancels_and_awaits_sibling`：一支抛 CancelledError，兄弟支阻塞，必须被取消并完成 finally。
- `test_baiduqianfan_caller_cancellation_awaits_asynchronous_branch_cleanup`：两个请求均已进入；取消父 task；finally 中再等待受控 Event，证明 search 确实等待清理完成，不只是发出 cancel。
- `test_baiduqianfan_retry_sleep_cancellation_stops_later_requests`：取消发生于既有重试等待时，不能继续发送下一次请求。
- 所有取消情形不记录 provider_partial_failure，也不伪造成功结果。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/web/test_zhihu.py -k cancellation -v
~~~

预期：百度尚未实现的清理断言 FAIL；知乎原有取消回归 PASS。

- [x] **Step 3: Describe the minimal implementation**

~~~text
外层 gather 捕获 asyncio.CancelledError
仅对 not task.done() 且 not task.cancelling() 的兄弟 task 再 cancel
await gather(全部子任务, return_exceptions=True) 完成清理
原样 re-raise CancelledError
分支普通异常捕获不包括 BaseException
~~~

不吞掉取消、不把 CancelledError 转成普通分支失败；防止二次 cancel 打断已经进入的异步 finally。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/web/test_zhihu.py tests/providers/test_http_executor.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：父子取消、retry wait 取消和所有旧取消测试 PASS，无遗留任务。

- [x] **Step 5: Refactor (keep tests green)**

保持清理代码靠近双任务创建/gather，避免增加任务注册器。若重构，复跑 Step 4。

### Task 7: 配额型 429 不重试，频率型 429 保留 Retry-After

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Modify: `tests/fixtures/providers/baiduqianfan/business_errors.json`
- Inspect: `src/agent_search_gateway/providers/http.py:424-465,665-669`、`tests/providers/test_http_executor.py`

- [x] **Step 1: Write the failing test**

使用真实 executor + MockTransport，按分支参数化：

| 测试 | 输入与断言 |
|---|---|
| `test_baiduqianfan_quota_and_balance_429_are_not_retried` | 两个已知 quota/balance code，各只发一次；兄弟支正常，成功结果保留 |
| `test_baiduqianfan_qps_and_unknown_429_keep_retry_after` | QPS code、未知 code、不能解码的 429 body；先 429 后成功，观察既有 server delay，第二次发送成功 |
| `test_baiduqianfan_uses_existing_transport_and_status_retry_policy` | 408、5xx、连接错误和 timeout，按已有尝试次数结束，不再重试整个 search |
| `test_baiduqianfan_non_retryable_http_error_does_not_retry` | 400/401/402/403/404/422 维持既有不可重试 HTTP 行为 |

QPS case 的 `Retry-After` 应与普通 backoff 明显不同，用注入 sleep/时钟记录等待；只断言 retryable=True 不能证明保留了服务端等待。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k '429 or retry_policy or non_retryable_http' -v
~~~

预期：quota/balance 仍被通用 429 重试；其他沿用公共策略的用例可能首次即 PASS，按回归记录。

- [x] **Step 3: Describe the minimal implementation**

~~~text
inspector 读取已知顶层 code
if status == 429 and code in {QUOTA_USER_DAILY_FREE, BILLING_INSUFFICIENT_BALANCE}:
    return ProviderResponseFailure(retryable=False, 本地 safe_reason, 安全诊断)
if status == 429:
    return None  # 交给共享 HTTP 状态/Retry-After 路径
if status == 200 and 明确业务错误:
    return 不可重试 ProviderResponseFailure
其他状态 return None
~~~

不要将 QPS/未知 429 包装为 retryable ProviderResponseFailure：这会改走不保留响应 Retry-After 的执行器路径。不要添加百度自己的 retry loop、每日额度计数或付费回退。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/providers/test_http_executor.py tests/unit/test_retry.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：已知不可重试额度每支一次；QPS/未知 429、408/5xx/transport 按既有策略重试。

- [x] **Step 5: Refactor (keep tests green)**

提取百度私有已知码集合即可；不扩张全局 reason allowlist 或全局 429 规则。若重构，复跑 Step 4。

### Task 8: 候选计数、分支日志与敏感信息边界

**Files:**
- Modify: `src/agent_search_gateway/providers/web/baiduqianfan.py`、`tests/providers/web/test_baiduqianfan.py`
- Inspect: `src/agent_search_gateway/providers/diagnostics.py:74-110,167-238`、`tests/observability/test_provider_diagnostics.py`、`tests/observability/test_redaction.py`

- [x] **Step 1: Write the failing test**

新增 `test_baiduqianfan_logs_only_safe_branch_diagnostics`，caplog 检查成功分支各一条 `provider_candidates_summary`，received=accepted+rejected，unprocessed=0，stage 名准确，合法空分支也有零计数 summary。

新增 `test_baiduqianfan_ordinary_exception_text_is_never_rendered`：普通异常、业务 message、request_id、未知 code、带签名 URL 均放入独立 sentinel；adapter partial/candidate 事件中不出现这些内容，已知码和固定类别仍能定位分支。检查参数化普通异常/HTTP 200/429 路径。

DEBUG 的 HTTP body preview 沿用现有截断脱敏规则；不要新增“所有 DEBUG 都不得含 query”的伪约束，也不要把现有安全 preview 的允许行为误判为泄漏。真实 key/Authorization/token sentinel 在最终格式化输出中必须不可见。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py -k 'safe_branch_diagnostics or exception_text' -v
~~~

预期：缺失 summary、安全分类不完整或无意传入 raw message 的断言 FAIL。

- [x] **Step 3: Describe the minimal implementation**

~~~text
每个成功分支维护一个 CandidateCounts，处理完后发一次 summary
partial_failure 复用 safe_failure_reason + diagnostic_fields_from_failure
Baidu 私有 response_diagnostics 只选择已认可 code 和本地分类
不直接使用会带入未审核 message 的通用 selected_error_fields
不记录 request_id、原始异常、卡片 body、签名 URL、完整 query 或完整 payload
HTTP attempt 日志继续由 executor 发；provider 终态继续由 orchestrator 发
~~~

`ProviderResponseFailure` 的 partial reason 可能是既有 `execution_failure`；不要为了输出新的 reason 修改公共协议，具体业务区别保留在安全 known code 中。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web/test_baiduqianfan.py tests/observability -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：计数完整，adapter 事件无未经审核内容，既有 body preview 和脱敏回归通过。

- [x] **Step 5: Refactor (keep tests green)**

只合并已重复的百度安全诊断构建代码，不增加新的日志层。若重构，复跑 Step 4。

### Task 9: 注册、Search-only 配置及示例/参考表同步

**Files:**
- Modify: `src/agent_search_gateway/providers/defaults.py:28-183`
- Modify: `tests/providers/test_registry.py:157-203`、`tests/unit/test_config_web_providers.py`
- Modify: `config.example.toml:139-190`、`README.md` Web 能力/配置和 rate reference 段、`docs/provider-rate-limits.md`
- Modify: `tests/docs/test_documented_config.py:30-103,344-391,494-522`
- Inspect: `src/agent_search_gateway/config.py:25-33,95-109,145-244`

- [x] **Step 1: Write the failing test**

新增 `test_default_registry_adds_one_baiduqianfan_search_only_provider`，断言唯一注册、factory 类型、search=True/fetch=False、requires_api_key=True、仅 api_url 专有配置。原四个新 provider 的顺序断言改为精确切片 `[20:24]`，再独立验证尾部仅百度；不改前 24 个顺序。

在现有 service-key search 配置矩阵加入百度，另以准确环境名验证 enabled 成功/未启用缺 key 仍有效。非法 Fetch、空/缺 key、未知键、非法 rate_limits 沿用现有拒绝。新增配置断言不要求硬编码环境变量读取：环境名由配置传入，适配器继续只接 SecretValue。

文档契约新增百度默认关闭和 3/1s 窗口；独立验证启用 Search、保持 Fetch=false。不能把百度直接加入要求 Search+Fetch 的新四 provider 启用矩阵。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/docs/test_documented_config.py -v
~~~

预期：百度未注册/示例缺表/参考页缺行的真实契约 FAIL。每次只新增当前增量测试，再实施对应变更。

- [x] **Step 3: Describe the minimal implementation**

在 registrations 末尾追加 `WebProviderRegistration("baiduqianfan", ProviderCapabilities(search=True, fetch=False), BaiduQianfanAdapter, frozenset({"api_url"}))`。保留 requires_api_key 默认 true，不修改 config.py。

新增完整 opt-in 表：

~~~toml
[web_providers.baiduqianfan]
# One search calls Web Search and Agentic Search; retries count as additional attempts.
# This shared window is conservative and does not enforce free billing quotas.
rate_limits = [{ requests = 3, period_seconds = 1 }]
enable_search = false
enable_fetch = false
api_url = "https://qianfan.baidubce.com"
api_key_env = "[REDACTED_SECRET]"
~~~

上表的 `api_key_env` 使用环境变量名占位符；实施时必须将该字段写成本文固定的环境变量名 `BAIDUQIANFAN_API_KEY`，不能写入真实 key 值，也不能保留占位符作为最终配置。文档测试必须断言这个准确环境名。

知乎表只补注释：统一固定 QPS 未公开、账户额度不能推出通用速率，继续继承默认并发量及 429 重试；不添加 rate_limits/min_interval_seconds，不改变启用状态。

README 增加百度能力行、env 名、固定双分支、raw_content/媒体链接边界、一次逻辑调用通常两次物理请求和免费额度非本地保障。`docs/provider-rate-limits.md` 增加一条官方来源完整的百度行，Web 数量 24→25，总数 31→32；共享 3/1s 是本项目保守策略，不声称两种免费额度独立。

文档测试保持完整覆盖：总行数预期 32；旧行 Verified 继续 2026-11-19，百度新增行是 2026-11-21，以按 provider 的日期期望校验。不要为通过统一日期断言而伪造重新核验全部旧 provider。expected_windows 增加百度，示例 provider 集合增加百度。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/docs -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：registry/config/docs 一起 PASS；知乎 policy 仍无固定窗口/间隔，百度 disabled 不要求 key。

- [x] **Step 5: Refactor (keep tests green)**

复用 Search-only 配置测试矩阵；保持旧四双能力 provider 的用例不受影响。若重构，复跑 Step 4。按本任务整体完成后再提交，避免注册数量与文档断言暂时不一致的提交。

### Task 10: 共享 RateGate、重试尝试计数与 Runtime 装配

**Files:**
- Modify: `tests/runtime/test_new_web_runtime_assembly.py`、`tests/runtime/test_rate_gate.py`
- Modify: `tests/providers/web/test_baiduqianfan.py`（必要的适配器+真实 executor 场景）
- Inspect: `src/agent_search_gateway/runtime.py:222-286`、`tests/support/controlled.py:29-54`、`tests/providers/test_http_executor.py:632-714`
- Production change: 原则上无；只有测试证明百度适配器绕过已注入 executor 时才修正该适配器

- [x] **Step 1: Write the failing test**

单独新增 `test_baiduqianfan_runtime_owns_one_executor_and_gate`：显式 Search=true/Fetch=false，构建 Runtime；只有一个百度搜索 adapter、零百度 fetch adapter、一个 executor/gate，两个分支都使用此 executor，关闭时 client 只释放一次。不要把百度加入现有假设双能力的 `_ADAPTERS` 矩阵。

新增 `test_baiduqianfan_two_searches_share_three_request_window`：同一 adapter 同时 search 两次，真实 executor + MockTransport + ManualClock，3/1s；t=0 只能看到前三次 HTTP，第四次仍等待，推进到 t=1 后发出。不约束第四次恰好属于哪个分支。

新增 `test_baiduqianfan_branch_retry_reserves_shared_rate_permit` 和 `test_baiduqianfan_rate_wait_timeout_does_not_send_blocked_attempt`：重试再次计 permit；等待超预算的分支没有发出 HTTP，沿用 rate_limit_wait_timeout，兄弟支仍可成功。删除 rate_limits 后无隐含 3 QPS 限制。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime/test_new_web_runtime_assembly.py tests/runtime/test_rate_gate.py tests/providers/web/test_baiduqianfan.py -k 'baiduqianfan and (executor or gate or window or permit or rate_wait)' -v
~~~

预期：如果 adapter 未完整复用 executor/gate，接线断言 FAIL；正确复用时可首次 PASS，记录为 RATE-01～05 的回归证据，不修改已正确的公共组件。

- [x] **Step 3: Describe the minimal implementation**

~~~text
每个分支直接调用 self._http.request_json，不 new client/executor/gate
Runtime 默认 factory 装配一次百度 adapter
每次物理 send/retry 的 permit 继续由同一 HttpJsonExecutor 获取
不在逻辑 search 外再获取一个 rate permit，不按逻辑搜索数计费
取消/超时没有发出的请求不算已发送尝试
~~~

gate identity 可沿用现有断言：executor 的 gate 与 `runtime.quotas.get_rate("web", "baiduqianfan")` 是同一实例。该私有断言只补充实际四次 HTTP 时间行为，不能单独代替行为验证。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/runtime tests/providers/web/test_baiduqianfan.py tests/providers/test_http_executor.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：共享窗口、重试和预算规则 PASS；删除配置仍沿用原无限频率语义。

- [x] **Step 5: Refactor (keep tests green)**

只复用已有 ManualClock、_ClientFactory、runtime 构建辅助，保留原四提供商用例。无生产重构需求；发生测试重构则复跑 Step 4。

### Task 11: Provider 选择、URL 去重和原文进入既有安全流程

**Files:**
- Modify: `tests/orchestrators/test_keyword_provider_filtering.py`、`tests/orchestrators/test_keyword_search_state.py`、`tests/orchestrators/test_url_fetch_flow.py`
- Inspect: `src/agent_search_gateway/orchestrators/search.py:681-745`、`src/agent_search_gateway/orchestrators/fetch.py:91-178`
- Inspect: `tests/orchestrators/test_keyword_search_state.py:82-207`、`tests/orchestrators/test_url_fetch_flow.py:17-85`
- Production change: 无编排器/URLStore 协议改动

- [x] **Step 1: Write the failing test**

增加真实百度 adapter 参与的薄连接用例：

- `test_baiduqianfan_selection_calls_only_one_logical_provider`：选择百度时其他 provider 不调用，但百度两个内部路径均调用；仅 Fetch 不支持的限制仍由 registry/config 决定。
- `test_baiduqianfan_results_use_existing_url_normalization_and_dedup`：覆盖两个分支完全相同的 URL、scheme/主机名大小写与首尾空白的现有规范化、Google goto 与同一直接目标的去重。不同 query 参数继续遵循现有 URL 身份规则；带追踪参数的 URL 保留其 query，不新增去追踪功能，也不要求不同追踪 query 必然合并。
- `test_baiduqianfan_agentic_raw_content_uses_existing_clean_and_safety_pipeline`：Search 调 judge，原文入同一 URLStore 后 content 仍空；Fetch 用缓存原文调用 content_clean，再 safety，无二次网页 fetch。
- 增量覆盖 cheap_check/judge 拒绝正文但保留已准入 URL/摘要，以及 safety 拒绝不能返回正文；URL-only 媒体不进入新媒体处理链。
- 通过 caplog 验证单支失败仍只有一次逻辑 provider_completed；双失败只有一次 provider_failed。adapter 的 partial_failure 不算第二个终态。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators/test_keyword_provider_filtering.py tests/orchestrators/test_keyword_search_state.py tests/orchestrators/test_url_fetch_flow.py -k baiduqianfan -v
~~~

预期：若 raw_content/title/snippet 映射错误、去重被绕过或终态重复，对应断言 FAIL；其余既有流程可以直接作为 PASS 回归。

- [x] **Step 3: Describe the minimal implementation**

~~~text
SearchOrchestrator 仍消费 KeywordSearchHit
Baidu adapter 只纠正已被测试定位的映射错误
网页原文保留 raw_content；content=""
媒体无标题时提供诚实类型标签以满足 title/snippet 非空摘要路径
去重、admission、judge、clean、安全拒绝均沿用原实现
~~~

连接测试使用现有受控 LLM client/flow fixture 和同一个 URLStore；不要在测试中复制整套生产 pipeline，也不要为了新 provider 改公共 orchestrator。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/orchestrators tests/acceptance/test_gateway_workflows.py tests/observability/test_provider_event_logging.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：筛选、去重、缓存原文清洗、安全拒绝与终态计数 PASS，JSONL 结果契约不变。

- [x] **Step 5: Refactor (keep tests green)**

复用现有受控 fixtures，保留职责边界。无生产改动时直接记录证据；若调整 fixture/映射，复跑 Step 4。

### Task 12: 实现独立、无凭证的 open-webSearch 健康 GET

**Files:**
- Modify: `src/agent_search_gateway/doctor.py`（新增私有 URL 构建及探针）
- Create: `tests/doctor/test_open_websearch_health.py`
- Inspect: `src/agent_search_gateway/providers/web/open_websearch.py:72-90`、`tests/doctor/_support.py`
- Docs: [open-webSearch HTTP API](https://github.com/Aas-ee/open-webSearch/blob/main/docs/http-api.md)

- [x] **Step 1: Write the failing test**

直接测试私有异步探针（暂不改 run_doctor 路由），用 MockTransport 捕获请求：

| 测试 | 场景/断言 |
|---|---|
| `test_health_accepts_exact_success_envelope` | HTTP 200、status=ok、data.daemon=running 得到 OK |
| `test_health_preserves_path_prefix_and_strips_query_fragment` | 根、末尾斜杠、/service 前缀、含 query/fragment 的配置均得到正确 /health URL |
| `test_health_rejects_invalid_url_and_userinfo_without_client` | 空字符串、None、非字符串、错误 scheme/host/port、userinfo；FAIL 且不构建 client |
| `test_health_sends_no_credentials_body_or_redirected_request` | GET 无 Authorization/Proxy-Authorization/Cookie/body，只发一次，客户端不继承代理/凭证 |

所有含 userinfo/query 的安全哨兵只用保留的 `example.test` 域和合成 token。断言错误不回显原地址。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/doctor/test_open_websearch_health.py -k 'success_envelope or path_prefix or invalid_url or credentials' -v
~~~

预期：健康 GET/安全 URL 处理尚未提供的行为断言 FAIL；模块/函数接口壳不包含网络实现。

- [x] **Step 3: Describe the minimal implementation**

~~~text
_probe_open_websearch_health(raw_api_url: object):
    验证 str、非空、HTTP(S)、host、可解析端口
    拒绝 userinfo，不触发 HTTPX 自动 BasicAuth
    保留服务 path 前缀，rstrip("/") 后追加 "/health"
    清除 query 和 fragment
    创建独立 AsyncClient(follow_redirects=False, trust_env=False, timeout=2.0)
    无 auth、无继承 cookie/header、无业务 body
    只 GET 一次
    仅精确 HTTP200 + dict status="ok" + dict data.daemon="running" 为 OK
~~~

默认 URL 与 adapter 相同：`http://127.0.0.1:3210`。缺少配置键的默认选择在 Task 14 完成；非法的显式值不能回退默认。不要使用会丢路径前缀的 `urljoin(base, "/health")`，也不要直接把 /health 接在含 query 的原字符串末尾。

本项目选择健康探针直连、禁重定向且不继承环境代理；官方 localhost 示例也建议绕过 shell proxy。实现不扩展 TLS、代理或兼容性配置。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/doctor -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：合法 GET/URL/凭证边界 PASS；尚未接入 run_doctor 时旧本地检查仍通过。

- [x] **Step 5: Refactor (keep tests green)**

健康代码留在现有 doctor 模块，必要时一个私有 URL helper 足够；不引入通用 HealthClient。若重构，复跑 Step 4。

### Task 13: 健康失败分类、总超时和取消传播

**Files:**
- Modify: `src/agent_search_gateway/doctor.py`、`tests/doctor/test_open_websearch_health.py`

- [x] **Step 1: Write the failing test**

逐个增加：

- `test_health_requires_exact_success_envelope`：200 的空 body、非 JSON、顶层数组、缺 status/data/daemon、错误值/类型均 FAIL。
- `test_health_classifies_transport_timeout_and_http_failures_safely`：ConnectError、TimeoutException、协议错误、301/302/307/401/404/500、其他非 200 状态；静态原因/HTTP 数字可见，响应/异常/Location 中的秘密哨兵不可见，不跟随重定向。
- `test_health_has_total_deadline_without_retry`：异步 transport 等待 Event，测试注入很小的私有时限，验证完整 await 有界结束且只尝试一次；不真实等两秒，不只检查 HTTPX timeout 属性。
- `test_health_cancellation_propagates_and_closes_client`：请求已进入后取消父 task，传播 CancelledError，client 被关闭，不伪造 OK/FAIL。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/doctor/test_open_websearch_health.py -k 'requires_exact or classifies or total_deadline or cancellation' -v
~~~

预期：未处理的失败/总时限/取消边界 FAIL。测试人为抛出的异常必须来自受控 transport，不能访问真实网络。

- [x] **Step 3: Describe the minimal implementation**

~~~text
使用 asyncio.timeout(2.0) 包住整段异步探测，给出总墙钟预算
HTTPX 阶段 timeout 只是补充，不作为总预算替代
已知 URL 错误 -> FAIL invalid_health_url
超时 -> FAIL timeout；传输/协议失败 -> FAIL connection_failed
非 200 -> FAIL + 安全状态数字
坏 JSON/错误 envelope -> FAIL invalid_health_response
不输出 body、原始异常、URL、Location；不重试
CancelledError 原样传播，async with 关闭 client
~~~

不要捕获 BaseException，不自动启动进程，不给健康探测套 HTTP executor 的重试/限流。意外实现错误继续由现有 CLI internal-error 边界处理。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/doctor tests/cli/test_cli.py -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：失败分类、总超时、无重试和取消清理 PASS。

- [x] **Step 5: Refactor (keep tests green)**

失败文案使用固定短类别，避免散落原始异常插值；若重构，复跑 Step 4。

### Task 14: Doctor 按启用状态接线，保持离线分支与退出码

**Files:**
- Modify: `src/agent_search_gateway/doctor.py:49-62,111-153`
- Modify: `tests/doctor/test_open_websearch_health.py`、`tests/doctor/test_no_network.py`、`tests/doctor/test_config.py`
- Modify: `tests/acceptance/test_debug_and_doctor.py`、`tests/docs/test_installation_contract.py`
- Docs: `README.md`、`docs/public-interface.md`、`CHANGELOG.md`
- Inspect: `tests/doctor/_support.py`、`tests/doctor/test_socket.py`、`tests/doctor/test_filesystem.py`、`tests/cli/test_cli.py:538-648`

- [x] **Step 1: Write the failing test**

先新增 `test_doctor_probes_enabled_open_websearch_once`：Search-only、Fetch-only、both 都只调用一次私有异步探针；省略 api_url 使用 adapter 默认地址，自定义值原样交给探针验证，非法显式 None 不能被当作未启用。

增量新增：

- `test_doctor_resolves_config_once_and_passes_enabled_target`：真实 resolve_config 外层 spy 只被调用一次，保持配置/env 检查。
- `test_doctor_skips_health_for_missing_disabled_or_invalid_config`：未配置、两能力关闭、缺配置、非法 TOML、其他 provider 缺 key、未知配置项时不探测；依然报告目录和 socket。
- `test_doctor_health_failure_is_not_hidden_by_missing_gateway_socket`：健康 FAIL + 网关 socket INFO，完整报告 exit_code=1；健康 OK + socket INFO 可以为 0；socket 最后一行不变。
- `test_doctor_cli_enabled_open_websearch_reports_health_without_runtime`：经既有 CLI/acceptance 路径渲染真实 DoctorReport，验证输出和退出码，禁止 Runtime/搜索/子进程。
- 把现有 `test_doctor_does_not_build_runtime_or_construct_http_client` 明确限定并参数化“未配置/未启用 open_websearch”；保留禁止 HTTP client/Runtime 的断言。现有 Agent 缺失可执行文件仍保持本地静态测试。

所有启用场景通过 monkeypatch 私有探针或 MockTransport 隔离，不读取用户 home 配置，不启动/终止 daemon。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/doctor/test_open_websearch_health.py tests/doctor/test_config.py tests/doctor/test_no_network.py tests/acceptance/test_debug_and_doctor.py -k 'doctor and (open_websearch or health or config or network)' -v
~~~

预期：旧 run_doctor 没有探测已启用服务，健康 FAIL/启用判定的断言 RED；旧离线分支保持 PASS。

- [x] **Step 3: Describe the minimal implementation**

~~~text
_configuration_checks:
    原有一次 load_toml / resolve_config
    所有错误返回 _DoctorConfiguration(existing_fail_checks, None)
    成功保留原有 checks，选出启用 Search 或 Fetch 的 open_websearch
    返回 _DoctorConfiguration(tuple(checks), enabled_provider_or_none)

run_doctor:
    config_result = _configuration_checks(...)
    加入 config_result.checks
    按原顺序探测 socket parent、results、logs 三目录
    if config_result.open_websearch is not None:
        raw = options.get("api_url", "http://127.0.0.1:3210")
        加入 await _probe_open_websearch_health(raw)
    最后继续既有 Unix socket 检查
    返回原 DoctorReport；exit_code 原聚合逻辑不变
~~~

不要新增 health_probe 公共参数。私有函数在调用时从模块取值，测试可替换它；CLI runner fake 不需要改签名。既有 directory/socket 参数与返回类型保持不变。

同步当前用户文档：README 开头和安装段不再笼统声称 doctor 永不联网；说明没有启用 open_websearch 时仍 no network，启用时仅一个无凭证 GET /health，失败 exit 1，网关 socket 缺失仍 INFO；健康只证明 daemon liveness。API key 是否真实有效、额度、云服务可用性、Agent 可执行文件和内部引擎 readiness 仍未在线验证。

在 `docs/public-interface.md` 记录这个已启用服务例外和退出约定，不修改 CLI/daemon/JSONL schema；在 `CHANGELOG.md` 的未发布区域（没有则新增 Unreleased）记录百度、doctor 和知乎说明三项，不修改版本号或已发布历史。不回写旧版本设计文档来掩盖过去的离线行为。

更新已有 installation contract 测试，同时保留 “no network” 的有条件承诺并断言新增例外；不仅删除旧断言。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/doctor tests/cli/test_cli.py tests/acceptance/test_debug_and_doctor.py tests/docs -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：DOC-01～15 全部覆盖；未启动 open-webSearch 为 FAIL，网关未启动 INFO 不遮蔽它；默认测试仍不访问公网/真实本地 HTTP 服务。

- [x] **Step 5: Refactor (keep tests green)**

仅抽取 private config result/health helper；不扩展全体 Web options 校验，不校验其他 provider 的真实连通性。旧 socket 最后一行、三个目录顺序和 Agent 静态分支保持通过。若重构，复跑 Step 4。

### Task 15: 可选单提供商 live smoke 与离线 smoke 判定验证

**Files:**
- Create: `tests/integration/test_live_baiduqianfan.py`
- Create: `tests/unit/test_live_baiduqianfan_validation.py`
- Docs: `README.md` Opt-in live integration checks
- Inspect: `tests/integration/test_live_new_web_providers.py`、`tests/unit/test_live_smoke_validation.py`

- [x] **Step 1: Write the failing test**

先在 unit 文件用 MockTransport 驱动拟新增的 smoke runner，不发送真实请求，验证：

- `test_baidu_smoke_requires_both_branches_to_be_verified`：参数化一支 HTTP 403、HTTP 200 业务错误、HTTP 200 坏 envelope；另一支均正常。adapter 合理返回部分结果，但 smoke 不能把它报告为“两接口全部通过”；两个状态码都是 200 也不足以通过。
- `test_baidu_smoke_accepts_verified_empty_results`：两个分支成功解析且合法零命中也可完成协议验证，并断言存在两个准确 stage 的零计数 summary；不能用 bool(hits) 或仅路径/状态代替成功解析证据。
- `test_baidu_smoke_reports_only_safe_metrics`：异常、body、凭据、签名 URL sentinel 不出现在输出；仅固定阶段、次数、HTTP 数字、已知分类/码、命中数和耗时。
- `test_baidu_smoke_skips_missing_credentials_without_network`：缺 key 跳过、不构造 client；配置齐全后的鉴权/额度/协议失败不能被 SKIP 掩盖。
- 现有 module-level `WEB_SEARCH_RUN_INTEGRATION` opt-in 与普通测试隔离保持一致。unit 测试显式注入 fake client/环境，不因复用 live helper 真正联网。

- [x] **Step 2: Run test to verify it fails**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_live_baiduqianfan_validation.py -v
~~~

预期：smoke 的两分支证明、安全报告或缺 key 行为尚未实现，针对性的行为断言 FAIL；live 测试本身仍默认 SKIP。

- [x] **Step 3: Describe the minimal implementation**

~~~text
live 模块沿用现有 opt-in marker，只用 BAIDUQIANFAN_API_KEY
创建真实百度 adapter、单 executor、3/1s gate 和有界现有 RetryPolicy
一次短 query 调用 adapter.search；不再分别重复请求每个分支
临时仅启用百度 adapter logger 的 DEBUG，捕获 provider_candidates_summary 的安全结构字段
按固定路径/阶段观察每支实际尝试和 HTTP 状态；按两个 stage 的 summary 证明成功解析
退出收集上下文时恢复 logger 的原配置（含异常路径），不全局启用 HTTP body preview
两个分支均有成功协议证据 -> smoke 通过，命中数允许 0
只有逻辑部分成功 -> 报告已验证/失败分支，smoke FAIL
打印安全 metrics；pytest.fail 使用固定信息且 pytrace=False，避免 raw exception/payload
~~~

使用 pytest 的 `caplog.at_level(logging.DEBUG, logger=<百度 adapter 的 logger 名>)` 或等价的可恢复收集器。只读取该 adapter 的 summary 事件及固定 stage/计数字段，不打印原始 LogRecord、body 或 response；复用项目当前 LogRecord 结构，不假设默认日志级别会创建 DEBUG 事件。离线 smoke 验证也走同一个收集机制。

可沿用已有新 Web smoke 的三次尝试、30 秒单次请求上限；额外尝试按既有重试和同一个 gate 计数。不引入第二套 API 解析器来“验证”生产解析器，不新增 LLM 依赖或环境变量。`WEB_SEARCH_RUN_INTEGRATION` 是项目已有测试开关，不是新的运行时必需配置。

README 给出只运行此文件的显式命令和额度含义；不把两种免费额度描述为必然独立或保证不会产生费用。

- [x] **Step 4: Run test to verify it passes (and full suite)**

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/unit/test_live_baiduqianfan_validation.py tests/integration/test_live_baiduqianfan.py -q -rs
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
~~~

预期：离线 smoke 判定测试 PASS，新 live case SKIP。只有实施者明确选择以下调用且 key 已存在时才运行真实验收；本计划编写阶段不执行：

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=1 uv run pytest tests/integration/test_live_baiduqianfan.py -v -rs -rP
~~~

它会消耗两个分支的调用额度，重试可能增加物理请求。只记录实际观察到的分支成功/失败，不因未运行而报告 live PASS，不输出任何 key 值。手工 CLI 验证可使用现有 `keyword-search "Python 官方文档" --provider baiduqianfan`，但需由实施者在其已启用该 provider 的隔离/已授权运行环境中执行，不自动修改用户配置或启动共享 daemon。

- [x] **Step 5: Refactor (keep tests green)**

仅复用现有 smoke 模式和必要的私有 helper，避免给生产适配器增加测试专用状态/API。若重构，复跑 Step 4 的离线命令；无需为了测试重构反复消耗真实额度。

---

## 完成全部任务后的验收

### 定向回归与质量门

全部命令在目标工作树执行。采用当前 README 和 `.github/workflows/ci.yml` 的实际质量命令，mypy 包含 src、tests、scripts。下面是实施阶段要求，编写计划不运行这些功能验收。

~~~bash
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest tests/providers/web tests/providers/test_http_executor.py tests/providers/test_registry.py tests/unit/test_config_web_providers.py tests/runtime tests/orchestrators tests/doctor tests/observability tests/docs tests/cli/test_cli.py tests/acceptance -q
env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q
uv run ruff format --check src tests scripts
uv run ruff check .
uv run mypy src tests scripts
uv run python scripts/build_docs.py
uv build
~~~

预期：全部离线测试和质量/文档/构建门通过，live 默认跳过；没有未处理任务/资源警告。已有 CI 的依赖审计、打包 smoke 和 secret scan 继续执行，本补丁不改工作流或豁免这些门。若 build_docs 需要依赖，使用既有 docs 依赖组，不更新锁文件。生成目录遵循已有忽略/提交规则，不提交无关产物。

核对 diff 只包含任务文件，所有新 fixture 使用合成文本/保留域名，无真实认证字段或完整线上响应；不要为进行验证在共享环境执行全局 tool install、切换 Git 分支或终止用户进程。

### 可选真实 open-webSearch 检查

普通离线测试已经覆盖服务运行/停止和失败退出码。若已有受授权运行的 daemon，可在明确隔离的配置上验证一次 /health 成功；未运行场景用受控测试地址或既有离线 ConnectError 场景证明即可。不得为了制造 FAIL 停止用户的服务，也不把 /health 成功报告成内部引擎全部可用。

### 需求与任务映射

| 设计/测试要求 | 实现或验证任务 |
|---|---|
| D1 单 provider、固定双 API、一个并发租约 | 1、5、9、10 |
| D2 两个独立请求/响应 shape，原样 query | 1、2、3、4 |
| D3 真实 HTTP(S) URL、媒体后备、无卡片递归、共享去重 | 2、3、11 |
| D4 Web 片段与 Agentic 原文区分 | 2、3、11 |
| D5 物理尝试/重试共享 rate gate、日志职责 | 7、8、10、11 |
| D6 只对已启用 open_websearch 探测 liveness | 12、13、14 |
| D7 resolve 一次、保留 public doctor/CLI/报告 | 14 |
| D8 知乎无虚构 QPS、继承并发/重试 | 9 |
| D9 SecretValue、静态安全原因、不泄漏凭据 | 1、7、8、12、13、14、15 |
| BQ-01～04 请求、独立协议、确定顺序/并发 | 1、5 |
| BQ-05～10 媒体、raw_content、原样 query、空/坏条目 | 1、2、3、4、8 |
| BQ-11～12 去重、Search→Fetch 清洗安全流程 | 11 |
| BQ-13～16 部分成功、合法空成功、双失败/唯一终态 | 5、8、11 |
| BQ-17～18 业务/协议错误与官方 nullable 例外 | 1、3、4、7 |
| BQ-19～22 通用重试、QPS/额度/余额/未知 429 | 7、10 |
| BQ-23～24 分支/父任务取消、等待清理 | 6 |
| BQ-25 候选计数/安全分支 summary | 2、3、8 |
| RATE-01～03 实际 send、重试、四次请求窗口和等待预算 | 10 |
| RATE-04～05 不配则不限频、非法配置/Fetch 拒绝 | 9、10 |
| DOC-01～05 未配置/关闭离线、Search/Fetch/both 一次 | 14 |
| DOC-06 路径前缀与尾斜杠 | 12 |
| DOC-07 精确健康 envelope | 12、13 |
| DOC-08～11 连接/超时/HTTP/非法 JSON 失败 | 13、14 |
| DOC-12 整体配置失败绝不探测 | 14 |
| DOC-13～14 不泄漏秘密、无凭证 GET、禁重定向 | 12、13、14 |
| DOC-15 健康请求取消传播 | 13 |
| 文档完整、旧 provider/CLI/JSONL 回归 | 9、11、14、最终质量门 |
| 显式 opt-in live、两接口结果区分、默认 CI 离线 | 15 |

### 各 Task 实施记录（含阶段性结果）

每个 Task 完成后补充真实证据，不预填 PASS。有效 RED 需要记录具体失败断言；既有能力首次已绿记录为“回归，生产改动为零”。未运行的 live 必须写未运行及原因。

| Task | RED 或既有回归证据 | GREEN / 定向回归 | 全量测试/质量门 | 提交 |
|---|---|---|---|---|
| 1 | 双 POST 行为 RED：实际 0 次请求，预期 2 次；base/query 测试首次即绿，属既有回归 | 百度与知乎 87 passed | 全量 3559 passed / 18 skipped；受影响文件 Ruff/格式/mypy 通过 | 统一实施提交 |
| 2 | 媒体后备、坏记录隔离、ftp 校验、空白 URL 后备、网页文本保留均经历有效 RED/GREEN；其他边界首次绿记回归 | 百度/豆包/URL 定向 155 passed | 全量 3584 passed / 18 skipped；Ruff/格式/mypy 通过；一次 Antigravity 时序失败单测及全量复跑恢复 | 统一实施提交 |
| 3 | 缺媒体、可选/null 模态、仅卡片及三模态坏记录均有效 RED/GREEN；四项容器边界首次绿 | 百度 41 passed | 全量 3597 passed / 18 skipped；Ruff/格式/mypy 通过 | 统一实施提交 |
| 4 | 两分支 HTTP 200 InvalidArgument 曾 DID NOT RAISE；业务诊断和畸形码有效 RED/GREEN，坏 envelope 复用既有回归 | 百度+HTTP 238 passed | 全量 3620 passed / 18 skipped；Ruff/格式/mypy 通过 | 统一实施提交 |
| 5 | HTTP 403 丢成功兄弟及双失败误返回空均有效 RED/GREEN；并发首次绿 | 百度+知乎 163 passed；部分失败 12 组合、双失败 2 顺序通过 | 全量 3635 passed / 18 skipped；Ruff/格式/mypy 通过 | 统一实施提交 |
| 6 | 分支取消未等待兄弟 finally、重复 cancel 打断异步 finally 均有效 RED/GREEN；retry wait 取消首次绿 | 百度+知乎+HTTP 341 passed | 全量 3639 passed / 18 skipped；Ruff/格式/mypy 通过 | 统一实施提交 |
| 7 | quota/balance 两分支共 4 个有效 RED/GREEN；28 个既有 HTTP/retry 回归首次绿，6 个实测 Retry-After | 百度+HTTP+retry 312 passed | 全量 3671 passed / 18 skipped；Ruff/格式/mypy 通过；Antigravity 时序用例单测及全量复跑恢复 | 统一实施提交 |
| 8 | 7 个计数/日志边界回归首次绿；QPS 耗尽重试的已知错误码丢失有效 RED/GREEN | 百度+日志 575 passed | 全量 3679 passed / 18 skipped；Ruff/格式/mypy 通过 | 统一实施提交 |
| 9 | 注册尾部缺失及示例缺表有效 RED；初次执行时 4 个既存文件的编辑受工具保护阻塞 | 注册+配置 277 passed；首次含文档 304 passed / 6 failed；用户明确授权 Bash 后补齐 12 处修改，相关文档/配置/Runtime 48 passed | 原阶段全量 3691 passed / 7 failed / 18 skipped；阻塞现已解决，最终全量 3813 passed / 19 skipped；未降低测试断言 | 统一实施提交 |
| 10 | 5 个实际行为回归通过，生产改动零；超时用例曾误写异常类型后修正，不计功能 RED | Runtime+百度+HTTP 420 passed / 1 个既知旧注册列表 failed | 全量 3702 passed / 7 个既知阻塞 failed / 18 skipped；2 个测试文件 Ruff/格式/mypy 通过 | 统一实施提交 |
| 11 | 10 个真实百度适配器连接案例全部首次绿；筛选/URL 去重/query 身份/正文拒绝/媒体/终态/缓存清洗安全均为既有回归，生产改动零 | 编排+网关验收+provider 日志 316 passed | 全量 3712 passed / 7 个既知阻塞 failed / 18 skipped；3 个测试文件 Ruff/格式/mypy/diff 检查通过 | 统一实施提交 |
| 12 | 6 个未发 GET、3 个 URL 拼接、18 个非法地址及 1 个客户端隔离均有效 RED/GREEN；其余 2 个 URL 回归首次绿 | 新增健康探针 30 passed；doctor 48 passed | 全量 3742 passed / 7 个既知阻塞 failed / 18 skipped；2 个文件 Ruff/格式/mypy 通过 | 统一实施提交 |
| 13 | 13 个坏正文、12 个传输/HTTP 分类及总时限均有效 RED/GREEN；取消首次绿 | doctor+CLI 157 passed | 全量 3769 passed / 7 个既知阻塞 failed / 18 skipped；2 个文件 Ruff/格式/mypy 通过；总时限测试注入 0.02s 且 client 关闭 | 统一实施提交 |
| 14 | 4 个启用/URL 接线用例 await=0 RED→GREEN；解析一次/跳过/聚合/CLI/离线回归通过 | doctor+CLI+验收+文档首次 209 passed / 7 个文档 failed；README 两处条件联网合同现已补齐并通过回归 | 原阶段全量 3784 passed / 8 个写入阻塞 failed / 18 skipped；最终全量 3813 passed / 19 skipped；Ruff/格式/mypy 通过 | 统一实施提交 |
| 15 | 两支协议证明、合法空结果、安全输出、缺 key、logger 恢复和 Retry-After 总时限均有真实 RED/GREEN；首次 live 发现 cards=null，已窄化复现并修复 | 离线 12 passed / 1 opt-in skipped；真实 smoke 1 passed：Web HTTP 200、40/40 verified；Agentic HTTP 200、10/10 verified；各 1 次请求；README smoke 段落已补齐 | 最终全量 3813 passed / 19 skipped；Ruff/格式/mypy、文档构建、打包、隔离 CLI 检查通过；本次补文档未重复真实请求 | 统一实施提交 |

### 独立审查修复记录

- 百度适配器 Tasks 1–8 独立只读审查发现 3 项具体边界：调用方重复取消打断分支异步清理；超深 JSON 的 429 在检查器解码时丢失 Retry-After；媒体后备 URL 合法但文本字段错误被误计为 missing_url。
- 三项均有修复前行为失败证据，并通过新的独立实现代理修正；新增 6 个离线回归用例，定向 755 passed，受影响文件 Ruff/格式/mypy 通过。
- 修复后全量为 3697 passed / 7 个既知写入保护阻塞 failed / 18 skipped；未引入新失败。独立复核运行 6 个新增用例及 1 个原有单次取消对照，7 passed；结论 No findings。

- Doctor 独立审查发现损坏 gzip 正文导致报告中断，以及无效 Location 遮蔽非 200 状态两项边界；新实现代理以真实 HTTPX + MockTransport 公开 run_doctor 的 3 个 RED/GREEN 用例修复。
- 修复后 doctor/CLI/验收 186 passed，全量 3799 passed / 8 个既知阻塞 failed / 19 skipped；Ruff/格式/mypy/diff 检查通过。原审查者复跑 3 项，No findings。
- 独立日志审查确认正式 CLI 与项目 debug 配置未引入 HTTPX 原始 URL 日志；不对外部宿主主动启用第三方 INFO/DEBUG 作全局禁日志承诺。

### 验收标准（已通过）

- 一个 baiduqianfan 注册，无新增 fetch 能力或分支配置；示例环境名准确、默认关闭。
- 两 API 并行且部分失败可降级，合法零结果、双失败、取消和清理均有证明。
- 官方 nullable 媒体处理明确；只保留真实链接；Agentic 原文仍经过原有正文处理。
- 已知 quota/balance 429 不重试；QPS/未知 429 保留原 Retry-After；所有实际尝试共享配置 gate。
- doctor 能发现启用但未启动的 open-webSearch；未启用/整体配置无效时没有 HTTP；原 Unix socket 与退出码语义保留。
- 知乎只补真实说明，未捏造固定 QPS；现有相关测试通过。
- 所有离线门和可选 live 结果分别记录，凭据/原始响应不进入提交或不安全日志。
- 最终提交仅本任务改动；无版本发布或共享进程操作。

### 真实接口兼容修复与最终验收

- 首次真实 smoke 的 Web 分支正常；Agentic 返回 HTTP 200，但 cards=null 与原有严格列表校验冲突。只读结构诊断确认 `card_result` 是对象且显式含有 `cards: null`；未输出或保存原始响应、网页内容或密钥。
- 新实现代理先得到有效 RED，再仅扩展显式存在的 `card_result.cards = null` 为空卡片；缺失 cards、整个容器 null、错误类型及顶层业务错误码仍保持失败。官方成功示例的 cards 为数组；cards=null 单独标为本次线上观察，未将其伪称为官方文档约定。
- 新增合成 fixture 与 6 个回归用例；38 个边界、321 个 provider/executor/live 验证和 10 个编排用例通过。独立只读复审结论 No findings，无额外真实请求。
- 最终真实双接口 smoke：Web 与 Agentic 均 HTTP 200、各 1 次请求，分别 received/hits=40/40 与 10/10；pytest 1 passed in 3.14s。此结果证明本次双接口调用与解析成功，不等同于对所有账号或未来服务可用性的保证。

| 最终质量门 | 实际结果 |
|---|---|
| `uv run ruff format --check src tests scripts` | 通过，236 files already formatted |
| `uv run ruff check .` | 通过 |
| `uv run mypy src tests scripts` | 通过，233 source files |
| `uv audit --locked --preview-features audit-command` | 通过，23 个包无已知漏洞或不利项目状态 |
| 计划要求的定向离线回归 | 2718 passed |
| `env WEB_SEARCH_RUN_INTEGRATION=0 uv run pytest -q` | 3813 passed / 19 skipped，无失败 |
| `uv run python scripts/build_docs.py` | 通过 |
| `uv build` | 通过，生成 1.0.0 sdist 和 wheel；未变更版本 |
| 新 wheel 的隔离 CLI `--help` 与 `start --help` | 均通过；未安装到全局或启动 daemon |

### 写入阻塞的解决与最终复核

- 原先 `config.example.toml`、`README.md`、`docs/provider-rate-limits.md`、`tests/runtime/test_runtime_assembly.py` 的专用编辑调用被 LocalHost secret-looking guard 拒绝。用户随后明确授权直接使用 Bash 写入；重新加载适用指令、确认目标工作树和 Bash full 配置后，由新的实现代理完成这四个文件的 12 处精确修改。
- 写入使用最新 SHA-256、唯一旧文本匹配、符号链接检查及写前并发复核，并保留原文件模式和 inode。反向还原这 12 处替换后得到各原文件 SHA，证明其余字节未变；没有从脱敏读取副本重建整份文件。
- 四文件定向验收为 48 passed，原有 8 个失败全部消除。独立复核另指出 README 的媒体说明应包含保留的标题和摘要；新实现代理只改该句，文档回归 27 passed，复审 No findings。
- 最终全量为 3813 passed / 19 skipped，计划要求的定向回归为 2718 passed。全量测试后的最后一处修改仅为上述 README 表述，已另跑 27 项文档回归，并重建文档与发行包、通过两个隔离 CLI 检查。生产实现及测试代码未再改变。
- 15 项任务均已完成；本记录随统一实施提交保存，具体提交标识以本分支 Git 历史为准。未推送、创建 PR 或发布，未改变版本号、用户实际配置、凭据、共享进程或其他工作树。
