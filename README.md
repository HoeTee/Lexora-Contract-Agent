# 合同审查 Agent

合同审查 Agent 提供三种 DOCX 合同审查入口：

- Web 页面：面向浏览器操作的合同审查。
- 异步 API：面向平台系统集成。
- 本地 CLI：面向一次性本地审查任务。

系统会读取合同 DOCX，加载审查要点，运行 agent 审查工作流，并生成带批注的 DOCX 审查结果。

## 快速开始

本地启动服务：

```powershell
uvicorn app:app --host 0.0.0.0 --port 5000
```

浏览器访问：

```text
http://127.0.0.1:5000
```

本地 CLI 审查：

```powershell
python main.py --contract .\contract.docx
```

API 集成从这些接口开始：

- `POST /api/review/jobs`
- `POST /api/review/jobs/status`
- `POST /api/review/jobs/result`

请求和响应细节见 [docs/ASYNC_REVIEW_API.md](docs/ASYNC_REVIEW_API.md)。

## 配置

从 `.env.example` 创建 `.env`，至少填写：

```env
LLM_API_KEY=...
LLM_MODEL_NAME=
EMBED_API_KEY=...
EMBEDDING_MODEL_NAME=
RERANK_API_KEY=...
RERANKER_MODEL_NAME=
SESSION_SECRET_KEY=replace-with-a-long-random-secret
```

主要运行配置在 [config.yaml](config.yaml)，Python 配置加载入口是 [config.py](config.py)。

关键配置区域：

- `llm`：审查模型地址和生成限制。
- `embedding`：向量模型地址。
- `rerank`：reranker 地址、模型名、provider 模型名映射和可选 instruction。
- `workflow`：并发、重试、超时和 subagent 工具白名单。
- `api`：异步 API 文件留存、结果 URL 上传、元数据字段、回调和 URL 下载限制。
- `retrieval`：选择 `llamaindex` 或 `docxindex`，并配置两套检索参数。
- `logging`：API events、workflow、trace、MCP 和 conversation 的统一日志开关。

reranker 配置说明见 [docs/RERANKER_CONFIGURATION.md](docs/RERANKER_CONFIGURATION.md)。

## 数据目录

运行数据使用项目内固定目录：

```text
data/
  api/
    <task_id>/
  web/
    <tenant_id>/
      <task_id>/

profiles/
  tenant_profiles.json
  tenants/
    <tenant_id>/
      profiles.json
      criteria/
        <username>.docx
  api_clients.json
```

API 和 Web 任务数据保持隔离：

- API 任务：`data/api/<task_id>/`
- Web 任务：`data/web/<tenant_id>/<task_id>/`
- Web 租户注册表：`profiles/tenant_profiles.json`
- Web 用户配置：`profiles/tenants/<tenant_id>/profiles.json`
- API client 映射：`profiles/api_clients.json`

任务目录中保存 `task.json`、输入文件、输出文件，以及开启日志后的任务级日志。
任务目录只在合法提交进入任务生命周期后创建；请求头、Content-Type 或请求体错误不应生成 `task_id` 或空任务目录。

## API

主要异步 API：

- `POST /api/review/jobs`：提交 DOCX 文件或 URL 审查任务。
- `POST /api/review/jobs/status`：通过 JSON body 查询任务状态。
- `GET /api/review/jobs/{task_id}`：通过路径参数查询任务状态。
- `POST /api/review/jobs/result`：以文件或 URL 形式返回结果。
- `POST /api/review/jobs/{task_id}/result`：通过路径 task id 返回结果。
- `POST /api/review/jobs/{task_id}/cancel`：请求取消任务。

API 文档：

- [docs/ASYNC_REVIEW_API.md](docs/ASYNC_REVIEW_API.md)
- [docs/API_REVIEW_ENDPOINT.md](docs/API_REVIEW_ENDPOINT.md)
- [docs/LOGGER_DESIGN.md](docs/LOGGER_DESIGN.md)

## Web

Web 流程复用 review worker 和 task-store 状态模型，但用户任务数据保存到 `data/web/`。

相关文档：

- [docs/FRONTEND_USER_JOURNEY.md](docs/FRONTEND_USER_JOURNEY.md)
- [docs/WEB_TENANT_USAGE.md](docs/WEB_TENANT_USAGE.md)
- [docs/USER_MANAGEMENT.md](docs/USER_MANAGEMENT.md)

## CLI

使用系统默认审查要点：

```powershell
python main.py --contract .\contract.docx
```

使用自定义审查要点：

```powershell
python main.py --contract .\contract.docx --criteria .\criteria.docx
```

指定输出路径：

```powershell
python main.py --contract .\contract.docx --criteria .\criteria.docx --output .\contract_reviewed.docx
```

## Docker

生产镜像采用双层源码保护：项目 Python 模块先编译并通过 AES-256-GCM 加密为 `code.bin`，负责密钥重建和解密的 loader 共享库再经过 VMP 虚拟化。容器通过 `/app/host` 加载 `/app/libloader.vmp.so`，最终镜像不包含项目 `.py`、明文 `.pyc`、未加壳 `libloader.so` 或 VMP 构建工具。

API client、租户和用户管理脚本同样编译、加密进 `code.bin`。本地开发使用 `python scripts/manage_*.py`；受保护容器内使用 `/app/host -m scripts.manage_*`。完整命令和数据边界见 [管理 CLI](docs/MANAGEMENT_CLI.md)。

运行数据应持久化到容器外：

```yaml
volumes:
  - ./data:/app/data
  - ./profiles:/app/profiles
  - ./resources/criteria/criteria.docx:/app/resources/criteria/criteria.docx:ro
```

Docker 部署和排障文档：

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- [docs/SOURCE_PROTECTION.md](docs/SOURCE_PROTECTION.md)
- [docs/MANAGEMENT_CLI.md](docs/MANAGEMENT_CLI.md)
- [docs/DOCKER_NETWORK_TROUBLESHOOTING.md](docs/DOCKER_NETWORK_TROUBLESHOOTING.md)

## 项目文档

架构和工作流：

- [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)
- [docs/AGENT_WORKFLOW_ARCHITECTURE.md](docs/AGENT_WORKFLOW_ARCHITECTURE.md)
- [docs/DOCX_XML_ANCHOR_INDEXING.md](docs/DOCX_XML_ANCHOR_INDEXING.md)
- [docs/DOCX_ANNOTATION_DESIGN.md](docs/DOCX_ANNOTATION_DESIGN.md)

运维和配置：

- [docs/QUICK_START.md](docs/QUICK_START.md)
- [docs/LOGGER_DESIGN.md](docs/LOGGER_DESIGN.md)
- [docs/RERANKER_CONFIGURATION.md](docs/RERANKER_CONFIGURATION.md)
- [docs/REVIEW_BOUNDARIES.md](docs/REVIEW_BOUNDARIES.md)
