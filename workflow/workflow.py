"""
ContractReviewWorkflow orchestrates one annotated-DOCX contract review run.

Phases:
  1. Ingest criteria + contract to markdown
  2. Build the configured temporary contract index
  3. Planner extracts review criteria
  4. Orchestrator executes criterion reviews with retrieval + reflection
  5. Summarizer creates a compact summary comment
  6. Generate the annotated original-contract DOCX
"""
import inspect
import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from config import (
    LOGGING_ENABLED,
    MCP_SERVER_TARGET,
    RETRIEVAL_BACKEND,
)
from loggers.workflow_logger import WorkflowLogger, save_review_outputs_json
from loggers.trace_logger import TraceLogger, reset_current_trace, set_current_trace
from loggers.call_log import set_call_context, reset_call_context
from agents.base_agent import Settings
from agents.planner import PlannerAgent, assemble_tasks, split_criteria_from_docx
from agents.orchestrator import OrchestratorAgent, review_progress_from_result
from agents.summarizer import SummarizerAgent, apply_summary_duplicate_groups
from mcp_service.client.client import MinimalMCPClient
from endpoints.runtime.errors import classify_model_call_error
from endpoints.review.review_state import ReviewStateStore
from endpoints.review.task_store import summarize_criteria
from routing import load_mandatory_tasks, mandatory_source_manifest
from routing.schemas import ReviewExecutionPlan
from tools.document.reporting.docx_report import deduplicate_review_results_by_range


def _normalize_annotation_text(value: str) -> str:
    return "".join(str(value or "").split()).strip("，。：；、“”\"'")


def _public_criteria(criteria_list: list[dict], results: list[dict]) -> list[dict[str, Any]]:
    by_id = {str(result.get("criterion_id")): result for result in results}
    entries: list[dict[str, Any]] = []
    for criterion in criteria_list:
        criterion_id = str(criterion["id"])
        result = by_id.get(criterion_id)
        if result is None:
            progress = {
                "execution_status": "not_started",
                "review_result": None,
                "error": None,
            }
        else:
            progress = review_progress_from_result(result)
        entries.append(
            {
                "id": criterion_id,
                "criterion": str(criterion.get("criterion") or ""),
                "execution_status": progress["execution_status"],
                "review_result": progress["review_result"],
                "error": progress["error"],
            }
        )
    return entries


def _confirmed_review_instruction(result: dict) -> str | None:
    """Return a fixed summary instruction only when a confirmed issue authorizes it."""
    placement = result.get("placement") or {}
    if placement.get("target", "inline") != "summary":
        return None
    if not result.get("issues"):
        return None
    instruction = str(result.get("review_instruction") or "").strip()
    return instruction or None


def _prepare_inline_results(results: list[dict]) -> tuple[list[dict], list[str]]:
    """Select confirmed edit anchors and explicit opening instructions for reporting."""
    inline_results: list[dict] = []
    opening_notices: list[str] = []
    seen_annotations: set[tuple[str, str, str, str]] = set()

    for result in results:
        placement = result.get("placement") or {}
        placement_target = placement.get("target", "inline")
        if placement_target == "summary":
            review_instruction = _confirmed_review_instruction(result)
            if review_instruction and review_instruction not in opening_notices:
                opening_notices.append(review_instruction)
            continue

        rendered_issues: list[dict] = []
        for issue in result.get("issues", []):
            edit_anchors: list[dict] = []
            for anchor in issue.get("anchors") or []:
                if anchor.get("role", "edit") != "edit":
                    continue
                if not all(str(anchor.get(field) or "").strip() for field in ("xml_anchor_type", "xml_anchor_id", "quoted_text")):
                    continue
                comment = str(anchor.get("comment_text") or issue.get("issue_comment") or "").strip()
                normalized_action = (
                    "missing_value"
                    if anchor.get("defect_type") == "missing_value"
                    else _normalize_annotation_text(comment)
                )
                key = (
                    str(anchor.get("xml_anchor_type")),
                    str(anchor.get("xml_anchor_id")),
                    _normalize_annotation_text(anchor.get("quoted_text", "")),
                    normalized_action,
                )
                if key in seen_annotations:
                    continue
                seen_annotations.add(key)
                local_anchor = deepcopy(anchor)
                local_anchor["comment_text"] = comment
                edit_anchors.append(local_anchor)

            if edit_anchors or placement_target == "body_end":
                local_issue = deepcopy(issue)
                local_issue["anchors"] = edit_anchors
                rendered_issues.append(local_issue)

        if rendered_issues:
            local_result = deepcopy(result)
            local_result["issues"] = rendered_issues
            inline_results.append(local_result)

    return inline_results, opening_notices


