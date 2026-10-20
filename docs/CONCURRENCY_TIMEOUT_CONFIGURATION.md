# 并发、超时与重试配置说明

更新日期：2026-10-19。所有 LLM 共用同一发送与响应接收模块。`llm.stream` 默认 `false`：非流式请求使用逐次请求总时限；流式请求使用连续无新增内容的时限，持续输出可以超过 60 秒。Redis 名额自动续期。本轮不新增配置，模型额外发送上限仍为 5、要点总尝试次数仍为 3。尚未部署到公司内网。第 13 节保留各轮实现的验证记录，旧记录的超时规则不代表当前实现。

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

上表按提交的部署模板说明 DB 职责；实际由 `queue.broker_url` 和 `model_semaphore.*.redis_url` 决定。本地当前配置使用 broker DB 2、模型名额 DB 0，本轮保留这两个地址，不因文档示意修改部署。DB 0、DB 2 是同一个 Redis 服务内的逻辑数据库，共享 CPU、内存和故障范围。分库便于区分职责，不提供独立资源隔离。Kombu 是 Celery 的依赖库，不需要单独部署服务。

任务消息携带 `client_dir` 和 `task_id`，不携带整份合同。`client_dir` 是数据范围标识：API 任务目前位于 `<DATA_DIR>/api/<task_id>/`；Web 标识 `web:<tenant_id>` 对应 `<DATA_DIR>/web/<tenant_id>/<task_id>/`。本次不调整数据目录结构。

## 2. 配置文件在哪里、什么时候生效

部署宿主机维护 `config.yaml`。API 服务和三个 worker 挂载同一份文件到 `/app/config.yaml`，并共用 `/app/data`、租户配置和审查规则的挂载目录。

`config.py` 读取配置并校验；工具进程继承任务截止时间，使用同一部署的模型控制配置。配置在进程加载时读取，没有热更新。更新后需要协调重启 API 和全部 worker，新启动的工具进程才会使用更新后的设置。

仓库的 `config.example.yaml`、`docker-compose.example.yaml` 是提交到版本库的模板。真实的 `config.yaml`、`docker-compose.yaml` 在 `.gitignore` 中。本次已更新开发目录的真实控制配置，但不会因此更新内网宿主机文件或运行镜像。

启动日志的 `[config] execution-control` 输出有效配置和下文列出的内部值。`workflow.model_wait_timeout_seconds` 表示同一逻辑操作累计等待名额的预算；日志字段 `model_request_timeout_seconds` 对应 YAML 的 `workflow.model_call_timeout_seconds`。`llm_timeout_mode` 为 `request_total` 时限制单次请求总时长，为 `content_idle` 时限制连续无新增内容的时长。`lease_auto_renew` 和 `lease_renew_interval_seconds` 只报告内部续期行为，不是可添加到 YAML 的配置。`criterion_retry_max_attempts` 表示要点总尝试次数，`model_extra_send_limit` 表示每次模型操作的额外发送上限；以部署实际值为准。Redis 地址中的用户名、密码被去除。`deployment_contract` 表示部署预期为三个 worker、每个并发 1；实际实例数仍由 Compose/启动命令决定。

## 3. 并发配置

### 3.1 三个层级

| 层级 | 当前基准值 | 范围 |
| --- | --- | --- |
| 合同执行 | 3 个 worker，各 `--concurrency=1 --pool=prefork` | 最多三份合同同时执行 |
| 合同要点 | `workflow.max_orchestrator_concurrency: 2` | 每份合同同时推进两个要点，三份合计最多六个 |
| 模型网络请求 | LLM 5、embedding 3、reranker 10 | 所有 worker、工具进程共用各类型的请求上限 |

一个要点可以产生多个模型请求，六个要点不等于六个请求。模型控制模块约束最终实际发送的请求。

DocxIndex 的索引摘要、embedding 建库和检索扫描仍使用现有内部有界并行，本轮不改成每份合同只并行一个请求。全局上限可以被多个合同同时申请，但实际持有名额、正在发送的同类请求总数仍不能超过配置值。多余调用排队，消耗等待预算。

### 3.2 `workflow`

| 配置 | 本次值或决定 | 精确含义 |
| --- | --- | --- |
| `max_orchestrator_concurrency` | 5 改为 2 | 每份合同同时推进的要点数；2 是首轮验证值 |
| `max_reflection_rounds` | 内网值 10 保留 | 每个要点的业务反思轮数，不是网络错误重试次数 |
| `subagent_allowed_tools` | 保留 `contract_search` | 子 Agent 的可用工具列表，不控制网络并发 |
| `max_api_concurrent_reviews` | 移除 | 合同执行数量统一由三个 worker 决定 |
| `criterion_retry_max_attempts` | 当前值及缺省值 3 | 每个要点的总尝试次数，包含首次；首次执行抛出异常后，最多重新执行该失败要点两次。已保存且版本兼容的成功结果直接复用 |

### 3.3 `model_semaphore.<类型>`

类型是 `llm`、`embedding`、`reranker`。

| 配置 | 当前基准值 | 含义及约束 |
| --- | --- | --- |
| `max_concurrent_requests` | 5 / 3 / 10 | 各类型全部执行进程合计的请求上限，必须为正整数 |
| `redis_url` | `redis://redis:6379/2` | 模型共享名额所在的 Redis DB 2 |
| `key` | `contract_agent:model:<类型>:semaphore` | 同类请求共享同一 key；三种类型使用不同 key |
| `lease_seconds` | 600 | 获得名额后，记录的租约有效时间；不是 HTTP 执行时限 |
| `poll_interval_seconds` | 0.2 | 等待名额时再次检查的间隔，必须大于 0，且受等待预算和整任务截止时间约束 |
| `enabled` | 移除 | 共享限流固定启用 |
| `wait_timeout_seconds` | 继续拒绝旧字段 | 等待统一使用 `workflow.model_wait_timeout_seconds`，不恢复逐类型等待配置 |

同一资源的 worker 必须使用相同地址、key、上限和租约。Redis 中的策略检查拒绝仍有持有者或等待者时混用不同上限/租约；没有持有者和等待者时才允许更换策略。

等待按入队顺序分配名额；重试释放名额后重新排队。取消、超时的等待记录被清理。Redis 连接失败明确报错，不绕过全局限流。

租约规则：

- 正常请求结束，或者本地请求已经取消后，主动释放自己的名额，不继续等满 600 秒。
- 持有名额期间，后台每隔 `lease_seconds / 3` 自动续期，并刷新相关 Redis key 的 TTL；这个间隔是内部实现，不新增配置。
- worker 硬终止来不及释放时，续期停止，记录最多残留至最后一次续期的租约到期；后续申请清除过期记录。
- 启动校验要求 `lease_seconds >= model_call_timeout_seconds + 5`。5 秒是内部清理余量，不增加模型请求预算。
- lease 从取得名额时开始，排队没有持有名额，不计入 lease。每次发送结束后释放，重试重新申请；不需要加上累计等待预算，也不需要将所有重试时长相加。
- 流式请求持续输出时，可以运行超过一次 lease 的长度；续期使名额继续有效。续期原子核验持有者、到期时间和容量策略，绝不重新创建已丢失的名额。续期失败会取消正在执行的请求，关闭连接并清理自己的名额，记录 `semaphore_renewal`，不把它当模型停顿超时重试。
- 600 秒的代价是异常退出后名额回收偏慢；它不会把正常请求执行时间延长为十分钟。
- 如果进程崩溃且名额残留，其他调用仍会按各自等待预算到期，不能保证都等到 600 秒租约回收。正常完成后的主动释放不受这项等待影响。
- 租约到期不停止模型推理，过短的租约会让旧请求未结束时就放入新请求。

每个基础 key 还使用 `:waiting`、`:deadlines`、`:sequence`、`:policy` 辅助 key。Lua 原子清理过期记录、校验策略、记录等待顺序并分配名额，避免多个 worker 同时越过上限。

## 4. 统一模型超时与重试

| 配置 | 本次值或决定 | 含义 |
| --- | --- | --- |
| `workflow.model_wait_timeout_seconds` | 新增，默认 180 | 同一次逻辑操作累计等待 Redis 名额的预算，LLM、embedding、reranker 共用此配置；必须是大于 0 的有限数 |
| `workflow.model_call_timeout_seconds` | 保留 60，按通信模式计时 | 非流式 LLM、embedding、reranker 限制单次请求总时长；流式 LLM 限制首次新增内容前的等待，以及此后连续无新增内容的时间。均不含名额排队、退避及输出校验 |
| `workflow.model_call_max_retries` | 当前值及示例值 5 | 首次发送之外最多再发送五次，即每次逻辑操作最多发送 6 次；网络重试、请求超时重试和 JSON/schema 修复共用这 6 次，每次发送重新获得对应模式的计时 |

一次逻辑操作汇总同一结果的首次生成和修复重试，累计等待预算与发送次数；每次实际 HTTP 请求独立计时：

