# 异步审查 API

本文说明普通外部客户使用的 `/api` 异步合同审查接口。

系统不提供同步 `POST /api/review`。调用方先提交任务，再查询任务状态，任务成功后请求导出结果。

## 1. 鉴权与客户管理

每个 `/api` 请求都必须在 `Authorization` 请求头中传入平台分配的密钥：

```http
Authorization: <api_key>
```

服务端接受原始 key 或 `Bearer <api_key>`。平台负责该 key 的鉴权；本服务只用其 SHA-256 指纹查找本地客户映射。

管理员通过管理 CLI 登记平台 key 映射；`profiles/api_clients.json` 只保存 SHA-256 指纹，不保存明文 key。本地 `.venv` 和 VMP 受保护容器的两种命令形式、完整子命令及参数见 [管理 CLI 手册](MANAGEMENT_CLI.md)。

## 2. 异步调用流程

```text
POST /api/review/jobs
  -> 返回 202 Accepted 和 task_id

POST /api/review/jobs/status
  -> 返回 pending、queued、running、succeeded、failed 或 cancelled

POST /api/review/jobs/result
  -> 仅 succeeded 后可输出结果 DOCX 文件流或 URL
```

Web 和 API 固定将审查任务投递到 Redis DB 0 的 Celery 队列，由三个独立 worker 执行；每个 worker 同时执行一份合同。不存在 `queue.enabled` 或进程内审查回退分支，OA 入口已移除。状态查询仍然读取共享数据目录中的 `task.json`，外部 API 路径、鉴权方式和请求体不变。API 进程不直接承载 workflow、MCP、LLM 调用和 DOCX 生成。

worker 内的模型请求统一经过 Redis DB 2 限流。`workflow.model_call_timeout_seconds` 同时覆盖名额等待、HTTP、退避和同一次调用的输出修复；重试共用原截止时间。`queue.queued_timeout_seconds` 默认 7200 秒，只控制排队等待；`queue.task_time_limit_seconds` 默认 1800 秒，控制 worker 执行整份合同的最长时间。硬超时后任务记录为失败，不自动重新提交；不配置软时限。各项配置及迁移说明见 [并发与超时配置说明](CONCURRENCY_TIMEOUT_CONFIGURATION.md)。

Celery task、Redis broker 和 worker 的部署边界见 [部署说明](DEPLOYMENT.md)，workflow 内部并发与恢复边界见 [智能体与工作流架构](AGENT_WORKFLOW_ARCHITECTURE.md)。

`GET /api/review/jobs/{task_id}` 和 `POST /api/review/jobs/{task_id}/result` 保留为兼容接口。`POST /api/review/jobs/{task_id}/cancel` 仍使用 path 中的 `task_id`，只能取消 `pending` 或 `queued` 任务。`running` 表示任务已经进入 `workflow.run()`，该 API 不支持中止。

| 状态 | 含义 |
| --- | --- |
| `pending` | 任务已被系统接受并写入存储，尚未完成入队。 |
| `queued` | 任务已成功写入 Redis 队列，正在等待空闲 Worker 接手。 |
| `running` | worker 已接手任务，审查工作流正在运行。 |
| `succeeded` | 审查输出已生成，可导出。 |
| `failed` | 工作流失败，状态查询响应中包含 `error`。 |
| `cancelled` | 任务在开始前已取消。 |

## 3. 提交任务

```http
POST /api/review/jobs
Authorization: <api_key>
```

该接口按 `Content-Type` 分流，且只支持以下两种请求体：

| `Content-Type` | 输入方式 | 必填字段 | 可选字段 |
| --- | --- | --- | --- |
| `multipart/form-data` | 上传二进制 DOCX 文件 | `file` | `criteria_file` |
| `application/json` | 通过 URL 下载 DOCX | `file_url` | `criteria_file_url` |

提交接口必须先完成请求头和请求体校验，再进入任务生命周期。`Content-Type` 不受支持、JSON 语法错误、JSON body 不是对象、缺少 `file_url`、把状态查询 body 错发到提交接口等请求协议或请求体错误，直接返回 HTTP 错误，不生成 `task_id`，也不创建 `data/api/<task_id>/` 目录。

