from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_CLIENTS_FILE = PROJECT_ROOT / "profiles" / "api_clients.json"


@dataclass(frozen=True)
class ApiClient:
    client_id: str
    client_dir: str
    source_ip: str


class ApiClientMappingError(Exception):
    pass


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
        raise ApiClientMappingError("api_clients.json must contain a clients list.")
    return clients


def _request_source_ip(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def _platform_api_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def api_key_fingerprint(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()


async def resolve_api_client(request: Request) -> ApiClient:
    api_key = _platform_api_key(request)
    if not api_key:
        raise HTTPException(status_code=400, detail="Authorization header is required.")

    try:
        clients = load_api_clients()
    except ApiClientMappingError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    fingerprint = api_key_fingerprint(api_key)
    matches = [client for client in clients if client.get("api_key_fingerprint") == fingerprint]
    if not matches:
        raise HTTPException(status_code=400, detail="API key has no local client mapping.")
    if len(matches) > 1:
        raise HTTPException(status_code=500, detail="API key maps to multiple clients.")

    client = matches[0]
    if not client.get("enabled", True):
        raise HTTPException(status_code=403, detail="API client mapping is disabled.")

    client_id = str(client.get("client_id") or "").strip()
    if not client_id:
        raise HTTPException(status_code=500, detail="API client record is missing client_id.")
    return ApiClient(
        client_id=client_id,
        client_dir=str(client.get("client_dir") or client_dir_name(client_id)),
        source_ip=_request_source_ip(request),
    )