```text
开始逻辑操作
  → 等待 Redis 名额：消耗累计等待预算，尚未开始本次请求的 60 秒
  → 取得名额：lease 开始并自动续期，本次请求开始计时
  → 非流式：完整接收必须在 60 秒内结束
  → 流式：每收到新增内容重置 60 秒，持续输出不受 60 秒总时长限制
  → HTTP 请求返回或超时：结束本次请求计时
  → 请求退出后主动释放自己的名额
  → 输出校验：此时已释放名额，不扣下一次 HTTP 的 60 秒
  → 需要重试且发送次数尚有剩余：退避，再排队消耗剩余等待预算
  → 再次取得名额：新请求重新计时，不继承上次剩余时间或部分内容
```

等待预算 180 秒、请求时限 60 秒时，等待 45 秒后取得名额仍有 60 秒请求时间，模型再用 40 秒返回可以成功；整任务已消耗约 85 秒。网络重试和 JSON/schema 修复沿用同一逻辑操作的剩余等待预算和发送次数，但不沿用上次 HTTP 的剩余时间。

若第一次等待已用 120 秒，后续重试累计最多再等 60 秒。若第一次请求 38 秒返回但校验不通过，修复请求取得名额后重新获得 60 秒，而不是只剩 22 秒；若第一次请求用满 60 秒超时，只要次数、等待预算和合同剩余时间允许，仍可重新申请名额再请求。退避和校验不扣下一次 HTTP 时限，但计入合同总时长。任一阶段遇到合同执行截止时间，立即停止，不延长整任务。

一个 scan 批次的一次判断是一次逻辑操作；不同独立批次分别记录。Agent 正常进入下一步、工具结果续写或业务反思时产生新的逻辑操作。同一结果的 JSON/结构修复继续使用原逻辑操作的累计等待预算和发送额度，每次实际请求独立获得 60 秒。SubAgent 与 Reflector 的不同生成步骤不会共用请求倒计时。

每个失败要点默认最多三次执行，第二、三次执行中的新逻辑操作建立新的累计等待预算和最多 6 次发送额度；其中每次实际请求按流式/非流式规则重新计时，并遵守相同的 Redis 并发上限和原合同截止时间。其他已经成功的要点不重跑。取消及合同硬时限不会因此获得新的一份执行时间。

网络、超时和 schema 修复不各自增加一组重试次数。若网络重试已使用 2 次发送，随后 JSON/schema 修复最多再发送 4 次。非流式模式默认最多 6 次请求，每次最多 60 秒，累计 HTTP 请求时间最多约 360 秒，另有名额等待、退避及校验。流式模式不能按 `60 × 请求次数` 计算总时长，因为持续输出会重置无内容计时。两种模式都由合同总时限提前终止。等待预算耗尽、合同到期及外部取消不会通过请求重试重新获得预算。

各入口的边界如下。同一行中的所有请求仍使用同一类 Redis 全局名额；“独立操作”只表示有自己的累计等待预算和发送次数，不增加并发上限，每次 HTTP 请求另行计时。

| 调用入口 | 一次操作覆盖什么 | 何时建立下一次操作 |
| --- | --- | --- |
| Agent 的模型生成 | 一次回答及该回答的传输重试、JSON/schema 修复 | 下一个正常生成步骤、工具结果续写或业务反思 |
| DocxIndex LLM | 一次路由、扫描、结构判断或层级批次判断及其输出修复 | 下一个独立批次或判断 |
| DocxIndex 摘要 | 同一摘要结果的生成、校验及压缩修复 | 不同节点或不同摘要批次 |
| DocxIndex embedding | 一次实际输入批次及其传输/输出重试 | 下一个 embedding 批次 |
| DocxIndex 独立 reranker | 一次候选批次重排及其传输/输出重试 | 下一候选批次或下一轮合并重排 |
| LlamaIndex LLM、embedding、reranker | 每次适配器请求及其传输/输出重试 | 下一次独立适配器请求 |

还有一类业务修订：模型回答已经通过 JSON/schema 校验，但确定性的证据、锚点、覆盖范围或路由检查发现结论不符合要求时，Orchestrator 会把具体反馈交给 SubAgent 生成新的完整结论。每次检查最多触发 2 次新结论修订，由内部 `max_revisions=2` 控制。这两次是正常业务操作，各自建立模型预算；不是网络或 schema 修复次数。初次结论和每次业务反思后的结论都可以进入这项检查，因此整个要点不只允许两次业务修订。网络、schema 或确定性检查最终失败并抛出异常时，外层再按 `criterion_retry_max_attempts` 决定是否重新执行这个要点；不是重新执行整份合同。

**60 秒在非流式下限制单次请求总时长，在流式下限制连续无新增内容的时长；都不是整个要点的总时长。**一个逻辑操作可以包含首次发送和修复重试，一个要点还可以经历检索、初次结论、业务修订和反思等多个操作；全部工作仍受部署配置的合同总硬时限约束。

批量摘要已经成功且通过结构校验时，未获得有效摘要的其他节点可以分别生成；这属于不同节点的工作。批量请求因网络、模型时限、Redis、无效 JSON 或修复次数耗尽而失败时，错误向上报告，不能通过改为逐节点生成来重新获得预算。

同一节点的摘要过长时，压缩修复在原操作内进行。若输出修复次数耗尽，但已经收到过结构合法的过长摘要，可以直接截断该已收到的文本；不再发送请求。网络、时限或 Redis 错误不使用截断结果掩盖失败。

可重试连接错误、单次 HTTP 请求超时、408/409/429、可重试服务错误和输出校验错误，在发送次数、累计等待预算及合同剩余时间允许时重试。明确的参数错误等直接结束。`Retry-After` 和其他退避计入合同总时长，不扣后续 HTTP 请求的 60 秒。

可选的层级增强若耗尽输出校验修复次数，保留原有确定性分块/标题回退，不再发送请求；网络、时间和 Redis 错误继续向上报告。

SDK 自带重试关闭。DocxIndex 和保留的 LlamaIndex 网络调用全部接入公共模块。同步调用通过常驻异步网络线程等待实际可取消的协程，不使用无法停止同步 HTTP 的 `to_thread` 包装来冒充超时控制。批处理仍有内部有界调度，但没有第二套用户配置的模型请求限额。

等待预算耗尽后不再申请或发送；单次请求超时后，按剩余发送次数决定是否重试；整任务到期优先终止当前阶段，不重试。本地 HTTP 协程先退出或完成取消，再释放自己的 Redis 名额，下一次发送重新排队。退出清理单独最多等待 2 秒；取消发生在清理期间时，也等待这次有界清理结束后再向上传递取消，避免任务事件循环关闭时遗留清理协程。清理失败依靠租约回收，不延长本次请求时限。

正在传播的超时或取消不会被随后的 Redis 清理错误覆盖，清理故障另行记录。若模型已经正常返回，但释放 Redis 失败，则报告释放故障，不再次发送模型请求。

LlamaIndex 的普通 `chat/achat/complete/acomplete` 与其他 LLM 入口使用相同的 `llm.stream`。它们向业务返回完整响应。公开的分块生成器 `stream_chat/astream_chat/stream_complete/astream_complete` 继续明确报不支持，防止继承实现绕过统一控制；这不影响普通接口内部按配置使用流式通信。

取消本地 HTTP 是否同步停止公司内网服务端推理，尚未验证。不能仅凭客户端连接关闭认定服务端已经停止推理。

### 4.1 LLM 流式开关

```yaml
llm:
  stream: false
```

这是 LLM 通信模式开关。省略时使用 `false`；显式值必须能解析为布尔值，`null` 和空值被拒绝。生产目录的 `config.yaml`、`deploy/config.yaml` 及版本库模板 `config.example.yaml` 都已补充默认值。修改后重启 API 和全部三个 worker；没有热更新，也没有各 Agent 独立的流式开关。

| 值 | 与模型服务的通信 | 交给业务层的结果 |
| --- | --- | --- |
| `false` | `stream=false`，等待完整响应 | 完整 SDK 响应，包含文本、工具调用和服务返回的 usage |
| `true` | `stream=true`，请求 `stream_options.include_usage=true`，逐块接收 | 拼接完成后的同一种完整响应；工具调用按 choice/tool 索引合并，参数拼接完成后才执行工具 |

作用范围包括 SubAgent、Reflector、Summarizer、自定义要点 Planner、DocxIndex 附件层级与摘要、结构检索与正文扫描，以及保留的 LlamaIndex LLM 分支。`base_agent.py` 是 Agent 公共底座；Orchestrator 负责调度，未增加一类独立模型请求。Embedding 和 reranker 使用各自接口，不受该开关控制。Web/API 仍按原来的任务状态及报告接口交互，该开关不表示浏览器逐字显示。

三处适配器均调用 `endpoints/runtime/llm_client.py:request_llm()`。输入是 SDK resource、模型及 messages/prompt/tools 等请求参数；输出是 `(完整响应, HTTP 状态)`。该函数必须在 `execute_model(model_type="llm")` 的实际发送阶段调用，否则在发送前报错。它读取部署配置，覆盖调用参数或 `extra_body` 中的 stream 设置，防止某个调用自行切换模式。SDK 自带重试仍为 0。

