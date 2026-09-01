# 日志设计

日志是任务级排障数据，不是任务状态源。API 和 Web 都以 `task.json` 保存生命周期状态；只有 `logging.enabled=true` 时才写入 `logs/`。

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
