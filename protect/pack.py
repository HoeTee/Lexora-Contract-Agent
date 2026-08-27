from __future__ import annotations

import argparse
import importlib.util
import json
import marshal
import os
import struct
import sys
import zipfile
from io import BytesIO
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PACKAGE_MAGIC = b"LEXORA1\0"
NONCE_SIZE = 12
KEY_SIZE = 32
PYC_HEADER_SIZE = 16


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile and encrypt project Python modules.")
    parser.add_argument("--root", type=Path, required=True, help="Project source root.")
    parser.add_argument("--modules-file", type=Path, required=True, help="Protected root allowlist.")
    parser.add_argument("--key-file", type=Path, required=True, help="Raw 32-byte AES key.")
    parser.add_argument("--output", type=Path, required=True, help="Encrypted package output path.")
    parser.add_argument("--key-header", type=Path, required=True, help="Generated C++ key header path.")
    return parser.parse_args()


def load_key(path: Path) -> bytes:
    key = path.read_bytes()
    if len(key) != KEY_SIZE:
        raise ValueError(f"AES key must contain exactly {KEY_SIZE} bytes; received {len(key)} bytes.")
    return key


def load_roots(root: Path, modules_file: Path) -> list[Path]:
    project_root = root.resolve()
    selected: list[Path] = []
    for line in modules_file.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        candidate = (project_root / item).resolve()
        if project_root not in candidate.parents and candidate != project_root:
            raise ValueError(f"Protected path escapes project root: {item}")
        if not candidate.exists():
            raise FileNotFoundError(f"Protected path does not exist: {candidate}")
        selected.append(candidate)
    if not selected:
        raise ValueError("Protected module allowlist is empty.")
    return selected


def collect_python_files(roots: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for selected in roots:
        if selected.is_file():
            if selected.suffix != ".py":
                raise ValueError(f"Protected file is not Python source: {selected}")
            files.add(selected)
            continue
        files.update(path for path in selected.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def module_metadata(project_root: Path, source_path: Path) -> tuple[str, bool, str]:
    relative = source_path.relative_to(project_root)
    package = relative.name == "__init__.py"
    module_parts = relative.parent.parts if package else relative.with_suffix("").parts
    module_name = ".".join(module_parts)
    if not module_name:
        raise ValueError(f"Cannot derive module name from {source_path}")
    origin = "/app/" + relative.with_suffix(".pyc").as_posix()
    return module_name, package, origin


def compile_pyc(source_path: Path, origin: str) -> bytes:
    source = source_path.read_text(encoding="utf-8-sig")
    code = compile(source, origin.removesuffix("c"), "exec", dont_inherit=True, optimize=2)
    header = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, 0)
    payload = header + marshal.dumps(code)
    if len(payload) <= PYC_HEADER_SIZE:
        raise RuntimeError(f"Compiled payload is empty: {source_path}")
    return payload


def build_archive(project_root: Path, python_files: list[Path]) -> bytes:
    manifest: dict[str, dict[str, object]] = {}
    archive_buffer = BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for source_path in python_files:
            module_name, package, origin = module_metadata(project_root, source_path)
            entry = "modules/" + module_name.replace(".", "/") + ("/__init__.pyc" if package else ".pyc")
            if module_name in manifest:
                raise ValueError(f"Duplicate protected module: {module_name}")
            info = zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            archive.writestr(info, compile_pyc(source_path, origin))
            manifest[module_name] = {
                "entry": entry,
                "namespace": False,
                "package": package,
                "origin": origin,
            }

        namespace_packages: set[str] = set()
        for module_name in manifest:
            parts = module_name.split(".")
            namespace_packages.update(".".join(parts[:index]) for index in range(1, len(parts)))
        for module_name in sorted(namespace_packages):
            if module_name in manifest:
                continue
            manifest[module_name] = {
                "entry": None,
                "namespace": True,
                "package": True,
                "origin": "/app/" + module_name.replace(".", "/"),
            }

        manifest_info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_STORED
        manifest_info.external_attr = 0o600 << 16
        archive.writestr(
            manifest_info,
            json.dumps(
                {
                    "format": 1,
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                    "modules": manifest,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
    return archive_buffer.getvalue()


def encrypt_archive(archive: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, archive, PACKAGE_MAGIC)
    return PACKAGE_MAGIC + nonce + ciphertext


def write_key_header(path: Path, key: bytes) -> None:
    mask = os.urandom(KEY_SIZE)
    masked_key = bytes(left ^ right for left, right in zip(key, mask, strict=True))

    def render(name: str, value: bytes) -> str:
        values = ", ".join(f"0x{item:02x}" for item in value)
        return f"inline constexpr std::array<unsigned char, {KEY_SIZE}> {name} = {{{values}}};"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#pragma once\n#include <array>\n\nnamespace lexora::protected_key {\n"
        + render("part_a", mask)
        + "\n"
        + render("part_b", masked_key)
        + "\n}\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    project_root = args.root.resolve()
    python_files = collect_python_files(load_roots(project_root, args.modules_file.resolve()))
    key = load_key(args.key_file.resolve())
    archive = build_archive(project_root, python_files)
    encrypted = encrypt_archive(archive, key)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encrypted)
    write_key_header(args.key_header, key)
    print(f"protected_modules={len(python_files)} encrypted_bytes={len(encrypted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
