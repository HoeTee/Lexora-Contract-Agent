# 并发、超时与重试配置说明及修改方案

状态：方案文档，尚未实现。日期：2026-10-16。

本文说明本次修改涉及的配置、配置之间的约束及实施范围。下文的目标配置不能直接用于当前版本；必须先完成代码、配置模板和部署文件的同步修改。

依据为公司内网配置及已核对的项目代码。用户已确认内网与本地依赖版本一致：Celery 5.6.3、Kombu 5.6.2、redis-py 8.1.0。

## 1. 已确定的设计

- 保留三个 Celery worker，每个 worker 的执行并发为 1。
- 合同审查只保留 Web、API 两个提交入口，统一通过 Celery 入队。
- 移除 OA 审查入口，保留现有 Web 登录、管理、历史、结果展示和下载功能。
- 保留同一 Redis 服务中的 DB 0、DB 2。DB 0 用于任务消息，DB 2 用于模型名额。
- 所有 LLM、embedding、reranker 请求使用统一的模型执行控制。
- 模型全局限流固定启用，移除其 `enabled` 配置。
- 保留名额租约、检查间隔和 Redis key 的配置。
- 一次模型调用只使用一份时间预算和一份重试次数。
- 整份合同只配置 Celery 硬执行时限。移除方案中的软时限，不保留 `null` 占位，也不新增项目级软时限配置。
- 本轮文档不改变业务代码、运行配置或部署配置。

60 秒模型预算、30 分钟合同上限及要点并发 2 是待验证的首轮值。内网模型的最优并发和时限目前不确定。这些设置不能保证合同成功，也不能保证恢复到 20 分钟以内。

## 2. 完整执行链路与数据位置

```text
Web / API 提交合同
  → 保存输入文件、创建业务任务记录
  → 调用公共入队函数，发送 Celery 任务消息到 Redis DB 0
  → 三个 worker 中的空闲执行位置领取任务
  → 根据任务记录运行合同工作流
  → 按要点并发执行
  → 每次模型调用进入统一调度，开始计算模型预算
  → 在 Redis DB 2 申请对应类型的模型名额
  → 向公司内网模型服务发送请求
  → 释放名额，记录模型结果及要点进度
  → 完成合同，记录最终状态、Web 历史及报告
```

| 位置 | 保存内容 | 执行控制由谁负责 |
| --- | --- | --- |
| Redis DB 0 | 待执行的 Celery 消息及消息传输相关记录 | Celery 和 worker |
| Redis DB 2 | 模型名额的成员记录、租约到期时间 | 项目模型控制代码及 Redis Lua |
| 服务共享的数据挂载目录 | 合同、任务 `task.json`、要点进度、日志、报告 | 项目任务存储与工作流 |

DB 0、DB 2 共享同一个 Redis 实例的 CPU、内存和故障范围。分库用于区分职责，不提供独立资源隔离。

Celery 使用 Kombu 传输消息，Kombu 通过 redis-py 操作 Redis；不需要额外部署 Kombu 服务。任务消息携带 `client_dir`、`task_id` 等参数，不携带整份合同文件。

`client_dir` 是数据范围标识。当前 API 任务路径为 `<DATA_DIR>/api/<task_id>/`；Web 标识为 `web:<tenant_id>`，路径为 `<DATA_DIR>/web/<tenant_id>/<task_id>/`。本次不调整路径结构。

### 部署文件从哪里来

在内网部署宿主机维护 `config.yaml` 和 Compose 文件。API 服务容器及三个 worker 容器读取同一份挂载到 `/app/config.yaml` 的配置。合同等持久数据写入各服务共同挂载的 `/app/data` 对应目录。

本地开发目录、用户提供的 Downloads 配置副本、内网宿主机文件和运行中的容器是不同位置。后续修改仓库文件不会自动更新内网服务；部署时必须更新宿主机挂载文件及镜像，并协调重启全部相关服务。

