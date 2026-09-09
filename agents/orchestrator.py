"""
OrchestratorAgent - dispatches sub-agents per criterion with retrieval + reflection.

"""
import asyncio
from copy import deepcopy
import hashlib
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
from tools.docxindex.indexing import estimate_tokens


_PLACEMENT_DIRECTIVES = {
    "summary": "本标准发现的问题一律使用 anchors=[] 输出；系统会将 issue_comment 汇总进入文首审查总结，不得挂到具体条款。",
    "inline_at_title": "本标准发现的问题，批注锚点必须指向相关标题所在段落。",
    "tail": "本标准发现的问题，批注锚点必须指向合同末尾签署栏区域的段落。",
    "body_end": "本标准发现的问题使用 anchors=[] 输出；系统会将批注挂到正文结尾处。",
}

def _finding_entries(opinion: dict) -> dict[str, dict]:
    """Index findings by the SubAgent's stable issue id and edit ordinal."""
    entries: dict[str, dict] = {}
    for issue in opinion.get("issues", []):
        edits = [anchor for anchor in issue.get("anchors", []) if anchor.get("role", "edit") == "edit"]
        evidence = [anchor for anchor in issue.get("anchors", []) if anchor.get("role") == "evidence"]
        for edit_index, anchor in enumerate(edits or [None]):
            identity = [issue.get("issue_id"), edit_index]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
            local_issue = deepcopy(issue)
            if anchor:
                local_issue["anchors"] = [deepcopy(anchor), *deepcopy(evidence)]
                if len(edits) > 1:
                    local_issue["issue_id"] = f"{issue.get('issue_id')}.{edit_index + 1}"
            entries[key] = {"issue": local_issue, "anchor": deepcopy(anchor)}
    return entries


def _issue_edit_identity(issue: dict) -> tuple | None:
    """Return the stable source defect represented by one issue."""
    edits = [
        (
            anchor.get("xml_anchor_type", ""),
            anchor.get("xml_anchor_id", ""),
            "".join(str(anchor.get("quoted_text", "")).split()),
            anchor.get("defect_type", ""),
        )
        for anchor in issue.get("anchors", [])
        if anchor.get("role", "edit") == "edit"
    ]
    return tuple(edits) if edits else None


def _preserve_revision_issue_ids(previous: dict, revised: dict) -> dict:
    """Keep issue IDs stable when a revision still describes the same source defect."""
    previous_ids = {
        identity: issue.get("issue_id", "")
        for issue in previous.get("issues", [])
        if (identity := _issue_edit_identity(issue)) is not None
    }
    normalized = deepcopy(revised)
    issues = normalized.get("issues", [])

    # Reserve restored IDs first. A newly inserted finding may reuse an ordinal
    # that belongs to a later existing finding after the model renumbers the list.
    restored_ids: set[str] = set()
    unmatched_issues: list[dict] = []
    for issue in issues:
        identity = _issue_edit_identity(issue)
        if identity in previous_ids:
            restored_id = previous_ids[identity]
            issue["issue_id"] = restored_id
            restored_ids.add(restored_id)
        else:
            unmatched_issues.append(issue)

    used_ids = set(restored_ids)
    for issue in unmatched_issues:
        proposed_id = str(issue.get("issue_id") or "issue")
        candidate = proposed_id
        suffix = 1
        while candidate in used_ids:
            candidate = f"{proposed_id}.new{suffix}"
            suffix += 1
        issue["issue_id"] = candidate
        used_ids.add(candidate)

    return SubAgentOutput.model_validate(normalized).model_dump(mode="json")


