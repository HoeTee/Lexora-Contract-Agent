from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from config import DATA_DIR, USERS_FILE
from endpoints.runtime.auth import VALID_ROLES, hash_password, load_users, save_users
from endpoints.runtime.tenancy import require_tenant, tenant_profiles_file
from loggers.resolve_review_task_paths import initialize_user_data_dir, safe_path_part


class UserManagementError(Exception):
    pass


def find_user_index(users: list[dict], username: str) -> int | None:
    for index, user in enumerate(users):
        if user.get("username") == username:
            return index
    return None


def validate_role(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized not in VALID_ROLES:
        raise UserManagementError(f"Role must be user or admin: {role}")
    return normalized


def create_user_account(
    *,
    tenant_id: str | None = None,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    users_file: str | Path = USERS_FILE,
    data_dir: str | Path = DATA_DIR,
) -> str:
    if tenant_id:
        require_tenant(tenant_id)
        users_file = tenant_profiles_file(tenant_id)
    safe_username = safe_path_part(username, "user")
    normalized_role = validate_role(role)
    users = load_users(users_file)
    if find_user_index(users, safe_username) is not None:
        raise UserManagementError(f"User already exists: {safe_username}")

    users.append({
        "tenant_id": tenant_id,
        "username": safe_username,
        "password_hash": hash_password(password),
        "display_name": display_name or safe_username,
        "role": normalized_role,
        "enabled": True,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_users(users_file, users)
    if tenant_id:
        initialize_user_data_dir(Path(data_dir), safe_username, tenant_id=tenant_id)
    else:
        initialize_user_data_dir(Path(data_dir), safe_username)
    return safe_username


def set_user_role(
    *,
    tenant_id: str | None = None,
    username: str,
    role: str,
    users_file: str | Path = USERS_FILE,
) -> str:
    if tenant_id:
        require_tenant(tenant_id)
        users_file = tenant_profiles_file(tenant_id)
    normalized_role = validate_role(role)
    users = load_users(users_file)
    index = find_user_index(users, username)
    if index is None:
        raise UserManagementError(f"User not found: {username}")
    users[index]["role"] = normalized_role
    users[index]["role_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_users(users_file, users)
    return normalized_role


def reset_user_password(
    *,
    tenant_id: str | None = None,
    username: str,
    password: str,
    users_file: str | Path = USERS_FILE,
) -> None:
    if tenant_id:
        require_tenant(tenant_id)
        users_file = tenant_profiles_file(tenant_id)
    users = load_users(users_file)
    index = find_user_index(users, username)
    if index is None:
        raise UserManagementError(f"User not found: {username}")
    users[index]["password_hash"] = hash_password(password)
    users[index]["password_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_users(users_file, users)


def set_user_enabled(
    *,
    tenant_id: str | None = None,
    username: str,
    enabled: bool,
    users_file: str | Path = USERS_FILE,
    data_dir: str | Path = DATA_DIR,
) -> None:
    if tenant_id:
        require_tenant(tenant_id)
        users_file = tenant_profiles_file(tenant_id)
    users = load_users(users_file)
    index = find_user_index(users, username)
    if index is None:
        raise UserManagementError(f"User not found: {username}")
    users[index]["enabled"] = enabled
    key = "enabled_at" if enabled else "disabled_at"
    users[index][key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_users(users_file, users)
    if enabled:
        initialize_user_data_dir(Path(data_dir), username, tenant_id=tenant_id) if tenant_id else initialize_user_data_dir(Path(data_dir), username)


def delete_user_account(
    *,
    tenant_id: str | None = None,
    username: str,
    keep_data: bool = False,
    users_file: str | Path = USERS_FILE,
    data_dir: str | Path = DATA_DIR,
) -> Path | None:
    if tenant_id:
        require_tenant(tenant_id)
        users_file = tenant_profiles_file(tenant_id)
    safe_username = safe_path_part(username, "user")
    users = load_users(users_file)
    index = find_user_index(users, safe_username)
    if index is None:
        raise UserManagementError(f"User not found: {safe_username}")

    del users[index]
    save_users(users_file, users)

    if tenant_id:
        return Path(data_dir) / "web" / tenant_id

    user_root = Path(data_dir) / safe_username
    if keep_data:
        return user_root

    data_root = Path(data_dir).resolve()
    resolved_user_root = user_root.resolve()
    if data_root == resolved_user_root or data_root not in resolved_user_root.parents:
        raise UserManagementError(f"Refusing to delete unsafe path: {resolved_user_root}")

    if resolved_user_root.exists():
        shutil.rmtree(resolved_user_root)
        return resolved_user_root
    return None