## 3. 并发控制的三个范围

| 范围 | 目标值 | 含义 |
| --- | --- | --- |
| 合同执行 | 三个 worker，各 `--concurrency=1` | 三份合同同时执行，其他合同在 DB 0 等待 |
| 每份合同的要点 | `workflow.max_orchestrator_concurrency=2` | 每份合同同时推进两个要点，三份合计最多六个要点 |
| 模型网络请求 | LLM 5、embedding 3、reranker 10 | 所有 worker 及其工具进程合计的各类请求上限 |

一个要点能够产生多次或多批模型请求，要点数不能直接换算成请求数。模型并发上限是最终的网络请求边界。

实现要求：

1. 所有实际模型请求经过同一个控制模块，包括 Agent、DocxIndex、工具进程和保留的模型适配器。
2. 删除模型客户端中重复的本地请求信号量，不再配置独立的检索模型并发。
3. 保留有界批处理执行，按需提交批次，避免一次创建大量等待任务。工作线程数不再成为第二套用户可配置的模型限额。
4. 等待名额的请求按入队顺序获得可用名额；重试重新排到队尾。
5. 等待调度、获取名额均受本次调用截止时间约束。取消或过期的等待记录必须清除。
6. Redis 连接故障明确失败，不切换到绕过限流的执行方式。
7. 三个 worker 对同一资源使用相同的 Redis DB、key 和并发上限。配置不一致不能靠各自的限额保证统一上限。

## 4. 模型调用的时间预算和重试

### 4.1 时间预算的边界

`workflow.model_call_timeout_seconds` 统一作用于 LLM、embedding、reranker，首轮为 60 秒。

一次调用的范围是一次模型操作及其同一结果的重试或修复。例如，一个 scan 批次的一次判断是一次调用；多个独立批次分别计算预算。Agent 正常进入下一步、下一次业务反思时产生新的调用。

预算从该调用进入模型调度时开始，到得到通过要求校验的结果为止，包含：

```text
本地调度等待 + Redis 名额等待 + HTTP 请求 + 重试退避 + 同一结果的格式/结构修复
```

等待 45 秒后获得名额，只剩 15 秒用于请求、校验和重试。不能再独立获得 60 秒。客户端的连接、读取等超时根据剩余时间设置，同时需要约束整个操作的总时间；仅设置 SDK 的读取超时不能替代总预算。

超时后停止后续尝试，并实际取消本地网络请求。不能只让上层返回，而底层线程仍继续等待或发送。同步 DocxIndex 需要接入可取消的网络执行层，不能用无法停止底层请求的线程等待包装来声称已经完成超时控制。

取消本地 HTTP 连接是否同步停止内网服务端推理，需要通过内网网关验证；客户端断开本身不能证明服务端推理已经停止。

### 4.2 重试次数的边界

`workflow.model_call_max_retries=1` 表示首次发送之外最多再发送一次。网络重试、429/服务错误重试、JSON 或结构校验后的修复发送，共用这一个次数，且不能重置时间预算。

| 情况 | 处理 |
| --- | --- |
| 可重试连接错误、429、可重试服务错误 | 有剩余时间和次数时重试 |
| 输出格式或结构不满足同一调用要求 | 有剩余时间和次数时发送修复请求 |
| 参数错误、模型不存在等明确不可重试错误 | 直接结束该调用 |
| 等待或请求已经耗尽总预算 | 直接结束，不重新创建相同操作以续时 |

统一关闭 SDK 自动重试，删除 scan 整批重试、客户端独立限流重试、独立输出修复次数等叠加机制。失败要点不再自动整要点重跑，已经成功的要点和进度继续保存。

业务上的多步执行、反思和正常路由不合并成一次请求；这些流程也不能把同一失败操作重新命名后当作新调用续时。

## 5. 保留、修改、新增、移除的配置

本节以用户提供的内网配置为比较基准。表中的目标含义需要代码实施后才成立。

