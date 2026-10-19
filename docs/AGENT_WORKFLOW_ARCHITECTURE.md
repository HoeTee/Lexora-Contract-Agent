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

`agents/prompts.yaml` 中 SubAgent 负责判定，现有 Reflector 独立复核；不增加新的 Agent 类型：

- “照抄”等编辑指令不作为最终意见，也不能在目标文字不明确时编造提示语。通用提醒返回 `anchors=[]` 进入总结；具体缺陷才锚定问题原文。
- 字段是否未填写由 LLM 结合字段名称、相邻文字、表格关系和合同语境判断，不由业务正则硬匹配。`[合同中心-合同编码]`、`#乙方合同号#【     】`、字段后的 `XX` 是提示词示例；`【5‰】`、`【5%】` 等已有值和条件性补件说明是反例。批注必须直接引用对应字段和未填写位置。
- 分公司签约授权与代理人签署授权分别审查。条件句不证明条件成立，未检索到外部授权书不等于未授权。有对应主体事实且仍需外部材料确认时，以待核实事项和空 anchors 进入总结；不将纯假设写成缺陷。
- 审查业务范围以输入的原 checkpoint 为准，不为某个要点编号在 Agent 代码内增加判断分支。`quoted_text` 定位现有原文，`comment_text` 提供修改建议；附件编号、标题和归属由检索证据提供，模型不能自行编造。
- Summarizer 必须保留输入中的不确定性，不将“待核实”升级成“授权缺失”或“签署无效”。输出字段和工作流阶段不变。

这些是模型行为约束，不是确定性正确率保证；需要正式审查产物验证，不能以提示词文本测试代替实际准确度评估。

| Agent | 文件 | 职责 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| PlannerAgent | `agents/planner.py` | 只为用户上传标准提取执行计划，固定标准从已有 YAML 转换同一种计划 | 用户标准 skeleton | `CriterionStrategy.execution` |
| OrchestratorAgent | `agents/orchestrator.py` | 逐条派发 SubAgent，串联检索、反思、并发控制 | `criteria_list` | `results` |
| SubAgent | `agents/base_agent.py` 的 `Agent` + `SUB_AGENT_BASE_PROMPT` | 审查单条标准；配置完整覆盖审计时逐证据返回判断 | 标准 + 检索上下文 + 可见工具 | `SubAgentOutput`（status / issues / coverage_decisions） |
| ReflectorAgent | `agents/reflector.py` | 逐条质量复核 finding，按明确审计模式检查覆盖，并检查当前标准范围内有证据的实质漏检 | criterion + subagent_output + 检索证据 + finding_history + audit_mode | `{ status, feedback, finding_decisions[], coverage_gaps[] }` |
| SummarizerAgent | `agents/summarizer.py` | 生成总览并标记跨要点语义重复；程序只接受 edit anchor 与 placement 完全相同的重复组，不改变锚点，不删除独立问题 | 带稳定 issue_key 的已确认问题及 `incomplete_reviews` 未完成范围 | `{ overall_comment, priority_comments, duplicate_groups }` |

同一审查要点在 Reflector 修订后由程序按原文 edit anchor 恢复已有 `issue_id`；新增问题若占用了已有编号，程序分配无碰撞后缀。Reflector 或修订模型调用失败、修订结果校验失败时抛回 criterion 恢复流程；完整重试耗尽后记录为该要点 `ERROR`。只要仍有至少一个有效要点，工作流继续生成总结和 DOCX；执行阶段仅在全部要点均失败时中止。未知程序、状态保存、文件读写和 DOCX 生成错误仍导致任务失败。

SubAgent 的一个 issue 含多个 edit anchor 时，FindingLedger 按锚点拆成独立 finding，并为拆分结果生成唯一的局部 `issue_id`；原 issue 的判断内容不变，每个位置仍分别进入 Reflector 与批注写入器。

基础设施：`agents/base_agent.py`（`Agent` 基类与 `Settings` 模型配置）、`agents/schemas.py`（结构化输出 schema）、`agents/prompts.yaml`（集中维护 agent 提示词，含审查边界和结构化输出要求）、`agents/prompts.py`（加载提示词并填入本次任务数据）。系统提示、任务模板和输出修复提示统一由该 YAML 提供；各 agent 只传入标准、证据、模型结果等动态内容。旧版 `agents/prompts/` 目录不再保留。

## 智能体结构化输出 schema

