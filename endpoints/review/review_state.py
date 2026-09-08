from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from endpoints.review.task_store import (
    criterion_error_state_path,
    criterion_input_state_path,
    criterion_output_state_path,
    criteria_state_dir,
    now_iso,
    plan_state_path,
    state_dir,
)


CRITERION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
REVIEW_CONTRACT_VERSION = 2


def _safe_criterion_id(criterion_id: str) -> str:
    value = str(criterion_id).strip()
    if not value or not CRITERION_ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid criterion_id: {criterion_id!r}")
    return value


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return path


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    return data


class ReviewStateStore:
    """Persists workflow recovery state under data/<scope>/<task_id>/state."""

    def __init__(self, client_dir: str, task_id: str) -> None:
        self.client_dir = client_dir
        self.task_id = task_id

    @property
    def root_dir(self) -> Path:
        return state_dir(self.client_dir, self.task_id)

    @property
    def criteria_dir(self) -> Path:
        return criteria_state_dir(self.client_dir, self.task_id)

    def ensure_dirs(self) -> None:
        self.criteria_dir.mkdir(parents=True, exist_ok=True)

    def save_plan(self, criteria_list: list[dict[str, Any]], criteria_source: dict | None = None) -> Path:
        return _write_json(
            plan_state_path(self.client_dir, self.task_id),
            {
                "task_id": self.task_id,
                "saved_at": now_iso(),
                "criteria_count": len(criteria_list),
                "criteria": criteria_list,
                "criteria_source": criteria_source or {},
                "review_contract_version": REVIEW_CONTRACT_VERSION,
            },
        )

    def load_plan(self) -> dict[str, Any] | None:
        plan = _read_json(plan_state_path(self.client_dir, self.task_id))
        return plan if plan and plan.get("review_contract_version") == REVIEW_CONTRACT_VERSION else None

    def criterion_input_path(self, criterion_id: str) -> Path:
        return criterion_input_state_path(self.client_dir, self.task_id, _safe_criterion_id(criterion_id))

    def criterion_output_path(self, criterion_id: str) -> Path:
        return criterion_output_state_path(self.client_dir, self.task_id, _safe_criterion_id(criterion_id))

    def criterion_error_path(self, criterion_id: str) -> Path:
        return criterion_error_state_path(self.client_dir, self.task_id, _safe_criterion_id(criterion_id))

    def save_criterion_input(self, criterion: dict[str, Any], *, attempt: int, max_attempts: int) -> Path:
        criterion_id = _safe_criterion_id(str(criterion.get("id", "")))
        return _write_json(
            self.criterion_input_path(criterion_id),
            {
                "task_id": self.task_id,
                "criterion_id": criterion_id,
                "section": criterion.get("section", "其他"),
                "criterion": criterion.get("criterion", ""),
                "check_points": criterion.get("check_points", []),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "saved_at": now_iso(),
            },
        )

    def load_criterion_output(self, criterion_id: str) -> dict[str, Any] | None:
        result = _read_json(self.criterion_output_path(criterion_id))
        return result if result and result.get("review_contract_version") == REVIEW_CONTRACT_VERSION else None

    def save_criterion_output(self, result: dict[str, Any], *, attempt: int) -> Path:
        criterion_id = _safe_criterion_id(str(result.get("criterion_id", "")))
        payload = dict(result)
        payload["review_contract_version"] = REVIEW_CONTRACT_VERSION
        payload.update(
            {
                "task_id": self.task_id,
                "criterion_id": criterion_id,
                "attempt": attempt,
                "saved_at": now_iso(),
            }
        )
        return _write_json(self.criterion_output_path(criterion_id), payload)

    def save_criterion_error(
        self,
        criterion: dict[str, Any],
        exc: BaseException,
        *,
        attempt: int,
        max_attempts: int,
    ) -> Path:
        criterion_id = _safe_criterion_id(str(criterion.get("id", "")))
        previous = _read_json(self.criterion_error_path(criterion_id)) or {}
        attempts = previous.get("attempts") if isinstance(previous.get("attempts"), list) else []
        error = {
            "attempt": attempt,
            "failed_at": now_iso(),
            "error_type": exc.__class__.__name__,
            "message": str(exc),
            "detail": getattr(exc, "detail", str(exc)),
            "http_status": getattr(exc, "http_status", None),
            "component": getattr(exc, "component", None),
            "event_type": getattr(exc, "event_type", None),
        }
        attempts.append(error)
        return _write_json(
            self.criterion_error_path(criterion_id),
            {
                "task_id": self.task_id,
                "criterion_id": criterion_id,
                "section": criterion.get("section", "其他"),
                "criterion": criterion.get("criterion", ""),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "failed_at": error["failed_at"],
                "latest_error": error,
                "attempts": attempts,
            },
        )
