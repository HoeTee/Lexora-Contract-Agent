from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from endpoints.runtime.tenancy import validate_tenant_id


def safe_path_part(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or fallback


@dataclass(frozen=True)
class ResolvedReviewTaskPaths:
    username: str
    original_filename: str
    data_dir: Path
    created_at: datetime
    task_id: str
    safe_contract_stem: str
    tenant_id: str = ""

    @property
    def date_str(self) -> str:
        return self.created_at.strftime("%Y-%m-%d")

    @property
    def file_prefix(self) -> str:
        return f"{self.created_at.strftime('%Y%m%d')}_{self.task_id}"

    @property
    def task_name(self) -> str:
        return f"{self.task_id}_{self.safe_contract_stem}"

    @property
    def user_root(self) -> Path:
        if self.tenant_id:
            return self.data_dir / "web" / self.tenant_id
        return self.data_dir / self.username

    @property
    def task_dir(self) -> Path:
        if self.tenant_id:
            return self.user_root / self.task_id
        return self.task_log_dir

    @property
    def input_dir(self) -> Path:
        return self.task_dir / "input"

    @property
    def output_dir(self) -> Path:
        return self.task_dir / "output"

    @property
    def logs_dir(self) -> Path:
        if self.tenant_id:
            return self.task_dir / "logs"
        return self.user_root / "logs"

    @property
    def contracts_dir(self) -> Path:
        return self.input_dir if self.tenant_id else self.user_root / "contracts"

    @property
    def reports_docx_dir(self) -> Path:
        return self.output_dir if self.tenant_id else self.user_root / "reports_docx"

    @property
    def stored_contract_path(self) -> Path:
        if self.tenant_id:
            return self.input_dir / self.original_filename
        return self.contracts_dir / f"{self.file_prefix}_{self.original_filename}"

    @property
    def final_report_path(self) -> Path:
        filename = f"{self.safe_contract_stem}_reviewed.docx"
        if self.tenant_id:
            return self.output_dir / filename
        return self.reports_docx_dir / f"{self.file_prefix}_{filename}"

    @property
    def task_log_dir(self) -> Path:
        if self.tenant_id:
            return self.logs_dir
        return self.logs_dir / self.date_str / self.task_name

    @property
    def review_outputs_path(self) -> Path:
        return self.task_log_dir / "review_outputs.json"

    @property
    def conversation_log_dir(self) -> Path:
        return self.task_log_dir / "conversations"

    @property
    def mcp_log_path(self) -> Path:
        return self.task_log_dir / "mcp.log"

    @property
    def api_events_path(self) -> Path:
        return self.task_log_dir / "events.log"

    @property
    def trace_path(self) -> Path:
        return self.task_log_dir / "trace.json"

    @property
    def task_json_path(self) -> Path:
        return self.task_dir / "task.json"

    def ensure_user_dirs(self) -> None:
        if self.tenant_id:
            for path in (self.input_dir, self.output_dir, self.task_log_dir, self.conversation_log_dir):
                path.mkdir(parents=True, exist_ok=True)
            return

        for path in (
            self.user_root / "criteria",
            self.contracts_dir,
            self.reports_docx_dir,
            self.logs_dir,
            self.user_root / "records",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def ensure_task_dirs(self) -> None:
        self.ensure_user_dirs()
        for path in (self.task_log_dir, self.conversation_log_dir):
            path.mkdir(parents=True, exist_ok=True)


def resolve_review_task_paths(
    *,
    username: str,
    original_filename: str,
    data_dir: Path,
    tenant_id: str = "",
) -> ResolvedReviewTaskPaths:
    created_at = datetime.now()
    safe_filename = Path(original_filename.replace("\\", "/")).name
    stem = safe_path_part(Path(safe_filename).stem, "contract")
    task_id = f"{created_at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    return ResolvedReviewTaskPaths(
        tenant_id=validate_tenant_id(tenant_id) if tenant_id else "",
        username=safe_path_part(username, "user"),
        original_filename=safe_filename,
        data_dir=data_dir,
        created_at=created_at,
        task_id=task_id,
        safe_contract_stem=stem,
    )


def initialize_user_data_dir(data_dir: Path, username: str, *, tenant_id: str = "") -> Path:
    safe_username = safe_path_part(username, "user")
    if tenant_id:
        user_root = data_dir / "web" / validate_tenant_id(tenant_id)
        user_root.mkdir(parents=True, exist_ok=True)
        return user_root

    user_root = data_dir / safe_username
    for child in ("criteria", "contracts", "reports_docx", "logs", "records"):
        (user_root / child).mkdir(parents=True, exist_ok=True)
    return user_root
