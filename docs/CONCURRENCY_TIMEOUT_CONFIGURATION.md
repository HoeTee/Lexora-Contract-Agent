# 并发、超时与重试配置说明

更新日期：2026-10-16。状态：本次代码已经实现；尚未部署到公司内网，尚未复测内网模型的耗时和审查质量。

本文说明合同任务、模型调用和 Redis 名额的配置，列出旧配置的迁移方式。配置解释以当前代码为准。地址、凭据和模型名称继续使用各部署自己的设置，不把本地开发机的模型配置套用到内网。

## 1. 运行结构

```text
Web 提交 / HTTP API 提交（CLI 也作为 HTTP API 客户）
  → 保存合同和业务任务记录
  → 公共入队函数发送 Celery 消息到 Redis DB 0
  → 三个独立 worker，各自 concurrency=1、pool=prefork
  → 每份合同同时推进最多两个要点
  → 模型请求统一调度，在 Redis DB 2 申请对应名额
  → 调用公司内网 LLM、embedding 或独立 reranker
  → 释放名额，保存要点进度
  → 保存报告、业务任务状态及 Web 历史
```

- 后端合同执行只从 Web/API 入队，OA 入口及队列旁路已移除。
- 三个 worker 合计最多同时执行三份合同；第四份合同继续排队。
- Web 的登录、管理、历史、状态展示和下载保留。历史更新在 worker 完成流程中执行。
- CLI 不再在本地直接运行工作流，而是向已运行的 API 提交合同。
- Web/API 不承担合同执行；消息提交失败返回失败，不切换到进程内运行。

| 存储 | 数据 | 控制方 |
| --- | --- | --- |
| Redis DB 0 | 待领取任务消息、路由绑定和消息确认记录 | Celery，通过 Kombu 和 redis-py 传输 |
| Redis DB 2 | 模型名额、等待顺序、等待截止时间和共享容量策略 | 项目模型控制代码及 Redis Lua |
| 共享数据挂载目录 | 输入文件、`task.json`、要点进度、日志、报告 | 项目工作流和任务存储 |

DB 0、DB 2 是同一个 Redis 服务内的逻辑数据库，共享 CPU、内存和故障范围。分库便于区分职责，不提供独立资源隔离。Kombu 是 Celery 的依赖库，不需要单独部署服务。

任务消息携带 `client_dir` 和 `task_id`，不携带整份合同。`client_dir` 是数据范围标识：API 任务目前位于 `<DATA_DIR>/api/<task_id>/`；Web 标识 `web:<tenant_id>` 对应 `<DATA_DIR>/web/<tenant_id>/<task_id>/`。本次不调整数据目录结构。

## 2. 配置文件在哪里、什么时候生效

部署宿主机维护 `config.yaml`。API 服务和三个 worker 挂载同一份文件到 `/app/config.yaml`，并共用 `/app/data`、租户配置和审查规则的挂载目录。

`config.py` 读取配置并校验；工具进程继承任务截止时间，使用同一部署的模型控制配置。配置在进程加载时读取，没有热更新。更新后需要协调重启 API 和全部 worker，新启动的工具进程才会使用更新后的设置。

仓库的 `config.example.yaml`、`docker-compose.example.yaml` 是提交到版本库的模板。真实的 `config.yaml`、`docker-compose.yaml` 在 `.gitignore` 中。本次已更新开发目录的真实控制配置，但不会因此更新内网宿主机文件或运行镜像。

启动日志的 `[config] execution-control` 输出有效配置和下文列出的内部值。Redis 地址中的用户名、密码被去除。`deployment_contract` 表示部署预期为三个 worker、每个并发 1；实际实例数仍由 Compose/启动命令决定。

## 3. 并发配置

### 3.1 三个层级

| 层级 | 当前基准值 | 范围 |
| --- | --- | --- |
| 合同执行 | 3 个 worker，各 `--concurrency=1 --pool=prefork` | 最多三份合同同时执行 |
| 合同要点 | `workflow.max_orchestrator_concurrency: 2` | 每份合同同时推进两个要点，三份合计最多六个 |
| 模型网络请求 | LLM 5、embedding 3、reranker 10 | 所有 worker、工具进程共用各类型的请求上限 |

