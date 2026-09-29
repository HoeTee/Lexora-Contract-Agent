from __future__ import annotations

from pathlib import Path
from typing import Any

from endpoints.review.task_store import summarize_criteria


def present_submit_response(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "message": task["message"],
    }


def present_review_task(
    task: dict[str, Any],
    *,
    include_meta_fields: bool = False,
) -> dict[str, Any]:
    result_path = Path(task["output"]["result_path"])
    source_path = Path(task["input"]["contract_path"])
    terminal_status = task["status"] in {"succeeded", "failed", "cancelled"}
    source_fallback_available = (
        task["status"] in {"failed", "cancelled"}
        and source_path.exists()
    )
    response = {
        "task_id": task["task_id"],
        "status": task["status"],
        "created_at": task["created_at"],
        "queued_at": task.get("queued_at"),
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "message": task["message"],
        "error": task["error"],
        "quality": summarize_criteria(task.get("criteria") or []),
        "criteria": task.get("criteria", []),
        "input": {
            "contract_filename": task["input"]["contract_filename"],
            "criteria_source": task["input"]["criteria_source"],
            "criteria_filename": task["input"]["criteria_filename"],
            "stored": "input" in Path(task["input"]["contract_path"]).parts,
        },
        "output": {
            "result_filename": task["output"]["result_filename"],
            "ready": (
                terminal_status
                and (result_path.exists() or source_fallback_available)
            ),
        },
    }
    if include_meta_fields:
        response["meta_fields"] = task.get("meta_fields", {})
    return response

