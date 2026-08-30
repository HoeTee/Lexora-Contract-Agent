from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from docx import Document


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.json_utils import extract_json  # noqa: E402
from tools.document.reporting.docx_report import DocxReportGenerator  # noqa: E402

from evaluate_results import (  # noqa: E402
    DEFAULT_CASES_DIR,
    DEFAULT_TASK_ROOT,
    load_candidates,
    load_records,
    normalized,
    write_csv,
)


DEFAULT_FAILURES = (
    SUITE_ROOT
    / "results"
    / "final_10_contracts_20260830_corrected"
    / "recall_failures.csv"
)
DEFAULT_CHANGES = SUITE_ROOT / "xml_changes_10.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use saved task traces, conversations, and DOCX anchors to diagnose recall failures."
    )
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--failures-csv", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--changes-csv", type=Path, default=DEFAULT_CHANGES)
    parser.add_argument("--task-root", type=Path, default=DEFAULT_TASK_ROOT)
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def criterion_in_change(raw_ids: str, criterion: int) -> bool:
    return criterion in {
        int(value.strip()) for value in raw_ids.split(";") if value.strip().isdigit()
    }


def relevant_mutations(
    changes: list[dict[str, str]], contract_id: str, criterion: int
) -> list[str]:
    return list(
        dict.fromkeys(
            row["after_text"].strip()
            for row in changes
            if row["contract_id"] == contract_id
            and criterion_in_change(row["criterion_ids"], criterion)
            and row["after_text"].strip()
        )
    )


def latest_subagent_conversation(task_dir: Path, criterion: int) -> Path:
    paths = sorted(
        (task_dir / "logs" / "conversations").glob(f"SubAgent_C{criterion}_*.json")
    )
    if not paths:
        raise FileNotFoundError(
            f"SubAgent_C{criterion} conversation not found under {task_dir}"
        )
    return paths[-1]


def trace_diagnosis(task_dir: Path, criterion: int | None = None) -> dict[str, Any]:
    trace_file = task_dir / "logs" / "trace.json"
    trace = json.loads(trace_file.read_text(encoding="utf-8"))
    runs = trace.get("runs") if isinstance(trace, dict) else None
    if not isinstance(runs, list):
        raise ValueError(f"trace has no runs list: {trace_file}")

    if criterion is not None:
        criterion_name = f"criterion.C{criterion}"
        criterion_run = next(
            (run for run in runs if run.get("name") == criterion_name),
            None,
        )
        if criterion_run is None:
            raise ValueError(f"{criterion_name} not found in {trace_file}")
        child_runs = [
            run for run in runs if run.get("parent_id") == criterion_run.get("id")
        ]
        return {
            "trace_path": str(trace_file.resolve()),
            "trace_criterion_status": criterion_run.get("status", ""),
            "trace_criterion_duration_seconds": criterion_run.get("duration_seconds", ""),
            "trace_criterion_outputs": json.dumps(
                criterion_run.get("outputs") or {}, ensure_ascii=False
            ),
            "trace_contract_search_count": sum(
                1 for run in child_runs if run.get("name") == "mcp.contract_search"
            ),
        }

    report_run = next(
        (run for run in runs if run.get("name") == "phase.generate_docx"),
        None,
    )
    if report_run is None:
        raise ValueError(f"phase.generate_docx not found in {trace_file}")
    return {
        "trace_path": str(trace_file.resolve()),
        "trace_docx_status": report_run.get("status", ""),
        "trace_docx_duration_seconds": report_run.get("duration_seconds", ""),
        "trace_docx_outputs": json.dumps(report_run.get("outputs") or {}, ensure_ascii=False),
    }


