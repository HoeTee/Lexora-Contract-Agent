# Agent 提示词管理

所有 agent 的静态提示词统一保存在 `agents/prompts.yaml`。`agents/prompts.py` 负责读取与渲染；Planner、SubAgent、Reflector、Summarizer 和公共 JSON 校验流程只传入本次任务的数据。旧的 `agents/prompts/` 目录已经移除。

| 条目 | 用途 |
|---|---|
| `PLANNER_STRATEGY_PROMPT`、`PLANNER_STRATEGY_TASK_PROMPT` | 用户上传审查标准的路线规划 |
| `SUB_AGENT_BASE_PROMPT`、`ORCHESTRATOR_REVIEW_TASK` | 按当前标准和检索证据生成审查结果 |
| `ORCHESTRATOR_COVERAGE_AUDIT`、`ORCHESTRATOR_COMPARISON_AUDIT` | 证据覆盖和正文与附件比较的字段、行为要求 |
| `REFLECTOR_SYSTEM_PROMPT`、`REFLECTOR_REVIEW_TASK_PROMPT` | 独立复核结论和漏检情况 |
| `SUMMARIZER_SYSTEM_PROMPT`、`SUMMARIZER_TASK_PROMPT` | 文首总结与重复问题归并 |
| 四个 `*_EXPECTED_JSON` 条目 | 各类输出的完整 JSON 示例 |
| `JSON_INITIAL_REQUEST_PROMPT`、`JSON_REPAIR_PROMPT` | 首次发送格式约束、校验失败后的修复要求 |
| `ORCHESTRATOR_*` 其余条目、`AGENT_TOOL_LIMIT_PROMPT` | 证据说明、批注位置、修订反馈与工具额度耗尽指令 |

`PLANNER_SYSTEM_PROMPT` 保留旧调用兼容；当前 Planner 使用 `PLANNER_STRATEGY_PROMPT`。DocxIndex 检索与索引工具自己的提示词不属于这些 agent，仍由对应工具模块管理。

模板中的动态位置使用 `${name}`。Python 将审查标准、证据、上一轮结果等作为值传入；替换只进行一次。JSON 的花括号不需要转义，插入数据中的 `${...}` 和美元符号保持原样。不要随意更改占位符名称；缺少参数或未知提示词名称会明确报错。

首次调用就发送完整输出示例，而不是等校验失败后再发送。示例中的锚点和结论不能当成合同事实；未要求的 `coverage_decisions`、`comparison_decisions` 返回空数组。

覆盖记录每条必须包含 `xml_anchor_type`、`xml_anchor_id`、`decision`、`reason`，不得以 `anchor_id` 替代前两个字段。关系比较中的证据类型与证据 ID 必须成对有效或成对为空；无证据时使用 JSON `null`，不能使用字符串 `"null"`。`matched`、`mismatch` 必须引用有效证据；`not_found` 没有证据，`not_applicable` 不得同时生成该引用的问题批注。

运行时的严格校验仍由 `agents/schemas.py` 执行。修改字段、枚举或空值规则时，需要同时维护 YAML 示例和 schema。测试会把四份格式示例交给实际 schema 校验，并检查首次请求是否携带格式。

修复提示按字段归纳重复错误。例如 H01 的 29 条覆盖记录如果都用错字段，87 条校验错误会合并为三类字段错误。修复反馈不再反复附带每个输入对象和校验文档链接；完整错误仍保留在调用日志中。该变更不修改模型操作的总预算、发送上限或整要点尝试次数。

提示词在进程启动时加载。只改 YAML 正文后，也需要重启 API 和全部三个 worker，确保四个服务使用同一份内容。首次迁移涉及 Python 加载模块和路径，必须重建镜像；之后只改已有模板正文可保留镜像。服务器上的文件、挂载路径和更新步骤见 `docs/DEPLOYMENT.md`。

离线验证：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests/criteria_formal_recall -p 'test_*.py'
.venv\Scripts\python.exe -m ruff check agents
```

这些测试验证配置加载、模板渲染、首次格式传递与已有工作流契约，不能证明模型每次都遵守指令。实测时还应检查调用日志中的实际请求、响应与要点最终状态。此次未修改 H18 的检索范围或附件目录提取逻辑，提示词修复不能替代检索覆盖修复。
