from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from endpoints.runtime.tenancy import (  # noqa: E402
    TenantError,
    create_tenant,
    delete_tenant,
    load_tenants,
    set_tenant_enabled,
)


def create_tenant_command(args) -> None:
    try:
        tenant = create_tenant(tenant_id=args.tenant_id, name=args.name)
    except TenantError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Created tenant: {tenant['tenant_id']} ({tenant['name']})")


def list_tenants_command(args) -> None:
    print("tenant_id\tenabled\tname\tcreated_at")
    for tenant in load_tenants():
        enabled = str(bool(tenant.get("enabled", True))).lower()
        print(
            f"{tenant.get('tenant_id', '')}\t"
            f"{enabled}\t"
            f"{tenant.get('name', '')}\t"
            f"{tenant.get('created_at', '')}"
        )


def enable_tenant_command(args) -> None:
    try:
        tenant = set_tenant_enabled(tenant_id=args.tenant_id, enabled=True)
    except TenantError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Enabled tenant: {tenant['tenant_id']}")


def disable_tenant_command(args) -> None:
    try:
        tenant = set_tenant_enabled(tenant_id=args.tenant_id, enabled=False)
    except TenantError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Disabled tenant: {tenant['tenant_id']}")


def delete_tenant_command(args) -> None:
    try:
        tenant = delete_tenant(tenant_id=args.tenant_id)
    except TenantError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Deleted tenant profile: {tenant['tenant_id']}")
    print("Tenant user profiles and task data were not deleted.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage web tenants.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a tenant")
    create.add_argument("--tenant-id", required=True)
    create.add_argument("--name", required=True)
    create.set_defaults(func=create_tenant_command)

    list_parser = subparsers.add_parser("list", help="List tenants")
    list_parser.set_defaults(func=list_tenants_command)

    enable = subparsers.add_parser("enable", help="Enable a tenant")
    enable.add_argument("--tenant-id", required=True)
    enable.set_defaults(func=enable_tenant_command)

    disable = subparsers.add_parser("disable", help="Disable a tenant")
    disable.add_argument("--tenant-id", required=True)
    disable.set_defaults(func=disable_tenant_command)

    delete = subparsers.add_parser("delete", help="Delete a tenant profile")
    delete.add_argument("--tenant-id", required=True)
    delete.set_defaults(func=delete_tenant_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