各 agent 的输出由 `agents/schemas.py` 的 Pydantic 模型严格校验（`extra=forbid`，不接受未定义字段）。检索与执行能力类型统一定义在 `routing/schemas.py`；Agent schema 复用共享类型，不再重复定义批注位置。主要类与字段如下：

```python
class ReviewExecutionPlan:                 # routing/schemas.py，共享内部执行计划
    route: RoutePlan | None = None
    placement: Placement | None = None
    review_instruction: str = ""
    audit_mode: AuditMode                  # coverage / comparison，默认 false
    edit_region: str = ""
    evidence_region: str = ""

class CriterionStrategy:
    id: str
    execution: ReviewExecutionPlan

class SubAgentIssue:
    issue_id: str
    risk_level: Literal["high", "medium", "low"]
    issue_comment: str
    anchors: list[SubAgentAnchor]
    reasoning: str
    criterion: str
    check_point: str

class SubAgentAnchor:
    role: Literal["edit", "evidence"]    # 修改位置 / 对照证据
    xml_anchor_type: Literal["paragraph", "table"]
    xml_anchor_id: str
    quoted_text: str
    comment_text: str                     # edit: 1~300 字；evidence: 空字符串

class SubAgentOutput:
    status: Literal["compliant", "issues_found", "not_applicable"]
    applicability_reason: str = ""
    issues: list[SubAgentIssue] = []
    coverage_decisions: list[EvidenceCoverageDecision] = []
    comparison_decisions: list[EvidenceComparisonDecision] = []

class ReflectorOutput:
    status: Literal["PASS", "REJECT"]
    feedback: str = ""
    finding_decisions: list[FindingDecision] = []  # finding_id + confirm/revise/remove + reason
    coverage_gaps: list[CoverageGap] = []

class SummaryOutput:
    overall_comment: str = ""
    priority_comments: list[str] = []     # overall + priority 合计 <=200 字
```

## 单条标准的执行与反思循环

固定 YAML 的 `route/placement/review_instruction/coverage_audit/comparison_audit/edit_region/evidence_region` 由 `routing/profile.py` 一次性转换为 `task.execution`。用户上传标准由 Planner 返回同一种 `ReviewExecutionPlan`。原用户配置文件写法不变，不新增配置，也不保留一套旧平铺执行字段供运行时猜测。

`execution.route` 负责怎样取原文；`execution.audit_mode` 负责是否要求逐项记录和成对比较；`execution.edit_region/evidence_region` 限定批注与佐证的来源；`execution.placement` 指定报告去向。检索方法不能代替审查要求：同一个 `scan` 路线可以服务不同检查能力。启用成对比较时必须明确对照证据范围；不能默认所有比较都是正文与附件。Orchestrator 读取执行计划，不按 H 编号或固定/上传来源选择检查规则。`route=None` 明确表示由通用检索流程选择路线，取代重复的 `fallback_llm_route` 标记。

`OrchestratorAgent.execute_single_criterion` 负责一条审查标准的完整处理：

