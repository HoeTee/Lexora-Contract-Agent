from __future__ import annotations

from contextlib import contextmanager, suppress
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import UploadFile
from config import API_KEEP_INPUT, DATA_DIR, LOGGING_ENABLED
from endpoints.runtime.filenames import safe_upload_filename
from loggers.api_event_logger import append_api_event


TASK_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{4}$")
BEIJING_TZ = timezone(timedelta(hours=8))
ACTIVE_STATUSES = {"pending", "queued", "running"}
TASK_WRITE_LOCK_TIMEOUT_SECONDS = 30
TASK_WRITE_LOCK_STALE_SECONDS = 300
TASK_WRITE_LOCK_POLL_SECONDS = 0.05


def now_iso() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def new_task_id() -> str:
    created_at = datetime.now(BEIJING_TZ)
    return f"{created_at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def validate_task_id(task_id: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("invalid task_id")
    return task_id


def validate_client_dir(client_dir: str) -> str:
    value = str(client_dir).strip()
    if value.startswith("web:"):
        tenant_id = value.removeprefix("web:")
        if not tenant_id or any(part in tenant_id for part in ('/', '\\', ':')) or tenant_id in {".", ".."}:
            raise ValueError("invalid client_dir")
        return value
    if not value or any(part in value for part in ('/', '\\', ':')) or value in {".", ".."}:
        raise ValueError("invalid client_dir")
    return value


def web_client_dir(tenant_id: str) -> str:
    value = str(tenant_id).strip()
    if not value or any(part in value for part in ('/', '\\', ':')) or value in {".", ".."}:
        raise ValueError("invalid tenant_id")
    return f"web:{value}"


def _task_scope_root(client_dir: str) -> Path:
    validated = validate_client_dir(client_dir)
    if validated.startswith("web:"):
        tenant_id = validated.removeprefix("web:")
        if not tenant_id:
            raise ValueError("invalid web client_dir")
        return Path(DATA_DIR) / "web" / tenant_id
    return Path(DATA_DIR) / "api"


def task_dir(client_dir: str, task_id: str) -> Path:
    return _task_scope_root(client_dir) / validate_task_id(task_id)


def task_json_path(client_dir: str, task_id: str) -> Path:
    return task_dir(client_dir, task_id) / "task.json"


@contextmanager
def task_write_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + TASK_WRITE_LOCK_TIMEOUT_SECONDS
    fd: int | None = None

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} created_at={time.time()}".encode("utf-8"))
            break
        except (FileExistsError, PermissionError):
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > TASK_WRITE_LOCK_STALE_SECONDS:
                    try:
                        lock_path.unlink()
                        continue
                    except OSError:
                        pass
            except OSError:
                pass

            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for task state write lock: {lock_path}") from None
            time.sleep(TASK_WRITE_LOCK_POLL_SECONDS)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            lock_path.unlink()


def input_dir(client_dir: str, task_id: str) -> Path:
    return task_dir(client_dir, task_id) / "input"


def output_dir(client_dir: str, task_id: str) -> Path:
    return task_dir(client_dir, task_id) / "output"


def logs_dir(client_dir: str, task_id: str) -> Path:
    return task_dir(client_dir, task_id) / "logs"


def state_dir(client_dir: str, task_id: str) -> Path:
    return task_dir(client_dir, task_id) / "state"


def criteria_state_dir(client_dir: str, task_id: str) -> Path:
    return state_dir(client_dir, task_id) / "criteria"


def plan_state_path(client_dir: str, task_id: str) -> Path:
    return state_dir(client_dir, task_id) / "plan.json"


def criterion_input_state_path(client_dir: str, task_id: str, criterion_id: str) -> Path:
    return criteria_state_dir(client_dir, task_id) / f"{criterion_id}.input.json"


def criterion_output_state_path(client_dir: str, task_id: str, criterion_id: str) -> Path:
    return criteria_state_dir(client_dir, task_id) / f"{criterion_id}.output.json"


def criterion_error_state_path(client_dir: str, task_id: str, criterion_id: str) -> Path:
    return criteria_state_dir(client_dir, task_id) / f"{criterion_id}.error.json"


def conversation_log_dir(client_dir: str, task_id: str) -> Path:
    return logs_dir(client_dir, task_id) / "conversations"


def api_events_path(client_dir: str, task_id: str) -> Path:
    return logs_dir(client_dir, task_id) / "events.log"


def trace_path(client_dir: str, task_id: str) -> Path:
    return logs_dir(client_dir, task_id) / "trace.json"


def review_outputs_path(client_dir: str, task_id: str) -> Path:
    return logs_dir(client_dir, task_id) / "review_outputs.json"


