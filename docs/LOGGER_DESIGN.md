# 日志设计

## 开关与目录

```yaml
logging:
  enabled: true
  detailed: false
```

`enabled` 控制日志写入；`detailed` 默认关闭，只控制详细排障资料。配置变化作用于后续执行，不批量删除历史文件。API 任务目录是 `data/api/<task_id>/`；Web 是 `data/web/<tenant_id>/<task_id>/`。MCP 与 worker 必须访问同一持久化数据目录。

下表中“关闭”表示 `enabled=true, detailed=false`，“开启”表示 `enabled=true, detailed=true`。

| 目录或文件 | 关闭 | 开启 | 内容与结构 |
| --- | --- | --- | --- |
| `task.json` | 保留 | 保留 | 生命周期、要点进度、输出与实际日志路径。新增 `logs.logical_calls_dir`；关闭时详细日志路径和 `state.docxindex_dir` 为 `null`。 |
| `input/` | 按 `api.keep_input` 保留 | 同左 | 业务输入，与详细日志开关无关。 |
| `output/` | 保留现有规则 | 同左 | 原合同批注输出；上传后的留存仍按 `api.keep_output` 控制。 |
| `logs/events.log` | 保留，简短摘要 | 保留 | 任务状态、主要阶段耗时、最终业务统计及最终错误。关闭时不写 traceback、模型正文和重复 token 字段，错误文本最多 200 字符。 |
| `logs/logical_calls/` | 保留 | 保留 | 每个逻辑模型调用一份 prettified JSON；包含逐次 attempts、预算、耗时、错误及已知用量，不包含完整请求或响应正文。 |
| `logs/calls/` | 不生成 | 保留并增加字段 | 每次尝试的完整请求、响应或异常。新增逻辑 ID、类别、attempt、is_retry 和三个 token 字段。 |
| `logs/trace.json` | 不生成 | 保留 | workflow、criterion、LLM、MCP 执行树；调用记录通过 call_id/call_path 关联。 |
| `logs/route_events/` | 不生成 | 保留，调整字段 | 路由规划、分步检索、扫描批次、回退及最终选中节点。 |
| `logs/conversations/` | 不生成 | 保留现有行为 | 新任务的完整模型正文已放在 calls，不再重复保存 conversations/*.json；agent_steps.jsonl 只在详细模式写入。 |
| `logs/mcp.log` | 不生成 | 保留 | MCP 连接、工具调用与错误。 |
| `logs/review_outputs.json` | 不生成 | 保留 | 审查结果调试快照。 |
| `logs/annotation_events.json` | 不生成 | 保留 | 批注匹配、跳过和定位细节；批注统计直接返回工作流，不依赖此文件。 |
| `state/plan.json` | 保留 | 保留 | 完整要点计划与恢复版本。 |
| `state/criteria/*.output.json` | 保留 | 保留 | 完整要点恢复结果和逐轮反思历史；不再持久化混入证据文本估算量的 tokens 字段。 |
| `state/criteria/*.input.json`、`*.error.json` | 不生成 | 保留 | 要点输入及多轮业务执行错误详情。 |
| `state/docxindex/` | 不生成 | 保留 | 内存索引的调试导出；关闭时检索继续使用内存索引。 |
| `runtime_input/` | 任务结束清理 | 同左 | 临时业务输入与请求级模型配置。 |

`enabled=false` 时不写上述日志及详细排障资料，但 `task.json`、业务输入输出、plan 和 criterion output 恢复状态仍按业务规则工作。

不生成 `model_usage_summary.json`，也不在其他文件添加 token 平均值、中位值、最小值或最大值汇总。

## 逻辑调用

同一次模型结果的网络重试、JSON/schema 修复、扫描批次输出修复、层级批次输出修复及摘要压缩修复，共用一个 logical_call_id、累计名额等待预算和发送次数。logical_call_id 用于关联和统计，不表示多次 HTTP 请求共用一个 60 秒倒计时。每次实际 HTTP 请求取得名额后独立计时，默认 60 秒；请求超时、网络重试和 schema 修复共同使用最多 6 次发送（`workflow.model_call_max_retries=5`）。合同执行截止时间始终有效。不同独立批次、不同摘要节点、正常业务修订、反思反馈修订及工具结果后的继续生成分别建立逻辑调用。工具执行期间不继承 Agent 的逻辑调用，工具内部模型请求独立记录。

失败要点按 `workflow.criterion_retry_max_attempts` 重新执行，默认 3 包含首次，最多重跑该要点两次。新一轮要点执行产生新的逻辑调用及模型预算，仍受原合同截止时间和相同的全局并发上限控制。已经保存且版本兼容的成功要点直接复用。详细模式下，`state/criteria/<ID>.error.json` 累积失败尝试，最终 output 的 `attempt` 记录成功属于哪次要点执行；单个逻辑调用的 `retry_count` 不能代替整要点重试统计。模型网络/超时错误不能在同一次要点执行中通过批量摘要转为逐节点摘要来重置预算。

确定性的证据、锚点、覆盖或路由反馈属于业务修订，须在前次回答通过 schema 后进入；内部每次检查最多请求 2 次新结论，初始结论和反思后结论可以分别触发。它和传输/schema 重试分开记录；60 秒作用于每次实际 HTTP 请求，合同仍受整任务硬时限约束。

文件名为 `<source>__<stage>__<logical_call_id>.json`，例如 `docxindex_H12__scan-step1-batch2__a524...json`。保留下划线，其他不适合文件名的字符替换为连字符；每段最多 64 字符，完整 source/stage 仍在 JSON 中。每次请求前先写 pending；调用结束原子更新同一文件。

下面展示主要汇总字段；实际文件还包含逐次 `attempts`。

```json
{
  "logical_call_id": "a524...",
  "category": "docxindex.retrieval.llm",
  "source": "docxindex_H12",
  "stage": "scan-step1-batch2",
  "reason": null,
  "model": "qwen3.6-35b-a3b",
  "started_at": "2026-10-12T12:00:00.000+08:00",
  "finished_at": "2026-10-12T12:00:02.000+08:00",
  "status": "succeeded",
  "send_count": 1,
  "retry_count": 0,
  "input_tokens": 1000,
  "output_tokens": 200,
  "input_output_tokens": 1200,
  "known_input_tokens": 1000,
  "known_output_tokens": 200,
  "input_usage_complete": true,
  "output_usage_complete": true,
  "usage_complete": true,
  "error": null
}
```

`send_count` 是实际发送数；`retry_count` 是首次发送之外的实际发送数。等待并发许可失败不增加发送次数。`input_tokens/output_tokens/input_output_tokens` 保留最终成功结果的 usage 语义，不累加此前被替代的响应；最终失败时这三个汇总字段为 null。embedding 不存在输出 token，`output_tokens` 为 null，`input_output_tokens` 等于已知 `input_tokens`。

`known_input_tokens/known_output_tokens` 累加所有已发送尝试中已收到的 usage，包括最终校验失败或释放 Redis 失败的响应。未知用量不估算；已知总计为 0 不表示没有消耗，必须同时检查 `input_usage_complete/output_usage_complete/usage_complete`。LLM 缺少输出 usage 时不能标为完整；embedding 不要求输出 usage。统计成功任务的模型用量和重试成本时，应使用这些已知总计及完整性字段，不能只读最终结果的三个 token 字段。

普通日志和详细日志都保留 `attempts` 的基础信息：`sent/send_number`、HTTP 状态、模型类型、信号量 key、预算、剩余时间、失败阶段、重试原因和等待/请求/退避/总耗时。只有详细模式记录完整请求和响应正文。

逐次发送的请求耗时与逻辑调用累计耗时必须分开读取：一次逻辑调用可能先超时 60 秒，再由修复或重试请求成功，累计请求时间可以超过 60 秒；不能据此认为某一次 HTTP 超过配置时限。

| `attempts[].timing` 字段 | 当前含义 |
| --- | --- |
| `request_seconds` | 当前这次发送的实际 HTTP 耗时 |
| `model_elapsed_seconds` | 同一逻辑调用截至当前的累计 HTTP 请求耗时，不含名额等待、校验、退避和清理 |
| `remaining_seconds` | 当前或最后一次请求剩余时间，受合同截止时间限制；超时后为 0，下一次发送重新获得请求时限 |
| `waiting_total_seconds` / `wait_remaining_seconds` | 同一逻辑调用累计等待名额的耗时 / 剩余等待预算 |
| `total_seconds` | 包含此前尝试及名额等待的逻辑调用墙钟耗时，不是单次 HTTP 耗时 |

等待超时记录为 `semaphore_wait`，整任务到期记录为 `task_deadline`；它们均不触发请求重试。2026-10-19 本次修复前的历史 `model_elapsed_seconds` 包含除名额等待外的累计执行耗时，不能与新版累计 HTTP 耗时混用；历史文件不回写。

失败 error 仅含 type、http_status、message（最多 200 字符）。本地请求超时而未收到响应时 http_status=null；服务返回 HTTP 错误时记录实际状态；收到 HTTP 成功响应但校验失败仍记为逻辑失败。成功 HTTP 状态也从实际响应读取，无法取得时不默认填写 200。

类别为：

- `agent`
- `docxindex.indexing.llm`
- `docxindex.retrieval.llm`
- `docxindex.indexing.embedding`
- `docxindex.retrieval.embedding`
- `docxindex.retrieval.reranker`

source 使用 SubAgent_H12、Reflector_H12 等角色加要点名称；Planner、Summarizer 是任务级角色。索引 LLM 和 Embedding 分别使用 docxindex_indexing_llm、docxindex_indexing_embedding；检索使用 docxindex_H12。归类按调用上下文和入口定义。DocxIndex 的 rerank 使用 `rerank` 中配置的独立模型和 `reranker` 全局名额，支持 Higress Qwen、BGE、DashScope Qwen 协议，不再使用检索 LLM 排序。

stage 标识请求用途：route-plan、scan-step1-batch2、structure-step1-part2、vector-query-step1、rerank-pass1-batch2、index-summary-batch1、index-embedding-batch1、index-hierarchy-节点和锚点范围等。SubAgent 使用 review-initial、review-initial-output-repair1、review-reflection-revision1；工具返回后的请求追加 tool-result1。Reflector 使用 reflection-round1。step 是检索动作，不是模型请求数；一个 step 的多个分批分别记录。

reason 正常请求为 null；内容校验修订时写实际校验错误摘要，反思修订时写反馈摘要，工具后续请求写继续处理工具结果的说明。JSON/schema 重试在原逻辑调用的 reason 中补充校验原因。reason 最多 200 字符，不保存完整输入输出正文。

详细 calls 文件名为 `<source>__<stage>__<UTC时间>__<call_id>.json`，source/stage 与逻辑调用一致，并保存 criterion_id、reason、logical_call_id、category、attempt（从 1 开始）、is_retry、task_id、worker_pid 和 token 字段。未收到响应时 usage 未知；已收到响应后，即使输出校验或 Redis 释放失败，也保留实际 HTTP 状态及收到的 usage。`http_status` 表示收到的 HTTP 响应，`error.http_status` 表示错误所属响应；Redis 释放错误没有 HTTP 状态，不能用它覆盖已经收到的 HTTP 200。失败阶段用 `failure_stage` 区分。

## 检索与 route_events

语义步骤先取结构候选；结构结果不足时按现有配置补充向量候选，按 node_id 去重。保留 structure_candidate_limit 和向量补足上限，但不在每步重排或按最终数量截取。所有步骤结果合并、按 node_id 去重并合并 sources 后，统一按同一审查问题重排，再截取 max_selected_nodes。完整区域、全文扫描及全部标题路线保持完整覆盖，跳过最终 TopK 截取。

超出重排输入预算时，各批次先重排保留最多最终 K 个候选，合并幸存候选后再次重排；每个原候选均进入其批次。预算按问题和候选信息估算。若连单个候选都放不下，或分批后不能缩小池子，明确失败并提示调整预算，不能按原顺序静默丢弃候选。模型必须对本批全部候选各评分一次；遗漏、重复或未知节点触发 schema 重试，不回填未经评分的顺序结果。关闭 rerank 时不调用模型，按去重后的合并顺序截取。

每个 route_event 的外层字段仍为 operation_id、criterion_id、stage、event、at、payload。stage 标识实际步骤，如 route.1.region；最终结果为 route.result。

semantic_retrieval 的 payload 只保留：scope、structure_candidate_count、vector_enabled、vector_triggered、vector_added_count、candidate_count_before_rerank。最后一个是单步补充并去重后的候选数。

result 的 payload 为：candidate_count_before_rerank（跨步骤去重后的总池）、rerank_enabled、rerank_triggered、selected_node_count、selected_node_ids、source_counts、content_parts、has_more。

不记录 vector_trigger_reason、vector_probe_limit、vector_probe_count、vector_duplicate_count、single_candidate_rerank_skipped。plan、step、fallback、scan 批次事件维持现有职责。

## 配置与恢复

索引参数、路由方法描述和备用 llamaindex 参数有代码默认值，允许可选覆盖。示例配置显式列出 rerank provider 名单、subagent 工具名单和 worker 禁止预取项，便于核对实际部署。模型名称及地址保留各部署自己的值；BGE embedding 和 reranker 的地址与 payload 约束见[配置说明](CONCURRENCY_TIMEOUT_CONFIGURATION.md#61-bge-embedding-和-reranker-怎么配置)。

`model_semaphore.llm/embedding/reranker` 各自配置 `max_concurrent_requests`、`redis_url`、`key`、`lease_seconds`、`poll_interval_seconds`。逐类型 `enabled`、`wait_timeout_seconds` 和旧共享 `limits` 格式已移除，保留旧字段会在启动时明确报迁移错误。等待名额累计消耗 `workflow.model_wait_timeout_seconds`（默认 180 秒）；取得名额后，每次实际 HTTP 请求独立使用 `workflow.model_call_timeout_seconds`（默认 60 秒），超时后释放名额，按剩余发送次数重试。退避和输出校验不扣后续请求的 60 秒，但仍受合同截止时间控制。网络、请求超时和 schema 修复共同消耗 `workflow.model_call_max_retries`，不各自叠加。lease 在取得名额时开始，请求结束主动释放；退出清理最多 2 秒，worker 强制终止或 Redis 不可用时依靠租约回收。完整配置和任务硬时限见[配置说明](CONCURRENCY_TIMEOUT_CONFIGURATION.md)。

恢复契约版本为 7；旧版 plan/output 不作为新版执行的恢复结果，历史文件保留。新写入的 criterion output 删除 tokens；工作流内存结果和返回值中的 token_stats 保留兼容，不用于准确模型 usage 统计。准确用量读取 logical_calls。旧任务文件不改名、不补写字段；旧 Reflector 日志缺少要点归属，不能可靠补回 H 编号。

具体日志路径、开关和恢复实现分别在 endpoints/review/task_store.py、loggers/call_log.py、endpoints/review/review_state.py。详细 calls 的文件名说明见 [任务日志文件名阅读指南](TASK_LOG_FILENAME_GUIDE.md)。

## 汇总建议与失败回退日志（2026-10-18）

汇总前保存已完成审查结果，去重应用后更新最终审查日志。去重候选数、接受数、跳过数及逐组原因记录到汇总 trace 和 `summary.deduplication`；错误去重建议不删除原意见。汇总调用或输出校验耗尽时记录原始错误及 `summary.status=fallback`，只生成程序基础总览，报告和 API 明确披露。未知程序或文件错误不适用这一回退。所有修复共用最多 6 次发送及累计等待预算，每次实际请求独立使用 60 秒，不通过基础总览追加模型请求。本次修复前的历史记录仍按当时累计执行预算解释。
