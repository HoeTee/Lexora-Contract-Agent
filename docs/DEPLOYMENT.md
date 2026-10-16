# 部署说明

## 环境边界

- **镜像构建机**：持有项目源码、Docker/BuildKit 和仓库根目录下的本地 `vmp-tools/`；VMP 只临时挂载到 Docker 构建阶段。
- **远端部署宿主机**：只需要生产镜像、Compose 配置、`.env`、`config.yaml` 和持久化目录；不要求存在源码、Python、VMP 或项目脚本。
- **运行服务容器**：只包含运行依赖、`/app/host`、`/app/libloader.vmp.so`、`/app/code.bin` 和明确列出的可读资源。
- **临时管理容器**：执行用户或 API client 管理命令时，挂载与服务容器相同的 `profiles/`、`data/` 和配置文件。

## 构建生产镜像

构建机需要 Docker/BuildKit，以及仓库根目录下的本地 `vmp-tools/`。该目录已被 Git 和主 Docker 构建上下文忽略，不会随源码提交，也不会被复制进镜像。目录结构必须是：

```text
<仓库根目录>\vmp-tools\
├─ vmprotect_con
├─ libVMProtectSDK64.so
├─ libjitterentropy.so.3
├─ VMProtectLicense.ini
└─ sdk\
   └─ VMProtectSDK.h
```

在项目根目录执行：

```powershell
.\protect\build.ps1
```

`VmpToolsDir` 的优先级为：命令行 `-VmpToolsDir`、环境变量 `VMP_TOOLS_DIR`、仓库根目录 `vmp-tools/`。需要临时覆盖默认目录时使用：

```powershell
.\protect\build.ps1 -VmpToolsDir C:\path\to\vmp-tools
```

脚本默认生成并清理一次性 32 字节 AES 密钥；如需固定密钥，使用项目目录之外的 `-KeyFile`。VMP 目录作为独立 BuildKit named context 传入，Dockerfile 只在 `RUN` 期间挂载它，不把工具、SDK、动态库或许可证复制进镜像层。缺少工具不影响运行已有镜像，但构建受保护镜像会在启动 Docker 前明确失败。三阶段构建、工具文件要求和密钥边界见 [源码保护方案](SOURCE_PROTECTION.md)。

构建脚本固定产出与 Compose 一致的 `lexora:latest`，并在构建后使用断网临时容器加载 `cl100k_base`；离线 tokenizer 数据不可用时脚本直接失败，不会输出成功提示。

## 构建后验证

构建完成后做最小运行产物检查：

```powershell
$image = "lexora:latest"

docker run --rm --entrypoint sh $image -c "test -x /app/host && test -s /app/libloader.vmp.so && test -s /app/code.bin"
docker run --rm --entrypoint find $image /app -type f -name "*.py"
docker run --rm --entrypoint find $image /app -type f -name "*.pyc"
```

第一条命令必须成功，两条 `find` 命令必须无输出。完整静态证据检查见 [源码保护方案](SOURCE_PROTECTION.md)。

## 启动服务

远端部署宿主机使用已经构建并推送的镜像：

```powershell
docker compose pull
docker compose up -d --force-recreate
```

如果通过 TAR 交付，在构建机导出：

```powershell
docker save -o lexora.tar lexora:latest
```

远端导入后直接启动：

```bash
docker load -i lexora.tar
docker compose up -d --force-recreate
```

两份 Compose 都只运行镜像，不执行 `docker build`，因此远端宿主机和运行容器都不需要挂载 VMP。

API、Celery worker 和本地 MCP 子进程都通过 host 的 `-m` 接口进入受保护 Python 运行时：

```text
/app/host -m uvicorn app:app ...
/app/host -m celery -A task_queue.celery_app:celery_app worker ...
```

host 根据自身路径 `dlopen` 同目录的 `libloader.vmp.so`；共享库重建 AES 密钥、解密 `/app/code.bin` 并通过内存 importer 加载模块，不会把项目 `.pyc` 写回容器文件系统。加壳后的共享库运行时不依赖 `libVMProtectSDK64.so`。

## 受保护镜像中的管理 CLI

API client、租户和用户管理脚本已经编译、加密进 `code.bin`。最终镜像没有 `/app/scripts/*.py`，因此不能在容器内执行 `python scripts/manage_*.py`。

