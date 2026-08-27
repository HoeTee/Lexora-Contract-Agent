from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DATA_DIR
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


def users_file_for_tenant(tenant_id: str) -> Path:
    require_tenant(tenant_id)
    return tenant_profiles_file(tenant_id)


def create_user_account(
    *,
    tenant_id: str,
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    data_dir: str | Path = DATA_DIR,
) -> str:
    users_file = users_file_for_tenant(tenant_id)
    safe_username = safe_path_part(username, "user")
    normalized_role = validate_role(role)
    users = load_users(users_file)
    if find_user_index(users, safe_username) is not None:
        raise UserManagementError(f"User already exists: {tenant_id}/{safe_username}")

    users.append(
        {
            "tenant_id": tenant_id,
            "username": safe_username,
            "password_hash": hash_password(password),
            "display_name": display_name or safe_username,
            "role": normalized_role,
            "enabled": True,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_users(users_file, users)
    initialize_user_data_dir(Path(data_dir), safe_username, tenant_id=tenant_id)
    return safe_username


def set_user_role(*, tenant_id: str, username: str, role: str) -> str:
    users_file = users_file_for_tenant(tenant_id)
    normalized_role = validate_role(role)
    users = load_users(users_file)
    index = find_user_index(users, username)
    if index is None:
        raise UserManagementError(f"User not found: {tenant_id}/{username}")
    users[index]["role"] = normalized_role
    users[index]["role_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_users(users_file, users)
    return normalized_role


def reset_user_password(*, tenant_id: str, username: str, password: str) -> None:
    users_file = users_file_for_tenant(tenant_id)
    users = load_users(users_file)
    index = find_user_index(users, username)
    if index is None:
        raise UserManagementError(f"User not found: {tenant_id}/{username}")
    users[index]["password_hash"] = hash_password(password)
    users[index]["password_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_users(users_file, users)


def set_user_enabled(*, tenant_id: str, username: str, enabled: bool, data_dir: str | Path = DATA_DIR) -> None:
    users_file = users_file_for_tenant(tenant_id)
    users = load_users(users_file)
    index = find_user_index(users, username)
    if index is None:
        raise UserManagementError(f"User not found: {tenant_id}/{username}")
    users[index]["enabled"] = enabled
    key = "enabled_at" if enabled else "disabled_at"
    users[index][key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_users(users_file, users)
    if enabled:
        initialize_user_data_dir(Path(data_dir), username, tenant_id=tenant_id)


def delete_user_account(*, tenant_id: str, username: str) -> None:
    users_file = users_file_for_tenant(tenant_id)
    safe_username = safe_path_part(username, "user")
    users = load_users(users_file)
    index = find_user_index(users, safe_username)
    if index is None:
        raise UserManagementError(f"User not found: {tenant_id}/{safe_username}")
    del users[index]
    save_users(users_file, users)


def list_users(*, tenant_id: str) -> list[dict]:
    users_file = users_file_for_tenant(tenant_id)
    return load_users(users_file)


def create_user(args) -> None:
    try:
        username = create_user_account(
            tenant_id=args.tenant_id,
            username=args.username,
            password=args.password,
            display_name=args.display_name,
            role=args.role,
        )
    except Exception as exc:
        raise SystemExit(str(exc))
    print(f"Created user: {args.tenant_id}/{username}")


def list_users_command(args) -> None:
    try:
        users = list_users(tenant_id=args.tenant_id)
    except Exception as exc:
        raise SystemExit(str(exc))
    print("tenant_id\tusername\tenabled\trole\tdisplay_name\tcreated_at")
    for user in users:
        print(
            f"{args.tenant_id}\t"
            f"{user.get('username', '')}\t"
            f"{str(bool(user.get('enabled', True))).lower()}\t"
            f"{user.get('role', '')}\t"
            f"{user.get('display_name', '')}\t"
            f"{user.get('created_at', '')}"
        )


def set_role(args) -> None:
    try:
        role = set_user_role(tenant_id=args.tenant_id, username=args.username, role=args.role)
    except Exception as exc:
        raise SystemExit(str(exc))
    print(f"Updated user role: {args.tenant_id}/{args.username} -> {role}")


def reset_password(args) -> None:
    try:
        reset_user_password(tenant_id=args.tenant_id, username=args.username, password=args.password)
    except Exception as exc:
        raise SystemExit(str(exc))
    print(f"Reset user password: {args.tenant_id}/{args.username}")


def disable_user(args) -> None:
    try:
        set_user_enabled(tenant_id=args.tenant_id, username=args.username, enabled=False)
    except Exception as exc:
        raise SystemExit(str(exc))
    print(f"Disabled user: {args.tenant_id}/{args.username}")


def enable_user(args) -> None:
    try:
        set_user_enabled(tenant_id=args.tenant_id, username=args.username, enabled=True)
    except Exception as exc:
        raise SystemExit(str(exc))
    print(f"Enabled user: {args.tenant_id}/{args.username}")


def delete_user(args) -> None:
    try:
        delete_user_account(tenant_id=args.tenant_id, username=args.username)
    except Exception as exc:
        raise SystemExit(str(exc))
    print(f"Deleted user profile: {args.tenant_id}/{args.username}")
    print("Task data was not deleted.")


def add_tenant_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage web users for one tenant.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a user")
    add_tenant_arg(create)
    create.add_argument("--username", required=True)
    create.add_argument("--password", required=True)
    create.add_argument("--display-name", default="")
    create.add_argument("--role", choices=sorted(VALID_ROLES), default="user")
    create.set_defaults(func=create_user)

    list_parser = subparsers.add_parser("list", help="List users")
    add_tenant_arg(list_parser)
    list_parser.set_defaults(func=list_users_command)

    role = subparsers.add_parser("set-role", help="Set a user's role")
    add_tenant_arg(role)
    role.add_argument("--username", required=True)
    role.add_argument("--role", choices=sorted(VALID_ROLES), required=True)
    role.set_defaults(func=set_role)

    reset = subparsers.add_parser("reset-password", help="Reset a user's password")
    add_tenant_arg(reset)
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", required=True)
    reset.set_defaults(func=reset_password)

    disable = subparsers.add_parser("disable", help="Disable a user")
    add_tenant_arg(disable)
    disable.add_argument("--username", required=True)
    disable.set_defaults(func=disable_user)

    enable = subparsers.add_parser("enable", help="Enable a user")
    add_tenant_arg(enable)
    enable.add_argument("--username", required=True)
    enable.set_defaults(func=enable_user)

    delete = subparsers.add_parser("delete", help="Delete a user profile")
    add_tenant_arg(delete)
    delete.add_argument("--username", required=True)
    delete.set_defaults(func=delete_user)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
