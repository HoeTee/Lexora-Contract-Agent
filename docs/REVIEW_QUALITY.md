# 审查判断、质量验收与批注契约

## 范围与原则

固定18条审查标准及其路线、检查点和批注去向全部由 `resources/criteria/criteria-routes.yaml` 提供；它们跳过 Planner 和路由 LLM，但仍经过 Orchestrator、检索、SubAgent 与 Reflector。用户通过 API/Web 单独上传的 DOCX 才由 Planner 在现有 `title/rule/scan/region/hybrid` 五种路线中规划。此次不修改索引算法、进程并发或全局模型限流。

审查依据是任务实际使用的标准原文。字段缺失不等于已填写值不一致；关键词命中不授权扩大禁用词范围。建议必须放回完整句子检查，不擅自改变起算条件、权利义务。名称中的书名号、编号、空格等引用格式差异不构成附件名称不一致。

正文与附件比较以附件为客观内容基准：确认同一事项实质冲突后建议修改正文。附件未重复正文内容不直接证明冲突，也不据此删除正文约定。附件含糊、附件互相矛盾或合同明确约定正文优先时，说明待核实的冲突，不强行选择版本。

## 输出字段

| 字段 | 用途与约束 |
| --- | --- |
| `anchors[].role` | `edit` 为实际修改位置，`evidence` 为对照证据。旧结果缺省为 edit；新提示词明确要求提供。 |
| `anchors[].defect_type` | `missing_value / inconsistency / wording / other`；区分同一字段缺失和其他修改，也参与发现追踪。 |
| `xml_anchor_type / xml_anchor_id / quoted_text` | 保持原有 XML 定位契约，原文必须连续、逐字匹配。 |
| `anchors[].comment_text` | edit 必须有具体修改意见，最多 300 字符；evidence 必须为空。超长通过校验重试，禁止静默截断。 |
| `issue_comment` | 完整问题概述，最多 600 字符；不能以概括替代真实字段对应的修改位置。 |
| `Reflector.finding_decisions[]` | 每项为 `finding_id`、`decision(confirm/revise/remove)` 和非空 `reason`；每条 finding 必须独立决定。 |
| `quality_status` | `passed`：当前输出通过反思及发现完整性检查；`pending_review`：未通过、模型异常或存在未解释丢失；`not_required`：初次无问题，沿用已有短路流程，不能解释为已反思。 |
| `quality_history` | 逐轮结果快照和反思反馈，供日志诊断；不交给 Summarizer。 |
| `finding_decisions` | 逐条确认、修改或撤回决定；语义理由仍由 LLM 判断。 |
| `unresolved_issues` | 尚未确认的问题，仅供任务状态和日志诊断，不进入 Summarizer 或 DOCX。 |

内部发现指纹由原文位置、引用及缺陷类型生成，不按评论措辞生成；无锚点发现使用检查点与问题文本。模型使用同一审查项内稳定的 F001 等短编号，不自行生成 ID。current_findings 仅是位置索引，不是完整证据；完整结果由 subagent_output 提供。引用粒度改变也会触发撤回核对，不能把这一保守检查等同于语义完备性证明。

路线 schema 统一定义在根目录 `routing/schemas.py`，Planner 与 docxindex 执行器共同校验同一契约，避免两份 schema 漂移。非法的 region + scan 等组合仍会触发现有 JSON 校验重试。

## 执行与写入

固定任务和用户任务完成反思后，工作流会用原始 DOCX 解析每个 edit anchor 的 `paragraph_path/start_index/end_index`。只有两类来源落在完全相同字符范围时才冲突，固定任务保留、用户任务删除；同一段落内不同字符范围、无法解析的锚点以及同来源结果均保留。该步骤发生在 Summarizer 和 DOCX Writer 之前。

1. 字段完整性不使用业务正则判定。标准明确字段名时由 `rule` 定位，标准要求全合同检查且未限定字段时由 `scan` 分批交给 LLM 结合字段语义、相邻文字和表格关系判断。
2. 已有反思调用接收检索证据、当前发现和先前未保留的发现；不新增复核 Agent。
3. Reflector 对每条 finding 返回 confirm/revise/remove。一个 finding 需修改不会连带屏蔽同一 criterion 中已确认的问题。
4. 对“信息是否齐全”“包括……等”这类非穷举标准，scan 必须遍历批次内全部原文且不设候选 Top-K；Reviewer 必须逐项判断 scan 返回的字段候选，不能把示例字段当作封闭清单。
4. pending_review 只写入 API 状态和日志；不得进入 AI 审查总结，不得暴露 criterion/finding 编号或 XML path。
5. edit 位置逐处写入；evidence 永不产生批注。删除 merged_by_party 聚合器。旧结果含该 placement 时也按普通位置处理，不恢复聚合行为。
6. Schema 要求每个 missing_value issue 只有一个 edit、每个 issue_id 唯一，并拒绝完全重复的 XML 位置、引用和缺陷类型；同段不同字段不因为同属 missing_value 就合并。不使用业务正则或模糊相似度阈值判断字段是否未填写。
7. 保留输入 DOCX 已有人工及普通批注，替换作者为“AI 审查总结”的旧总结，确保最终只有一条总结。Summarizer 输入上限保持 90k。

## 任务与状态

`task.json` 与任务查询响应增加：

- `criteria_manifest`：实际标准文件名、SHA-256、拆分数量及每条标准原文。
- `quality`：`status`、`pending_criteria`、`error_criteria`。

执行状态 `succeeded` 表示审查文件生成成功，不代表所有判断通过质量复核。质量未通过时 message 明确提示“部分审查项待复核”；消费者同时读取 quality。未通过内容不作为确认的法律结论。

state/plan.json 保存标准来源摘要；本次逐 finding 决策使用 `review_contract_version=4`，旧结果不自动复用。日志按原有 API/Web 用户分区保存，不新增平行日志系统。

## 验证

运行已有 `tests/criteria_formal_recall` 回归测试；相关质量测试已合并到现有测试脚本，不新增独立运行脚本。局部测试只能证明代码契约，不能证明模型语义准确率。

端到端使用项目根目录 `docker-compose.yaml` 启动正式 API/Worker，并上传原始输入合同。核对全部标准内真实占位字段、正确批注保留、旧 AI 总结替换、原文定位、索引与总耗时；不以单份结果推断全项目准确率。

代码改动需要重新构建服务镜像。只同步挂载提示词不能部署 schema、反思及写入逻辑；必须验证运行镜像版本，不能让新字段提示词长期搭配旧代码。
