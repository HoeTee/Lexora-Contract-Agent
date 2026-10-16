# 合同审查 Agent

审查输出契约见[审查质量与批注规则](docs/REVIEW_QUALITY.md)。根目录 `routing/` 管理固定审查路线和用户标准动态路线；`tools/docxindex/` 只负责索引与检索执行。Reflector 逐条确认 finding，最终 DOCX 只写已确认问题并替换旧 AI 审查总结。

合同审查 Agent 只提供两个服务执行入口：浏览器 `/web` 和异步 `/api`；OA 已移除。Web、API 均通过 Redis DB 0 排队，由三个 Celery worker 执行。CLI 作为 API 客户端提交任务，不在本地直接运行审查。系统读取合同与审查标准，执行 agent 工作流，并输出带批注的 DOCX。

| 使用场景 | 入口 | 结果 |
| --- | --- | --- |
| 浏览器审查 | `/web` | 页面历史记录与批注 DOCX |
| 系统集成 | `/api` | 异步任务状态与 DOCX 文件流或 URL |
| 命令行提交 | CLI → `/api` | 下载批注 DOCX 到指定位置 |

## 本地准备与服务启动

安装依赖并创建配置：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

创建测试租户和用户：

```powershell
.\.venv\Scripts\python.exe scripts/manage_tenants.py create --tenant-id demo --name "Demo Tenant"
.\.venv\Scripts\python.exe scripts/manage_users.py create --tenant-id demo --username testuser --password Test123456
```

Web、API 都要求 Redis 和三个 Celery worker，不存在关闭队列后在服务进程内执行的模式。每个 worker 必须使用 `prefork` 和 `concurrency=1`，整合同硬时限依赖 Linux 进程池。完整部署优先使用三个 worker 的 Compose 配置：

```bash
docker compose up -d
```

镜像与挂载文件的准备见 [部署说明](docs/DEPLOYMENT.md)。启动后访问 `http://127.0.0.1:5000/web/login`，使用 `demo / testuser / Test123456` 登录。

手动开发部署应在同一 Linux/WSL 项目中启动 `uvicorn app:app --host 0.0.0.0 --port 5000` 和三个 `celery -A task_queue.celery_app:celery_app worker --pool=prefork --concurrency=1` 进程，三个 worker 需使用不同名称。API、worker 和 MCP 使用相同配置、可达的 Redis 地址及共享数据路径；Redis DB 0 存任务消息，DB 2 协调 LLM/embedding/reranker 请求上限。Windows 原生 `solo` 池不提供本方案的硬时限保证。Windows 开发机的 `.venv` 可用于管理、离线测试和 API 客户端，不要与看不到其绝对文件路径的容器 worker 混搭执行任务。

## 合同 CLI

CLI 需要已运行的 API、三个 worker、Redis，以及环境变量 `API_KEY` 中的已注册 API key。上传的输入来自命令所在机器，审查在服务端执行，结果下载到 `--output` 指定的位置。业务元数据的完整选项以 `--help` 为准。当前 CLI 使用服务端模型配置，需要 `api.require_request_model_config=false`。

### 开发机 `.venv` 客户端

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py --base-url http://127.0.0.1:5000 --contract .\contract.docx --criteria .\criteria.docx --output .\contract_reviewed.docx --template-code demo --serial-no cli-test
```

### VMP 受保护容器

先把输入文件放入宿主机挂载的 `data/`，再进入正在运行的容器：

```bash
docker exec -it lexora bash
/app/host -m main --base-url http://127.0.0.1:8000 --contract /app/data/contract.docx --output /app/data/contract_reviewed.docx --template-code demo --serial-no cli-test
```

命令在容器内作为 API 客户端运行；输入来自宿主机挂载的 `./data/contract.docx`，服务端通过 Celery 审查，客户端下载的输出最终写到宿主机 `./data/contract_reviewed.docx`。不要把输入或长期结果写入未挂载的容器临时目录。

## 异步 API

主要接口：

- `POST /api/review/jobs`
- `POST /api/review/jobs/status`
- `POST /api/review/jobs/result`
- `POST /api/review/jobs/{task_id}/cancel`

请求、响应、状态和错误契约见 [异步审查 API](docs/ASYNC_REVIEW_API.md)。

## 配置与数据

模型密钥放在 `.env`；模型、workflow、API、检索、队列和日志配置放在 `config.yaml`。配置加载入口是 `config.py`。各配置作用范围、默认值和旧字段迁移见 [并发与超时配置说明](docs/CONCURRENCY_TIMEOUT_CONFIGURATION.md)。模型限流固定启用，单次操作共享等待、请求和修复的 60 秒预算，默认最多发送 3 次；失败要点默认最多执行 2 次（含首次），成功结果复用，整合同仍受 Celery 硬时限约束。

合同检索保留 DocxIndex 和 LlamaIndex 两种临时索引后端，通过 `retrieval.backend` 选择。

运行数据固定按入口分区：

```text
data/
  api/<task_id>/
  web/<tenant_id>/<task_id>/

