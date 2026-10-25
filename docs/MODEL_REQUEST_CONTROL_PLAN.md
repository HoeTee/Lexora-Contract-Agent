# 模型请求统一控制修改方案

日期：2026-10-25  
状态：已获批准，正在实施与验证。真实合同验收结果另行记录；尚未宣称整批通过。

## 1. 本次目的和范围

本次将 LLM、embedding、reranker 的实际请求集中到 runtime，统一输入计数、输入上限、并发、RPM、超时、重试和日志。保留三个 worker，以及现有 Web、API 两个入口。

输入长度控制服务于模型资源约束。保留原有 checkpoint、审查规则、Pydantic 数据校验和 Reflector 复核，不针对某个要点编号增加分支。

已确认的决定：

| 项目 | 本次决定 |
| --- | --- |
| 三类模型输入上限 | 各自只使用 max_input_tokens |
| LLM 三个旧字段 | 移除 max_context_tokens、max_prompt_tokens、max_result_tokens；原 max_prompt_tokens 的数值迁入 max_input_tokens |
| 额外容量检查 | 移除从离线记录读取容量、与配置值取最小值的处理；不使用 tokenizer.model_max_length 限制输入 |
| LLM 记忆 | 保留必要消息和较新的完整消息；超限时移除较旧完整消息组 |
| 配置组合风险 | 输出 WARN，继续启动，不因预计超限直接判定任务失败 |
| 实际请求超限 | 先整理历史或执行已有切片；必需输入仍超限时拒绝发送并明确记录原因 |
| HTTP 客户端工厂 | 移除 model_http.py，不新增 AsyncOpenAI 工厂 |
| tokenizer | 六套原版资源离线打包；一对多名称映射；必须显式配置 other |
| 未匹配模型 | 按 other 使用 cl100k_base，日志明确标记估算回退 |
| RPM | 每类模型独立、跨三个 worker 共享、滚动统计此前 60 秒 |
| MCP | 增加专用启动入口，只重定向加载阶段的普通打印 |
| 正式合同验收 | 一批混合六份不同合同，一次性提交，必须包括用户指定的两份；整批全部成功才通过 |

本文中的输入上限按应用端选定 tokenizer 的计数执行。使用 cl100k_base 回退时属于估算，不能将其宣称为服务端精确 usage。

## 2. 修改前代码依据

本节是制定方案时的代码快照，行号和行为用于说明修改原因。实施后的实际行为以 [配置说明](C:/Users/lenovo/Desktop/contract_agent/docs/CONCURRENCY_TIMEOUT_CONFIGURATION.md) 和实施验收记录为准。

| 当前事实 | 代码位置 |
| --- | --- |
| LLM 输入安全检查仍使用 cl100k_base，max_context_tokens 超过后仅警告 | [base_agent.py:36](C:/Users/lenovo/Desktop/contract_agent/agents/base_agent.py:36)、[base_agent.py:147](C:/Users/lenovo/Desktop/contract_agent/agents/base_agent.py:147) |
| max_result_tokens 实际用于截断工具返回内容 | [base_agent.py:233](C:/Users/lenovo/Desktop/contract_agent/agents/base_agent.py:233) |
| DocxIndex LLM 请求没有经过 Agent 的输入上限检查 | [client.py:96](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/llm/client.py:96) |
| 当前 embedding/reranker 会使用配置和离线容量的较小值 | [model_input.py:71](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/model_input.py:71) |
| 当前 LLM 流式合并要求所有返回项带 finish_reason | [llm_client.py:46](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_client.py:46) |
| 公共控制流程已经存在，实际名额控制位于 Redis semaphore | [model_calls.py:78](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/model_calls.py:78)、[llm_semaphore.py:225](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_semaphore.py:225) |
| 请求失败、逻辑调用失败时存在清空 token 的处理 | [call_log.py:198](C:/Users/lenovo/Desktop/contract_agent/loggers/call_log.py:198)、[call_log.py:320](C:/Users/lenovo/Desktop/contract_agent/loggers/call_log.py:320) |
| 扫描路径会合并全部返回页，单页预算不能限制最终工具返回总量 | [retriever.py:375](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/retriever.py:375) |
| Reflector 每轮新建；SubAgent 在复核修改时继续使用已有会话 | [orchestrator.py:1166](C:/Users/lenovo/Desktop/contract_agent/agents/orchestrator.py:1166)、[orchestrator.py:1253](C:/Users/lenovo/Desktop/contract_agent/agents/orchestrator.py:1253) |
| server.py 导入 config.py；config.py 启动时有两处普通打印 | [server.py:25](C:/Users/lenovo/Desktop/contract_agent/mcp_service/server/server.py:25)、[config.py:480](C:/Users/lenovo/Desktop/contract_agent/config.py:480) |
| 部署配置把 model_wait_timeout_seconds 少写了末尾 s | [deploy/config.yaml:42](C:/Users/lenovo/Desktop/contract_agent/deploy/config.yaml:42) |

## 3. 目标目录和职责

目标目录：C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/

