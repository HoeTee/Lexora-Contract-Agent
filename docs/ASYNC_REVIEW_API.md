# 异步审查 API

本文说明普通外部客户使用的 `/api` 异步合同审查接口。

系统不提供同步 `POST /api/review`。调用方先提交任务，再查询任务状态，任务成功后请求导出结果。

## 1. 鉴权与客户管理

每个 `/api` 请求都必须在 `Authorization` 请求头中传入平台分配的密钥：

```http
Authorization: <api_key>
```

服务端接受原始 key 或 `Bearer <api_key>`。平台负责该 key 的鉴权；本服务只用其 SHA-256 指纹查找本地客户映射。

管理员在本地登记平台 key 映射。`profiles/api_clients.json` 只保存 key 的 SHA-256 指纹，不保存明文 key。

以下命令用于拥有源码的本地开发机，应在项目根目录执行。受保护 Docker 镜像没有明文脚本，应进入挂载了相同 `profiles/`、`data/` 的服务容器后，使用 `/app/host -m scripts.manage_api_clients` 执行；完整说明见 `docs/MANAGEMENT_CLI.md`。

| 命令 | 输入 | 输出或作用 |
| --- | --- | --- |
| `register` | `--client-id`、`--api-key` | 注册一个客户映射。重复的 `client_id` 或 `api_key` 会报错。 |
| `list` | 无 | 列出全部已注册客户。 |
| `check` | `--client-id` | 显示一个客户的注册状态和任务数量。 |
| `list --client-id` | `--client-id` | 以块状格式显示该客户的全部任务。 |
| `list --client-id --compact` | `--client-id` | 以紧凑单行格式显示该客户的全部任务。 |
| `reset-api-key` | `--client-id`、`--api-key` | 替换客户的平台 key 映射。新 key 不能属于其他客户。 |
| `disable` | `--client-id` | 禁用该客户的 `/api` 调用权限。 |
| `enable` | `--client-id` | 恢复该客户的 `/api` 调用权限。 |
| `delete` | `--client-id` | 仅移除鉴权映射，已有任务数据会保留。 |

```powershell
python scripts/manage_api_clients.py register --client-id "client_a" --api-key "platform-key-for-client-a"
python scripts/manage_api_clients.py list
python scripts/manage_api_clients.py check --client-id "client_a"
python scripts/manage_api_clients.py list --client-id "client_a"
python scripts/manage_api_clients.py list --client-id "client_a" --compact
python scripts/manage_api_clients.py reset-api-key --client-id "client_a" --api-key "new-platform-key"
python scripts/manage_api_clients.py disable --client-id "client_a"
python scripts/manage_api_clients.py enable --client-id "client_a"
python scripts/manage_api_clients.py delete --client-id "client_a"
```

受保护容器示例：

```bash
docker exec -it lexora bash
/app/host -m scripts.manage_api_clients list
/app/host -m scripts.manage_api_clients check --client-id "client_a"
```

旧记录若只包含 `secret_hash` 或 `secret_fingerprint`，不能用于平台 key 映射。请对该 `client_id` 执行一次 `reset-api-key --api-key "<platform_api_key>"`，脚本会移除旧字段并写入 `api_key_fingerprint`。

## 2. 异步调用流程

```text
POST /api/review/jobs
  -> 返回 202 Accepted 和 task_id

POST /api/review/jobs/status
  -> 返回 pending、queued、running、succeeded、failed 或 cancelled

POST /api/review/jobs/result
  -> 仅 succeeded 后可输出结果 DOCX 文件流或 URL
```

当 `queue.enabled=true` 时，提交接口只创建任务并投递到队列，审查 workflow 由独立 Celery worker 进程执行。状态查询仍然只读取 `task.json`，外部 API 路径、鉴权方式和请求体不变。这个设计的关键是进程隔离：`/status` 所在的 API 进程不再直接承载 workflow、MCP、LLM 调用和 DOCX 生成。

Celery task、Redis broker 和 worker 进程的区别见 `docs/QUEUE_WORKER.md`。