class _FindingLedger:
    """Keep per-finding reflection decisions inside the orchestrator."""

    def __init__(self, opinion: dict):
        self.observed: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.history: list[dict] = []
        self.observe(opinion)

    def observe(self, opinion: dict) -> None:
        for key, entry in _finding_entries(opinion).items():
            if key not in self.observed:
                entry["finding_id"] = f"F{len(self.observed) + 1:03d}"
            else:
                entry["finding_id"] = self.observed[key]["finding_id"]
            self.observed[key] = entry

    def current(self, opinion: dict) -> list[dict]:
        return [
            {
                "finding_id": self.observed[key]["finding_id"],
                "issue_id": entry["issue"].get("issue_id", ""),
                "location": entry["anchor"],
                "description": entry["issue"].get("issue_comment", ""),
            }
            for key, entry in _finding_entries(opinion).items()
        ]

    def missing(self, opinion: dict) -> list[dict]:
        current_keys = set(_finding_entries(opinion))
        return [
            {
                "finding_id": entry["finding_id"],
                "location": entry["anchor"],
                "description": entry["issue"].get("issue_comment", ""),
            }
            for key, entry in self.observed.items()
            if key not in current_keys and self.decisions.get(entry["finding_id"], {}).get("decision") != "remove"
        ]

    def review(self, opinion: dict, review: dict, round_number: int) -> bool:
        current = self.current(opinion)
        missing = self.missing(opinion)
        required_ids = {item["finding_id"] for item in current + missing}
        decisions = {item["finding_id"]: item for item in review.get("finding_decisions", [])}
        unknown_ids = sorted(set(decisions) - required_ids)
        missing_ids = sorted(required_ids - set(decisions))
        self.decisions.update({
            finding_id: decision
            for finding_id, decision in decisions.items()
            if finding_id in required_ids
        })
        self.history.append(
            {
                "round": round_number,
                "opinion": deepcopy(opinion),
                "review": deepcopy(review),
                "unknown_finding_ids": unknown_ids,
                "undecided_finding_ids": missing_ids,
            }
        )
        current_ids = {item["finding_id"] for item in current}
        all_confirmed = all(
            self.decisions.get(finding_id, decisions.get(finding_id, {})).get("decision") == "confirm"
            for finding_id in current_ids
            if finding_id in decisions
        )
        removed_missing = all(
            self.decisions.get(item["finding_id"], decisions.get(item["finding_id"], {})).get("decision") == "remove"
            for item in missing
            if item["finding_id"] in decisions
        )
        return (
            review["status"] == "PASS"
            and not unknown_ids
            and not missing_ids
            and all_confirmed
            and removed_missing
        )

    def confirmed_issues(self, opinion: dict) -> list[dict]:
        confirmed: list[dict] = []
        current_entries = _finding_entries(opinion)
        for key, observed_entry in self.observed.items():
            finding_id = observed_entry["finding_id"]
            if self.decisions.get(finding_id, {}).get("decision") == "confirm":
                entry = current_entries.get(key, observed_entry)
                issue = deepcopy(entry["issue"])
                issue["finding_id"] = finding_id
                confirmed.append(issue)
        return confirmed

    def unresolved_issues(self, opinion: dict) -> list[dict]:
        current_entries = _finding_entries(opinion)
        unresolved: list[dict] = []
        for key, entry in self.observed.items():
            finding_id = entry["finding_id"]
            decision = self.decisions.get(finding_id, {}).get("decision")
            if decision in {"confirm", "remove"}:
                continue
            source = current_entries.get(key, entry)
            issue = deepcopy(source["issue"])
            issue["finding_id"] = finding_id
            issue["verification_status"] = "unresolved"
            unresolved.append(issue)
        return unresolved


