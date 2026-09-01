# 快速启动

本文用于验证服务是否能端到端跑通。

## 1. 安装依赖

```powershell
pip install -r requirements.txt
```

## 2. 创建测试租户和用户

```powershell
python scripts/manage_tenants.py create --tenant-id demo --name "Demo Tenant"
python scripts/manage_users.py create --tenant-id demo --username testuser --password Test123456 --display-name TestUser
```

创建用户时会自动初始化用户目录，并从 `resources/criteria/criteria.docx` 复制默认审查要点。

如果用户已经存在，可以重置密码：

```powershell
python scripts/manage_users.py reset-password --tenant-id demo --username testuser --password Test123456
```

## 3. 确认审查要点

默认审查要点应已经存在于：

```text
profiles/tenants/demo/criteria/testuser.docx
```

如果要临时使用其他审查要点，可以在 `/work` 页面上传合同的同时上传本次审查要点；如果要修改该用户默认审查要点，可以用管理员后台进入用户详情页上传覆盖。

## 4. 创建管理员用户

```powershell
python scripts/manage_users.py create --tenant-id demo --username admin --password Admin123456 --display-name 管理员 --role admin
```

如果管理员已经存在，可以重置密码：

```powershell
python scripts/manage_users.py reset-password --tenant-id demo --username admin --password Admin123456
```

## 5. 启动 Web 服务

如果 `config.yaml` 中 `queue.enabled=true`，先启动 Redis。Redis 是独立服务，不会随 `uvicorn` 自动启动。

```powershell
redis-server
```

```powershell
uvicorn app:app --host 0.0.0.0 --port 5000
```

另开一个终端启动审查 worker：

```powershell
celery -A task_queue.celery_app:celery_app worker --loglevel=info --pool=solo
```

Windows 本地建议使用 `--pool=solo`。如果 `queue.enabled=false`，可以不启动 Redis 和 Celery worker，API 会回退到进程内后台任务。

本机浏览器访问：

```text
http://127.0.0.1:5000
```

登录信息：

```text
tenant_id: demo
username: testuser
password: Test123456
```

管理员后台：

```text
http://127.0.0.1:5000/admin
```

## 6. 上传合同

在页面中上传真实 `.docx` 合同文件。

审查成功后，服务会写入：

```text
data/web/demo/<task_id>/input/
data/web/demo/<task_id>/output/
data/web/demo/<task_id>/logs/
data/web/demo/<task_id>/task.json
```

页面会显示批注版 DOCX 的下载链接。

前端完整用户旅程、页面状态和多标签页行为见 `docs/FRONTEND_USER_JOURNEY.md`。

## 7. 使用 curl 测试

先保存登录 cookie：

```powershell
$loginPage = Invoke-WebRequest -Uri http://127.0.0.1:5000/login -SessionVariable webSession
$loginToken = [regex]::Match($loginPage.Content, 'name="login_token" type="hidden" value="([^"]+)"').Groups[1].Value
```

登录：

```powershell
$loginResponse = Invoke-WebRequest `
  -Uri http://127.0.0.1:5000/login `
  -Method Post `
  -WebSession $webSession `
  -Body @{
    username = "testuser"
    password = "Test123456"
    login_token = $loginToken
  }
$ctx = [regex]::Match($loginResponse.Headers.Location, 'ctx=([^&]+)').Groups[1].Value
```

上传合同并触发审查：

```powershell
$form = @{
  file = Get-Item "C:\path\合同文件名.docx"
}
Invoke-WebRequest `
  -Uri "http://127.0.0.1:5000/review?ctx=$ctx" `
  -Method Post `
  -WebSession $webSession `
  -Form $form
```

`$webSession` 是 PowerShell 用来模拟浏览器保存登录态的临时对象，正常 Web 运行时浏览器会自动保存和发送 cookie。

## 8. Docker 冒烟测试

确认服务器上存在：

```text
.env
config.yaml
agents/prompts/cn_prompts.yaml
profiles/
data/
```

`profiles/` 和 `data/` 都可以是空目录。创建租户和用户后，程序会生成 `profiles/tenant_profiles.json`、`profiles/tenants/<tenant_id>/profiles.json` 和租户数据目录。

构建并启动：

```powershell
.\protect\build.ps1 `
  -Tag deep-research-agent:latest `
  -VmpToolsDir C:\secure\vmp-tools
docker compose up -d
```

`VmpToolsDir` 是构建机上的 VMP console/SDK 目录，不会进入运行镜像。正式镜像以 `/app/host` 启动，host 通过 `dlopen` 加载 `/app/libloader.vmp.so`，后者在内存中解密 `/app/code.bin`。镜像不包含项目 `.py`、明文项目 `.pyc`、未加壳 `libloader.so`、VMP console 或 `docs/`。

构建后先验证产物和保护边界：

```powershell
docker run --rm --entrypoint sh deep-research-agent:latest -c "test -x /app/host && test -s /app/libloader.vmp.so && test -s /app/code.bin"
docker run --rm --entrypoint sh deep-research-agent:latest -c "test ! -e /app/loader && test ! -e /app/libloader.so && test ! -e /app/libVMProtectSDK64.so"
docker run --rm --entrypoint find deep-research-agent:latest /app -type f -name "*.py"
docker run --rm --entrypoint find deep-research-agent:latest /app -type f -name "*.pyc"
docker run --rm --entrypoint sh deep-research-agent:latest -c "od -An -tx1 -N8 /app/code.bin"

foreach ($term in @("reconstruct_key", "aes_decrypt", "LEXORA1", "zipfile")) {
  docker run --rm --entrypoint grep deep-research-agent:latest -a -q $term /app/libloader.vmp.so
  if ($LASTEXITCODE -eq 0) { throw "Sensitive string remains in packed library: $term" }
}
```

产物检查应成功退出，两条 `find` 命令应无输出，关键字循环不应抛出异常；`code.bin` 文件头应为 `4c 45 58 4f 52 41 31 00`。`agents/prompts/cn_prompts.yaml` 以只读方式挂载；修改 Prompt 后需要重启 API 服务和所有 Celery worker。

验证受保护镜像中的管理 CLI：

```powershell
docker exec -it lexora /app/host -m scripts.manage_tenants list
docker exec -it lexora /app/host -m scripts.manage_users list --tenant-id demo
docker exec -it lexora /app/host -m scripts.manage_api_clients list
```

这些命令从 `/app/code.bin` 加载管理模块，并读写 Compose 共享的 `/app/profiles`、`/app/data`。完整命令见 `docs/MANAGEMENT_CLI.md`。

当前 compose 映射：

```text
http://<server-ip>:5000
```

如果只允许服务器本机访问，修改 `docker-compose.yaml`：

```yaml
ports:
  - "127.0.0.1:5000:8000"
```

然后访问：

```text
http://127.0.0.1:5000
```

## 9. 预期输出

上传合同：

```text
data/web/demo/<task_id>/input/<original_filename>.docx
```

批注结果：

```text
data/web/demo/<task_id>/output/<contract_name>_reviewed.docx
```

任务日志：

```text
data/web/demo/<task_id>/logs/
```

历史状态来源：

```text
data/web/demo/<task_id>/task.json
```
