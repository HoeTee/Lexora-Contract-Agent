# API 审查要点记录排查

本文用于按 `task_id` 排查某一条 API 审查要点。以下以固定要点 `H12` 为例；查询其他要点时替换 ID。先确认任务目录，再依次检查计划、要点状态和执行日志。这个顺序用于定位记录在哪个阶段中断，不代表运行时逐个要点串行执行。

## 1. 确认执行位置和任务目录

从提交接口响应或状态接口取得 `task_id`。命令在**远端宿主机**执行，读取的是服务持久化的任务数据：

```sh
TASK_ID='替换为实际task_id'
TASK_DIR="/data/api/$TASK_ID"
test -f "$TASK_DIR/task.json" && cat "$TASK_DIR/task.json"
```

把 `/data` 替换为宿主机实际挂载的总数据目录。项目提供的 Compose 示例把宿主机 `./data` 挂载到**服务容器**的 `/app/data`；如果命令在服务容器内执行，使用 `TASK_DIR="/app/data/api/$TASK_ID"`。临时管理容器只有挂载了同一数据目录才能读取这些文件。本地开发机的数据目录与远端宿主机互不等同。下面的命令只读取已有文件，不生成或修改任务数据。

`task.json` 是任务状态源；其中的 `status`、`state` 和 `logs` 可以帮助确认任务阶段及保存路径。若找不到该文件，先核对 `task_id`、执行环境和数据挂载；请求尚未进入任务生命周期时不会创建任务目录。

## 2. 确认 H12 是否进入计划

```sh
grep -n '"id": "H12"' "$TASK_DIR/state/plan.json"
```

`state/plan.json` 保存本次计划的完整要点列表。当前固定审查配置要求 `H01` 至 `H18`，其中 `H12` 是“是否有错别字”，路线为 `scan`。如果 `plan.json` 不存在，查看 `task.json` 和 `logs/trace.json` 中 `phase.ingest`、`phase.build_index`、`phase.plan` 的状态；计划前失败不会留下单条要点输入。如果计划存在却没有 H12，核对任务实际运行的镜像、配置和计划文件，不要直接用当前源码推断旧任务的输入。

## 3. 查要点状态文件

```sh
find "$TASK_DIR/state/criteria" -maxdepth 1 -type f -name 'H12.*' -print
cat "$TASK_DIR/state/criteria/H12.input.json"
```

| 文件 | 能回答的问题 |
| --- | --- |
| `H12.input.json` | 执行前保存的审查标准、检查点及当前 `attempt`；不包含检索证据或完整模型提示词。 |
| `H12.output.json` | 已保存的要点结构化结果；可核对 `status`、`issues` 等字段。 |
| `H12.error.json` | 执行抛出异常时的 `latest_error` 和各次 `attempts`；业务判断错误但没有异常时不会出现。 |

要点重跑时，`H12.input.json` 会被当前尝试覆盖；`H12.error.json` 保留异常尝试列表。若已有可复用的 `H12.output.json`，同一任务重跑会直接复用结果，不产生新的 SubAgent 调用。

如果计划中有 H12，但完全没有 `H12.input.json`，继续查第 4 步的 `phase.execute`；不要仅凭缺少对话文件判断模型没有调用。

## 4. 查执行轨迹和最终快照

```sh
grep -n -A 25 -B 5 '"name": "criterion.H12"' "$TASK_DIR/logs/trace.json"
grep -n '"criterion_id": "H12"' "$TASK_DIR/logs/review_outputs.json"
```

在 `trace.json` 中查看 `criterion.H12` 的 `status`、`error`、`metadata`、`inputs` 和子节点 `mcp.contract_search`、`llm.SubAgent_H12`。`inputs` 只是摘要，不是完整提示词。`review_outputs.json` 保存审查结果快照，但工作流在汇总阶段才写它；更早失败时该文件可能不存在。若 H12 只是判断错了而未抛异常，重点核对 `H12.output.json`、`review_outputs.json` 和模型对话。

`logs/` 仅在 `logging.enabled=true` 时写入。关闭日志时仍可从 `task.json` 和已生成的 `state/` 文件定位执行阶段。

## 5. 查 SubAgent 对话和检索边界

```sh
find "$TASK_DIR/logs/conversations" -maxdepth 1 -type f -name 'SubAgent_H12_*.json' -print
```

打开找到的 JSON，检查 `role=user` 的 `content` 及后续 assistant、tool 消息。SubAgent 的 user 提示词包含 H12 标准、检查点和**已检索出的**合同上下文。`grep -R H12 "$TASK_DIR/logs/conversations"` 只搜文件内容，不搜文件名，因此不能代替上述 `find`。

H12 的运行顺序是：`contract_search` 执行 `scan` 检索，随后才创建 `SubAgent_H12` 并提交审查提示词。若检索阶段失败，就不会有该要点的 SubAgent 对话；回到 `trace.json`、`H12.error.json` 和 `logs/events.log` 看失败信息。`scan` 内部的模型请求由检索组件发起，**不写入** `SubAgent_H12_*.json`；现有任务日志不提供每个 `scan` 批次的完整提示词记录。`logs/mcp.log` 可辅助排查 MCP 连接和工具调用异常，但不能把它当作完整 `scan` 输入记录。

## 结果判断

| 看到的情况 | 下一步 |
| --- | --- |
| `task.json` 不存在 | 核对 `task_id`、宿主机挂载目录和请求是否成功创建任务。 |
| `plan.json` 不存在 | 从任务状态和 `trace.json` 查计划前的失败阶段。 |
| `plan.json` 有 H12，但没有 `H12.input.json` | 查 `phase.execute` 是否开始或在写入前失败。 |
| `H12.input.json` 存在，但没有 SubAgent 对话 | 查 `H12.error.json` 与 `mcp.contract_search`；也检查日志开关及结果复用。 |
| `H12.output.json` 存在，结果却不正确 | 对照 SubAgent 对话、`review_outputs.json` 和 H12 的 `scan` 路线，区分检索证据与审查判断。 |

相关实现：[任务路径](../endpoints/review/task_store.py)、[要点状态](../endpoints/review/review_state.py)、[执行顺序](../agents/orchestrator.py)、[对话日志](../loggers/agent_logger.py)。H12 路线定义见 [`criteria-routes.yaml`](../resources/criteria/criteria-routes.yaml)，`scan` 检索设计见[检索算法](../检索算法.md)。