~~~text
model_calls.py        公共调用流程、重试衔接、日志、校验衔接
model_budget.py       等待、单次请求、整份任务的时间预算
model_limits.py       Redis 全局并发、RPM、名额续期和释放
model_tokens.py       名称映射、离线 tokenizer、输入输出计数
llm_client.py         直接使用 AsyncOpenAI，处理流式协议和响应合并
embedding_client.py   直接使用 AsyncOpenAI.embeddings，解析并校验向量
reranker_client.py    直接使用 httpx.AsyncClient，处理现有重排协议
~~~

保留现有 model_calls.py、model_budget.py，调整现有 llm_client.py。将 llm_semaphore.py 改为通用名称 model_limits.py；将 DocxIndex 中的 tokenizer 基础能力迁入 model_tokens.py。将三类模型的实际 HTTP 请求和响应解析集中到上述客户端。

删除 model_http.py 和全部引用。LLM、embedding 使用 AsyncOpenAI 自身的 HTTP 客户端；reranker 直接创建 httpx.AsyncClient。保留既有证书验证行为，不增加 HTTPS 配置。

不新增单纯创建 AsyncOpenAI 或转发参数的工厂。公共控制层需要保留，因为跨进程的 Redis 并发、RPM、项目日志和时间预算不是 SDK 自动提供的能力。

Agent、DocxIndex、LlamaIndex 的业务适配可以保留，但不能各自另写实际请求、重试和 token 计数逻辑。SDK 内部自动重试关闭，每次实际发送都经过公共控制。

### 3.1 公共接口的输入和输出

| 能力 | 输入 | 输出及副作用 |
| --- | --- | --- |
| 输入计数 | 模型类型、原始模型名称、完整请求内容、tokenizer 映射 | token 数、tokenizer 版本、匹配来源；不联网、不从资源记录推导上限 |
| Agent 请求整理 | 完整历史、必须保留的消息、工具定义、当前输入上限 | 发送用消息副本、整理前后计数、移除消息组清单；不改历史日志 |
| 请求准入 | 模型类型、共享资源键、并发和 RPM 限制、等待预算 | 名额和准入记录；受限时排队；超时、取消时清理等待者 |
| 公共执行 | 已准备的请求、发送函数、可选输出校验函数、现有预算 | 最终响应或明确错误；逐次记录实际发送和重试 |
| 具体客户端 | 该模型接口所需参数 | 统一可消费的响应和 HTTP 状态；保留原请求格式 |

将现有 ModelInputTooLong 错误移到公共错误模块，避免 Agent 依赖 DocxIndex。错误包含模型类型、输入 token、配置上限、调用位置、sent=false；输入未变化时不作为网络故障或要点故障原样重试。

## 4. 输入配置迁移

以下仅展示相关字段，模型地址、名称、凭据及其他参数沿用各环境现值。

~~~yaml
llm:
  max_input_tokens: 88000
  stream: false

embedding:
  max_input_tokens: 1024
  chunk_overlap_tokens: 32

rerank:
  max_input_tokens: 1024
~~~

| 旧字段或行为 | 迁移处理 |
| --- | --- |
| llm.max_prompt_tokens | 改为 llm.max_input_tokens，迁移原显式数值 |
| llm.max_context_tokens | 删除配置、常量、初始化参数、字符数容量警告和相关日志引用 |
| llm.max_result_tokens | 删除配置、常量和按独立阈值截断工具结果的代码 |
| max_prompt_tokens 必须小于 90000 | 删除这条硬编码限制 |
| effective_limit 取配置与模型容量的较小值 | 删除；最终上限只来自对应 max_input_tokens |
| manifest.context_tokens、内置模型容量字典 | 不再用于输入控制，准备脚本不再维护这项控制元数据 |
| tokenizer 自动截断 | 不用于隐藏超限输入，必须对完整待发送内容计数 |

配置 40000 就按 40000 检查，不自动降低为 8192 或 32768。没有第二个上下文上限，也不新增模型生成输出上限。本次仍对已收到的输出做统计。

DocxIndex 原有 input_tokens、output_tokens、scan_batch_tokens 等保留为批次或检索内容组织参数。它们不是新的模型容量限制，不取代发送前对完整请求的计数。

## 5. LLM 历史消息处理

### 5.1 采用较新完整消息，不做消息内部截断

发送前为完整请求计数，包括系统提示、当前任务、工具定义、历史消息、工具返回和模型消息模板。

未超限时使用原消息。超限时对发送副本整理：

1. 保留系统消息、当前任务原始要求、当前最新请求，以及尚未处理完成的工具交互。
2. 将其余历史划分为可移除的完整消息组。
3. 优先保留较新的组，从最旧的可移除组开始删除。
4. 每次删除后重新计算完整请求，满足上限即停止。
5. 全部可移除历史都删除后仍超限，拒绝发送并明确记录必需输入过长。

普通消息完整移除。包含工具调用的 assistant 消息，必须与其所有对应 tool 返回作为一组移除，不能留下孤立的 tool_call_id 或缺失返回的工具调用。工具定义属于请求结构，不能为了凑长度随意删除。

完整历史继续保存在原日志中，不在 self.messages 上原地删除。发送日志另外保存实际发出的消息或其引用、移除组的索引和计数，保证能重建实际输入。

不调用额外模型为历史生成记忆摘要，不按照字符数截掉半条消息，不针对 H11 等具体要点写保留规则。

历史不再出现在发送副本中，就不再对模型可见。是否影响具体合同的审查判断，实施前不确定，需要用真实合同验证；不能把长度合格等同于审查正确。