### 5.1 `workflow`

| 配置 | 内网当前值 | 决定及目标值 | 作用范围与含义 |
| --- | --- | --- | --- |
| `max_reflection_rounds` | 10 | 保留 10 | 每个要点业务反思轮数上限；本次不调整质量策略 |
| `max_orchestrator_concurrency` | 5 | 修改为 2，首轮验证值 | 每份合同同时推进的要点数 |
| `model_call_timeout_seconds` | 60 | 保留 60，修改语义 | 三类模型每次调用的总预算，包含等待及重试 |
| `model_call_max_retries` | 5 | 修改为 1 | 每次模型操作允许的额外发送次数，覆盖错误重试和结果修复 |
| `subagent_allowed_tools` | `contract_search` | 保留 | 子 Agent 可用工具列表，不控制网络并发和等待时间 |
| `max_api_concurrent_reviews` | 100 | 移除 | Web/API 合同执行数统一由三个 worker 决定 |
| `criterion_retry_max_attempts` | 2 | 移除 | 停止失败后自动重跑整个要点，防止内部调用全部再执行 |

### 5.2 `model_semaphore.<类型>`

类型为 `llm`、`embedding`、`reranker`。限流固定启用，但仍允许通过检索功能配置决定是否需要进行向量检索或重排。

| 配置 | 内网当前值 | 决定 | 作用范围与含义 |
| --- | --- | --- | --- |
| `max_concurrent_requests` | 5 / 3 / 10 | 保留 | 分别是所有执行进程合计的 LLM / embedding / reranker 请求上限 |
| `redis_url` | `redis://redis:6379/2` | 保留 | 共享名额所在的 Redis DB 2 |
| `key` | `contract_agent:model:<类型>:semaphore` | 保留 | 同类请求共享的资源记录名称 |
| `lease_seconds` | 600 | 保留 | 获得名额后，名额记录的有效时间；不是 HTTP 执行时限 |
| `poll_interval_seconds` | 0.2 | 保留 | 等待期间检查可用名额的间隔，不得跨越调用截止时间 |
| `enabled` | true | 移除 | 固定启用共享限流，删除绕过分支 |
| `wait_timeout_seconds` | 600 | 移除 | 等待使用模型调用的剩余预算，不再单独计时 |

租约要求：

- 正常结束或超时并停止请求后，立即释放自己的名额，不等待 600 秒。
- 执行进程崩溃后，残留名额最多保留至租约到期，后续申请清除过期记录。
- 租约必须覆盖允许的模型执行时间和清理余量；启动时进行约束检查，并说明余量规则。
- 租约到期本身不取消模型推理。提前到期会让仍在执行的旧请求与新请求重叠。
- 当前 600 秒可保留，其代价是崩溃后名额回收偏慢。本轮不额外改变该值。

### 5.3 `queue` 与 Celery

| 配置 | 当前状态 | 决定及目标值 | 计时或作用范围 |
| --- | --- | --- | --- |
| `broker_url` | DB 0 | 保留 `redis://redis:6379/0` | 合同任务消息的传输位置 |
| `worker_disable_prefetch` | YAML 省略，项目默认 true | 显式写为 true | 空闲执行位置可用时再取任务，减少提前占住任务 |
| `pending_timeout_seconds` | YAML 省略，项目默认 60 | 显式写为 60 | 从创建任务开始，约束尚未完成入队的等待 |
| `queued_timeout_seconds` | YAML 省略，项目默认 7200 | 显式写为 7200 | 从入队开始，约束尚未被 worker 接手的等待 |
| `watchdog_interval_seconds` | YAML 省略，项目默认 10 | 显式写为 10 | 检查等待状态任务超时的间隔 |
| `task_time_limit_seconds` | 项目没有该字段；Celery 未设置硬时限 | 新增，首轮为 1800 | 从 Celery 接受任务进入执行时开始，限制整份合同执行时间 |
| `enabled` | true | 移除 | Web/API 固定使用 Celery，删除直接执行的替代分支 |