非流式接收保留单次请求的 60 秒总时限。流式接收从实际发送开始计时：连接、HTTP 头及首段新增内容必须在 60 秒内返回；此后每收到新增内容，重新开始 60 秒计时。正文、`reasoning_content`/`reasoning` 推理文本及工具名/参数的新增非空白片段都算进展，片段不必是完整句子或合法 JSON。角色、编号、时间戳、空块、仅空白和 SSE 心跳不延长计时。持续输出 180 秒可以正常成功；最后一段内容出现在第 100 秒，此后无新增内容，则约第 160 秒超时。SDK 读取时限与此规则一致，不保留另一层固定 60 秒的整流限制。

等待名额仍单独消耗累计 180 秒预算，合同截止时间仍优先。长流会持续占用名额，其他请求仍可能因累计排队超过 180 秒失败；本轮不修改等待预算及合同总硬时限，也不增加 `null` 写法。

流式请求在所有 choice 获得 `finish_reason` 后才构造完整响应。已收齐结束原因和 usage 时，不继续等待网关补发 `[DONE]`；正常收到 `[DONE]` 或连接正常结束时，同样检查完整性。连接错误、数据未完整结束、无有效 choice 或不合法的数据块按现有发送上限重试；本次部分文本、工具参数不作为成功结果，也不带到下一次尝试。退出流式接收时先关闭 HTTP 响应，再停止续期并释放本次名额。请求超时、断流、取消均遵守同样的释放顺序。每次重试重新排队，并重新计时。

普通日志也记录 `timeout_kind`、首个 SDK 块及首段/末段内容时间、块数、正文/推理/工具字符数、最长无内容间隔、已收到的结束原因、usage 是否收到，以及 `lease_renewals`。不记录正文也能区分首段未返回、中途停止以及尾部结束信息；这些字段表示客户端接收证据，不能据此判定服务端内部调度原因。

模型服务必须支持 OpenAI 兼容的流式协议及所发送的 usage 参数。接口明确拒绝这些参数时记录真实错误，不自动切换为非流式。服务未返回 usage 时 token 数记录为未知，不用估算值或 0 补齐。

## 5. Celery、队列等待及整任务硬时限

| `queue` 配置 | 当前基准值 | 起点及作用 |
| --- | --- | --- |
| `broker_url` | `redis://redis:6379/0` | Celery 任务消息使用 DB 0 |
| `worker_disable_prefetch` | true | 有空闲执行位置时再取任务，减少提前占住任务 |
| `pending_timeout_seconds` | 60 | 从任务创建开始，限制尚未完成入队的等待 |
| `queued_timeout_seconds` | 7200 | 从入队开始，限制尚未被 worker 接手的等待 |
| `watchdog_interval_seconds` | 10 | 等待超时和执行截止状态的周期核对间隔 |
| `task_time_limit_seconds` | 保留 1800 | Celery 从任务被执行进程接受时计时，限制整份合同执行时间 |
| `enabled` | 移除 | Web/API 固定入队，不再支持直接执行旁路 |

软时限从项目配置和实现方案中移除，不提供 `task_soft_time_limit_seconds`，也不保留 `null` 占位。Celery 库内部显式保持软时限未启用。

Celery 原生 `task_time_limit` 是可选功能，库本身不要求设置整任务硬时限。本项目通过 `queue.task_time_limit_seconds` 为业务设置默认 1800 秒；删除 YAML 字段仍会采用默认值。`workflow.model_wait_timeout_seconds`、单次模型请求时限和模型名额 lease 均由项目实现，不是 Celery 要求的配置。

**7200 秒只控制等待 worker 的阶段，不终止已运行的合同。**周期检查存在调度延迟，不能宣称精确在第 7200 秒删除消息。过期任务先标记为失败，旧消息之后被领取也不会执行。

1800 秒包括任务执行中的文件处理、索引、工具、模型等待与请求、报告生成等，不包括此前的队列等待。达到硬时限后，Celery 终止执行子进程，补充执行进程处理后续任务，不自动重跑原合同。

任务执行函数开始时还建立并传递任务截止时间到工具进程；Celery 原生硬时限是最终的进程强制边界。每个阶段都使用剩余预算和合同剩余时长中较小的一项：

```text
排队最多等待 = min(逻辑操作剩余等待预算, 合同执行剩余时长)
非流式 HTTP 最多执行 = min(model_call_timeout_seconds, 合同执行剩余时长)
流式下一段内容最多等待 = min(model_call_timeout_seconds, 合同执行剩余时长)
```

合同已运行 29 分 50 秒时新建的调用只剩约 10 秒，不能再独立延长合同。

### 三个 worker 同时运行的过程

A、B、C 三份合同分别被三个 worker 接手，D 在 DB 0 等待。如果 A、B、C 合计六个调用申请 LLM 名额，最多五个发送，第六个消耗自己的等待预算，尚未开始该次 HTTP 的 60 秒计时。索引内部若产生更多申请，同样排队，不会使模型实发并发超过五个。

例如五个请求各执行 40 秒，第六个等待约 40 秒后再执行 40 秒，可以成功，不会只获得 20 秒执行时间。若等待累计超过 180 秒则报告等待名额超时；若整个合同先到期，则报告整任务到期。三个 worker 的各自任务截止时间和逻辑操作预算互不重置。

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

### 6.1 BGE embedding 和 reranker 怎么配置

两者是不同接口，不能用模型名称相似来判断协议兼容。

| 配置 | 地址和模型名的含义 | 实际发送 |
| --- | --- | --- |
| `embedding.base_url`、`embedding.name` | `base_url` 是 OpenAI 兼容 API 的根前缀，例如 `http://模型服务/v1`；`name` 填该服务实际部署的模型标识 | SDK 向根前缀下的 `/embeddings` 发送 `model`、`input` 等字段 |
| `rerank.base_url`、`rerank.name` | `base_url` 是完整重排地址，代码直接 POST，不自动补 `/rerank`；`name` 用于查找协议映射 | 按映射的 provider 构造下面的 payload |

BGE embedding 不需要加入 rerank 的 provider 名单。项目不按 embedding 名称选择厂商，配置中的任意模型标识会传给 OpenAI 兼容 embeddings 接口；服务必须接受该标识并返回兼容的 `data[].embedding/index` 结构。仅提供其他厂商私有向量接口时，当前适配器不能直接使用。公司内网的实际模型名称及地址应以部署值为准。

用户已确认两套环境的模型名称如下。该表记录部署选择，不代表两个环境的实际服务都已完成调用验证。

| 环境 | LLM | embedding | reranker |
| --- | --- | --- | --- |
| 公司内网生产 | `qwen3.6-35b-a3b` | `bge-m3` | `bge-reranker-v2-m3` |
| 测试 | `qwen3.6-35b-a3b` | `qwen3-embedding` | `qwen3-reranker` |

生产切换片段如下；地址是占位符，必须替换为实际环境地址，其他模型、控制及检索配置保留。若原配置还使用其他重排模型，将这里的 `bge` 项合并到已有 provider 名单。

```yaml
llm:
  name: qwen3.6-35b-a3b
embedding:
  base_url: "<内网 embedding API 根前缀>"
  name: bge-m3
rerank:
  base_url: "<内网完整重排 URL>"
  name: bge-reranker-v2-m3
  provider_model_names:
    bge:
      - bge-reranker-v2-m3
```

此片段说明内网切换方式，不表示本地现有模型地址已经切换或内网接口已完成实测。BGE 服务是否满足上面的接口契约，需要用实际部署地址验证。

测试环境的名称片段如下。embedding 地址仍是 OpenAI 兼容根前缀；reranker 地址仍是完整重排地址。当前为 `qwen3-reranker` 增加 Higress 协议别名，发送扁平的 `model/query/documents/top_n`。实际测试网关若要求嵌套的 `input/parameters`，应将该别名移到 `dashscope_qwen`，不能同时放入两个列表。名称只选择本地适配，不证明真实网关接受对应 payload。

```yaml
llm:
  name: qwen3.6-35b-a3b
embedding:
  base_url: "<测试 embedding API 根前缀>"
  name: qwen3-embedding
rerank:
  base_url: "<测试完整重排 URL>"
  name: qwen3-reranker
  provider_model_names:
    higress_qwen:
      - Qwen3-Reranker-8B
      - qwen3-reranker
```

尚未获得测试网的实际地址，此处验证范围是请求协议兼容，不能称为已连通该测试服务。

`rerank.provider_model_names` 显式列出模型名和请求协议。匹配不区分大小写，模型名必须唯一归属一个 provider；未知名称明确报错，不依据 `bge` 等子串猜测协议。默认映射如下，内网使用别名时，将实际别名加入对应列表，并把 `rerank.name` 设置为同一别名：

```yaml
rerank:
  provider_model_names:
    higress_qwen:
      - Qwen3-Reranker-8B
      - qwen3-reranker
    bge:
      - bge-reranker-v2-m3
    dashscope_qwen:
      - qwen3-rerank
      - gte-rerank-v2
```

