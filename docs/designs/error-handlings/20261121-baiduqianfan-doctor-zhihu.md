# Error Handling: 百度千帆双分支搜索、open-webSearch Doctor、知乎限流说明

- 日期：2026-11-21
- 状态：错误处理草案，待用户确认；设计阶段不修改业务代码
- 基线：e712f10 (v1.0.0)
- 关联架构：[20261121-baiduqianfan-doctor-zhihu.md](../architectures/20261121-baiduqianfan-doctor-zhihu.md)
- 目标：**最小侵入**。优先复用 ZhihuAdapter、HttpJsonExecutor、ProviderResponseFailure、CandidateCounts、DoctorCheck 和现有日志机制。

## 1. 责任边界

1. **HTTP 执行器**：负责请求、传输失败、HTTP 状态、Retry-After、统一退避、每次尝试的限流和 HTTP 日志；不引入新的公共重试策略。
2. **BaiduQianfanAdapter**：负责两个分支的响应封装、业务错误识别、候选条目逐个检查、部分失败汇总；不修改 SearchOrchestrator。
3. **SearchOrchestrator**：仍负责一个逻辑提供商的终态、URL 标准化和最终去重、正文评估；即使双分支产生两次 HTTP 请求，也只对 baiduqianfan 记录一个 provider_completed/provider_failed 终态。
4. **doctor**：仅检查被启用的 open-webSearch 的 liveness；使用 DoctorCheck(OK/FAIL)，与网关 daemon 的 Unix Socket 检查分别报告，不影响运行时搜索调度。
5. **知乎配置**：仅补充解释，不新增任何 Runtime 错误分支或 rate-limit 规则。

## 2. 百度双分支状态矩阵

| Web Search | Agentic Search | 适配器结果 | 日志 |
|---|---|---|---|
| 有效响应、存在 URL | 有效响应、存在 URL | 按固定分支顺序拼接有效命中；原编排器继续去重 | 各分支候选汇总；逻辑提供商完成一次 |
| 有效空结果 | 有效空结果 | 返回 []，是成功而非失败 | 每个分支的接收/接受数量为 0 |
| 成功（含 []） | HTTP/协议/业务失败 | 保留成功分支结果 | 一次 provider_partial_failure，固定标记 agentic_search |
| HTTP/协议/业务失败 | 成功（含 []） | 同上 | 一次 provider_partial_failure，固定标记 web_search |
| 两者都失败 | 两者都失败 | raise 现有 ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED)，保留一个安全的代表性原因 | 两个 HTTP 层记录允许存在；逻辑 provider_failed 仅一次 |
| 成功封装 + 部分坏条目 | 任意成功封装 | 只拒绝坏条目，保留合法结果 | CandidateCounts 拒绝原因汇总 |
| 任意分支执行中遭取消 | 任意状态 | 传播 asyncio.CancelledError 并清理未结束分支，不把取消视为可恢复失败 | 沿用现有取消边界 |

**成功的判定**：响应状态与结构满足官方契约，并正确包含允许的结果类别；即使所有合法候选被过滤或只有没有 URL 的结构化卡片，也允许结果为空。完整响应的必需形状异常时，不能伪装成“成功空列表”。一个条目损坏不影响同一列表内其他合法条目。

**执行顺序**：参考 ZhihuAdapter.search 的任务建立、gather 和取消清理逻辑。每分支各自完成现有 HTTP RetryPolicy；不得在整个 baiduqianfan.search 上再包裹一层重试，避免重复消耗额度。

## 3. HTTP 与千帆业务错误分类