不新增 `task_soft_time_limit_seconds`。目标配置中不出现该字段，也不保留 `null`。Celery 原生软时限保持未启用；启动审计应显示它未启用，避免误以为存在第二个执行时限。

7200 秒仅限制等待 worker 的阶段，不终止已经运行的合同。watchdog 周期检查会带来检查延迟，不能宣称它在第 7200 秒精确取消消息。等待超时首先更新业务状态；旧消息随后被取出时，worker 根据任务状态跳过执行。

整任务硬时限 1800 秒包括文件处理、索引、工具、模型等待与请求、报告生成等执行过程；不包括之前在 DB 0 排队的时间。到期后强制终止执行子进程，Celery 补充执行进程处理后续任务，不因此自动重跑原合同。

模型调用同时服从任务截止时间：

```text
调用有效截止时间 = min(调用开始时间 + 模型预算, 合同执行截止时间)
```

合同已执行 29 分 50 秒时新建的模型调用只剩约 10 秒，不能再独立延长合同执行时间。任务截止时间必须传递到工具进程及网络执行层。

### 5.4 检索中的重复配置

| 配置或机制 | 当前值或行为 | 决定 |
| --- | --- | --- |
| `retrieval.docxindex.concurrency.llm` | 5 | 移除，模型发送统一走 LLM 全局调度 |
| `retrieval.docxindex.concurrency.embedding` | 1 | 移除，模型发送统一走 embedding 全局调度 |
| `retrieval.docxindex.concurrency.reranker` | 10 | 移除，独立重排请求统一走 reranker 全局调度 |
| `retrieval.docxindex.indexing.hierarchy.retry_count` | 3 | 移除，修复请求共用模型重试次数和原预算 |
| DocxIndex LLM / embedding 内部 HTTP 时限 | 默认 120 秒 | 删除独立时限来源，根据统一预算设置网络超时 |
| DocxIndex 限流错误额外重试 | 最多额外 4 次 | 删除独立次数，合并到统一重试 |
| `complete_model()` 独立校验重试 | 默认额外 2 次 | 删除独立次数，合并到统一重试 |
| scan 整批异常重试 | 最多执行 2 次 | 删除该层重复重试 |

保留 `retrieval.docxindex.rerank.enabled`、`vector.enabled` 等功能选择。这些开关决定是否执行相应功能，不允许已经执行的模型请求绕过全局限制。

### 5.5 本轮不改变的关联配置

以下分组保持原有配置值和职责。本次实施必须确认它们仍被原流程正确使用。

| 配置 | 保留的作用 |
| --- | --- |
| `llm.base_url/name`、`embedding.base_url/name`、`rerank.base_url/name` | 选择公司内网模型地址及模型 |
| `rerank.instruct/provider_model_names` | 重排指令及接口协议适配 |
| `llm.enable_thinking/temperature/top_p/seed` | LLM 请求参数 |
| `llm.max_context_tokens/max_prompt_tokens/max_result_tokens` | 保留当前上下文、提示和结果处理逻辑；本轮不重新定义其含义 |
| `llm.max_tool_calls`、`embedding.max_input_tokens` | 工具调用次数及 embedding 输入约束 |
| `retrieval.backend` | 保留当前 `docxindex` 检索后端 |
| `retrieval.docxindex.input_tokens/output_tokens/scan_batch_tokens` | 检索输入、返回证据和扫描分批预算 |
| `retrieval.docxindex.vector.score_threshold/probe_limit` | 向量候选阈值和数量 |
| `retrieval.docxindex.semantic.*` | 结构候选数和最终节点数 |
| `retrieval.docxindex.routing.*` | 检索路由说明及正常的回退选择 |
| `retrieval.docxindex.indexing.*`，除 `hierarchy.retry_count` | 标题规则、分块、层级深度和索引增强功能 |
| `retrieval.llamaindex.*` | 保留该后端参数，网络调用接入统一控制 |
| `api.*` | 保留认证、元信息、输入输出、结果上传及下载等设置 |
| `docx.*`、`logging.*` | 保留文档批注和日志设置 |