| provider | 请求 JSON 结构 | 说明 |
| --- | --- | --- |
| `bge` | `model`、`query`、`documents`、`top_n` | 使用 `X-API-Key` 请求头，不发送 `instruct`；本轮不改变这些字段 |
| `higress_qwen` | `model`、`query`、`documents`、`top_n`，以及配置的非空 `instruct` | 模型标识随请求发送 |
| `dashscope_qwen` | `model`、`input: {query, documents}`、`parameters: {return_documents: false, top_n}`，以及配置的非空 `instruct` | 使用该接口的嵌套结构 |

配置模型名称不会转换服务端协议。若公司网关的 BGE 重排要求其他字段，应先确认它的实际接口契约，再增加适配，不能直接认定兼容。三个 provider 都经过同一 `reranker` 并发和模型预算控制。

### 6.2 Embedding 和 reranker 的输入长度与原节点排序

```yaml
embedding:
  max_input_tokens: 1024
  chunk_overlap_tokens: 32
rerank:
  max_input_tokens: 1024
```

| 配置 | 作用 |
| --- | --- |
| `embedding.max_input_tokens` | 每条实际 embedding 输入的 token 上限，包含该 tokenizer 加入的特殊 token。必须是正整数。代码兼容默认值为 8000，当前配置示例为 1024。 |
| `embedding.chunk_overlap_tokens` | embedding 输入分片相邻窗口的重叠 token 数，默认 32，可设为 0。必须小于输入上限，并允许切片继续向前推进。重叠内容计入每片的上限。 |
| `rerank.max_input_tokens` | 每组 query 与一条候选文本的输入上限，包含计数适配器纳入的指令及特殊 token。代码兼容默认值为 20000，当前配置示例为 1024。 |

没有 `rerank.chunk_overlap_tokens`。出现该字段会明确报配置错误。reranker 输入准备不使用重叠。

这些字段不更改原文档索引的节点切分。`retrieval.docxindex.indexing.paragraph/table` 继续决定文档结构中的节点；`retrieval.docxindex.input_tokens` 继续用于 LLM 结构检索及向量候选信息预算，不再控制 reranker 的每组输入长度；`output_tokens`、`scan_batch_tokens` 继续控制正文分页与扫描。

**向量构建与召回。** 原节点的标题、摘要、正文组成 embedding 输入，超过上限时按段落、句子边界优先切成输入片段，单句仍超长则继续切分。原文所有字符都必须被片段覆盖，不能只截取前半部分。每片生成一个向量，`vector_id` 唯一，`node_id` 仍为原节点 ID。`VectorItem.text` 记录实际编码的片段；`source_start/source_end` 是它在原 embedding 输入字符串中的字符范围，不是新的 DOCX 锚点。全文和原锚点保留在文档索引中。

查询向量与各片段向量计算余弦相似度，原节点分数取其所有片段的最高相似度。先合并到原节点，再按分数排序、执行 `score_threshold`、`probe_limit` 和候选信息预算。因此一个长节点的多个片段只占一个节点名额。限定区域仍先按原 node_id 过滤；排序命中的片段不替换后续返回的完整候选正文。

**重排准备、评分与合并。** DocxIndex 先按原候选生成与此前相同的候选文本字符串，随后检查 query 加每条候选的长度。超长字符串在候选准备层切为连续、不重叠的输入片段，原节点保持不变。片段不是新的业务节点，完整原文、原 `node_id`、已有 `text_override` 及定位范围均保留。`documents` 中的元素仍是字符串；分片是对字符串内容的处理，不增加或改名 HTTP 字段。

RerankerClient 只接收已满足上限的文本，按现有 provider 请求格式评分。每批最多 10 条输入，必须取得全部输入的分数，再在 DocxIndex 中将同一原 node_id 的分数取最高值；所有批次完成后统一排序。没有逐批淘汰再反复重排同一批原节点的过程。最终每个原节点只出现一次，`max_selected_nodes` 由调用方在合并后执行。同分保留原候选顺序，重复文本不会导致不同原节点被合并。

每个片段批次是独立逻辑模型操作，使用既有全局名额、等待预算、单次请求超时及重试配置。正常发送下一个批次不算重试。任何批次在重试后仍失败，整次索引构建或重排明确失败，不以缺失片段的部分结果冒充成功。HTTP 400 的异常信息包含服务端错误正文（最多 4096 字符），仍不对 400 原样盲重试。

查询本身及固定输入开销已超过上限时，会在请求前报告 `ModelInputTooLong`，不静默截断查询。批次内每条序列分别检查上限，不把一个 HTTP 批次中所有独立序列的长度相加后误当成单条上下文。

### 6.3 五种模型的离线 tokenizer

| 模型名及支持别名 | 固定资源来源 |
| --- | --- |
| `bge-m3` | `BAAI/bge-m3` |
| `bge-reranker-v2-m3` | `BAAI/bge-reranker-v2-m3` |
| `qwen3-embedding`、`Qwen3-Embedding-8B` | `Qwen/Qwen3-Embedding-8B` |
| `qwen3-reranker`、`Qwen3-Reranker-8B` | `Qwen/Qwen3-Reranker-8B` |
| `qwen3.6-35b-a3b` | `Qwen/Qwen3.6-35B-A3B` |