只有请求已经被识别为合法的任务提交后，服务才会生成 `task_id`、创建任务目录、保存输入文件、写入 `task.json` 并投递执行。此后发生的文件保存、URL 下载、DOCX 校验、审查标准校验或 worker 执行失败，才属于 task-level error，可以返回带 `task_id` 的失败任务结构。

### 3.1 提交参数如何判断是否必填

提交参数分为三类：

| 参数类别 | 字段 | 是否必填 |
| --- | --- | --- |
| 固定必填 | `Authorization` 请求头 | 所有请求都必填。 |
| 固定必填 | multipart 的 `file`，或 JSON 的 `file_url` | 二选一，由 `Content-Type` 决定。 |
| 固定可选 | `criteria_file` 或 `criteria_file_url` | 可选；上传后在系统硬性审查之外追加执行，不传时只执行硬性审查。 |
| 条件必填 | `api.meta_fields` 配置的业务字段 | `api.meta_required: true` 时全部必填；为 `false` 时可选。 |
| 条件必填 | 六个请求级模型字段 | `api.require_request_model_config: true` 时全部必填；为 `false` 时不要传，服务端不会使用。 |

当前仓库的 `config.yaml` 配置为：

```yaml
api:
  require_request_model_config: false
  meta_required: true
  meta_fields:
    - "templateCode"
    - "serialNo"
```

因此，当前配置下的最小提交必须包含合同输入、`templateCode` 和 `serialNo`；`criteria_file` / `criteria_file_url` 可省略，六个模型字段不需要传。

如果将 `api.require_request_model_config` 改为 `true`，提交任务时还必须同时传入以下六个字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `llm_api_key` | 字符串 | 是 | 本次任务使用的 LLM 密钥。 |
| `llm_model_name` | 字符串 | 是 | 本次任务使用的 LLM 模型名称。 |
| `embedding_api_key` | 字符串 | 是 | 本次任务使用的 Embedding 密钥。 |
| `embedding_model_name` | 字符串 | 是 | 本次任务使用的 Embedding 模型名称。 |
| `reranker_api_key` | 字符串 | 是 | 本次任务使用的 Reranker 密钥。 |
| `reranker_model_name` | 字符串 | 是 | 本次任务使用的 Reranker 模型名称。 |

当 `api.require_request_model_config: false` 时，服务采用根目录 `.env` 中的模型密钥和 `config.yaml` 中的模型名称，请求体中的上述字段不会被使用。Reranker 请求体风格不由客户传入；启用请求级模型配置后，服务根据 `reranker_model_name` 在 `config.yaml` 的 `rerank.provider_model_names` 中查找 provider。

`api.meta_fields` 定义任务提交接口接受的业务元字段。当前配置为 `templateCode` 和 `serialNo`。`api.meta_required: true` 时，所有配置字段必须是非空字符串，缺少或空白会在生成 `task_id` 前返回 `400`。`api.meta_required: false` 时字段可选，传入的值仍会保存到任务状态中；无论是否必填，已传入的字段都必须是字符串。

### 3.2 本地文件上传

```http
POST /api/review/jobs
Content-Type: multipart/form-data
Authorization: <api_key>
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | DOCX 文件 | 是 | 待审查的合同文件。 |
| `criteria_file` | DOCX 文件 | 否 | 本次追加的用户审查标准；不传时只执行系统硬性审查。 |
| `templateCode` | 字符串 | 当前配置必填 | `api.meta_required: true`，因此必须是非空字符串。 |
| `serialNo` | 字符串 | 当前配置必填 | `api.meta_required: true`，因此必须是非空字符串。 |

当前配置的最小请求，只包含必填参数：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -F "file=@C:/Users/lenovo/Desktop/contract.docx" `
  -F "templateCode=template_001" `
  -F "serialNo=serial_001"
```

需要为本次任务追加用户审查标准时，再增加可选的 `criteria_file`：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -F "file=@C:/Users/lenovo/Desktop/contract.docx" `
  -F "templateCode=template_001" `
  -F "serialNo=serial_001" `
  -F "criteria_file=@C:/Users/lenovo/Desktop/criteria.docx"
```

只有将 `api.require_request_model_config` 改为 `true` 后，才在上述请求中追加六个模型字段；六个字段必须同时提供，不能只传其中一部分。

### 3.3 URL 输入