API 的 URL 下载超时、结果上传超时是文件传输超时，不使用模型调用预算。`retrieval.docxindex.output_tokens` 是检索返回内容预算，不是模型生成输出上限。

## 6. reranker 的修复范围

当前 DocxIndex 重排在每个批次临时创建信号量，实际调用共享 LLM 客户端。它不能形成独立 reranker 的共享并发限制。

目标行为：

| 操作 | 模型来源 | Redis DB 2 名额 |
| --- | --- | --- |
| 审查、总结、检索中的 LLM 判断 | `llm` | `llm` |
| 向量生成 | `embedding` | `embedding` |
| 候选片段重排 | `rerank` 中配置的内网 `Qwen3-Reranker-8B` | `reranker` |

实施时复用已有协议适配的请求构造和分数解析，网络执行统一接入公共模型控制。删除批次临时信号量，删除适配器自有的独立重试和超时控制。

此项会把当前 LLM 排序改成独立重排模型打分，需要检查内网 `/v1/rerank` 请求协议、返回索引映射、排序结果和质量。失败时记录独立 reranker 错误，不静默切回 LLM 继续执行。若存在旧重排缓存，需要区分算法或模型版本。

## 7. 仍需显式说明的内部值

这些值不全部增加为用户可编辑配置，但必须在文档及启动审计中说明。

| 内部设置 | 当前值 | 方案与含义 |
| --- | --- | --- |
| worker 数量 / 每个执行并发 | 3 / 1 | 保留；Compose 明确 `prefork` |
| Celery `worker_prefetch_multiplier` | 1 | 保留项目固定值 |
| Celery `task_acks_late` | false | 保留并显式固定、输出；任务被执行进程接手时确认消息 |
| Celery `task_soft_time_limit` | 未设置 | 保持未启用；不增加项目配置字段 |
| Redis transport `visibility_timeout` | 3600 秒 | 保留并显式固定、输出；用于未确认消息恢复，不是合同执行时限 |
| 模型信号量整个 key 的过期时间 | `lease_seconds + 60` 秒 | 保留规则；成员租约与整个 key 过期不同 |
| 业务 worker 心跳间隔 | 10 秒 | 保留；它是状态更新频率，不是模型或合同执行时限 |
| 任务 JSON 文件锁 | 等待 30 秒、陈旧阈值 300 秒、检查间隔 0.05 秒 | 保留；用于共享文件写入，不参与模型预算 |

不新增整合同自动重试或失败自动重投。业务成功与否以项目任务状态为准，不能仅以 Celery 包装函数返回 `finished` 判断。

## 8. 三个 worker 同时工作的示例

合同 A、B、C 被三个 worker 接手，合同 D 留在 DB 0。每份合同同时推进两个要点。

如果六个调用同时申请 LLM 名额，最多五个发送，第六个等待。第六个已等待 45 秒时获得名额，只能再使用剩余 15 秒。它超时后不自动重跑整个要点；其他请求继续使用各自原有的预算。

一个请求释放名额，只影响模型请求的等待。只有整份合同结束、退出或被终止后，该 worker 才能接手下一份合同。

如果 A 触发整任务硬时限，终止 A 的执行子进程并记录 A 失败，补充执行进程处理后续合同。B、C 的计时不会重置或失效。A 残留名额会影响其他请求获得名额的速度，直到主动释放或租约回收；这属于共享容量变化，不是其他任务配置失效。

## 9. 任务状态、清理和日志

不以软时限为前提。正常结束、异常结束继续执行可完成的清理；硬终止依靠外部状态处理和租约兜底。

