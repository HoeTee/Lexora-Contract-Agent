# 模型调用日志

更新日期：2026-10-26。本轮只修改日志与关联元数据，没有新增配置，没有调整模型输入、提示词、并发上限、RPM、超时或重试次数。

## 1. 先读 events.log，再按编号找 JSON

每个合同任务有独立的 logs 目录：

```text
data/api/<task_id>/logs/
  events.log
  logical_calls/
    27__agent__SubAgent_H01__review-initial.json
    28__agent__SubAgent_H02__review-initial.json
  calls/
    <source>__<stage>__<timestamp>__<call_id>.json
```

Web 路径为 `data/web/<tenant_id>/<task_id>/logs/`。`calls/` 仅在 `logging.detailed=true` 时写入。

以下为格式示例，时间和 token 数仅用于说明：

```text
27 2026-10-26T12:50:38.290+08:00 INFO    agent H01/SubAgent_H01/review-initial | Operation started.
27 2026-10-26T12:50:38.418+08:00 INFO    agent H01/SubAgent_H01/review-initial | Waiting for model admission.
27 2026-10-26T12:50:39.000+08:00 INFO    agent H01/SubAgent_H01/review-initial | Request 1 sent. Input: 5970 tokens. Wait: 0.582s.
27 2026-10-26T12:51:39.002+08:00 INFO    agent H01/SubAgent_H01/review-initial | Model slot released.
27 2026-10-26T12:51:39.003+08:00 ERROR   agent H01/SubAgent_H01/review-initial | Request 1 timed out after 60.002s. Input: 5970 tokens. Output: unknown.
27 2026-10-26T12:51:39.004+08:00 WARNING agent H01/SubAgent_H01/review-initial | Retry scheduled in 0.500s.
27 2026-10-26T12:51:39.510+08:00 INFO    agent H01/SubAgent_H01/review-initial | Waiting for model admission.
27 2026-10-26T12:51:39.520+08:00 INFO    agent H01/SubAgent_H01/review-initial | Request 2 sent. Input: 5970 tokens. Wait: 0.010s.
27 2026-10-26T12:52:12.563+08:00 INFO    agent H01/SubAgent_H01/review-initial | Response 2 received in 33.043s. Input: 5970 tokens. Output: 5029 tokens.
27 2026-10-26T12:52:12.565+08:00 INFO    agent H01/SubAgent_H01/review-initial | Model slot released.
27 2026-10-26T12:52:12.570+08:00 INFO    agent H01/SubAgent_H01/review-initial | Validation passed. Requests: 2. Total input: 11940 tokens. Known output: 5029 tokens (incomplete).
```

找到编号 27 后，在 `logical_calls/` 搜索 `27__*.json`，其中 attempts 保存这两次实际请求。详细 calls JSON 内同样记录 `logical_no=27`，并保留 UUID。无需从行文复制长路径或 UUID。

## 2. 每行字段

| 位置 | 含义 |
| --- | --- |
| 开头自然数 | `logical_no`：一个逻辑操作的任务内编号；HTTP 重试、同次 JSON/schema 修复沿用同一编号 |
| 时间 | 事件产生时间，带毫秒与时区；容器使用 UTC 时显示 +00:00 |
| 级别 | INFO、WARNING 或 ERROR |
| category | `agent`，或 `<检索后端>.<indexing/retrieval>.<llm/embedding/reranker>` |
| 要点/调用方/阶段 | 例如 `H01/SubAgent_H01/review-initial`；不适用字段使用 - |
| 英文摘要 | 本次事件及请求次数、耗时、已知 token 数等信息 |

普通任务事件也使用相同列。例如任务完成事件没有模型编号和类别，使用 `-`；不会借用上一条模型调用的编号。模型事件每行重复 category 和位置，单看一行也能定位。

自然数只编号逻辑操作，不编号事件，也不代表完成顺序。多个操作同时执行时，编号会交错出现。阅读先后过程以文件中的事件时间线为准；跨进程近乎同时产生的事件可以在毫秒级交错，文件写锁保证完整行。计数器和锁都位于该任务 logs 目录，重启后继续编号；进程崩溃允许跳号，不复用号码。损坏计数器明确报错，避免覆盖已有记录。历史 JSON 不改名。

## 3. 事件与结果的边界

- `Operation started`：开始一次逻辑操作，不代表已经向模型发送。
- `Waiting for model admission`：等待全局并发名额或 RPM 额度；不逐次记录 Redis 轮询。
- `Request N sent`：第 N 次实际发送，包含实际输入 token 和本次准入等待时间。
- `Response N received`：已经接收响应，包含请求耗时与已知输出；之后仍需校验。
- `failed output validation` / `failed JSON/schema validation`：收到响应但内容校验失败，不能据接收事件判断成功。
- `Request not sent`：输入校验或准入失败；本地输入计数保留，不计入服务实际发送消耗。
- `Retry scheduled`：已决定重试及退避时间，不等于已经再次发送。
- `Model slot released`：Redis 清理已完成；清理失败另记 ERROR，并依靠租约回收。日志写入失败不能跳过释放操作。
- `Validation passed` / `Operation failed`：逻辑操作最终结果，包含全部实际尝试的累计统计。

同一个逻辑操作成功时，其内部仍可包含超时或校验失败的 attempts。最终合同成功与否仍以任务/API 状态为准。

## 4. token 与详细信息

`unknown` 表示未获得输出计数，不能当作 0；流式中断后收到的部分输出标记 partial。embedding/reranker 的输出是向量/分数，文本输出 token 使用 `not applicable`。输入优先服务 usage，本地 tokenizer 计数保留在 `local_input_tokens`，作为缺失 usage 时的回退。

