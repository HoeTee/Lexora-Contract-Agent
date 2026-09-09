# 智能体与工作流架构

本文说明合同审查的智能体（agent）与工作流（workflow）架构。工作流编排在 `workflow/workflow.py`，业务路由控制面在根目录 `routing/`，各 agent 在 `agents/`，索引和检索执行在 `tools/docxindex/`，能力通过 MCP 工具提供。

## 总览

`ContractReviewWorkflow` 是单次合同审查的总编排层。它只负责阶段顺序、进度上报、日志和最终结果组装；解析、检索、逐条审查、汇总等细节交给各 agent 和 MCP 工具。

当前是简化工作流：没有 web search，也没有接入制度 RAG；合同检索由 `retrieval.backend` 在 LlamaIndex 和 docxindex 之间选择。

## 工作流 6 个阶段

| 阶段 | 实现 | 说明 |
| --- | --- | --- |
| 1. 解析 ingest | MCP `ingest_file` | 把合同和审查标准 DOCX 转成 markdown |
| 2. 建索引 build_index | MCP `contract_build_index` | 按 `retrieval.backend` 为合同建立临时 LlamaIndex 或 docxindex 索引 |
| 3. 规划 plan | `routing.profile` + `PlannerAgent.extract_strategies` | 固定18条直接从 `criteria-routes.yaml` 组装；用户上传标准才调用 Planner |
| 4. 执行+反思 execute | `OrchestratorAgent.execute_criteria` | 逐条审查标准，产出 `results` |
| 5. 最终整理 summarize | Workflow 分流 + `SummarizerAgent.compile_summary_comment` | Summarizer 在同一次调用中生成总览并标记跨要点语义重复；Workflow 校验并应用合并结果 |
| 6. 批注 generate | MCP `generate_docx_report` | 用已确认的原文批注结果与 `final_review` 生成批注版 DOCX |

每个阶段会通过 `progress_callback` 上报阶段名，便于前端展示进度：`ingesting`、`building_index`、`planning`、`reviewing`、`summarizing`、`generating_docx`、`completed`。

## 智能体角色

### 批注意义与证据边界

`agents/prompts/cn_prompts.yaml` 中 SubAgent 负责判定，现有 Reflector 独立复核；不增加新的 Agent 类型：

- “照抄”等编辑指令不作为最终意见，也不能在目标文字不明确时编造提示语。通用提醒返回 `anchors=[]` 进入总结；具体缺陷才锚定问题原文。
- 字段是否未填写由 LLM 结合字段名称、相邻文字、表格关系和合同语境判断，不由业务正则硬匹配。`[合同中心-合同编码]`、`#乙方合同号#【     】`、字段后的 `XX` 是提示词示例；`【5‰】`、`【5%】` 等已有值和条件性补件说明是反例。批注必须直接引用对应字段和未填写位置。
- 分公司签约授权与代理人签署授权分别审查。条件句不证明条件成立，未检索到外部授权书不等于未授权。有对应主体事实且仍需外部材料确认时，以待核实事项和空 anchors 进入总结；不将纯假设写成缺陷。
- Summarizer 必须保留输入中的不确定性，不将“待核实”升级成“授权缺失”或“签署无效”。输出字段和工作流阶段不变。

这些是模型行为约束，不是确定性正确率保证；需要正式审查产物验证，不能以提示词文本测试代替实际准确度评估。

| Agent | 文件 | 职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| PlannerAgent | `agents/planner.py` | 只为用户上传标准提取五种路线和批注去向 | 用户标准 skeleton | `CriterionStrategy` |
| OrchestratorAgent | `agents/orchestrator.py` | 逐条派发 SubAgent，串联检索、反思、并发控制 | `criteria_list` | `results` |
| SubAgent | `agents/base_agent.py` 的 `Agent` + `SUB_AGENT_BASE_PROMPT` | 审查单条标准；配置完整覆盖审计时逐证据返回判断 | 标准 + 检索上下文 + 可见工具 | `SubAgentOutput`（status / issues / coverage_decisions） |
| ReflectorAgent | `agents/reflector.py` | 逐条质量复核 finding，并检查完整覆盖任务是否漏判输入证据 | criterion + subagent_output + 检索证据 | `{ status, feedback, finding_decisions[], coverage_gaps[] }` |
| SummarizerAgent | `agents/summarizer.py` | 生成总览并标记跨要点语义重复；不改变锚点，不删除独立问题 | 带稳定 issue_key 的已确认问题 | `{ overall_comment, priority_comments, duplicate_groups }` |

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
    finding_decisions: list[FindingDecision] = []  # finding_id + confirm/revise/remove + reason

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
   - 固定任务直接使用强制路由已提供的证据，不对 `anchors=[]` 问题反复执行缺失文本补充检索；用户上传要点仍可执行该补充检索。每轮 Reflector 使用独立会话，避免完整证据在历史消息中叠加。
   - `Reflector.review` 对每个 finding 返回 `confirm/revise/remove`；一个 finding 需修改不会连带删除同一 criterion 中已经确认的问题。
   - 全部 finding 决策完整且满足 PASS 才结束；达到上限或反思异常时标记 `quality_status=pending_review`，但已确认 finding 仍可交付，未确认 finding 只留在状态和日志中。
   - 修订仍指向同一 edit 原文缺陷时，Orchestrator 恢复原 `issue_id`，避免模型改号导致 finding 身份漂移。