### 5.2 不承诺通用 LLM 自动拆任务

现有扫描、节点摘要等已经具备批次结构，可以在各自已有组包位置对最终输入重新计数。普通 Agent 输入不能像 embedding 节点一样任意分片后合并答案。

本次主要用完整历史消息组移除处理会话增长，不引入通用提示词压缩器，不让模型自己调整尚未发送的超长请求。固定提示、当前任务等必需内容本身超限时明确失败，输入未变化时不重复发送。

## 6. Embedding、reranker 输入处理

### 6.1 Embedding

原节点保留完整原文和 node_id；向量化时按 embedding.max_input_tokens 切片。每条实际输入计入 tokenizer 加入的特殊 token，发送前再次检查。

相邻片段最多重叠 embedding.chunk_overlap_tokens 个 token。重叠属于输入准备参数，不传给模型服务。若当前切片太小，实际重叠需要保留推进新文本的空间，记录配置和实际值，不能形成不前进的切片循环。相关配置组合首先预警，不据此直接阻止服务启动。

同一节点的检索分数取其所有切片与 query 的最高相似度；返回原节点。query 也需要单独计数，不能把超长 query 原样重试或静默截断。

### 6.2 Reranker

候选准备阶段对长候选文本切片。每片必须与 query、适用接口的 instruct、输入包装一起计数。query 和固定包装先占用预算，剩余空间给候选片段。

节点重排分数取所有片段的最高分，返回原文和原 node_id。不新增 reranker 重叠配置，不修改服务请求格式和 provider 选择方式。

query 和固定包装本身已经超限时，拆候选片段不能解决问题，明确返回输入超限错误。

### 6.3 按条计数，不把批次数量当作单条长度

embedding 的上限按每条文本检查；reranker 的上限按每组 query 与候选片段检查。一次提交十条，不等于十条合计必须小于单条上限。

现有批次数量规则保留，不额外假设整批只能有 1024 token。原节点过长但切片后每条合格，是正常准备过程，记录切片数量，不反复报警为请求超限。

## 7. 配置关系预警与数量监督

### 7.1 预警不等于执行失败

跨配置组合存在超限风险，记录 WARN 并继续启动，不将预警直接写成合同失败，不暗中修改用户上限。

实际发送前必须使用最终输入计数。LLM 先移除旧历史；embedding、reranker 先准备片段；处理后仍超限才拒绝发送。

配置类型错误、非法负值、缺少必需 tokenizer 映射、资源文件损坏仍属于配置错误。它们与“合法配置组合可能使实际输入超限”的 WARN 区分。

### 7.2 LLM 相关配置

下表中 indexing 字段完整前缀为 retrieval.docxindex.indexing；部分值当前来自代码默认值，而非显式 YAML。

| 配置 | 当前值或默认值 | 需要监督的关系 |
| --- | --- | --- |
| retrieval.docxindex.input_tokens | 20000 | 结构内容预算加提示词、query、包装后，才是完整 LLM 输入 |
| retrieval.docxindex.scan_batch_tokens | 6000 | 扫描正文预算不含全部固定输入；同时还参与扫描返回分页 |
| retrieval.docxindex.output_tokens | 12000 | 是返回原文的分页预算；工具结果进入 Agent 历史后占用 LLM 输入 |
| indexing.summary.batch_max_tokens | 默认 20000 | 节点内容加摘要提示和包装后，与 LLM 上限比较 |
| indexing.summary.batch_max_nodes | 默认 10 | 影响组包和节点包装开销，不能单凭节点数推导 token |
| indexing.hierarchy.input_max_tokens | 默认 20000 | 层级识别输入预算，与完整层级请求的实际长度一并监测 |
| indexing.hierarchy.batch_target_tokens | 默认 16000 | 批次目标会影响单批内容量 |
| indexing.hierarchy.batch_overlap_tokens | 默认 800 | 影响重复输入和批次数量；是否已包含在批次中要按实际组包计数，不重复相加 |
| llm.max_tool_calls | 本地、部署为 8 | 更多工具交互可能累积更多历史；不等于 8 倍固定 token |
| workflow.max_reflection_rounds | 10 | SubAgent 可能继续累积修改会话；Reflector 当前每轮新建，不能直接乘以轮数 |
| workflow.model_call_max_retries | 5 | 原样 HTTP 重试不增加单次长度；追加格式修复反馈可能增加历史 |

检索返回多页时记录真实合并后的总长度，不能将单页预算当作最终工具响应上限。当前扫描路径会合并全部页，见第 2 节代码依据。

semantic.max_selected_nodes 是最终节点数量设置，不是“每个节点 output_tokens”；不能用 max_selected_nodes 乘以 output_tokens 作为固定输入长度。

### 7.3 Embedding、reranker 相关配置

