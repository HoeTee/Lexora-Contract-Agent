"""Run three real API/Celery contract jobs per input-limit profile in isolated Compose projects."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx
import yaml


ROOT = Path(__file__).resolve().parents[2]


def command(args, *, check=True):
    result = subprocess.run(args, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
    return result


def setup(destination, limit, image):
    destination.mkdir(parents=True)
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for key in ("embedding", "rerank"):
        config[key]["max_input_tokens"] = limit
    config["logging"]["detailed"] = True
    config["queue"]["broker_url"] = "redis://redis:6379/2"
    for settings in config["model_semaphore"].values():
        settings["redis_url"] = "redis://redis:6379/0"
    config_path = destination / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    profiles = destination / "profiles"
    profiles.mkdir()
    api_key = secrets.token_urlsafe(24)
    (profiles / "api_clients.json").write_text(json.dumps({"clients": [{
        "client_id": "chunk-verification", "client_dir": "chunk-verification", "enabled": True,
        "api_key_fingerprint": sha256(api_key.encode()).hexdigest(),
    }]}), encoding="utf-8")
    (destination / "data").mkdir()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
    common = {
        "image": image, "pull_policy": "never", "env_file": [str(ROOT / ".env")],
        "volumes": [f"{config_path.as_posix()}:/app/config.yaml:ro",
                    f"{(ROOT / '.env').as_posix()}:/app/.env:ro",
                    f"{profiles.as_posix()}:/app/profiles",
                    f"{(destination / 'data').as_posix()}:/app/data"],
        "depends_on": ["redis"],
    }
    services = {"redis": {"image": "redis:7-alpine", "pull_policy": "never"}}
    services["api"] = {**common, "ports": [f"127.0.0.1:{port}:8000"],
                       "command": ["/app/host", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]}
    for number in range(1, 4):
        services[f"worker{number}"] = {**common, "command": ["/app/host", "-m", "celery", "-A",
            "task_queue.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=1", "--pool=prefork"]}
    compose = destination / "compose.yaml"
    compose.write_text(yaml.safe_dump({"services": services}, sort_keys=False), encoding="utf-8")
    return compose, port, api_key, config


def collect(directory, statuses):
    results = []
    for public in statuses:
        task_id = public["task_id"]
        task_dir = directory / "data" / "api" / task_id
        task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        calls = [json.loads(path.read_text(encoding="utf-8")) for path in (task_dir / "logs" / "calls").glob("*.json")]
        sent = [call for call in calls if call.get("sent")]
        controls = []
        for call in sent:
            request = call["request"]
            control = request.get("input_control", request)
            if "input_token_counts" in control:
                controls.append({"component": call["component"], "configured": control["configured_input_tokens"],
                    "effective": control["effective_input_tokens"], "maximum": max(control["input_token_counts"], default=0),
                    "tokenizer": control["tokenizer"]})
        artifact = task_dir / "state" / "docxindex" / "vector_index.json"
        vector = json.loads(artifact.read_text(encoding="utf-8")) if artifact.exists() else {}
        document_path = artifact.with_name("document_index.json")
        nodes = {node["node_id"]: node for node in json.loads(document_path.read_text(encoding="utf-8"))["nodes"]}
        chunks = {}
        for item in vector.get("items", []):
            chunks.setdefault(item["node_id"], []).append(item)
        for node_id, pieces in chunks.items():
            node = nodes[node_id]
            source = f"标题：{node.get('title') or ''}\n摘要：{node.get('summary') or ''}\n正文：{node.get('text') or ''}".strip()
            covered = 0
            for piece in sorted(pieces, key=lambda value: value["source_start"]):
                start, end = piece["source_start"], piece["source_end"]
                if start > covered or piece["text"] != source[start:end]:
                    raise AssertionError(f"Source coverage or text changed: {task_id}/{node_id}")
                if sha256(piece["text"].encode()).hexdigest() != piece["text_hash"]:
                    raise AssertionError(f"Vector text hash mismatch: {task_id}/{node_id}")
                covered = max(covered, end)
            if covered != len(source):
                raise AssertionError(f"Source tail is missing: {task_id}/{node_id}")
        for control in controls:
            if control["maximum"] > control["effective"]:
                raise AssertionError(f"Model input exceeded its effective limit: {task_id}")
        output = task.get("output") or {}
        filename = output.get("result_filename")
        report_exists = bool(filename and (task_dir / "output" / filename).is_file())
        comment_count = None
        if report_exists:
            with ZipFile(task_dir / "output" / filename) as report:
                if report.testzip() is not None:
                    raise AssertionError(f"Report ZIP is corrupt: {task_id}")
                comments = ElementTree.fromstring(report.read("word/comments.xml"))
                comment_count = len(comments)
                if "未完成审查项：" in "".join(comments.itertext()):
                    raise AssertionError(f"Report contains an incomplete-review notice: {task_id}")
        review_outputs = json.loads((task_dir / "logs" / "review_outputs.json").read_text(encoding="utf-8"))
        review_complete = all(item["quality_status"] in {"passed", "not_required"} for item in review_outputs)
        events = (task_dir / "logs" / "events.log").read_text(encoding="utf-8")
        results.append({"task_id": task_id, "api_status": public["status"], "stored_status": task["status"],
            "quality": public.get("quality"), "criteria_count": len(public.get("criteria") or []),
            "report_exists": report_exists, "report_comment_count": comment_count,
            "review_outputs_complete": review_complete, "log_review_completed": "review_completed task_id=" + task_id in events,
            "vector_source_coverage_verified": True, "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "error": task.get("error"), "model_requests": len(sent),
            "request_failures": [{"component": c["component"], "error": c.get("error")} for c in sent if c["status"] == "failed"],
            "input_controls": controls, "vector_count": len(vector.get("items", [])),
            "vector_nodes": len({item["node_id"] for item in vector.get("items", [])}),
            "vector_format": vector.get("format_version"), "task_dir": str(task_dir)})
    return results


async def run_profile(root, limit, image):
    directory = root / str(limit)
    compose, port, api_key, config = setup(directory, limit, image)
    project = "chunk" + root.name[-6:].lower() + str(limit)
    base = ["docker", "compose", "-p", project, "-f", str(compose)]
    (directory / "project.json").write_text(json.dumps({"project": project, "port": port}), encoding="utf-8")
    command(base + ["up", "-d"])
    started = time.monotonic()
    peaks = {kind: 0 for kind in config["model_semaphore"]}
    samples = []
    public = []
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            for _ in range(120):
                try:
                    response = await client.get("/health")
                    if response.is_success:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Isolated API did not become healthy")
            headers = {"Authorization": api_key}
            async def submit(number):
                contract = ROOT / "deploy" / "test_contract.docx"
                response = await client.post("/api/review/jobs", headers=headers,
                    data={"templateCode": "chunk-verification", "serialNo": f"{limit}-{number}"},
                    files={"file": (contract.name, contract.read_bytes(),
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
                response.raise_for_status()
                return response.json()["task_id"]
            ids = await asyncio.gather(*(submit(number) for number in range(1, 4)))
            (directory / "task_ids.json").write_text(json.dumps(ids), encoding="utf-8")
            print(json.dumps({"limit": limit, "submitted": ids}), flush=True)
            keys = [settings["key"] for settings in config["model_semaphore"].values()]
            script = "local a={} for i,k in ipairs(KEYS) do a[i]=redis.call('ZCARD',k) end return a"
            deadline = time.monotonic() + 2100
            last_print = 0
            while time.monotonic() < deadline:
                responses = await asyncio.gather(*(client.post("/api/review/jobs/status", headers=headers,
                                                               json={"task_id": task_id}) for task_id in ids))
                for response in responses:
                    response.raise_for_status()
                public = [response.json() for response in responses]
                redis = await asyncio.to_thread(command, base + ["exec", "-T", "redis", "redis-cli", "--raw", "-n", "0",
                                                                "EVAL", script, str(len(keys)), *keys])
                values = list(map(int, redis.stdout.split()))
                for kind, count in zip(peaks, values, strict=True):
                    peaks[kind] = max(peaks[kind], count)
                    if count > config["model_semaphore"][kind]["max_concurrent_requests"]:
                        raise AssertionError(f"Global {kind} concurrency limit exceeded")
                samples.append({"elapsed": round(time.monotonic() - started, 2), "held": values})
                if time.monotonic() - last_print > 20:
                    print(json.dumps({"limit": limit, "states": [p["status"] for p in public],
                                      "elapsed": round(time.monotonic() - started), "sampled_peaks": peaks}), flush=True)
                    last_print = time.monotonic()
                if all(p["status"] in {"succeeded", "failed", "cancelled"} for p in public):
                    break
                await asyncio.sleep(2)
            (directory / "api_final.json").write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
            final_keys = [key + suffix for key in keys for suffix in ("", ":waiting", ":deadlines")]
            redis = command(base + ["exec", "-T", "redis", "redis-cli", "--raw", "-n", "0", "EVAL", script,
                                    str(len(final_keys)), *final_keys])
            final_slots = list(map(int, redis.stdout.split()))
            # Preserve cleanup evidence even if later artifact inspection fails.
            (directory / "final_slots.json").write_text(json.dumps(final_slots), encoding="utf-8")
            (directory / "semaphore_samples.json").write_text(json.dumps(samples), encoding="utf-8")
            result = {"limit": limit, "elapsed_seconds": round(time.monotonic() - started, 3),
                "sampled_peaks": peaks, "final_slots": final_slots, "jobs": collect(directory, public)}
            (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"limit": limit, "finished": [p["status"] for p in public], "final_slots": final_slots}), flush=True)
            return result
    finally:
        logs = command(base + ["logs", "--no-color"], check=False)
        (directory / "compose.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        command(base + ["down", "--remove-orphans"], check=False)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="lexora:chunks-20261020")
    parser.add_argument("--limits", nargs="+", type=int, default=[40000, 1024])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = args.output or Path(tempfile.mkdtemp(prefix="cc1020_"))
    destination.mkdir(parents=True, exist_ok=True)
    print("artifacts=" + str(destination), flush=True)
    results = []
    for limit in args.limits:
        results.append(await run_profile(destination, limit, args.image))
    (destination / "result.json").write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "profiles": results},
                                                       ensure_ascii=False, indent=2), encoding="utf-8")
    if any(profile["final_slots"] != [0] * 9 for profile in results):
        raise AssertionError("Model owners or waiters remain after the jobs ended")
    for profile in results:
        for job in profile["jobs"]:
            if (job["api_status"] != "succeeded" or job["stored_status"] != "succeeded"
                    or not job["report_exists"] or not job["review_outputs_complete"] or not job["log_review_completed"]
                    or job["quality"]["completed_count"] != job["quality"]["total_count"]):
                raise AssertionError(f"Real contract verification failed: {job['task_id']}")


if __name__ == "__main__":
    asyncio.run(main())
