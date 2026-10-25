# 模型请求、并发与超时配置说明

更新日期：2026-10-26。本文对应本轮实现。最终 r5 镜像的同批混合六合同、非流式专项及自动化回归结果见[实施与验收记录](C:/Users/lenovo/Desktop/contract_agent/docs/MODEL_REQUEST_CONTROL_IMPLEMENTATION.md)。

## 1. 完整链路

Web/API 保存合同 → Celery 发布消息 → Redis broker → 三个 worker（各 concurrency=1、prefork）→ 建索引/检索/Agent/汇总 → 生成原合同批注报告 → 保存 API 与任务最终状态。

每次实际模型请求：准备最终输入 → 选择离线 tokenizer 并计数 → 必要时整理 Agent 历史或切片 → 检查该类型 max_input_tokens → 进入 Redis 共享 FIFO → 同时满足并发和滚动 RPM → 取得并发名额并开始本次请求计时 → 接收结束或中止 → 主动释放名额 → 校验响应结构/业务数据 → 按剩余发送预算决定是否重试。

并发和 RPM 跨三个 worker 共享，但只共享相同 Redis 地址和资源 key 的部署。其他进程直接请求同一个模型而未接入这些 key，不会被本项目自动计入。

本地配置和 deploy/config.yaml 当前均使用 broker DB 2、模型控制 DB 0；config.example.yaml 使用 broker DB 0、模型控制 DB 2。DB 编号来自各自 URL，不具有固定业务含义，也不是 TCP 端口。它们仍是独立用途，迁移没有交换各环境的编号。

## 2. 三种模型与 tokenizer

| 字段 | 含义 | null / 省略 |
| --- | --- | --- |
| llm.base_url / name | LLM 接口及实际发送的模型名称 | 必须配置 |
| llm.enable_thinking | 传给现有接口的思考开关 | 沿用接口选项语义 |
| llm.stream | 所有 LLM 的网络流式开关；应用仍向 Agent 返回合并后的响应 | 省略为 false，null 非法 |
| llm.max_input_tokens | 单次完整消息、工具定义和模板的输入上限；由原 max_prompt_tokens 数值迁移 | 不限，仍计数 |
| llm.max_tool_calls | 一个 Agent 会话的工具调用次数上限 | 不限 |
| llm.temperature / top_p / seed | 采样行为参数；不是资源上限 | 必须配置，不表示无限 |
| embedding.base_url / name | OpenAI 兼容 embeddings 接口与模型名称 | 必须配置 |
| embedding.max_input_tokens | 每条实际 embedding 输入的上限；批次总和不是单条上限 | 不限，仍计数 |
| embedding.chunk_overlap_tokens | 应用端相邻向量切片重叠量；不传给服务 | 省略为 32，null 非法；0 表示不重叠 |
| rerank.base_url / name | 独立 reranker 接口完整地址和模型名称 | 必须配置 |
| rerank.max_input_tokens | 每组 query、单个候选片段和输入包装的上限；不限制整批累计 token | 不限，仍计数 |
| rerank.instruct | 现有接口所需任务说明；BGE 请求格式保持原状 | 按配置/现有协议 |
| rerank.provider_model_names | 现有请求格式的服务名称映射，与 tokenizer 映射独立 | 沿用代码列出的现有协议名称 |
| tokenizers | tokenizer ID → 多个模型服务名称；仅影响计数，不改发送的 model | 必须显式配置，并包含 other |

移除 llm.max_context_tokens、llm.max_prompt_tokens、llm.max_result_tokens；旧字段报迁移错误。不再检查“模型官方总容量”，不从资源 manifest 或 tokenizer.model_max_length 推导额外上限，不再用字符数截断工具结果，也不新增生成输出上限。配置 40000 就按 40000 检查。

名称匹配先去除首尾空白，再忽略大小写。同一名称不能属于两个 tokenizer。示例：

```yaml
tokenizers:
  qwen3.6-35b-a3b: [Qwen3.6-35B-A3B]
  qwen3-embedding: [Qwen-Embedding-8B, Qwen3-Embedding-8B, qwen3-embedding]
  qwen3-reranker: [Qwen-Reranker-8B, Qwen3-Reranker-8B, qwen3-reranker]
  bge-m3: [bge-m3]
  bge-reranker-v2-m3: [bge-reranker-v2-m3]
  deepseek-v4-flash-0731: [DeepSeek-V4-Flash-0731]
  cl100k_base: [other]
```

未匹配名称仅使用显式 other 回退；日志记录 estimated 和匹配来源。映射缺失、资源缺失、校验值错误会明确失败，不静默换 tokenizer。不能断言其他 DeepSeek 版本共用此版本的 tokenizer。