| 配置或数据 | 作用 |
| --- | --- |
| embedding.chunk_overlap_tokens | 影响片段重复量和新文本推进量；与输入上限接近或冲突时预警 |
| indexing.paragraph.split_threshold_tokens / chunk_target_tokens | 默认 1000 / 700，影响原节点形状和后续 embedding 切片数量 |
| indexing.table.split_threshold_tokens / chunk_target_tokens | 默认 20000 / 18000，影响表格节点和后续切片数量；不直接判定最终模型输入超限 |
| rerank.instruct、运行时 query | 按接口实际输入构造计入每组重排输入，不能只数候选正文 |
| retrieval.docxindex.vector.probe_limit | 20，主要影响向量候选数量和重排请求量 |
| retrieval.docxindex.semantic.structure_candidate_limit | 12，主要影响候选数量，不等于单条候选长度 |
| retrieval.docxindex.semantic.max_selected_nodes | 10，主要限制最后选中节点，不能当作重排前候选总量限制；完整覆盖路径还需单独记录 |

若启用 LlamaIndex，另外检查 retrieval.llamaindex.chunk_size、chunk_overlap、similarity_top_k、rerank_top_n。DocxIndex 模式不为未启用后端输出重复风险告警。

并发、RPM、模型超时和整份任务时限不直接决定单次输入长度，不能参与“单次 token 数乘法”。它们需要分别监督吞吐和耗时。

### 7.4 监督时机和日志

1. 启动加载配置：检查批次预算与模型输入上限的明显风险，输出 WARN，显露生效默认值。
2. 工具定义和任务数据就绪：计数固定提示、工具定义、实际 query 等，记录剩余输入空间。
3. 每次发送前：记录完整输入、历史整理前后长度、片段实际长度；只有这一步能判断实际请求是否合格。

三条最终关系：

~~~text
T(LLM 完整消息、工具定义与消息模板) <= llm.max_input_tokens
T(每条 embedding 实际输入) <= embedding.max_input_tokens
T(query、单个 reranker 片段与输入包装) <= rerank.max_input_tokens
~~~

T 使用对应 tokenizer；不要将不同 tokenizer 下的预算估计直接当作可精确相加的数量。占用比例为实际计数 / 配置上限；上限为 null 时该比例不计算。

例：LLM 上限为 6000，扫描正文预算也是 6000，则提示“正文预算已占满上限，尚未包含提示词和 query”，服务继续启动。该预警表示配置风险，不表示所有真实批次一定超限。

不新增预警比例和独立记忆 token 上限配置。按已有三个上限记录风险、实际长度和余量。相同配置风险启动时汇总一次；运行时按调用记录，避免同一片段反复刷告警。

## 8. Tokenizer 配置和离线资源

~~~yaml
tokenizers:
  qwen3.6-35b-a3b:
    - Qwen3.6-35B-A3B
  qwen3-embedding:
    - Qwen-Embedding-8B
    - Qwen3-Embedding-8B
    - qwen3-embedding
  qwen3-reranker:
    - Qwen-Reranker-8B
    - Qwen3-Reranker-8B
    - qwen3-reranker
  bge-m3:
    - bge-m3
  bge-reranker-v2-m3:
    - bge-reranker-v2-m3
  deepseek-v4-flash-0731:
    - DeepSeek-V4-Flash-0731
  cl100k_base:
    - other
~~~

左侧是本地 tokenizer ID，列表是模型服务实际接受的名称。一个 tokenizer 支持多个名称，名称忽略大小写和首尾空格，同一名称不能映射到不同 tokenizer。

不再读取隐藏内置模型别名；未匹配名称走 other。必须显式配置 cl100k_base 下的 other，缺少整个 tokenizers 或缺少 other 都在启动时报错。other 是保留标记，不作为请求模型名称。

指定的 tokenizer ID、文件或校验值错误，启动时报错，不悄悄回退。正常未匹配才按已声明的 other 规则回退，并记录 tokenizer=cl100k_base、match=other、estimated=true。

离线打包六套资源：Qwen3.6-35B-A3B、Qwen3-Embedding-8B、Qwen3-Reranker-8B、BGE-M3、BGE-Reranker-v2-m3、DeepSeek-V4-Flash-0731。DeepSeek 0731 使用该版本的 tokenizer 和消息编码文件，不用 1017 版本直接替换。

更新 prepare_tokenizers.py，固定仓库 revision，保存文件字节数、SHA256 和许可证。只下载计数、消息编码所需资源，不下载权重。运行时只读本地文件，不访问网络，不执行临时下载的远程代码。

现有五套实测约 94.46 MiB，加入 DeepSeek 0731 资源预计约 100.6 MiB，不含 Python 依赖和模型权重。API、三个 worker 和 MCP 子进程使用同一镜像内资源。

## 9. 流式与非流式处理

保留 llm.stream，默认 false。embedding、reranker 继续使用现有完整 JSON 响应，不增加流式开关。

| 流式接收情况 | 拟处理 |
| --- | --- |
| 所需返回项均有合法 finish_reason | 认可服务声明的结束依据，不要求另一个重复标志 |
| 收到 DONE，预期返回项和可用内容齐全，但缺 finish_reason | 允许进入后续数据校验，记录实际结束依据，不伪造 stop |
| 只有 DONE，没有可用正文或工具调用 | 空响应失败 |
| EOF / 断开，没有明确结束依据 | 断流失败，即使已有文本恰好是合法 JSON 也不能据此成功 |
| HTTP 错误或 HTTP 200 中的错误 JSON | 保存真实状态和错误体，不能统一报为缺少 finish_reason |
| 没有 usage | 不单独判定传输失败，保留本地计数并标记服务统计缺失 |