一个要点可以产生多个模型请求，六个要点不等于六个请求。模型控制模块约束最终实际发送的请求。

### 3.2 `workflow`

| 配置 | 本次值或决定 | 精确含义 |
| --- | --- | --- |
| `max_orchestrator_concurrency` | 5 改为 2 | 每份合同同时推进的要点数；2 是首轮验证值 |
| `max_reflection_rounds` | 内网值 10 保留 | 每个要点的业务反思轮数，不是网络错误重试次数 |
| `subagent_allowed_tools` | 保留 `contract_search` | 子 Agent 的可用工具列表，不控制网络并发 |
| `max_api_concurrent_reviews` | 移除 | 合同执行数量统一由三个 worker 决定 |
| `criterion_retry_max_attempts` | 移除 | 失败要点不自动整要点重跑；保留已写入的成功结果和进度 |

### 3.3 `model_semaphore.<类型>`

类型是 `llm`、`embedding`、`reranker`。

| 配置 | 当前基准值 | 含义及约束 |
| --- | --- | --- |
| `max_concurrent_requests` | 5 / 3 / 10 | 各类型全部执行进程合计的请求上限，必须为正整数 |
| `redis_url` | `redis://redis:6379/2` | 模型共享名额所在的 Redis DB 2 |
| `key` | `contract_agent:model:<类型>:semaphore` | 同类请求共享同一 key；三种类型使用不同 key |
| `lease_seconds` | 600 | 获得名额后，记录的租约有效时间；不是 HTTP 执行时限 |
| `poll_interval_seconds` | 0.2 | 等待名额时再次检查的间隔，必须大于 0，且受调用截止时间约束 |
| `enabled` | 移除 | 共享限流固定启用 |
| `wait_timeout_seconds` | 移除 | 等待名额消耗模型操作的剩余时间，不再单独计时 |

同一资源的 worker 必须使用相同地址、key、上限和租约。Redis 中的策略检查拒绝仍有持有者或等待者时混用不同上限/租约；没有持有者和等待者时才允许更换策略。

等待按入队顺序分配名额；重试释放名额后重新排队。取消、超时的等待记录被清理。Redis 连接失败明确报错，不绕过全局限流。

租约规则：

- 正常请求结束，或者本地请求已经取消后，主动释放自己的名额，不继续等满 600 秒。
- worker 硬终止来不及释放时，记录最多残留至租约到期；后续申请清除过期记录。
- 启动校验要求 `lease_seconds >= model_call_timeout_seconds + 5`。5 秒是内部清理余量，不增加模型请求预算。
- 600 秒的代价是异常退出后名额回收偏慢；它不会把正常请求执行时间延长为十分钟。
- 租约到期不停止模型推理，过短的租约会让旧请求未结束时就放入新请求。

每个基础 key 还使用 `:waiting`、`:deadlines`、`:sequence`、`:policy` 辅助 key。Lua 原子清理过期记录、校验策略、记录等待顺序并分配名额，避免多个 worker 同时越过上限。

## 4. 统一模型超时与重试

| 配置 | 本次值或决定 | 含义 |
| --- | --- | --- |
| `workflow.model_call_timeout_seconds` | 保留 60，统一语义 | 一次模型操作的总时间预算，作用于 LLM、embedding、reranker |
| `workflow.model_call_max_retries` | 5 改为 1 | 首次发送之外最多再发送一次；网络重试、输出修复共用次数 |

一次操作从进入模型调度开始，包含：

```text
调度和名额等待 + HTTP 请求 + 重试退避 + 同一结果的校验和修复
```

等待 45 秒后获得名额，只剩 15 秒。第一次请求已经用完时间，就不能重试。网络错误和输出错误不能分别再获得一份次数或时间预算。

一个 scan 批次的一次判断是一次模型操作；不同独立批次分别计时。Agent 正常进入下一步、工具结果续写或业务反思时产生新的操作。同一结果的 JSON/结构修复继续使用原操作预算。正常业务步骤不等于网络重试，失败后也不能通过重跑整个要点来重置预算。

