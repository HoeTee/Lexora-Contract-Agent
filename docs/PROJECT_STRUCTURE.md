# 项目结构

本文说明当前主要目录职责。普通 `/api`、浏览器 `/web`、后续 OA `/oa` 应保持边界清楚。

## 入口

- `app.py`：FastAPI 服务入口，注册 `/api`、`/web`、`/oa` 路由和静态文件。
- `main.py`：本地 CLI 审查入口。
- `config.py`：集中读取系统配置、路径和环境变量。API client 身份不在这里配置。
- `queue/`：普通 `/api` 审查任务队列层。API 完成请求校验并创建合法任务后投递 `client_dir/task_id`，Celery worker 调用审查执行入口。

## HTTP 路由

- `endpoints/api/client_mapping.py`：读取平台提供的 `Authorization` key，并从本地 `api_clients.json` 映射出 `client_id/client_dir`。
- `endpoints/api/review_jobs.py`：普通 `/api` 异步任务接口，提供提交、查询、结果文件流输出、结果 URL 输出和取消任务。
- `endpoints/oa/review.py`：OA 专用同步 API，接收 OA 文件和元字段，完成后按配置回传结果。
- `endpoints/web/user_routes.py`：浏览器普通用户页面和表单路由，URL 以 `/web` 开头。
- `endpoints/web/admin_routes.py`：管理端页面和表单路由，URL 以 `/web/admin` 开头。
- `endpoints/web/admin_services.py`：管理端展示所需的数据聚合。
- `endpoints/runtime/`：HTTP 层运行时支撑，包括认证、DOCX 校验、文件名、JSON 响应、错误类型和并发控制。

## 审查支撑

- `workflow/workflow.py`：合同审查主工作流，串联解析、建索引、规划、执行、汇总和报告生成。
- `mcp_service/client/client.py`：MCP 客户端封装，负责连接 MCP server、拉取工具列表和执行工具调用。
- `mcp_service/server/server.py`：MCP server 脚本，暴露文档解析、检索建索引、检索查询和报告生成工具。
- `protect/`：正式镜像保护构建入口，负责 Python 字节码打包、AES-256-GCM 加密、loader 共享库与 host 编译，并为 VMP 虚拟化阶段提供源码和构建入口；最终由根目录 `Dockerfile` 组装 `host + libloader.vmp.so + code.bin` 生产镜像。
- `endpoints/review/task_store.py`：读写 API/Web 任务状态、输入、输出和日志路径。
- `endpoints/review/job_worker.py`：执行异步审查后台任务。
- `endpoints/review/response.py`：构造对外 API 响应，避免暴露服务端路径和 secret hash。
- `endpoints/review/result_upload.py`：结果 URL 输出使用的附件上传 helper，沿用 `httpx.AsyncClient` multipart 上传。
- `endpoints/review/meta.py`：旧 meta fields helper；普通 `/api` 当前不再使用。
- `endpoints/review/callbacks.py`：旧 callback helper；普通 `/api` 异步主流程不依赖 callback。

## 服务和脚本

- `scripts/manage_users.py`：Web 租户用户管理脚本；提交到 Git，并在生产构建时加密进 `code.bin`。
- `scripts/manage_tenants.py`：Web 租户管理脚本；提交到 Git，并在生产构建时加密进 `code.bin`。
- `endpoints/web/user_management.py`：`/web` 管理端使用的用户管理逻辑。
- `scripts/manage_api_clients.py`：`/api` client 映射管理脚本，支持 register、list、check、reset-api-key、enable、disable、delete；提交到 Git，并在生产构建时加密进 `code.bin`。
- `scripts/api_review_callback_receiver.py`：本地测试 callback 的临时接收服务；普通 `/api` 异步主流程不依赖 callback，不提交到 Git，也不进入 Docker 构建上下文。

`scripts/docx_retrieval_eval/` 和 `scripts/docxindex/` 是本地评测/开发目录，不提交到 Git，也不进入 Docker 构建上下文。受保护容器中的管理 CLI 用法见 `docs/MANAGEMENT_CLI.md`。

## 身份文件

```text
profiles/
  tenant_profiles.json                 # Web 租户注册表
  tenants/
    <tenant_id>/
      profiles.json               # 某租户下的 Web 用户
      criteria/
        <username>.docx                # 某用户默认审查要点
  api_clients.json                     # /api 客户端映射
```

`api_clients.json` 保存 `client_id`、启用状态和 `api_key_fingerprint`。平台 API key 明文不会写入磁盘；`/api` 只使用该 key 解析本地 client 映射。

## 数据目录

普通 `/api` 数据目录：

```text
data/api/<task_id>/
  task.json
  input/
  output/
  logs/
  state/
```

Web 数据目录：

```text
data/web/<tenant_id>/<task_id>/
  task.json
  input/
  output/
  logs/
```

`client_id` 是业务身份。`client_dir` 为兼容既有 client 配置继续记录在 `task.json` 中，但普通 `/api` 不再按 `client_dir` 分目录。

普通 `/api` 的 `data/api/<task_id>/` 只对应已经进入任务生命周期的合法提交。请求协议错误、JSON 格式错误或提交字段缺失不应创建任务目录；如果接口响应返回了 `task_id`，该目录下应存在对应 `task.json` 状态记录。

## API 边界

普通 `/api`：

- 只做异步任务。
- 平台 key 映射到 `client_id/client_dir`。
- 按 `task_id` 直接存储，读取时校验 `client_dir` 归属。
- 提交任务先校验请求头和请求体，再生成 `task_id` 和任务目录。
- 不接收 `metafields`。
- 不依赖 callback。

浏览器 `/web`：

- 依赖 session/cookie。
- 面向页面、表单、历史记录和下载。
- 按租户和任务分区存储在 `data/web/<tenant_id>/<task_id>/`。

OA `/oa`：

- 使用单独同步入口。
- 应使用独立鉴权和 callback 逻辑。