```http
POST /api/review/jobs
Content-Type: application/json
Authorization: <api_key>
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_url` | URL 字符串 | 是 | 待审查合同 DOCX 的下载地址。 |
| `criteria_file_url` | URL 字符串 | 否 | 本次追加的用户审查标准 DOCX 下载地址；不传时只执行系统硬性审查。 |
| `templateCode` | 字符串 | 当前配置必填 | `api.meta_required: true`，因此必须是非空字符串。 |
| `serialNo` | 字符串 | 当前配置必填 | `api.meta_required: true`，因此必须是非空字符串。 |

当前配置的最小请求，只包含必填参数：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "file_url": "https://example.com/contract.docx", "templateCode": "template_001", "serialNo": "serial_001" }'
```

需要为本次任务追加用户审查标准时，再增加可选的 `criteria_file_url`：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "file_url": "https://example.com/contract.docx", "templateCode": "template_001", "serialNo": "serial_001", "criteria_file_url": "https://example.com/criteria.docx" }'
```

只有将 `api.require_request_model_config` 改为 `true` 后，才在 JSON 对象中追加六个模型字段；六个字段必须同时提供。

`application/json` 的文件输入只支持 URL：`file_url` 必填，`criteria_file_url` 可选；此外可以传入配置的业务元字段，以及启用请求级模型配置时所需的模型字段。URL 文件会先下载到任务 `input/` 目录，再执行与本地上传一致的 DOCX 校验。保存文件名优先使用下载响应 `Content-Disposition` 中的 `filename*` / `filename`，没有时使用最终 URL path、原始 URL path，仍取不到时使用默认 fallback。

### 3.4 成功响应

HTTP `202`：

```json
{
  "task_id": "20260714-143119-5ece",
  "status": "queued",
  "message": "Review job is waiting for an execution slot."
}
```

### 3.5 提交期错误响应

如果请求尚未进入任务生命周期，接口返回普通 HTTP 错误结构，不返回 `task_id`。例如把状态查询请求体发到提交接口：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "task_id": "20260714-143119-5ece" }'
```

该请求不是合法的 URL 提交任务请求，应返回 `400`，且不创建任务目录。

如果请求已通过请求头和请求体校验并生成 `task_id`，但文件保存、URL 下载、DOCX 校验或审查标准内容校验失败，接口返回标准失败结构：

```json
{
  "task_id": "20260714-143119-5ece",
  "status": "failed",
  "message": "Contract file URL download failed.",
  "error": {
    "code": "CONTRACT_URL_DOWNLOAD_FAILED",
    "message": "Contract file URL download failed.",
    "http_status": null
  }
}
```

模型调用失败时，状态查询响应的 `error` 固定包含上游 `http_status`、异常分类 `error_type` 和完整错误文本 `detail`：

```json
{
  "code": "agent_model_call_failed",
  "message": "模型调用失败（Agent 模型）",
  "component": "agent",
  "http_status": 429,
  "error_type": "RateLimitError",
  "detail": "模型调用失败（Agent 模型）：Error code: 429 - ..."
}
```

如果模型接口返回 HTTP 200，但响应内容无法通过程序 Schema 校验，`http_status` 仍记录为 `200`，`error_type` 为 `response_validation_error`。如果连接失败或超时且上游没有产生 HTTP 响应，`http_status` 为 `null`，`error_type` 和 `detail` 仍保留真实异常信息。状态查询接口本身成功读取任务时仍返回 HTTP 200；这里的 `error.http_status` 表示上游模型服务状态，不表示状态查询接口状态。

鉴权失败发生在 `task_id` 生成前，因此仍返回 FastAPI 错误结构。

| HTTP 状态码 | 返回场景 | 响应 |
| --- | --- | --- |
| `202` | 任务已接收。 | 任务 ID、初始状态和消息。 |
| `400` | JSON body 不是合法 JSON object、提交接口缺少 `file_url`、或把状态查询 body 错发到提交接口。 | `{ "detail": "..." }` 或不含 `task_id` 的错误结构。 |
| `400` | `api.require_request_model_config=true` 时缺少请求级模型配置字段，或 `reranker_model_name` 不在 `rerank.provider_model_names` 映射中。 | 不进入任务生命周期，不返回 `task_id`。 |
| `400` | `api.meta_required=true` 时缺少配置的元字段，或传入的元字段不是字符串。 | 不进入任务生命周期，不返回 `task_id`。 |
| `400` | 已生成 `task_id` 后，合同或审查标准不是有效 DOCX，或审查标准内容不符合要求。 | `{ "task_id": "...", "status": "failed", "message": "...", "error": {...} }` |
| `415` | 请求 `Content-Type` 不受支持。 | 不进入任务生命周期，不返回 `task_id`。 |
| `502` | 已生成 `task_id` 后，合同 URL 或审查标准 URL 下载失败。 | `{ "task_id": "...", "status": "failed", "message": "...", "error": {...} }` |
| `400` | 未传 `Authorization`，或平台 key 没有本地客户映射。 | `{ "detail": "Authorization header is required." }` 或 `{ "detail": "API key has no local client mapping." }` |
| `403` | 该客户映射已禁用。 | `{ "detail": "API client mapping is disabled." }` |
| `400` | 缺少必填的 `file` multipart 字段。 | 不含 `task_id` 的 `FILE_REQUIRED` 错误结构。 |
| `500` | 客户鉴权映射格式错误或密钥匹配到多个客户。 | `{ "detail": "..." }` |

## 4. 查询任务状态

```http
POST /api/review/jobs/status
Content-Type: application/json
Authorization: <api_key>
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | 字符串 | 是 | 要查询的任务 ID。 |

