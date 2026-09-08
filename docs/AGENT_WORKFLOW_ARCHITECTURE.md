# 智能体与工作流架构

本文说明合同审查的智能体（agent）与工作流（workflow）架构。工作流编排在 `workflow/workflow.py`，各 agent 在 `agents/`，MCP 客户端和服务脚本在 `mcp_service/`，检索与文档解析能力通过 MCP 工具提供。

## 总览

`ContractReviewWorkflow` 是单次合同审查的总编排层。它只负责阶段顺序、进度上报、日志和最终结果组装；解析、检索、逐条审查、汇总等细节交给各 agent 和 MCP 工具。

当前是简化工作流：没有 web search，也没有接入制度 RAG；合同检索由 `retrieval.backend` 在 LlamaIndex 和 docxindex 之间选择。

## 工作流 6 个阶段

| 阶段 | 实现 | 说明 |
| --- | --- | --- |
| 1. 解析 ingest | MCP `ingest_file` | 把合同和审查标准 DOCX 转成 markdown |
| 2. 建索引 build_index | MCP `contract_build_index` | 按 `retrieval.backend` 为合同建立临时 LlamaIndex 或 docxindex 索引 |
| 3. 规划 plan | `PlannerAgent.design_tasks` | 把审查标准 markdown 拆成结构化任务 `criteria_list` |
| 4. 执行+反思 execute | `OrchestratorAgent.execute_criteria` | 逐条审查标准，产出 `results` |
| 5. 最终整理 summarize | `build_review_presentation` + `SummarizerAgent.compile_summary_comment` | 程序确定性完成去重、锚点选择和展示分流；Summarizer 只生成总览 |
| 6. 批注 generate | MCP `generate_docx_report` | 用整理后的 `presentation_results` + `final_review` 生成批注版 DOCX |

每个阶段会通过 `progress_callback` 上报阶段名，便于前端展示进度：`ingesting`、`building_index`、`planning`、`reviewing`、`summarizing`、`generating_docx`、`completed`。

## 智能体角色

### 批注意义与证据边界

`agents/prompts/cn_prompts.yaml` 中 SubAgent 负责判定，现有 Reflector 独立复核；不增加 LLM 调用阶段：

- “照抄”等编辑指令不作为最终意见，也不能在目标文字不明确时编造提示语。通用提醒返回 `anchors=[]` 进入总结；具体缺陷才锚定问题原文。
- 已填写的 `【5‰】`、`【5%】` 不是空白占位；主体备选项与数值缺失分开判断。批注必须指出具体字段和缺失内容，引用最小完整的问题语句。
- 分公司签约授权与代理人签署授权分别审查。条件句不证明条件成立，未检索到外部授权书不等于未授权。有对应主体事实且仍需外部材料确认时，以待核实事项和空 anchors 进入总结；不将纯假设写成缺陷。
- Summarizer 必须保留输入中的不确定性，不将“待核实”升级成“授权缺失”或“签署无效”。输出字段和工作流阶段不变。

这些是模型行为约束，不是确定性正确率保证；需要正式审查产物验证，不能以提示词文本测试代替实际准确度评估。

| Agent | 文件 | 职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| PlannerAgent | `agents/planner.py` | 把审查标准拆成结构化审查任务 | 审查标准 markdown | `criteria_list`（id / section / criterion / check_points） |
| OrchestratorAgent | `agents/orchestrator.py` | 逐条派发 SubAgent，串联检索、反思、并发控制 | `criteria_list` | `results` |
| SubAgent | `agents/base_agent.py` 的 `Agent` + `SUB_AGENT_BASE_PROMPT` | 审查单条标准，可调用白名单允许的 MCP 工具 | 标准 + 检索上下文 + 可见工具 | `SubAgentOutput`（status / issues） |
| ReflectorAgent | `agents/reflector.py` | 质量复核 SubAgent 输出 | criterion + subagent_output + 补充检索 | `{ status: PASS/REJECT, feedback }` |
| SummarizerAgent | `agents/summarizer.py` | 根据程序整理后的完整问题清单生成总览，不拥有删除问题或选择锚点的权限 | `summarizer_results` | `{ overall_comment, priority_comments }` |

基础设施：`agents/base_agent.py`（`Agent` 基类与 `Settings` 模型配置）、`agents/schemas.py`（结构化输出 schema）、`agents/prompts/cn_prompts.yaml`（所有提示词，含审查边界）。

## 智能体结构化输出 schema

各 agent 的输出由 `agents/schemas.py` 的 Pydantic 模型严格校验（`extra=forbid`，不接受未定义字段）。类与字段如下：