每次 Planner、SubAgent、Reflector、Summarizer 等 `Agent` 调用在发送前统一计算完整消息及工具 schema 的 token 预算。`llm.max_prompt_tokens` 默认 88k 且配置值必须小于 90k；超限请求不会发送，trace 的 LLM input 记录 `estimated_prompt_tokens` 和 `prompt_token_limit`。
5. 返回该 criterion 结果：`issues` 只含已确认问题，`unresolved_issues` 只供日志诊断；另含 `criterion_id`、`section`、`status`、`quality_status`、`quality_history` 和 `finding_decisions`。

并发与容错（`execute_criteria`）：

- `execute_criteria` 会为 `criteria_list` 中的每条审查标准创建一个 `execute_with_limit(criterion)` coroutine，并用 `asyncio.gather(*execute_tasks, return_exceptions=True)` 一次性调度和等待这些任务。
- `asyncio.gather` 负责并发调度：多条 criterion 可以同时进入等待队列，并在外部模型调用、检索或反思等待期间交错推进。
- `asyncio.Semaphore(MAX_ORCHESTRATOR_CONCURRENCY)` 负责并发限制：同一时间最多允许指定数量的 criterion 进入 `execute_single_criterion()`；其余 coroutine 会等待 semaphore 释放。
- 因此，当前并发边界是“多条 criterion / 多个 SubAgent 审查任务并发”，不是单个 SubAgent 内部把同一条标准拆成多个并发步骤。
- 单条 criterion 最多执行 `workflow.criterion_retry_max_attempts` 次；重试耗尽后的 `ModelCallError` 或 `AgentOutputValidationError` 降级为该条 `status=ERROR`（记录 `error_message`），其他 criterion 继续执行并生成部分结果 DOCX。
- 未知程序异常保持任务级失败；全部 criterion 都失败时也不生成伪成功结果。部分 criterion 失败时任务为 `succeeded`，同时以 `quality.status=pending_review`、`quality.error_criteria` 和 DOCX“未完成审查项”披露缺口。

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
固定 criteria-routes.yaml -> 18个 mandatory tasks
用户上传标准 DOCX -> deterministic split -> Planner -> uploaded tasks
mandatory tasks + uploaded tasks -> criteria_list
合同 DOCX     -> DOCX XML anchor nodes -> 按 retrieval.backend 构建临时索引
每条 criterion: 检索 context -> SubAgent(可调用白名单工具) -> (Reflector 反思循环) -> 单条结果
所有单条结果 -> Workflow 按 placement 和 edit/evidence 分流
已确认问题 -> 按 DOCX paragraph_path/start_index/end_index 精确防重（mandatory 优先，同来源按 dedupe_priority），再对配置为同一 dedupe_domain 的跨要点包含范围防重
精确防重结果 -> Summarizer 标记语义重复 -> Workflow 校验并应用合并 -> final_review
已确认 edit anchors + final_review -> generate_docx_report -> 批注版 DOCX
```

Workflow 只把已确认问题交给 Summarizer 和 DOCX Writer；`role=edit` 才能写原文批注，`role=evidence` 只保留为审查证据。完全相同字符范围只保留一条：固定任务优先于上传任务，同来源按路线配置的 `dedupe_priority` 选择，顺序仅作为同优先级兜底。对显式配置为同一 `dedupe_domain` 的不同强制要点，同段中一个字符范围完全包含另一范围时，也按优先级保留一条；未配置领域、同一要点内或仅部分相交时不自动合并。其他不同范围只有在 Summarizer 明确判定为同一事实、同一风险和同一修改动作时才合并。固定提示来自路线配置的 `review_instruction`，不由正则猜测去向。

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

整份合同并发由 worker 数量和 Celery `--concurrency` 控制；单份合同内固定任务和上传任务共同受 `workflow.max_orchestrator_concurrency` 控制。这两个限制不是同一层。路由模块不创建新的线程池、进程池或 semaphore。

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

`model_semaphore.llm`、`embedding`、`reranker` 使用 Redis 租约控制跨进程、跨容器的模型 HTTP 请求总并发。固定任务跳过 Planner/路由 LLM，但其检索、SubAgent 和 Reflector 模型请求仍走现有客户端和同一 Redis 全局限制；路由模块没有旁路模型调用。

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

见[审查质量与批注规则](REVIEW_QUALITY.md)：逐字段 finding 独立确认，固定与上传标准分流规划，并在输出前按精确字符范围执行固定优先去重。
