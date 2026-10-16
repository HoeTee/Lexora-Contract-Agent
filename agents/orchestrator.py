"""
OrchestratorAgent - dispatches sub-agents per criterion with retrieval + reflection.

"""
import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable

from config import (
    CRITERION_RETRY_MAX_ATTEMPTS,
    MAX_REFLECTION_ROUNDS,
    MAX_ORCHESTRATOR_CONCURRENCY,
    RETRIEVAL_BACKEND,
    SUBAGENT_ALLOWED_TOOLS,
)
from agents.base_agent import Agent
from agents.json_utils import AgentOutputValidationError, agent_call_purpose, chat_until_valid_json
from agents.reflector import ReflectorAgent
from agents.prompts import get_prompt, render_prompt
from agents.schemas import SubAgentOutput
from endpoints.runtime.errors import ModelCallError, classify_model_call_error
from endpoints.review.review_state import ReviewStateStore
from endpoints.review.task_store import write_task_log_event
from loggers.trace_helpers import criterion_inputs, criterion_outputs, error_info
from loggers.trace_logger import get_current_trace
from tools.docxindex.indexing import estimate_tokens


_PLACEMENT_DIRECTIVES = {
    "summary": get_prompt("ORCHESTRATOR_PLACEMENT_SUMMARY"),
    "inline_at_title": get_prompt("ORCHESTRATOR_PLACEMENT_INLINE_AT_TITLE"),
    "tail": get_prompt("ORCHESTRATOR_PLACEMENT_TAIL"),
    "body_end": get_prompt("ORCHESTRATOR_PLACEMENT_BODY_END"),
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

    def decision_history(self) -> list[dict]:
        """Give a fresh reviewer the latest two decisions for every observed finding."""
        result = []
        for entry in self.observed.values():
            finding_id = entry["finding_id"]
            decisions = [
                {"round": item["round"], **decision}
                for item in self.history
                for decision in item["review"].get("finding_decisions", [])
                if decision["finding_id"] == finding_id
            ]
            if decisions:
                result.append({
                    "finding_id": finding_id,
                    "issue_id": entry["issue"].get("issue_id", ""),
                    "location": entry["anchor"],
                    "decisions": decisions[-2:],
                })
        return result

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
        all_resolved = all(
            self.decisions.get(finding_id, {}).get("decision") in {"confirm", "remove"}
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
            and not review.get("coverage_gaps")
            and not unknown_ids
            and not missing_ids
            and all_resolved
            and removed_missing
        )

    def apply_removals(self, opinion: dict) -> dict:
        """Remove rejected findings and reconcile their explicit audit dispositions."""
        normalized = deepcopy(opinion)
        entries = _finding_entries(opinion)
        removed = {
            key: entry for key, entry in entries.items()
            if self.decisions.get(self.observed[key]["finding_id"], {}).get("decision") == "remove"
        }
        if not removed:
            return normalized
        normalized["issues"] = [entry["issue"] for key, entry in entries.items() if key not in removed]
        retained_anchors = {
            (anchor.get("xml_anchor_type"), anchor.get("xml_anchor_id"))
            for issue in normalized["issues"] for anchor in issue.get("anchors", [])
            if anchor.get("role", "edit") == "edit"
        }
        removed_reasons = {
            (entry["anchor"]["xml_anchor_type"], entry["anchor"]["xml_anchor_id"]):
                self.decisions[self.observed[key]["finding_id"]]["reason"]
            for key, entry in removed.items() if entry["anchor"]
        }
        for decision in normalized.get("coverage_decisions", []):
            anchor = (decision.get("xml_anchor_type"), decision.get("xml_anchor_id"))
            if anchor in removed_reasons and anchor not in retained_anchors and decision["decision"] == "issue":
                decision.update(decision="compliant", reason=removed_reasons[anchor])
        # Pairwise comparison outcomes need an explicit revised classification;
        # removing a finding alone cannot establish matched/not_applicable.
        if not normalized["issues"]:
            normalized["status"] = "compliant"
        return normalized

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


SUB_AGENT_EXPECTED_JSON = get_prompt("SUB_AGENT_EXPECTED_JSON")


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
        task_prompt = getattr(sub_agent, "_review_task_prompt", None)
        if task_prompt is not None:
            base_context = sub_agent._review_base_context
            review_context = sub_agent._review_context
            if base_context:
                task_prompt = task_prompt.replace(base_context, review_context, 1)
            sub_agent.messages = [
                message for message in sub_agent.messages if message.get("role") == "system"
            ] + [{"role": "user", "content": task_prompt}]
        prompt = render_prompt(
            "ORCHESTRATOR_REVISION_CONTEXT", current_output=json.dumps(previous, ensure_ascii=False),
            instruction=prompt,
        )
        revised, _ = await chat_until_valid_json(
            sub_agent,
            prompt + get_prompt("ORCHESTRATOR_REVISION_IDENTITY"),
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
        search_query = render_prompt(
            "ORCHESTRATOR_SEARCH_QUERY",
            criterion_text=criterion_text, check_points_text=check_points_text,
        )
        retrieval_tokens = 0

        print(f"[Orchestrator] {cid}: Searching contract via temporary {RETRIEVAL_BACKEND} index...")
        search_payload = {
            "query": search_query,
            "api_events_path": self.api_events_path,
        }
        if RETRIEVAL_BACKEND == "docxindex":
            search_payload["return_metadata"] = True
            search_payload["criterion_id"] = cid
        if route_override:
            search_payload["route_override"] = route_override
        search_result = await self.mcp_client.call_tool(
            "contract_search",
            search_payload,
        )
        if RETRIEVAL_BACKEND == "docxindex" and isinstance(search_result, str):
            try:
                error_payload = json.loads(search_result).get("error")
            except (ValueError, AttributeError):
                error_payload = None
            if isinstance(error_payload, dict):
                error = ModelCallError(
                    "docxindex", str(error_payload.get("detail") or "DocxIndex retrieval failed"),
                    http_status=error_payload.get("http_status"),
                    error_type=error_payload.get("error_type"),
                )
                error.origin = "docxindex"
                error.failure_stage = error_payload.get("failure_stage")
                error.call_id = error_payload.get("call_id")
                error.call_path = error_payload.get("call_path")
                raise error
        if isinstance(search_result, str) and search_result.startswith("Error"):
            raise classify_model_call_error(
                search_result,
                default_component="embedding" if RETRIEVAL_BACKEND == "llamaindex" else "docxindex",
            )
        contexts = [search_result] if search_result else [get_prompt("ORCHESTRATOR_NO_EVIDENCE")]
        if RETRIEVAL_BACKEND == "docxindex" and isinstance(search_result, str):
            try:
                payload = json.loads(search_result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("contexts"), list):
                contexts = [str(item) for item in payload["contexts"] if str(item).strip()]
                if not contexts:
                    contexts = [get_prompt("ORCHESTRATOR_NO_EVIDENCE")]
        context = "\n\n".join(
            render_prompt("ORCHESTRATOR_EVIDENCE_BATCH", evidence=item) for item in contexts
        )
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
        normalized_context = context or get_prompt("ORCHESTRATOR_NO_EVIDENCE")
        normalized_context = re.sub(
            r"^##\s*检索结果\s*\d+(?:\s*\([^)]*\))?\s*$",
            "",
            normalized_context,
            flags=re.MULTILINE,
        )
        normalized_context = re.sub(r"\n{3,}", "\n\n", normalized_context).strip()
        guidance = get_prompt("ORCHESTRATOR_CONTEXT_GUIDANCE")
        anchor_guidance = get_prompt("ORCHESTRATOR_ANCHOR_GUIDANCE")
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
        retained_context = getattr(sub_agent, "_review_context", base_context)
        contexts = [retained_context]
        seen = {retained_context}
        for message in sub_agent.messages:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "").strip()
            if "xml_anchor_id:" not in content:
                continue
            prepared = self._prepare_review_context(content)
            if prepared not in seen and prepared not in retained_context:
                seen.add(prepared)
                contexts.append(prepared)
        return get_prompt("ORCHESTRATOR_SUPPLEMENTAL_EVIDENCE_SEPARATOR").join(contexts)

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
        """Repair provable model anchor mistakes while preserving invalid anchors for feedback."""
        evidence = self._retrieval_anchor_evidence(context)
        evidence_by_key = {
            (item["xml_anchor_type"], item["xml_anchor_id"]): item
            for item in evidence
        }
        stats = {"valid": 0, "repaired": 0, "invalid": 0}

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

                repaired_anchors.append(anchor)
                stats["invalid"] += 1
            issue["anchors"] = repaired_anchors
        return stats

    def _anchor_validation_errors(self, parsed_opinion: dict, context: str) -> list[str]:
        """Return objective anchor location and edit-range conflicts after repair."""
        evidence = self._retrieval_anchor_evidence(context)
        evidence_by_key = {
            (item["xml_anchor_type"], item["xml_anchor_id"]): item
            for item in evidence
        }
        errors: list[str] = []
        edit_ranges: list[tuple[str, str, str, int, int]] = []

        for issue in parsed_opinion.get("issues") or []:
            issue_id = str(issue.get("issue_id") or "")
            for anchor in issue.get("anchors") or []:
                anchor_type = str(anchor.get("xml_anchor_type") or "")
                anchor_id = str(anchor.get("xml_anchor_id") or "")
                quote = str(anchor.get("quoted_text") or "").strip()
                item = evidence_by_key.get((anchor_type, anchor_id))
                if item is None:
                    errors.append(
                        render_prompt(
                            "ORCHESTRATOR_ERROR_ANCHOR_MISSING", issue_id=issue_id,
                            anchor_type=anchor_type, anchor_id=anchor_id,
                        )
                    )
                    continue
                normalized_content = self._normalize_anchor_text(item["content"])
                normalized_quote = self._normalize_anchor_text(quote)
                starts = [
                    index
                    for index in range(len(normalized_content))
                    if normalized_content.startswith(normalized_quote, index)
                ] if normalized_quote else []
                if not starts:
                    errors.append(
                        render_prompt(
                            "ORCHESTRATOR_ERROR_QUOTE_NOT_IN_ANCHOR", issue_id=issue_id,
                            anchor_type=anchor_type, anchor_id=anchor_id, quote=repr(quote),
                        )
                    )
                    continue
                if len(starts) > 1:
                    errors.append(
                        render_prompt(
                            "ORCHESTRATOR_ERROR_QUOTE_AMBIGUOUS", issue_id=issue_id,
                            anchor_type=anchor_type, anchor_id=anchor_id, quote=repr(quote),
                        )
                    )
                    continue
                if anchor.get("role", "edit") == "edit":
                    start = starts[0]
                    edit_ranges.append(
                        (issue_id, anchor_type, anchor_id, start, start + len(normalized_quote))
                    )

        for index, left in enumerate(edit_ranges):
            for right in edit_ranges[index + 1:]:
                if left[1:3] != right[1:3]:
                    continue
                left_range = left[3:]
                right_range = right[3:]
                if left_range == right_range:
                    # Exact duplicates need semantic comparison and are handled downstream.
                    continue
                overlaps = left_range[0] < right_range[1] and right_range[0] < left_range[1]
                if overlaps:
                    errors.append(
                        render_prompt(
                            "ORCHESTRATOR_ERROR_EDIT_OVERLAP", left_id=left[0], right_id=right[0],
                            anchor_type=left[1], anchor_id=left[2],
                        )
                    )
        return errors

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
            errors.append(render_prompt("ORCHESTRATOR_ERROR_COVERAGE_MISSING", anchors=missing))
        if unknown:
            errors.append(render_prompt("ORCHESTRATOR_ERROR_COVERAGE_UNKNOWN", anchors=unknown))

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
            errors.append(render_prompt("ORCHESTRATOR_ERROR_COVERAGE_ISSUE_MISSING", anchors=missing_issues))
        issue_decision_keys = {
            (str(item.get("xml_anchor_type") or ""), str(item.get("xml_anchor_id") or ""))
            for item in decisions
            if item.get("decision") == "issue"
        }
        conflicting_issue_anchors = sorted(issue_anchor_keys - issue_decision_keys)
        if conflicting_issue_anchors:
            errors.append(
                render_prompt("ORCHESTRATOR_ERROR_COVERAGE_ISSUE_CONFLICT", anchors=conflicting_issue_anchors)
            )
        return errors

    @staticmethod
    def _comparison_audit_errors(
        parsed_opinion: dict,
        criterion: dict,
        context: str,
    ) -> list[str]:
        """Require every source-side candidate to be paired with objective evidence."""
        if not criterion.get("comparison_audit"):
            return []
        evidence_region = str(criterion.get("evidence_region") or "").strip()
        evidence = OrchestratorAgent._retrieval_anchor_evidence(context)
        expected_edits = {
            (item["xml_anchor_type"], item["xml_anchor_id"])
            for item in evidence
            if item.get("source_region") != evidence_region
        }
        allowed_evidence = {
            (item["xml_anchor_type"], item["xml_anchor_id"])
            for item in evidence
            if item.get("source_region") == evidence_region
        }
        decisions = parsed_opinion.get("comparison_decisions") or []
        actual_edits = {
            (str(item.get("edit_anchor_type") or ""), str(item.get("edit_anchor_id") or ""))
            for item in decisions
        }
        errors: list[str] = []
        missing = sorted(expected_edits - actual_edits)
        unknown = sorted(actual_edits - expected_edits)
        if missing:
            errors.append(render_prompt("ORCHESTRATOR_ERROR_COMPARISON_MISSING", anchors=missing))
        if unknown:
            errors.append(render_prompt("ORCHESTRATOR_ERROR_COMPARISON_UNKNOWN", anchors=unknown))

        issue_edits: set[tuple[str, str]] = set()
        issue_evidence: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for issue in parsed_opinion.get("issues") or []:
            edits = {
                (str(anchor.get("xml_anchor_type") or ""), str(anchor.get("xml_anchor_id") or ""))
                for anchor in issue.get("anchors") or []
                if anchor.get("role", "edit") == "edit"
            }
            evidences = {
                (str(anchor.get("xml_anchor_type") or ""), str(anchor.get("xml_anchor_id") or ""))
                for anchor in issue.get("anchors") or []
                if anchor.get("role") == "evidence"
            }
            issue_edits.update(edits)
            for edit in edits:
                issue_evidence.setdefault(edit, set()).update(evidences)

        for decision in decisions:
            edit = (
                str(decision.get("edit_anchor_type") or ""),
                str(decision.get("edit_anchor_id") or ""),
            )
            evidence_id = str(decision.get("evidence_anchor_id") or "").strip()
            evidence_key = (
                str(decision.get("evidence_anchor_type") or ""),
                evidence_id,
            )
            outcome = decision.get("decision")
            if evidence_id and evidence_key not in allowed_evidence:
                errors.append(render_prompt(
                    "ORCHESTRATOR_ERROR_COMPARISON_EVIDENCE_UNKNOWN", edit=edit, evidence=evidence_key,
                ))
            if outcome in {"mismatch", "not_found"} and edit not in issue_edits:
                errors.append(render_prompt("ORCHESTRATOR_ERROR_COMPARISON_ISSUE_MISSING", edit=edit, outcome=outcome))
            if outcome == "mismatch" and evidence_key not in issue_evidence.get(edit, set()):
                errors.append(render_prompt(
                    "ORCHESTRATOR_ERROR_COMPARISON_ISSUE_EVIDENCE_MISSING", edit=edit, evidence=evidence_key,
                ))
            if outcome in {"matched", "not_applicable"} and edit in issue_edits:
                errors.append(render_prompt("ORCHESTRATOR_ERROR_COMPARISON_ISSUE_CONFLICT", edit=edit, outcome=outcome))
        return errors

    @staticmethod
    def _policy_output_errors(
        parsed_opinion: dict,
        criterion: dict,
        context: str = "",
    ) -> list[str]:
        """Validate output invariants explicitly declared by a routing policy."""
        comparison_errors = OrchestratorAgent._comparison_audit_errors(
            parsed_opinion,
            criterion,
            context,
        )
        if criterion.get("comparison_audit"):
            return comparison_errors
        edit_region = str(criterion.get("edit_region") or "").strip()
        evidence_region = str(criterion.get("evidence_region") or "").strip()
        if not edit_region and not evidence_region:
            return comparison_errors
        retrieved_evidence = OrchestratorAgent._retrieval_anchor_evidence(context)
        allowed_edits = {
            (item["xml_anchor_type"], item["xml_anchor_id"])
            for item in retrieved_evidence
            if item.get("source_region") == edit_region
        }
        allowed_evidence = {
            (item["xml_anchor_type"], item["xml_anchor_id"])
            for item in retrieved_evidence
            if item.get("source_region") == evidence_region
        }
        errors = [*comparison_errors]
        for issue in parsed_opinion.get("issues") or []:
            roles = {anchor.get("role", "edit") for anchor in issue.get("anchors") or []}
            if not {"edit", "evidence"}.issubset(roles):
                errors.append(
                    render_prompt("ORCHESTRATOR_ERROR_POLICY_PAIRED_ANCHORS", issue_id=issue.get("issue_id"))
                )
                continue
            invalid_edits = [
                anchor.get("xml_anchor_id")
                for anchor in issue.get("anchors") or []
                if anchor.get("role", "edit") == "edit"
                and edit_region
                and (anchor.get("xml_anchor_type"), anchor.get("xml_anchor_id")) not in allowed_edits
            ]
            if invalid_edits:
                errors.append(
                    render_prompt(
                        "ORCHESTRATOR_ERROR_POLICY_EDIT_REGION", issue_id=issue.get("issue_id"),
                        region=edit_region, anchors=invalid_edits,
                    )
                )
            invalid_evidence = [
                anchor.get("xml_anchor_id")
                for anchor in issue.get("anchors") or []
                if anchor.get("role") == "evidence"
                and evidence_region
                and (anchor.get("xml_anchor_type"), anchor.get("xml_anchor_id")) not in allowed_evidence
            ]
            if invalid_evidence:
                errors.append(
                    render_prompt(
                        "ORCHESTRATOR_ERROR_POLICY_EVIDENCE_REGION", issue_id=issue.get("issue_id"),
                        region=evidence_region, anchors=invalid_evidence,
                    )
                )
        return errors

    async def _validate_and_revise_output(
        self,
        sub_agent: Agent,
        parsed_opinion: dict,
        raw_output: str,
        criterion: dict,
        base_context: str,
        *,
        coverage_audit: bool,
        anchor_validation: dict[str, int] | None = None,
        max_revisions: int = 2,
    ) -> tuple[dict, str, str, dict[str, int]]:
        """Run all deterministic output checks and request complete revisions on failure."""
        totals = anchor_validation if anchor_validation is not None else {}
        for revision in range(max_revisions + 1):
            # Only the criterion can enable audit modes, not unsolicited model fields.
            if not coverage_audit and "coverage_decisions" in parsed_opinion:
                parsed_opinion["coverage_decisions"] = []
            if not criterion.get("comparison_audit") and "comparison_decisions" in parsed_opinion:
                parsed_opinion["comparison_decisions"] = []
            review_context = self._review_evidence_context(base_context, sub_agent)
            sub_agent._review_context = review_context
            repair_stats = self._validate_and_repair_anchors(parsed_opinion, review_context)
            for key, value in repair_stats.items():
                totals[key] = totals.get(key, 0) + value

            grouped_errors: list[str] = []
            anchor_errors = self._anchor_validation_errors(parsed_opinion, review_context)
            grouped_errors.extend(
                render_prompt("ORCHESTRATOR_ERROR_GROUP_ANCHOR", error=error) for error in anchor_errors
            )
            if coverage_audit:
                coverage_errors = self._coverage_audit_errors(parsed_opinion, base_context)
                grouped_errors.extend(
                    render_prompt("ORCHESTRATOR_ERROR_GROUP_COVERAGE", error=error) for error in coverage_errors
                )
            policy_errors = self._policy_output_errors(parsed_opinion, criterion, review_context)
            grouped_errors.extend(
                render_prompt("ORCHESTRATOR_ERROR_GROUP_POLICY", error=error) for error in policy_errors
            )

            if not grouped_errors:
                return parsed_opinion, raw_output, review_context, totals
            if revision == max_revisions:
                raise AgentOutputValidationError(
                    "SubAgent output validation failed after deterministic repair: "
                    + "; ".join(grouped_errors)
                )

            previous_opinion = parsed_opinion
            stage = f"{getattr(sub_agent, 'call_stage', 'review-initial')}-output-repair{revision + 1}"
            with agent_call_purpose(sub_agent, stage, "; ".join(grouped_errors)):
                parsed_opinion, raw_output = await self._revise_subagent_output(
                    sub_agent,
                    render_prompt(
                        "ORCHESTRATOR_OUTPUT_REPAIR", errors="\n- ".join(grouped_errors),
                    ),
                    previous_opinion,
                )

        raise AssertionError("unreachable output validation state")

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
            search_query = render_prompt(
                "ORCHESTRATOR_MISSING_TEXT_QUERY",
                check_point=check_point, criterion_text=criterion_text,
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

            context = self._prepare_review_context(search_result or get_prompt("ORCHESTRATOR_NO_EVIDENCE"))
            notes.append(
                {
                    "issue_id": issue_id,
                    "check_point": check_point,
                    "search_query": search_query,
                    "search_result": context,
                }
            )

        if not notes:
            return get_prompt("ORCHESTRATOR_NO_MISSING_TEXT_NOTES")
        return json.dumps(notes, ensure_ascii=False, indent=2)

    def _build_reflector_input(
        self,
        criterion_text: str,
        check_points: list[str],
        parsed_opinion: dict,
        context: str = "",
        missing_findings: list | None = None,
        current_findings: list | None = None,
        finding_history: list | None = None,
        audit_mode: dict | None = None,
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
                "finding_history": finding_history or [],
                "audit_mode": audit_mode or {"coverage": False, "comparison": False},
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
    ) -> dict:
        """Return a reflection result and propagate model failures for criterion retry."""
        try:
            return await reflector.review(
                agent_output=reflector_input,
                missing_text_review_notes=missing_text_review_notes,
            )
        except (AgentOutputValidationError, ModelCallError) as exc:
            print(
                f"[Orchestrator] {cid}: reflection round {round_num} failed; "
                f"criterion failed: {exc}"
            )
            if self.logger:
                self.logger.log(
                    phase="Reflect",
                    sender="Reflector",
                    receiver=f"SubAgent_{cid}",
                    action=f"reflect_failed({cid}, round={round_num})",
                    input_summary=reflector_input,
                    output_summary=f"ModelCallError: {exc}",
                    tokens=reflector.token_usage.get("total_tokens", 0),
                    duration=0,
                )
            raise

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
        comparison_audit = bool(criterion.get("comparison_audit"))

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
            system_prompt=get_prompt("SUB_AGENT_BASE_PROMPT"),
            name=f"SubAgent_{cid}",
            criterion_id=cid,
            mcp_client=self.mcp_client,
            tools=tools,
            settings=self.settings,
        )

        sub_agent.call_stage = "review-initial"
        task_prompt = render_prompt(
            "ORCHESTRATOR_REVIEW_TASK", criterion_text=criterion_text,
            check_points_text=check_points_text, context=context,
        )
        if coverage_audit:
            task_prompt += get_prompt("ORCHESTRATOR_COVERAGE_AUDIT")
        if comparison_audit:
            task_prompt += get_prompt("ORCHESTRATOR_COMPARISON_AUDIT")
        if review_instruction:
            task_prompt += render_prompt(
                "ORCHESTRATOR_TRIGGER_INSTRUCTION", review_instruction=review_instruction,
            )
        placement_directive = _PLACEMENT_DIRECTIVES.get(placement_target)
        if placement_directive:
            task_prompt += render_prompt(
                "ORCHESTRATOR_PLACEMENT_INSTRUCTION", directive=placement_directive,
            )
        sub_agent._review_task_prompt = task_prompt
        sub_agent._review_base_context = context
        sub_agent._review_context = context
        parsed_opinion, sub_agent_opinion = await chat_until_valid_json(
            sub_agent,
            task_prompt,
            SubAgentOutput,
            SUB_AGENT_EXPECTED_JSON,
        )
        parsed_opinion, sub_agent_opinion, review_context, anchor_validation = (
            await self._validate_and_revise_output(
                sub_agent,
                parsed_opinion,
                sub_agent_opinion,
                criterion,
                context,
                coverage_audit=coverage_audit,
            )
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
                "comparison_decisions": parsed_opinion.get("comparison_decisions", []),
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
        quality_status = "incomplete"
        reflector_tokens = 0
        for round_num in range(MAX_REFLECTION_ROUNDS):
            if criterion.get("review_source") == "mandatory":
                missing_text_review_notes = get_prompt("ORCHESTRATOR_MANDATORY_EVIDENCE_NOTES")
            else:
                missing_text_review_notes = await self._build_missing_text_review_notes(parsed_opinion, cid)
            reflector = ReflectorAgent(criterion_id=cid, settings=self.settings)
            reflector.call_stage = f"reflection-round{round_num + 1}"
            reflector_input = self._build_reflector_input(
                criterion_text,
                check_points,
                parsed_opinion,
                context=review_context,
                missing_findings=ledger.missing(parsed_opinion),
                current_findings=ledger.current(parsed_opinion),
                finding_history=ledger.decision_history(),
                audit_mode={"coverage": coverage_audit, "comparison": comparison_audit},
            )
            review = await self._review_with_fallback(
                reflector,
                cid=cid,
                round_num=round_num + 1,
                reflector_input=reflector_input,
                missing_text_review_notes=missing_text_review_notes,
            )
            reflector_tokens += reflector.token_usage.get("total_tokens", 0)
            coverage_gaps = review.get("coverage_gaps") or []
            if coverage_gaps:
                gap_feedback = "\n".join(
                    render_prompt(
                        "ORCHESTRATOR_COVERAGE_GAP", anchor_id=gap.get("xml_anchor_id"),
                        quoted_text=gap.get("quoted_text"), reason=gap.get("reason"),
                    )
                    for gap in coverage_gaps
                )
                review["feedback"] = (
                    str(review.get("feedback") or "").strip()
                    + get_prompt("ORCHESTRATOR_COVERAGE_GAP_INSTRUCTION")
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
            passed = ledger.review(parsed_opinion, review, round_num + 1)
            if passed:
                resolved_opinion = ledger.apply_removals(parsed_opinion)
                errors = self._anchor_validation_errors(resolved_opinion, review_context)
                if coverage_audit:
                    errors += self._coverage_audit_errors(resolved_opinion, context)
                errors += self._policy_output_errors(resolved_opinion, criterion, review_context)
                if errors:
                    passed = False
                    ledger.history[-1]["validation_errors"] = errors
                    review["feedback"] += "\n" + "\n".join(errors)
                else:
                    parsed_opinion = resolved_opinion
            if passed:
                quality_status = "passed"
                print(f"[Orchestrator] {cid}: PASS after {round_num+1} round(s)")
                break
            else:
                if status == "PASS":
                    review["feedback"] += get_prompt("ORCHESTRATOR_UNRESOLVED_FINDINGS")
                if round_num + 1 == MAX_REFLECTION_ROUNDS:
                    break
                print(f"[Orchestrator] {cid}: REJECT round {round_num+1}, refining...")
                previous_opinion = parsed_opinion
                try:
                    with agent_call_purpose(sub_agent, f"review-reflection-revision{round_num + 1}", review.get("feedback")):
                        parsed_opinion, sub_agent_opinion = await self._revise_subagent_output(
                            sub_agent,
                            render_prompt(
                                "ORCHESTRATOR_REFLECTION_REVISION", feedback=review.get("feedback", ""),
                                decisions=json.dumps({
                                    "finding_decisions": review.get("finding_decisions", []),
                                    "current_findings": ledger.current(parsed_opinion),
                                    "missing_previous_findings": ledger.missing(parsed_opinion),
                                    "coverage_gaps": review.get("coverage_gaps", []),
                                    "finding_history": ledger.decision_history(),
                                }, ensure_ascii=False),
                            ),
                            previous_opinion,
                        )
                        parsed_opinion, sub_agent_opinion, review_context, anchor_validation = (
                            await self._validate_and_revise_output(
                                sub_agent,
                                parsed_opinion,
                                sub_agent_opinion,
                                criterion,
                                context,
                                coverage_audit=coverage_audit,
                                anchor_validation=anchor_validation,
                            )
                        )
                    ledger.observe(parsed_opinion)
                except (AgentOutputValidationError, ModelCallError) as exc:
                    print(
                        f"[Orchestrator] {cid}: refinement failed; "
                        f"criterion failed: {exc}"
                    )
                    if self.logger:
                        self.logger.log(
                            phase="Reflect",
                            sender=f"SubAgent_{cid}",
                            receiver="Orchestrator",
                            action=f"revision_failed({cid}, round={round_num+1})",
                            input_summary=review.get("feedback", ""),
                            output_summary=f"{type(exc).__name__}: {exc}",
                            tokens=sub_agent.token_usage.get("total_tokens", 0),
                            duration=round(time.time() - start, 2),
                        )
                    raise
        else:
            print(f"[Orchestrator] {cid}: Max {MAX_REFLECTION_ROUNDS} rounds reached")

        total_tokens = evidence_tokens + sub_agent.token_usage.get("total_tokens", 0) + reflector_tokens
        status_map = {
            "compliant": "COMPLIANT",
            "issues_found": "ISSUES_FOUND",
            "not_applicable": "NOT_APPLICABLE",
        }
        confirmed_issues = ledger.confirmed_issues(parsed_opinion)
        unresolved_issues = ledger.unresolved_issues(parsed_opinion)
        final_status = (
            "ISSUES_FOUND" if confirmed_issues or unresolved_issues
            else status_map["not_applicable" if parsed_opinion["status"] == "not_applicable" else "compliant"]
        )

        return {
            "criterion_id": cid,
            "criterion": criterion_text,
            "section": criterion.get("section", "其他"),
            "issues": confirmed_issues,
            "comparison_decisions": parsed_opinion.get("comparison_decisions", []),
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
                if self.state_store is not None:
                    write_task_log_event(
                        self.state_store.client_dir, self.state_store.task_id,
                        "criterion_failed", criterion_id=cid,
                        origin=getattr(exc, "origin", getattr(exc, "component", None)),
                        failure_stage=getattr(exc, "failure_stage", None),
                        http_status=getattr(exc, "http_status", None),
                        call_id=getattr(exc, "call_id", None),
                    )
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Criterion {cid} did not produce a result.")

    async def execute_criteria(
            self,
            criteria_list: list[dict],
            progress_callback: Callable[[dict], Awaitable[None] | None] | None = None,
    ) -> list[dict]:
        """Execute criteria with bounded retries, concurrency, and saved output recovery."""
        # execute_tasks = [self.execute_single_criterion(criterion) for criterion in criteria_list]
        semaphore = asyncio.Semaphore(MAX_ORCHESTRATOR_CONCURRENCY)
        max_attempts = CRITERION_RETRY_MAX_ATTEMPTS

        async def execute_with_limit(criterion: dict) -> dict:
            # async with semaphore:
            await semaphore.acquire()
            span = None
            try:
                await _emit_criterion_progress(
                    progress_callback,
                    {
                        "criterion_id": str(criterion["id"]),
                        "execution_status": "running",
                        "review_result": None,
                        "error": None,
                    },
                )
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
                    await _emit_criterion_progress(
                        progress_callback,
                        _criterion_progress_from_result(result),
                    )
                    return result
            except Exception as exc:
                if span is not None:
                    span.set_error(error_info(exc))
                await _emit_criterion_progress(
                    progress_callback,
                    {
                        "criterion_id": str(criterion["id"]),
                        "execution_status": "error",
                        "review_result": None,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                )
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


def _criterion_progress_from_result(result: dict) -> dict:
    status = str(result.get("status") or "").lower()
    execution_status = "error" if status == "error" else (
        "incomplete" if result.get("quality_status") == "incomplete" else "completed"
    )
    return {
        "criterion_id": str(result.get("criterion_id") or ""),
        "execution_status": execution_status,
        "review_result": None if execution_status == "error" else status,
        "error": (
            {
                "type": str(result.get("error_type") or "CriterionError"),
                "message": str(result.get("error_message") or "Criterion execution failed."),
            }
            if execution_status == "error"
            else None
        ),
    }


async def _emit_criterion_progress(
    callback: Callable[[dict], Awaitable[None] | None] | None,
    update: dict,
) -> None:
    if callback is None:
        return
    result = callback(update)
    if inspect.isawaitable(result):
        await result