```python
class PlannerCriterion:
    id: str
    section: str
    criterion: str
    check_points: list[str]

class PlannerOutput:
    criteria: list[PlannerCriterion]

class SubAgentIssue:
    issue_id: str
    risk_level: Literal["high", "medium", "low"]
    anchors: list[SubAgentAnchor]
    reasoning: str
    criterion: str
    check_point: str

class SubAgentAnchor:
    role: Literal["edit", "evidence"]    # 修改位置 / 对照证据
    defect_type: Literal["missing_value", "inconsistency", "wording", "other"]
    xml_anchor_type: Literal["paragraph", "table"]
    xml_anchor_id: str
    quoted_text: str
    comment_text: str                     # edit: 1~300 字；evidence: 空字符串

class SubAgentOutput:
    status: Literal["compliant", "issues_found", "not_applicable"]
    applicability_reason: str = ""
    issues: list[SubAgentIssue] = []

class ReflectorOutput:
    status: Literal["PASS", "REJECT"]
    feedback: str = ""
    removals: list[FindingRemoval] = []   # finding_id + reason；由 Reflector 输出

class SummaryOutput:
    overall_comment: str = ""
    priority_comments: list[str] = []     # overall + priority 合计 <=200 字
```

## 单条标准的执行与反思循环

`OrchestratorAgent.execute_single_criterion` 负责一条审查标准的完整处理：

1. 检索：调用 MCP `contract_search`（query = 标准 + 检查要点），由 `retrieval.backend` 选择 LlamaIndex 或 docxindex，得到合同相关片段 `context`。检索结果会加一段 guardrail 提示，并提供 `xml_anchor_type/xml_anchor_id` 给 SubAgent 复用。
2. SubAgent 审查：以 `SUB_AGENT_BASE_PROMPT` 创建 `SubAgent_<cid>`，只向模型暴露 `workflow.subagent_allowed_tools` 白名单中的 MCP 工具，输出 `SubAgentOutput`，`status` 为 `compliant` / `issues_found` / `not_applicable`，`issues[].anchors` 含 `xml_anchor_type`、`xml_anchor_id`、`quoted_text`、`comment_text` 等字段。
3. 若 `status == compliant`：短路，跳过反思直接返回。
4. 否则进入反思循环，最多 `MAX_REFLECTION_ROUNDS` 轮：
   - 对 `quoted_text` 为空的“缺失类” issue 再检索一次，生成 `missing_text_review_notes` 供 Reflector 判断。
   - `Reflector.review` 返回 `PASS` 或 `REJECT`。
   - `PASS` 且发现完整性检查通过才结束；`REJECT` 在还有反思轮次时按反馈改写。达到上限或反思异常时标记 `quality_status=pending_review`，不直接输出确定性批注。每轮保留发现快照及撤回理由。
5. 返回该 criterion 结果：`criterion_id`、`section`、`issues`、`status`、`applicability_reason`、`tokens`，以及 `quality_status`、`quality_history`、`finding_removals`、`unresolved_issues`。判断结论与质量状态分别记录。

并发与容错（`execute_criteria`）：

- `execute_criteria` 会为 `criteria_list` 中的每条审查标准创建一个 `execute_with_limit(criterion)` coroutine，并用 `asyncio.gather(*execute_tasks, return_exceptions=True)` 一次性调度和等待这些任务。
- `asyncio.gather` 负责并发调度：多条 criterion 可以同时进入等待队列，并在外部模型调用、检索或反思等待期间交错推进。
- `asyncio.Semaphore(MAX_ORCHESTRATOR_CONCURRENCY)` 负责并发限制：同一时间最多允许指定数量的 criterion 进入 `execute_single_criterion()`；其余 coroutine 会等待 semaphore 释放。
- 因此，当前并发边界是“多条 criterion / 多个 SubAgent 审查任务并发”，不是单个 SubAgent 内部把同一条标准拆成多个并发步骤。
- 单条 criterion 最多执行 `workflow.criterion_retry_max_attempts` 次；重试耗尽后的 `ModelCallError` 会导致整次审查失败。
- 其他异常在重试耗尽后降级为该条 `status=ERROR`（记录 `error_message`），不影响其它 criterion。

简化结构如下：

```python
semaphore = asyncio.Semaphore(MAX_ORCHESTRATOR_CONCURRENCY)

async def execute_with_limit(criterion: dict) -> dict:
    async with semaphore:
        return await execute_single_criterion(criterion)

execute_tasks = [
    execute_with_limit(criterion)
    for criterion in criteria_list
]

results = await asyncio.gather(
    *execute_tasks,
    return_exceptions=True,
)
```

这里 `gather` 和 `Semaphore` 不是同一层职责：`gather` 启动并等待多个 coroutine；`Semaphore` 限制同时执行的数量。

## 数据流

```text
审查标准 DOCX -> ingest -> 审查标准 markdown -> Planner -> criteria_list
合同 DOCX     -> DOCX XML anchor nodes -> 按 retrieval.backend 构建临时索引
每条 criterion: 检索 context -> SubAgent(可调用白名单工具) -> (Reflector 反思循环) -> 单条结果
所有单条结果 -> raw results -> build_review_presentation
build_review_presentation -> presentation_results + summarizer_results + summary_issue_comments
summarizer_results -> Summarizer -> final_review
presentation_results + final_review -> generate_docx_report -> 批注版 DOCX
```