当前本地 text-embedding-v4 和 qwen3-rerank 未映射到原版资源，按 other 使用 cl100k_base，其输入计数为估算。

六套原版资源位于 resources/tokenizers，固定 revision 与 SHA256；加 cl100k_base 约 101.3 MiB，不包含模型权重。运行时不下载。构建镜像与 protect/build.ps1 在断网环境执行 prepare_tokenizers.py --verify。

## 3. 输入控制与历史

Agent 超限时在发送副本中优先删除较旧的完整消息组，保留 system、原始任务、最新请求、当前工具交互及未完成工具事务。工具请求和对应 tool 结果整体保留或整体移除。原历史和原文日志不改；没有机械截断字符串或让模型自行缩短 HTTP 输入。

如果服务以 finish_reason=length 截断回复，且这条回复未通过 JSON/schema 校验，后续修复输入会排除这条完整的无效 assistant 消息，保留原任务、证据及校验反馈。完整原始回复、失败原因和 token 消耗仍保存在历史与调用日志中；input_preparation 记录排除的消息索引及原因。普通完整回复的校验修复继续携带上次输出，不删除工具事务，也不新增输出 token 上限。

Embedding 和 reranker 对原 node 生成临时切片，按最高切片相似度/重排分数给原 node 排序；返回完整原文和原 node_id。embedding 有可配置重叠，reranker 不新增重叠配置。切片计数包含对应模型包装与 reranker query。无法容纳 query 或必要消息时，在本地拒绝发送，记录 input_validation、sent=false；不消费 RPM，不以未改变输入原样重试。

配置预算预期有风险时仅 WARN，继续启动；最终实际请求超过上限才拒绝。监控 retrieval 的 input_tokens、output_tokens、scan_batch_tokens、摘要批次、层级识别预算与 LLM 上限的关系；监控 embedding overlap 与输入上限。节点数、工具次数、复核轮数影响数量和历史增长，不能直接乘成一个确定 token 上限。扫描多页合并后按真实总输入计数，不把单页预算当总返回上限。

## 4. 全局并发、RPM 和租约

| model_semaphore.<llm / embedding / reranker> 字段 | 含义 | null / 省略 |
| --- | --- | --- |
| max_concurrent_requests | 此类型正在实际请求的总数，跨三个 worker 共享 | 不限制，仍记录活动请求 |
| max_requests_per_minute | 此刻前 60 秒滚动窗口内实际发送总数，重试逐次计入 | 不限制，仍计数 |
| redis_url | 模型控制 Redis 地址和 DB | 沿用默认 redis://redis:6379/2 |
| key | 此类型的共享资源键；三类必须不同 | 默认 contract_agent:model:<类型>:semaphore |
| lease_seconds | 取得名额后的有限异常回收租约；正常结束主动释放，运行中自动续期 | 默认 600；必须为正数 |
| poll_interval_seconds | 请求排队的内部轮询间隔 | 默认 0.2；必须为正数 |

上述 Redis 地址、key、lease、poll 仅在省略时取默认值，显式 null 非法。

本地显式并发为 LLM 5、embedding 3、reranker 10；LLM RPM 30，其余 RPM null。部署原有显式并发值保留，不用本地值覆盖内网。

Redis TIME 和 Lua 原子处理 FIFO、活动名额及滚动窗口。等待 RPM 不占并发名额，等待并发不提前长期保留 RPM 资格。准入后立即发起请求：发送前取消撤销预留；发起后即使连接失败也保留该次 RPM 记录。流式片段不另算请求。请求结束释放并发名额，RPM 记录保留到超过 60 秒。

租约从取得名额开始，不覆盖合同全程或名额等待。每 lease_seconds / 3 续期；续期失败中止请求。无需让 lease 覆盖所有重试总时长。崩溃等待者由有限内部心跳清理，避免无限等待时死队首一直阻塞。

辅助 key 包括 :waiting、:deadlines、:sequence、:policy、:rpm。不同 worker 使用不一致策略时拒绝混用活动资源，不静默越过上限。

## 5. 超时、重试与 Celery