`GET /api/review/jobs/{task_id}` 和 `POST /api/review/jobs/{task_id}/result` 保留为兼容接口。`POST /api/review/jobs/{task_id}/cancel` 仍使用 path 中的 `task_id`，只能取消 `pending` 或 `queued` 任务。`running` 表示任务已经进入 `workflow.run()`，该 API 不支持中止。

| 状态 | 含义 |
| --- | --- |
| `pending` | 任务已被系统接受并写入存储，worker 尚未进入执行资源判断。 |
| `queued` | worker 已接手该任务，但并发已满，任务正在等待执行槽位。 |
| `running` | 已获得执行槽位，审查工作流正在运行。 |
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

如果 `config.yaml` 中 `api.require_request_model_config: true`，服务不会采用根目录 `.env` 和 `config.yaml` 中的模型配置，提交任务时必须在请求体中同时传入模型密钥和模型名称：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `llm_api_key` | 字符串 | 是 | 本次任务使用的 LLM 密钥。 |
| `llm_model_name` | 字符串 | 是 | 本次任务使用的 LLM 模型名称。 |
| `embedding_api_key` | 字符串 | 是 | 本次任务使用的 Embedding 密钥。 |
| `embedding_model_name` | 字符串 | 是 | 本次任务使用的 Embedding 模型名称。 |
| `reranker_api_key` | 字符串 | 是 | 本次任务使用的 Reranker 密钥。 |
| `reranker_model_name` | 字符串 | 是 | 本次任务使用的 Reranker 模型名称。 |

如果 `api.require_request_model_config: false`，服务采用根目录 `.env` 中的模型密钥和 `config.yaml` 中的模型名称，请求体中的上述字段不会被使用。Reranker 请求体风格不由客户传入，服务会根据 `reranker_model_name` 在 `config.yaml` 的 `rerank.provider_model_names` 中查找对应 provider。

`api.meta_fields` 定义任务提交接口接受的业务元字段。当前配置为 `templateCode` 和 `serialNo`。`api.meta_required: true` 时，所有配置字段必须是非空字符串，缺少或空白会在生成 `task_id` 前返回 `400`。`api.meta_required: false` 时字段可选，传入的值仍会保存到任务状态中；无论是否必填，已传入的字段都必须是字符串。

**本地文件上传**

```http
POST /api/review/jobs
Content-Type: multipart/form-data
Authorization: <api_key>
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | DOCX 文件 | 是 | 待审查的合同文件。 |
| `criteria_file` | DOCX 文件 | 否 | 本次审查标准；不传时使用默认审查标准。 |
| `templateCode` | 字符串 | 由 `api.meta_required` 决定 | 业务模板编号。 |
| `serialNo` | 字符串 | 由 `api.meta_required` 决定 | 业务流水号。 |

PowerShell 示例：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -F "file=@C:/Users/lenovo/Desktop/contract.docx" `
  -F "criteria_file=@C:/Users/lenovo/Desktop/criteria.docx" `
  -F "templateCode=template_001" `
  -F "serialNo=serial_001" `
  -F "llm_api_key=<llm_api_key>" `
  -F "llm_model_name=<llm_model_name>" `
  -F "embedding_api_key=<embedding_api_key>" `
  -F "embedding_model_name=<embedding_model_name>" `
  -F "reranker_api_key=<reranker_api_key>" `
  -F "reranker_model_name=<reranker_model_name>"
```

**URL 输入**

```http
POST /api/review/jobs
Content-Type: application/json
Authorization: <api_key>
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_url` | URL 字符串 | 是 | 待审查合同 DOCX 的下载地址。 |
| `criteria_file_url` | URL 字符串 | 否 | 本次审查标准 DOCX 的下载地址；不传时使用默认审查标准。 |
| `templateCode` | 字符串 | 由 `api.meta_required` 决定 | 业务模板编号。 |
| `serialNo` | 字符串 | 由 `api.meta_required` 决定 | 业务流水号。 |

PowerShell 示例：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: platform-key-for-client-a" `
  -H "Content-Type: application/json" `
  -d '{ "file_url": "https://example.com/contract.docx", "criteria_file_url": "https://example.com/criteria.docx", "templateCode": "template_001", "serialNo": "serial_001", "llm_api_key": "<llm_api_key>", "llm_model_name": "<llm_model_name>", "embedding_api_key": "<embedding_api_key>", "embedding_model_name": "<embedding_model_name>", "reranker_api_key": "<reranker_api_key>", "reranker_model_name": "<reranker_model_name>" }'
```

