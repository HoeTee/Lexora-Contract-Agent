# `/api` 审查接口说明

当前普通 `/api` 只提供异步任务接口：

```text
POST /api/review/jobs
POST /api/review/jobs/status
POST /api/review/jobs/result
POST /api/review/jobs/{task_id}/cancel
```

`GET /api/review/jobs/{task_id}` 和 `POST /api/review/jobs/{task_id}/result` 保留为兼容接口；推荐新调用方在 JSON body 中传 `task_id`。

普通外部 API 不提供同步 `POST /api/review`；OA 专用同步接口为 `POST /oa/review`。

## API 边界

`/api` 面向普通外部客户：

- 平台负责 `Authorization` key 鉴权；服务仅用该 key 映射 `client_id/client_dir`。
- 只做异步任务提交、查询、结果文件流输出或 URL 输出、取消。
- 不接收 `metafields`。
- 不 callback。
- 数据按 `client_id` 分区。
- `queue.enabled=true` 时，提交接口投递 Celery 队列，审查由独立 worker 执行；外部 API 不变。
- 创建任务前必须先校验请求头和请求体。请求协议错误、JSON 格式错误、提交接口缺少 `file_url`、把状态查询 body 错发到提交接口等 pre-task error 不生成 `task_id`，也不创建任务目录。

OA 集成不属于 `/api`。OA 后续应放在 `/oa`，使用独立鉴权和 callback 流程。

## API Client 管理

API client 不在 `config.yaml` 配置。使用脚本管理：

```powershell
python scripts/manage_api_clients.py register --client-id "client_a" --api-key "<platform_key>"
python scripts/manage_api_clients.py list
python scripts/manage_api_clients.py check --client-id "client_a"
python scripts/manage_api_clients.py list --client-id "client_a"
python scripts/manage_api_clients.py list --client-id "client_a" --compact
python scripts/manage_api_clients.py reset-api-key --client-id "client_a" --api-key "<new_platform_key>"
python scripts/manage_api_clients.py disable --client-id "client_a"
python scripts/manage_api_clients.py enable --client-id "client_a"
python scripts/manage_api_clients.py delete --client-id "client_a"
```

`list` shows registered clients. `check --client-id` shows one client detail. `list --client-id` shows tasks under that client in block format; add `--compact` for one-line rows with truncated filenames. `delete --client-id` removes only the auth mapping and keeps task data.

数据保存位置：

```text
profiles/api_clients.json
```

File stores `api_key_fingerprint`, not a plaintext platform key. The platform key is used only to resolve the local client mapping; run `reset-api-key` when the platform key changes.

## 请求鉴权

Submit jobs with `multipart/form-data`; the platform key must be passed through `Authorization`:

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs" `
  -H "Authorization: <platform_api_key>" `
  -F "file=@C:\path\contract.docx"
```

Status, result output, and cancel use the same `Authorization` header. Body, form, query string, and `X-API-Key` are not used for API key mapping.

文件上传使用 `-F` 发送 multipart 字段；JSON 请求使用 `-H "Content-Type: application/json"` 和 `-d`。不要把 `Content-Type` 写成 `-F "Content-Type: application/json"`。

状态查询推荐写法：

```powershell
curl.exe -X POST "http://localhost:5000/api/review/jobs/status" `
  -H "Authorization: <platform_api_key>" `
  -H "Content-Type: application/json" `
  -d '{ "task_id": "20260712-144123-dccf" }'
```

以上是拥有源码的本地开发命令。受保护镜像不包含明文脚本；进入 `lexora` 容器后，将启动部分替换为：

```bash
/app/host -m scripts.manage_api_clients register --client-id "client_a" --api-key "<platform_key>"
/app/host -m scripts.manage_api_clients list
```

其余子命令和参数不变。完整说明见 `docs/MANAGEMENT_CLI.md`。

结果输出推荐写法：

```json
{
  "task_id": "20260712-144123-dccf",
  "output_type": "url"
}
```

Auth failures:

| 情况 | 状态码 |
| --- | --- |
| Missing `Authorization` | `400` |
| Platform key has no local client mapping | `400` |
| Client mapping is disabled | `403` |

## 数据目录

任务目录：

```text
data/api/<task_id>/
  task.json
  input/
  output/
  logs/
```

只有合法提交进入任务生命周期后，才会创建上述目录并写入 `task.json`。`/api/review/jobs` 收到错误请求体时，不应留下空任务目录；如果响应中返回了 `task_id`，磁盘上也应存在对应的 `task.json` 状态记录。

`client_id` 是业务身份；`client_dir` 为兼容既有 client 配置继续记录在 `task.json` 中，但不再参与目录分区。`task.json` 会记录两者：

```json
{
  "client_id": "某某行社",
  "client_dir": "某某行社",
  "task_id": "20260712-144123-dccf",
  "status": "pending"
}
```

普通 API 响应不会返回服务端绝对路径，也不会返回 `api_key_fingerprint`。

## 代码位置

- `scripts/manage_api_clients.py`：读写 `api_clients.json`，登记平台 key 指纹映射。
- `endpoints/api/client_mapping.py`：根据平台 `Authorization` key 映射 `/api` 客户目录。
- `endpoints/api/review_jobs.py`：异步任务 API 路由。
- `queue/`：普通 `/api` 审查任务队列层，负责 Celery app、task 和投递 helper。
- `endpoints/review/result_upload.py`：结果 URL 输出使用的附件上传 helper，沿用 `httpx.AsyncClient` multipart 上传。
- `endpoints/review/task_store.py`：按 client 分区的 task 存储。
- `endpoints/review/job_worker.py`：异步审查 worker。
- `endpoints/review/response.py`：过滤对外响应。

## 存储开关

`config.yaml` 中仍保留运行时存储开关：

```yaml
api:
  keep_input: true
  keep_output: true
logging:
  enabled: true
```

- `output/` 会先写入结果文件；`api.keep_output=false` 时，URL 输出上传成功后清理本地结果文件。
- `input/` 是否保留由 `api.keep_input` 控制。
- `logs/` 及其中的 API events、workflow、trace、MCP、conversation 日志统一由 `logging.enabled` 控制。
