"""Download versioned tokenizer resources before building an offline image."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "bge-m3": ("BAAI/bge-m3", 8192),
    "bge-reranker-v2-m3": ("BAAI/bge-reranker-v2-m3", 8192),
    "qwen3-embedding": ("Qwen/Qwen3-Embedding-8B", 32768),
    "qwen3-reranker": ("Qwen/Qwen3-Reranker-8B", 32768),
    "qwen3.6-35b-a3b": ("Qwen/Qwen3.6-35B-A3B", 262144),
}
FILES = {
    "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    "sentencepiece.bpe.model", "tokenizer.model", "vocab.json", "merges.txt",
    "added_tokens.json", "chat_template.jinja", "config.json", "LICENSE",
}


def download_one(name: str, destination: Path) -> tuple[str, dict]:
    repo, context = MODELS[name]
    manifest_path = destination / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        revision = (existing.get("models", {}).get(name) or {}).get("revision")
        response = client.get(f"https://huggingface.co/api/models/{repo}" + (f"/revision/{revision}" if revision else ""))
        response.raise_for_status()
        metadata = response.json()
        revision = metadata["sha"]
        directory = destination / name
        directory.mkdir(parents=True, exist_ok=True)
        records = {}
        for item in metadata["siblings"]:
            filename = item["rfilename"]
            if filename not in FILES:
                continue
            target = directory / filename
            old = ((existing.get("models", {}).get(name) or {}).get("files") or {}).get(filename)
            if old and target.exists() and sha256(target.read_bytes()).hexdigest() == old["sha256"]:
                records[filename] = old
                continue
            response = client.get(f"https://huggingface.co/{repo}/resolve/{revision}/{filename}")
            response.raise_for_status()
            data = response.content
            if filename.endswith(".json"):
                json.loads(data)
            if data.startswith(b"version https://git-lfs.github.com/spec"):
                raise RuntimeError(f"Received an LFS pointer instead of tokenizer data: {repo}/{filename}")
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
            records[filename] = {"sha256": sha256(data).hexdigest(), "bytes": len(data)}
            print(f"downloaded {name}/{filename}: {len(data)} bytes", flush=True)
        if not {"tokenizer.json", "tokenizer_config.json"} <= records.keys():
            raise RuntimeError(f"Tokenizer files are incomplete for {repo}")
        return name, {"repository": repo, "revision": revision, "context_tokens": context, "files": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "resources" / "tokenizers")
    parser.add_argument("--verify", action="store_true", help="Check hashes and load all tokenizers without downloading.")
    args = parser.parse_args()
    if args.verify:
        # Verification is also run in an isolated Docker build step without a network.
        from transformers import AutoTokenizer

        manifest = json.loads((args.destination / "manifest.json").read_text(encoding="utf-8"))
        if set(manifest["models"]) != set(MODELS):
            raise RuntimeError("Offline manifest must contain all five required models")
        for name, entry in manifest["models"].items():
            directory = args.destination / name
            for filename, expected in entry["files"].items():
                if sha256((directory / filename).read_bytes()).hexdigest() != expected["sha256"]:
                    raise RuntimeError(f"Tokenizer checksum mismatch: {name}/{filename}")
            tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, use_fast=True,
                                                        trust_remote_code=False)
            ids = tokenizer.encode("合同运输费用由甲方承担。 Test 123!", add_special_tokens=True)
            if not ids:
                raise RuntimeError(f"Tokenizer returned no tokens: {name}")
            print(json.dumps({"model": name, "revision": entry["revision"], "test_tokens": len(ids)}), flush=True)
        return
    args.destination.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        models = dict(pool.map(lambda name: download_one(name, args.destination), MODELS))
    manifest = {"version": 1, "models": models}
    (args.destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"models": len(models), "bytes": sum(f["bytes"] for m in models.values() for f in m["files"].values())}))


if __name__ == "__main__":
    main()
