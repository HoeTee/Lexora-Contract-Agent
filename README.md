# 合同审查 Agent

合同审查 Agent 支持三种 DOCX 审查入口：浏览器 `/web`、异步 `/api` 和本地 CLI。系统读取合同与审查标准，执行 agent 工作流，并输出带批注的 DOCX。

## 本地启动

安装依赖并创建配置：

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
```

创建测试租户和用户：

```powershell
python scripts/manage_tenants.py create --tenant-id demo --name "Demo Tenant"
python scripts/manage_users.py create --tenant-id demo --username testuser --password Test123456
```

启动 Web 服务：

```powershell
uvicorn app:app --host 0.0.0.0 --port 5000
```

浏览器访问 `http://127.0.0.1:5000/web/login`，使用 `demo / testuser / Test123456` 登录。

如果 `config.yaml` 中 `queue.enabled=true`，异步 API 还需要 Redis 和 Celery worker：

```powershell
redis-server
celery -A task_queue.celery_app:celery_app worker --loglevel=info --pool=solo
```

## 本地合同 CLI

```powershell
python main.py --contract .\contract.docx
python main.py --contract .\contract.docx --criteria .\criteria.docx --output .\contract_reviewed.docx
```

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

拥有源码时直接运行脚本；完整子命令以脚本 `--help` 为准：

```powershell
python scripts/manage_api_clients.py --help
python scripts/manage_tenants.py --help
python scripts/manage_users.py --help
```

受保护容器没有明文脚本。进入已挂载 `profiles/`、`data/` 的服务容器后执行加密模块：

```bash
docker exec -it lexora bash
/app/host -m scripts.manage_api_clients --help
/app/host -m scripts.manage_tenants --help
/app/host -m scripts.manage_users --help
```

## Docker

生产镜像通过 `/app/host` 加载加密代码包，不包含项目 `.py` 或明文项目 `.pyc`。构建、部署、挂载和管理命令见 [部署说明](docs/DEPLOYMENT.md)，保护实现与验证边界见 [源码保护方案](docs/SOURCE_PROTECTION.md)。

## 文档

- [异步审查 API](docs/ASYNC_REVIEW_API.md)
- [Web 多租户使用说明](docs/WEB_TENANT_USAGE.md)
- [部署说明](docs/DEPLOYMENT.md)
- [Docker 网络排障](docs/DOCKER_NETWORK_TROUBLESHOOTING.md)
- [智能体与工作流架构](docs/AGENT_WORKFLOW_ARCHITECTURE.md)
- [DOCX 结构化索引与检索](docs/DOCX_STRUCTURE_RETRIEVAL_DESIGN.md)
- [DOCX 批注与修订处理](docs/DOCX_ANNOTATION_DESIGN.md)
- [日志设计](docs/LOGGER_DESIGN.md)
- [源码保护方案](docs/SOURCE_PROTECTION.md)