可重试连接错误、408/409/429、可重试服务错误和输出校验错误，在剩余时间及次数允许时重试。明确的参数错误等直接结束。`Retry-After` 等待也在预算内。

可选的层级增强若耗尽输出校验修复次数，保留原有确定性分块/标题回退，不再发送请求；网络、时间和 Redis 错误继续向上报告。

SDK 自带重试关闭。DocxIndex 和保留的 LlamaIndex 网络调用全部接入公共模块。同步调用通过常驻异步网络线程等待实际可取消的协程，不使用无法停止同步 HTTP 的 `to_thread` 包装来冒充超时控制。批处理仍有内部有界调度，但没有第二套用户配置的模型请求限额。

截止后不再发送模型请求。释放 Redis 名额的退出清理单独最多等待 2 秒；清理失败依靠租约回收，不获得新的模型执行时间。

LlamaIndex 当前业务不使用流式请求。其流式入口明确报不支持，防止继承方法绕过控制。

取消本地 HTTP 是否同步停止公司内网服务端推理，尚未验证。不能仅凭客户端连接关闭认定服务端已经停止推理。

## 5. Celery、队列等待及整任务硬时限

| `queue` 配置 | 当前基准值 | 起点及作用 |
| --- | --- | --- |
| `broker_url` | `redis://redis:6379/0` | Celery 任务消息使用 DB 0 |
| `worker_disable_prefetch` | true | 有空闲执行位置时再取任务，减少提前占住任务 |
| `pending_timeout_seconds` | 60 | 从任务创建开始，限制尚未完成入队的等待 |
| `queued_timeout_seconds` | 7200 | 从入队开始，限制尚未被 worker 接手的等待 |
| `watchdog_interval_seconds` | 10 | 等待超时和执行截止状态的周期核对间隔 |
| `task_time_limit_seconds` | 新增 1800 | Celery 从任务被执行进程接受时计时，限制整份合同执行时间 |
| `enabled` | 移除 | Web/API 固定入队，不再支持直接执行旁路 |

软时限从项目配置和实现方案中移除，不提供 `task_soft_time_limit_seconds`，也不保留 `null` 占位。Celery 库内部显式保持软时限未启用。

**7200 秒只控制等待 worker 的阶段，不终止已运行的合同。**周期检查存在调度延迟，不能宣称精确在第 7200 秒删除消息。过期任务先标记为失败，旧消息之后被领取也不会执行。

1800 秒包括任务执行中的文件处理、索引、工具、模型等待与请求、报告生成等，不包括此前的队列等待。达到硬时限后，Celery 终止执行子进程，补充执行进程处理后续任务，不自动重跑原合同。

任务执行函数开始时还建立并传递任务截止时间到工具进程；Celery 原生硬时限是最终的进程强制边界。模型调用使用两者中更早的截止时间：

```text
模型调用截止时间 = min(调用开始时间 + 60 秒, 合同执行截止时间)
```

合同已运行 29 分 50 秒时新建的调用只剩约 10 秒，不能再独立延长合同。

### 三个 worker 同时运行的过程

A、B、C 三份合同分别被三个 worker 接手，D 在 DB 0 等待。如果 A、B、C 合计六个调用申请 LLM 名额，最多五个发送，第六个继续消耗自己的时间预算等待。

单个模型请求完成只释放 DB 2 名额，合同仍继续执行。合同结束或退出后，worker 才接手下一份合同。

如果 A 硬超时，只终止 A 的执行子进程并记录 A 失败。B、C 的计时和配置不重置。A 的残留名额会影响可用容量，直到释放或租约回收；这是共享容量变化，不是其他配置失效。

### 清理与失败状态

正常退出继续清理心跳、临时输入和名额，不依赖软时限。硬终止不能保证执行 `finally`，由父进程回调写业务失败状态；执行进程异常退出记为中断。Linux 工具进程设置父进程退出保护，父进程失败处理也会终止本任务关联工具。

父回调缺失时，watchdog 对已记录的任务截止时间增加 5 秒核对余量，最终将过期活跃任务记为失败。它不是第二个执行宽限时限，也不负责继续执行任务。API 重启不会无差别把所有活跃 worker 任务标记失败。