逻辑 JSON 保留 `logical_no`、`logical_call_id`、`category`、`task_id`、`criterion_id`、`criterion_attempt`、`source`、`stage`、attempts、错误、token 来源与完整性。`criterion_attempt` 表示整条要点重跑轮次，与同一逻辑操作的 `send_number` 不同；它通过应用/MCP 内部传递，不加入模型工具 schema。

`events.log` 与 JSON 使用同一 CallRecord/LogicalCall 的数据，时间线不另算 token。API 统计继续按 UUID `call_id` 去重，不累加事件行，避免把发送、接收、结束三个事件算成三次消耗。详细日志保存客户端记录的请求与响应明细；embedding 响应保留 usage、vector_count，不保存完整向量数组。

## 5. 配置与脚本

| 配置 | 结果 |
| --- | --- |
| logging.enabled=true、detailed=false | 事件时间线与基础 logical_calls JSON |
| logging.enabled=true、detailed=true | 再增加 calls 中的单次请求、客户端记录的响应明细 |
| logging.enabled=false | 不创建本轮日志、编号计数器或锁 |

`loggers/task_log_io.py` 管理任务内跨进程编号与完整行追加；`api_event_logger.py` 负责统一格式；`call_log.py` 负责关联和统计；`endpoints/runtime/model_calls.py` 记录等待、请求、响应、失败、重试；`model_limits.py` 在真实清理完成后记录释放。Agent/Orchestrator/MCP 负责传递所属要点及重跑轮次。

## 6. 本轮验收

自动化回归覆盖：多进程唯一编号、独立任务、重启续号、历史文件、日志开关、全类型模型事件、超时重试、取消、释放及校验后重试。真实测试以独立 Compose 项目同时提交四个 `deploy/test_contract.docx` 任务，三个 worker 各 concurrency=1，保持本机配置的模型控制参数。

证据目录：`data/verification/numbered-logs-20261026/`。测试使用本机 Docker 与已配置的外网模型服务，不代表内网部署验证。

### 验收结果

同批四个请求均在 2026-10-26 12:58:52 左右提交。使用的合同均为 `deploy/test_contract.docx` 的独立任务，源文件 SHA256 为 `40c9f049db42189abc0052d489b0655d849711657859f8b5e9951d5b67819648`。三个 worker 各执行一个任务，第四个先排队，取得空闲 worker 后执行。非流式真实模型验收镜像为 `lexora:numbered-logs-20261026-final`。

| 任务 | 最终状态 | 要点完成 | 逻辑调用 JSON | 实际模型请求 |
| --- | --- | --- | --- | --- |
| 20261026-125853-0338 | succeeded | 18/18 | 82 | 83 |
| 20261026-125854-512c | succeeded | 18/18 | 91 | 97 |
| 20261026-125853-6e63 | succeeded | 18/18 | 84 | 89 |
| 20261026-125853-57f1 | succeeded | 18/18 | 85 | 93 |

整批验收耗时 701.516 秒；`final-four/result.json` 中 `passed=true`、`failures=[]`。全部事件编号、category、要点/调用方/阶段均与逻辑 JSON 对应，每个逻辑操作只有一个最终事件；详细 calls 与 attempts 双向对应，token 数及发送状态一致。API、任务存储、审查输出和报告检查均通过。

| 服务 | 实际请求数 | 实际准入并发峰值 / 配置 | 滚动一分钟峰值 / RPM 配置 |
| --- | --- | --- | --- |
| LLM | 265 | 5 / 5 | 30 / 30 |
| embedding | 80 | 3 / 3 | 48 / 不限 |
| reranker | 17 | 1 / 10 | 6 / 不限 |

结束时三类服务的 held、waiting、deadlines 均为 0。RPM 窗口计数仍按一分钟自然过期，不属于占用名额。基础编号日志能呈现校验失败后的后续请求；最终成功不掩盖内部失败尝试。

真实超时恢复示例：任务 `20261026-125853-57f1` 的 `35__agent__SubAgent_H04__review-initial.json` 中，第 1 次请求输入 5413 token，60.011 秒超时，输出未知；释放名额并退避后，第 2 次请求仍输入 5413 token，6.008 秒返回 896 输出 token，逻辑操作最终成功。两次请求使用同一编号 35，失败输入也保留在累计统计中。

Windows 全量回归执行 527 项：526 通过、1 项 Linux 专项跳过；该专项另在 Linux 容器执行，与日志 IO 检查合计 14 项全部通过。覆盖流式/非流式、超时、断流、取消和释放。全量测试中修复了一处测试收集器竞态：第一阶段可能已消费 worker 的 done 通知，第二阶段必须继续累计，不能重新从 0 等待；没有修改生产并发或超时。

对应证据：`regression-complete.log`、`linux-probe-final.log`、`final-four/result.json`、`final-four/final_slots.json`、各任务 logs 和报告。早期 `live-four/` 是最终镜像更新前主动停止的预检查，不能作为完成验收；最终验收以 `final-four/` 为准。

真实验收结束后，仅对普通任务完成行做了格式压缩：token_stats 保留输入、输出、总数、请求次数及不完整标记；annotation_stats 保留计数；summary 保留状态和错误类型。26 项定向测试通过，并用四份真实结束事件的原始元数据重放验证，见 `format-replay/result.json`。原实测日志保留不重写。此项未修改请求执行或统计计算，没有重复调用模型；包含该格式收尾的镜像为 `lexora:numbered-logs-20261026-compact`。