def annotation_events_path(client_dir: str, task_id: str) -> Path:
    return logs_dir(client_dir, task_id) / "annotation_events.json"


def mcp_log_path(client_dir: str, task_id: str) -> Path:
    return logs_dir(client_dir, task_id) / "mcp.log"


def runtime_input_dir(client_dir: str, task_id: str) -> Path:
    return task_dir(client_dir, task_id) / "runtime_input"


def model_config_path(client_dir: str, task_id: str) -> Path:
    return runtime_input_dir(client_dir, task_id) / "model_config.json"


def should_write_task_file(kind: str) -> bool:
    if kind == "input":
        return bool(API_KEEP_INPUT)
    if kind == "output":
        return True
    if kind == "logs":
        return bool(LOGGING_ENABLED)
    raise ValueError(f"Unknown task file kind: {kind}")


def task_input_work_dir(client_dir: str, task_id: str) -> Path:
    if should_write_task_file("input"):
        return input_dir(client_dir, task_id)
    return runtime_input_dir(client_dir, task_id)


def save_task_input_upload(client_dir: str, task_id: str, upload_file: UploadFile, filename: str) -> Path:
    target_dir = task_input_work_dir(client_dir, task_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    with target_path.open("wb") as f:
        shutil.copyfileobj(upload_file.file, f)
    return target_path


def _content_disposition_param(value: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|;)\s*{re.escape(name)}\s*=\s*(\"[^\"]*\"|[^;]*)",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip().strip('"')


def _decode_header_filename(value: str) -> str:
    if "''" in value:
        charset, encoded = value.split("''", 1)
        return unquote(encoded, encoding=charset or "utf-8", errors="replace")
    return unquote(value)


def filename_from_content_disposition(value: str | None) -> str | None:
    if not value:
        return None
    resolved = (
        _content_disposition_param(value, "filename*")
        or _content_disposition_param(value, "filename")
    )
    if not resolved:
        return None
    safe_name = safe_upload_filename(_decode_header_filename(resolved))
    return safe_name if safe_name.lower().endswith(".docx") else None


def _filename_from_url_path(file_url: str) -> str | None:
    raw_name = Path(urlsplit(file_url).path).name
    if not raw_name.lower().endswith(".docx"):
        return None
    return safe_upload_filename(unquote(raw_name))


def save_task_input_url(
    client_dir: str,
    task_id: str,
    file_url: str,
    fallback_filename: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> Path:
    target_dir = task_input_work_dir(client_dir, task_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / fallback_filename
    total = 0
    try:
        with httpx.stream("GET", file_url, timeout=timeout_seconds, follow_redirects=True) as response:
            response.raise_for_status()
            resolved_filename = (
                filename_from_content_disposition(response.headers.get("content-disposition"))
                or _filename_from_url_path(str(response.url))
                or fallback_filename
            )
            target_path = target_dir / resolved_filename
            with target_path.open("wb") as f:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Downloaded file exceeds configured max size.")
                    f.write(chunk)
    except Exception:
        if target_path.exists():
            target_path.unlink()
        raise
    return target_path


def save_task_input_copy(client_dir: str, task_id: str, source_path: Path, filename: str) -> Path:
    target_dir = task_input_work_dir(client_dir, task_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    shutil.copy2(source_path, target_path)
    return target_path


def cleanup_runtime_input(client_dir: str, task_id: str) -> None:
    runtime_dir = runtime_input_dir(client_dir, task_id)
    if not runtime_dir.exists():
        return
    shutil.rmtree(runtime_dir)


def save_task_model_config(client_dir: str, task_id: str, model_config: dict[str, str]) -> Path:
    path = model_config_path(client_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(model_config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return path


def read_task_model_config(client_dir: str, task_id: str) -> dict[str, str] | None:
    path = model_config_path(client_dir, task_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    result = {str(key): str(value) for key, value in data.items() if str(value).strip()}
    return result or None


def task_api_events_path(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    return api_events_path(client_dir, task_id)


def task_trace_path(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    return trace_path(client_dir, task_id)


def task_review_outputs_path(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    return review_outputs_path(client_dir, task_id)


def task_annotation_events_path(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    return annotation_events_path(client_dir, task_id)


def task_conversation_log_dir(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    return conversation_log_dir(client_dir, task_id)


def task_mcp_log_file(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    return mcp_log_path(client_dir, task_id)


def write_task_log_event(client_dir: str, task_id: str, event: str, **fields: Any) -> Path | None:
    if not should_write_task_file("logs"):
        return None
    path = api_events_path(client_dir, task_id)
    append_api_event(path, event, task_id=task_id, **fields)
    return path


def ensure_task_dirs(client_dir: str, task_id: str) -> None:
    task_dir(client_dir, task_id).mkdir(parents=True, exist_ok=True)
    output_dir(client_dir, task_id).mkdir(parents=True, exist_ok=True)

    if should_write_task_file("input"):
        input_dir(client_dir, task_id).mkdir(parents=True, exist_ok=True)

    if should_write_task_file("logs"):
        for path in (
            logs_dir(client_dir, task_id),
            conversation_log_dir(client_dir, task_id),
        ):
            path.mkdir(parents=True, exist_ok=True)


def write_task(task: dict[str, Any]) -> dict[str, Any]:
    path = task_json_path(task["client_dir"], task["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with task_write_lock(path):
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(path)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()
    return task


def read_task(client_dir: str, task_id: str) -> dict[str, Any] | None:
    path = task_json_path(client_dir, task_id)
    if not path.exists():
        return None
    task = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(task, dict) or task.get("client_dir") != validate_client_dir(client_dir):
        return None
    return task


def read_task_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists() or path.name != "task.json":
        return None
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(task, dict):
        return None
    task_id = str(task.get("task_id") or "")
    client_dir = str(task.get("client_dir") or "")
    try:
        validate_task_id(task_id)
        validate_client_dir(client_dir)
    except ValueError:
        return None
    if task_json_path(client_dir, task_id) != path:
        return None
    return task


def iter_task_json_paths() -> list[Path]:
    data_root = Path(DATA_DIR)
    paths: list[Path] = []
    api_root = data_root / "api"
    if api_root.exists():
        paths.extend(api_root.glob("*/task.json"))
    web_root = data_root / "web"
    if web_root.exists():
        paths.extend(web_root.glob("*/*/task.json"))
    return sorted(paths)


def iter_active_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in iter_task_json_paths():
        task = read_task_json_file(path)
        if task and task.get("status") in ACTIVE_STATUSES:
            tasks.append(task)
    return tasks


def update_task(client_dir: str, task_id: str, **updates: Any) -> dict[str, Any]:
    task = read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    task.update(updates)
    return write_task(task)


def create_task(
    *,
    client_id: str,
    client_dir: str,
    task_id: str,
    contract_filename: str,
    contract_path: Path,
    criteria_source: str,
    criteria_filename: str | None,
    criteria_path: Path | None,
    result_filename: str,
    result_path: Path,
    model_config_meta: dict[str, Any] | None = None,
    meta_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    task_api_log = task_api_events_path(client_dir, task_id)
    task_trace_log = task_trace_path(client_dir, task_id)
    task_review_outputs_log = task_review_outputs_path(client_dir, task_id)
    task_annotation_events_log = task_annotation_events_path(client_dir, task_id)
    task_conversation_log = task_conversation_log_dir(client_dir, task_id)
    task_mcp_log = task_mcp_log_file(client_dir, task_id)
    task = {
        "client_id": client_id,
        "client_dir": validate_client_dir(client_dir),
        "task_id": task_id,
        "status": "pending",
        "created_at": now_iso(),
        "started_at": None,
        "finished_at": None,
        "message": "Review job submitted.",
        "error": None,
        "meta_fields": dict(meta_fields or {}),
        "input": {
            "contract_filename": contract_filename,
            "contract_path": str(contract_path),
            "criteria_source": criteria_source,
            "criteria_filename": criteria_filename,
            "criteria_path": str(criteria_path) if criteria_path else None,
        },
        "output": {
            "result_filename": result_filename,
            "result_path": str(result_path),
        },
        "cancel": {
            "requested": False,
            "requested_at": None,
            "cancelled_at": None,
        },
        "worker": {
            "worker_id": None,
            "worker_started_at": None,
            "heartbeat_at": None,
            "interrupted_at": None,
        },
        "model_config": model_config_meta or {
            "source": "env",
            "llm": False,
            "embedding": False,
            "reranker": False,
        },
        "state": {
            "state_dir": str(state_dir(client_dir, task_id)),
            "plan_path": str(plan_state_path(client_dir, task_id)),
            "criteria_dir": str(criteria_state_dir(client_dir, task_id)),
        },
        "logs": {
            "api_events_path": str(task_api_log) if task_api_log else None,
            "events_log_path": str(task_api_log) if task_api_log else None,
            "trace_path": str(task_trace_log) if task_trace_log else None,
            "review_outputs_path": str(task_review_outputs_log) if task_review_outputs_log else None,
            "annotation_events_path": str(task_annotation_events_log) if task_annotation_events_log else None,
            "conversation_log_dir": str(task_conversation_log) if task_conversation_log else None,
            "mcp_log_path": str(task_mcp_log) if task_mcp_log else None,
        },
    }
    return write_task(task)


def mark_worker_started(client_dir: str, task_id: str, worker_id: str) -> dict[str, Any]:
    timestamp = now_iso()
    task = read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    worker = task.get("worker") if isinstance(task.get("worker"), dict) else {}
    worker.update(
        {
            "worker_id": worker_id,
            "worker_started_at": timestamp,
            "heartbeat_at": timestamp,
            "interrupted_at": None,
        }
    )
    task["worker"] = worker
    return write_task(task)


def mark_worker_heartbeat(client_dir: str, task_id: str, worker_id: str) -> dict[str, Any] | None:
    task = read_task(client_dir, task_id)
    if task is None or task.get("status") not in ACTIVE_STATUSES:
        return None
    worker = task.get("worker") if isinstance(task.get("worker"), dict) else {}
    if worker.get("worker_id") != worker_id:
        return None
    worker["heartbeat_at"] = now_iso()
    task["worker"] = worker
    return write_task(task)


def mark_running(client_dir: str, task_id: str) -> dict[str, Any]:
    return update_task(
        client_dir,
        task_id,
        status="running",
        started_at=now_iso(),
        message="Contract review is running.",
    )


def mark_queued(client_dir: str, task_id: str) -> dict[str, Any]:
    return update_task(
        client_dir,
        task_id,
        status="queued",
        message="Review job is waiting for an execution slot.",
    )


def mark_succeeded(client_dir: str, task_id: str, *, quality: dict | None = None,
                   criteria_manifest: dict | None = None) -> dict[str, Any]:
    return update_task(
        client_dir,
        task_id,
        status="succeeded",
        finished_at=now_iso(),
        message=("审查文件已生成，部分审查项待复核。" if quality and quality.get("status") == "pending_review"
                 else "Contract review succeeded."),
        quality=quality or {},
        criteria_manifest=criteria_manifest or {},
        error=None,
    )


def mark_failed(
    client_dir: str,
    task_id: str,
    *,
    code: str,
    message: str,
    component: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if error is None:
        error = {"code": code, "message": message}
        if component:
            error["component"] = component
    return update_task(
        client_dir,
        task_id,
        status="failed",
        finished_at=now_iso(),
        message="Contract review failed.",
        error=error,
    )


def mark_worker_interrupted(
    client_dir: str,
    task_id: str,
    *,
    reason: str = "service_startup_reconciliation",
) -> dict[str, Any]:
    timestamp = now_iso()
    task = read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    worker = task.get("worker") if isinstance(task.get("worker"), dict) else {}
    worker["interrupted_at"] = timestamp
    task["worker"] = worker
    task["status"] = "failed"
    task["finished_at"] = timestamp
    task["message"] = "Contract review was interrupted."
    task["error"] = {
        "code": "WORKER_INTERRUPTED",
        "message": "审查任务因服务重启或进程退出中断，请重新提交。",
        "component": "worker",
        "reason": reason,
    }
    return write_task(task)


def reconcile_interrupted_tasks(*, reason: str = "service_startup_reconciliation") -> list[dict[str, Any]]:
    interrupted: list[dict[str, Any]] = []
    for task in iter_active_tasks():
        client_dir = task["client_dir"]
        task_id = task["task_id"]
        updated = mark_worker_interrupted(client_dir, task_id, reason=reason)
        write_task_log_event(
            client_dir,
            task_id,
            "worker_interrupted",
            previous_status=task.get("status"),
            reason=reason,
        )
        write_task_log_event(
            client_dir,
            task_id,
            "review_failed",
            error="WORKER_INTERRUPTED",
            reason=reason,
        )
        interrupted.append(updated)
    return interrupted


def request_cancel(client_dir: str, task_id: str) -> dict[str, Any]:
    task = read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)

    status = task["status"]
    if status in {"pending", "queued"}:
        task["status"] = "cancelled"
        task["finished_at"] = now_iso()
        task["message"] = "Review job cancelled."
        task["cancel"]["requested"] = True
        task["cancel"]["requested_at"] = task["cancel"]["requested_at"] or now_iso()
        task["cancel"]["cancelled_at"] = now_iso()
        return write_task(task)

    return task


def is_cancel_requested(client_dir: str, task_id: str) -> bool:
    task = read_task(client_dir, task_id)
    if task is None:
        return False
    return bool(task.get("cancel", {}).get("requested"))


def mark_cancelled(client_dir: str, task_id: str) -> dict[str, Any]:
    task = read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    timestamp = now_iso()
    task["status"] = "cancelled"
    task["finished_at"] = timestamp
    task["message"] = "Review job cancelled."
    task["cancel"]["requested"] = True
    task["cancel"]["requested_at"] = task["cancel"]["requested_at"] or timestamp
    task["cancel"]["cancelled_at"] = timestamp
    return write_task(task)