上表 DONE 指 SSE 的 [DONE] 标记。在 SSE 接收层保存它，不能在 SDK 消费掉标记后仅靠 finish_reason 判断。

内部响应结构允许保留缺失结束原因，同时校验响应结构；不使用跳过校验或捏造字段的方式绕过问题。工具调用可以没有正文，但必须作为可用工具调用继续走参数校验。

明确结束不代表业务数据合格：先完成传输判断，再解析 JSON、执行该环节 Pydantic 校验；需要 Reflector 的环节再进行语义复核。原业务规则保留，日志分别记录传输失败、数据校验失败和复核不通过。

## 10. 全局并发、RPM、超时与 lease

### 10.1 新增 RPM，不新增另一套等待配置

~~~yaml
model_semaphore:
  llm:
    max_concurrent_requests: 5
    max_requests_per_minute: 30
  embedding:
    max_concurrent_requests: 3
    max_requests_per_minute: null
  reranker:
    max_concurrent_requests: 10
    max_requests_per_minute: null
~~~

这是相关字段示例，redis_url、key、lease_seconds、poll_interval_seconds 等现有部署值保留。各环境明确填写的并发值不在迁移时擅自改动。

每类模型独立控制，三个 worker 接入相同 Redis 资源键，使用 Redis 时间和原子脚本。统计区间为此刻前 60 秒的滚动区间，不使用整点分钟计数，也不将滑动窗口当作均匀间隔发送器。

原子检查排队顺序、活动名额和窗口请求数。达到 RPM 或并发上限则排队，两个条件同时允许才准入。等待 RPM 不占活动名额，等待并发也不预先消费一个长期搁置的 RPM 发送资格。

准入后紧接着调用 HTTP 发送函数。若明确在发送前取消，撤销未使用的 RPM 预留；一旦发起发送，连接失败、超时或服务失败都计入本地请求统计，不因结果失败退还 RPM。重试每次计数，流式片段不另算请求。

请求结束释放并发名额，RPM 记录保留到移出窗口。使用不同 Redis 或资源键的其他部署不会自动共享这个限制，启动摘要必须显示实际共享资源键。

### 10.2 时间关系

| 配置 | 含义 |
| --- | --- |
| workflow.model_wait_timeout_seconds | 一次逻辑模型操作累计等待并发与 RPM 的预算，包括其后续实际请求的再次等待 |
| workflow.model_call_timeout_seconds | 每次实际 HTTP 请求的时间规则；非流式为总时长，流式为连续无新增内容时长 |
| workflow.model_call_max_retries | 保留现有发送预算定义；5 表示初次加最多 5 次重新发送，结构修复与传输重试使用现有共享预算 |
| workflow.criterion_retry_max_attempts | 整条要点的现有尝试上限，独立于单次 HTTP 请求；输入未变的本地超限不触发原样循环 |
| queue.pending_timeout_seconds / queued_timeout_seconds | 等待 worker 接手前的队列监督，不是模型名额等待时间 |
| queue.task_time_limit_seconds | worker 接手后的任务总硬时限，包含执行中的模型排队和调用 |
| model_semaphore.*.lease_seconds | 拿到活动名额后用于异常回收的有限租约；自动续期，正常结束主动释放 |

显式填写的 60 秒请求配置保留。每次实际重新发送独立享有该时间，排队不扣除请求时间。

流式时新增正文、推理内容、工具调用参数会刷新空闲计时；心跳、空白片段、角色声明、usage 不刷新。超时或断流后关闭该次响应，释放名额，再按预算决定重试。

lease 不覆盖整份合同，也不覆盖取到名额之前的排队。每次请求分别取得名额并续期，不需要将等待时间和全部重试时长相加来配置 lease。不再用请求总时长推导静态 lease 必须覆盖整个流式过程；保留正数、续期和清理可行性约束。续期失败时中止受保护请求，避免在名额失效后继续占用模型。

等待者也有内部心跳和失效清理，避免无限等待配置下，崩溃的队首永久阻塞后续请求。

## 11. null、默认值和配置迁移

限制项规则统一为：明确数字使用该限制，null 或省略表示不设置该项应用限制。计数和监督仍运行。

| 字段类别 | null / 省略语义 |
| --- | --- |
| 三个 max_input_tokens | 不设该类型的应用输入上限，仍统计 token |
| 三类 max_concurrent_requests、max_requests_per_minute | 不设相应并发或频率限制，仍记录活动请求和窗口数量 |
| 模型等待、模型请求、任务硬时限、pending/queued 时限 | 禁用该项截止时间或对应监督分支 |
| max_tool_calls、max_reflection_rounds、模型重试、要点尝试上限 | 不设该项次数上限，不以隐藏有限默认值替代 |
| 候选数、选中节点数等上限 | 不设该层数量上限，后续实际请求仍经过模型输入检查 |
| API 上传/下载超时、下载大小上限 | 不设对应应用限制，消费者需显式支持 None，避免库的默认值重新形成隐藏限制 |

检索内容预算、批次最大值如允许省略，必须按其“上限”性质逐项支持 None，并在最终组包时服从模型输入检查；不能继续静默套用有限默认值。split_threshold、chunk_target、overlap、temperature、top_p、seed、lease、poll_interval 等算法或行为参数不机械解释成无限，其实际值与默认值在配置文档逐项列清。

