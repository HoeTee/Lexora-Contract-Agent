# 合同审查 Agent

审查输出契约见[审查质量与批注规则](docs/REVIEW_QUALITY.md)。根目录 `routing/` 管理固定审查路线和用户标准动态路线；`tools/docxindex/` 只负责索引与检索执行。Reflector 逐条确认 finding，最终 DOCX 只写已确认问题并替换旧 AI 审查总结。

合同审查 Agent 支持三种 DOCX 审查入口：浏览器 `/web`、异步 `/api` 和本地 CLI。系统读取合同与审查标准，执行 agent 工作流，并输出带批注的 DOCX。

| 使用场景 | 入口 | 结果 |
| --- | --- | --- |
| 浏览器审查 | `/web` | 页面历史记录与批注 DOCX |
| 系统集成 | `/api` | 异步任务状态与 DOCX 文件流或 URL |
| 单次本地处理 | CLI | 指定位置的批注 DOCX |

## 本地启动

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

启动 Web 服务：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 5000
```

浏览器访问 `http://127.0.0.1:5000/web/login`，使用 `demo / testuser / Test123456` 登录。

如果 `config.yaml` 中 `queue.enabled=true`，异步 API 还需要 Redis 和 Celery worker：

```powershell
redis-server
.\.venv\Scripts\python.exe -m celery -A task_queue.celery_app:celery_app worker --loglevel=info --pool=solo
```

## 合同 CLI

### 本地 `.venv`

```powershell
.\.venv\Scripts\python.exe main.py --contract .\contract.docx
.\.venv\Scripts\python.exe main.py --contract .\contract.docx --criteria .\criteria.docx --output .\contract_reviewed.docx
```

### VMP 受保护容器

先把输入文件放入宿主机挂载的 `data/`，再进入正在运行的容器：

```bash
docker exec -it lexora bash
/app/host -m main --contract /app/data/contract.docx --output /app/data/contract_reviewed.docx
```

输入来自宿主机 `./data/contract.docx`，输出最终写到宿主机 `./data/contract_reviewed.docx`。

## 异步 API

主要接口：

- `POST /api/review/jobs`
- `POST /api/review/jobs/status`
- `POST /api/review/jobs/result`
- `POST /api/review/jobs/{task_id}/cancel`

请求、响应、状态和错误契约见 [异步审查 API](docs/ASYNC_REVIEW_API.md)。

## 配置与数据

模型密钥放在 `.env`；模型、workflow、API、检索、队列和日志配置放在 `config.yaml`。配置加载入口是 `config.py`。

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
    criteria/<username>.docx
```

API 和 Web 任务都保存 `task.json`、`input/`、`output/` 和启用后的 `logs/`；API 任务还保存 criterion 恢复用的 `state/`。两类数据不能混用。

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

## 文档导航

| 类别 | 文档 |
| --- | --- |
| 操作 | [管理 CLI](docs/MANAGEMENT_CLI.md) · [异步审查 API](docs/ASYNC_REVIEW_API.md) · [Web 多租户](docs/WEB_TENANT_USAGE.md) |
| 部署 | [部署说明](docs/DEPLOYMENT.md) · [Docker 网络排障](docs/DOCKER_NETWORK_TROUBLESHOOTING.md) · [源码保护](docs/SOURCE_PROTECTION.md) |
| 架构 | [智能体与工作流](docs/AGENT_WORKFLOW_ARCHITECTURE.md) · [DOCX 结构化索引与检索](docs/DOCX_STRUCTURE_RETRIEVAL_DESIGN.md) · [DOCX 批注与修订](docs/DOCX_ANNOTATION_DESIGN.md) · [日志设计](docs/LOGGER_DESIGN.md) |
