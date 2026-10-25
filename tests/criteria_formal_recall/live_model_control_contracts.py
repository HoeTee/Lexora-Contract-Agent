"""Submit one mixed batch of real contracts to an isolated API and three workers.

This is a live acceptance harness, not a mocked model test. Failed attempts and
missing artifacts are recorded before any acceptance assertion is raised.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx
import yaml


ROOT = Path(__file__).resolve().parents[2]
SAMPLES = Path("C:/Users/lenovo/Downloads/合同样例")
CONTRACTS = [
    ROOT / "deploy" / "test_contract.docx",
    SAMPLES / "【20251224已审查】2025年度算力资源服务租赁项目（算力资源一）采购合同1223.docx",
    SAMPLES / "【已审查】（2026年058号）2026年度GPU算力服务租赁采购合同-20260617_批注版.docx",
    SAMPLES / "【已审查】（456号）农商财富大厦办公场地租赁合同11.17.docx",
    SAMPLES / "【已审查】（524号）IPv6 上网行为管理设备采购合同.docx",
    SAMPLES / "【已审查】（528号）2026年至2028年贵宾医疗服务合作协议-邵逸夫医院.docx",
]
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def instant():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def save(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def filesystem_path(path: Path) -> Path:
    """Read Windows evidence paths beyond MAX_PATH without changing mounts."""
    if os.name != "nt":
        return path
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return Path(resolved)
    if resolved.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + resolved[2:])
    return Path("\\\\?\\" + resolved)


def command(args, *, check=True):
    result = subprocess.run(args, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
    return result


def isolated_redis(url: str):
    parts = urlsplit(url)
    return urlunsplit(("redis", "redis:6379", parts.path or "/0", "", ""))


def prepare(directory: Path, *, image: str, stream: bool, count: int, redis_image: str):
    directory.mkdir(parents=True, exist_ok=True)
    fixtures = []
    for number, source in enumerate(CONTRACTS[:count], 1):
        content = source.read_bytes()
        with ZipFile(source) as archive:
            if archive.testzip() is not None or "word/document.xml" not in archive.namelist():
                raise ValueError(f"Contract is not a valid DOCX: {source}")
        fixtures.append({"fixture": f"C{number}", "source": str(source), "sha256": sha256(content).hexdigest(),
                         "size_bytes": len(content), "content": content})
    if len({item["sha256"] for item in fixtures}) != count:
        raise ValueError("The acceptance batch must contain distinct contract contents")
    save(directory / "fixtures.json", [{key: value for key, value in item.items() if key != "content"} for item in fixtures])

    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    original_hash = sha256((ROOT / "config.yaml").read_bytes()).hexdigest()
    config["llm"]["stream"] = stream
    config.setdefault("logging", {})["detailed"] = True
    config["queue"]["broker_url"] = isolated_redis(config["queue"]["broker_url"])
    for settings in config["model_semaphore"].values():
        settings["redis_url"] = isolated_redis(settings["redis_url"])
    config_path = directory / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    profiles = directory / "profiles"
    profiles.mkdir(exist_ok=True)
    api_key = secrets.token_urlsafe(24)
    save(profiles / "api_clients.json", {"clients": [{"client_id": "model-verification", "client_dir": "model-verification",
         "enabled": True, "api_key_fingerprint": sha256(api_key.encode()).hexdigest()}]})
    (directory / "data").mkdir(exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    common = {
        "image": image, "pull_policy": "never", "env_file": [str(ROOT / ".env")],
        "volumes": [f"{config_path.as_posix()}:/app/config.yaml:ro", f"{(ROOT / '.env').as_posix()}:/app/.env:ro",
                    f"{profiles.as_posix()}:/app/profiles", f"{(directory / 'data').as_posix()}:/app/data"],
        "depends_on": ["redis"],
    }
    services = {"redis": {"image": redis_image, "pull_policy": "never"}}
    services["api"] = {**common, "ports": [f"127.0.0.1:{port}:8000"], "command": [
        "/app/host", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]}
    for number in range(1, 4):
        services[f"worker{number}"] = {**common, "command": ["/app/host", "-m", "celery", "-A",
            "task_queue.celery_app:celery_app", "worker", "--loglevel=info", "--concurrency=1", "--pool=prefork"]}
    compose = directory / "compose.yaml"
    compose.write_text(yaml.safe_dump({"services": services}, sort_keys=False), encoding="utf-8")
    save(directory / "configuration.json", {"source_config_sha256": original_hash,
         "effective_config_sha256": sha256(config_path.read_bytes()).hexdigest(), "stream": stream,
         "image": image, "worker_count": 3, "concurrency_per_worker": 1,
         "changed_fields": ["llm.stream", "logging.detailed", "queue.broker_url", "model_semaphore.*.redis_url"]})
    return compose, port, api_key, config, fixtures


def read_json(path: Path, errors: list[str], *, required=True):
    try:
        return json.loads(filesystem_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if required:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return None


def inspect_job(directory: Path, public: dict, fixture: dict):
    task_id = public["task_id"]
    task_dir = directory / "data" / "api" / task_id
    errors = []
    task = read_json(task_dir / "task.json", errors) or {}
    uploaded = task_dir / "input" / ((task.get("input") or {}).get("contract_filename") or Path(fixture["source"]).name)
    try:
        if sha256(filesystem_path(uploaded).read_bytes()).hexdigest() != fixture["sha256"]:
            errors.append("Stored contract differs from the submitted source SHA256")
    except OSError as exc:
        errors.append(f"Stored contract: {exc}")
    calls = []
    for path in (task_dir / "logs" / "calls").glob("*.json"):
        value = read_json(path, errors)
        if value:
            calls.append(value)
    sent = list({item["call_id"]: item for item in calls if item.get("sent")}.values())
    if not sent:
        errors.append("No actual model requests were recorded")
    token_stats = public.get("token_stats")
    if not isinstance(token_stats, dict):
        errors.append("API has no model-attempt token statistics")
        token_stats = {}
    if token_stats != task.get("token_stats"):
        errors.append("API and stored token statistics differ")
    counted_input = sum(item.get("input_tokens") or 0 for item in sent)
    counted_output = sum(item.get("output_tokens") or 0 for item in sent)
    recomputed_stats = {"input": counted_input, "output": counted_output,
                        "total": counted_input + counted_output, "sent_attempts": len(sent)}
    for field, expected in recomputed_stats.items():
        if token_stats.get(field) != expected:
            errors.append(f"Token statistics differ from unique actual attempts: {field}: {token_stats.get(field)} != {expected}")
    for item in sent:
        counts, limit = item.get("input_token_counts"), item.get("max_input_tokens")
        if not counts or item.get("local_input_tokens") is None:
            errors.append(f"Missing pre-send input counts: {item.get('call_id')}")
        elif limit is not None and max(counts) > limit:
            errors.append(f"Input limit exceeded: {item.get('call_id')}: {max(counts)} > {limit}")
        if item.get("admitted_at_ms") is None or item.get("semaphore_key") is None:
            errors.append(f"Request lacks shared admission evidence: {item.get('call_id')}")
    index_dir = task_dir / "state" / "docxindex"
    vector = read_json(index_dir / "vector_index.json", errors) or {}
    document = read_json(index_dir / "document_index.json", errors) or {}
    nodes = {node["node_id"]: node for node in document.get("nodes", [])}
    groups = defaultdict(list)
    for piece in vector.get("items", []):
        groups[piece["node_id"]].append(piece)
    for node_id, pieces in groups.items():
        node = nodes.get(node_id)
        if node is None:
            errors.append(f"Vector changed original node_id: {node_id}")
            continue
        source = f"标题：{node.get('title') or ''}\n摘要：{node.get('summary') or ''}\n正文：{node.get('text') or ''}".strip()
        covered = 0
        for piece in sorted(pieces, key=lambda value: value["source_start"]):
            start, end = piece["source_start"], piece["source_end"]
            if start > covered or piece["text"] != source[start:end]:
                errors.append(f"Original text coverage changed: {node_id}/{start}")
            if sha256(piece["text"].encode()).hexdigest() != piece["text_hash"]:
                errors.append(f"Vector text hash mismatch: {node_id}/{start}")
            covered = max(covered, end)
        if covered != len(source):
            errors.append(f"Original node tail missing: {node_id}")
    if not groups:
        errors.append("No vector source coverage evidence")

    output = task.get("output") or {}
    filename = output.get("result_filename")
    report = task_dir / "output" / filename if filename else None
    comments_count = None
    try:
        if report is None:
            raise FileNotFoundError("No report filename in task output")
        with ZipFile(filesystem_path(report)) as archive:
            if archive.testzip() is not None:
                errors.append("Report ZIP is corrupt")
            ElementTree.fromstring(archive.read("word/document.xml"))
            if "word/comments.xml" in archive.namelist():
                comments = ElementTree.fromstring(archive.read("word/comments.xml"))
                comments_count = len(comments)
                if "未完成审查项：" in "".join(comments.itertext()):
                    errors.append("Report contains an incomplete-review notice")
    except (OSError, ValueError, KeyError, ElementTree.ParseError) as exc:
        errors.append(f"Report: {type(exc).__name__}: {exc}")
    reviews = read_json(task_dir / "logs" / "review_outputs.json", errors) or []
    if not reviews or any(item.get("quality_status") not in {"passed", "not_required"} for item in reviews):
        errors.append("Not all criterion outputs passed final quality validation")
    try:
        events = filesystem_path(task_dir / "logs" / "events.log").read_text(encoding="utf-8")
    except OSError as exc:
        events = ""
        errors.append(f"Events: {exc}")
    if "review_completed task_id=" + task_id not in events:
        errors.append("Missing successful final event")
    quality = public.get("quality") or {}
    if not quality.get("total_count") or quality.get("completed_count") != quality.get("total_count"):
        errors.append("API quality count is incomplete")
    if quality != task.get("quality"):
        errors.append("API and stored quality summaries differ")
    if public.get("criteria") != task.get("criteria"):
        errors.append("API and stored criterion final states differ")
    if quality.get("total_count") != len(reviews):
        errors.append("API criterion count differs from final review-output log")
    if public.get("status") != "succeeded" or task.get("status") != "succeeded":
        errors.append(f"Final API/storage status: {public.get('status')}/{task.get('status')}")
    return {"fixture": fixture["fixture"], "source_sha256": fixture["sha256"], "task_id": task_id,
            "task_dir": str(task_dir), "report": str(report) if report else None, "report_comment_count": comments_count,
            "api_status": public.get("status"), "stored_status": task.get("status"), "quality": quality,
            "summary": public.get("summary"), "source_sha256_verified": not any("contract" in error for error in errors),
            "token_stats": token_stats, "recomputed_token_stats": recomputed_stats,
            "original_task_error": task.get("error"), "request_count": len(sent),
            "request_failures": [{key: call.get(key) for key in ("call_id", "model_type", "error", "input_tokens", "output_tokens")}
                                 for call in sent if call.get("status") == "failed"],
            "errors": errors}, sent


def audit_calls(calls: list[dict], config: dict):
    errors, metrics = [], {}
    for kind, settings in config["model_semaphore"].items():
        group = [item for item in calls if item.get("model_type") == kind]
        times = sorted(item["admitted_at_ms"] for item in group if item.get("admitted_at_ms") is not None)
        window, peak = deque(), 0
        for current in times:
            while window and window[0] <= current - 60000:
                window.popleft()
            window.append(current)
            peak = max(peak, len(window))
        concurrency_peak = max((item.get("acquired_concurrency") or 0 for item in group), default=0)
        rpm = settings.get("max_requests_per_minute")
        concurrency = settings.get("max_concurrent_requests")
        if rpm is not None and peak > rpm:
            errors.append(f"{kind} rolling-minute peak {peak} exceeds RPM {rpm}")
        if concurrency is not None and concurrency_peak > concurrency:
            errors.append(f"{kind} admitted concurrency {concurrency_peak} exceeds {concurrency}")
        metrics[kind] = {"request_count": len(group), "rolling_minute_peak": peak,
                         "admitted_concurrency_peak": concurrency_peak, "rpm_limit": rpm, "concurrency_limit": concurrency}
    return metrics, errors


def redis_counts(base: list[str], config: dict):
    result = {}
    script = "local a={} for i,k in ipairs(KEYS) do a[i]=redis.call('ZCARD',k) end return a"
    for kind, settings in config["model_semaphore"].items():
        keys = [settings["key"] + suffix for suffix in ("", ":waiting", ":deadlines", ":rpm")]
        db = urlsplit(settings["redis_url"]).path.lstrip("/") or "0"
        response = command(base + ["exec", "-T", "redis", "redis-cli", "--raw", "-n", db,
                                   "EVAL", script, str(len(keys)), *keys])
        values = list(map(int, response.stdout.split()))
        result[kind] = dict(zip(("held", "waiting", "deadlines", "rpm_retained"), values, strict=True))
    return result


async def run(args):
    directory = (args.output or Path(tempfile.mkdtemp(prefix="model_control_"))).resolve()
    compose, port, api_key, config, fixtures = prepare(directory, image=args.image, stream=args.stream == "true",
                                                      count=args.count, redis_image=args.redis_image)
    batch_id = "mc" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
    base = ["docker", "compose", "-p", batch_id, "-f", str(compose)]
    save(directory / "project.json", {"project": batch_id, "port": port, "started_at": instant()})
    save(directory / "image.json", {"inspect": command(["docker", "image", "inspect", args.image], check=False).stdout})
    submissions, statuses, samples, failures = [], [], [], []
    started = time.monotonic()
    print("artifacts=" + str(directory), flush=True)
    try:
        command(base + ["up", "-d"])
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
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
            barrier = asyncio.Barrier(args.count)

            async def submit(fixture):
                await barrier.wait()
                item = {"fixture": fixture["fixture"], "source_sha256": fixture["sha256"], "started_at": instant()}
                try:
                    response = await client.post("/api/review/jobs", headers=headers,
                        data={"templateCode": "model-verification", "serialNo": batch_id + "-" + fixture["fixture"]},
                        files={"file": (Path(fixture["source"]).name, fixture["content"], MIME)})
                    item.update(http_status=response.status_code, response=response.text)
                    response.raise_for_status()
                    item["task_id"] = response.json()["task_id"]
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {exc}"
                item["finished_at"] = instant()
                return item

            submissions = await asyncio.gather(*(submit(fixture) for fixture in fixtures))
            save(directory / "submissions.json", {"batch_id": batch_id, "barrier_parties": args.count, "submissions": submissions})
            print(json.dumps({"batch_id": batch_id, "submissions": submissions}, ensure_ascii=False), flush=True)
            ids = [item["task_id"] for item in submissions if item.get("task_id")]
            timeout = args.timeout or ((config["queue"].get("queued_timeout_seconds") or 7200)
                                      + (config["queue"].get("task_time_limit_seconds") or 7200) + 180)
            deadline, last_print = time.monotonic() + timeout, 0
            while ids and time.monotonic() < deadline:
                responses = await asyncio.gather(*(client.post("/api/review/jobs/status", headers=headers,
                    json={"task_id": task_id}) for task_id in ids))
                statuses = []
                for task_id, response in zip(ids, responses, strict=True):
                    response.raise_for_status()
                    statuses.append({**response.json(), "task_id": task_id})
                save(directory / "api_final.json", statuses)
                sample = {"at": instant(), "elapsed": round(time.monotonic() - started, 2),
                          "counts": await asyncio.to_thread(redis_counts, base, config)}
                samples.append(sample)
                save(directory / "semaphore_samples.json", samples)
                for kind, values in sample["counts"].items():
                    limit = config["model_semaphore"][kind].get("max_concurrent_requests")
                    if limit is not None and values["held"] > limit:
                        violation = f"{kind} sampled held slots {values['held']} exceed concurrency {limit}"
                        if violation not in failures:
                            failures.append(violation)
                if time.monotonic() - last_print > 20:
                    print(json.dumps({"batch_id": batch_id, "states": [value["status"] for value in statuses],
                                      "elapsed": sample["elapsed"], "counts": sample["counts"]}), flush=True)
                    last_print = time.monotonic()
                if all(value.get("status") in {"succeeded", "failed", "cancelled"} for value in statuses):
                    break
                await asyncio.sleep(2)
            if len(ids) != args.count:
                failures.append("Not every contract was accepted by the API")
            if not statuses or any(value.get("status") not in {"succeeded", "failed", "cancelled"} for value in statuses):
                failures.append("Batch did not reach all terminal states before harness timeout")
    except Exception as exc:
        failures.append(f"Harness: {type(exc).__name__}: {exc}")
    finally:
        logs = command(base + ["logs", "--no-color"], check=False)
        (directory / "compose.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        save(directory / "api_final.json", statuses)
        save(directory / "submissions.json", {"batch_id": batch_id, "barrier_parties": args.count, "submissions": submissions})
        try:
            final_counts = await asyncio.to_thread(redis_counts, base, config)
            save(directory / "final_slots.json", final_counts)
            for kind, values in final_counts.items():
                if any(values[field] for field in ("held", "waiting", "deadlines")):
                    failures.append(f"{kind} active slots or waiters remain: {values}")
        except Exception as exc:
            final_counts = {"error": f"{type(exc).__name__}: {exc}"}
            failures.append("Cannot capture final Redis owners and waiters")
            save(directory / "final_slots.json", final_counts)
        jobs, calls = [], []
        fixture_by_id = {item["fixture"]: item for item in fixtures}
        for submission in submissions:
            if not submission.get("task_id"):
                continue
            public = next((item for item in statuses if item["task_id"] == submission["task_id"]),
                          {"task_id": submission["task_id"], "status": "unknown"})
            try:
                job, requests = inspect_job(directory, public, fixture_by_id[submission["fixture"]])
                jobs.append(job)
                calls.extend(requests)
            except Exception as exc:
                jobs.append({"task_id": submission["task_id"], "api_status": public.get("status"),
                             "original_task_error": public.get("error"),
                             "errors": [f"Artifact inspection: {type(exc).__name__}: {exc}"]})
        metrics, audit_errors = audit_calls(calls, config)
        failures.extend(audit_errors)
        if "Failed to parse JSONRPC message from server" in logs.stdout + logs.stderr:
            failures.append("MCP protocol parsing failed")
        for job in jobs:
            failures.extend(f"{job['task_id']}: {error}" for error in job["errors"])
        if len(jobs) != args.count:
            failures.append(f"Expected {args.count} task artifacts, found {len(jobs)}")
        save(directory / "result.json", {"batch_id": batch_id, "stream": args.stream == "true", "count": args.count,
             "elapsed_seconds": round(time.monotonic() - started, 3), "jobs": jobs, "metrics": metrics,
             "final_slots": final_counts, "failures": failures, "passed": not failures})
        down = command(base + ["down", "--remove-orphans"], check=False)
        (directory / "compose_down.log").write_text(down.stdout + down.stderr, encoding="utf-8")
    if failures:
        raise AssertionError(f"Live acceptance failed; evidence: {directory / 'result.json'}\n" + "\n".join(failures))
    print(json.dumps({"passed": True, "batch_id": batch_id, "result": str(directory / "result.json")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--stream", choices=("true", "false"), required=True)
    parser.add_argument("--count", type=int, choices=(2, 6), default=6)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--redis-image", default="redis:7-alpine")
    parser.add_argument("--timeout", type=float, help="Harness wait limit in seconds; does not alter service timeouts")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