在有网络的开发/打包机器上，提前运行：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_tokenizers.py
.\.venv\Scripts\python.exe scripts/prepare_tokenizers.py --verify
```

文件写入项目 `resources/tokenizers/`，`manifest.json` 记录各仓库固定 revision、每个文件的字节数和 SHA256。已有 manifest 时继续使用其中 revision，不会随上游 main 静默更新。脚本只下载 tokenizer、词表、配置与许可证，不下载模型权重。

Dockerfile 将文件复制到 `/app/resources/tokenizers/`，并在 `RUN --network=none` 中加载验证全部五个分词器。镜像启动使用本地 AutoTokenizer，`local_files_only=True`，禁用远程代码；镜像同时设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。已支持模型缺文件或校验失败会报错，不回退到 cl100k_base，更不会在内网尝试下载。API、三个 worker 及工具子进程使用同一镜像资源，无需远端宿主机安装 Python 或添加各自挂载。

Qwen3.6 的 tokenizer 也已离线准备并验证加载；本轮没有将其他 LLM 结构估算、提示词或请求模板改写为新的切分流程。

**配置上限与模型容量。** `max_input_tokens` 是客户端允许的上限，不会扩大模型容量。有效上限还受已知模型容量限制：BGE 两种资源为 8192，Qwen3 embedding/reranker 为 32768。因此把 BGE 配成 40000，不表示服务能够接收 40000；公司服务若进一步收紧为 1024，仍需将配置设为 1024。原版 tokenizer 文件的 model_max_length 不能用来猜测网关实际开放容量。私有网关额外拼接规则也需要与现有计数口径核对，HTTP 请求模板保持原样。

**原有闭源接口的兼容边界。** 当前本地外网配置的 `text-embedding-v4`、`qwen3-rerank` 不冒充上述五种原版模型。它们保留原有 cl100k_base 估算方式，日志 tokenizer 标记为 `legacy-estimate:cl100k_base`，不宣称是服务端精确计数。兼容预算分别保守限制在 8000、32768。已支持的 BGE/Qwen3 则使用离线原版 tokenizer，日志包含其 revision。估算输入 token 数与模型实际返回的 usage 分开记录。

向量索引格式版本为 2，记录配置上限、有效上限、重叠量及 tokenizer 标识。旧截断索引没有后半部分向量，不能通过改版本号补齐；需从原文重新构建。当前合同任务每次临时建索引，新任务自动使用新规则。

### 6.4 本轮验证入口

```powershell
.\.venv\Scripts\python.exe -m unittest tests.criteria_formal_recall.test_chunked_model_inputs
.\.venv\Scripts\python.exe tests/criteria_formal_recall/live_chunk_contracts.py --image lexora:chunks-verified-20261020 --limits 40000 1024
```

真实合同脚本为每档创建独立 Compose 项目，使用一个 API、三个 concurrency=1 的 Celery worker 以及独立 Redis，连续提交三份 `deploy/test_contract.docx`，记录任务 ID、API/存储状态、报告、输入长度、向量数量、并发采样与最终名额；结束后清理自己创建的容器，保留宿主机临时目录中的验证数据，不停止已有部署。

真实合同调用当前本地配置的可访问模型服务；五种 tokenizer 的断网验证和本地严格长度模拟，不等于已经调用公司内网 BGE/Qwen 服务。验证结果必须列明实际端点/模型、配置上限和有效上限。配置 40000 而有效上限为 8000 的结果，不能表述成模型接收了 40000 token。

### 6.5 2026-10-20 实测记录与离线包

两档均通过 API 同时提交三份 `deploy/test_contract.docx`，由三个独立的 Celery prefork worker 执行。实测模型是本地配置的外网 `qwen3.6-35b-a3b`、`text-embedding-v4`、`qwen3-rerank`，不是公司内网 BGE 服务。LLM 保持非流式及原有 60 秒时限。

| embedding / rerank 配置上限 | 任务 ID | 执行秒数 | 最终结果 | 向量数 / 原节点数 |
| --- | --- | --- | --- | --- |
| 40000 / 40000 | `20261020-213636-d8ba` | 289 | succeeded，18/18 | 142 / 142 |
| 40000 / 40000 | `20261020-213637-aace` | 316 | succeeded，18/18 | 142 / 142 |
| 40000 / 40000 | `20261020-213637-e086` | 345 | succeeded，18/18 | 142 / 142 |
| 1024 / 1024 | `20261020-214410-3c09` | 313 | succeeded，18/18 | 153 / 142 |
| 1024 / 1024 | `20261020-214410-7881` | 321 | succeeded，18/18 | 153 / 142 |
| 1024 / 1024 | `20261020-214410-83b2` | 342 | succeeded，18/18 | 153 / 142 |

六份任务均核对了 API、task.json、review_outputs.json、完成事件及 DOCX 报告。所有原节点的 embedding 输入均被切片完整覆盖，片段文本与源字符范围及 SHA256 一致。报告 ZIP 完整，具有批注，未出现“未完成审查项”提示。原节点没有被向量片段替换。

40K 档的兼容接口有效上限为 embedding 8000、reranker 32768；该合同实际最高输入估算分别为 3686、1477。1K 档有效上限均为 1024，实际最高分别为 1020、1024，确实执行了切片。两档都没有 embedding/reranker 请求失败或 HTTP 400。40K 档有两次 LLM 请求达到原有 60 秒时限，随后按既有配置重试并成功；其他模型输出校验失败也经过既有修复流程，不表示六份任务零重试。

两档每约两秒采样的最高占用均为 LLM 5、embedding 3、reranker 1，没有观察到超过配置上限。1K 档结束后，三类模型的占用、等待队列及等待截止集合九项计数全部为 0。40K 档监控脚本最初汇总时使用旧任务目录布局，后从已保存任务恢复结果，没有重复提交合同；该档最终 Redis 快照未保留下来，不能宣称已经直接验证其终态九项计数为 0。脚本已修正路径，并改为先保存名额快照、再检查业务产物。

原版 BGE/Qwen3 的 1K 长度及最高片段合并规则由本地严格 HTTP 模拟和离线 tokenizer 用例验证。它们不替代内网实际服务复测，也不证明该合同包含接近 40000 token 的单条输入。

回归复跑共 405 项：378 项通过，27 项因未设置集成运行条件而跳过。其中包含新增的 9 项离线 tokenizer、完整覆盖、最高片段聚合及严格 1K HTTP 模拟测试。另用独立 Redis 执行模型控制套件 38 项，全部通过；Linux 源码验证中的 9 项 Redis 传输测试和 1 项 Celery 硬终止测试通过，验证了三进程共用 5 个名额、续期、超时/断流释放，以及替补进程接手后续任务。交付保护镜像内另运行 15 项流式/非流式传输测试，全部通过。

附加验证也保留失败记录：Windows 集成套件和 Linux 源码挂载套件曾在亚秒级模拟时限中失败，后者的 0.16 秒推理/工具片段用例有 3 个子用例报超时；它在交付镜像的 15 项传输测试和 Windows 全量复跑中均通过。该附加环境差异的根因不确定，本轮未修改 LLM 时限实现或业务 60 秒配置。保护启动器不支持 Python multiprocessing 的 `-c` 启动参数，因此三进程白盒用例通过挂载同版源码的 Linux 验证容器运行；六份真实 Celery 合同均直接使用保护镜像。

完整日志和六份报告保存在开发机 `data/verification/model-inputs-20261020/`；该目录属于本地运行数据，不提交到 Git。新的离线镜像包为 `deploy/images-chunked-inputs-20261020.tar`，包含 `lexora:chunks-verified-20261020` 和 `redis:7-alpine`，五种 tokenizer 已在应用镜像内部。归档成员完整读取验证通过，旁边的 `.sha256` 文件用于传输后核对。

镜像包大小为 493002752 字节，SHA256 为 `205e4b5200e4dd17111e3b416de30742dbb4b217183e46ce60da266f19e45223`。

在内网宿主机的部署目录中执行：

```bash
sha256sum -c images-chunked-inputs-20261020.tar.sha256
docker load -i images-chunked-inputs-20261020.tar
```

将该部署自己的 Compose API 和三个 worker 的 `image` 都设为 `lexora:chunks-verified-20261020`，并在挂载的 `config.yaml` 中设置本环境模型地址、名称、输入上限和 embedding 重叠量，再按该部署原有的项目名启动。无需在内网安装 Transformers 或下载 tokenizer，不覆盖其他部署的项目名、端口或挂载数据。

## 7. 移除配置清单及不变配置

必须删除以下旧字段，不能靠保留旧值继续启动：

```text
workflow.max_api_concurrent_reviews
queue.enabled
queue.task_soft_time_limit_seconds（此前仅方案中出现，不再实现）
model_semaphore.<类型>.enabled
model_semaphore.<类型>.wait_timeout_seconds
retrieval.docxindex.concurrency.*
retrieval.docxindex.indexing.hierarchy.retry_count
```

旧共享格式 `model_semaphore.limits`、共享的 Redis/lease 等字段需要迁到逐类型结构；旧 `llm.global_semaphore_*` 和 `llm.max_global_concurrent_requests` 不再接收。单独的模型 timeout、rerank 重试退避字段也不再接收。配置加载明确报迁移错误，防止旧字段被静默忽略。

已移除内部独立次数：DocxIndex 限流错误额外重试、`complete_model()` 独立修复次数、scan 整批重试。每次 HTTP 时限统一取 `model_call_timeout_seconds` 与合同剩余时长中较小的一项，不再沿用 DocxIndex 默认 120 秒。`workflow.criterion_retry_max_attempts` 已恢复为有效配置，不再属于迁移时删除的字段；当前缺省值和示例值均为 3。

| 保留配置 | 作用边界 |
| --- | --- |
| `llm/embedding/rerank` 的地址、模型名和凭据来源 | 保留部署自己的模型服务选择 |
| `rerank.instruct/provider_model_names` | 独立重排请求和协议适配 |
| `llm.enable_thinking/temperature/top_p/seed` | 保留模型请求参数 |
| `llm.stream` | 默认 `false`，统一全部 LLM 的通信模式，见第 4.1 节 |
| `llm.max_context_tokens/max_prompt_tokens/max_result_tokens` | 保留当前上下文、提示、结果处理逻辑，本轮不重新定义 |
| `llm.max_tool_calls`、`embedding.max_input_tokens` | 保留工具调用次数及输入约束 |
| `embedding.chunk_overlap_tokens`、`rerank.max_input_tokens` | embedding 切片重叠量及独立重排输入上限，见第 6.2 节 |
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
| Redis 连接及操作时限 | 2 秒 | 基础连接故障边界；申请名额还受等待预算和整任务截止时间约束 |
| Redis 退出清理时限 | 2 秒 | 请求已结束后的释放动作，失败交给租约 |
| 租约最小清理余量 | 5 秒 | 租约至少比单次模型请求时限长 5 秒，不加等待预算或重试次数 |
| worker 心跳间隔 | 10 秒 | 状态更新频率，不是任务执行时限 |
| 任务文件锁等待/检查间隔 | 30 秒 / 0.05 秒 | 共享任务文件写入；锁由 OS 持有，无旧版 300 秒陈旧锁等待 |
| 截止状态核对余量 | 5 秒 | 父回调缺失时的状态兜底，加上 watchdog 周期发现 |
| 确定性业务检查的 `max_revisions` | 每次检查最多 2 次新结论修订 | 证据、锚点、覆盖或路由反馈；前次回答须先通过 schema。初始及每次反思后可分别触发，不是网络/schema 重试，也不是整要点重跑 |

## 9. 控制配置完整片段

该片段对应本次实现。部署时保留原有模型、检索、API 等其他分组，并删除第 7 节的旧字段。

```yaml
llm:
  stream: false

workflow:
  max_reflection_rounds: 10
  max_orchestrator_concurrency: 2
  model_wait_timeout_seconds: 180
  model_call_timeout_seconds: 60
  model_call_max_retries: 5
  criterion_retry_max_attempts: 3
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

LLM 每次实际发送还记录下表字段。这些字段同时保存在普通模式的 `logical_calls/*.json` 的 `attempts` 和详细模式的 `calls/*.json`；无需增加日志配置。

| 字段 | 含义 |
| --- | --- |
| `transport` | `unified_llm` 表示经过统一 LLM 发送模块 |
| `stream` | 此次实际使用的部署流式开关值 |
| `first_chunk_seconds` | 流式发送开始至收到首个 SDK 数据块的耗时，可能只是角色或思考信息，不等于首个正文 token；非流式或首块前失败为 `null` |
| `response_seconds` | 统一发送函数的耗时；包含接收或失败退出，不含名额等待及 Redis 清理 |
| `stream_complete` | 流式完整接收并构造响应后为 `true`，流式失败为 `false`；非流式为 `null` |

等待名额超时、单次 HTTP 请求超时、合同执行到期和 Redis 连接失败分别记录，不能把 Redis 无法连接解释为模型推理太慢。逐次发送的 HTTP 耗时与逻辑操作累计耗时分开统计；多次发送累计超过 60 秒属于允许范围。等待计入日志总耗时和整任务耗时，不计入任一次 HTTP 请求时限。等待耗尽记录在 `semaphore_wait` 阶段，合同到期记录在 `task_deadline` 阶段，这两类情况不触发请求重试。