不把 None、0、字符串 null 或无穷数混用。无限请求时限需要显式关闭 SDK/httpx 的隐式超时；无限任务时限需要同时调整 Celery 配置、任务截止时间上下文、MCP 子进程环境和 watchdog 分支。

仅将模型等待设为 null，不会解除有限任务硬时限。lease 和内部失效清理保持有限，不改成永久占位。布尔字段 llm.stream 的 null 仍是非法值。

迁移时保留各环境原有显式数值，不自动把 180、1800 或 7200 改成无限。本地与部署现有 criterion_retry_max_attempts、task_time_limit_seconds 有差异，记录差异，不用本地文件覆盖部署值。

修正 deploy/config.yaml 的 model_wait_timeout_second 为 model_wait_timeout_seconds。新配置拒绝旧字段和未知字段，给出具体迁移提示，防止拼写错误被当作省略后无限。

## 12. Token 日志和结果统计

每次准备输入就保存本地计数，包含超限后未发送的尝试。每次实际发送有独立记录，sent 字段用于区分准备、排队和模型调用。

| 信息 | 要求 |
| --- | --- |
| local_input_tokens | 最终准备输入的本地计数，失败不清空 |
| received_output_tokens | 已收到正文、推理和工具参数的本地计数；流式按累计内容计算，避免逐片相加的分词边界误差 |
| usage_input_tokens / usage_output_tokens | 服务实际返回时保存，不与本地数据混为同一来源 |
| tokenizer / match / estimated | 保存资源版本、明确名称匹配或 other 回退、本地估算标记 |
| max_input_tokens / headroom / utilization | 配置上限、剩余空间和占用比例；不限时后两项可为空 |
| memory_before / memory_after / removed_groups | 历史整理前后计数、移除完整消息组数量及位置引用 |
| chunk_count / max_chunk_tokens / candidate_count | 切片数量、单片最大输入、候选数；不把总条数误当单条长度 |
| failure_stage / retry_count / sent | 区分输入检查、排队、传输、断流、业务数据校验，以及实际重试次数 |
| waiting_reason / waiting_seconds / rpm_count | 区分并发等待、RPM 等待和窗口数量 |

非流式失败且未收到正文时，服务实际生成的输出未知，不能编造为零。已收到 usage 但后续校验失败，也必须保留。Embedding 向量维度和 reranker 分数不冒充生成文本输出 token。

逻辑调用最终失败不能清空尝试记录。汇总按实际发送的尝试去重累加，本地未发送准备不算服务消耗；另行统计本地超限与记忆移除次数。统计来源、完整性与未知项必须保留。

logging.detailed=false 时仍记录基础统计和预警。原文级详细请求内容按原详细日志策略处理。Agent、日志、已有 API 和报告的 token 汇总使用同一统计结果，不只累计最终成功响应。

## 13. MCP 启动入口

新增 C:/Users/lenovo/Desktop/contract_agent/mcp_service/server/stdio_entry.py：

~~~python
import importlib
import sys
from contextlib import redirect_stdout


def main():
    with redirect_stdout(sys.stderr):
        server = importlib.import_module("mcp_service.server.server")
    server.mcp.run()


if __name__ == "__main__":
    main()
~~~

复用现有 config.py 的 MCP_SERVER_TARGET，设为 module:mcp_service.server.stdio_entry。TARGET 仅决定客户端启动哪个程序；入口内的 redirect_stdout 才改变加载期间普通打印的去向。该变量不是重定向机制的必要条件，也不新增对应 YAML 配置。

导入 server.py 及 config.py 在同一 MCP 子进程执行，期间普通 print 使用临时重定向的 sys.stdout。退出 with 后恢复 stdout，再启动 MCP 协议。不能将 mcp.run 或整个 runpy.run_module 包在重定向中，否则协议消息也会被送往 stderr。

保留运行期间已有日志重定向和客户端严格 JSONRPC 解析，不通过忽略非 JSON 行掩盖协议污染。config.py 启动摘要同步改为显示新输入配置，不再引用已删除字段；不依靠逐个为两处打印加 file 参数解决启动问题。

更新直接写死旧模块的冒烟脚本，使正式流程和标准冒烟经过同一入口。自定义测试服务仍可显式指定自己的目标。MCP 子进程的父进程退出保护不再以存在任务截止时间环境变量为前提。

## 14. 文件改动清单