| 上游场景 | 可重试 | 归属及行为 |
|---|---|---|
| DNS/TCP/连接断开/超时 | 是，遵守现有尝试次数 | HttpJsonExecutor 的 transport/timeout 与 terminal logging |
| HTTP 408、5xx | 是 | 现有全局退避（含有效 Retry-After） |
| HTTP 429，code=RATE_LIMIT_SEARCH_QPS | 是 | 现有 HTTP 状态重试；按 RateGate 计算物理请求次数 |
| HTTP 429，code=QUOTA_USER_DAILY_FREE | 否 | 通过百度分支现有 response_inspector 类型接口返回 ProviderResponseFailure(retryable=False)，避免对已耗尽免费额度重复请求 |
| HTTP 429，code=BILLING_INSUFFICIENT_BALANCE | 否 | 同上，不把欠费判断成需要退避的瞬时流控 |
| HTTP 429，未识别/无法读取具体 code | 继续沿用现有重试 | 不凭猜测将所有 429 改为永久失败 |
| HTTP 400/401/402/403/404/422 | 否 | 现有非可重试 HTTP 状态逻辑；鉴权/权限/付费提示仅来自安全分类，不打印上游原始描述 |
| HTTP 200，code=InvalidArgument 或明确非成功业务错误 | 否 | 百度分支响应检查器识别为 ProviderResponseFailure(retryable=False)，避免将其当成“空结果” |
| HTTP 200，code=InternalError | 视官方可恢复性和现有检查器策略决定 | 不新增全局重试；只在确认属于短暂故障时标记可重试；否则按业务失败处理 |
| HTTP 200，顶层 code 类型异常或无法确认成功结构 | 否 | adapter invalid_envelope；不得把未知 code 当作成功 |
| HTTP 200，非 JSON / 非 object | 否 | 现有 decode / ProtocolFailure 或 adapter invalid_envelope |
| 超出本地 rate_limits 且等待超过请求预算 | 不发送当前请求 | 复用 rate_limit_wait_timeout，不额外绕过 RateGate |

**实现细节**：HTTP 执行器本身已在判断状态码之前调用 response_inspector。Baidu Adapter 应使用该现有钩子从有限且明确的官方 code 白名单中识别不应重试的 429，无须修改共享 executor 的 429 规则。针对 200 内业务错误使用相同机制（或适配器现有 parser 的 typed failure）并保持调用层失败语义。错误码记录经过安全诊断字段过滤，不写原始 message、request_id 或响应 body。

**免费额度**：每次并行搜索通常产生两次上游收费单位，各分支及其重试独立消耗额度。是否达到当天额度，以及账号是否开通后付费，不由本项目保证；不实现隐式日配额限制或付费回退。文档必须明确这一点。

官方依据：百度 Web Search API 在错误码表中区分 RATE_LIMIT_SEARCH_QPS、QUOTA_USER_DAILY_FREE、BILLING_INSUFFICIENT_BALANCE；Agentic API 的业务失败字段包含 code/message。参考：
- https://cloud.baidu.com/doc/qianfan-api/s/bmwbqlbad
- https://cloud.baidu.com/doc/qianfan-api/s/hmwbqleyk

## 4. 百度结果内容与异常

### 4.1 顶层 envelope

- **Web Search**：优先确认 HTTP 200 中没有错误 code；成功结果需遵守官方 references 数组形状，refs 为空数组属于合法零结果。缺失、类型错误不能直接变成零命中。
- **Agentic Search**：先识别可选 code/message 错误；正常成功响应至少可判断其网页、图片、视频结果容器或仅卡片容器。各已出现的列表字段若类型不正确，判为该分支协议异常；卡片结果没有 URL 时不强造。
- 响应的额外未知字段可忽略；不对任意嵌套层级进行启发式扫描，避免误提取敏感或不相关 URL。

### 4.2 单条候选

- 对 object 形状、所选 URL 字段、HTTP(S) URL 合法性和可选文字类型逐条检查；坏记录只增加 CandidateCounts 的既有 rejection reason。
- image/video 缺少标题摘要时，填充准确的简短媒体类型标签，使现有 SearchOrchestrator 不会因 empty_abstract 放弃有效 URL；内容正文必须留空。
- Agentic 网页原文只写入 raw_content；不把它标记为已清洗 content，也不把 Web Search 的 snippet/节选写为整页原文。
- 重复 URL 不在百度适配器内部新增第二套去重。继续交由 SearchOrchestrator/URLStore 现有路径处理。

### 4.3 候选日志

