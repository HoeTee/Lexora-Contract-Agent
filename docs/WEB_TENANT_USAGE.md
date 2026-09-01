# Web 多租户使用说明

本文说明当前 `/web` 的租户模型、本地数据布局、CLI 操作和前端行为。

## 直接结论

`/web` 按租户隔离。租户和租户用户通过 CLI 管理。前端登录页需要填写 `tenant_id`、用户名和密码。运行期审查数据保存在：

```text
data/web/<tenant_id>/<task_id>/
```

## 身份文件

租户注册表：

```text
profiles/
  tenant_profiles.json
```

租户用户和用户默认审查要点：

```text
profiles/
  tenants/
    <tenant_id>/
      profiles.json
      criteria/
        <username>.docx
```

字段含义：

- `tenant_profiles.json` 保存租户记录，包括 `tenant_id`、展示名称 `name`、启用状态和时间戳。
- `tenants/<tenant_id>/profiles.json` 只保存该租户下的用户。
- `tenants/<tenant_id>/criteria/<username>.docx` 保存该用户默认审查要点。

系统不保存明文密码。用户密码通过 `endpoints/runtime/auth.py` 存储为哈希。

## 运行数据

Web 审查任务数据结构：

```text
data/
  web/
    <tenant_id>/
      <task_id>/
        task.json
        input/
        output/
        logs/
```

任务目录含义：

- `task.json`：任务元数据，包括 `tenant_id`、`tenant_name`、`username`、状态、文件名和时间戳。
- `input/`：本次任务上传的合同，以及可选上传的本次审查要点。
- `output/`：审查后的 DOCX 结果。
- `logs/`：workflow、conversation、MCP 和 API event 日志。

`data/web/` 下没有 `/users` 或 `/tasks` 中间层。

## 租户 CLI

以下示例用于拥有源码的本地开发机。受保护 Docker 镜像中，将 `python scripts/manage_tenants.py` 替换为 `/app/host -m scripts.manage_tenants`，将 `python scripts/manage_users.py` 替换为 `/app/host -m scripts.manage_users`；完整说明见 `docs/MANAGEMENT_CLI.md`。

创建租户：

```bash
python scripts/manage_tenants.py create --tenant-id tenant_a --name "Tenant A"
```

列出租户：

```bash
python scripts/manage_tenants.py list
```

禁用或启用租户：

```bash
python scripts/manage_tenants.py disable --tenant-id tenant_a
python scripts/manage_tenants.py enable --tenant-id tenant_a
```

删除租户注册记录：

```bash
python scripts/manage_tenants.py delete --tenant-id tenant_a
```

边界：删除租户注册记录不会删除租户用户和任务数据，只会移除注册表中的租户记录。

## 用户 CLI

所有用户命令都必须指定 `--tenant-id`。

创建租户管理员：

```bash
python scripts/manage_users.py create --tenant-id tenant_a --username admin --password "ChangeMe123" --role admin --display-name "Tenant Admin"
```

创建普通用户：

```bash
python scripts/manage_users.py create --tenant-id tenant_a --username alice --password "ChangeMe123" --role user --display-name "Alice"
```

列出某个租户下的用户：

```bash
python scripts/manage_users.py list --tenant-id tenant_a
```

修改角色：

```bash
python scripts/manage_users.py set-role --tenant-id tenant_a --username alice --role admin
```

重置密码：

```bash
python scripts/manage_users.py reset-password --tenant-id tenant_a --username alice --password "NewPassword123"
```

禁用或启用用户：

```bash
python scripts/manage_users.py disable --tenant-id tenant_a --username alice
python scripts/manage_users.py enable --tenant-id tenant_a --username alice
```

删除用户配置：

```bash
python scripts/manage_users.py delete --tenant-id tenant_a --username alice
```

边界：删除用户配置不会删除 `data/web/<tenant_id>/` 下已有任务数据。

## 前端登录

登录页：

```text
/web/login
```

登录输入：

- `tenant_id`
- 用户名
- 密码

登录后：