- worker 父进程侧处理任务硬超时，写入明确的业务失败状态，不能依赖被终止子进程一定执行 `finally`。
- 识别并处理关联工具进程，验证子进程退出后不会留下继续执行本任务的工具工作。
- 保存已经写入的成功要点和失败原因。硬终止不能承诺保存尚未落盘的结果。
- 临时文件允许后续清理，不把删除所有临时文件作为任务结束的前置条件。
- 改造启动核对：Web/API 服务重启不等于 worker 中的任务已经中断，不能一律将活跃任务标记失败。
- Web 队列化时迁移历史记录更新逻辑，保持历史、结果展示和下载行为。

每次模型操作至少记录：业务任务编号、worker、调用编号、模型类型和实际模型、等待耗时、请求耗时、退避耗时、总耗时、剩余预算、各次尝试及原因、终止阶段、每次返回的 token usage。

没有 usage 的失败请求记录未知值，不记录为零。操作汇总与逐次发送区分，防止只统计最后一次成功返回而遗漏重试成本。基础统计不应依赖开启完整请求正文日志。

## 10. 目标配置片段

以下片段仅包含本次控制方案涉及的目标配置。其余保留分组按现有内网配置补充。当前版本不能直接使用该片段替换配置。

```yaml
workflow:
  max_reflection_rounds: 10
  max_orchestrator_concurrency: 2
  model_call_timeout_seconds: 60
  model_call_max_retries: 1
  subagent_allowed_tools:
    - "contract_search"

queue:
  broker_url: "redis://redis:6379/0"
  worker_disable_prefetch: true
  pending_timeout_seconds: 60
  queued_timeout_seconds: 7200
  watchdog_interval_seconds: 10
  task_time_limit_seconds: 1800

model_semaphore:
  llm:
    max_concurrent_requests: 5
    redis_url: "redis://redis:6379/2"
    key: "contract_agent:model:llm:semaphore"
    lease_seconds: 600
    poll_interval_seconds: 0.2
  embedding:
    max_concurrent_requests: 3
    redis_url: "redis://redis:6379/2"
    key: "contract_agent:model:embedding:semaphore"
    lease_seconds: 600
    poll_interval_seconds: 0.2
  reranker:
    max_concurrent_requests: 10
    redis_url: "redis://redis:6379/2"
    key: "contract_agent:model:reranker:semaphore"
    lease_seconds: 600
    poll_interval_seconds: 0.2
```

## 11. 配置迁移及实施顺序

1. 方案审阅后，按项目要求在代码修改前提交相应快照，不混入无关改动。
2. 建立统一模型控制接口，覆盖同步与异步调用、工具进程、截止时间传递、实际取消和共享调度。
3. 收拢重试及失败传播，移除重复模型限流和整要点自动重跑。
4. 接入独立 reranker 并验证协议和排序。
5. Web/API 统一入队，迁移 Web 完成处理，移除 OA。
6. 接入 Celery 硬时限及父进程任务状态处理，修正启动核对。
7. 同步修改配置读取、`config.yaml`、示例配置、Compose、部署及接口文档；将本文从方案状态更新为实际生效说明。
8. 同时检查本地旧共享格式 `model_semaphore.limits`、内网逐类型格式、旧 `llm.global_semaphore_*` 的迁移，不能让旧字段继续覆盖新策略。被移除字段明确报迁移信息，不静默忽略；冲突配置拒绝启动。
9. 部署前结束或排空旧版本任务，避免不同策略的 worker 同时使用同一组名额。更新挂载配置与镜像后协调重启服务，检查各服务启动审计一致。
10. 完成适当验证后，按项目要求提交代码修改后的 commit，并说明实现和验证结果。

## 12. 验收标准

