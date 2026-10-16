from __future__ import annotations

from contextlib import contextmanager, suppress
import json
import errno
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import UploadFile
from config import API_KEEP_INPUT, DATA_DIR, LOGGING_ENABLED, LOGGING_DETAILED
from endpoints.runtime.filenames import safe_upload_filename
from loggers.api_event_logger import append_api_event
from endpoints.runtime.model_budget import task_deadline

if os.name == "nt":
    import msvcrt
else:
    import fcntl


TASK_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{4}$")
BEIJING_TZ = timezone(timedelta(hours=8))
ACTIVE_STATUSES = {"pending", "queued", "running"}
TASK_WRITE_LOCK_TIMEOUT_SECONDS = 30
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
    """OS-owned file lock: killing a writer releases it without stale-lock delays."""
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + TASK_WRITE_LOCK_TIMEOUT_SECONDS
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for task state write lock: {lock_path}") from None
                time.sleep(TASK_WRITE_LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(fd)
        # Keep the inode: unlinking can split simultaneous writers across two locks.


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


def docxindex_state_dir(client_dir: str, task_id: str) -> Path:
    return state_dir(client_dir, task_id) / "docxindex"


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
    if not should_write_task_file("logs") or not LOGGING_DETAILED:
        return None
    return trace_path(client_dir, task_id)


def task_review_outputs_path(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs") or not LOGGING_DETAILED:
        return None
    return review_outputs_path(client_dir, task_id)


def task_annotation_events_path(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs") or not LOGGING_DETAILED:
        return None
    return annotation_events_path(client_dir, task_id)


def task_conversation_log_dir(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs") or not LOGGING_DETAILED:
        return None
    return conversation_log_dir(client_dir, task_id)


def task_mcp_log_file(client_dir: str, task_id: str) -> Path | None:
    if not should_write_task_file("logs") or not LOGGING_DETAILED:
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
        logs_dir(client_dir, task_id).mkdir(parents=True, exist_ok=True)
        (logs_dir(client_dir, task_id) / "logical_calls").mkdir(parents=True, exist_ok=True)
        if LOGGING_DETAILED:
            conversation_log_dir(client_dir, task_id).mkdir(parents=True, exist_ok=True)


def _write_task_file(path: Path, task: dict[str, Any]) -> None:
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def write_task(task: dict[str, Any]) -> dict[str, Any]:
    path = task_json_path(task["client_dir"], task["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with task_write_lock(path):
        _write_task_file(path, task)
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
    path = task_json_path(client_dir, task_id)
    with task_write_lock(path):
        task = read_task(client_dir, task_id)
        if task is None:
            raise FileNotFoundError(task_id)
        task.update(updates)
        _write_task_file(path, task)
        return task


def mutate_task_if_status(
    client_dir: str,
    task_id: str,
    expected_statuses: set[str],
    mutator: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    path = task_json_path(client_dir, task_id)
    if not path.exists():
        return None
    with task_write_lock(path):
        task = read_task_json_file(path)
        if task is None or task.get("status") not in expected_statuses:
            return None
        if not mutator(task):
            return None
        _write_task_file(path, task)
    return task


def summarize_criteria(criteria: list[dict[str, Any]] | None) -> dict[str, Any]:
    items = criteria or []
    statuses = [str(item.get("execution_status") or "not_started") for item in items]
    incomplete_ids = [
        str(item.get("id")) for item in items
        if item.get("id") and item.get("execution_status") == "incomplete"
    ]
    error_ids = [
        str(item.get("id")) for item in items
        if item.get("id") and item.get("execution_status") == "error"
    ]
    return {
        "total_count": len(items),
        "completed_count": statuses.count("completed"),
        "incomplete_count": len(incomplete_ids),
        "error_count": len(error_ids),
        "not_started_count": statuses.count("not_started"),
        "running_count": statuses.count("running"),
        "incomplete_criteria": incomplete_ids,
        "error_criteria": error_ids,
    }


def initialize_task_criteria(
    client_dir: str,
    task_id: str,
    criteria: list[dict[str, Any]],
) -> dict[str, Any]:
    entries = [
        {
            "id": str(item["id"]),
            "criterion": str(item.get("criterion") or ""),
            "execution_status": "not_started",
            "review_result": None,
            "error": None,
        }
        for item in criteria
    ]
    return update_task(client_dir, task_id, criteria=entries, quality=summarize_criteria(entries))


def update_task_criterion(
    client_dir: str,
    task_id: str,
    criterion_id: str,
    *,
    execution_status: str,
    review_result: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def apply(task: dict[str, Any]) -> bool:
        criteria = [dict(item) for item in task.get("criteria") or []]
        for item in criteria:
            if str(item.get("id")) == str(criterion_id):
                item.update(execution_status=execution_status, review_result=review_result, error=error)
                break
        else:
            criteria.append({"id": str(criterion_id), "criterion": "", "execution_status": execution_status,
                             "review_result": review_result, "error": error})
        task["criteria"] = criteria
        task["quality"] = summarize_criteria(criteria)
        return True

    updated = mutate_task_if_status(client_dir, task_id, ACTIVE_STATUSES, apply)
    task = updated or read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    return task


def copy_contract_to_result(contract_path: Path | str, result_path: Path | str) -> Path:
    """Atomically copy the original contract to the task's existing result path."""
    source = Path(contract_path)
    target = Path(result_path)
    if source.resolve() == target.resolve():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


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
    copy_contract_to_result(contract_path, result_path)
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
        "queued_at": None,
        "started_at": None,
        "finished_at": None,
        "message": "Review job submitted.",
        "error": None,
        "criteria": [],
        "quality": summarize_criteria([]),
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
        "queue": {
            "task_id": None,
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
            "docxindex_dir": str(docxindex_state_dir(client_dir, task_id)) if LOGGING_ENABLED and LOGGING_DETAILED else None,
        },
        "logs": {
            "api_events_path": str(task_api_log) if task_api_log else None,
            "events_log_path": str(task_api_log) if task_api_log else None,
            "trace_path": str(task_trace_log) if task_trace_log else None,
            "logical_calls_dir": str(task_api_log.parent / "logical_calls") if task_api_log else None,
            "calls_dir": str(task_api_log.parent / "calls") if task_api_log and LOGGING_DETAILED else None,
            "route_events_dir": str(task_api_log.parent / "route_events") if task_api_log and LOGGING_DETAILED else None,
            "review_outputs_path": str(task_review_outputs_log) if task_review_outputs_log else None,
            "annotation_events_path": str(task_annotation_events_log) if task_annotation_events_log else None,
            "conversation_log_dir": str(task_conversation_log) if task_conversation_log and LOGGING_DETAILED else None,
            "mcp_log_path": str(task_mcp_log) if task_mcp_log else None,
        },
    }
    return write_task(task)


def mark_worker_started(client_dir: str, task_id: str, worker_id: str) -> dict[str, Any] | None:
    timestamp = now_iso()

    def apply(task: dict[str, Any]) -> bool:
        worker = task.get("worker") if isinstance(task.get("worker"), dict) else {}
        if worker.get("worker_id"):
            return False
        worker.update(
            {
                "worker_id": worker_id,
                "worker_started_at": timestamp,
                "execution_deadline": task_deadline(),
                "heartbeat_at": timestamp,
                "interrupted_at": None,
            }
        )
        task["worker"] = worker
        return True

    return mutate_task_if_status(client_dir, task_id, {"pending", "queued"}, apply)


def mark_worker_heartbeat(client_dir: str, task_id: str, worker_id: str) -> dict[str, Any] | None:
    def apply(task: dict[str, Any]) -> bool:
        worker = task.get("worker") or {}
        if worker.get("worker_id") != worker_id:
            return False
        worker["heartbeat_at"] = now_iso()
        task["worker"] = worker
        return True
    return mutate_task_if_status(client_dir, task_id, ACTIVE_STATUSES, apply)


def mark_running(client_dir: str, task_id: str) -> dict[str, Any] | None:
    def apply(task: dict[str, Any]) -> bool:
        task.update(status="running", started_at=now_iso(), message="Contract review is running.")
        return True
    return mutate_task_if_status(client_dir, task_id, {"pending", "queued"}, apply)


def mark_queued(client_dir: str, task_id: str, queue_task_id: str | None = None) -> dict[str, Any] | None:
    queued_at = now_iso()

    def apply(task: dict[str, Any]) -> bool:
        task.update(
            status="queued",
            queued_at=queued_at,
            queue={"task_id": queue_task_id},
            message="Review job is waiting for an execution slot.",
        )
        return True

    return mutate_task_if_status(client_dir, task_id, {"pending"}, apply)


def _parse_task_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BEIJING_TZ)
    return parsed


def expire_stale_waiting_tasks(
    *,
    pending_timeout_seconds: float,
    queued_timeout_seconds: float,
    current_time: datetime | None = None,
) -> list[dict[str, Any]]:
    now = current_time or datetime.now(BEIJING_TZ)
    expired: list[dict[str, Any]] = []

    for task in iter_active_tasks():
        status = str(task.get("status") or "")
        timeout_seconds = pending_timeout_seconds if status == "pending" else queued_timeout_seconds
        if status not in {"pending", "queued"} or timeout_seconds <= 0:
            continue
        worker = task.get("worker") if isinstance(task.get("worker"), dict) else {}
        if worker.get("worker_id"):
            continue
        timestamp = _parse_task_timestamp(
            task.get("created_at") if status == "pending" else task.get("queued_at") or task.get("created_at")
        )
        if timestamp is None or (now - timestamp).total_seconds() <= timeout_seconds:
            continue

        code = "QUEUE_DISPATCH_TIMEOUT" if status == "pending" else "QUEUE_WAIT_TIMEOUT"
        message = (
            "Review job was not dispatched before the pending timeout."
            if status == "pending"
            else "Review job did not start before the queue timeout."
        )

        def mark_timeout(current: dict[str, Any], code=code, message=message,
                         timeout_seconds=timeout_seconds) -> bool:
            current_worker = current.get("worker") if isinstance(current.get("worker"), dict) else {}
            if current_worker.get("worker_id"):
                return False
            current["status"] = "failed"
            current["finished_at"] = now.isoformat(timespec="seconds")
            current["message"] = "Contract review failed."
            current["error"] = {
                "code": code,
                "message": message,
                "component": "queue",
                "timeout_seconds": timeout_seconds,
            }
            return True

        updated = mutate_task_if_status(
            task["client_dir"],
            task["task_id"],
            {status},
            mark_timeout,
        )
        if updated is None:
            continue
        write_task_log_event(
            task["client_dir"],
            task["task_id"],
            "review_queue_timeout",
            previous_status=status,
            timeout_seconds=timeout_seconds,
            error=code,
        )
        expired.append(updated)

    return expired


def mark_succeeded(
    client_dir: str,
    task_id: str,
    *,
    criteria: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    task = read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    final_criteria = criteria if criteria is not None else list(task.get("criteria") or [])
    quality = summarize_criteria(final_criteria)
    incomplete_count = quality["incomplete_count"]
    error_count = quality["error_count"]
    if incomplete_count or error_count:
        message = (
            f"共{quality['total_count']}项：{quality['completed_count']}项完成，"
            f"{incomplete_count}项未完整，{error_count}项失败。"
        )
    else:
        message = f"合同审查完成，共{quality['total_count']}项审查项。"
    def apply(current: dict[str, Any]) -> bool:
        current.update(
            status="succeeded",
            finished_at=now_iso(),
            message=message,
            criteria=final_criteria,
            quality=quality,
            error=None,
        )
        return True
    updated = mutate_task_if_status(client_dir, task_id, ACTIVE_STATUSES, apply)
    return updated or task



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
    def apply(task: dict[str, Any]) -> bool:
        task.update(status="failed", finished_at=now_iso(), message="Contract review failed.", error=error)
        return True
    updated = mutate_task_if_status(client_dir, task_id, ACTIVE_STATUSES, apply)
    task = updated or read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    return task


def request_cancel(client_dir: str, task_id: str) -> dict[str, Any]:
    def apply(task: dict[str, Any]) -> bool:
        # Preserve existing API semantics: already running tasks cannot be cancelled here.
        if (task.get("worker") or {}).get("worker_id"):
            return False
        task["status"] = "cancelled"
        task["finished_at"] = now_iso()
        task["message"] = "Review job cancelled."
        task["cancel"]["requested"] = True
        task["cancel"]["requested_at"] = task["cancel"]["requested_at"] or now_iso()
        task["cancel"]["cancelled_at"] = now_iso()
        return True
    updated = mutate_task_if_status(client_dir, task_id, {"pending", "queued"}, apply)
    task = updated or read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    return task


def is_cancel_requested(client_dir: str, task_id: str) -> bool:
    task = read_task(client_dir, task_id)
    if task is None:
        return False
    return bool(task.get("cancel", {}).get("requested"))


def mark_cancelled(client_dir: str, task_id: str) -> dict[str, Any]:
    timestamp = now_iso()
    def apply(task: dict[str, Any]) -> bool:
        task.update(status="cancelled", finished_at=timestamp, message="Review job cancelled.")
        cancel = task.setdefault("cancel", {})
        cancel.update(requested=True, requested_at=cancel.get("requested_at") or timestamp, cancelled_at=timestamp)
        return True
    updated = mutate_task_if_status(client_dir, task_id, ACTIVE_STATUSES, apply)
    task = updated or read_task(client_dir, task_id)
    if task is None:
        raise FileNotFoundError(task_id)
    return task


def fail_active_task(client_dir: str, task_id: str, *, code: str, detail: str,
                     queue_id: str | None = None, worker_id: str | None = None) -> dict[str, Any] | None:
    def apply(task: dict[str, Any]) -> bool:
        stored_queue_id = (task.get("queue") or {}).get("task_id")
        if queue_id and queue_id != stored_queue_id:
            return False
        if worker_id and worker_id != (task.get("worker") or {}).get("worker_id"):
            return False
        task.update(status="failed", finished_at=now_iso(), message="Contract review failed.",
                    error={"code": code, "message": detail, "component": "worker"})
        return True
    return mutate_task_if_status(client_dir, task_id, ACTIVE_STATUSES, apply)


def expire_executing_tasks() -> list[dict[str, Any]]:
    expired = []
    for task in iter_active_tasks():
        worker = task.get("worker") or {}
        deadline = worker.get("execution_deadline")
        if deadline is not None and time.time() > float(deadline) + 5:
            updated = fail_active_task(task["client_dir"], task["task_id"],
                code="TASK_TIME_LIMIT_EXCEEDED", detail="Contract execution deadline expired; worker failure reconciled.",
                queue_id=(task.get("queue") or {}).get("task_id"), worker_id=worker.get("worker_id"))
            if updated:
                expired.append(updated)
    return expired