| `attempts[].timing` 字段 | 当前含义 |
| --- | --- |
| `request_seconds` | 当前这次实际发送的 HTTP 耗时 |
| `model_elapsed_seconds` | 同一逻辑操作截至当前，所有实际 HTTP 请求耗时的累计值；不含等待、校验、退避和名额清理 |
| `remaining_seconds` | 当前或最后一次请求的剩余总时限，或流式剩余无内容等待时间；受合同截止时间限制。超时后为 0 不表示整组禁止重试，下一次发送重新计时 |
| `waiting_total_seconds` / `wait_remaining_seconds` | 同一逻辑操作累计名额等待耗时 / 剩余等待预算 |
| `total_seconds` | 整个逻辑操作截至当前的墙钟耗时，包含此前尝试及名额等待 |

例如非流式第一次请求超时 60 秒、第二次请求 38 秒成功时，第二次的 `request_seconds` 约为 38，`model_elapsed_seconds` 约为 98；不能把累计 98 秒解释为单次 HTTP 超过 60 秒。流式单次 `request_seconds` 超过 60 秒本身不表示异常，应结合 `timeout_kind`、内容时间及 `max_content_gap_seconds` 判断。旧日志不补写，字段应按日志产生时的版本解释。

- `input_tokens/output_tokens/input_output_tokens` 保留已有最终结果字段语义，不把它们当成全部重试成本。
- `known_input_tokens/known_output_tokens` 汇总已知的逐次 usage；未知部分不自动计为零。
- `input_usage_complete/output_usage_complete/usage_complete` 表示统计是否完整。LLM 缺输出 usage 时不能标为完整；embedding 不要求输出 token。
- HTTP 已成功但释放 Redis 失败时，`attempts.http_status` 保留已经收到的 HTTP 状态及 usage，标记 `semaphore_release`，不重复发送。此时 Redis 错误本身没有 HTTP 状态，`error.http_status` 可以为未知，不能把两者混淆。
- 业务成功与失败读取 `task.json`。Celery 函数正常返回只说明消息处理函数返回，不能替代业务成功判断。
- 整要点重试产生新的逻辑模型调用，不能仅把单个 `logical_call_id` 的 `retry_count` 当作要点的全部恢复次数。详细模式下，`state/criteria/<ID>.error.json` 保存失败尝试列表；最终成功的 `<ID>.output.json` 中 `attempt=2` 表示第二次要点执行成功，首次失败记录仍保留。

## 11. 部署迁移

1. 等待或排空旧 worker 的任务后再升级，不混用不同模型控制策略的版本。
2. 在内网宿主机迁移挂载的 `config.yaml`；依据第 7 节删除旧字段，保留内网地址、凭据、并发、lease、重试和算法配置。新增 `workflow.model_wait_timeout_seconds: 180`；省略新字段也采用 180 秒默认值，不恢复逐类型 `wait_timeout_seconds`。
3. 使用包含本次代码的新镜像，同时更新 API、三个 worker 和配套挂载。
4. 检查各服务的 `[config] execution-control`，确认 Redis 地址/数据库、各上限、累计等待预算、模型超时模式、重试及合同硬时限一致。模板的累计等待预算为 180 秒、模型时限为 60 秒；流式应显示 `llm_timeout_mode=content_idle`，名额应显示自动续期。重试显示值应与部署文件一致。确认三个 prefork worker 每个并发 1。
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

此前控制逻辑使用本地测试 HTTP 和隔离 Redis 验证，并用本地配置中的阿里云模型端点完成真实合同任务，没有把它当作公司内网模型实测。本节及第 13.1—13.4 节记录历史验证范围，使用当时的累计执行预算；不能将这些结果直接视为当前逐次 HTTP 独立时限的验证结果。

| 验证 | 已验证内容 |
| --- | --- |
| 现有单元测试和新增回归 | 原功能、两入口队列化、状态竞态、共享重试、逐次 token 统计及迁移错误 |
| 三个独立进程 + 真实 Redis + 本地 HTTP | LLM 5、embedding 3、reranker 10 的共享上限和名额最终释放 |
| 等待、退避、持续慢响应 | 不重置预算，截止后实际断开本地 HTTP，取消等待不释放他人名额 |
| 同步/异步模型适配器 | SDK 解析、类型选择、嵌套预算、任务截止时间继承及底层请求取消 |
| Linux 镜像中的真实 Celery prefork | 硬时限和持有文件锁时 SIGKILL；业务失败状态、工具停止、执行进程补充和下一任务成功 |
| 静态检查和 Compose | 代码编译、Ruff、示例 Compose 结构验证 |

Linux 集成使用与内网一致的 Celery/Kombu/redis-py 版本及当前源码，测试将合同执行替换为可控工作，不使用真实模型推理。它证明进程控制和状态处理有效，不证明内网模型吞吐或排序质量。

当前非流式单次 HTTP 总时限为 60 秒，流式为连续无新增内容 60 秒；合同硬上限和要点并发未因本次修改改变。上述历史结果不证明内网服务在此规则下一定成功，需要在内网按同批合同复测。

## 13. 验证记录

本节第 13.1—13.6 节均为历史记录。下文“60 秒操作预算”“逐块返回不延长时限”等描述对应版本当时的行为，当前规则见第 4 节，第 13.7 节记录流式空闲计时及自动续期的验证。

### 13.1 前轮实现验证

- 正式回归集：236 项，235 项通过；默认跳过的 Linux 专用测试已在容器中单独执行通过。该次回归设置 `TEST_MODEL_REDIS_URL`，包含真实 Redis/HTTP 的 6 项集成场景。
- 消融兼容离线测试：22 项通过，无真实模型请求。
- Linux Celery 集成：真实 prefork、Request 回调、2 秒硬时限。任务持有状态写锁时约 2.226 秒记录超时；SIGKILL、工具停止、执行进程补充及下一任务成功均通过。
- Ruff、Python 编译和示例 Compose 配置检查通过。内网真实模型的质量及 20 分钟目标尚未复测。

集成测试使用隔离临时 Redis，不操作原有部署服务。测试文件分别为 `test_model_control.py`、`test_celery_hard_timeout_linux.py`；后者需要 Linux/prefork 与显式隔离 broker。

### 13.2 前轮复核和真实合同（模型最多发送 2 次、要点执行 1 次）

本节保留恢复重试策略前的验证结果，不代表第 9 节当前配置的运行结果。

使用当前源码、三个独立 worker、隔离 Redis DB 0/DB 2，经 API 提交 `deploy/test_contract.docx`。任务在 222.532 秒（约 3 分 43 秒）后进入 `succeeded` 并生成 44 条批注；18 个要点中 17 个成功，H18 的 schema 校验失败。因此任务完成和报告生成不等于所有要点审查成功。

实际发送 104 次：LLM 82、embedding 19、reranker 3；共 95 个逻辑操作、9 次额外发送，单操作最多发送 2 次。全部已发送记录都包含模型类型、名额 key、预算、剩余时间和发送序号。逐次记录中最大的 `timing.total_seconds` 为 57.941402 秒，它从所属逻辑预算的起点累计，不是单次 HTTP 耗时。没有发生模型超时。任务结束后，DB 2 三类模型的持有者、等待者和等待截止记录均为 0。

本轮正式回归共 260 项，259 项通过、1 项 Linux 专项在 Windows 跳过；该 Linux 专项另在 Docker 中通过，耗时 11.223 秒。消融兼容测试 22 项通过。真实 Redis／本地 HTTP 专项验证了请求及释放阶段超时、重复取消、清理失败和硬杀后的租约回收；真实合同本身未发生超时，不能用这次合同成功替代超时测试。

该任务调用本地配置中的阿里云端点，不能据此推断公司内网 BGE 或测试网 Qwen 服务的性能。生产 BGE、测试 Qwen 的适配仅通过 MockTransport 协议回归，没有伪称已访问其真实部署地址。源码审计、清理故障及取消测试、任务记录和报告证据见[本轮审计报告](code-review/model-control-audit-20261016.md)。

### 13.3 恢复重试后的三次真实合同验证

本轮仅恢复发送上限和要点尝试次数：`model_call_max_retries=2`、`criterion_retry_max_attempts=2`。保留三个 worker、60 秒模型操作预算、1800 秒合同硬时限及 LLM/embedding/reranker 的 5/3/10 上限。提示词、schema 和检索逻辑没有修改；与前一轮相同，使用本地配置的阿里云端点，不是公司内网服务，未替换内网部署镜像。

| 次数 | 本轮总耗时 | 本轮完成要点 | 前轮完成要点 | 本轮 H18 |
| --- | --- | --- | --- | --- |
| 1 | 322.515 秒 | 18/18 | 16/18 | 完成 |
| 2 | 约 393.9 秒（状态轮询精度 0.1 秒） | 17/18 | 17/18 | 失败 |
| 3 | 293.390 秒 | 18/18 | 16/18 | 完成 |