本节只说明部署环境中的启动方式；完整子命令、参数和删除边界见 [管理 CLI 手册](MANAGEMENT_CLI.md)。

| 环境 | 命令形式 | 适用位置 |
| --- | --- | --- |
| 本地 `.venv` | `.\.venv\Scripts\python.exe scripts/manage_tenants.py list` | 有源码和虚拟环境的开发机 |
| VMP 受保护容器 | `/app/host -m scripts.manage_tenants list` | 已构建镜像的服务容器 |

远端镜像部署不要求存在源码、`.venv` 或 Python 命令，因此下面使用第二种形式。

进入正在运行且已经挂载 `profiles/`、`data/` 的服务容器：

```bash
docker exec -it lexora bash
```

再由 `/app/host` 启动嵌入式 Python 并执行加密模块：

```bash
/app/host -m scripts.manage_api_clients list
/app/host -m scripts.manage_tenants list
/app/host -m scripts.manage_users list --tenant-id tenant_a
```

输入来自命令行参数；身份配置写入 `/app/profiles`，任务目录读取或初始化于 `/app/data`；运行中的 API 和 worker 通过相同宿主机挂载读取结果。完整参数直接查看模块帮助：

```bash
/app/host -m scripts.manage_api_clients --help
/app/host -m scripts.manage_tenants --help
/app/host -m scripts.manage_users --help
```

## 服务器上必须准备的文件和目录

```text
.env
config.yaml
agents/prompts.yaml
criteria-routes.yaml
profiles/
data/
```

Compose 将宿主机输入挂载为：

```yaml
volumes:
  - ./.env:/app/.env:ro
  - ./config.yaml:/app/config.yaml:ro
  - ./agents/prompts.yaml:/app/agents/prompts.yaml:ro
  - ./criteria-routes.yaml:/app/resources/criteria/criteria-routes.yaml:ro
  - ./profiles:/app/profiles
  - ./data:/app/data
```

输入文件来自远端部署宿主机；`.env`、`config.yaml` 和 `agents/prompts.yaml` 以只读方式供 API 与三个 worker 启动时读取，`profiles/` 和 `data/` 写入宿主机持久化目录，不写入镜像层。上面的 `./agents/prompts.yaml` 是宿主机相对 Compose 文件的路径，容器始终从 `/app/agents/prompts.yaml` 读取。部署包应保留 `agents/` 这一层目录；API 和三个 worker 必须挂载同一份文件。修改提示词后，需要重启 API 服务和全部三个 worker。

旧版提示词目录已移除，加载器也随代码迁移到了新模块。首次升级必须在构建机重建镜像、同步新的 `agents/prompts.yaml`，再修改远端 Compose 的四处提示词挂载并重建 API 与三个 worker 容器。只更换挂载路径不能让旧镜像使用新加载器。以后只改 YAML 中的提示词正文时，可以保留镜像，但仍须同步文件并重启这四个服务。

`profiles/` 可以为空。创建租户和用户后，程序会生成 `profiles/tenant_profiles.json` 和 `profiles/tenants/<tenant_id>/profiles.json`；不会自动生成用户审查要点文件。该目录必须可写，因为管理 CLI、管理员后台和用户资料修改都会更新身份数据。

固定18条标准及路线全部由一个只读 YAML 提供：

```yaml
- ./criteria-routes.yaml:/app/resources/criteria/criteria-routes.yaml:ro
```

输入来自远端宿主机当前目录，容器只读该配置。用户自定义审查要点不通过 Compose 挂载；用户通过 API 或 Web 上传后，文件落入任务 `input/` 并通过 `./data:/app/data` 持久化，由 Planner 生成追加任务。

## 端口与健康检查

容器内部服务监听 `0.0.0.0:8000`。例如：

```yaml
ports:
  - "0.0.0.0:5000:8000"
```

外部访问 `http://服务器IP:5000`。如果只允许宿主机本地访问，改为：

```yaml
ports:
  - "127.0.0.1:5000:8000"
```

健康检查地址：

```text
宿主机：http://127.0.0.1:5000/health
容器内：http://127.0.0.1:8000/health
```

## Queue worker

