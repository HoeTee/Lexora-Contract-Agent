# 任务日志文件名阅读指南

每次 API 或 Web 审查都有独立任务目录。以下的 `TASK_DIR` 指本次任务目录，例如 API 任务的 `data/api/<task_id>`。先用 `task_id` 找到任务，再在其 `logs/` 下查看文件；不同任务的同名要点互不混用。

## 两种文件名

| 目录 | 新任务的格式 | 示例 |
| --- | --- | --- |
| `logs/calls/` | `<来源>__<调用阶段>__<UTC时间>__<call_id>.json` | `H12__docxindex-scan__20260923T034500123Z__a524...json` |
| `logs/route_events/` | `<要点>__<事件>__<UTC时间>__<event_id>.json` | `H12__plan__20260923T034500123Z__b715...json` |

四段以双下划线 `__` 分隔，扩展名均为 `.json`。末段是完整的 32 位随机 UUID；示例中的 `...` 仅为排版省略，实际文件名不会省略。UUID 用于避免并发写入重名，**不是合同或日志内容的 hash**。文件名中的时间使用 UTC：`20260923T034500123Z` 表示 2026 年 9 月 23 日 03:45:00.123 UTC；JSON 内的 `started_at`、`at` 按运行环境显示时区，两者可相差八小时。

### 来源和调用阶段

- `H12`、`H15`：该调用或路线属于对应审查要点。`H12__docxindex-scan` 是 DocxIndex 在 H12 的 `scan` 路线内调用模型；`H12__agent-model` 是后续 SubAgent 的模型调用。两者是不同阶段。
- `Index`：建立合同索引时的调用，没有单个要点编号，例如 `Index__embedding`、`Index__llm`。
- `Summarizer`、`Planner`、`Reflector`：任务级 Agent 调用，例如 `Summarizer__model`。这些记录的 `criterion_id` 为 `null`；没有 MCP 检索操作时 `operation_id` 也为 `null`，这不表示日志缺失。
- `Task`：没有要点或更具体角色的任务级记录。实际来源以 JSON 内的 `source`、`component`、`stage` 为准。

`calls` 文件在发出外部请求前创建，初始 `status=pending`，完成后改为 `succeeded` 或 `failed`。完整请求、响应或异常在文件内。`call_id` 与文件名末段相同；在 `trace.json` 或 `state/criteria/H12.error.json` 中找到 `call_path` 时，应优先直接打开该路径，不必猜文件名。

### 路线事件

`route_events` 的第二段是事件名，例如 `plan`（决定路线）、`step`（步骤完成）、`fallback`（回退判断）、`result`（检索结果摘要）。一个要点可产生多个事件；重试时同一种事件也可出现多次。文件内的 `operation_id` 把这些事件与一次 `mcp.contract_search` 关联起来。路线事件只记录规划和摘要；实际发给模型的完整输入在对应 `calls` 文件中。

## 查询示例

Windows PowerShell（本地开发机）：

```powershell
$taskDir = 'C:\Users\lenovo\desktop\contract_agent\data\api\<task_id>'
Get-ChildItem "$taskDir\logs\calls\H12__*.json" | Sort-Object Name
Get-ChildItem "$taskDir\logs\route_events\H12__*.json" | Sort-Object Name
Get-Content -LiteralPath '<实际文件路径>' -Raw -Encoding UTF8 | ConvertFrom-Json
```

PowerShell 中的 `cat` 是 `Get-Content` 的别名。对于无 BOM 的 UTF-8 JSON，应明确使用 `-Encoding UTF8`；如果省略，某些 PowerShell 版本会按系统编码读取，出现“浣犳槸”等乱码。这是读取显示问题，不代表文件内容损坏。

Linux 宿主机或容器中，先把 `TASK_DIR` 设为**该执行位置**实际看到的挂载目录；例如宿主机可能是 `/data/api/<task_id>`，示例服务容器内是 `/app/data/api/<task_id>`：

```sh
find "$TASK_DIR/logs/calls" -maxdepth 1 -type f -name 'H12__*.json' | sort
find "$TASK_DIR/logs/route_events" -maxdepth 1 -type f -name 'H12__*.json' | sort
```

命令只读取已有文件，不要求远端宿主机安装项目源码或 Python。完整调用记录可能包含合同原文，应按业务数据权限查看。

## 旧任务

改动前的 `calls`、`route_events` 文件名只有 UUID，例如 `ec284f5bead84093829a9551bbf6ff23.json`。旧文件不会批量改名，因为已有 trace 和错误状态中的 `call_path` 指向原路径。查旧任务时按 `call_id`、`criterion_id` 或 `operation_id` 搜索 JSON 内容；新旧文件均可按 UTF-8 读取。要点排查顺序见 [API 审查要点记录排查](API_CRITERION_LOG_TROUBLESHOOTING.md)。