| 文件或目录 | 拟改动 |
| --- | --- |
| C:/Users/lenovo/Desktop/contract_agent/config.py | 新输入字段、移除旧字段、tokenizer 映射、RPM、可空限制、预警摘要和 MCP 目标 |
| C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/ | 公共流程、预算、全局限制、tokenizer 和三类客户端；删除 model_http.py |
| C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/errors.py | 公共输入超限错误与错误阶段，避免按网络故障原样重试 |
| C:/Users/lenovo/Desktop/contract_agent/agents/base_agent.py | 完整消息组选择、发送副本、统一计数、移除旧三字段和字符截断 |
| C:/Users/lenovo/Desktop/contract_agent/agents/summarizer.py | 使用统一输入字段和计数，保留原业务数据规则 |
| C:/Users/lenovo/Desktop/contract_agent/agents/orchestrator.py | 通用错误传递；不将未变化的输入超限当成可原样重试故障；不新增要点编号分支 |
| C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/ | 客户端迁移、计数调用和输入检查；保留原文、节点映射、切片最高分与既有检索规则 |
| C:/Users/lenovo/Desktop/contract_agent/tools/retrieval/llamaindex/ | 三类模型接入同一请求和计数能力，移除 HTTP 工厂引用 |
| C:/Users/lenovo/Desktop/contract_agent/loggers/call_log.py | 失败计数保留、来源与完整性、实际尝试汇总、预警和记忆整理统计 |
| C:/Users/lenovo/Desktop/contract_agent/workflow/workflow.py | 已有结果统计统一取值，错误和最终状态传递一致 |
| C:/Users/lenovo/Desktop/contract_agent/task_queue/ | 可空任务硬时限和截止时间传递，不新增软时限 |
| C:/Users/lenovo/Desktop/contract_agent/endpoints/review/task_store.py | 可空 pending/queued 监督以及明确最终错误状态 |
| C:/Users/lenovo/Desktop/contract_agent/mcp_service/ | 专用入口、启动目标衔接和子进程生命周期保护 |
| C:/Users/lenovo/Desktop/contract_agent/scripts/prepare_tokenizers.py | 六套离线资源、固定版本、编码文件与断网验证，不维护容量控制字段 |
| C:/Users/lenovo/Desktop/contract_agent/resources/tokenizers/ | 新增 DeepSeek 0731，更新 manifest 和校验 |
| C:/Users/lenovo/Desktop/contract_agent/config.yaml、config.example.yaml、deploy/config.yaml | 同步新结构，保留各环境地址、凭据位置、Redis 地址和显式限额 |
| C:/Users/lenovo/Desktop/contract_agent/Dockerfile、protect/build.ps1 | 包含新入口、runtime 模块及资源，断网验证计数资源；实际可执行包包含新代码 |
| C:/Users/lenovo/Desktop/contract_agent/tests/criteria_formal_recall/ | 更新已有测试适配，补充边界、Redis、子进程和混合六合同验收 |
| C:/Users/lenovo/Desktop/contract_agent/docs/CONCURRENCY_TIMEOUT_CONFIGURATION.md | 实施后改为实际代码对应的配置解释，清除旧三字段、隐藏容量和过时默认描述 |

只有 tokenizer 映射、三类 RPM 和 LLM 输入字段迁移是新增或调整的用户配置入口。不恢复逐模型 enabled、旧逐类型等待时限、Celery 软时限或 OA 入口。

## 15. 验证和结束条件

### 15.1 本地回归与独立 Redis

使用现有 unittest 与集成脚本；项目没有 CI 时，不将本地通过表述成 CI 通过。

| 领域 | 必须覆盖 |
| --- | --- |
| 输入配置 | 旧三字段删除、新字段生效、40000 不被自动降低、null 与省略无隐藏输入上限 |
| 配置预警 | 合法风险组合只 WARN，服务仍启动；未知字段、错误类型、缺少 other 明确报错 |
| 记忆 | 保留必要消息、优先保留较新历史、完整移除工具消息组、原历史不变、整理后最终计数合格 |
| 无法容纳 | 必需消息本身超限不发送，不消耗 RPM 和实际模型重试次数，不重复要点原样尝试 |
| 切片 | embedding 特殊 token、重叠、reranker 的 query 和包装、原文与 node_id 保留、最高切片分数合并 |
| tokenizer | 多名称、大小写、other 回退、资源损坏、六套断网加载、LLM 消息和工具定义计数 |
| 流式 | 有 finish_reason 无 DONE、有 DONE 无 finish_reason、空响应、EOF、错误 JSON、心跳、推理和工具参数进展 |
| 超时重试 | 每次请求独立时间、非流式总时限、流式空闲时限、断流后重试、SDK 不隐式多发 |
| 全局控制 | 三进程并发、滚动窗口边界、FIFO、RPM 等待不占名额、取消预留、排队者异常退出、lease 续期与释放 |
| 可空时限 | 模型等待与任务硬时限分别关闭、有限外层仍生效、子进程保护不丢失、无限等待不被崩溃队首卡死 |
| 日志 | 失败输入、部分输出、服务 usage、未知输出、未发送超限、重试汇总、detailed=false 统计 |
| MCP | 新入口启动日志在 stderr，stdout 可逐行解析为 JSONRPC，工具调用正常，运行期协议未被重定向 |

移除 model_http.py 后更新原测试的模拟位置，直接拦截 SDK 或 HTTP transport，不能为了旧测试保留无必要工厂。

### 15.2 正式验收：同批混搭六份不同合同

一次性提交同一批六个任务，六份合同混搭，不按合同类型先后分批，不以同一合同复制六次代替。必须包含用户明确指定的 C1、C2，其余四份从同一合同样例目录选择不同合同，默认组合如下。

