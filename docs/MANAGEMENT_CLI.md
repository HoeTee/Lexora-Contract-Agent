# 管理 CLI

项目提供三个管理 CLI：

- `scripts/manage_api_clients.py`：管理 `/api` client 与平台 API key 指纹映射。
- `scripts/manage_tenants.py`：管理 Web 租户注册表。
- `scripts/manage_users.py`：管理指定租户下的 Web 用户。

这三个脚本提交到 Git，并在生产镜像构建时编译、加密进 `/app/code.bin`。最终运行镜像不会保留 `/app/scripts/*.py` 或明文 `.pyc`。

## 两种运行环境

本地开发机拥有源码，在项目根目录使用 Python 直接运行：

```powershell
python scripts/manage_api_clients.py list
python scripts/manage_tenants.py list
python scripts/manage_users.py list --tenant-id tenant_a
```

受保护的 Docker 镜像没有明文脚本。先进入正在运行的服务容器：

```bash
docker exec -it lexora bash
```

再通过 `/app/host` 从加密代码包执行同一模块：

```bash
/app/host -m scripts.manage_api_clients list
/app/host -m scripts.manage_tenants list
/app/host -m scripts.manage_users list --tenant-id tenant_a
```

`/app/host -m scripts.manage_api_clients` 对应本地的 `python scripts/manage_api_clients.py`。脚本的子命令和参数保持不变，只有加密镜像中的启动器不同。普通 `python` 无法读取 `/app/code.bin`。

## API client

```bash
/app/host -m scripts.manage_api_clients register --client-id client_a --api-key "<platform-key>"
/app/host -m scripts.manage_api_clients list
/app/host -m scripts.manage_api_clients check --client-id client_a
/app/host -m scripts.manage_api_clients list --client-id client_a
/app/host -m scripts.manage_api_clients list --client-id client_a --compact
/app/host -m scripts.manage_api_clients reset-api-key --client-id client_a --api-key "<new-platform-key>"
/app/host -m scripts.manage_api_clients disable --client-id client_a
/app/host -m scripts.manage_api_clients enable --client-id client_a
/app/host -m scripts.manage_api_clients delete --client-id client_a
```

`register` 和 `reset-api-key` 接收 `--api-key`，不接受 `--secret-key`。`delete` 只删除 `profiles/api_clients.json` 中的鉴权映射，保留 `data/api/` 下已有任务。

## 租户

```bash
/app/host -m scripts.manage_tenants create --tenant-id tenant_a --name "Tenant A"
/app/host -m scripts.manage_tenants list
/app/host -m scripts.manage_tenants disable --tenant-id tenant_a
/app/host -m scripts.manage_tenants enable --tenant-id tenant_a
/app/host -m scripts.manage_tenants delete --tenant-id tenant_a
```

`delete` 只删除租户注册记录，保留该租户的用户配置、审查标准和任务数据。

## 租户用户

所有用户命令都必须提供 `--tenant-id`：

```bash
/app/host -m scripts.manage_users create --tenant-id tenant_a --username admin --password "<password>" --role admin --display-name "Tenant Admin"
/app/host -m scripts.manage_users create --tenant-id tenant_a --username alice --password "<password>" --role user --display-name "Alice"
/app/host -m scripts.manage_users list --tenant-id tenant_a
/app/host -m scripts.manage_users set-role --tenant-id tenant_a --username alice --role admin
/app/host -m scripts.manage_users reset-password --tenant-id tenant_a --username alice --password "<new-password>"
/app/host -m scripts.manage_users disable --tenant-id tenant_a --username alice
/app/host -m scripts.manage_users enable --tenant-id tenant_a --username alice
/app/host -m scripts.manage_users delete --tenant-id tenant_a --username alice
```

`delete` 只删除 `profiles/tenants/<tenant_id>/profiles.json` 中的用户配置，保留 `data/web/<tenant_id>/` 下已有任务数据。当前 CLI 不支持 `--keep-data`，因为租户用户删除默认就不会清理任务数据。

## 数据边界

Docker Compose 将宿主机目录共享给 API、worker 和管理命令：

```text
远端宿主机 ./profiles  -> 容器 /app/profiles
远端宿主机 ./data      -> 容器 /app/data
```

管理命令写入：

```text
/app/profiles/api_clients.json
/app/profiles/tenant_profiles.json
/app/profiles/tenants/<tenant_id>/profiles.json
/app/profiles/tenants/<tenant_id>/criteria/<username>.docx
/app/data/api/
/app/data/web/<tenant_id>/
```

服务通过相同挂载目录读取这些数据。不要在没有 `profiles/`、`data/` 挂载的临时容器中执行写操作，否则数据只存在于该容器的临时文件系统。

API key 和密码作为命令行参数时可能进入 shell history。生产操作应限制 Docker 权限、控制容器 shell 访问，并避免在共享终端中输入真实凭据。
