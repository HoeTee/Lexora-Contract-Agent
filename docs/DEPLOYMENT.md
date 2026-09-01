# 部署说明

## 环境边界

- **镜像构建机**：持有项目源码、Docker/BuildKit、32字节 AES 密钥和 VMP console/SDK 工具目录，负责生成生产镜像。
- **远端部署宿主机**：只需要生产镜像、Compose 配置、`.env`、`config.yaml` 和持久化目录；不要求存在源码、Python、VMP 或项目脚本。
- **运行服务容器**：只包含运行依赖、`/app/host`、`/app/libloader.vmp.so`、`/app/code.bin` 和明确列出的可读资源。
- **临时管理容器**：执行用户或 API client 管理命令时，挂载与服务容器相同的 `profiles/`、`data/` 和配置文件。

## 构建生产镜像

正式构建是三阶段流程：

1. `protect-builder`（`python:3.12-slim`）：编译项目字节码并生成 AES-256-GCM 密文 `code.bin`，同时编译未加壳 `libloader.so` 和明文小型 host。
2. `vmp-pack`（`ubuntu:24.04`）：使用 glibc 2.39 运行 `vmprotect_con`，把 `libloader.so` 中标记的密钥重建和 AES 解密函数虚拟化为 `libloader.vmp.so`。
3. `runtime`（`python:3.12-slim`）：只复制 `host + libloader.vmp.so + code.bin`，不复制未加壳 loader、VMP 工具、构建密钥或项目源码。

VMP 工具目录由构建机提供，不提交到 Git。目录至少包含：

```text
<vmp-tools>/
  vmprotect_con
  libVMProtectSDK64.so
  libjitterentropy.so.3
  VMProtectLicense.ini
  sdk/
    VMProtectSDK.h
```

在项目根目录执行：

```powershell
.\protect\build.ps1 `
  -Tag southernbanker/lexora:latest `
  -VmpToolsDir C:\secure\vmp-tools
```

也可以通过环境变量提供工具目录：

```powershell
$env:VMP_TOOLS_DIR = "C:\secure\vmp-tools"
.\protect\build.ps1 -Tag southernbanker/lexora:latest
```

脚本默认生成一次性32字节 AES 密钥，通过 BuildKit secret `source_key` 传入 `protect-builder`，完成后删除临时密钥。如需可重现构建，可以显式提供项目目录之外的原始32字节密钥：

```powershell
.\protect\build.ps1 `
  -Tag southernbanker/lexora:latest `
  -VmpToolsDir C:\secure\vmp-tools `
  -KeyFile C:\secure\lexora-aes.key
```

`vmprotect_con` 需要 glibc 2.38或更高版本，不能在 `python:3.12-slim` 的 Debian bookworm 层直接运行。正式 Dockerfile 因此固定使用 `ubuntu:24.04` 执行加壳；运行阶段仍使用 `python:3.12-slim`。

## 构建后验证

Dockerfile 会检查加壳日志中同时出现 `lexora_key_reconstruct`、`lexora_aes_decrypt` 和 `Compilation completed`；任何一项缺失都会中止构建。

继续检查最终镜像：

```powershell
$image = "southernbanker/lexora:latest"

docker run --rm --entrypoint sh $image -c "test -x /app/host && test -s /app/libloader.vmp.so && test -s /app/code.bin"
docker run --rm --entrypoint sh $image -c "test ! -e /app/loader && test ! -e /app/libloader.so && test ! -e /app/libVMProtectSDK64.so && test ! -e /app/vmprotect_con"
docker run --rm --entrypoint find $image /app -type f -name "*.py"
docker run --rm --entrypoint find $image /app -type f -name "*.pyc"
docker run --rm --entrypoint sh $image -c "od -An -tx1 -N8 /app/code.bin"

foreach ($term in @("reconstruct_key", "aes_decrypt", "LEXORA1", "zipfile")) {
  docker run --rm --entrypoint grep $image -a -q $term /app/libloader.vmp.so
  if ($LASTEXITCODE -eq 0) { throw "Sensitive string remains in packed library: $term" }
}

