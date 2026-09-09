from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


PROFILES_ROOT = Path(PROJECT_ROOT) / "profiles"
TENANT_PROFILES_FILE = PROFILES_ROOT / "tenant_profiles.json"
TENANTS_ROOT = PROFILES_ROOT / "tenants"
TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TenantError(Exception):
    pass


def validate_tenant_id(tenant_id: str) -> str:
    value = str(tenant_id or "").strip()
    if not TENANT_ID_PATTERN.fullmatch(value):
        raise TenantError("tenant_id must contain only letters, numbers, underscore, or hyphen.")
    return value


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_tenants(tenants_file: str | Path | None = None) -> list[dict[str, Any]]:
    path = Path(tenants_file or TENANT_PROFILES_FILE)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    tenants = data.get("tenants", [])
    if not isinstance(tenants, list):
        raise TenantError("tenant_profiles.json must contain a tenants list.")
    return tenants


def save_tenants(tenants: list[dict[str, Any]], tenants_file: str | Path | None = None) -> None:
    path = Path(tenants_file or TENANT_PROFILES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tenants": tenants}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_tenant(tenant_id: str, tenants_file: str | Path | None = None) -> dict[str, Any] | None:
    tenant_id = validate_tenant_id(tenant_id)
    for tenant in load_tenants(tenants_file):
        if tenant.get("tenant_id") == tenant_id:
            return tenant
    return None


def require_tenant(tenant_id: str, tenants_file: str | Path | None = None) -> dict[str, Any]:
    tenant = find_tenant(tenant_id, tenants_file)
    if tenant is None:
        raise TenantError(f"Tenant not found: {tenant_id}")
    if not tenant.get("enabled", True):
        raise TenantError(f"Tenant is disabled: {tenant_id}")
    return tenant


def tenant_profiles_file(tenant_id: str) -> Path:
    return TENANTS_ROOT / validate_tenant_id(tenant_id) / "profiles.json"


def safe_profile_name(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or fallback


def create_tenant(
    *,
    tenant_id: str,
    name: str,
    tenants_file: str | Path | None = None,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    tenants = load_tenants(tenants_file)
    if any(tenant.get("tenant_id") == tenant_id for tenant in tenants):
        raise TenantError(f"Tenant already exists: {tenant_id}")
    timestamp = now_text()
    tenant = {
        "tenant_id": tenant_id,
        "name": name.strip() or tenant_id,
        "enabled": True,
        "created_at": timestamp,
    }
    tenants.append(tenant)
    save_tenants(tenants, tenants_file)
    tenant_profiles_file(tenant_id).parent.mkdir(parents=True, exist_ok=True)
    return tenant


def set_tenant_enabled(
    *,
    tenant_id: str,
    enabled: bool,
    tenants_file: str | Path | None = None,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    tenants = load_tenants(tenants_file)
    for tenant in tenants:
        if tenant.get("tenant_id") == tenant_id:
            tenant["enabled"] = enabled
            tenant["enabled_updated_at"] = now_text()
            save_tenants(tenants, tenants_file)
            return tenant
    raise TenantError(f"Tenant not found: {tenant_id}")


def delete_tenant(
    *,
    tenant_id: str,
    tenants_file: str | Path | None = None,
) -> dict[str, Any]:
    tenant_id = validate_tenant_id(tenant_id)
    tenants = load_tenants(tenants_file)
    for index, tenant in enumerate(tenants):
        if tenant.get("tenant_id") == tenant_id:
            deleted = tenants.pop(index)
            save_tenants(tenants, tenants_file)
            return deleted
    raise TenantError(f"Tenant not found: {tenant_id}")