- `role=user` 进入 `/web/work`。
- `role=admin` 进入 `/web/admin`。
- 右上角用户区域显示租户名称和当前用户。

租户信息保存在服务端 session 的认证上下文中。用户只能看到自己租户、自己用户名下的任务记录。

## 租户管理端

租户管理员使用 `/web/admin`。

当前管理端能力：

- 列出当前租户用户。
- 创建当前租户用户。
- 启用、禁用、删除、重置密码、修改当前租户用户角色。
- 查看当前租户用户审查历史。
- 下载、上传或恢复某个用户的默认审查要点。
- 查看当前租户任务的 API event 日志。

当前实现没有超级管理员前端。跨租户控制由 CLI 处理。

## 审查流程

普通用户流程：

1. 使用 `tenant_id`、用户名和密码登录。
2. 打开 `/web/work`。
3. 上传合同 DOCX。
4. 可选上传本次审查要点 DOCX。
5. 系统创建 `data/web/<tenant_id>/<task_id>/`。
6. `task.json` 初始为 `pending`，可能进入 `queued`，然后进入 `running`，最终为 `succeeded` 或 `failed`。
7. 结果 DOCX 写入 `output/`。
8. `/web/history` 从 `data/web/<tenant_id>/*/task.json` 读取任务记录。

`task.json` 是任务状态的唯一文件。提交路由不能把它替换成 history-only JSON；审查成功后，只能把历史展示字段合并到已有任务记录中。

当前 Web 后台任务运行在应用进程内，没有外部任务队列。服务启动时，后端会扫描 `data/api/` 和 `data/web/` 中遗留的 `pending`、`queued`、`running` 任务，并把它们收敛为 `failed / WORKER_INTERRUPTED`。这些任务只能作为历史状态和排障线索，不能继续显示为正在执行，也不能阻止用户重新提交。

经验原则：不要把持久化 `task.json` 中的 `running` 直接等同于真实运行中的 worker。只要 worker 生命周期没有持久化队列托管，服务启动时就必须做一次状态收敛，否则强制退出后留下的 `running` 会误导前端、API 调用方和排障人员。

如果本次任务没有上传审查要点，工作流使用：

```text
profiles/tenants/<tenant_id>/criteria/<username>.docx
```

## 部署注意事项

Docker 部署时要区分这些位置：

- 开发机器：本地源码、本地 `data/`、本地 `profiles/`。
- 远程宿主机：运行 Docker 的机器。
- 服务容器：运行应用的容器。
- 临时管理容器或宿主机 shell：可能执行 CLI 命令的位置。

持久化数据必须挂载进服务容器。服务需要长期访问：

```text
profiles/
data/
```

推荐 compose 挂载：

```yaml
volumes:
  - ./profiles:/app/profiles
  - ./data:/app/data
```

输入文件来自浏览器上传。服务会把任务输入、输出、日志和任务元数据写入挂载后的 `data/web/<tenant_id>/<task_id>/` 目录。租户和用户 CLI 会把身份配置写入挂载后的 `profiles/` 目录。

不要只把租户配置或任务数据写入正在运行容器的临时文件系统。容器重建后，这些数据会丢失。

## 快速开始

本地开发机拥有源码时执行：

```bash
python scripts/manage_tenants.py create --tenant-id tenant_a --name "Tenant A"
python scripts/manage_users.py create --tenant-id tenant_a --username admin --password "ChangeMe123" --role admin
python scripts/manage_users.py create --tenant-id tenant_a --username alice --password "ChangeMe123" --role user
```

受保护镜像已经运行时，可进入服务容器执行同一 CLI：

```bash
docker exec -it lexora bash
/app/host -m scripts.manage_tenants create --tenant-id tenant_a --name "Tenant A"
/app/host -m scripts.manage_users create --tenant-id tenant_a --username admin --password "ChangeMe123" --role admin
/app/host -m scripts.manage_users create --tenant-id tenant_a --username alice --password "ChangeMe123" --role user
```

然后打开：

```text
http://<host>/web/login
```

用以下信息登录：

```text
tenant_id: tenant_a
username: alice
password: ChangeMe123
```