任务状态写入使用操作系统文件锁，硬杀释放锁；心跳和并发要点更新在锁内读取，避免覆盖其他要点或终态。晚到的失败回调不能覆盖已完成任务，也不能误杀复用执行进程中的新工具。

## 6. 独立 reranker

DocxIndex 原先使用 LLM 输出排序，并在每个批次创建临时信号量。现在使用 `rerank` 中配置的独立模型，例如内网 `Qwen3-Reranker-8B`，使用 `reranker` 名额。

协议保留 Higress Qwen、BGE、DashScope Qwen 适配。校验返回结果数、索引范围、唯一性和分数。协议/JSON 错误使用统一重试次数；失败明确报错，不切回 LLM 偷跑一次。重排缓存使用独立版本，避免复用旧 LLM 排序缓存。

此项改变排序方式，需要在内网验证效果。网络和协议测试通过不代表真实审查质量已经验证。

## 7. 移除配置清单及不变配置

必须删除以下旧字段，不能靠保留旧值继续启动：

```text
workflow.max_api_concurrent_reviews
workflow.criterion_retry_max_attempts
queue.enabled
queue.task_soft_time_limit_seconds（此前仅方案中出现，不再实现）
model_semaphore.<类型>.enabled
model_semaphore.<类型>.wait_timeout_seconds
retrieval.docxindex.concurrency.*
retrieval.docxindex.indexing.hierarchy.retry_count
```

旧共享格式 `model_semaphore.limits`、共享的 Redis/lease 等字段需要迁到逐类型结构；旧 `llm.global_semaphore_*` 和 `llm.max_global_concurrent_requests` 不再接收。单独的模型 timeout、rerank 重试退避字段也不再接收。配置加载明确报迁移错误，防止旧字段被静默忽略。

已移除内部独立次数：DocxIndex 限流错误额外重试、`complete_model()` 独立修复次数、scan 整批重试、整要点失败自动重跑。HTTP 时限统一从剩余预算派生，不再沿用 DocxIndex 默认 120 秒。

| 保留配置 | 作用边界 |
| --- | --- |
| `llm/embedding/rerank` 的地址、模型名和凭据来源 | 保留部署自己的模型服务选择 |
| `rerank.instruct/provider_model_names` | 独立重排请求和协议适配 |
| `llm.enable_thinking/temperature/top_p/seed` | 保留模型请求参数 |
| `llm.max_context_tokens/max_prompt_tokens/max_result_tokens` | 保留当前上下文、提示、结果处理逻辑，本轮不重新定义 |
| `llm.max_tool_calls`、`embedding.max_input_tokens` | 保留工具调用次数及输入约束 |
| 检索 token、分块、候选数、阈值、路由、索引增强 | 除明确移除项外保留原值及算法职责 |
| `retrieval.docxindex.vector.enabled/rerank.enabled` | 决定是否执行相应功能，不允许已执行的请求绕过限流 |
| `api.*` | 保留认证、元信息、输入输出、上传和下载行为 |
| `docx.*`、`logging.*` | 保留批注和日志设置 |

`retrieval.docxindex.output_tokens` 是检索返回证据预算，不是模型生成上限。API 文件下载、结果上传和 CLI 客户端等待时限控制各自边界 I/O，不是模型执行时限。

## 8. 需要知道的内部值

以下值在代码中固定，并通过启动审计或本节说明，不全部增加为可编辑配置。