`raw results` 继续写入审查日志，保留各 criterion 的原始输出；`presentation_results` 只包含最终写入原文的批注。没有有效锚点、合同整体期限问题，以及条款断号/标题顺序问题由程序放入 `summary_issue_comments`，不依赖 Reviewer 返回的锚点数量。每个原始 `issue_key` 必须进入一个规范化问题，否则程序直接报错，不能静默遗漏。Summarizer 只读取规范化问题，不再返回 discard 或锚点决策。该调整复用原有 Summarizer 调用，不增加 LLM 调用次数。索引与检索见 `DOCX_STRUCTURE_RETRIEVAL_DESIGN.md`，批注写入与 `quoted_text` 定位见 `DOCX_ANNOTATION_DESIGN.md`。

`build_review_presentation()` 的输入是 Orchestrator `results`，输出是 `ReviewPresentation`：`inline_results`、`summarizer_results`、`summary_issue_comments`。相同 XML 范围且修改动作相同的跨要点批注只保留一次；“错别字/语病/重复”等文字质量要点命中同一范围时也合并，并在规范化 issue 的 `source_issue_keys` 中记录全部来源。合同框架缺失、全局断号、履行期限和无锚点问题进入总结；关系型问题中仅作正确值参照的附件标题锚点不写批注。当前 `check_point` 含“写明/写入”的固定引号文字时，只保留一个落点或总结项并原样写入。该步骤不调用模型。

## 架构边界

- 简化工作流：无 web search、无制度 RAG，检索后端由 `retrieval.backend` 选择。
- 审查只基于合同文本和检索片段。完全依赖工商、涉诉、知识产权、法人授权或现行法律知识库的检查点必须返回 `not_applicable`，不能编造外部事实；合同文本本身足以判断的部分仍应审查。
- 正文与附件分别审查；只有审查标准明确要求时才扩展正文与附件内容一致性检查。

## API 队列与进程边界

当 `queue.enabled=true` 时，普通 `/api` 任务由 API 写入 `task.json` 后，通过 `task_queue.review_queue` 投递到 Redis；Celery worker 执行 `review.run_job`。Broker 只保存 `client_dir + task_id` 调度消息，不保存合同、审查结果或恢复状态。

```text
API process -> Redis broker -> Celery worker -> workflow.run()
```

API 进程负责提交、状态和结果响应，worker 负责 workflow、MCP、模型调用和 DOCX 生成。Web 任务目前仍在 Web 应用进程内执行，不经过 Celery。

整份合同并发由 worker 数量和 Celery `--concurrency` 控制；单份合同内 criterion 并发由 `workflow.max_orchestrator_concurrency` 控制。这两个限制不是同一层。

## Criterion 恢复状态

API 任务通过 `endpoints/review/review_state.py` 在任务目录保存可重放状态：

```text
state/
  plan.json
  criteria/
    <criterion_id>.input.json
    <criterion_id>.output.json
    <criterion_id>.error.json
```

成功的 criterion 输出可以在同一任务重跑时复用；失败 criterion 按 `workflow.criterion_retry_max_attempts` 重新执行。Redis broker 不承担业务状态持久化。当前恢复边界不覆盖 ingest、build_index 和 plan 失败。

## 全局模型并发

`model_semaphore.llm`、`embedding`、`reranker` 使用 Redis 租约控制跨进程、跨容器的模型 HTTP 请求总并发。它不会替代 Celery 合同并发或 workflow criterion 并发。

每类配置包含 `enabled`、`max_concurrent_requests`、`redis_url`、`key`、`wait_timeout_seconds`、`lease_seconds` 和 `poll_interval_seconds`。实际读取与租约实现位于 `config.py` 和 `endpoints/runtime/llm_semaphore.py`。

## SubAgent 工具白名单

MCP server 可以暴露多个工具，但 SubAgent 不直接继承完整 MCP 工具列表。`OrchestratorAgent` 创建 SubAgent 前会根据 `config.yaml` 过滤工具：

```yaml
workflow:
  subagent_allowed_tools:
    - "contract_search"
```

字段含义：

- `workflow.subagent_allowed_tools`：SubAgent 可见 MCP 工具白名单。
- 配置缺失时默认只允许 `contract_search`。
- 配置为空列表时，SubAgent 不可调用任何 MCP 工具。
- 配置了 MCP server 未暴露的工具名时，该工具不会传给 SubAgent，程序不会回退为完整工具列表。

`llm.max_tool_calls` 仍是当前 `Agent` 的工具调用次数上限。不要在没有明确不同语义时新增另一个 SubAgent 工具次数配置；否则会形成两个配置控制同一件事。

## 审查输出质量契约更新

见[审查质量与批注规则](REVIEW_QUALITY.md)：新增 edit/evidence 锚点用途、逐字段批注、以附件为基准修改正文、反思待复核状态及标准版本摘要。docxindex 路由和召回算法不变。新代码须随镜像部署，不能只更新提示词。