1. 检索：调用 MCP `contract_search`（query = 标准 + 检查要点），由 `retrieval.backend` 选择 LlamaIndex 或 docxindex，得到合同相关片段 `context`。检索证据不添加展示序号，只提供 `xml_anchor_type/xml_anchor_id` 给 SubAgent 逐字符复用，避免展示序号与真实段落锚点混淆。
2. SubAgent 审查：以 `SUB_AGENT_BASE_PROMPT` 创建 `SubAgent_<cid>`，只向模型暴露 `workflow.subagent_allowed_tools` 白名单中的 MCP 工具，输出 `SubAgentOutput`，`status` 为 `compliant` / `issues_found` / `not_applicable`，`issues[].anchors` 含 `xml_anchor_type`、`xml_anchor_id`、`quoted_text`、`comment_text` 等字段。
3. 若 `status == compliant` 且未启用完整覆盖审计：短路，跳过反思直接返回。
4. 否则进入反思循环，最多 `MAX_REFLECTION_ROUNDS` 轮：
   - 已有明确执行路线时使用路线提供的证据，不对 `anchors=[]` 问题反复执行缺失文本补充检索；路线为空时可执行通用补充检索。这一行为与要点来源无关。每轮 Reflector 使用独立会话，并接收每个 finding 最近两次裁决、理由和位置组成的 `finding_history`；改变前轮判断时须说明新增证据或前轮具体错误。
   - `audit_mode.coverage/comparison` 来自 `execution.audit_mode`。模型自行返回的 `coverage_decisions/comparison_decisions` 不能开启完整审计；当前标准范围内有证据的实质漏检仍须检查。
   - `Reflector.review` 对每个 finding 返回 `confirm/revise/remove`，含义分别为保留、修改、撤销。schema 检查字段、枚举、重复或未知编号、漏裁决等结构要求；是否完成由 Orchestrator 检查具体待办与结果，不能只看模型返回的 `PASS/REJECT`。全部当前问题均确认或撤销，没有待修改、待恢复或漏检事项时，程序应用删除，再次校验原文锚点、已启用的覆盖/比较要求和标准约束，通过才结束。即使模型原始总标记为 `REJECT`，满足这些条件也能当轮完成；原始 `PASS` 带待修改事项则仍不能结束。空拒绝而无具体处理内容不能自动完成。原始总标记保留在 `quality_history[].review.status`，程序有效结论记为 `quality_history[].effective_status`。
   - 需要修订时重建 SubAgent 会话，保留系统提示、原任务和最新检索证据，附上当前完整输出及本轮指令；反思修订以结构化裁决和问题映射为准，旧相反反馈不再叠加。修订仍指向同一 edit 原文缺陷时，Orchestrator 恢复原 `issue_id`，避免模型改号导致 finding 身份漂移。
   - 达到轮次上限时标记 `quality_status=incomplete`，但已确认 finding 仍可交付，未确认 finding 只留在状态和日志中。反思调用异常会进入 criterion 恢复流程，重试耗尽后记录为 `ERROR`。

每次 Planner、SubAgent、Reflector、Summarizer 等 `Agent` 调用在发送前统一计算完整消息及工具 schema 的 token 预算。`llm.max_prompt_tokens` 默认 88k 且配置值必须小于 90k；超限请求不会发送，trace 的 LLM input 记录 `estimated_prompt_tokens` 和 `prompt_token_limit`。
5. 返回该 criterion 结果：`issues` 只含已确认问题，`unresolved_issues` 只供日志诊断；另含 `criterion_id`、`section`、`status`、`quality_status`、`quality_history` 和 `finding_decisions`。复核完成且保留问题时为 `ISSUES_FOUND + passed`，全部问题撤销时为 `COMPLIANT + passed`；不适用时保留 `NOT_APPLICABLE`。初审合规短路仍为 `COMPLIANT + not_required`。仍有待办且达到轮数上限时为 `status=null + quality_status=incomplete`，不会把尚未完成的范围标成合规或完整的问题结论；已确认问题仍可写入报告。模型或输出校验重试耗尽则为 `ERROR`。

运行中要点更新与最终 API 列表都通过 `review_progress_from_result()` 转换。未完成项统一为 `execution_status=incomplete, review_result=null`，错误项为 `error/null`，已完成项才公开 `compliant/issues_found/not_applicable`。这不改变 Celery 或整份任务的 `status`；部分要点未完成时，整份任务仍按已有规则返回可下载的部分结果，并由 `quality.incomplete_count` 和报告“未完成审查项”披露缺口。

并发与容错（`execute_criteria`）：

