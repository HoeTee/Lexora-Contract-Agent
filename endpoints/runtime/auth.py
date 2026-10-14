from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any


PBKDF2_ITERATIONS = 260_000
VALID_ROLES = {"user", "admin"}


def hash_password(
    password: str, 
    *, 
    salt: bytes | None = None
) -> str:
    salt = salt or os.urandom(16) # generate 16 random bytes
    digest = hashlib.pbkdf2_hmac( # python standard hash calc function, with parameters of hash algorithm ("sh256"), byptes converted from password, salt bytes, and iterations
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    ) # The logic is: password (string) -> PBKDF2 iterations-> digest (bytes), with the salt and iterations adding security. 
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"), # bytes -> ASCII string for salt
        base64.b64encode(digest).decode("ascii"), # bytes -> ASCII string for digest
    )


def verify_password(
    password: str, 
    stored_hash: str
) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3) # split 3 times at most
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii")) # ASCII string -> bytes for salt
        expected = base64.b64decode(digest_b64.encode("ascii")) # ASCII string -> bytes for expected digest
        actual = hashlib.pbkdf2_hmac( # password string -> bytes for actual digest
            "sha256",
            password.encode("utf-8"), 
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected) # return True if the two digests match
    except Exception:
        return False

# json.loads(Pathlib.read_text())
def load_users(users_file: str | Path) -> list[dict[str, Any]]:
    path = Path(users_file)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8")) # raw string of the users.json file -> dict of user metadata, including username, password_hash, enabled status, and any other relevant info.
    users = data.get("users", [])
    if not isinstance(users, list):
        raise ValueError("users.json must contain a users list.")
    return users # list of dicts, each dict contains user metadata like username, password_hash, enabled status, etc.

# Pathlib.write_text(json.dumps(...))
def save_users(
    users_file: str | Path, 
    users: list[dict[str, Any]]
) -> None:
    path = Path(users_file)
    path.parent.mkdir(parents=True, exist_ok=True) # if the dir with users.json does not exist
    path.write_text(
        json.dumps({"users": users}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    ) # save by writing into users.json


def find_user(
    users_file: str | Path, 
    username: str
) -> dict[str, Any] | None:
    for user in load_users(users_file):
        if user.get("username") == username:
            return user
    return None


def normalize_role(role: str | None) -> str:
    value = (role or "user").strip().lower()
    return value if value in VALID_ROLES else "user"


def verify_login(
    users_file: str | Path, 
    username: str, 
    password: str
) -> dict[str, Any] | None:
    user = find_user(users_file, username) # a dict of user metadata if found, otherwise None
    if not user or not user.get("enabled", True):
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    user["role"] = normalize_role(user.get("role"))
    return user