SUB_AGENT_EXPECTED_JSON = """
{
  "status": "issues_found",
  "applicability_reason": "仅当 status 为 not_applicable 时填写不适用理由；其他状态可省略",
  "issues": [
    {
      "issue_id": "当前criterion_id.序号",
      "risk_level": "low",
      "issue_comment": "问题整体说明，600字以内，不得以长度限制遗漏真实缺陷",
      "anchors": [
        {
          "role": "edit",
          "defect_type": "missing_value",
          "xml_anchor_type": "paragraph",
          "xml_anchor_id": "必须复用检索结果中的 xml_anchor_id",
          "quoted_text": "逐字摘录的单一连续合同原文",
          "comment_text": "写入该处 Word 批注的修改意见，300字以内；evidence 角色必须为空"
        }
      ],
      "reasoning": "说明为什么构成实质风险，以及如何对应 check_point",
      "criterion": "当前审查标准原文",
      "check_point": "该 issue 对应的具体检查点"
    }
  ],
  "coverage_decisions": []
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

    @staticmethod
    async def _revise_subagent_output(
        sub_agent: Agent,
        prompt: str,
        previous: dict,
    ) -> tuple[dict, str]:
        """Request a revision and restore IDs for defects that remain at the same source."""
        revised, _ = await chat_until_valid_json(
            sub_agent,
            prompt
            + "\n修订同一原文缺陷时必须保留原 issue_id；只有新增的独立缺陷才能使用新 issue_id。",
            SubAgentOutput,
            SUB_AGENT_EXPECTED_JSON,
        )
        try:
            normalized = _preserve_revision_issue_ids(previous, revised)
        except ValueError as exc:
            raise AgentOutputValidationError(
                f"{sub_agent.name} revision identity normalization failed: {exc}"
            ) from exc
        return normalized, json.dumps(normalized, ensure_ascii=False)

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
        route_override: dict | None = None,
    ) -> tuple[str, int]:
        """Unified retrieval: returns (context_text, retrieval_tokens)."""
        search_query = f"{criterion_text}\n检查要点：{check_points_text}"
        retrieval_tokens = 0

        print(f"[Orchestrator] {cid}: Searching contract via temporary {RETRIEVAL_BACKEND} index...")
        search_payload = {
            "query": search_query,
            "api_events_path": self.api_events_path,
        }
        if RETRIEVAL_BACKEND == "docxindex":
            search_payload["return_metadata"] = True
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
        contexts = [search_result] if search_result else ["未找到相关内容。"]
        if RETRIEVAL_BACKEND == "docxindex" and isinstance(search_result, str):
            try:
                payload = json.loads(search_result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("contexts"), list):
                contexts = [str(item) for item in payload["contexts"] if str(item).strip()]
                if not contexts:
                    contexts = ["未找到相关内容。"]
        context = "\n\n".join(f"# 检索证据批次\n{item}" for item in contexts)
        retrieval_tokens = estimate_tokens(context)

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
            '注意：下面内容中的“相关度分数”“检索片段标题”等仅是检索系统包装信息，'
            '不是合同原始条款标题或所在位置。输出”所在位置”时只能填写合同原文中真实出现的条款、'
            '章节或段落位置；若合同原文未标明具体条款编号，请写”合同缺失审查要点要求书写的内容”，'
            '绝对不要照抄检索包装标题。'
        )
        anchor_guidance = (
            "每条检索证据会提供 xml_anchor_type 和 xml_anchor_id。"
            "输出 anchors 时必须从 quoted_text 所在的同一证据对象逐字符复制 xml_anchor_type/xml_anchor_id；"
            "禁止根据证据展示顺序、批次编号、正文顺序或段落内容自行构造 xml_anchor_id。"
            "输出前必须确认 quoted_text 是该 xml_anchor_id 对应合同文本中的单一连续片段。"
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
            r"(?:source_region:\s*([^\n]+)\s*\n)?"
            r"xml_anchor_type:\s*(paragraph|table)\s*\n"
            r"xml_anchor_id:\s*([^\n]+)\s*\n"
            r"内容：\s*\n(.*?)(?=\n\n---\n\n|\Z)",
            flags=re.DOTALL,
        )
        return [
            {
                "source_region": (match.group(1) or "").strip(),
                "xml_anchor_type": match.group(2).strip(),
                "xml_anchor_id": match.group(3).strip(),
                "content": match.group(4).strip(),
            }
            for match in pattern.finditer(context or "")
            if match.group(4).strip()
        ]

    def _review_evidence_context(self, base_context: str, sub_agent: Agent) -> str:
        """Include contract-search evidence gathered by the SubAgent itself."""
        contexts = [base_context]
        seen = {base_context}
        for message in sub_agent.messages:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "").strip()
            if "xml_anchor_id:" not in content:
                continue
            prepared = self._prepare_review_context(content)
            if prepared not in seen:
                seen.add(prepared)
                contexts.append(prepared)
        return "\n\n--- SubAgent 补充检索证据 ---\n\n".join(contexts)

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

                exact_candidates: list[tuple[dict[str, str], str]] = []
                containment_candidates: list[tuple[dict[str, str], str]] = []
                for item in evidence:
                    candidate_quote = self._normalized_span(item["content"], quote)
                    if candidate_quote:
                        exact_candidates.append((item, candidate_quote))
                        continue
                    normalized_content = self._normalize_anchor_text(item["content"])
                    if len(normalized_content) >= 4 and normalized_content in normalized_quote:
                        containment_candidates.append((item, item["content"]))

                # A full quote match is authoritative. Short evidence text may
                # also appear inside that quote (for example a party name), but
                # must not make an otherwise unique exact match ambiguous.
                candidates = exact_candidates or containment_candidates
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

    def _coverage_audit_errors(self, parsed_opinion: dict, context: str) -> list[str]:
        """Validate one semantic decision for every evidence anchor in an audited region."""
        evidence = self._retrieval_anchor_evidence(context)
        expected = {
            (item["xml_anchor_type"], item["xml_anchor_id"])
            for item in evidence
        }
        decisions = parsed_opinion.get("coverage_decisions") or []
        actual = {
            (str(item.get("xml_anchor_type") or ""), str(item.get("xml_anchor_id") or ""))
            for item in decisions
        }
        errors: list[str] = []
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            errors.append(f"coverage_decisions 缺少证据锚点：{missing}")
        if unknown:
            errors.append(f"coverage_decisions 包含不存在的证据锚点：{unknown}")

        issue_anchor_keys = {
            (str(anchor.get("xml_anchor_type") or ""), str(anchor.get("xml_anchor_id") or ""))
            for issue in parsed_opinion.get("issues") or []
            for anchor in issue.get("anchors") or []
            if anchor.get("role", "edit") == "edit"
        }
        missing_issues = sorted(
            (str(item.get("xml_anchor_type") or ""), str(item.get("xml_anchor_id") or ""))
            for item in decisions
            if item.get("decision") == "issue"
            and (str(item.get("xml_anchor_type") or ""), str(item.get("xml_anchor_id") or "")) not in issue_anchor_keys
        )
        if missing_issues:
            errors.append(f"标记为 issue 的证据没有对应 edit 批注：{missing_issues}")
        issue_decision_keys = {
            (str(item.get("xml_anchor_type") or ""), str(item.get("xml_anchor_id") or ""))
            for item in decisions
            if item.get("decision") == "issue"
        }
        conflicting_issue_anchors = sorted(issue_anchor_keys - issue_decision_keys)
        if conflicting_issue_anchors:
            errors.append(
                "edit 批注对应的 coverage_decisions 未标记为 issue："
                f"{conflicting_issue_anchors}"
            )
        return errors

    @staticmethod
    def _policy_output_errors(
        parsed_opinion: dict,
        criterion: dict,
        context: str = "",
    ) -> list[str]:
        """Validate output invariants explicitly declared by a routing policy."""
        evidence_region = str(criterion.get("evidence_region") or "").strip()
        if not evidence_region:
            return []
        allowed_evidence = {
            (item["xml_anchor_type"], item["xml_anchor_id"])
            for item in OrchestratorAgent._retrieval_anchor_evidence(context)
            if item.get("source_region") == evidence_region
        }
        errors = []
        for issue in parsed_opinion.get("issues") or []:
            roles = {anchor.get("role", "edit") for anchor in issue.get("anchors") or []}
            if not {"edit", "evidence"}.issubset(roles):
                errors.append(
                    f"issue {issue.get('issue_id')} 必须同时包含正文 edit 和附件 evidence；"
                    "只有单侧证据时删除该问题"
                )
                continue
            invalid_evidence = [
                anchor.get("xml_anchor_id")
                for anchor in issue.get("anchors") or []
                if anchor.get("role") == "evidence"
                and (anchor.get("xml_anchor_type"), anchor.get("xml_anchor_id")) not in allowed_evidence
            ]
            if invalid_evidence:
                errors.append(
                    f"issue {issue.get('issue_id')} 的 evidence 不来自 {evidence_region} region："
                    f"{invalid_evidence}"
                )
        return errors

    async def _build_missing_text_review_notes(
        self,
        parsed_opinion: dict,
        criterion_id: str,
    ) -> str:
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
                    "criterion_id": criterion_id,
                    "route_override": {
                        "steps": [
                            {
                                "method": "hybrid",
                                "terms": [],
                                "regions": [],
                                "all_titles": False,
                                "anchor_terms": [],
                                "coverage": "relevant",
                            }
                        ],
                        "fallback": False,
                    },
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
        context: str = "",
        missing_findings: list | None = None,
        current_findings: list | None = None,
    ) -> str:
        """Give Reflector the current criterion plus the exact sub-agent output."""
        return json.dumps(
            {
                "criterion": criterion_text,
                "check_points": check_points,
                "subagent_output": parsed_opinion,
                "retrieved_evidence": context,
                "missing_previous_findings": missing_findings or [],
                "current_findings": current_findings or [],
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
            route_override = criterion["route"]
        placement = criterion.get("placement") if isinstance(criterion.get("placement"), dict) else {}
        placement_target = placement.get("target", "inline")
        review_instruction = (criterion.get("review_instruction") or "").strip()
        coverage_audit = bool(criterion.get("coverage_audit"))

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
            "- 若当前标准只要求比较两处内容是否一致，任一侧为空白、模板变量或未填写时，不得在该一致性标准下输出 missing_value；"
            "只有两侧均为实际有效值且内容不一致时才能报告 inconsistency。\n\n"
            "- 输出前必须逐条检查字段长度：issue_comment 不得超过600个字符，anchor.comment_text 不得超过300个字符；"
            "需要说明多个事实时应保留核心缺陷和修改要求，不得用超长背景复述合同。\n\n"
            "- 检索结果若含‘检索候选判定理由’，表示 scan 已逐项发现该原文可能触发当前标准；"
            "必须逐条作出实质判断。原文确为未填写时必须分别输出 issue，只有能从该原文看出已实际填写或不属于标准范围时才可排除。\n\n"
            f"以下是合同原文中与此标准相关的内容：\n\n{context}\n\n"
        )
        if coverage_audit:
            task_prompt += (
                "完整覆盖审计：必须对上述每一个检索证据锚点输出一条 coverage_decisions。"
                "decision=issue 时必须同时生成锚定同一 xml_anchor_id 的 edit issue；"
                "decision=compliant/not_applicable 时在 reason 中说明与当前标准的关系。"
                "不得因为已经发现部分问题就跳过合同号、日期、联系方式等其他字段。\n\n"
            )
        if review_instruction:
            task_prompt = (
                task_prompt
                + f"触发指令：{review_instruction}\n"
                + "只有标准原文本身给出完整目标文字且触发条件成立时才逐字采用。省略号‘…’、‘照抄’或‘报错…’不是完整目标，不得照搬；应按真实缺陷提供具体修改意见。\n\n"
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
        review_context = self._review_evidence_context(context, sub_agent)
        anchor_validation = self._validate_and_repair_anchors(parsed_opinion, review_context)
        if coverage_audit:
            for _ in range(2):
                coverage_errors = self._coverage_audit_errors(parsed_opinion, context)
                if not coverage_errors:
                    break
                previous_opinion = parsed_opinion
                parsed_opinion, sub_agent_opinion = await self._revise_subagent_output(
                    sub_agent,
                    "完整覆盖审计未通过：\n- " + "\n- ".join(coverage_errors)
                    + "\n请返回完整修正后的JSON，不得遗漏原有正确问题。",
                    previous_opinion,
                )
                review_context = self._review_evidence_context(context, sub_agent)
                repair_stats = self._validate_and_repair_anchors(parsed_opinion, review_context)
                for key, value in repair_stats.items():
                    anchor_validation[key] = anchor_validation.get(key, 0) + value
            else:
                remaining_coverage_errors = self._coverage_audit_errors(parsed_opinion, context)
                if remaining_coverage_errors:
                    raise AgentOutputValidationError(
                        "SubAgent complete-region coverage audit failed: "
                        + "; ".join(remaining_coverage_errors)
                    )
        review_context = self._review_evidence_context(context, sub_agent)
        for _ in range(2):
            policy_errors = self._policy_output_errors(parsed_opinion, criterion, review_context)
            if not policy_errors:
                break
            previous_opinion = parsed_opinion
            parsed_opinion, sub_agent_opinion = await self._revise_subagent_output(
                sub_agent,
                "强制路由输出约束未通过：\n- " + "\n- ".join(policy_errors)
                + "\n请返回完整修正后的 JSON，不得保留无双侧证据的对照问题。",
                previous_opinion,
            )
            review_context = self._review_evidence_context(context, sub_agent)
            repair_stats = self._validate_and_repair_anchors(parsed_opinion, review_context)
            for key, value in repair_stats.items():
                anchor_validation[key] = anchor_validation.get(key, 0) + value
        else:
            remaining_policy_errors = self._policy_output_errors(
                parsed_opinion,
                criterion,
                review_context,
            )
            if remaining_policy_errors:
                raise AgentOutputValidationError(
                    "SubAgent routing-policy output validation failed: "
                    + "; ".join(remaining_policy_errors)
                )
        if coverage_audit:
            remaining_coverage_errors = self._coverage_audit_errors(parsed_opinion, context)
            if remaining_coverage_errors:
                raise AgentOutputValidationError(
                    "SubAgent complete-region coverage audit failed after output repair: "
                    + "; ".join(remaining_coverage_errors)
                )

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
        if parsed_opinion["status"] == "compliant" and not coverage_audit:
            print(f"[Orchestrator] {cid}: All check points compliant, skipping reflection.")
            return {
                "criterion_id": cid,
                "criterion": criterion_text,
                "section": criterion.get("section", "其他"),
                "issues": parsed_opinion["issues"],
                "status": "COMPLIANT",
                "quality_status": "not_required",
                "placement": placement or None,
                "review_instruction": review_instruction,
                "review_source": criterion.get("review_source", "uploaded"),
                "policy_id": criterion.get("policy_id", ""),
                "dedupe_priority": int(criterion.get("dedupe_priority") or 0),
                "dedupe_domain": str(criterion.get("dedupe_domain") or ""),
                "applicability_reason": parsed_opinion.get("applicability_reason", ""),
                "tokens": sub_agent.token_usage.get("total_tokens", 0) + evidence_tokens,
                "error_message": None,
                "anchor_validation": anchor_validation,
            }

        # Step 3: Reflection loop. Use a fresh reflector conversation per round;
        # otherwise complete retrieval evidence is duplicated in message history.
        ledger = _FindingLedger(parsed_opinion)
        quality_status = "pending_review"
        reflector_tokens = 0
        for round_num in range(MAX_REFLECTION_ROUNDS):
            if criterion.get("review_source") == "mandatory":
                missing_text_review_notes = "强制路由已提供当前标准的完整检索证据，不再重复执行缺失文本补充检索。"
            else:
                missing_text_review_notes = await self._build_missing_text_review_notes(parsed_opinion, cid)
            reflector = ReflectorAgent(settings=self.settings)
            reflector_input = self._build_reflector_input(
                criterion_text,
                check_points,
                parsed_opinion,
                context=review_context,
                missing_findings=ledger.missing(parsed_opinion),
                current_findings=ledger.current(parsed_opinion),
            )
            review = await self._review_with_fallback(
                reflector,
                cid=cid,
                round_num=round_num + 1,
                reflector_input=reflector_input,
                missing_text_review_notes=missing_text_review_notes,
            )
            reflector_tokens += reflector.token_usage.get("total_tokens", 0)
            if review is None:
                break

            coverage_gaps = review.get("coverage_gaps") or []
            if coverage_gaps:
                gap_feedback = "\n".join(
                    f"漏检 {gap.get('xml_anchor_id')}: {gap.get('quoted_text')}；{gap.get('reason')}"
                    for gap in coverage_gaps
                )
                review["feedback"] = (
                    str(review.get("feedback") or "").strip()
                    + "\n请补充以下覆盖缺口：\n"
                    + gap_feedback
                ).strip()
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
            if ledger.review(parsed_opinion, review, round_num + 1):
                quality_status = "passed"
                print(f"[Orchestrator] {cid}: PASS after {round_num+1} round(s)")
                break
            else:
                if status == "PASS":
                    review["feedback"] += "\n存在未逐条确认、未经说明删除或仍需修订的发现；请按 finding_id 修正。"
                if round_num + 1 == MAX_REFLECTION_ROUNDS:
                    break
                print(f"[Orchestrator] {cid}: REJECT round {round_num+1}, refining...")
                previous_opinion = parsed_opinion
                try:
                    parsed_opinion, sub_agent_opinion = await self._revise_subagent_output(
                        sub_agent,
                        f"请根据以下质量审查反馈，补充和完善你的审查结果：\n\n{review.get('feedback', '')}",
                        previous_opinion,
                    )
                    review_context = self._review_evidence_context(context, sub_agent)
                    repair_stats = self._validate_and_repair_anchors(parsed_opinion, review_context)
                    for key, value in repair_stats.items():
                        anchor_validation[key] = anchor_validation.get(key, 0) + value
                    if coverage_audit:
                        coverage_errors = self._coverage_audit_errors(parsed_opinion, context)
                        if coverage_errors:
                            previous_opinion = parsed_opinion
                            parsed_opinion, sub_agent_opinion = await self._revise_subagent_output(
                                sub_agent,
                                "完整覆盖审计未通过：\n- " + "\n- ".join(coverage_errors)
                                + "\n请返回完整修正后的JSON，不得遗漏原有正确问题。",
                                previous_opinion,
                            )
                            review_context = self._review_evidence_context(context, sub_agent)
                            repair_stats = self._validate_and_repair_anchors(parsed_opinion, review_context)
                            for key, value in repair_stats.items():
                                anchor_validation[key] = anchor_validation.get(key, 0) + value
                            remaining_errors = self._coverage_audit_errors(parsed_opinion, context)
                            if remaining_errors:
                                raise AgentOutputValidationError(
                                    "SubAgent complete-region coverage audit failed after reflection: "
                                    + "; ".join(remaining_errors)
                                )
                    policy_errors = self._policy_output_errors(parsed_opinion, criterion, review_context)
                    if policy_errors:
                        previous_opinion = parsed_opinion
                        parsed_opinion, sub_agent_opinion = await self._revise_subagent_output(
                            sub_agent,
                            "强制路由输出约束未通过：\n- " + "\n- ".join(policy_errors)
                            + "\n请删除无法提供正文 edit 与附件 evidence 双侧证据的对照问题。",
                            previous_opinion,
                        )
                        review_context = self._review_evidence_context(context, sub_agent)
                        repair_stats = self._validate_and_repair_anchors(parsed_opinion, review_context)
                        for key, value in repair_stats.items():
                            anchor_validation[key] = anchor_validation.get(key, 0) + value
                        remaining_policy_errors = self._policy_output_errors(
                            parsed_opinion,
                            criterion,
                            review_context,
                        )
                        if remaining_policy_errors:
                            raise AgentOutputValidationError(
                                "SubAgent routing-policy output validation failed after reflection: "
                                + "; ".join(remaining_policy_errors)
                            )
                    if coverage_audit:
                        remaining_coverage_errors = self._coverage_audit_errors(parsed_opinion, context)
                        if remaining_coverage_errors:
                            raise AgentOutputValidationError(
                                "SubAgent complete-region coverage audit failed after reflection repair: "
                                + "; ".join(remaining_coverage_errors)
                            )
                    ledger.observe(parsed_opinion)
                except (AgentOutputValidationError, ModelCallError) as exc:
                    parsed_opinion = previous_opinion
                    quality_status = "pending_review"
                    print(
                        f"[Orchestrator] {cid}: refinement unavailable; "
                        f"preserving the latest schema-valid review: {exc}"
                    )
                    if self.logger:
                        self.logger.log(
                            phase="Reflect",
                            sender=f"SubAgent_{cid}",
                            receiver="Orchestrator",
                            action=f"revision_fallback({cid}, round={round_num+1})",
                            input_summary=review.get("feedback", ""),
                            output_summary=f"{type(exc).__name__}: {exc}",
                            tokens=sub_agent.token_usage.get("total_tokens", 0),
                            duration=round(time.time() - start, 2),
                        )
                    break
        else:
            print(f"[Orchestrator] {cid}: Max {MAX_REFLECTION_ROUNDS} rounds reached")

        total_tokens = evidence_tokens + sub_agent.token_usage.get("total_tokens", 0) + reflector_tokens
        status_map = {
            "compliant": "COMPLIANT",
            "issues_found": "ISSUES_FOUND",
            "not_applicable": "NOT_APPLICABLE",
        }
        final_status = status_map[parsed_opinion["status"]]

        confirmed_issues = ledger.confirmed_issues(parsed_opinion)
        unresolved_issues = ledger.unresolved_issues(parsed_opinion)

        return {
            "criterion_id": cid,
            "criterion": criterion_text,
            "section": criterion.get("section", "其他"),
            "issues": confirmed_issues,
            "quality_status": quality_status,
            "quality_history": ledger.history,
            "finding_decisions": ledger.decisions,
            "unresolved_issues": unresolved_issues,
            "status": final_status,
            "placement": placement or None,
            "review_instruction": review_instruction,
            "review_source": criterion.get("review_source", "uploaded"),
            "policy_id": criterion.get("policy_id", ""),
            "dedupe_priority": int(criterion.get("dedupe_priority") or 0),
            "dedupe_domain": str(criterion.get("dedupe_domain") or ""),
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
                criterion = criteria_list[i]
                cid = str(criterion["id"])
                print(f"[Orchestrator] Error on criterion {cid}: {result}")
                final_results.append(
                    {
                        "criterion_id": cid,
                        "criterion": criterion["criterion"],
                        "section": criterion.get("section", "其他"),
                        "issues": [],
                        "status": "ERROR",
                        "quality_status": "error",
                        "review_source": criterion.get("review_source", "uploaded"),
                        "policy_id": criterion.get("policy_id", ""),
                        "applicability_reason": "",
                        "tokens": 0,
                        "error_message": str(result),
                        "error_type": type(result).__name__,
                    }
                )
            else:
                final_results.append(result)

        if final_results and all(result.get("status") == "ERROR" for result in final_results):
            raise RuntimeError("All criteria failed; no valid review result is available for reporting.")

        total_tokens = sum(r.get("tokens", 0) for r in final_results)
        print(f"  Completed: {len(final_results)} reviews, Total tokens: {total_tokens:,}")

        return final_results