| 配置 | 本地 / 部署显式值 | 作用及包含关系 |
| --- | --- | --- |
| workflow.model_wait_timeout_seconds | 180 / 180 | 同一次逻辑模型操作累计等待并发与 RPM 的预算，包括后续重试再次等待 |
| workflow.model_call_timeout_seconds | 60 / 60 | 每次实际请求重新计时；非流式为总时长，流式为连续无新增内容时长 |
| workflow.model_call_max_retries | 5 / 5 | 初次之外最多再发送 5 次；传输重试与 JSON/schema 修复共用发送预算 |
| workflow.criterion_retry_max_attempts | 3 / 2 | 整条要点总尝试次数；独立于一次逻辑模型操作；未变化的本地输入超限不重试 |
| workflow.max_reflection_rounds | 10 / 10 | 要点结论的语义复核与修改轮数，不是 HTTP 重试次数 |
| workflow.max_orchestrator_concurrency | 2 / 2 | 一份合同内部同时推进的要点数；最终 HTTP 仍受全局模型限制 |
| workflow.subagent_allowed_tools | contract_search | SubAgent 能调用的工具清单，空列表表示无工具 |
| queue.broker_url | DB 2 / DB 2 | Celery 任务消息地址，不负责模型并发 |
| queue.pending_timeout_seconds | 60 / 60 | 仍处于 pending、尚未进入发布流程的任务监督 |
| queue.queued_timeout_seconds | 7200 / 7200 | 保存发布身份并进入 queued 后至 worker 接手的监督，包括发布调用尚未返回的区间；不是模型执行超时 |
| queue.task_time_limit_seconds | 1800 / 7200 | worker 接手后的整份合同硬时限，包含模型排队、实际请求、重试、检索和报告 |
| queue.watchdog_interval_seconds | 10 / 10 | 检查队列任务的间隔；行为参数，必须为正数 |
| queue.worker_disable_prefetch | true / true | 保留三 worker 各领取一份执行的部署约定 |

上表的次数和时间上限填 null 或省略即取消该项限制；watchdog 间隔和工具列表等行为参数不适用无限语义。数值 0 不等于无限；仅 model_call_max_retries=0 表示不重新发送。无限模型等待仍受有限合同硬时限约束。

关系：模型名额等待与实际 HTTP 计时相互独立；两者都处于 worker 执行总时长之内。非流式当次有效时间是请求时限与合同剩余时长的较小值；流式下一次内容等待也是两者较小值。没有“请求必须小于 lease”的静态关系，因为名额自动续期。

流式新增非空正文、推理内容或工具参数刷新空闲计时；心跳、空白、角色和 usage 不刷新。有 finish_reason，或明确 [DONE] 加完整可用响应，才能进入后续数据校验；仅 [DONE] 无内容、无结束依据的 EOF 均失败。全部 finish_reason 到齐后冻结模型请求计时，内部最多再等 0.1 秒接收可选 usage；任务总时限仍约束这段等待，缺少 usage 不使已完成响应失败。明确传输结束后仍需解析 JSON、执行 Pydantic 结构/规则校验，并在需要时执行 Reflector；这些不是同一项校验。

Celery 隐含设置保持显露：三个 worker、每 worker concurrency=1、prefork；task_soft_time_limit 禁用；worker_prefetch_multiplier=1；task_acks_late=false；Redis visibility_timeout=3600。硬时限终止执行子进程，Celery 补充子进程维持 worker 池。未增加软时限或逐模型 enabled 开关。

## 6. 检索与索引配置

这些预算组织检索内容，最终请求仍须通过三个模型各自输入检查。数量和最大批次上限可 null/省略；算法目标、结构规则与重叠量保留明确默认值。

| 字段（retrieval.docxindex 下） | 当前显式值或算法默认 | 用途 |
| --- | --- | --- |
| input_tokens | 20000 | 结构检索分批内容预算，不含全部提示包装 |
| output_tokens | 12000 | 原文返回分页预算；多页总和不受单页值限制 |
| scan_batch_tokens | 6000 | 扫描批次正文预算及其返回分页预算 |
| vector.enabled / score_threshold | true / 0.45 | 是否使用向量分支及相似度门槛 |
| vector.probe_limit | 20 | 向量候选原节点数上限 |
| semantic.structure_candidate_limit | 12 | 结构检索候选数上限 |
| semantic.max_selected_nodes | 10 | 最终选中节点数上限；完整覆盖路径保留原行为 |
| rerank.enabled | true | 是否对适用候选执行重排 |
| routing.fallback_enabled | true | 原路由的后备检索开关 |
| indexing.summary.batch_max_nodes / batch_max_tokens | 显式 10 / 20000 | 摘要每批节点数量和正文预算；省略不限 |
| indexing.hierarchy.input_max_tokens | 显式 20000 | 层级识别输入内容预算；省略不限 |
| indexing.hierarchy.batch_target_tokens / batch_overlap_tokens | 默认 16000 / 800 | 大输入时分批目标和批次重叠；是算法参数 |
| indexing.hierarchy.max_levels / attachment.max_levels | 默认 6 / 6 | DOCX 标题结构层级约束，范围 1..6，不是模型资源上限 |
| indexing.paragraph.split_threshold_tokens / chunk_target_tokens | 默认 1000 / 700 | 原段落节点拆分阈值和目标，不改原文 |
| indexing.table.split_threshold_tokens / chunk_target_tokens | 默认 20000 / 18000 | 原表格节点拆分阈值和目标 |
| indexing.enrichment.llm_expand / llm_summary / llm_attachment_hierarchy | 默认 true | 是否启用对应索引补充能力 |
| indexing.heading.profiles | 代码内置标题示例集 | 既有标题识别规则 |