```json
{
  "task_id": "20260714-143119-5ece"
}
```

PowerShell 示例：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs/status" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "task_id": "20260714-143119-5ece" }'
```

成功响应，HTTP `200`：

```json
{
  "task_id": "20260714-143119-5ece",
  "status": "running",
  "created_at": "2026-07-14T14:31:19+08:00",
  "started_at": "2026-07-14T14:31:20+08:00",
  "finished_at": null,
  "message": "Contract review is running.",
  "error": null,
  "meta_fields": {
    "templateCode": "template_001",
    "serialNo": "serial_001"
  },
  "input": {
    "contract_filename": "contract.docx",
    "criteria_source": "mandatory",
    "criteria_filename": null,
    "stored": true
  },
  "output": {
    "result_filename": "【已AI审查】contract.docx",
    "ready": false
  },
  "quality": {
    "total_count": 18,
    "completed_count": 7,
    "incomplete_count": 0,
    "error_count": 0,
    "not_started_count": 8,
    "running_count": 3,
    "incomplete_criteria": [],
    "error_criteria": []
  },
  "criteria": [
    {
      "id": "H01",
      "criterion": "审查要点原文",
      "execution_status": "completed",
      "review_result": "issues_found",
      "error": null
    },
    {
      "id": "H02",
      "criterion": "审查要点原文",
      "execution_status": "running",
      "review_result": null,
      "error": null
    }
  ]
}
```

任务状态为 `succeeded` 表示输出 DOCX 已成功生成。`criteria` 始终展示全部审查项；`execution_status` 为 `not_started / running / completed / incomplete / error`。`incomplete` 仅表示 Reflector 达到最大轮次后仍未完成确认，模型调用超时、结构化输出错误等异常在统一模型预算及重试额度耗尽后记为 `error`；不自动重跑整条要点。`review_result` 仅在执行产生有效审查结果时填写。调用方应同时读取 `quality` 汇总和 `criteria` 明细，不能只根据任务级 `status` 判断所有标准均已完成。

| HTTP 状态码 | 返回场景 | 响应 |
| --- | --- | --- |
| `200` | 已鉴权客户拥有该任务。 | 经过过滤的任务状态，不返回服务端文件系统路径。 |
| `400` | 未传 `Authorization`，或平台 key 没有本地客户映射。 | `{ "detail": "..." }` |
| `403` | 该客户映射已禁用。 | `{ "detail": "API client mapping is disabled." }` |
| `404` | 任务 ID 不合法、不存在，或属于其他客户。 | `{ "detail": "Task not found." }` |
| `500` | 客户鉴权映射格式错误或密钥匹配到多个客户。 | `{ "detail": "..." }` |

兼容接口：

```http
GET /api/review/jobs/{task_id}
Authorization: <api_key>
```

## 5. 获取已完成结果

```http
POST /api/review/jobs/result
Content-Type: application/json
Authorization: <api_key>
```

该接口通过请求体中的 `task_id` 指定任务，通过可选的 `output_type` 覆盖默认输出方式。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | 字符串 | 是 | 已完成任务的 ID。 |
| `output_type` | `file` 或 `url` | 否 | 省略时使用 `api.result_output_default`；当前默认值是 `file`。 |

| `output_type` | 响应类型 | 调用方处理方式 |
| --- | --- | --- |
| `file` | DOCX 文件流 | 使用 `-o` 或等价方式保存为 `.docx` 文件。 |
| `url` | JSON | 读取响应中的 `url` 字段。 |

`task_id` 必填；`output_type` 可省略，省略时使用 `api.result_output_default`。默认配置为 `file`，兼容已有调用方。主接口不能省略整个请求体。

当前配置的最小请求，只包含必填参数，并按默认值返回文件流：

```powershell
curl.exe --fail -X POST "http://localhost:5000/api/review/jobs/result" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "task_id": "20260714-143119-5ece" }' `
  -o "C:\Users\lenovo\Desktop\review_result.docx"