def conversation_diagnosis(path: Path, mutations: list[str]) -> dict[str, Any]:
    messages = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        raise ValueError(f"conversation must contain a JSON list: {path}")

    user_messages = [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assistant_payloads = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        assistant_payloads.append(extract_json(content))

    if not assistant_payloads:
        raise ValueError(f"conversation has no structured assistant output: {path}")

    initial_context = user_messages[0] if user_messages else ""
    marker = "以下是合同原文中与此标准相关的内容："
    retrieved_context = initial_context.split(marker, 1)[-1] if marker in initial_context else initial_context
    normalized_context = normalized(retrieved_context)
    matched_mutations = [
        mutation for mutation in mutations if normalized(mutation) in normalized_context
    ]
    statuses = [str(payload.get("status") or "") for payload in assistant_payloads]
    initial_status = statuses[0]
    final_status = statuses[-1]
    if initial_status == "issues_found" and final_status != "issues_found":
        root_cause = "reflector_overrode_initial_issue"
    elif initial_status == "not_applicable":
        root_cause = "initial_not_applicable"
    elif initial_status == "compliant":
        root_cause = "initial_compliant"
    else:
        root_cause = "unclassified_review_chain"

    return {
        "conversation_path": str(path.resolve()),
        "assistant_status_chain": json.dumps(statuses, ensure_ascii=False),
        "initial_status": initial_status,
        "final_status": final_status,
        "reflector_feedbacks": json.dumps(user_messages[1:], ensure_ascii=False),
        "retrieved_context_chars": len(retrieved_context),
        "mutation_count": len(mutations),
        "retrieved_mutation_count": len(matched_mutations),
        "retrieved_mutations": json.dumps(matched_mutations, ensure_ascii=False),
        "all_mutations_retrieved": "yes"
        if mutations and len(matched_mutations) == len(mutations)
        else "no",
        "review_root_cause": root_cause,
    }


def diagnose_review_failures(
    failures: list[dict[str, str]],
    changes: list[dict[str, str]],
    records: dict[str, dict[str, Any]],
    task_root: Path,
) -> list[dict[str, Any]]:
    rows = []
    for failure in failures:
        if failure["failure_stage"] != "review":
            continue
        contract_id = failure["contract_id"]
        criterion = int(failure["criterion_id"])
        task_id = str(records[contract_id]["task_id"])
        mutations = relevant_mutations(changes, contract_id, criterion)
        conversation_path = latest_subagent_conversation(task_root / task_id, criterion)
        rows.append(
            {
                **failure,
                "task_id": task_id,
                "mutations": json.dumps(mutations, ensure_ascii=False),
                **trace_diagnosis(task_root / task_id, criterion),
                **conversation_diagnosis(conversation_path, mutations),
            }
        )
    return rows


def diagnose_docx_failures(
    failures: list[dict[str, str]],
    records: dict[str, dict[str, Any]],
    task_root: Path,
    cases_dir: Path,
) -> list[dict[str, Any]]:
    rows = []
    documents: dict[str, Document] = {}
    candidate_cache: dict[str, tuple[list[Any], dict[int, int]]] = {}

    for failure in failures:
        if failure["failure_stage"] != "docx_write":
            continue
        contract_id = failure["contract_id"]
        criterion = int(failure["criterion_id"])
        task_id = str(records[contract_id]["task_id"])
        if contract_id not in documents:
            documents[contract_id] = Document(cases_dir / f"{contract_id}.docx")
        if contract_id not in candidate_cache:
            candidates, _, _, issue_counts = load_candidates(task_root, task_id)
            candidate_cache[contract_id] = (candidates, issue_counts)
        all_candidates, issue_counts = candidate_cache[contract_id]
        candidates = [
            candidate for candidate in all_candidates if candidate.criterion_id == criterion
        ]
        matched = []
        for candidate in candidates:
            anchor = DocxReportGenerator._find_text_range_anchor_in_xml_anchor(
                documents[contract_id],
                candidate.anchor_type,
                candidate.anchor_id,
                candidate.quoted_text,
            )
            matched.append(
                {
                    "issue_id": candidate.issue_id,
                    "anchor_id": candidate.anchor_id,
                    "quoted_text": candidate.quoted_text,
                    "comment_text": candidate.comment_text,
                    "matched": anchor is not None,
                    "matched_path": anchor.path if anchor is not None else "",
                    "matched_text": anchor.match.matched_text if anchor is not None else "",
                }
            )

        issue_count = issue_counts.get(criterion, 0)
        anchored_issue_ids = {candidate.issue_id for candidate in candidates}
        anchorless_issue_count = max(0, issue_count - len(anchored_issue_ids))
        matched_anchor_count = sum(1 for item in matched if item["matched"])
        if anchorless_issue_count == issue_count:
            root_cause = "anchorless_issue_skipped_by_writer"
        elif not matched_anchor_count:
            root_cause = "xml_anchor_scoped_quote_match_failed"
        elif matched_anchor_count == len(candidates) and not anchorless_issue_count:
            root_cause = "resolver_matched_but_comment_not_visible"
        else:
            root_cause = "mixed_annotation_failures"

        rows.append(
            {
                **failure,
                "task_id": task_id,
                **trace_diagnosis(task_root / task_id),
                "input_docx": str((cases_dir / f"{contract_id}.docx").resolve()),
                "output_docx": str(Path(str(records[contract_id]["output_docx"])).resolve()),
                "backend_issue_count": issue_count,
                "anchorless_issue_count": anchorless_issue_count,
                "anchor_count": len(candidates),
                "replayed_anchor_match_count": matched_anchor_count,
                "replayed_anchors": json.dumps(matched, ensure_ascii=False),
                "docx_root_cause": root_cause,
            }
        )
    return rows


def main() -> int:
    args = parse_args()
    task_root = args.task_root.resolve()
    cases_dir = args.cases_dir.resolve()
    output_dir = args.output_dir.resolve()
    failures = read_csv(args.failures_csv.resolve())
    changes = read_csv(args.changes_csv.resolve())
    records = load_records([path.resolve() for path in args.run_dir])

    review_rows = diagnose_review_failures(failures, changes, records, task_root)
    docx_rows = diagnose_docx_failures(failures, records, task_root, cases_dir)
    if len(review_rows) != 45 or len(docx_rows) != 62:
        raise ValueError(
            f"unexpected failure counts: review={len(review_rows)}, docx_write={len(docx_rows)}"
        )

    review_fields = tuple(review_rows[0].keys())
    docx_fields = tuple(docx_rows[0].keys())
    write_csv(output_dir / "review_failure_diagnosis.csv", review_fields, review_rows)
    write_csv(output_dir / "docx_write_failure_diagnosis.csv", docx_fields, docx_rows)

    print("Review root causes:")
    for name, count in sorted(Counter(row["review_root_cause"] for row in review_rows).items()):
        print(f"  {name}: {count}")
    print("DOCX root causes:")
    for name, count in sorted(Counter(row["docx_root_cause"] for row in docx_rows).items()):
        print(f"  {name}: {count}")
    print(f"Output: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
