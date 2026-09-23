# 日志设计

## 当前任务日志（2026-09）

新任务的完整模型请求和响应统一写入 `logs/calls/`；文件名包含来源、调用阶段、UTC 时间和 `call_id`，格式见[任务日志文件名阅读指南](TASK_LOG_FILENAME_GUIDE.md)。每条记录在发出请求前先以 `pending` 状态落盘，再更新为 `succeeded` 或 `failed`。记录包含 `operation_id`、`criterion_id`、`component`、`stage`、`model`、请求、响应或异常、`http_status`。超时发生在收到 HTTP 响应前时，`http_status` 为 `null`；`failure_stage=semaphore_wait` 表示等待全局并发许可时失败。这里可能包含合同正文，应按敏感业务数据保护。

`trace.json` 由任务 worker 写入，包含 `criterion.H12`、`mcp.contract_search`、DocxIndex 内部模型调用、`llm.SubAgent_H12` 等节点。节点中的 `call_id` 和 `call_path` 指向完整调用记录。DocxIndex 路线规划、执行步骤、回退和结果写入 `logs/route_events/*.json` 并导入同一 trace；确定性步骤不会伪装成模型调用。DocxIndex MCP 进程只写自身的调用与路线记录，不直接修改 worker 的 `trace.json`。

`state/criteria/H12.error.json` 保留各次失败的 `origin`、`failure_stage`、上游异常类型、`http_status`、`call_id` 和 `call_path`。旧任务文件缺少这些字段时按原格式查询。`events.log` 用于任务生命周期以及每个最终失败要点的一条摘要；高频路线细节不再写入此文件。`task.json.logs.api_events_path` 是 `events_log_path` 的兼容别名。

新任务不再重复写入 `logs/conversations/*.json`；该目录仅供旧任务读取。`logging.enabled=false` 时不写 `calls`、`route_events`、`trace`，但要点错误状态仍保存。API/Web 分区与原有任务目录一致；DocxIndex MCP 服务必须看到同一持久化数据挂载，否则无法写入任务调用记录。

日志是任务级排障数据，不是任务状态源。API 和 Web 都以 `task.json` 保存生命周期状态；只有 `logging.enabled=true` 时才写入 `logs/`。

按单条 API 审查要点定位输入、结果及缺失记录时，见 [API 审查要点记录排查](API_CRITERION_LOG_TROUBLESHOOTING.md)。

## 目录边界

```text
data/api/<task_id>/
  task.json
  logs/

data/web/<tenant_id>/<task_id>/
  task.json
  logs/
```

API 与 Web 日志不能混用。Web 历史按 `tenant_id` 定位任务，再按 `task.json.username` 过滤用户。

请求头、Content-Type、JSON 语法或必填字段错误属于 pre-task error：不生成 `task_id`，也不创建空任务目录。进入任务生命周期后的失败必须保留 `task.json`；开启日志时同时保留排障日志。

## 日志文件

| 文件 | 作用 | 主要实现 |
| --- | --- | --- |
| `events.log` | 文件处理、任务状态、模型调用和外部 HTTP 事件 | `loggers/api_event_logger.py` |
| `trace.json` | workflow、criterion、LLM、MCP 的树状执行轨迹和耗时 | `loggers/trace_logger.py`、`trace_helpers.py` |
| `review_outputs.json` | 完整结构化审查结果快照 | `loggers/workflow_logger.py` |
| `mcp.log` | MCP 连接、工具调用与异常 | `loggers/mcp_logger.py` |
| `conversations/*.json` | Planner、SubAgent、Reflector、Summarizer 的模型上下文 | `loggers/agent_logger.py` |

`trace.json` 只保存摘要化输入输出；完整审查结果放在 `review_outputs.json`。日志不得记录完整 API key、密码或带签名 query string 的 URL。

`phase.summarize` span 会记录 `raw_issue_count`、`confirmed_issue_count`、`presentation_issue_count`、`presentation_anchor_count`、`opening_notices` 和 `semantic_deduplication`。`semantic_deduplication` 包含已应用、跳过及删除的问题数量；Summarizer 只标记重复组，Workflow 校验 issue_key 后执行，不允许模型改变锚点或任意删除独立问题。

事件名称以 `loggers/api_event_logger.py` 和实际调用点为准，不在文档中维护第二份枚举。

## 路径职责

- `endpoints/review/task_store.py`：生成并解析 API/Web 任务、输入、输出、状态和日志路径。
- `loggers/resolve_review_task_paths.py`：生成 workflow 使用的合同、审查标准、报告和日志路径。
- `loggers/review_history.py`：读取租户任务的 `task.json`，兼容不带租户上下文的旧 `review_history.json`。

当前租户 Web 新任务只写入：

```text
data/web/<tenant_id>/<task_id>/
```

旧的 `data/<username>/records/review_history.json` 和带合同名的日志目录仅用于历史数据兼容，不是新任务写入结构。

## 日志开关

```yaml
logging:
  enabled: true
```

关闭后，任务目录、`task.json`、输入和输出仍会创建，但不会写入 `events.log`、`trace.json`、`review_outputs.json`、`mcp.log` 或 `conversations/`。

## 审查输出质量契约更新

见[审查质量与批注规则](REVIEW_QUALITY.md)：`finding_decisions`、`unresolved_issues` 和 pending 状态保存在任务日志中，不进入用户 DOCX；计划状态记录唯一固定审查配置 `criteria-routes.yaml` 的哈希，用于恢复校验。
