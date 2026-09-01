from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

API_CLIENTS_FILE = PROJECT_ROOT / "profiles" / "api_clients.json"
API_CLIENT_TASKS_ROOT = PROJECT_ROOT / "data" / "api"


class ApiClientCliError(Exception):
    pass


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def client_dir_name(client_id: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", client_id).strip().strip(".")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "api_client"


def load_api_clients(clients_file: str | Path = API_CLIENTS_FILE) -> list[dict[str, Any]]:
    path = Path(clients_file)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    clients = data.get("clients", [])
    if not isinstance(clients, list):
        raise ApiClientCliError("api_clients.json must contain a clients list.")
    return clients


def save_api_clients(
    clients: list[dict[str, Any]],
    clients_file: str | Path = API_CLIENTS_FILE,
) -> None:
    path = Path(clients_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"clients": clients}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_client_index(clients: list[dict[str, Any]], client_id: str) -> int | None:
    for index, client in enumerate(clients):
        if client.get("client_id") == client_id:
            return index
    return None


def find_client(clients: list[dict[str, Any]], client_id: str) -> dict[str, Any] | None:
    index = find_client_index(clients, client_id)
    if index is None:
        return None
    return clients[index]


def api_key_fingerprint(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()


def find_api_key_fingerprint_owner(
    clients: list[dict[str, Any]],
    fingerprint: str,
    *,
    exclude_client_id: str | None = None,
) -> str | None:
    for client in clients:
        if exclude_client_id is not None and client.get("client_id") == exclude_client_id:
            continue
        if client.get("api_key_fingerprint") == fingerprint:
            return str(client.get("client_id") or "")
    return None


def register_api_client(
    *,
    client_id: str,
    api_key: str,
    clients_file: str | Path = API_CLIENTS_FILE,
) -> dict[str, Any]:
    client_id = client_id.strip()
    api_key = api_key.strip()
    if not client_id:
        raise ApiClientCliError("client_id is required.")
    if not api_key:
        raise ApiClientCliError("api_key is required.")

    clients = load_api_clients(clients_file)
    if find_client_index(clients, client_id) is not None:
        raise ApiClientCliError(f"API client already exists: {client_id}")
    fingerprint = api_key_fingerprint(api_key)
    owner = find_api_key_fingerprint_owner(clients, fingerprint)
    if owner:
        raise ApiClientCliError(f"api_key is already registered for API client: {owner}")

    timestamp = now_text()
    client = {
        "client_id": client_id,
        "client_dir": client_dir_name(client_id),
        "api_key_fingerprint": fingerprint,
        "enabled": True,
        "created_at": timestamp,
        "api_key_updated_at": timestamp,
    }
    clients.append(client)
    save_api_clients(clients, clients_file)
    return client


def reset_api_client_api_key(
    *,
    client_id: str,
    api_key: str,
    clients_file: str | Path = API_CLIENTS_FILE,
) -> dict[str, Any]:
    api_key = api_key.strip()
    if not api_key:
        raise ApiClientCliError("api_key is required.")

    clients = load_api_clients(clients_file)
    index = find_client_index(clients, client_id)
    if index is None:
        raise ApiClientCliError(f"API client not found: {client_id}")

    fingerprint = api_key_fingerprint(api_key)
    owner = find_api_key_fingerprint_owner(clients, fingerprint, exclude_client_id=client_id)
    if owner:
        raise ApiClientCliError(f"api_key is already registered for API client: {owner}")

    clients[index].pop("secret_hash", None)
    clients[index].pop("secret_fingerprint", None)
    clients[index].pop("secret_updated_at", None)
    clients[index]["api_key_fingerprint"] = fingerprint
    clients[index]["api_key_updated_at"] = now_text()
    clients[index]["client_dir"] = clients[index].get("client_dir") or client_dir_name(client_id)
    save_api_clients(clients, clients_file)
    return clients[index]


def set_api_client_enabled(
    *,
    client_id: str,
    enabled: bool,
    clients_file: str | Path = API_CLIENTS_FILE,
) -> dict[str, Any]:
    clients = load_api_clients(clients_file)
    index = find_client_index(clients, client_id)
    if index is None:
        raise ApiClientCliError(f"API client not found: {client_id}")
    clients[index]["enabled"] = enabled
    clients[index]["enabled_updated_at"] = now_text()
    clients[index]["client_dir"] = clients[index].get("client_dir") or client_dir_name(client_id)
    save_api_clients(clients, clients_file)
    return clients[index]


def delete_api_client(
    *,
    client_id: str,
    clients_file: str | Path = API_CLIENTS_FILE,
) -> dict[str, Any]:
    clients = load_api_clients(clients_file)
    index = find_client_index(clients, client_id)
    if index is None:
        raise ApiClientCliError(f"API client not found: {client_id}")
    client = clients.pop(index)
    save_api_clients(clients, clients_file)
    return client


def task_root_for_client(client: dict[str, Any]) -> Path:
    return API_CLIENT_TASKS_ROOT


def load_client_tasks(client: dict[str, Any]) -> list[dict[str, Any]]:
    client_id = str(client.get("client_id") or "")
    root = task_root_for_client(client)
    if not root.exists():
        return []
    tasks: list[dict[str, Any]] = []
    for task_json in sorted(root.glob("*/task.json")):
        try:
            task = json.loads(task_json.read_text(encoding="utf-8"))
        except Exception as exc:
            tasks.append(
                {
                    "task_id": task_json.parent.name,
                    "status": "unreadable",
                    "error": repr(exc),
                }
            )
            continue
        if isinstance(task, dict) and str(task.get("client_id") or "") == client_id:
            tasks.append(task)
    return tasks


def task_display_fields(task: dict[str, Any]) -> dict[str, str]:
    input_data = task.get("input", {}) if isinstance(task.get("input"), dict) else {}
    output_data = task.get("output", {}) if isinstance(task.get("output"), dict) else {}
    return {
        "task_id": str(task.get("task_id") or ""),
        "status": str(task.get("status") or ""),
        "created_at": str(task.get("created_at") or ""),
        "started_at": str(task.get("started_at") or ""),
        "finished_at": str(task.get("finished_at") or ""),
        "contract_filename": str(input_data.get("contract_filename") or ""),
        "result_filename": str(output_data.get("result_filename") or ""),
    }


def ellipsize(value: str, max_length: int = 28) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return "." * max_length
    return value[: max_length - 3] + "..."


def print_tasks_block(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("No tasks found.")
        return
    for index, task in enumerate(tasks):
        fields = task_display_fields(task)
        if index:
            print("---")
        print(f"task_id: {fields['task_id']}")
        print(f"status: {fields['status']}")
        print(f"created_at: {fields['created_at']}")
        print(f"started_at: {fields['started_at']}")
        print(f"finished_at: {fields['finished_at']}")
        print(f"contract_filename: {fields['contract_filename']}")
        print(f"result_filename: {fields['result_filename']}")


def print_tasks_compact(tasks: list[dict[str, Any]]) -> None:
    print(
        f"{'task_id':<22} "
        f"{'status':<10} "
        f"{'created_at':<25} "
        f"{'finished_at':<25} "
        f"{'contract_filename':<31} "
        "result_filename"
    )
    for task in tasks:
        fields = task_display_fields(task)
        print(
            f"{ellipsize(fields['task_id'], 22):<22} "
            f"{ellipsize(fields['status'], 10):<10} "
            f"{ellipsize(fields['created_at'], 25):<25} "
            f"{ellipsize(fields['finished_at'], 25):<25} "
            f"{ellipsize(fields['contract_filename'], 31):<31} "
            f"{ellipsize(fields['result_filename'], 31)}"
        )


def register_client(args) -> None:
    try:
        client = register_api_client(client_id=args.client_id, api_key=args.api_key)
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Registered API client: {client['client_id']}")


def reset_api_key(args) -> None:
    try:
        client = reset_api_client_api_key(client_id=args.client_id, api_key=args.api_key)
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Reset API key mapping for API client: {client['client_id']}")


def list_clients(args) -> None:
    try:
        clients = load_api_clients()
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None

    if args.client_id:
        client = find_client(clients, args.client_id)
        if client is None:
            raise SystemExit(f"API client not found: {args.client_id}")
        tasks = load_client_tasks(client)
        if args.compact:
            print_tasks_compact(tasks)
        else:
            print_tasks_block(tasks)
        return

    print("client_id\tenabled\tclient_dir\tcreated_at\tapi_key_updated_at")
    for client in clients:
        enabled = str(bool(client.get("enabled", True))).lower()
        print(
            f"{client.get('client_id', '')}\t"
            f"{enabled}\t"
            f"{client.get('client_dir', '')}\t"
            f"{client.get('created_at', '')}\t"
            f"{client.get('api_key_updated_at', '')}"
        )


def check_client(args) -> None:
    try:
        clients = load_api_clients()
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None
    client = find_client(clients, args.client_id)
    if client is None:
        raise SystemExit(f"API client not found: {args.client_id}")
    tasks = load_client_tasks(client)
    print(f"client_id: {client.get('client_id', '')}")
    print(f"client_dir: {client.get('client_dir', '')}")
    print(f"enabled: {str(bool(client.get('enabled', True))).lower()}")
    print(f"created_at: {client.get('created_at', '')}")
    print(f"api_key_updated_at: {client.get('api_key_updated_at', '')}")
    print(f"has_api_key_fingerprint: {str(bool(client.get('api_key_fingerprint'))).lower()}")
    print(f"task_count: {len(tasks)}")
    print(f"task_root: {task_root_for_client(client)}")


def disable_client(args) -> None:
    try:
        client = set_api_client_enabled(client_id=args.client_id, enabled=False)
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Disabled API client: {client['client_id']}")


def enable_client(args) -> None:
    try:
        client = set_api_client_enabled(client_id=args.client_id, enabled=True)
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Enabled API client: {client['client_id']}")


def delete_client(args) -> None:
    try:
        client = delete_api_client(client_id=args.client_id)
    except ApiClientCliError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Deleted API client: {client['client_id']}")
    print("Task data was not deleted.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage API clients.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="Register an API client platform key mapping")
    register.add_argument("--client-id", required=True)
    register.add_argument("--api-key", required=True)
    register.set_defaults(func=register_client)

    reset = subparsers.add_parser("reset-api-key", help="Replace an API client platform key mapping")
    reset.add_argument("--client-id", required=True)
    reset.add_argument("--api-key", required=True)
    reset.set_defaults(func=reset_api_key)

    list_parser = subparsers.add_parser("list", help="List API clients")
    list_parser.add_argument("--client-id")
    list_parser.add_argument("--compact", action="store_true", help="Print client tasks in one-line compact rows")
    list_parser.set_defaults(func=list_clients)

    check = subparsers.add_parser("check", help="Check one API client")
    check.add_argument("--client-id", required=True)
    check.set_defaults(func=check_client)

    disable = subparsers.add_parser("disable", help="Disable an API client")
    disable.add_argument("--client-id", required=True)
    disable.set_defaults(func=disable_client)

    enable = subparsers.add_parser("enable", help="Enable an API client")
    enable.add_argument("--client-id", required=True)
    enable.set_defaults(func=enable_client)

    delete = subparsers.add_parser("delete", help="Delete an API client mapping")
    delete.add_argument("--client-id", required=True)
    delete.set_defaults(func=delete_client)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