Web 和 API 固定通过队列提交审查；OA 入口已移除。部署必须同时运行 Redis、Web/API 服务和三个 Celery worker，每个 worker 使用 `prefork`、`--concurrency=1`：

```text
redis
api
review-worker-1
review-worker-2
review-worker-3
```

Redis DB 0 保存 Celery 消息，DB 2 保存模型请求名额。Web/API 负责请求和状态读取，worker 负责 workflow、MCP、模型调用、DOCX 生成和日志写入。API 与三个 worker 必须挂载相同的 `data/`、`profiles/` 和配置文件。容器配置中的 Redis 地址应使用 Compose service 名称，而不是 `localhost`：

```yaml
workflow:
  model_wait_timeout_seconds: 180
  model_call_timeout_seconds: 60

queue:
  broker_url: "redis://redis:6379/0"
  worker_disable_prefetch: true
  pending_timeout_seconds: 60
  queued_timeout_seconds: 7200
  watchdog_interval_seconds: 10
  task_time_limit_seconds: 1800
```

`queued_timeout_seconds: 7200` 只限制尚未被 worker 接手的任务；`task_time_limit_seconds: 1800` 从 Celery 开始执行时计算，超时终止执行子进程并补充新的子进程，不自动重跑原合同。不配置软时限。Windows 的 `solo` 池不能提供这里的硬时限保证，部署使用 Linux 容器或 Linux/WSL 测试环境。

`model_wait_timeout_seconds: 180` 限制同一次逻辑调用累计等待模型名额的时间，排队不消耗 `model_call_timeout_seconds: 60` 的执行预算。重试继续使用两份剩余预算，整任务时限始终有效。名额 lease 从取得名额时开始，不包含排队；本次保留原 lease 和三个 worker 的并发设置。

所有 worker 和其 MCP 子进程必须使用相同的 DB 2 地址、模型 key、容量及租约设置。不要通过更换 Redis 数据库、key 或关闭限流绕过已有请求上限。Redis 不是合同与报告的存储位置，持久化结果仍写入共享挂载的 `data/`。

变更并发策略或升级信号量脚本时，先停止新提交并等待旧任务结束，再统一更新 API 与全部 worker；保留现有任务、文件和 Redis 数据，不执行清库。Celery task、broker、worker 与 workflow 内部并发的边界见 [智能体与工作流架构](AGENT_WORKFLOW_ARCHITECTURE.md)，各配置含义见 [并发与超时配置说明](CONCURRENCY_TIMEOUT_CONFIGURATION.md)。

真实链路日志验证脚本在开发机运行，向已部署 API 上传 DOCX；服务通过正常 DB 0 队列和 DB 2 限流执行，不修改服务配置。开发机必须能读取部署共享的 `data/api/`，并在环境变量 `API_KEY` 中提供已注册的 API key。示例中的 `--task-root` 是开发机看见的目录，不是容器内 `/app/data/api`：

```powershell
.\.venv\Scripts\python.exe scripts/verify_real_contract_logging.py --base-url http://127.0.0.1:5000 --contract deploy/test_contract.docx --task-root .\data\api
```

执行该命令会提交真实模型任务；仅查看参数使用 `--help`。完整调用文件和路由事件核验要求服务启动前设置 `logging.enabled: true`、`logging.detailed: true`；脚本不自动修改日志设置。

## 安全和授权边界

- VMP 提高的是离线静态分析成本，不保证运行时明文永不可获得。
- 能运行容器并使用 `docker exec`、`ptrace`、`LD_PRELOAD` 或进程内存转储的攻击者，仍可提取内存中的解密归档。
- 生产环境应配合非 root 运行、限制容器管理权限、避免开放 `docker exec`、启用宿主机审计，并在兼容业务写入需求的前提下收紧容器文件系统权限。
- 当前 VMP 工具来源存在授权风险，仅限已获合法授权的内部研究或保护场景；分发和商用前必须完成法律审核。

## 审查输出质量契约更新

见[审查质量与批注规则](REVIEW_QUALITY.md)：`criteria-routes.yaml` 必须随镜像或挂载目录提供，不写入 `config.yaml`；用户 DOCX 只能通过 API/Web 上传。本地正式回归使用项目根目录 `docker-compose.yaml`，不能用部署包 compose 替代。
