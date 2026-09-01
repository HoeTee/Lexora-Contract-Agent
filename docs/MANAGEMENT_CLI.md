# 管理 CLI

本文是项目管理命令的完整入口，覆盖 API client、租户和租户用户管理。三个真实模块如下：

| 模块 | 管理对象 | 主要写入位置 |
| --- | --- | --- |
| `scripts.manage_api_clients` | `/api` client 与平台 API key 指纹映射 | `profiles/api_clients.json` |
| `scripts.manage_tenants` | Web 租户注册表 | `profiles/tenant_profiles.json` |
| `scripts.manage_users` | 指定租户下的 Web 用户 | `profiles/tenants/<tenant_id>/profiles.json` |

生产镜像会将这三个模块编译、加密进 `/app/code.bin`，不会保留 `/app/scripts/*.py`。两种命令只在启动前缀上不同，子命令和参数保持一致：

| 管理对象 | 本地 `.venv` | VMP 受保护容器 |
| --- | --- | --- |
| API client | `.\.venv\Scripts\python.exe scripts/manage_api_clients.py` | `/app/host -m scripts.manage_api_clients` |
| 租户 | `.\.venv\Scripts\python.exe scripts/manage_tenants.py` | `/app/host -m scripts.manage_tenants` |
| 租户用户 | `.\.venv\Scripts\python.exe scripts/manage_users.py` | `/app/host -m scripts.manage_users` |

## 1. 执行方式

