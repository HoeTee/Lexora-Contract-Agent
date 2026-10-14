from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


HISTORY_SCHEMA_VERSION = 2


def records_dir(data_dir: Path, username: str) -> Path:
    return data_dir / username / "records"


def history_file_path(data_dir: Path, username: str) -> Path:
    return records_dir(data_dir, username) / "review_history.json"


def tenant_task_root(data_dir: Path, tenant_id: str) -> Path:
    return data_dir / "web" / tenant_id


def format_file_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "-"

    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def load_history_records(data_dir: Path, username: str, tenant_id: str = "") -> list[dict]:
    if tenant_id:
        root = tenant_task_root(data_dir, tenant_id)
        if not root.exists():
            return []
        records: list[dict] = []
        for task_json in sorted(root.glob("*/task.json")):
            try:
                record = json.loads(task_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("username") == username:
                records.append(record)
        return records

    path = history_file_path(data_dir, username)
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]