profiles/
  api_clients.json
  tenant_profiles.json
  tenants/<tenant_id>/
    profiles.json
```

API 和 Web 任务都保存 `task.json`、`input/`、`output/` 和启用后的 `logs/`；API 任务还保存 criterion 恢复用的 `state/`。两类数据不能混用。系统固定审查面来自 `resources/criteria/criteria-routes.yaml`；用户自定义审查要点仅作为单次任务输入上传并保存到该任务的 `input/`。

## 管理 CLI

完整子命令、必填/可选参数、容器执行方式和数据边界见 [管理 CLI 手册](docs/MANAGEMENT_CLI.md)。

本地 `.venv` 形式：

```powershell
.\.venv\Scripts\python.exe scripts/manage_api_clients.py --help
.\.venv\Scripts\python.exe scripts/manage_tenants.py --help
.\.venv\Scripts\python.exe scripts/manage_users.py --help
```

VMP 受保护容器形式：

```bash
docker exec -it lexora bash
/app/host -m scripts.manage_api_clients --help
/app/host -m scripts.manage_tenants --help
/app/host -m scripts.manage_users --help
```

## Docker

生产镜像通过 `/app/host` 加载加密代码包，不包含项目 `.py` 或明文项目 `.pyc`。构建机默认从仓库根目录下、被 Git 和主构建上下文忽略的 `vmp-tools/` 读取 VMP 工具；镜像 TAR、远端 Compose 和运行容器均不需要 VMP。构建、部署、挂载和管理命令见 [部署说明](docs/DEPLOYMENT.md)，保护实现与验证边界见 [源码保护方案](docs/SOURCE_PROTECTION.md)。

构建所需的 Python wheel 和 Debian 软件包保存在 `packages/`，Dockerfile 使用这些本地包离线安装依赖。

## 开发验证

现有离线单元测试覆盖路由、检索、日志、审查质量，以及临时合同检索和 Web 历史、认证功能。执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/criteria_formal_recall -p "test_*.py" -q
.\.venv\Scripts\python.exe -m unittest discover -s tests/docxindex_ablation -p "test_*.py" -q
```

真实模型审查和召回评测的运行要求见 [正式召回测试](tests/criteria_formal_recall/README.md) 与 [执行级消融试验](tests/docxindex_ablation/README.md)。

## 文档导航

| 类别 | 文档 |
| --- | --- |
| 操作 | [管理 CLI](docs/MANAGEMENT_CLI.md) · [异步审查 API](docs/ASYNC_REVIEW_API.md) · [Web 多租户](docs/WEB_TENANT_USAGE.md) |
| 部署 | [部署说明](docs/DEPLOYMENT.md) · [Docker 网络排障](docs/DOCKER_NETWORK_TROUBLESHOOTING.md) · [源码保护](docs/SOURCE_PROTECTION.md) |
| 架构 | [智能体与工作流](docs/AGENT_WORKFLOW_ARCHITECTURE.md) · [DOCX 结构化索引与检索](docs/DOCX_STRUCTURE_RETRIEVAL_DESIGN.md) · [DOCX 批注与修订](docs/DOCX_ANNOTATION_DESIGN.md) · [日志设计](docs/LOGGER_DESIGN.md) |
