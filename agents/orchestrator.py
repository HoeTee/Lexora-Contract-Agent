"""
OrchestratorAgent - dispatches sub-agents per criterion with retrieval + reflection.

"""
import asyncio
import json
import re
import time

from config import (
    CRITERION_RETRY_MAX_ATTEMPTS,
    MAX_REFLECTION_ROUNDS,
    MAX_ORCHESTRATOR_CONCURRENCY,
    RETRIEVAL_BACKEND,
    SUBAGENT_ALLOWED_TOOLS,
)
from agents.base_agent import Agent
from agents.json_utils import AgentOutputValidationError, chat_until_valid_json
from agents.reflector import ReflectorAgent
from agents.prompts.cn_prompts import SUB_AGENT_BASE_PROMPT
from agents.schemas import SubAgentOutput
from endpoints.runtime.errors import ModelCallError, classify_model_call_error
from endpoints.review.review_state import ReviewStateStore
from loggers.trace_helpers import criterion_inputs, criterion_outputs, error_info
from loggers.trace_logger import get_current_trace


_PLACEMENT_DIRECTIVES = {
    "summary": "本标准发现的问题一律使用 anchors=[] 输出；系统会将 issue_comment 汇总进入文首审查总结，不得挂到具体条款。",
    "inline_at_title": "本标准发现的问题，批注锚点必须指向相关标题所在段落。",
    "tail": "本标准发现的问题，批注锚点必须指向合同末尾签署栏区域的段落。",
    "merged_by_party": "本标准针对合同首部主体信息：按甲方、乙方分别输出问题，锚点指向首部对应主体信息所在段落。",
    "body_end": "本标准发现的问题使用 anchors=[] 输出；系统会将批注挂到正文结尾处。",
}


SUB_AGENT_EXPECTED_JSON = """
{
  "status": "compliant 或 issues_found 或 not_applicable",
  "applicability_reason": "仅当 status 为 not_applicable 时填写不适用理由；其他状态可省略",
  "issues": [
    {
      "issue_id": "当前criterion_id.序号",
      "risk_level": "high | medium | low",
      "issue_comment": "问题整体说明，160字以内",
      "anchors": [
        {
          "xml_anchor_type": "paragraph | table",
          "xml_anchor_id": "必须复用检索结果中的 xml_anchor_id",
          "quoted_text": "逐字摘录的单一连续合同原文",
          "comment_text": "写入该处 Word 批注的修改意见，100字以内"
        }
      ],
      "reasoning": "说明为什么构成实质风险，以及如何对应 check_point",
      "criterion": "当前审查标准原文",
      "check_point": "该 issue 对应的具体检查点"
    }
  ]
}
"""