- `execute_criteria` 会为 `criteria_list` 中的每条审查标准创建一个 `execute_with_limit(criterion)` coroutine，并用 `asyncio.gather(*execute_tasks, return_exceptions=True)` 一次性调度和等待这些任务。
- `asyncio.gather` 负责并发调度：多条 criterion 可以同时进入等待队列，并在外部模型调用、检索或反思等待期间交错推进。
- `asyncio.Semaphore(MAX_ORCHESTRATOR_CONCURRENCY)` 负责并发限制：同一时间最多允许指定数量的 criterion 进入 `execute_single_criterion()`；其余 coroutine 会等待 semaphore 释放。
- 因此，当前并发边界是“多条 criterion / 多个 SubAgent 审查任务并发”，不是单个 SubAgent 内部把同一条标准拆成多个并发步骤。
- `workflow.model_call_timeout_seconds` 默认 60 秒，限制每次取得名额后的实际 HTTP 请求；网络重试、请求超时重试及输出修复都重新取得名额、独立计时。它们共用 `model_call_max_retries` 发送上限，默认每次逻辑操作最多发送 6 次。名额等待使用 `workflow.model_wait_timeout_seconds`，默认同一逻辑操作累计 180 秒，不因重试重置。退避及输出校验不扣下一次请求的 60 秒，合同总时限仍覆盖全部阶段。等待预算耗尽、合同到期或取消不触发请求重试。发送额度耗尽后的 `ModelCallError` 或 `AgentOutputValidationError` 交给要点恢复流程；`workflow.criterion_retry_max_attempts` 默认 3，包含首次，最多重新执行该失败要点两次。整要点尝试耗尽后该条记为 `status=ERROR`（记录 `error_message`），其他 criterion 继续执行并生成部分结果 DOCX。
- 未知程序异常保持任务级失败；全部 criterion 都失败时也不生成伪成功结果。部分 criterion 失败时任务为 `succeeded`，同时以 `criteria[].execution_status=error`、`quality.error_criteria` 和 DOCX“未完成审查项”披露缺口。

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
mandatory tasks + uploaded tasks -> criteria_list（共享 execution 结构）
合同 DOCX     -> DOCX XML anchor nodes -> 按 retrieval.backend 构建临时索引
每条 criterion: 检索 context -> SubAgent(可调用白名单工具) -> (Reflector 反思循环) -> 单条结果
所有单条结果 -> Workflow 按 placement 和 edit/evidence 分流
已确认问题 -> 按 DOCX paragraph_path/start_index/end_index 精确防重（mandatory 优先，同来源按 dedupe_priority），再对配置为同一 dedupe_domain 的跨要点包含范围防重
精确防重结果 -> Summarizer 标记语义重复 -> Workflow 校验并应用合并 -> final_review
已确认 edit anchors + final_review -> generate_docx_report -> 批注版 DOCX
```

Workflow 只把已确认问题交给 Summarizer 和 DOCX Writer；`role=edit` 才能写原文批注，`role=evidence` 只保留为审查证据。完全相同字符范围只保留一条：固定任务优先于上传任务，同来源按路线配置的 `dedupe_priority` 选择，顺序仅作为同优先级兜底。对显式配置为同一 `dedupe_domain` 的不同强制要点，同段中一个字符范围完全包含另一范围时，也按优先级保留一条；未配置领域、同一要点内或仅部分相交时不自动合并。其他不同范围只有在 Summarizer 明确判定为同一事实、同一风险和同一修改动作时才合并。固定提示来自路线配置的 `review_instruction`，不由正则猜测去向。

要点未完成时，已确认问题仍进入相同的批注和总结流程，包括写在总结位置的确认意见。Summarizer 同时收到 `incomplete_reviews`（要点名称、执行状态、已确认意见数），不能只因问题列表为空就称全部合规。报告首部独立列出“未完成审查项”；若已有部分确认意见，提示这些意见已列出、剩余内容尚未完成，避免把部分意见说成完整结论。

## 汇总失败与去重建议的边界

Summarizer 接收所有已确认意见，但不能自由配对不同原文位置。程序按完整 edit anchor 签名（type、id、去空白的 quoted_text）及 placement 生成至少包含两个不同 issue_key 的候选组，模型仅判断候选内是否属于同一事实、同一风险和同一修改动作。候选不包含无锚点问题或编号歧义项。

汇总文字与可选去重建议分别校验。单个去重建议仍使用严格的 `SummaryDuplicateGroup` 检查；空合并列表、未知键、错位置、自合并及跨组重复或循环不会删除任何相关原意见。无效建议被跳过，原因记录到 `summary_metadata.skipped_group_details`；合法建议在应用时再次验证。新增的候选和审计字段属于内部执行数据，不是用户配置。

工作流在调用汇总模型之前先保存审查结果，成功应用去重后再保存最终结果。汇总模型调用或 JSON/schema 修复耗尽时，只有已知 `ModelCallError` 和 `AgentOutputValidationError` 可以触发程序基础总览；不再发送新的模型请求，不重置预算。基础总览只列统计、已确认意见和未完成范围，报告独立注明“自动总结未生成”。原要点的 completed/incomplete/error 和质量统计不改变，H04 等失败项仍保持失败。

工作流返回并在任务/API 中保存 `summary.status=completed|fallback`、错误信息及可用的去重诊断。回退时 API 消息与报告说明保持一致。真正整任务失败或取消后返回原件时，API 的 `output.kind=source` 并使用原文件名；正常审查报告为 `output.kind=report`。下载就绪不代表已完成全部审查。

## 架构边界

- 简化工作流：无 web search、无制度 RAG，检索后端由 `retrieval.backend` 选择。
- 审查只基于合同文本和检索片段。完全依赖工商、涉诉、知识产权、法人授权或现行法律知识库的检查点必须返回 `not_applicable`，不能编造外部事实；合同文本本身足以判断的部分仍应审查。
- 正文与附件分别审查；只有审查标准明确要求时才扩展正文与附件内容一致性检查。

## API 队列与进程边界

下述 DB 0/DB 2 编号沿用部署模板；实际职责由 broker_url 和模型 redis_url 决定。本地当前配置为 broker DB 2、模型名额 DB 0，本轮不调整地址。

`/web` 和 `/api` 是仅有的服务执行入口，OA 已移除。CLI 也通过 `/api` 提交。Web/API 写入 `task.json` 后，固定通过 `task_queue.review_queue` 投递到 Redis DB 0；Celery worker 执行 `review.run_job`。Broker 只保存 `client_dir + task_id` 调度消息，不保存合同、审查结果或恢复状态。入队失败明确报错，不回退到 API 进程执行。

```text
Web/API process -> Redis DB 0 broker -> 3 Celery workers -> workflow.run()
```

Web/API 进程负责提交、状态和结果响应，worker 负责 workflow、MCP、模型调用和 DOCX 生成。Web 完成后同样更新历史、展示结果并支持下载。

整份合同并发由三个 worker 及每个 worker 的 `--pool=prefork --concurrency=1` 控制；单份合同内固定任务和上传任务共同受 `workflow.max_orchestrator_concurrency` 控制，初值 2。模型 HTTP 合计上限由 Redis DB 2 控制，LLM/embedding/reranker 初值为 5/3/10。路由模块不创建新的模型请求限流器。

队列等待上限 `queue.queued_timeout_seconds` 默认为 7200 秒，只适用于尚未被 worker 接手的任务；Celery 整合同硬时限 `queue.task_time_limit_seconds` 默认为 1800 秒，从执行开始计时。不配置软时限；硬终止由 worker 父进程侧记录失败并补充执行进程，不自动重新执行合同。

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

成功的 criterion 输出保留为恢复数据，版本兼容时直接复用。当前 `review_contract_version=7`；旧版计划和输出不复用，当前版计划还须通过共享 `ReviewExecutionPlan` 校验，不对旧平铺执行字段做兼容猜测。失败 criterion 按 `workflow.criterion_retry_max_attempts` 执行，默认 3 包含首次，即最多整条重跑两次；重跑的模型操作建立新预算，仍受原合同截止时间和同一全局并发上限约束。详细模式下，`error.json` 保留失败尝试，`output.json` 的 `attempt` 记录成功属于哪次执行。Redis broker 不承担业务状态持久化。当前恢复边界不覆盖 ingest、build_index 和 plan 失败，也不提供硬超时后的自动整合同重投。

## 全局模型并发

`model_semaphore.llm/embedding/reranker.max_concurrent_requests` 分别控制三个 worker 合计的三类模型请求数。每类保留 `redis_url`、`key`、`lease_seconds`、`poll_interval_seconds`；删除 `enabled` 和 `wait_timeout_seconds`，全局限流固定启用。旧共享格式必须迁移，不能静默忽略旧字段。

`endpoints/runtime/model_calls.py` 统一处理等待名额、请求、重试退避、输出修复和逐次日志；同一逻辑调用复用 `model_budget.py` 中的累计等待预算及发送次数，每次实际 HTTP 请求另行建立独立时限。取得 Redis 名额后开始请求计时；请求退出或取消完成后释放名额，重试重新排队。退避和输出校验不占下一次请求时限，每个阶段都服从整合同截止时间。logical_calls 汇总首次发送和重试，不代表共享请求倒计时。所有 Agent、DocxIndex、LlamaIndex 请求都使用这条入口，SDK 重试关闭；同步调用通过 `run_sync` 等待可取消的异步 HTTP。

`llm_semaphore.py` 使用 DB 2 的 Lua 原子分配 FIFO 名额；正常结束主动释放，进程崩溃后依靠租约回收。租约到期不会取消服务端推理，客户端断开是否停止内网模型服务推理由网关能力决定。DocxIndex 重排已接入配置的独立 reranker，不再占用 LLM 名额；LlamaIndex 同样使用该重排适配器。未被业务使用的 LlamaIndex 流式入口明确拒绝，不能绕过统一控制。

全部配置的含义、默认值、删除项及迁移步骤见 [并发与超时配置说明](CONCURRENCY_TIMEOUT_CONFIGURATION.md)。

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