```

### 5.1 文件流输出

下面显式传入可选的 `output_type: file`，用于不依赖服务端默认值的调用方。

请求体：

```json
{
  "task_id": "20260714-143119-5ece",
  "output_type": "file"
}
```

PowerShell 示例：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs/result" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "task_id": "20260714-143119-5ece", "output_type": "file" }' `
  -o "C:\Users\lenovo\Desktop\review_result.docx"
```

成功时返回 HTTP `200`，响应体是 DOCX 文件流：

```http
Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document
```

### 5.2 URL 输出

URL 输出不是默认值，必须显式传入 `output_type: url`，并要求服务端启用结果上传。

请求体：

```json
{
  "task_id": "20260714-143119-5ece",
  "output_type": "url"
}
```

PowerShell 示例：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs/result" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "task_id": "20260714-143119-5ece", "output_type": "url" }'
```

成功响应，HTTP `200`：

```json
{
  "task_id": "20260714-143119-5ece",
  "status": "succeeded",
  "output_type": "url",
  "filename": "【已AI审查】contract.docx",
  "url": "https://example.com/【已AI审查】contract.docx"
}
```

### 5.3 URL 输出配置

URL 输出依赖以下配置：

```yaml
api:
  keep_output: true
  result_output_default: "file"
  result_upload_enabled: false
  result_upload_domain: "https://upload.example.com"
  result_upload_path: "/api/files"
  result_upload_authorization: ""
  result_upload_timeout_seconds: 60
```

上传请求等价于：

```bash
curl -X POST "https://upload.example.com/api/files" \
  -H "Authorization: <result_upload_authorization>" \
  -F "files=@/path/to/【已AI审查】contract.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document"
```

`keep_output=false` 只在 URL 输出上传成功后清理本地结果文件；文件流输出不能在响应发送前删除本地结果文件。

上传成功后，服务会从上传接口 JSON 响应中的常见 URL 字段解析结果地址，例如 `url`、`fileUrl`、`downloadUrl` 以及它们在 `data` 或 `data[0]` 下的形式；如果解析不到 URL，接口返回 `502`。

### 5.4 错误响应

| HTTP 状态码 | 返回场景 | 响应 |
| --- | --- | --- |
| `200` | 任务已成功，请求 `file` 输出。 | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` 响应体。 |
| `200` | 任务已成功，请求 `url` 输出。 | `{ "task_id": "...", "status": "succeeded", "output_type": "url", "url": "..." }` |
| `400` | `output_type` 非 `file` / `url`，或 URL 输出未启用。 | `{ "detail": "..." }` |
| `400` | 未传 `Authorization`，或平台 key 没有本地客户映射。 | `{ "detail": "..." }` |
| `403` | 该客户映射已禁用。 | `{ "detail": "API client mapping is disabled." }` |
| `404` | 任务 ID 不合法、不存在，或属于其他客户。 | `{ "detail": "Task not found." }` |
| `409` | 任务状态不是 `succeeded`。 | `{ "detail": "Task is not finished. Current status: ..." }` |
| `500` | 服务端结果源文件不存在，或客户鉴权映射异常。 | `{ "detail": "..." }` |
| `502` | URL 输出上传失败，或上传响应中无法解析 URL。 | `{ "detail": "Result file URL upload failed." }` |

### 5.5 兼容接口

```http
POST /api/review/jobs/{task_id}/result
Authorization: <api_key>
```

兼容接口可继续只传 `output_type`，也可以不传请求体并使用 `api.result_output_default`。

## 6. 取消等待中的任务

```http
POST /api/review/jobs/{task_id}/cancel
Authorization: <api_key>
```

| 参数位置 | 参数 | 必填 | 说明 |
| --- | --- | --- | --- |
| path | `task_id` | 是 | 要取消的任务 ID。 |
| body | 无 | 否 | 该接口不需要请求体。 |

PowerShell 示例：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs/20260714-143119-5ece/cancel" `
  -H "Authorization: platform-key-for-client-a"
