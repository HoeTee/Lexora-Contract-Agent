from __future__ import annotations

from pathlib import Path

from endpoints.review.task_store import read_task, update_task
from endpoints.runtime.filenames import build_report_display_name
from loggers.review_history import HISTORY_SCHEMA_VERSION, format_timestamp


def complete_web_history(client_dir: str, task_id: str) -> None:
    task = read_task(client_dir, task_id)
    if not task or not client_dir.startswith("web:") or task.get("status") != "succeeded":
        return
    contract = Path(task["input"]["contract_path"])
    report = Path(task["output"]["result_path"])
    original = task["input"]["contract_filename"]
    contract_stat = contract.stat() if contract.exists() else None
    report_stat = report.stat() if report.exists() else None
    criteria_source = task.get("criteria_source") or task["input"].get("criteria_source", "mandatory")
    update_task(
        client_dir,
        task_id,
        schema_version=HISTORY_SCHEMA_VERSION,
        contract_original_name=original,
        contract_stored_name=contract.name,
        contract_size_bytes=contract_stat.st_size if contract_stat else None,
        contract_uploaded_at=task.get("contract_uploaded_at") or task.get("created_at"),
        criteria_source=criteria_source,
        criteria_original_name=task["input"].get("criteria_filename") if criteria_source == "uploaded" else None,
        report_display_name=build_report_display_name(original),
        report_stored_name=report.name,
        report_size_bytes=report_stat.st_size if report_stat else None,
        report_created_at=format_timestamp(report_stat.st_mtime) if report_stat else None,
    )