| 设置 | 值 | 含义 |
| --- | --- | --- |
| worker 数量及单个执行并发 | 3 / 1 | Compose 约定，进程池 prefork |
| `worker_prefetch_multiplier` | 1 | Celery 预取策略 |
| `task_acks_late` | false | 执行进程接手后确认消息，不等于合同成功 |
| `task_reject_on_worker_lost` | false | 不因执行进程丢失自动重投原合同 |
| Celery 原生软时限 | 未启用 | 没有项目级软时限字段 |
| Redis transport `visibility_timeout` | 3600 秒 | 未确认消息恢复机制，不是合同执行时限 |
| 信号量 key 的 TTL | `int(lease_seconds) + 60` 秒 | 整个 key 的清理规则；与成员租约不同 |
| Redis 连接及操作时限 | 2 秒 | 基础连接故障边界，仍服从调用总截止时间 |
| Redis 退出清理时限 | 2 秒 | 请求已结束后的释放动作，失败交给租约 |
| 租约最小清理余量 | 5 秒 | 租约至少比模型预算长 5 秒 |
| worker 心跳间隔 | 10 秒 | 状态更新频率，不是任务执行时限 |
| 任务文件锁等待/检查间隔 | 30 秒 / 0.05 秒 | 共享任务文件写入；锁由 OS 持有，无旧版 300 秒陈旧锁等待 |
| 截止状态核对余量 | 5 秒 | 父回调缺失时的状态兜底，加上 watchdog 周期发现 |

## 9. 控制配置完整片段

该片段对应本次实现。部署时保留原有模型、检索、API 等其他分组，并删除第 7 节的旧字段。

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

## 10. 日志统计

每次逻辑操作记录多次实际发送的 `attempts`，包括模型类型、HTTP 状态、阶段、等待/请求/退避/总耗时、预算、原因及 token usage。普通日志模式也保留基础统计，完整请求正文仍由详细日志控制。

- `input_tokens/output_tokens/input_output_tokens` 保留已有最终结果字段语义，不把它们当成全部重试成本。
- `known_input_tokens/known_output_tokens` 汇总已知的逐次 usage；未知部分不自动计为零。
- `input_usage_complete/output_usage_complete/usage_complete` 表示统计是否完整。LLM 缺输出 usage 时不能标为完整；embedding 不要求输出 token。
- HTTP 已成功但释放 Redis 失败时，`attempts.http_status` 保留已经收到的 HTTP 状态及 usage，标记 `semaphore_release`，不重复发送。此时 Redis 错误本身没有 HTTP 状态，`error.http_status` 可以为未知，不能把两者混淆。
- 业务成功与失败读取 `task.json`。Celery 函数正常返回只说明消息处理函数返回，不能替代业务成功判断。

## 11. 部署迁移

1. 等待或排空旧 worker 的任务后再升级，不混用不同模型控制策略的版本。
2. 在内网宿主机迁移挂载的 `config.yaml`；依据第 7 节删除旧字段，保留内网地址、凭据和算法配置。
3. 使用包含本次代码的新镜像，同时更新 API、三个 worker 和配套挂载。
4. 检查各服务的 `[config] execution-control`，确认 DB 0/DB 2、各上限、预算、重试及硬时限一致，并确认三个 prefork worker 每个并发 1。
5. 通过 Web/API 做提交、状态查询、历史和下载验证，再用同批合同复测成功率、完整性、等待、重试和总耗时。

旧版活跃任务没有 `execution_deadline`，不能凭 API 重启判断它们已失败。升级前需排空或人工核对旧任务。不能通过清空 DB 0/DB 2 代替状态核对和协调升级。

### CLI 客户端的等待设置

CLI 仍通过 HTTP API 使用相同队列，不导入本地工作流或模型配置。当前 CLI 不提供请求级模型凭据参数，需要服务端 `api.require_request_model_config=false`。`API_KEY` 环境变量优先于 `--api-key`，服务地址通过 `--base-url` 或 `REVIEW_API_URL` 指定；元信息使用 `--template-code`、`--serial-no`。

| 设置 | 默认值 | 边界 |
| --- | --- | --- |
| `--wait-timeout-seconds` | 9000 秒，按 7200 队列等待 + 1800 执行基准设置 | 仅限制客户端从上传到下载完成的等待；需要额外传输余量时显式增大 |
| CLI 单次 HTTP I/O 超时 | 60 秒 | 上传、查询、下载的单次网络边界，不是模型预算 |
| CLI 状态查询间隔 | 2 秒 | 控制客户端查询频率 |

CLI 等待到期不会取消服务端合同，也不会改变 Celery 时限；之后可以用原任务编号继续查询。只有任务成功才下载报告，下载完成前使用临时文件，失败不覆盖已有输出。

## 12. 验证及边界

本次在本地测试服务和隔离 Redis 上验证控制逻辑，不请求公司内网模型。