```

成功响应，HTTP `200`：

```json
{
  "task_id": "20260714-143119-5ece",
  "status": "cancelled",
  "message": "Review job cancelled."
}
```

| HTTP 状态码 | 返回场景 | 响应 |
| --- | --- | --- |
| `200` | 当前状态是 `pending` 或 `queued`，任务已取消。 | 任务 ID、`cancelled` 和消息。 |
| `400` | 未传 `Authorization`，或平台 key 没有本地客户映射。 | `{ "detail": "..." }` |
| `403` | 该客户映射已禁用。 | `{ "detail": "API client mapping is disabled." }` |
| `404` | 任务 ID 不合法、不存在，或属于其他客户。 | `{ "detail": "Task not found." }` |
| `409` | 任务是 `running`、`succeeded`、`failed` 或已经 `cancelled`。 | `{ "detail": "..." }`，或针对 `running` 的 JSON 消息。 |
| `500` | 客户鉴权映射格式错误或密钥匹配到多个客户。 | `{ "detail": "..." }` |

## 7. 客户数据隔离

任务按照 `task_id` 直接存储在 API 根目录：

```text
data/api/<task_id>/
  task.json
  input/
  output/
  logs/
```

调用方不提交 `client_id`。平台已认证的 `Authorization` key 仅用于本地映射；服务端根据映射得到客户身份，并按 `task.json` 中记录的 `client_dir` 校验任务归属。

例如，客户 A 拥有任务 `20260714-143119-5ece`，客户 B 使用自己的有效密钥访问该任务时，返回如下：

| 客户 B 对客户 A 任务的请求 | HTTP 状态码 | 响应 |
| --- | --- | --- |
| `GET /api/review/jobs/20260714-143119-5ece` | `404` | `{ "detail": "Task not found." }` |
| `POST /api/review/jobs/status` 传客户 A 的 `task_id` | `404` | `{ "detail": "Task not found." }` |
| `POST /api/review/jobs/result` 传客户 A 的 `task_id` | `404` | `{ "detail": "Task not found." }` |
| `POST /api/review/jobs/20260714-143119-5ece/result` | `404` | `{ "detail": "Task not found." }` |
| `POST /api/review/jobs/20260714-143119-5ece/cancel` | `404` | `{ "detail": "Task not found." }` |

任务不存在时也返回同一 `404`，因此客户不能通过状态码判断其他客户的任务是否存在。

任务目录不再按 `client_dir` 分区。不要手工创建或修改 `profiles/api_clients.json`，避免 API key 映射到错误业务身份。

## 8. 常见请求错误

| 错误请求 | 返回结果 | 正确请求 |
| --- | --- | --- |
| `POST /review/jobs/{task_id}` | `404 Not Found` | 查询状态应使用 `POST /api/review/jobs/status` 并在 body 传 `task_id`。 |
| `POST /api/review/jobs/status` 未传 `task_id` | `400` | 在 JSON body 中传 `{ "task_id": "..." }`。 |
| `POST /api/review/jobs/result` 未传 `task_id` | `400` | 最小 JSON body 为 `{ "task_id": "..." }`；仅在需要覆盖默认输出方式时再传 `output_type`。 |
| `POST /api/review/jobs/{task_id}/result` 传 `output_path` | 仍返回文件流，不会写入该路径。 | 使用 `curl.exe -o` 或客户端代码保存响应体。 |
| PowerShell `curl.exe` 未使用 `-o` | 文件内容输出到终端。 | 使用上文的 `-o "C:\...\review_result.docx"` 写法。 |

## 审查输出质量契约更新

见[审查质量与批注规则](REVIEW_QUALITY.md)：`quality.incomplete_criteria` 表示 Reflector 达到轮次上限后仍未完成确认，`quality.error_criteria` 表示调用或校验重试耗尽；固定18条始终执行，上传的 `criteria_file` 作为附加用户标准进入动态规划。