def _incomplete_review_notices(results: list[dict]) -> list[str]:
    """Disclose missing conclusions while retaining any confirmed partial issues."""
    notices: list[str] = []
    for result in results:
        if (
            result.get("status") != "ERROR"
            and result.get("quality_status") != "incomplete"
        ):
            continue
        criterion = str(result.get("criterion") or "").strip()
        section = str(result.get("section") or "").strip()
        label = f"{section}：{criterion}" if section and criterion else criterion or section
        if label:
            detail = (
                "已确认意见已列出，其余内容尚未完成审查。"
                if result.get("issues")
                else "审查结果不包含该项结论。"
            )
            notices.append(f"{label}（本次未完成，{detail}）")
    return notices


class ContractReviewWorkflow:
    """High-level coordinator for one contract review run.

    This module is intentionally the workflow glue layer. It owns phase order,
    progress emission, logging, and final result assembly, while the detailed
    work for parsing, retrieval, review, and summarization stays in helper
    agents and MCP tools.
    """

    def __init__(
        self,
        server_target: str = MCP_SERVER_TARGET,
        conversation_log_dir: str | None = None,
        mcp_log_file: str | None = None,
        api_events_path: str | None = None,
        trace_path: str | None = None,
        review_outputs_path: str | None = None,
        annotation_events_path: str | None = None,
        state_store: ReviewStateStore | None = None,
        index_artifacts_path: str | None = None,
        settings: Settings | None = None,
        mcp_env: dict[str, str] | None = None,
    ):
        self.client = MinimalMCPClient(server_target, log_file=mcp_log_file, env=mcp_env)
        self.logger = WorkflowLogger(events_path=api_events_path)
        self.trace = TraceLogger(trace_path, metadata={"component": "workflow"})
        self.conversation_log_dir = conversation_log_dir
        self.api_events_path = api_events_path
        self.review_outputs_path = review_outputs_path
        self.annotation_events_path = annotation_events_path
        self.annotation_report_summary: dict[str, Any] = {}
        self.state_store = state_store
        self.index_artifacts_path = index_artifacts_path
        self.settings = settings or Settings()

    async def run(
        self,
        contract_path: str,
        criteria_path: str | None = None,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        output_dir: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute the full review workflow.
        Returns: structured review data for the API layer.
        """
        if not output_dir and not output_path:
            raise ValueError(
                "output_dir or output_path is required for report generation."
            )

        workflow_start = time.time()
        print("=" * 60)
        print("Contract Review Workflow Started")
        print("=" * 60)
        print(
            "This is an over-simplified workflow without web search or institutional RAG, "
            f"with retrieval mode being {RETRIEVAL_BACKEND}."
        )
        trace_token = set_current_trace(self.trace)
        call_token = set_call_context(self.api_events_path, stage="agent")
        # Initialize MCP client
        mcp_cleaned = False
        start = time.time()
        try:
            async with self.trace.span(
                "workflow.run",
                run_type="workflow",
                inputs={
                    "contract_path": contract_path,
                    "criteria_path": criteria_path,
                    "output_path": output_path,
                    "output_dir": output_dir,
                },
            ) as workflow_span:
                async with self.trace.span("mcp.connect", run_type="mcp"):
                    await self.client.connect()
                self.logger.log(
                    phase="MCP",
                    sender="Workflow",
                    receiver="MCP",
                    action="mcp_connect",
                    duration=round(time.time() - start, 2),
                )

                # Phase 1: Ingest files
                await self._emit_progress(progress_callback, "ingesting", "Parsing review input files")
                async with self.trace.span("phase.ingest", run_type="phase"):
                    criteria_md, contract_md = await self._phase_ingest(contract_path, criteria_path)

                # Phase 2: Build index
                await self._emit_progress(
                    progress_callback,
                    "building_index",
                    f"Building temporary {RETRIEVAL_BACKEND} contract index",
                )
                async with self.trace.span("phase.build_index", run_type="phase"):
                    index_metadata = await self._phase_build_index(contract_path)

                # Phase 3: Plan tasks
                criteria_file = Path(criteria_path) if criteria_path else None
                self.criteria_source = {
                    "source": "uploaded" if criteria_file else "mandatory",
                    "filename": criteria_file.name if criteria_file else None,
                    "sha256": hashlib.sha256(criteria_file.read_bytes()).hexdigest() if criteria_file else None,
                    "mandatory": mandatory_source_manifest(),
                }
                await self._emit_progress(progress_callback, "planning", "Extracting review criteria")
                async with self.trace.span("phase.plan", run_type="phase", inputs={"criteria_chars": len(criteria_md)}) as span:
                    criteria_list = await self._phase_plan(criteria_md, criteria_path)
                    self._save_plan_state(criteria_list)
                    span.set_outputs({"criteria_count": len(criteria_list)})

                # Phase 4: Execute + Reflect
                print(f"\n  Search mode: {RETRIEVAL_BACKEND}")
                await self._emit_progress(
                    progress_callback,
                    "reviewing",
                    f"Reviewing {len(criteria_list)} criteria with the agent workflow",
                    criteria=criteria_list,
                )

                async with self.trace.span("phase.execute", run_type="phase", inputs={"criteria_count": len(criteria_list)}) as span:
                    results = await self._phase_execute(criteria_list, progress_callback)
                    results, deduplication = deduplicate_review_results_by_range(contract_path, results)
                    span.set_outputs(
                        {
                            "result_count": len(results),
                            "error_count": sum(1 for result in results if result.get("status") == "ERROR"),
                            "deduplication": deduplication,
                        }
                    )

                public_criteria = _public_criteria(criteria_list, results)
                quality = summarize_criteria(public_criteria)

                # Phase 5: Summarize
                await self._emit_progress(progress_callback, "summarizing", "Creating summary comment")
                async with self.trace.span("phase.summarize", run_type="phase", inputs={"result_count": len(results)}) as span:
                    raw_issue_count = sum(len(result.get("issues", [])) for result in results)
                    summary_payload = await self._phase_summarize(results)
                    results, semantic_deduplication = apply_summary_duplicate_groups(
                        results,
                        summary_payload.pop("duplicate_groups", []),
                    )
                    final_review = summary_payload
                    presentation_results, opening_notices = _prepare_inline_results(results)
                    final_review["opening_notices"] = opening_notices
                    final_review["incomplete_reviews"] = _incomplete_review_notices(results)
                    review_outputs_path = self._save_review_outputs(results)
                    span.set_outputs(
                        {
                            "summary_section_count": len(final_review),
                            "raw_issue_count": raw_issue_count,
                            "confirmed_issue_count": sum(
                                len(result.get("issues", [])) for result in results
                            ),
                            "semantic_deduplication": semantic_deduplication,
                            "presentation_issue_count": sum(
                                len(result.get("issues", [])) for result in presentation_results
                            ),
                            "presentation_anchor_count": sum(
                                len(issue.get("anchors", []))
                                for result in presentation_results
                                for issue in result.get("issues", [])
                            ),
                            "opening_notices": opening_notices,
                            "incomplete_reviews": final_review["incomplete_reviews"],
                        }
                    )

                token_stats = self._collect_token_stats(results)
                total_tokens = token_stats["total"]
                elapsed = round(time.time() - workflow_start, 1)

                # Phase 6: Generate annotated DOCX only
                await self._emit_progress(progress_callback, "generating_docx", "Generating annotated DOCX")
                async with self.trace.span("phase.generate_docx", run_type="phase") as span:
                    annotated_docx_path = await self._phase_generate_annotated_docx(
                        contract_path=contract_path,
                        results=presentation_results,
                        summary_sections=final_review,
                        output_dir=output_dir,
                        output_path=output_path,
                        region_boundaries=index_metadata.get("region_boundaries") or {},
                    )
                    span.set_outputs({
                        "annotated_docx_path": annotated_docx_path,
                        **self.annotation_report_summary,
                    })

                start = time.time()
                async with self.trace.span("mcp.cleanup", run_type="mcp"):
                    await self.client.cleanup()
                mcp_cleaned = True
                self.logger.log(
                    phase="MCP",
                    sender="Workflow",
                    receiver="MCP",
                    action="mcp_cleanup",
                    duration=round(time.time() - start, 2),
                )

                elapsed = round(time.time() - workflow_start, 1)
                workflow_span.set_outputs(
                    {
                        "report_docx": annotated_docx_path,
                        "criteria_count": len(results),
                        "quality": quality,
                        "criteria": public_criteria,
                        "issue_count": sum(len(result.get("issues", [])) for result in results),
                        "error_count": sum(1 for result in results if result.get("status") == "ERROR"),
                        "elapsed_seconds": elapsed,
                        "phase_durations": self.logger.summarize_phase_durations(),
                        "logged_duration_total_seconds": self.logger.total_logged_duration(),
                        "total_tokens": total_tokens,
                        "token_stats": token_stats,
                        "review_outputs_log": review_outputs_path,
                        "annotation_events_log": self.annotation_events_path,
                        "state_dir": str(self.state_store.root_dir) if self.state_store else None,
                    }
                )
                self._print_completion_summary(
                    elapsed,
                    token_stats,
                    annotated_docx_path,
                    review_outputs_path,
                )

                await self._emit_progress(progress_callback, "completed", "Review completed")

                return {
                    "quality": quality,
                    "criteria": public_criteria,
                    "report_docx": annotated_docx_path,
                    "criteria_count": len(results),
                    "issue_count": sum(len(result.get("issues", [])) for result in results),
                    "total_tokens": total_tokens,
                    "elapsed_seconds": elapsed,
                    "phase_durations": self.logger.summarize_phase_durations(),
                    "annotation_stats": self.annotation_report_summary,
                    "retrieval_mode": RETRIEVAL_BACKEND,
                    "review_outputs_log": review_outputs_path,
                    "annotation_events_log": self.annotation_events_path,
                    "state_dir": str(self.state_store.root_dir) if self.state_store else None,
                }

        finally:
            try:
                if not mcp_cleaned:
                    start = time.time()
                    async with self.trace.span("mcp.cleanup", run_type="mcp"):
                        await self.client.cleanup()
                    self.logger.log(
                        phase="MCP",
                        sender="Workflow",
                        receiver="MCP",
                        action="mcp_cleanup",
                        duration=round(time.time() - start, 2),
                    )
            finally:
                reset_current_trace(trace_token)
                reset_call_context(call_token)

    # ==================== Phases ====================

    async def _emit_progress(
        self,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
        stage: str,
        message: str,
        **details: Any,
    ) -> None:
        """Forward a lightweight stage update to the optional UI callback."""
        if progress_callback is None:
            return

        update = {"stage": stage, "message": message, **details}
        maybe_awaitable = progress_callback(update)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    async def _phase_ingest(
            self,
            contract_path: str,
            criteria_path: str | None,
    ) -> tuple[str, str]:
        """Phase 1: ingest the contract and an optional uploaded criteria DOCX."""
        print("\n[Phase 1] Ingesting files...")
        criteria_md = (
            await self._ingest_markdown_file(criteria_path, "criteria")
            if criteria_path
            else ""
        )
        contract_md = await self._ingest_markdown_file(contract_path, "contract")

        print(f"  Uploaded criteria: {len(criteria_md)} chars")
        print(f"  Contract: {len(contract_md)} chars")
        return criteria_md, contract_md

    async def _ingest_markdown_file(
            self,
            file_path: str,
            input_label: str
    ) -> str:
        """Run the shared ingestion tool and strip its response wrapper.

        The MCP tool returns:
            File 'name' ingested. Content:
            <markdown body>
        Downstream phases only need the markdown body, so this helper keeps the
        call, logging, and wrapper stripping in one place.
        """
        start = time.time()
        tool_result = await self.client.call_tool(
            "ingest_file", {"file_path": file_path}
        )
        self.logger.log(
            phase="Ingestion", sender="Workflow", receiver="MCP:ingest_file",
            action=f"ingest_file({input_label})",
            input_summary=os.path.basename(file_path),
            output_summary=f"{len(tool_result)} chars",
            duration=round(time.time() - start, 2)
        )
        return tool_result.split("\n\n", 1)[-1] if "\n\n" in tool_result else tool_result

    async def _phase_build_index(
            self,
            contract_path: str
    ) -> dict[str, Any]:
        """Phase 2: Build the configured temporary contract retrieval index."""
        print(f"\n[Phase 2] Building temporary {RETRIEVAL_BACKEND} contract index...")
        start = time.time()

        result = await self.client.call_tool(
            "contract_build_index",
            {
                "docx_path": contract_path,
                "api_events_path": self.api_events_path,
                "artifact_dir": self.index_artifacts_path,
            },
        )

        self.logger.log(
            phase="Index Building", sender="Workflow", receiver="MCP:contract_build_index",
            action="contract_build_index",
            input_summary=os.path.basename(contract_path),
            output_summary=result,
            duration=round(time.time() - start, 2)
        )

        try:
            parsed = json.loads(result)
        except (TypeError, json.JSONDecodeError) as exc:
            response_preview = str(result or "").strip()[:500] or "<empty response>"
            raise RuntimeError(
                f"{RETRIEVAL_BACKEND} index build returned an invalid MCP response: "
                f"{response_preview}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"{RETRIEVAL_BACKEND} index build returned {type(parsed).__name__}; expected an object"
            )
        if parsed.get("error"):
            error = parsed["error"]
            if isinstance(error, dict):
                detail = str(error.get("detail") or error.get("message") or error)
                http_status = error.get("http_status")
                error_type = error.get("error_type")
            else:
                detail = str(error)
                http_status = None
                error_type = None
            raise classify_model_call_error(
                f"{RETRIEVAL_BACKEND} index build failed: {detail}",
                default_component="embedding" if RETRIEVAL_BACKEND == "llamaindex" else "agent",
                http_status=http_status,
                error_type=error_type,
            )

        print(f"  Temporary index built in {round(time.time()-start,1)}s: {result[:200]}")
        return parsed

    async def _phase_plan(
            self,
            criteria_md: str,
            criteria_path: str | None,
    ) -> list[dict]:
        """Phase 3: split criteria deterministically, then extract per-criterion strategies."""
        print("\n[Phase 3] Planning tasks...")
        start = time.time()

        cached_plan = self._load_plan_state()
        if cached_plan is not None:
            criteria_list = cached_plan
            self._planner_tokens = 0
            self.logger.log(
                phase="Planning", sender="Workflow", receiver="Planner",
                action="reuse_plan_state",
                input_summary=f"{len(criteria_md)} chars criteria",
                output_summary=f"{len(criteria_list)} criteria reused",
                tokens=0,
                duration=round(time.time() - start, 2)
            )
            print(f"  Criteria: {len(criteria_list)} tasks reused from state")
            for c in criteria_list:
                print(f"    {c['id']}: {c['criterion']}")
            return criteria_list

        mandatory_tasks = load_mandatory_tasks()
        uploaded_tasks: list[dict] = []
        self._planner_tokens = 0
        if criteria_path:
            skeletons = split_criteria_from_docx(criteria_path)
            for position, skeleton in enumerate(skeletons, 1):
                skeleton["id"] = f"U{position:02d}"
            planner = PlannerAgent(settings=self.settings)
            strategies = await planner.extract_strategies(skeletons)
            uploaded_tasks = assemble_tasks(skeletons, strategies)
            self._planner_tokens = planner.token_usage.get("total_tokens", 0)
        criteria_list = mandatory_tasks + uploaded_tasks
        self.logger.log(
            phase="Planning", sender="Workflow", receiver="Planner",
            action="extract_strategies",
            input_summary=f"{len(criteria_md)} chars criteria",
            output_summary=(
                f"{len(mandatory_tasks)} mandatory criteria loaded with fixed routes; "
                f"{len(uploaded_tasks)} uploaded criteria planned"
            ),
            tokens=self._planner_tokens,
            duration=round(time.time() - start, 2)
        )

        print(
            f"  Criteria: {len(criteria_list)} tasks "
            f"({len(mandatory_tasks)} mandatory, {len(uploaded_tasks)} uploaded)"
        )
        for c in criteria_list:
            print(f"    {c['id']}: {c['criterion']}")
        return criteria_list

    async def _phase_execute(
        self,
        criteria_list: list[dict],
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> list[dict]:
        """Phase 4: Orchestrator runs sub-agents with configured retrieval + reflection."""
        print(f"\n[Phase 4] Executing {len(criteria_list)} criteria reviews...")
        start = time.time()

        orchestrator = OrchestratorAgent(
            mcp_client=self.client,
            logger=self.logger,
            settings=self.settings,
            api_events_path=self.api_events_path,
            state_store=self.state_store,
        )
        results = await orchestrator.execute_criteria(
            criteria_list,
            progress_callback=progress_callback,
        )
        self._retrieval_tokens = getattr(orchestrator, 'retrieval_tokens', 0)

        self.logger.log(
            phase="Execution", sender="Orchestrator", receiver="Workflow",
            action="execute_criteria_complete",
            input_summary=f"{len(criteria_list)} criteria",
            output_summary=f"{len(results)} results",
            duration=round(time.time() - start, 2)
        )

        print(f"  Completed: {len(results)} reviews")
        return results

    async def _phase_summarize(
            self,
            results: list[dict]
    ) -> dict[str, str]:
        """Phase 5: Summarizer produces overview and priority advice sections."""
        print("\n[Phase 5] Summarizing...")
        start = time.time()

        summarizer = SummarizerAgent(settings=self.settings)
        summary_sections = await summarizer.compile_summary_comment(results)

        self._summarizer_tokens = summarizer.token_usage.get("total_tokens", 0)
        self.logger.log(
            phase="Summarization", sender="Summarizer", receiver="Workflow",
            action="compile_summary_comment",
            input_summary=f"{len(results)} criterion results",
            output_summary=f"{len(json.dumps(summary_sections, ensure_ascii=False))} chars summary",
            tokens=self._summarizer_tokens,
            duration=round(time.time() - start, 2)
        )

        print(f"  Summary sections: {len(summary_sections)}")
        return summary_sections

    def _collect_token_stats(self, results: list[dict]) -> dict[str, int]:
        """Aggregate token counters collected across workflow phases."""
        planner_tokens = getattr(self, "_planner_tokens", 0)
        retrieval_tokens = getattr(self, "_retrieval_tokens", 0)
        summarizer_tokens = getattr(self, "_summarizer_tokens", 0)
        execute_tokens = sum(result.get("tokens", 0) for result in results)
        return {
            "planner": planner_tokens,
            "retrieval": retrieval_tokens,
            "execute": execute_tokens,
            "summarizer": summarizer_tokens,
            "total": planner_tokens + retrieval_tokens + execute_tokens + summarizer_tokens,
        }

    def _save_review_outputs(self, results: list[dict]) -> str | None:
        """Persist the raw criterion review results for debugging."""
        if not LOGGING_ENABLED:
            return None
        if not self.review_outputs_path:
            return None
        return save_review_outputs_json(results, self.review_outputs_path)

    def _save_plan_state(self, criteria_list: list[dict]) -> str | None:
        """Persist planned criteria so a retry can reuse the planned execution surface."""
        if self.state_store is None:
            return None
        self.state_store.ensure_dirs()
        return str(self.state_store.save_plan(criteria_list, getattr(self, "criteria_source", {})))

    def _load_plan_state(self) -> list[dict] | None:
        """Load planned criteria from recovery state when the same task is rerun."""
        if self.state_store is None:
            return None
        plan = self.state_store.load_plan()
        if not plan:
            return None
        if plan.get("criteria_source") != getattr(self, "criteria_source", {}):
            return None
        criteria = plan.get("criteria")
        if not isinstance(criteria, list):
            return None
        validated: list[dict] = []
        for item in criteria:
            if not isinstance(item, dict) or not isinstance(item.get("execution"), dict):
                return None
            try:
                execution = ReviewExecutionPlan.model_validate(item["execution"])
            except ValidationError:
                return None
            validated.append({**item, "execution": execution.model_dump(mode="json")})
        return validated

    async def _phase_generate_annotated_docx(
        self,
        contract_path: str,
        results: list[dict],
        summary_sections: dict,
        output_dir: str | None = None,
        output_path: str | None = None,
        region_boundaries: dict[str, dict[str, str]] | None = None,
    ) -> str:
        """Phase 6: Generate only the annotated original-contract DOCX."""
        print("\n[Phase 6] Generating annotated DOCX...")
        start = time.time()
        contract_name = os.path.splitext(os.path.basename(contract_path))[0]
        tool_args = {
            "contract_name": contract_name,
            "contract_path": contract_path,
            "results_json": json.dumps(results, ensure_ascii=False),
            "summary_sections_json": json.dumps(summary_sections, ensure_ascii=False),
            "region_boundaries_json": json.dumps(region_boundaries or {}, ensure_ascii=False),
        }
        if output_dir is not None:
            tool_args["output_dir"] = output_dir
        if output_path is not None:
            tool_args["output_path"] = output_path
        if self.annotation_events_path is not None:
            tool_args["annotation_events_path"] = self.annotation_events_path

        result = await self.client.call_tool(
            "generate_docx_report",
            tool_args,
        )

        self.logger.log(
            phase="Report", sender="Workflow", receiver="MCP:generate_docx_report",
            action="generate_docx_report",
            input_summary=f"{len(results)} criterion results",
            output_summary=result,
            duration=round(time.time() - start, 2)
        )

        parsed = json.loads(result)
        if parsed.get("error"):
            raise RuntimeError(f"DOCX report generation failed: {parsed['error']}")
        self.annotation_report_summary = {
            "annotation_events_path": parsed.get("annotation_events_path"),
            "annotation_stats": parsed.get("annotation_stats", {}),
            "annotation_skipped_count": parsed.get("annotation_skipped_count", 0),
        }
        docx_path = parsed["docx"]
        print(f"  DOCX: {docx_path}")
        return docx_path

    def _print_completion_summary(
        self,
        elapsed_seconds: float,
        token_stats: dict[str, int],
        annotated_docx_path: str,
        review_outputs_path: str | None,
    ) -> None:
        """Print the compact end-of-run summary used by local/CLI execution."""
        mins, secs = divmod(int(elapsed_seconds), 60)
        print(f"\n{'=' * 60}")
        print(f"Workflow complete! Total time: {mins}m {secs}s")
        print(
            "Total tokens: "
            f"{token_stats['total']:,} "
            f"(Planner: {token_stats['planner']:,} | "
            f"Retrieval: {token_stats['retrieval']:,} | "
            f"Execute: {token_stats['execute']:,} | "
            f"Summarizer: {token_stats['summarizer']:,})"
        )
        print(f"Annotated DOCX: {annotated_docx_path}")
        print(f"Review outputs: {review_outputs_path}")
        print(f"{'=' * 60}")