| 编号 | 合同路径 | 来源 |
| --- | --- | --- |
| C1 | C:/Users/lenovo/Desktop/contract_agent/deploy/test_contract.docx | 用户指定，必测 |
| C2 | C:/Users/lenovo/Downloads/合同样例/【20251224已审查】2025年度算力资源服务租赁项目（算力资源一）采购合同1223.docx | 用户指定，必测 |
| C3 | C:/Users/lenovo/Downloads/合同样例/【已审查】（2026年058号）2026年度GPU算力服务租赁采购合同-20260617_批注版.docx | 补充的不同合同 |
| C4 | C:/Users/lenovo/Downloads/合同样例/【已审查】（456号）农商财富大厦办公场地租赁合同11.17.docx | 补充的不同合同 |
| C5 | C:/Users/lenovo/Downloads/合同样例/【已审查】（524号）IPv6 上网行为管理设备采购合同.docx | 补充的不同合同 |
| C6 | C:/Users/lenovo/Downloads/合同样例/【已审查】（528号）2026年至2028年贵宾医疗服务合作协议-邵逸夫医院.docx | 补充的不同合同 |

编写方案时已检查六份文件存在、可作为 ZIP 打开并包含 word/document.xml。此检查仅证明测试输入文件可用，不表示已经执行合同审查。

六个 API 提交并发发起，每个任务使用独立 task_id 和输出目录；记录同一 batch_id、源文件路径与 SHA256、提交开始/结束时间。通过六个请求共同的提交屏障启动，不能等上一份完成后再提交下一份。

保留三个 worker、每个 concurrency=1：六个任务同批提交后，正常形态是三个执行、其余排队，后续接手。这里验证六任务同批压力，不改成六个 worker，也不宣称三个执行槽可同时执行六份完整合同。

### 15.3 通信模式和测试配置

流式、非流式都做专项验证，并使用真实合同请求；至少覆盖 C1、C2。专项验证不能替代同批六合同的正式验收。

正式混合六任务使用一套冻结的待交付配置，记录 llm.stream、三个 max_input_tokens、并发、RPM、等待和任务时限。整批期间不修改全局 stream 或限额，不将六个任务拆成两组三个来分别测试模式。

若复测另一个通信模式的六任务压力，仍按同一批 C1—C6 一次性提交；两次结果分开记录，不能拼成一批全成功。具体模型地址和凭据使用实施时获授权的可访问环境，不把本地模拟或外网服务结果当作内网实测。

建议正式输入限额沿用迁移后的 88000 / 1024 / 1024，单次模型时间保持 60 秒；其余现有显式配置不为了得到通过结果而静默调整。需要改变测试配置时记录原因、差异并重新启动相关进程。

### 15.4 必须全部满足才可结束实施任务

1. 本地、Redis、子进程、离线资源、流式与非流式专项检查通过。
2. 正式同批 C1—C6 六份全部成功；不是五份成功、一份失败，也不是分批通过后拼接结果。
3. 每份日志、API 和报告最终状态一致，报告可打开，所有要点正常完成或明确不适用；存在合同风险不等于程序执行失败，不为通过验收把风险判成合规。
4. 每次实际输入按选定 tokenizer 计数满足配置；历史整理、切片和 WARN 有完整记录，没有隐藏截断或隐藏容量取最小值。
5. 三 worker 共享并发与 RPM，重试和 MCP 子进程没有绕过限制；整批结束后活动名额及等待者清理完成。
6. 中间失败可以通过已配置重试恢复，但必须保留次数、原因和消耗；不能删除失败尝试来声称零失败。
7. 任一合同最终失败，先定位并修复，再整批六份重新提交；不能只补跑失败的那一份就结束。修复触及的专项检查也要重跑。
8. 外部服务阻断真实验证时，只能如实记录尚未完成验收，不能以模拟结果替代，也不能宣称本方案实施完成。

以上全部通过才达到用户要求的结束条件。本文档本身的交付不代表这些条件已经执行或达到。

## 16. 打包、迁移和实施顺序

1. 获得实施授权后，按仓库要求先提交代码修改前基线；不将当前无关改动和已暂存删除混入提交。
2. 实现配置读取、tokenizer 映射和输入计数，迁移三类输入字段，移除额外容量检查。
3. 实现完整历史消息组选择及配置 WARN，迁移三类客户端，删除 model_http.py。
4. 接入全局滑动窗口、可空限制、时间预算和名额清理；修复流式结束判断和失败统计。
5. 接入 MCP 专用入口和父进程保护，更新标准冒烟路径。
6. 更新配置副本、配置解释和测试，先完成本地、Redis、子进程和资源验证。
7. 下载并固定 DeepSeek 0731 等缺少的离线资源，更新实际保护镜像构建清单；构建时断网验证六套 tokenizer 和 cl100k_base。
8. 同步部署用挂载配置的新字段，保留该环境模型地址、凭据、Redis DB 编号和资源键；不将本地 DB 用途硬写成全部环境的固定用途。
9. 以包含新代码和资源的同一镜像启动 API、三个 worker，核对最终生效摘要；没有配置热更新承诺。
10. 执行通信模式专项和正式混合六合同验收，保存证据。挂载总数据目录，按现有用户/任务分区保存输入、日志和报告，不写入容器临时文件系统作为长期交付。
11. 达到全部结束条件后提交实施结果，交付配置文档、提交记录、六任务清单、逐份最终状态和报告路径、token 与时延统计、并发/RPM 峰值、最终 Redis 名额清理结果。

本方案已获实施授权，代码修改、离线资源准备、镜像构建及真实合同验收正在执行。必须按第 15.4 节达到全部条件后，才可以将状态改为验收通过。
