# 日志设计

日志相关代码统一放在 `loggers/` 目录下。`resolve_review_task_paths.py` 负责生成任务编号和所有输入、输出、日志路径；其他 logger 模块只负责写日志。

## 任务目录

当前租户 Web 审查的每个合同都会创建一个独立任务目录：

```text
data/web/<tenant_id>/<task_id>/
```

其中输入、输出、任务状态和日志统一按任务保存：

```text
input/
output/
logs/
task.json
```

`task_id` 不包含合同文件名，避免长文件名继续放大 Windows 路径长度；合同原文件名、提交用户和任务状态从 `task.json` 读取。

无登录 API 审查使用独立日志目录，不写入用户合同、报告和历史记录目录：

```text
data/api/<task_id>/logs/
```

该目录只在 `config.yaml` 中 `logging.enabled: true` 时写入；关闭时 API 任务不会创建 `events.log`、`trace.json`、`review_outputs.json`、`mcp.log`、conversation 日志，`task.json.logs.*` 为 `null`。

`<任务目录>` 由 `endpoints/review/task_store.py` 生成，格式为 `YYYYMMDD-HHMMSS-xxxx`。直接 API 的完整接口和错误响应说明见 `docs/API_REVIEW_ENDPOINT.md`。

API 任务目录只在合法提交进入任务生命周期后创建。请求头、Content-Type、JSON 语法或提交字段错误属于 pre-task error，不应创建 `data/api/<task_id>/`，也不应写入空的 `events.log` 或 `task.json`。如果错误响应返回 `task_id`，必须能通过该 `task_id` 读到对应 failed task 和排障日志。

任务日志目录结构：

```text
events.log
trace.json
review_outputs.json
mcp.log
conversations/
```

## 路径解析

模块：

```text
loggers/resolve_review_task_paths.py
```

职责：

```text
为单次审查任务生成任务 ID，以及上传合同、批注输出、审查要点和各类日志的完整路径。
```

主要生成：

```text
stored_contract_path
final_report_path
criteria_path
task_log_dir
conversation_log_dir
api_events_path
trace_path
review_outputs_path
mcp_log_path
```

## 日志读取与合同名关联

当前租户 Web 任务的历史记录来自：

```text
data/web/<tenant_id>/<task_id>/task.json
```

页面按 `tenant_id` 定位租户任务目录，再以 `task.json.username` 过滤当前用户。该文件同时建立 `task_id` 与合同名的对应关系。关键字段包括：

```json
{
  "task_id": "164455_f78fbf27",
  "contract_original_name": "原始合同文件名.docx",
  "contract_stored_name": "20260614_164455_f78fbf27_原始合同文件名.docx",
  "report_stored_name": "20260614_164455_f78fbf27_【已AI审查】原始合同文件名.docx"
}
```

当前读取流程如下：

1. 枚举 `data/web/<tenant_id>/*/task.json`。
2. 按 `task.json.username` 过滤用户，并读取合同名、报告名与任务状态。
3. 按 `task_id` 定位日志目录：

```text
data/web/<tenant_id>/<task_id>/logs/
```

4. 打开日志详情时读取该目录下的 `events.log`、`trace.json`、`review_outputs.json`、`mcp.log`、`conversations/`。

不带租户上下文的旧任务仍兼容以下历史结构：

```text
data/<username>/records/review_history.json
data/<username>/logs/<YYYY-MM-DD>/<task_id>_<contract_stem>/
```

这只是旧数据兼容路径，不是当前租户 Web 任务的新写入结构。

## Review Outputs

模块：

```text
loggers/workflow_logger.py
```

文件：

```text
review_outputs.json
```

记录内容：

```text
完整审查结构化输出，包括每条 criterion 的 status、issues、anchors、quoted_text、comment_text、reasoning 和 tokens。该文件是业务结果快照，不是执行轨迹。
```

workflow 阶段流转、耗时、token 统计和任务总览写入 `trace.json` 的 `workflow.run.outputs`，不再生成 `workflow/` 目录、`workflow_*.md` 或 `run_summary.json`。

## Agent 对话日志

模块：

```text
loggers/agent_logger.py
```

文件：

```text
conversations/Planner_*.json
conversations/SubAgent_C1_*.json
conversations/Reflector_*.json
conversations/Summarizer_*.json
```

记录内容：

