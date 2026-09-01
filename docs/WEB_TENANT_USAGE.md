# Web 多租户使用说明

`/web` 按租户隔离。登录必须提供 `tenant_id`、用户名和密码；身份配置写入 `profiles/`，审查任务写入 `data/web/`。

## 身份与数据

```text
profiles/
  tenant_profiles.json
  tenants/<tenant_id>/
    profiles.json
    criteria/<username>.docx

data/web/<tenant_id>/<task_id>/
  task.json
  input/
  output/
  logs/
```

- `tenant_profiles.json`：租户名称、启用状态和时间戳。
- `profiles.json`：该租户的用户、角色、启用状态和密码哈希；不保存明文密码。
- `criteria/<username>.docx`：用户默认审查标准。
- `task.json`：任务状态、提交用户、文件名和时间戳，是 Web 任务状态源。

用户只能读取同一 `tenant_id` 下属于自己用户名的任务。当前没有超级管理员前端；跨租户管理只能通过 CLI 完成。

## 管理 CLI

本地源码环境：

```powershell
python scripts/manage_tenants.py --help
python scripts/manage_users.py --help
```

受保护服务容器：

```bash
docker exec -it lexora bash
/app/host -m scripts.manage_tenants --help
/app/host -m scripts.manage_users --help
```

命令参数以脚本 `--help` 为准。所有用户命令都要求 `--tenant-id`。

删除边界：

- 删除租户只移除 `tenant_profiles.json` 中的注册记录，不级联删除租户用户、审查标准或任务。
- 删除用户只移除对应 `profiles.json` 中的用户记录，不删除 `data/web/<tenant_id>/` 下的历史任务。

## 前端流程

```text
GET /web/login
  -> 输入 tenant_id、用户名、密码
  -> user 跳转 /web/work
  -> admin 跳转 /web/admin

POST /web/review
  -> 校验合同与可选审查标准
  -> 创建 data/web/<tenant_id>/<task_id>/task.json
  -> 在应用进程内执行 Web 审查任务
  -> 结果写入 output/

GET /web/history
  -> 枚举当前租户 task.json
  -> 按当前 username 过滤
```

普通用户可以上传合同、选择本次临时审查标准、查看历史和下载结果。租户管理员可以管理本租户用户、角色、密码、默认审查标准和用户审查历史。

## 页面状态边界

- `task.json` 保存持久化任务状态，不能被单独的 history JSON 覆盖。
- 一次性页面提示保存在 session 中；重新进入登录入口或成功登录时应清理旧提示和旧认证上下文。
- 历史 `failed` 任务可以展示在历史页，但不能阻止新任务提交，也不能在新登录后继续显示为当前错误。
- Web 任务当前运行在应用进程内。服务启动时会把遗留的 `pending`、`queued`、`running` 收敛为 `failed / WORKER_INTERRUPTED`，不能把磁盘中的旧 `running` 当成仍有真实 worker 执行。
- 同一用户确有活动任务时，工作台显示其状态并阻止重复提交。

## 审查标准

本次上传审查标准时，文件只属于该任务并保存在 `input/`。未上传时使用：

```text
profiles/tenants/<tenant_id>/criteria/<username>.docx
```

管理员修改默认审查标准时写回该文件，不影响已创建任务的输入快照。

## Docker 持久化

输入文件来自浏览器。服务、管理命令和后续重启都必须读取相同宿主机数据：

```yaml
volumes:
  - ./profiles:/app/profiles
  - ./data:/app/data
```

不要把身份或任务数据只写进容器临时文件系统。完整镜像启动和管理命令见 [部署说明](DEPLOYMENT.md)。
