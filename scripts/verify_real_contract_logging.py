"""Submit to a running API/Celery deployment and inspect its shared task files.

Requires an existing registered API key in API_KEY, running workers, Redis DB 0
and DB 2, and logging.enabled/detailed=true. --task-root is the directory visible
to THIS process that maps to the deployment's data/api, not a container path.
No application settings, credentials, queue routing or model limits are patched.
Executing this script submits a real review and calls the configured models.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from xml.etree import ElementTree
import zipfile

import httpx


def summarize(task_dir: Path, task: dict, public_status: dict) -> dict:
    calls = [json.loads(path.read_text(encoding="utf-8")) for path in (task_dir / "logs" / "calls").glob("*.json")]
    logical = [
        json.loads(path.read_text(encoding="utf-8")) for path in (task_dir / "logs" / "logical_calls").glob("*.json")
    ]
    trace_path = task_dir / "logs" / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else {}
    # The stored result_path belongs to the SERVER. Translate only the known
    # output filename into the caller's explicitly supplied shared task root.
    filename = str((task.get("output") or {}).get("result_filename") or "")
    if filename and (Path(filename).name != filename or "/" in filename or "\\" in filename):
        raise ValueError("Task output contains an invalid result filename.")
    result_path = task_dir / "output" / filename if filename else None
    result_exists = bool(result_path and result_path.is_file())
    comment_count = 0
    if result_exists:
        with zipfile.ZipFile(result_path) as document:
            if "word/comments.xml" in document.namelist():
                comment_count = len(ElementTree.fromstring(document.read("word/comments.xml")))
    sent = [call for call in calls if call.get("sent")]
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "result_exists": result_exists,
        "result_comments": comment_count,
        "call_count": len(calls),
        "logical_call_count": len(logical),
        "retried_logical_calls": sum((call.get("retry_count") or 0) > 0 for call in logical),
        "retry_count": sum(call.get("retry_count") or 0 for call in logical),
        "known_input_tokens": sum(call["input_tokens"] for call in sent if call.get("input_tokens") is not None),
        "known_output_tokens": sum(call["output_tokens"] for call in sent if call.get("output_tokens") is not None),
        "unknown_usage_attempts": sum(call.get("input_tokens") is None for call in sent),
        "pending_calls": sum(call.get("status") == "pending" for call in calls),
        "docxindex_call_count": sum(str(call.get("component", "")).startswith("docxindex") for call in calls),
        "agent_call_count": sum(call.get("component") == "agent" for call in calls),
        "H12_call_count": sum(call.get("criterion_id") == "H12" for call in calls),
        "H15_call_count": sum(call.get("criterion_id") == "H15" for call in calls),
        "trace_runs": len(trace.get("runs") or []),
        "route_events": len(list((task_dir / "logs" / "route_events").glob("*.json"))),
        "criterion_error_history_files": len(list((task_dir / "state" / "criteria").glob("*.error.json"))),
        "public_criteria_count": len(public_status.get("criteria") or []),
        "public_quality": public_status.get("quality"),
        "public_internal_fields": sorted(
            field for field in ("state", "logs", "criteria_manifest") if field in public_status
        ),
        "task_dir": str(task_dir),
        "task_error": task.get("error"),
    }


async def verify(args, api_key: str) -> int:
    fields = {"templateCode": "logging-verification", "serialNo": "logging-test"}
    fields.update(args.form_field)
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=args.http_timeout) as client:
        with args.contract.open("rb") as document:
            response = await client.post(
                "/api/review/jobs",
                headers={"Authorization": api_key},
                files={
                    "file": (
                        args.contract.name,
                        document,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                data=fields,
            )
        print("submit_status", response.status_code, flush=True)
        if response.status_code != 202:
            print(response.text[:2000], flush=True)
            return 1
        task_id = response.json()["task_id"]
        if not isinstance(task_id, str) or not task_id or Path(task_id).name != task_id or "\\" in task_id:
            raise ValueError("API returned an invalid task identifier.")
        print("task_id", task_id, flush=True)
        deadline = time.monotonic() + args.max_seconds
        last_status = None
        while time.monotonic() < deadline:
            status_response = await client.post(
                "/api/review/jobs/status", headers={"Authorization": api_key}, json={"task_id": task_id}
            )
            status_response.raise_for_status()
            public_status = status_response.json()
            status = public_status["status"]
            if status != last_status:
                print("task_status", status, flush=True)
                last_status = status
            if status in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(args.poll_interval)
        else:
            print("timed_out_waiting_for_task; server task continues; task_id=" + task_id, flush=True)
            return 2

    task_dir = args.task_root / task_id
    task_path = task_dir / "task.json"
    if not task_path.is_file():
        raise RuntimeError(
            f"Task files are not visible at {task_dir}; map --task-root to the server's shared data/api."
        )
    summary = summarize(task_dir, json.loads(task_path.read_text(encoding="utf-8")), public_status)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    public_quality = summary["public_quality"] or {}
    valid = (
        summary["status"] == "succeeded"
        and summary["result_exists"]
        and summary["result_comments"]
        and not summary["pending_calls"]
        and summary["docxindex_call_count"]
        and summary["agent_call_count"]
        and summary["H12_call_count"]
        and summary["H15_call_count"]
        and summary["trace_runs"]
        and summary["route_events"]
        and summary["public_criteria_count"] == args.expected_criteria
        and public_quality.get("total_count") == args.expected_criteria
        and not summary["public_internal_fields"]
    )
    return 0 if valid else 1


def form_field(value: str) -> tuple[str, str]:
    key, separator, content = value.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError("Use NAME=VALUE for --form-field.")
    return key, content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", required=True, help="Running Web/API service URL; its workers must already be running."
    )
    parser.add_argument(
        "--contract", type=Path, required=True, help="Local DOCX to upload; running this submits a real task."
    )
    parser.add_argument(
        "--task-root", type=Path, required=True, help="Local/shared directory mapping the server's data/api."
    )
    parser.add_argument(
        "--api-key-env", default="API_KEY", help="Environment variable holding an existing registered API key."
    )
    parser.add_argument(
        "--max-seconds", type=int, default=1900, help="Polling bound only; does not cancel the server task."
    )
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--http-timeout", type=float, default=60)
    parser.add_argument("--expected-criteria", type=int, default=18)
    parser.add_argument(
        "--form-field",
        action="append",
        type=form_field,
        default=[],
        help="Additional metadata/model form field NAME=VALUE.",
    )
    args = parser.parse_args()
    args.contract = args.contract.resolve()
    args.task_root = args.task_root.resolve()
    if not args.contract.is_file() or args.contract.suffix.lower() != ".docx":
        parser.error("--contract must be an existing DOCX file.")
    if not args.task_root.is_dir():
        parser.error("--task-root must be the existing shared data/api directory.")
    if min(args.max_seconds, args.poll_interval, args.http_timeout, args.expected_criteria) <= 0:
        parser.error("Timeouts, polling interval and expected criterion count must be positive.")
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        parser.error(f"Set the {args.api_key_env} environment variable to an existing registered API key.")
    return asyncio.run(verify(args, api_key))


if __name__ == "__main__":
    raise SystemExit(main())
