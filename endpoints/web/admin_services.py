from __future__ import annotations

import json
from pathlib import Path

from config import DATA_DIR
from endpoints.runtime.auth import load_users, normalize_role
from endpoints.runtime.tenancy import tenant_user_criteria_path, tenant_profiles_file
from loggers.review_history import format_file_size, format_timestamp, load_history_records


def list_admin_users(tenant_id: str) -> list[dict]:
    rows = []
    data_dir = Path(DATA_DIR)
    for user in load_users(tenant_profiles_file(tenant_id)):
        username = user.get("username", "")
        records = load_history_records(data_dir, username, tenant_id=tenant_id)
        latest_record = max(records, key=lambda item: item.get("report_created_at") or "", default={})
        rows.append(
            {
                "username": username,
                "display_name": user.get("display_name") or username,
                "role": normalize_role(user.get("role")),
                "enabled": user.get("enabled", True),
                "history_count": len(records),
                "latest_review_at": latest_record.get("report_created_at") or "-",
            }
        )
    return sorted(rows, key=lambda item: item["username"])


def get_admin_user(tenant_id: str, username: str) -> dict | None:
    for user in load_users(tenant_profiles_file(tenant_id)):
        if user.get("username") == username:
            return {
                **user,
                "role": normalize_role(user.get("role")),
                "display_name": user.get("display_name") or username,
            }
    return None


def list_user_review_history(tenant_id: str, username: str) -> list[dict]:
    data_dir = Path(DATA_DIR)
    rows = []
    for record in load_history_records(data_dir, username, tenant_id=tenant_id):
        report_name = record.get("report_stored_name")
        report_path = data_dir / "web" / tenant_id / str(record.get("task_id") or "") / "output" / str(report_name or "")
        rows.append(
            {
                **record,
                "contract_display_name": record.get("contract_original_name") or record.get("contract_stored_name") or "-",
                "report_display_name": record.get("report_display_name") or record.get("report_stored_name") or "-",
                "contract_size": format_file_size(record.get("contract_size_bytes")),
                "report_size": format_file_size(record.get("report_size_bytes")),
                "report_exists": bool(report_name and report_path.exists()),
            }
        )
    rows.sort(key=lambda item: item.get("report_created_at") or "", reverse=True)
    return rows


def get_user_criteria_info(tenant_id: str, username: str) -> dict:
    path = tenant_user_criteria_path(tenant_id, username)
    if not path.exists():
        return {"exists": False, "size": "-", "updated_at": "-", "path": path}
    stat = path.stat()
    return {
        "exists": True,
        "size": format_file_size(stat.st_size),
        "updated_at": format_timestamp(stat.st_mtime),
        "path": path,
    }


def list_user_log_tasks(tenant_id: str, username: str) -> list[dict]:
    root = Path(DATA_DIR) / "web" / tenant_id
    if not root.exists():
        return []

    rows = []
    for task_json in root.glob("*/task.json"):
        task_dir = task_json.parent
        logs_dir = task_dir / "logs"
        api_events = logs_dir / "events.log"
        try:
            task = json.loads(task_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        if task.get("username") != username:
            continue
        stat = task_dir.stat()
        rows.append(
            {
                "date": str(task.get("created_at") or "")[:10],
                "task_name": task_dir.name,
                "relative_path": str(logs_dir.relative_to(root)),
                "updated_at": format_timestamp(stat.st_mtime),
                "has_api_events": api_events.exists(),
            }
        )
    rows.sort(key=lambda item: item["updated_at"], reverse=True)
    return rows


def admin_summary(tenant_id: str) -> dict:
    users = list_admin_users(tenant_id)
    return {
        "user_count": len(users),
        "admin_count": sum(1 for user in users if user["role"] == "admin"),
        "disabled_count": sum(1 for user in users if not user["enabled"]),
        "review_count": sum(user["history_count"] for user in users),
        "recent_reviews": sorted(
            [user for user in users if user["latest_review_at"] != "-"],
            key=lambda item: item["latest_review_at"],
            reverse=True,
        )[:10],
    }