- 每个分支成功解析后复用 provider_candidates_summary：received、accepted、rejected、rejection_counts、stage=web_search/agentic_search。
- 只记录固定的阶段标识与有限标量（状态、错误码、安全类别、耗时、条目数量）。不记录查询全文、网页正文、图片/视频原始 URI（尤其带临时签名者）或未经审核的上游消息。
- branch 失败的 adapter-local provider_partial_failure 沿用知乎的 safe_failure_reason 与 diagnostic_fields_from_failure；provider_completed/provider_failed 属于编排器，适配器不重复发终态事件。

## 5. Doctor：仅已启用 open-webSearch 的健康检查

| 场景 | 结果 | 行为 |
|---|---|---|
| open_websearch 未配置，或 Search/Fetch 均为 false | 不新增健康检查记录 | 不创建网络客户端，不探测网络 |
| 整体配置无效、缺失或环境变量验证失败 | 现有配置 FAIL | 不尝试外部探测，保留其余目录/Socket 检查 |
| open_websearch 有至少一种能力为 true；GET /health 返回 HTTP 200，status=ok，data.daemon=running | 新增 [ok] open-webSearch daemon healthy | 不执行 POST /search，不检查底层引擎 |
| 连接拒绝、DNS 失败、网络不可达 | 新增 [fail] | 使用静态原因，例如 connection_failed，不把可能包含敏感 URL 的原始异常打印出来 |
| 连接超时或读超时 | 新增 [fail] | 单次探测建议总超时 2 秒；无重试、无自动启动 |
| HTTP 3xx/4xx/5xx 或连接到非目标健康服务 | 新增 [fail] | 不跟随重定向，不凭 TCP 开放就称为健康 |
| HTTP 200 但非法 JSON / 缺少 status、data.daemon / 值不符合官方契约 | 新增 [fail] | 分类为 invalid_health_response，不把上游 body 回显到诊断 |
| Gateway 自己的 Unix socket 缺失 | 继续现有 [info] daemon not running | 与 open-webSearch 不可达的 [fail] 独立展示 |
| 健康探测任务取消 | 传播取消 | 不隐藏取消，也不创造额外目录或网络重试 |

- 不扩展 DoctorReport、DoctorStatus、公共 CLI 命令或退出码约定：有任何 FAIL 则整体退出码为 1，全部 OK/INFO 则为 0。
- 固定和安全的输出建议：open-webSearch daemon healthy / unavailable (connection_failed) / unhealthy (status_code) / unhealthy (invalid_health_response)。不要直接插入原始 api_url（可能含 token 参数）、Exception 字符串或返回 payload。
- 仅通过已通过 resolve_config 的配置获取待测 api_url；不在探测前随意访问未知文件或启动进程。
- 该 GET 证明**daemon liveness**，不是搜索引擎 readiness；即便返回 healthy，底层引擎仍可能执行失败。
- 如代理或自定义 URL 的传输细节影响 GET 行为，只处理已启用自部署服务当前正常可用的 HTTP(S) URL，不新增跨站重定向、额外证书选项或兼容性回退。

官方依据：https://github.com/Aas-ee/open-webSearch/blob/main/docs/architecture/local-http-adapter.md

## 6. 知乎无固定窗口的解释

- 更新 config.example.toml 注释，不设置 rate_limits/min_interval_seconds；保留 default_max_concurrency 和已实现的并行双端点与 429 重试。
- 文档区分“账号有额度/服务会报告超频”和“官方公布所有账号共同适用的固定 QPS”两个概念；前者不能推出后者。
- 不新增任何回归风险较高的知乎代码修改。

## 7. 日志与测试验收约束

- HTTP attempt 由 executor 独立记录（含每次发送、重试、最终失败），provider 单次终态仅由 orchestrator 负责。
- HTTP 200 的明显错误业务 envelope 不能变成成功空结果；单条坏命中不得影响同级合法命中。
- 知道 code 时区分 429 QPS 与免费额度/欠费，未知 429 保持既有重试；保留重试次数上限。
- open-webSearch 停止时运行 doctor 必须有 FAIL 且退出码 1；未启用时必须继续无网络。
- 所有错误路径均不输出凭据、原始请求体、未审查的上游 message、带签名的 URL。

## Review Gate

待用户审阅本文件后再编写第三部分测试设计；三份设计文档均获批准后再按 brainstorming 流程提交设计文档。当前不修改业务代码。