实际有 7 个逻辑调用使用了第 3 次发送，其中 5 个最终成功；整要点第 2 次执行救回 3 项：第 1 遍 H15，以及第 2、3 遍 H01。H18 本轮仍为 2 次完成、1 次失败，与前轮相同，不能据此说 H18 完成率改善。第 2 遍 H18 首次执行耗尽业务修订机会，第二次执行的实际模型请求约 59.961 秒后超时，名额等待约 0.00065 秒，说明新的尝试仍受 60 秒操作预算限制，并非等待名额耗尽时间。

三遍共实际发送 362 次，其中逻辑调用首次发送之外的额外发送为 32 次。观测到三类模型占用峰值为 5/3/1，未超过配置上限；全部任务结束后，三类 Redis 持有者、等待者及等待截止记录均为 0。正式回归 267 项，266 项通过、1 项 Linux 专项在 Windows 跳过；消融兼容测试 22 项通过。

`error.json` 中保留的首次失败不等于该要点最终失败；本表按最终状态及成功 output 判断。附件目录遗漏仍然存在，完成也不代表审查完整。三次样本不能推断长期成功率或内网性能。逐次结果、错误链及控制记录见[恢复重试对照报告](code-review/retry-recovery-comparison-20261016.md)。

### 13.4 分开等待与执行计时（2026-10-17）

本轮模型控制验证使用隔离 Redis 和本地 HTTP 模拟服务，不访问公司内网模型，不能据此推断内网推理性能或审查质量。三个 worker、各类型全局上限、索引内部并行和原 lease 值保持不变。

| 验证范围 | 结果 |
| --- | --- |
| 除模型控制专项外的完整回归 | 260 项，259 项通过，1 项 Linux 专用用例在 Windows 跳过 |
| 隔离 Redis 模型控制专项 | 27/27 通过，覆盖独立等待/执行预算、重试、超时、取消和名额清理 |
| Linux Docker 真实 Celery prefork | 上述跳过的 1 项单独执行通过；2 秒硬时限实际约 2.275 秒终止执行进程及任务关联工具，补充执行进程后下一任务成功 |

合计 287 个不同测试用例最终通过。三进程专项同时产生 15 个请求，全部完成，实际请求峰值为 5；至少一个请求等待超过其 1.2 秒模型执行预算，取得名额后仍能完成约 0.8 秒的模拟请求。结束后 Redis 持有者、等待者和等待截止记录均为 0。这些短时限是测试夹具，实际配置仍为等待默认 180 秒、执行 60 秒、lease 600 秒。

修改本地源码不会更新内网已运行镜像。内网需要重建包含本次修改的镜像，协调更新 Web/API 与三个 worker，并在共享挂载的 `config.yaml` 中设置等待预算；不填写新字段时采用默认 180 秒。启动后核对等待与执行预算，再复测真实合同。

### 13.5 每次实际 HTTP 请求独立计时（2026-10-19）

本轮未执行真实合同，未调用公司内网或外部模型服务。全量回归共 32 个模块、372 项：371 项通过、1 项 Linux Celery 硬时限专项因 Windows 环境跳过，0 失败、0 错误，耗时 62.609 秒。上述总数已包含 18 项真实隔离 Redis + 本地 HTTP 集成用例，不能再次相加。

- 受控时钟用例模拟首轮 38 秒返回但 schema 校验失败，修复请求再用 50 秒成功；两次传给 SDK 的请求时限均为 60 秒，证明修复请求没有沿用首轮剩余的 22 秒。此用例不是等待真实模型运行 88 秒。
- 真实本地 HTTP 集成使用短夹具时限验证请求超时后的底层取消、释放名额和按配置重新请求；验证合同截止、取消及累计等待耗尽不会通过请求重试重置预算。
- 三个独立进程的并发控制验证保留 LLM/embedding/reranker 的全局 5/3/10 上限。等待预算和发送次数继续由同一逻辑操作累计，传输与 schema 修复共用发送次数。
- 生产配置中的单次模型请求时限仍为 60 秒，没有新增配置，也未修改三个 worker 的部署数量。

这些测试验证请求计时与恢复控制，不证明 H01/H12 在内网模型上一定能在 60 秒内返回；内网实际耗时仍需部署后复测。

### 13.6 统一 LLM 发送与流式开关（2026-10-19）

本轮最终本地回归共 384 项：383 项通过，1 项 Linux Celery 硬时限专项因 Windows 环境跳过；0 失败、0 错误，耗时 83.355 秒。总数已经包含 20 项隔离 Redis 与本地 HTTP 集成测试，不另行相加。修改文件的编译检查和 Ruff 检查均通过。

- 启动校验覆盖省略开关、`false`、`true`、无效值及 `null`；省略时确认为非流式。
- 两种模式均经过实际 OpenAI SDK 解析；验证分段文本、并行工具参数、服务返回的 usage，以及缺失 usage 保持未知。
- 两种模式各启动三个独立进程，每进程并发发送 5 个 LLM 请求，15 个请求竞争同一个 Redis 上限 5；HTTP 服务观测并发不超过 5，结束后持有名额、等待与截止记录均为 0。
- 真实本地 HTTP 服务注入非流式/流式持续慢响应与断开连接，配合真实 Redis 验证：超时和断流后按发送上限重试，连接关闭，名额主动释放。逐块返回不会延长单次请求时限。取消不触发重试；单位测试还覆盖无有效结束标记的流式响应及丢弃部分结果。
- Agent、DocxIndex 和保留的 LlamaIndex 三类适配器都在两种模式下验证。业务入口的所有实际 LLM 发送均经过 `execute_model` 和 `request_llm`；公开的分块生成器不参与业务流程。

真实合同采用本地独立 Compose 项目：API、三个 Celery worker、独立 Redis，共享修改后的源码和独立数据目录。运行环境复用本地镜像中的 Python 及依赖；没有替换原有 v6/v7 容器。调用开发配置的外部 `qwen3.6-35b-a3b` 模型，合同为 `deploy/test_contract.docx`；未连接公司内网。每轮保持单次请求 60 秒、LLM 并发 5，只有测试副本的 `llm.stream` 在两轮之间切换，详细日志用于核对实际请求。

非流式一轮三份合同全部成功，均为 18/18 要点完成，报告下载成功；总计 242 次实际 LLM 发送，包含输出校验后的修复。三个 worker 的所有发送均记录 `transport=unified_llm`、`stream=false`，每次独立请求预算为 60 秒。该轮总墙钟约 347 秒，Redis 采样 LLM 峰值为 5，结束后名额及等待记录均为 0。

流式一轮三份合同也全部成功，均为 18/18 要点完成，报告下载成功；总计 270 次实际 LLM 发送。所有发送均记录 `transport=unified_llm`、`stream=true`，成功接收的响应均有 `stream_complete=true`。该轮总墙钟约 410 秒，Redis 采样 LLM 峰值为 5，结束后名额及等待记录均为 0。逐轮耗时包含业务反思及输出修复，不作为两种接收方式的性能结论。

| 模式 | 同时提交的真实任务 | 最终要点状态 | 实际 LLM 发送 |
| --- | --- | --- | --- |
| 非流式 | `20261019-181811-1705`、`20261019-181812-c21c`、`20261019-181811-4354` | 每份 18/18，无失败要点 | 84、79、79 |
| 流式 | `20261019-182409-0a33`、`20261019-182409-013c`、`20261019-182409-b0da` | 每份 18/18，无失败要点 | 95、72、103 |

流式任务 `20261019-182409-013c` 的 H18 提供了实际模型超时后的恢复记录，同一逻辑操作内依次发生：

1. 首次请求首块耗时约 0.506 秒，接收未在 60 秒内完成；约 60.001 秒处记录 `ModelCallTimeout`，`stream_complete=false`，usage 未知。
2. 退避约 0.5 秒后，重新取得名额，获得新的约 60 秒；约 11.869 秒完整返回，但 JSON 校验不通过。
3. 修复请求重新取得名额，再获得新的约 60 秒；约 3.329 秒返回并通过校验。逻辑操作及合同最终均成功。

这证明收到首块不代表完整响应已经成功，也不重置 60 秒时限；传输超时和输出修复共用发送额度，但每次发送独立计时。该轮总时长小于 600 秒租约，结束名额已为 0，没有等待租约自然到期。断流恢复另由真实本地 HTTP 与 Redis 故障注入验证，不把它称为外部模型实际发生的故障。

Linux 合同容器使用 OpenAI SDK 2.54.0、Celery 5.6.3、Kombu 5.6.2、redis-py 8.1.0。新增的 12 项测试在该容器中也全部通过，使用隔离的测试 DB 15，不触碰合同正在使用的模型 DB 0 和队列 DB 2。本地完整回归的 OpenAI SDK 为 2.44.0；两套已安装 SDK 均验证了统一发送代码。内网镜像及模型服务的流式兼容性仍需内网部署后确认，默认配置保持非流式。