`application/json` 的文件输入只支持 URL：`file_url` 必填，`criteria_file_url` 可选；此外可以传入配置的业务元字段，以及启用请求级模型配置时所需的模型字段。URL 文件会先下载到任务 `input/` 目录，再执行与本地上传一致的 DOCX 校验。保存文件名优先使用下载响应 `Content-Disposition` 中的 `filename*` / `filename`，没有时使用最终 URL path、原始 URL path，仍取不到时使用默认 fallback。

**成功响应**

HTTP `202`：

```json
{
  "task_id": "20260714-143119-5ece",
  "status": "pending",
  "message": "Review job submitted."
}
```

**提交期错误响应**

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

模型调用失败时，状态查询响应的 `error` 会包含 `http_status` 和完整错误文本 `detail`：

```json
{
  "code": "agent_model_call_failed",
  "message": "模型调用超时或重试失败（Agent 模型）",
  "component": "agent",
  "http_status": 429,
  "detail": "模型调用超时或重试失败（Agent 模型）：Error code: 429 - ..."
}
```

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
    "criteria_source": "default",
    "criteria_filename": null,
    "stored": true
  },
  "output": {
    "result_filename": "【已AI审查】contract.docx",
    "ready": false
  },
  "logs": {
    "enabled": true
  }
}
```

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

该接口通过请求体中的 `task_id` 指定任务，通过 `output_type` 指定输出方式。

| `output_type` | 响应类型 | 调用方处理方式 |
| --- | --- | --- |
| `file` | DOCX 文件流 | 使用 `-o` 或等价方式保存为 `.docx` 文件。 |
| `url` | JSON | 读取响应中的 `url` 字段。 |

`task_id` 必填；`output_type` 可省略，省略时使用 `api.result_output_default`。默认配置为 `file`，兼容已有调用方。主接口不能省略整个请求体。

**文件流输出**

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

**URL 输出**

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

**URL 输出配置**

URL 输出依赖以下配置：

```yaml
api:
  keep_output: true
  result_output_default: "file"
  result_upload_enabled: false
  result_upload_domain: "http://64.202.33.42:30843"
  result_upload_path: "/openapi/agentar/v1/attachment/batchUploadAttachmentFile.json"
  result_upload_authorization: ""
  result_upload_timeout_seconds: 60
```

上传请求等价于：

```bash
curl -X POST "http://64.202.33.42:30843/openapi/agentar/v1/attachment/batchUploadAttachmentFile.json" \
  -H "Authorization: <result_upload_authorization>" \
  -F "files=@/path/to/【已AI审查】contract.docx;type=application/vnd.openxmlformats-officedocument.wordprocessingml.document"
```

`keep_output=false` 只在 URL 输出上传成功后清理本地结果文件；文件流输出不能在响应发送前删除本地结果文件。

上传成功后，服务会从上传接口 JSON 响应中的常见 URL 字段解析结果地址，例如 `url`、`fileUrl`、`downloadUrl` 以及它们在 `data` 或 `data[0]` 下的形式；如果解析不到 URL，接口返回 `502`。

**错误响应**

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

**兼容接口**

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
| `POST /api/review/jobs/result` 未传 `task_id` | `400` | 在 JSON body 中传 `{ "task_id": "...", "output_type": "file" }`。 |
| `POST /api/review/jobs/{task_id}/result` 传 `output_path` | 仍返回文件流，不会写入该路径。 | 使用 `curl.exe -o` 或客户端代码保存响应体。 |
| PowerShell `curl.exe` 未使用 `-o` | 文件内容输出到终端。 | 使用上文的 `-o "C:\...\review_result.docx"` 写法。 |