$dependencies = docker run --rm --entrypoint ldd $image /app/libloader.vmp.so
if ($dependencies -match "VMProtectSDK") { throw "Packed library still depends on VMProtect SDK." }
```

预期结果：

- `/app/host`、`/app/libloader.vmp.so`、`/app/code.bin` 存在。
- `/app/loader`、未加壳 `libloader.so`、VMP SDK和console不存在。
- 两条 `find` 命令无输出，即 `/app` 下不存在项目 `.py` 或明文 `.pyc`；第三方依赖位于 `/usr/local/lib/python3.12/site-packages/`，仍可包含依赖自身的公开源码。
- `code.bin` 前8字节为 `4c 45 58 4f 52 41 31 00`（`LEXORA1\0`）。
- 加壳 `.so` 中无法直接检索到密钥重建、AES解密或 importer 的关键明文字符串。

## 启动服务

远端部署宿主机使用已经构建并推送的镜像：

```powershell
docker compose pull
docker compose up -d --force-recreate
```

API、Celery worker 和本地 MCP 子进程都通过 host 的 `-m` 接口进入受保护 Python 运行时：

```text
/app/host -m uvicorn app:app ...
/app/host -m celery -A task_queue.celery_app:celery_app worker ...
```

host 根据自身路径 `dlopen` 同目录的 `libloader.vmp.so`；共享库重建 AES 密钥、解密 `/app/code.bin` 并通过内存 importer 加载模块，不会把项目 `.pyc` 写回容器文件系统。加壳后的共享库运行时不依赖 `libVMProtectSDK64.so`。

## 受保护镜像中的管理 CLI

API client、租户和用户管理脚本已经编译、加密进 `code.bin`。最终镜像没有 `/app/scripts/*.py`，因此不能在容器内执行 `python scripts/manage_*.py`。

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

输入来自命令行参数；身份配置写入 `/app/profiles`，任务目录读取或初始化于 `/app/data`；运行中的 API 和 worker 通过相同宿主机挂载读取结果。完整子命令、删除边界和本地开发命令见 `docs/MANAGEMENT_CLI.md`。

## 服务器上必须准备的文件和目录

```text
.env
config.yaml
agents/prompts/cn_prompts.yaml
profiles/
data/
```

Compose 将宿主机输入挂载为：

```yaml
volumes:
  - ./.env:/app/.env:ro
  - ./config.yaml:/app/config.yaml:ro
  - ./agents/prompts/cn_prompts.yaml:/app/agents/prompts/cn_prompts.yaml:ro
  - ./profiles:/app/profiles
  - ./data:/app/data
```

输入文件来自远端部署宿主机；`.env`、`config.yaml` 和 Prompt 以只读方式供 API 与 worker 启动时读取，`profiles/` 和 `data/` 写入宿主机持久化目录，不写入镜像层。修改 Prompt 后，需要重启 API 服务和所有 worker。

`profiles/` 可以为空。创建租户和用户后，程序会生成 `profiles/tenant_profiles.json`、`profiles/tenants/<tenant_id>/profiles.json` 和用户审查标准文件。该目录必须可写，因为管理 CLI、管理员后台和用户资料修改都会更新身份数据。

如需自定义默认审查要点，可挂载单个文件：

```yaml
- ./criteria.docx:/app/resources/criteria/criteria.docx:ro
```

不要用空目录覆盖 `/app/resources/criteria/`。

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

如果 `queue.enabled=true`，必须同时运行 Redis、API 和至少一个 Celery worker：

```text
redis
api
review-worker
```

Redis 是 broker，API 负责请求和状态读取，worker 负责 workflow、MCP、LLM调用、DOCX生成和日志写入。容器配置中的 Redis 地址应使用 Compose service 名称，而不是 `localhost`：

```yaml
queue:
  enabled: true
  broker_url: "redis://redis:6379/0"
```

Celery task、broker 和 worker 的详细边界见 `docs/QUEUE_WORKER.md`。

## 安全和授权边界

- VMP提高的是离线静态分析成本，不保证运行时明文永不可获得。
- 能运行容器并使用 `docker exec`、`ptrace`、`LD_PRELOAD` 或进程内存转储的攻击者，仍可提取内存中的解密归档。
- 生产环境应配合非root运行、限制容器管理权限、避免开放 `docker exec`、启用宿主机审计，并在兼容业务写入需求的前提下收紧容器文件系统权限。
- 当前VMP工具来源存在授权风险，仅限已获合法授权的内部研究或保护场景；分发和商用前必须完成法律审核。