```text
agent 调用大模型时的 messages、system prompt、user prompt、assistant response、tool message 等对话上下文。
```

## MCP 日志

模块：

```text
loggers/mcp_logger.py
```

文件：

```text
mcp.log
```

记录内容：

```text
MCP client 的连接、断开、工具列表和异常信息。
```

## Trace 日志

模块：

```text
loggers/trace_logger.py
loggers/trace_helpers.py
```

文件：

```text
trace.json
```

记录内容：

```text
单次审查任务的树状执行轨迹。每个 run 都包含 id、parent_id、name、type、status、start_time、end_time、duration_seconds、inputs、outputs、metadata 和 error。
```

`name` 由埋点位置决定，例如 `workflow.run`、`phase.execute`、`criterion.C1`、`llm.SubAgent_C1`、`mcp.contract_search`。`inputs` 和 `outputs` 由 `trace_helpers.py` 做摘要化生成，避免把完整合同、完整检索结果和密钥写入 trace。任务总览字段放在 `workflow.run.outputs`，完整审查结构放在 `review_outputs.json`。

## API 事件日志

模块：

```text
loggers/api_event_logger.py
```

文件：

```text
events.log
```

该文件由 Python `logging` 写入，格式与 MCP logger 保持一致。这里用于追加记录 API 层事件，例如上传文件、DOCX 校验通过、审查开始、审查完成和失败原因。

### API Event 枚举

当前 `events.log` 会记录以下事件：

```text
upload_received              收到上传请求和原始文件名
api_review_received          收到无登录 API 审查请求和原始文件名
criteria_uploaded            本次任务上传了临时审查要点
contract_saved               合同文件已保存到用户数据目录
contract_url_downloaded      合同 URL 已下载到任务 input 目录
criteria_url_downloaded      审查要点 URL 已下载到任务 input 目录
contract_url_download_failed 合同 URL 下载失败
criteria_url_download_failed 审查要点 URL 下载失败
result_url_uploaded          结果 DOCX 已上传并生成 URL
result_url_upload_failed     结果 DOCX 上传 URL 失败
docx_validation_passed       DOCX 文件格式校验通过
review_started               后台审查任务开始执行
agent_model_call_failed      Agent 大模型调用超时或重试失败
embedding_call_failed        Embedding 模型调用超时或重试失败
reranker_call_retry          Reranker 模型调用发生一次重试
reranker_call_failed         Reranker 模型调用超时或重试失败
review_completed             审查完成并生成批注 DOCX
review_failed                审查任务失败，记录最终失败原因
```

文件保存类事件会记录本地路径和文件大小：

```text
2026-08-12 10:00:00,000 - APIEvents... - INFO - contract_saved task_id=... file_path="..." size_bytes=12345
2026-08-12 10:00:01,000 - APIEvents... - INFO - result_url_uploaded task_id=... file_path="..." size_bytes=12345 http_status=200 url=https://example.com/result.docx
```

URL 下载或结果上传失败事件会记录上游 HTTP 状态码；连接失败、超时或没有 HTTP response 时 `http_status` 为 `null`。日志中的 `source_url` 不包含 query string，避免记录 OSS 签名参数：

```text
2026-08-12 10:00:00,000 - APIEvents... - ERROR - contract_url_download_failed task_id=... source_url=https://example.com/a.docx http_status=403 error="..."
2026-08-12 10:00:01,000 - APIEvents... - ERROR - result_url_upload_failed task_id=... file_path="..." http_status=502 error="..."
```

模型调用失败事件会记录组件和 HTTP 状态码；连接错误或超时时 `http_status` 为 `null`：

```text
2026-08-12 10:00:00,000 - APIEvents... - ERROR - agent_model_call_failed task_id=... component=agent http_status=429 error="..."
```

模型类失败事件会先记录具体组件事件，再记录 `review_failed`。前端 `/work` 页面会显示模型类失败的具体错误文案；OA 同步 API `/oa/review` 会返回 JSON 错误信息；非模型类异常仍显示或返回通用失败提示并要求查看任务日志。

## 日志开关

根目录 `config.yaml` 中的唯一开关控制是否写入任务日志：

```yaml
logging:
  enabled: true
```

如果设置为 `false`，任务目录仍会用于状态和结果，但不会创建 `logs/` 及其中的 `events.log`、`trace.json`、`review_outputs.json`、`mcp.log`、`conversations/`。