| 验证 | 通过条件 |
| --- | --- |
| 两个入口及 OA 移除 | Web/API 都通过 DB 0；OA 不再注册；Web 历史及下载保持正常 |
| 三个独立 worker | 最多同时执行三份合同，其他任务等待；每份要点并发符合配置 |
| 模型共享上限 | 实际发出的三类请求分别遵守 5 / 3 / 10；DocxIndex 重排使用独立 reranker |
| 等待、错误和修复 | 等待、请求、退避、校验修复共用时间预算及重试次数 |
| 慢响应及取消 | 慢速持续返回也不能突破总预算；上层结束时本地网络请求已停止 |
| 单请求超时 | 不重置其他调用的时间，不错误删除其他请求名额 |
| 硬终止及崩溃 | 任务状态能收敛为失败、执行进程补充、残留名额最终回收、旧任务不自动重跑 |
| 队列等待超时 | 仅影响尚未接手任务；过期消息之后被领取也不执行 |
| 配置一致性 | 旧字段有迁移提示；同类资源共享相同 DB、key、上限；所有运行服务输出有效设置 |
| 内网效果复测 | 同批合同比较成功率、完整性、总耗时、等待和重试；再决定 60 秒是否调整为 30 秒 |

共享并发及取消验证使用真实 Redis 和可控制响应的测试 HTTP 服务，覆盖三个独立执行进程。Celery 硬时限在与内网一致的 Linux/prefork 容器条件下验证，不以 Windows 开发机行为代替部署验证。

## 13. 当前代码依据（实施前）

以下引用只说明本次计划的来源，不代表目标设计已完成。

| 位置 | 当前行为 |
| --- | --- |
| [app.py:36](C:/Users/lenovo/Desktop/contract_agent/app.py:36) | 注册 OA 路由 |
| [app.py:43](C:/Users/lenovo/Desktop/contract_agent/app.py:43) | 启动时核对中断任务 |
| [user_routes.py:285](C:/Users/lenovo/Desktop/contract_agent/endpoints/web/user_routes.py:285) | Web 执行包装及历史更新 |
| [user_routes.py:738](C:/Users/lenovo/Desktop/contract_agent/endpoints/web/user_routes.py:738) | Web 直接创建执行任务 |
| [review_jobs.py:688](C:/Users/lenovo/Desktop/contract_agent/endpoints/api/review_jobs.py:688) | API 根据队列开关选择执行方式 |
| [review_queue.py:6](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_queue.py:6) | 提交 Celery 消息 |
| [review_tasks.py:9](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_tasks.py:9) | worker 执行公共审查函数 |
| [celery_app.py:14](C:/Users/lenovo/Desktop/contract_agent/task_queue/celery_app.py:14) | Celery 预取等配置，未设置任务执行时限 |
| [config.py:266](C:/Users/lenovo/Desktop/contract_agent/config.py:266) | 模型限流参数读取 |
| [config.py:379](C:/Users/lenovo/Desktop/contract_agent/config.py:379) | 队列设置和 60 / 7200 / 10 默认值 |
| [llm_semaphore.py:46](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_semaphore.py:46) | Redis 原子申请名额 Lua |
| [llm_semaphore.py:110](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_semaphore.py:110) | 独立等待时限、租约和轮询 |
| [base_agent.py:107](C:/Users/lenovo/Desktop/contract_agent/agents/base_agent.py:107) | Agent SDK 时限 |
| [orchestrator.py:1323](C:/Users/lenovo/Desktop/contract_agent/agents/orchestrator.py:1323) | 要点并发与自动恢复次数 |
| [llm/client.py:87](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/llm/client.py:87) | DocxIndex LLM 独立时限及本地限流 |
| [vector/client.py:59](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/vector/client.py:59) | embedding 独立时限及本地限流 |
| [rerank.py:59](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/retrieval/rerank.py:59) | 批次内临时信号量 |
| [retriever.py:360](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/retriever.py:360) | 重排使用共享 LLM 客户端 |
| [task_store.py:743](C:/Users/lenovo/Desktop/contract_agent/endpoints/review/task_store.py:743) | 等待超时只处理尚未接手任务 |