### 本地开发机：`.venv`

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/manage_api_clients.py --help
.\.venv\Scripts\python.exe scripts/manage_tenants.py --help
.\.venv\Scripts\python.exe scripts/manage_users.py --help
```

本地命令直接读写项目目录中的 `.env`、`config.yaml`、`profiles/` 和 `data/`。

### VMP 受保护的服务容器

先进入容器：

```bash
docker exec -it lexora bash
```

再执行对应模块：

```bash
/app/host -m scripts.manage_api_clients --help
/app/host -m scripts.manage_tenants --help
/app/host -m scripts.manage_users --help
```

该容器必须已经按 Compose 挂载宿主机的 `.env`、`config.yaml`、`profiles/` 和 `data/`。命令写入 `/app/profiles` 或 `/app/data` 后，实际数据会保存在宿主机对应的挂载目录中，API 和 worker 通过相同挂载读取。

### 一次性临时管理容器

在存在 `docker-compose.yaml`、`.env`、`config.yaml`、`profiles/` 和 `data/` 的部署目录执行：

```bash
docker compose run --rm --no-deps --entrypoint /app/host lexora -m scripts.manage_tenants list
```

`docker compose run` 使用 `lexora` 服务已经定义的挂载，命令结束后删除临时容器。不要直接使用不带这些挂载的 `docker run` 执行写操作，否则数据只会留在临时容器文件系统中。

## 2. API client 管理

| 子命令 | 必填参数 | 可选参数 | 行为 |
| --- | --- | --- | --- |
| `register` | `--client-id`、`--api-key` | 无 | 新增 client 与平台 key 指纹映射。 |
| `reset-api-key` | `--client-id`、`--api-key` | 无 | 替换已有 client 的 key 指纹。 |
| `list` | 无 | `--client-id`、`--compact` | 不传 client 时列出全部 client；传入后查看该 client 的任务。 |
| `check` | `--client-id` | 无 | 查看 client 状态、指纹存在性和任务数量。 |
| `disable` | `--client-id` | 无 | 禁止该 client 继续调用 `/api`。 |
| `enable` | `--client-id` | 无 | 恢复该 client 的 `/api` 调用权限。 |
| `delete` | `--client-id` | 无 | 删除鉴权映射，不删除历史任务。 |

本地 `.venv` 示例：

```powershell
.\.venv\Scripts\python.exe scripts/manage_api_clients.py register --client-id client_a --api-key "<platform-key>"
.\.venv\Scripts\python.exe scripts/manage_api_clients.py list
```

VMP 受保护容器示例：

```bash
/app/host -m scripts.manage_api_clients register --client-id client_a --api-key "<platform-key>"
/app/host -m scripts.manage_api_clients list
/app/host -m scripts.manage_api_clients check --client-id client_a
/app/host -m scripts.manage_api_clients list --client-id client_a --compact
/app/host -m scripts.manage_api_clients reset-api-key --client-id client_a --api-key "<new-platform-key>"
/app/host -m scripts.manage_api_clients disable --client-id client_a
/app/host -m scripts.manage_api_clients enable --client-id client_a
/app/host -m scripts.manage_api_clients delete --client-id client_a
```

`register` 和 `reset-api-key` 只接受 `--api-key`，不接受 `--secret-key`。配置文件只保存 SHA-256 指纹，不保存明文 API key。

## 3. 租户管理

| 子命令 | 必填参数 | 可选参数 | 行为 |
| --- | --- | --- | --- |
| `create` | `--tenant-id`、`--name` | 无 | 创建并启用租户。 |
| `list` | 无 | 无 | 列出全部租户。 |
| `disable` | `--tenant-id` | 无 | 禁止该租户登录和使用 Web 功能。 |
| `enable` | `--tenant-id` | 无 | 重新启用租户。 |
| `delete` | `--tenant-id` | 无 | 删除租户注册记录，不级联删除用户和任务数据。 |

本地 `.venv` 示例：

```powershell
.\.venv\Scripts\python.exe scripts/manage_tenants.py create --tenant-id tenant_a --name "租户 A"
.\.venv\Scripts\python.exe scripts/manage_tenants.py list
```

VMP 受保护容器示例：

```bash
/app/host -m scripts.manage_tenants create --tenant-id tenant_a --name "租户 A"
/app/host -m scripts.manage_tenants list
/app/host -m scripts.manage_tenants disable --tenant-id tenant_a
/app/host -m scripts.manage_tenants enable --tenant-id tenant_a
/app/host -m scripts.manage_tenants delete --tenant-id tenant_a
```

## 4. 租户用户管理

所有用户子命令都必须传入 `--tenant-id`，并且租户必须已经存在。

| 子命令 | 其他必填参数 | 可选参数 | 行为 |
| --- | --- | --- | --- |
| `create` | `--username`、`--password` | `--display-name`、`--role` | 创建用户；`--role` 默认为 `user`，可选 `user` 或 `admin`。 |
| `list` | 无 | 无 | 列出该租户下的用户。 |
| `set-role` | `--username`、`--role` | 无 | 修改用户角色。 |
| `reset-password` | `--username`、`--password` | 无 | 重置密码哈希。 |
| `disable` | `--username` | 无 | 禁止用户登录。 |
| `enable` | `--username` | 无 | 重新启用用户。 |
| `delete` | `--username` | 无 | 删除用户配置，不删除历史任务。 |

本地 `.venv` 示例：

```powershell
.\.venv\Scripts\python.exe scripts/manage_users.py create --tenant-id tenant_a --username admin --password "<password>" --role admin
.\.venv\Scripts\python.exe scripts/manage_users.py list --tenant-id tenant_a
```

VMP 受保护容器示例：

```bash
/app/host -m scripts.manage_users create --tenant-id tenant_a --username admin --password "<password>" --role admin --display-name "租户管理员"
/app/host -m scripts.manage_users create --tenant-id tenant_a --username alice --password "<password>"
/app/host -m scripts.manage_users list --tenant-id tenant_a
/app/host -m scripts.manage_users set-role --tenant-id tenant_a --username alice --role admin
/app/host -m scripts.manage_users reset-password --tenant-id tenant_a --username alice --password "<new-password>"
/app/host -m scripts.manage_users disable --tenant-id tenant_a --username alice
/app/host -m scripts.manage_users enable --tenant-id tenant_a --username alice
/app/host -m scripts.manage_users delete --tenant-id tenant_a --username alice
```

创建或重新启用用户时，程序会初始化 `data/web/<tenant_id>/<username>/`。删除用户只移除 `profiles.json` 中的账号记录，不删除该目录中的历史任务。

## 5. 数据与安全边界

```text
宿主机 ./profiles  -> 容器 /app/profiles
宿主机 ./data      -> 容器 /app/data
```

- 删除 API client 只删除 `profiles/api_clients.json` 中的映射，保留 `data/api/` 中的任务。
- 删除租户只删除 `profiles/tenant_profiles.json` 中的注册记录，保留租户用户、审查标准和任务。
- 删除用户只删除租户 `profiles.json` 中的账号记录，保留 `data/web/<tenant_id>/` 中的任务。
- API key 和密码作为命令行参数时可能进入 shell history，不要在共享终端中输入真实凭据。
- 实际参数始终以对应模块的 `--help` 输出为准。