完整请求、API 状态、报告、Redis 采样和独立核对结果保存在本地验证目录：`C:\Users\lenovo\AppData\Local\Temp\contract-stream-live-20261019-slj_qwsi`，汇总见其中的 `verification.json`，真实超时恢复见 `real-timeout-recovery.json`。测试临时服务在验证后清理，原有 v6/v7 服务保留。

### 13.7 流式内容空闲计时与自动续期（2026-10-19）

本轮不新增配置，不修改等待预算、合同硬时限及模型并发上限。`llm.stream` 默认仍为 `false`；设为 `true` 后，`model_call_timeout_seconds: 60` 限制连续无新增内容的时间，非流式保留单次请求总时限。

- Linux 容器完整回归发现 396 个用例，395 项通过，1 项 Celery 硬时限专项因缺少专用测试参数跳过。随后对隔离 Redis 补跑该专项通过；合计 396 个不同用例全部通过。代码编译和 Ruff 检查通过。
- 用真实本地 HTTP、OpenAI SDK 和隔离 Redis 验证持续内容超过初始时限仍成功、首段及中途停顿、角色/空块/心跳不延期、断流丢弃部分结果、超时重试、取消、合同总截止、续期失败以及名额释放。
- 三个独立进程同时产生 15 个请求竞争 5 个名额，覆盖两种通信模式及请求超过租约长度的情况；HTTP 服务观测并发不超过 5。过期或被删除的名额不能通过续期重新创建；续期失败取消请求，清理失败不覆盖原始错误，残留名额可按租约回收。
- 另用真实经过的 60 秒而非缩短的测试时限验证：受控流式请求持续返回内容，运行 64.406 秒后成功；6 秒测试 lease 在此期间续期 32 次。另一条请求首段返回后停止输出，在约 60 秒无内容后超时，重新获得完整计时并成功重试；结束时持有者、等待者及等待截止记录均为 0。该项是本地受控 SDK 流加真实 Redis，不是外部模型生成耗时。
- Celery 专项使用真实 prefork 进程：2 秒测试硬时限约 2.168 秒终止执行进程及关联工具，另测进程丢失，后续任务均由补充进程成功执行。

真实合同使用独立 Compose 项目的 API、三个 Celery worker 和 Redis，通过共享挂载的源码与独立数据目录执行；复用 `lexora:v9` 镜像中的运行时和依赖，未替换原 v6/v7 服务。输入为同一份 `deploy/test_contract.docx`。调用本机配置的外网 `qwen3.6-35b-a3b`，未连接公司内网模型。验证副本开启详细日志，其余模型控制参数沿用本机配置。最终副本的 212 个 Python 源文件与工作区逐一比较一致。

| 模式 | 任务 | 任务耗时 | 最终结果 | 报告批注数 |
| --- | --- | --- | --- | --- |
| 非流式 | `20261019-214604-87ad` | 310 秒 | 18/18，0 错误 | 50 |
| 非流式 | `20261019-214605-fb14` | 366 秒 | 18/18，0 错误 | 53 |
| 非流式 | `20261019-214605-648d` | 285 秒 | 18/18，0 错误 | 48 |
| 流式 | `20261019-215256-8aa1` | 253 秒 | 18/18，0 错误 | 46 |
| 流式 | `20261019-215256-f698` | 280 秒 | 18/18，0 错误 | 47 |
| 流式 | `20261019-215256-79fa` | 261 秒 | 18/18，0 错误 | 49 |

每组均由三个不同 worker 同时执行。两组 LLM 名额采样峰值均为 5，结束后各模型持有者、等待者和等待截止记录均为 0。六份的 `task.json`、API 和日志终态均为成功，报告下载均返回 HTTP 200；所有 LLM 请求均记录 `unified_llm` 及对应通信模式。

非流式实际发送 232 次 LLM 请求，最长 32.383 秒；流式实际发送 252 次，最长 33.916 秒，最长无新增内容间隔 1.617 秒。非流式有 29 次、流式有 35 次中间输出校验失败，均在现有修复机制内恢复，最终没有要点错误。本轮真实外网合同未触发模型超时；超时及断流后的恢复由上述故障注入测试验证，不能把模拟故障说成外网实际故障。

监控收集遇到 Windows 长路径问题后恢复读取，复用原非流式任务，没有重新提交。流式监控另出现一次状态查询 HTTP 404，随后对同一任务重查为 HTTP 200，最终状态一致；这次短暂 404 的原因不确定，本轮未修改状态读取逻辑，也未重新提交合同。

完整证据保存在本机 `C:\Users\lenovo\AppData\Local\Temp\contract-stream-idle-20261019-9nc601f4` 的 `verification.json`、回归日志、60 秒验证结果及两组真实任务日志/报告中。以上验证不替代公司内网模型服务的部署复测。

## 14. 代码入口

| 文件与入口 | 输入 → 输出或作用 |
| --- | --- |
| [config.py](C:/Users/lenovo/Desktop/contract_agent/config.py:247) | 配置字典 → 拒绝旧字段和冲突格式 |
| [config.py](C:/Users/lenovo/Desktop/contract_agent/config.py:483) | 已加载配置 → 不含 Redis 密码的运行审计 |
| [task_queue/review_queue.py](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_queue.py:9) | client_dir、task_id → 持久化消息编号并入 DB 0 |
| [task_queue/review_tasks.py](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_tasks.py:21) | 任务参数 → worker 执行及真实业务状态 |
| [task_queue/review_request.py](C:/Users/lenovo/Desktop/contract_agent/task_queue/review_request.py:39) | Celery 超时/进程失败事件 → 父进程业务失败处理 |
| [endpoints/runtime/model_budget.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/model_budget.py:73) | 累计排队、请求总时限或内容空闲时限、任务截止 → 剩余时间及发送额度 |
| [endpoints/runtime/model_calls.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/model_calls.py:78) | 模型类型、发送和校验函数 → 按请求独立计时，流式进展更新计时，并在共同发送上限内重试 |
| [endpoints/runtime/llm_client.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_client.py:82) | SDK resource 和请求参数 → 按 `llm.stream` 接收、记录新增内容并拼接完整响应，禁止绕过公共控制器 |
| [endpoints/runtime/llm_semaphore.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_semaphore.py:78) | 类型、key、容量及等待预算 → Lua 原子名额申请、续期及释放 |
| [endpoints/runtime/llm_semaphore.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/runtime/llm_semaphore.py:50) | 外部取消 → 等待同一次有界清理结束后传播取消 |
| [agents/base_agent.py](C:/Users/lenovo/Desktop/contract_agent/agents/base_agent.py:304) | Agent 生成请求 → 公共 LLM 并发和预算控制 |
| [agents/json_utils.py](C:/Users/lenovo/Desktop/contract_agent/agents/json_utils.py:74) | JSON/schema 及模型响应 → 同一操作最多 6 次发送内生成和修复，每次请求按流式/非流式规则重新计时 |
| [agents/orchestrator.py](C:/Users/lenovo/Desktop/contract_agent/agents/orchestrator.py:809) | schema 合法的结论及业务反馈 → 最多 2 次新的完整结论修订 |
| [agents/orchestrator.py](C:/Users/lenovo/Desktop/contract_agent/agents/orchestrator.py:1259) | 要点及总尝试上限 → 复用成功结果，或最多执行该要点 3 次 |
| [tools/docxindex/llm/client.py](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/llm/client.py:96) | DocxIndex 提示及校验函数 → 公共 LLM 并发和预算控制 |
| [tools/docxindex/vector/client.py](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/vector/client.py:71) | 一批文本 → 公共 embedding 并发和预算控制 |
| [tools/docxindex/reranker.py](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/reranker.py:67) | 查询和候选文档 → 独立重排模型的索引及分数 |
| [tools/docxindex/indexing/llm_summary.py](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/indexing/llm_summary.py:119) | 批量摘要结果 → 保存有效摘要，独立生成未覆盖节点 |
| [tools/docxindex/indexing/llm_summary.py](C:/Users/lenovo/Desktop/contract_agent/tools/docxindex/indexing/llm_summary.py:170) | 单节点摘要 → 原操作内压缩修复或本地截断已有候选 |
| [tools/retrieval/llamaindex/limited_llm.py](C:/Users/lenovo/Desktop/contract_agent/tools/retrieval/llamaindex/limited_llm.py:34) | LlamaIndex LLM 请求 → 公共 LLM 并发和预算控制 |
| [tools/retrieval/llamaindex/limited_embedding.py](C:/Users/lenovo/Desktop/contract_agent/tools/retrieval/llamaindex/limited_embedding.py:28) | LlamaIndex 查询/文本/批量向量请求 → 公共 embedding 并发和预算控制 |
| [loggers/call_log.py](C:/Users/lenovo/Desktop/contract_agent/loggers/call_log.py:250) | 逐次实际发送记录 → 操作汇总、重试及 token 完整性 |
| [endpoints/review/web_history.py](C:/Users/lenovo/Desktop/contract_agent/endpoints/review/web_history.py:10) | 完成的 Web 任务 → 历史和下载元数据 |
| [main.py](C:/Users/lenovo/Desktop/contract_agent/main.py:85) | 本地 DOCX、服务地址和凭据 → 通过 API 获取报告 |