class OrchestratorAgent:
    """Coordinates per-criterion sub-agents with retrieval and reflection."""

    def __init__(
        self,
        mcp_client=None,
        logger=None,
        settings=None,
        api_events_path: str | None = None,
        state_store: ReviewStateStore | None = None,
    ):
        self.mcp_client = mcp_client
        self.logger = logger
        self.settings = settings
        self.api_events_path = api_events_path
        self.state_store = state_store
        self.tools = None
        self._warned_missing_subagent_tools = False
        self.retrieval_tokens = 0  # Track MCP tool internal LLM tokens

    async def _get_tools(self) -> list[dict]:
        """Cache MCP tools list."""
        if self.tools is None:
            tools = await self.mcp_client.get_available_tools()
            self.tools = tools
        return self.tools

    def _filter_tools_by_name(
        self,
        tools: list[dict],
        allowed_names: set[str],
    ) -> list[dict]:
        """Return only tools visible to the current agent role."""
        filtered = [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") in allowed_names
        ]
        exposed_names = {
            tool.get("function", {}).get("name")
            for tool in tools
            if tool.get("function", {}).get("name")
        }
        missing = allowed_names - exposed_names
        if missing and not self._warned_missing_subagent_tools:
            print(
                "[Orchestrator] SubAgent allowed tools not exposed by MCP: "
                f"{sorted(missing)}"
            )
            self._warned_missing_subagent_tools = True
        return filtered

    async def _retrieve_context(
        self, cid: str,
        criterion_text: str,
        check_points_text: str,
        start: float,
        route_override: str | None = None,
    ) -> tuple[str, int]:
        """Unified retrieval: returns (context_text, retrieval_tokens)."""
        search_query = f"{criterion_text}\n检查要点：{check_points_text}"
        retrieval_tokens = 0

        print(f"[Orchestrator] {cid}: Searching contract via temporary {RETRIEVAL_BACKEND} index...")
        search_payload = {
            "query": search_query,
            "api_events_path": self.api_events_path,
        }
        if route_override:
            search_payload["route_override"] = route_override
            search_payload["criterion_id"] = cid
        search_result = await self.mcp_client.call_tool(
            "contract_search",
            search_payload,
        )
        if isinstance(search_result, str) and search_result.startswith("Error"):
            raise classify_model_call_error(
                search_result,
                default_component="embedding" if RETRIEVAL_BACKEND == "llamaindex" else "agent",
            )
        context = search_result if search_result else "未找到相关内容。"

        if self.logger:
            self.logger.log(
                phase="Execute", sender="Orchestrator", receiver="MCP:contract_search",
                action=f"contract_search({RETRIEVAL_BACKEND},{cid})",
                input_summary=criterion_text,
                output_summary=f"{len(context)} chars retrieved",
                duration=round(time.time() - start, 2)
            )

        return self._prepare_review_context(context), retrieval_tokens

    def _prepare_review_context(
            self,
            context: str
    ) -> str:
        """Add guardrails so retrieval wrapper text is not mistaken for contract locations."""
        normalized_context = context or "未找到相关内容。"
        normalized_context = re.sub(
            r"^##\s*检索结果\s*\d+(?:\s*\([^)]*\))?\s*$",
            "",
            normalized_context,
            flags=re.MULTILINE,
        )
        normalized_context = re.sub(r"\n{3,}", "\n\n", normalized_context).strip()
        guidance = (
            '注意：下面内容中的”检索结果1/2””相关度分数””检索片段标题”等仅是检索系统包装信息，'
            '不是合同原始条款标题或所在位置。输出”所在位置”时只能填写合同原文中真实出现的条款、'
            '章节或段落位置；若合同原文未标明具体条款编号，请写”合同缺失审查要点要求书写的内容”，'
            '绝对不要照抄检索包装标题。'
        )
        anchor_guidance = (
            "每条检索结果会提供 xml_anchor_type 和 xml_anchor_id。"
            "输出 anchors 时必须原样复用对应检索结果中的 xml_anchor_type/xml_anchor_id；"
            "quoted_text 只能摘录该 anchor 对应合同文本中的单一连续片段。"
        )
        return f"{guidance}\n{anchor_guidance}\n\n{normalized_context}"

    @staticmethod
    def _normalize_anchor_text(text: str) -> str:
        normalized = re.sub(r"[\s\u3000]+", "", str(text or ""))
        return (
            normalized.replace("（", "(")
            .replace("）", ")")
            .replace("：", ":")
            .replace("，", ",")
            .replace("。", ".")
            .replace("；", ";")
        )

    @staticmethod
    def _retrieval_anchor_evidence(context: str) -> list[dict[str, str]]:
        """Parse the anchor-scoped contract text included in retrieval context."""
        pattern = re.compile(
            r"xml_anchor_type:\s*(paragraph|table)\s*\n"
            r"xml_anchor_id:\s*([^\n]+)\s*\n"
            r"内容：\s*\n(.*?)(?=\n\n---\n\n|\Z)",
            flags=re.DOTALL,
        )
        return [
            {
                "xml_anchor_type": match.group(1).strip(),
                "xml_anchor_id": match.group(2).strip(),
                "content": match.group(3).strip(),
            }
            for match in pattern.finditer(context or "")
            if match.group(3).strip()
        ]

    @staticmethod
    def _normalized_span(text: str, quote: str) -> str | None:
        """Return the exact source substring for a normalized in-source match."""
        normalized_quote = OrchestratorAgent._normalize_anchor_text(quote)
        if not normalized_quote:
            return None
        normalized_chars: list[str] = []
        source_indexes: list[int] = []
        for source_index, char in enumerate(text):
            normalized_char = OrchestratorAgent._normalize_anchor_text(char)
            if not normalized_char:
                continue
            normalized_chars.append(normalized_char)
            source_indexes.extend([source_index] * len(normalized_char))
        normalized_text = "".join(normalized_chars)
        match_start = normalized_text.find(normalized_quote)
        if match_start < 0:
            return None
        match_end = match_start + len(normalized_quote) - 1
        return text[source_indexes[match_start]:source_indexes[match_end] + 1]

    def _validate_and_repair_anchors(
        self,
        parsed_opinion: dict,
        context: str,
    ) -> dict[str, int]:
        """Repair provable model anchor mistakes and drop unprovable anchors."""
        evidence = self._retrieval_anchor_evidence(context)
        evidence_by_key = {
            (item["xml_anchor_type"], item["xml_anchor_id"]): item
            for item in evidence
        }
        stats = {"valid": 0, "repaired": 0, "dropped": 0}

        for issue in parsed_opinion.get("issues", []):
            repaired_anchors: list[dict] = []
            for anchor in issue.get("anchors") or []:
                key = (anchor.get("xml_anchor_type", ""), anchor.get("xml_anchor_id", ""))
                quote = str(anchor.get("quoted_text") or "").strip()
                declared = evidence_by_key.get(key)
                exact_quote = self._normalized_span(declared["content"], quote) if declared else None
                if exact_quote:
                    anchor["quoted_text"] = exact_quote
                    repaired_anchors.append(anchor)
                    stats["valid"] += 1
                    continue

                normalized_quote = self._normalize_anchor_text(quote)
                if declared:
                    normalized_content = self._normalize_anchor_text(declared["content"])
                    if len(normalized_content) >= 4 and normalized_content in normalized_quote:
                        anchor["quoted_text"] = declared["content"]
                        repaired_anchors.append(anchor)
                        stats["repaired"] += 1
                        continue

                candidates: list[tuple[dict[str, str], str]] = []
                for item in evidence:
                    candidate_quote = self._normalized_span(item["content"], quote)
                    if candidate_quote:
                        candidates.append((item, candidate_quote))
                        continue
                    normalized_content = self._normalize_anchor_text(item["content"])
                    if len(normalized_content) >= 4 and normalized_content in normalized_quote:
                        candidates.append((item, item["content"]))

                unique_candidates = {
                    (item["xml_anchor_type"], item["xml_anchor_id"], candidate_quote): (item, candidate_quote)
                    for item, candidate_quote in candidates
                }
                if len(unique_candidates) == 1:
                    item, candidate_quote = next(iter(unique_candidates.values()))
                    anchor["xml_anchor_type"] = item["xml_anchor_type"]
                    anchor["xml_anchor_id"] = item["xml_anchor_id"]
                    anchor["quoted_text"] = candidate_quote
                    repaired_anchors.append(anchor)
                    stats["repaired"] += 1
                    continue

                stats["dropped"] += 1
            issue["anchors"] = repaired_anchors
        return stats

    async def _build_missing_text_review_notes(self, parsed_opinion: dict) -> str:
        """Search again for issues that claim a missing contract provision."""
        notes = []
        for issue in parsed_opinion.get("issues", []):
            if issue.get("anchors"):
                continue

            issue_id = issue.get("issue_id", "")
            check_point = issue.get("check_point", "")
            criterion_text = issue.get("criterion", "")
            search_query = (
                f"查找合同中是否约定以下检查要点：{check_point}\n"
                f"所属审查标准：{criterion_text}"
            )
            search_result = await self.mcp_client.call_tool(
                "contract_search",
                {
                    "query": search_query,
                    "api_events_path": self.api_events_path,
                },
            )
            if isinstance(search_result, str) and search_result.startswith("Error"):
                raise classify_model_call_error(
                    search_result,
                    default_component="embedding" if RETRIEVAL_BACKEND == "llamaindex" else "agent",
                )

            context = self._prepare_review_context(search_result or "未找到相关内容。")
            notes.append(
                {
                    "issue_id": issue_id,
                    "check_point": check_point,
                    "search_query": search_query,
                    "search_result": context,
                }
            )

        if not notes:
            return "无 anchors 为空的缺失类 issue，无补充检索结果。"
        return json.dumps(notes, ensure_ascii=False, indent=2)

    def _build_reflector_input(
        self,
        criterion_text: str,
        check_points: list[str],
        parsed_opinion: dict,
    ) -> str:
        """Give Reflector the current criterion plus the exact sub-agent output."""
        return json.dumps(
            {
                "criterion": criterion_text,
                "check_points": check_points,
                "subagent_output": parsed_opinion,
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _review_with_fallback(
        self,
        reflector: ReflectorAgent,
        *,
        cid: str,
        round_num: int,
        reflector_input: str,
        missing_text_review_notes: str,
    ) -> dict | None:
        """Return a reflection result, or preserve the valid review on model failure."""
        try:
            return await reflector.review(
                agent_output=reflector_input,
                missing_text_review_notes=missing_text_review_notes,
            )
        except (AgentOutputValidationError, ModelCallError) as exc:
            print(
                f"[Orchestrator] {cid}: reflection round {round_num} unavailable; "
                f"preserving the latest valid review: {exc}"
            )
            if self.logger:
                self.logger.log(
                    phase="Reflect",
                    sender="Reflector",
                    receiver=f"SubAgent_{cid}",
                    action=f"reflect_fallback({cid}, round={round_num})",
                    input_summary=reflector_input,
                    output_summary=f"ModelCallError: {exc}",
                    tokens=reflector.token_usage.get("total_tokens", 0),
                    duration=0,
                )
            return None

    async def execute_single_criterion(
            self,
            criterion: dict
    ) -> dict:
        """
        Review a single criterion:
        1. Retrieve relevant contract sections from the configured temporary index
        2. Create sub-agent with context
        3. Run reflector loop until PASS or max rounds
        """
        cid = criterion["id"]
        criterion_text = criterion["criterion"]
        check_points = criterion.get("check_points") or [criterion_text]
        check_points_text = "\n".join(f"- {cp}" for cp in check_points)
        start = time.time()
        evidence_tokens = 0

        route_override = None
        if criterion.get("route") and not criterion.get("fallback_llm_route"):
            route_override = json.dumps(criterion["route"], ensure_ascii=False)
        placement = criterion.get("placement") if isinstance(criterion.get("placement"), dict) else {}
        placement_target = placement.get("target", "inline")
        placement_note = placement.get("note", "")
        review_instruction = (criterion.get("review_instruction") or "").strip()

        # Step 1: Retrieve relevant contract sections
        context, evidence_tokens = await self._retrieve_context(
            cid,
            criterion_text,
            check_points_text, start,
            route_override=route_override,
        )

        # Step 2: Create sub-agent with retrieved context
        tools = self._filter_tools_by_name(
            await self._get_tools(),
            set(SUBAGENT_ALLOWED_TOOLS),
        )
        sub_agent = Agent(
            system_prompt=SUB_AGENT_BASE_PROMPT,
            name=f"SubAgent_{cid}",
            mcp_client=self.mcp_client,
            tools=tools,
            settings=self.settings,
        )

        task_prompt = (
            f"审查标准：{criterion_text}\n"
            f"检查要点：\n{check_points_text}\n\n"
            "执行约束：\n"
            "- 检索结果中的确定性计数和范围说明是已经执行完毕的代码结论，不是待核实信息。\n"
            "- 若审查标准以某词语缺失为触发条件，且确定性全文检索明确显示出现次数为0，"
            "必须输出 issues_found，不得输出 compliant。\n"
            "- 若审查标准要求在触发后‘直接写明’或‘如无则写入’指定提示，且检索结果证明触发条件成立，"
            "必须按该标准输出 issue，不得再以风险较低、已有相关实体条款或一般完善建议为由跳过。\n\n"
            f"以下是合同原文中与此标准相关的内容：\n\n{context}\n\n"
        )
        if review_instruction:
            task_prompt = (
                task_prompt
                + f"触发指令：{review_instruction}\n"
                + "触发条件成立时，issue_comment 必须逐字使用指令中的固定文字，不得改写。\n\n"
            )
        placement_directive = _PLACEMENT_DIRECTIVES.get(placement_target)
        if placement_directive:
            task_prompt += f"批注去向约束：{placement_directive}\n\n"
        parsed_opinion, sub_agent_opinion = await chat_until_valid_json(
            sub_agent,
            task_prompt,
            SubAgentOutput,
            SUB_AGENT_EXPECTED_JSON,
        )
        anchor_validation = self._validate_and_repair_anchors(parsed_opinion, context)

        if self.logger:
            self.logger.log(
                phase="Execute", sender=f"SubAgent_{cid}", receiver="LLM",
                action=f"review({cid})",
                input_summary=f"criterion + {len(context)} chars context",
                output_summary=sub_agent_opinion,
                tokens=sub_agent.token_usage.get("total_tokens", 0),
                duration=round(time.time() - start, 2)
            )

        # If sub-agent found no issues at all across all check points, short-circuit
        if parsed_opinion["status"] == "compliant":
            print(f"[Orchestrator] {cid}: All check points compliant, skipping reflection.")
            return {
                "criterion_id": cid,
                "criterion": criterion_text,
                "section": criterion.get("section", "其他"),
                "issues": parsed_opinion["issues"],
                "status": "COMPLIANT",
                "placement": placement or None,
                "applicability_reason": parsed_opinion.get("applicability_reason", ""),
                "tokens": sub_agent.token_usage.get("total_tokens", 0) + evidence_tokens,
                "error_message": None,
                "anchor_validation": anchor_validation,
            }

        # Step 3: Reflection loop. Each criterion needs its own reflector state;
        # sharing one ReflectorAgent across concurrent criteria mixes messages.
        reflector = ReflectorAgent(settings=self.settings)
        for round_num in range(MAX_REFLECTION_ROUNDS):
            missing_text_review_notes = await self._build_missing_text_review_notes(parsed_opinion)
            reflector_input = self._build_reflector_input(
                criterion_text,
                check_points,
                parsed_opinion,
            )
            review = await self._review_with_fallback(
                reflector,
                cid=cid,
                round_num=round_num + 1,
                reflector_input=reflector_input,
                missing_text_review_notes=missing_text_review_notes,
            )
            if review is None:
                break

            status = review["status"]
            if self.logger:
                self.logger.log(
                    phase="Reflect", sender="Reflector", receiver=f"SubAgent_{cid}",
                    action=f"reflect({cid}, round={round_num+1})",
                    input_summary=reflector_input,
                    output_summary=f"{status}: {review.get('feedback', '')}",
                    tokens=reflector.token_usage.get("total_tokens", 0),
                    duration=round(time.time() - start, 2)
                )
            if status == "PASS":
                print(f"[Orchestrator] {cid}: PASS after {round_num+1} round(s)")
                break
            else:
                print(f"[Orchestrator] {cid}: REJECT round {round_num+1}, refining...")
                parsed_opinion, sub_agent_opinion = await chat_until_valid_json(
                    sub_agent,
                    f"请根据以下质量审查反馈，补充和完善你的审查结果：\n\n{review.get('feedback', '')}",
                    SubAgentOutput,
                    SUB_AGENT_EXPECTED_JSON,
                )
                round_validation = self._validate_and_repair_anchors(parsed_opinion, context)
                for key, value in round_validation.items():
                    anchor_validation[key] += value
        else:
            print(f"[Orchestrator] {cid}: Max {MAX_REFLECTION_ROUNDS} rounds reached")

        total_tokens = evidence_tokens + sub_agent.token_usage.get("total_tokens", 0) + reflector.token_usage.get("total_tokens", 0)
        status_map = {
            "compliant": "COMPLIANT",
            "issues_found": "ISSUES_FOUND",
            "not_applicable": "NOT_APPLICABLE",
        }
        final_status = status_map[parsed_opinion["status"]]

        return {
            "criterion_id": cid,
            "criterion": criterion_text,
            "section": criterion.get("section", "其他"),
            "issues": parsed_opinion["issues"],
            "status": final_status,
            "placement": placement or None,
            "applicability_reason": parsed_opinion.get("applicability_reason", ""),
            "tokens": total_tokens,
            "error_message": None,
            "anchor_validation": anchor_validation,
        }

    async def _execute_criterion_with_recovery(
        self,
        criterion: dict,
        *,
        max_attempts: int,
    ) -> dict:
        cid = str(criterion["id"])
        if self.state_store is not None:
            existing_output = self.state_store.load_criterion_output(cid)
            if existing_output is not None:
                existing_output["recovered_from_state"] = True
                print(f"[Orchestrator] {cid}: reused criterion output from state.")
                return existing_output

        last_error: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            if self.state_store is not None:
                self.state_store.save_criterion_input(
                    criterion,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            try:
                result = await self.execute_single_criterion(criterion)
                if self.state_store is not None:
                    self.state_store.save_criterion_output(result, attempt=attempt)
                return result
            except Exception as exc:
                last_error = exc
                if self.state_store is not None:
                    self.state_store.save_criterion_error(
                        criterion,
                        exc,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                if attempt < max_attempts:
                    print(
                        f"[Orchestrator] {cid}: attempt {attempt}/{max_attempts} failed, retrying: {exc}"
                    )
                    continue
                print(f"[Orchestrator] {cid}: failed after {max_attempts} attempt(s): {exc}")
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Criterion {cid} did not produce a result.")

    async def execute_criteria(
            self,
            criteria_list: list[dict]
    ) -> list[dict]:
        """Execute criteria with concurrency control and auto-retry for failures."""
        # execute_tasks = [self.execute_single_criterion(criterion) for criterion in criteria_list]
        semaphore = asyncio.Semaphore(MAX_ORCHESTRATOR_CONCURRENCY)
        max_attempts = max(1, int(CRITERION_RETRY_MAX_ATTEMPTS))

        async def execute_with_limit(criterion: dict) -> dict:
            # async with semaphore:
            await semaphore.acquire()
            span = None
            try:
                async with get_current_trace().span(
                    f"criterion.{criterion.get('id')}",
                    run_type="criterion",
                    inputs=criterion_inputs(criterion),
                ) as span:
                    result = await self._execute_criterion_with_recovery(
                        criterion,
                        max_attempts=max_attempts,
                    )
                    if result.get("recovered_from_state"):
                        span.set_metadata({"recovered_from_state": True})
                    span.set_outputs(criterion_outputs(result))
                    return result
            except Exception as exc:
                if span is not None:
                    span.set_error(error_info(exc))
                raise
            finally: 
                semaphore.release()

        execute_tasks = [execute_with_limit(criterion) for criterion in criteria_list]
        results = await asyncio.gather(*execute_tasks, return_exceptions=True)

        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                if isinstance(result, ModelCallError):
                    raise result
                print(f"[Orchestrator] Error on criterion {criteria_list[i]['id']}: {result}")
                final_results.append(
                    {
                        "criterion_id": criteria_list[i]["id"],
                        "criterion": criteria_list[i]["criterion"],
                        "section": criteria_list[i].get("section", "其他"),
                        "issues": [],
                        "status": "ERROR",
                        "applicability_reason": "",
                        "tokens": 0,
                        "error_message": str(result),
                    }
                )
            else:
                final_results.append(result)

        total_tokens = sum(r.get("tokens", 0) for r in final_results)
        print(f"  Completed: {len(final_results)} reviews, Total tokens: {total_tokens:,}")

        failed_results = [result for result in final_results if result.get("status") == "ERROR"]
        if failed_results:
            failed_summary = "; ".join(
                f"{result.get('criterion_id')}: {result.get('error_message')}"
                for result in failed_results
            )
            raise RuntimeError(
                "Criterion execution failed after recovery attempts: "
                f"{failed_summary}"
            )

        return final_results