| 验证 | 已验证内容 |
| --- | --- |
| 现有单元测试和新增回归 | 原功能、两入口队列化、状态竞态、共享重试、逐次 token 统计及迁移错误 |
| 三个独立进程 + 真实 Redis + 本地 HTTP | LLM 5、embedding 3、reranker 10 的共享上限和名额最终释放 |
| 等待、退避、持续慢响应 | 不重置预算，截止后实际断开本地 HTTP，取消等待不释放他人名额 |
| 同步/异步模型适配器 | SDK 解析、类型选择、嵌套预算、任务截止时间继承及底层请求取消 |
| Linux 镜像中的真实 Celery prefork | 硬时限和持有文件锁时 SIGKILL；业务失败状态、工具停止、执行进程补充和下一任务成功 |
| 静态检查和 Compose | 代码编译、Ruff、示例 Compose 结构验证 |

Linux 集成使用与内网一致的 Celery/Kombu/redis-py 版本及当前源码，测试将合同执行替换为可控工作，不使用真实模型推理。它证明进程控制和状态处理有效，不证明内网模型吞吐或排序质量。

60 秒模型预算、30 分钟硬上限、要点并发 2 仍是首轮参数。是否需要将模型预算调整回 30 秒、并发上限是否符合模型服务能力，目前不确定，需要内网同批合同复测。

## 13. 本次验证记录

- 正式回归集：236 项，235 项通过；默认跳过的 Linux 专用测试已在容器中单独执行通过。该次回归设置 `TEST_MODEL_REDIS_URL`，包含真实 Redis/HTTP 的 6 项集成场景。
- 消融兼容离线测试：22 项通过，无真实模型请求。
- Linux Celery 集成：真实 prefork、Request 回调、2 秒硬时限。任务持有状态写锁时约 2.226 秒记录超时；SIGKILL、工具停止、执行进程补充及下一任务成功均通过。
- Ruff、Python 编译和示例 Compose 配置检查通过。内网真实模型的质量及 20 分钟目标尚未复测。

集成测试使用隔离临时 Redis，不操作原有部署服务。测试文件分别为 `test_model_control.py`、`test_celery_hard_timeout_linux.py`；后者需要 Linux/prefork 与显式隔离 broker。

## 14. 代码入口

| 文件与入口 | 输入 → 输出或作用 |
| --- | --- |
| [config.py](C:/Users/lenovo/Desktop/contract_agent/config.py:244) | 配置字典 → 拒绝旧字段和冲突格式 |
| [config.py](C:/Users/lenovo/Desktop/contract_agent/config.py:468) | 已加载配置 → 不含 Redis 密码的运行审计 |
| [task_queue/review_queue.py](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_queue.py:9) | client_dir、task_id → 持久化消息编号并入 DB 0 |
| [task_queue/review_tasks.py](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_tasks.py:21) | 任务参数 → worker 执行及真实业务状态 |
| [task_queue/review_request.py](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_request.py:39) | Celery 超时/进程失败事件 → 父进程业务失败处理 |
| [endpoints/runtime/model_budget.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/model_budget.py:63) | 统一预算与任务截止时间 → 剩余时间及发送额度 |
| [endpoints/runtime/model_calls.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/model_calls.py:49) | 模型类型、发送和校验函数 → 在共享预算内获得结果或明确失败 |
| [endpoints/runtime/llm_semaphore.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_semaphore.py:47) | 类型、key、容量及截止时间 → Lua 原子名额申请/释放 |
| [tools/docxindex/reranker.py](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/reranker.py:67) | 查询和候选文档 → 独立重排模型的索引及分数 |
| [loggers/call_log.py](C:/Users/lenovo/Desktop/contract_agent/loggers/call_log.py:250) | 逐次实际发送记录 → 操作汇总、重试及 token 完整性 |
| [endpoints/review/web_history.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/review/web_history.py:10) | 完成的 Web 任务 → 历史和下载元数据 |
| [main.py](C:/Users/lenovo/Desktop/contract_agent/main.py:85) | 本地 DOCX、服务地址和凭据 → 通过 API 获取报告 |