合法范围：input_tokens、output_tokens、scan_batch_tokens 的有限值至少 500；摘要 batch_max_tokens、层级 input_max_tokens 的有限值至少 1000。层级 batch_target_tokens 为 1000..26000、batch_overlap_tokens 为 0..5000 且小于目标；目标高于层级输入预算只 WARN。段落/表格 chunk_target_tokens 不能大于其 split_threshold_tokens；段落二者至少 100，表格至少 500。这些算法关系不以 null 表示无限。

retrieval.backend 必须是 docxindex 或 llamaindex。仅启用的后端产生相应配置风险预警。LlamaIndex 的 chunk_size=512、chunk_overlap=100 是算法参数；similarity_top_k、rerank_top_n 是候选/结果上限，null 或省略表示保留全部候选。两种后端的实际网络发送都进入 runtime。

## 7. API、日志、文件与配置生效

| 配置 | 含义 |
| --- | --- |
| api.require_request_model_config | 是否要求请求提供模型服务配置；沿用现有入口校验 |
| api.keep_input / keep_output | 是否保留任务输入与输出 |
| api.result_output_default | file 或 url |
| api.result_upload_enabled / domain / path / authorization | 结果上传开关、目标及授权；凭据不写入公开统计 |
| api.result_upload_timeout_seconds | HTTPX 连接、读、写和连接池各阶段超时；不表示整体上传截止时间；null/省略关闭这些超时 |
| api.meta_required / meta_fields | 业务元数据要求及字段名 |
| api.callback_enabled / callback_url / callback_file_field | 既有回调开关、地址和文件字段 |
| api.url_download_timeout_seconds / url_download_max_size | URL 下载 HTTPX 各阶段超时和累计大小上限；null/省略不限；大小支持 B/KB/MB/GB |
| docx.comment_include_criterion | 批注是否包含要点文字 |
| logging.enabled | 基础日志是否启用 |
| logging.detailed | 详细原文请求、响应、轨迹等；false 时仍保存基础调用统计、失败计数和预警 |

密钥与 SESSION_SECRET_KEY 继续放 .env。修改配置后重建/重启对应 API 与三个 worker，不能只更新宿主机文件就认定进程已经生效。config.example.yaml 为仓库模板，config.yaml 和 deploy/config.yaml 为各环境实际副本，保留其地址、DB 和显式数值。未知字段、旧字段、错误类型明确报错，避免拼错后被当成“省略不限”。

基础逻辑调用记录包含实际发送尝试，按 call_id 去重汇总全部重试。输入保存本地计数，usage 另外保存；服务 usage 优先，本地回退有来源标记。失败不清空输入，流式部分输出保留并标记 partial；未收到输出的实际服务生成量未知。向量维度、重排分数不是文本输出 token。未发送的本地拒绝不计服务消耗。API/workflow 结果使用相同尝试汇总，未知输出时总数为已知下限。

## 8. 脚本职责与 MCP

endpoints/runtime/model_calls.py 负责公共执行和重试；model_budget.py 负责等待、单次请求和合同截止时间；model_limits.py 负责 Redis 并发、滚动 RPM 与租约；model_tokens.py 负责离线计数；llm_client.py、embedding_client.py、reranker_client.py 负责各自实际请求和协议解析。DocxIndex/LlamaIndex 保留业务适配，旧 llm_semaphore.py 仅保留导入兼容，model_http.py 已删除。

MCP_SERVER_TARGET 指向 module:mcp_service.server.stdio_entry。新入口仅在导入 server 及依赖期间将普通 stdout 临时重定向到 stderr，之后恢复 stdout 再运行 MCP。config.py 原 print 无需改成逐条 stderr；协议消息仍从 stdout 返回。运行期的既有日志重定向保留，不在客户端忽略非法 JSONRPC。

## 9. 验证要求

本地 unittest 包含模拟服务和独立 Redis 测试，不称为 CI 或内网实测。真实测试使用独立 Compose 项目，读取本地已配置模型服务；不据此宣称公司内网已经通过。

验收脚本 tests/criteria_formal_recall/live_model_control_contracts.py：分别覆盖流式/非流式真实合同；正式验收以一次屏障同时提交六份不同合同，包含用户指定两份。保留 source SHA256、提交时间、任务 ID、API 状态、日志、报告、Redis 采样和逐次准入证据。任一任务最终失败则本批不通过，修复后重新跑整批六份。
