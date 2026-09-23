"""Exercise the real API review path with a local DOCX and inspect task logs.

Usage: .venv/Scripts/python scripts/verify_real_contract_logging.py --contract deploy/test_contract.docx
The test uses an in-process API client mapping and a temporary MCP launcher. It does
not change the running service, config.yaml, profiles, or the real Redis settings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
import zipfile
from xml.etree import ElementTree
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def verify(contract: Path, max_seconds: int) -> int:
    import config
    from endpoints.api import client_mapping, review_jobs
    from endpoints.review import job_worker
    from endpoints.review.task_store import read_task

    for settings in config.MODEL_SEMAPHORE_CONFIGS.values():
        settings["enabled"] = False
    review_jobs.QUEUE_ENABLED = False
    api_key = "local-review-log-test"
    client_mapping.load_api_clients = lambda: [{
        "client_id": "local-log-test", "client_dir": "api", "enabled": True,
        "api_key_fingerprint": client_mapping.api_key_fingerprint(api_key),
    }]

    with tempfile.TemporaryDirectory(prefix="contract-agent-mcp-test-") as temp_dir:
        launcher = Path(temp_dir) / "mcp_test_launcher.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import config\n"
            "[item.__setitem__('enabled', False) for item in config.MODEL_SEMAPHORE_CONFIGS.values()]\n"
            "from mcp_service.server.server import mcp\n"
            "mcp.run()\n", encoding="utf-8",
        )
        job_worker.MCP_SERVER_TARGET = str(launcher)
        from app import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local-test", timeout=120) as client:
            with contract.open("rb") as document:
                response = await client.post(
                    "/api/review/jobs", headers={"Authorization": api_key},
                    files={"file": (contract.name, document,
                                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                    data={"templateCode": "logging-verification", "serialNo": "local-test"},
                )
            print("submit_status", response.status_code, flush=True)
            if response.status_code != 202:
                print(response.text[:2000], flush=True)
                return 1
            task_id = response.json()["task_id"]
            print("task_id", task_id, flush=True)
            deadline = time.monotonic() + max_seconds
            last_status = None
            while time.monotonic() < deadline:
                status_response = await client.post(
                    "/api/review/jobs/status", headers={"Authorization": api_key},
                    json={"task_id": task_id},
                )
                status_response.raise_for_status()
                task = read_task("api", task_id)
                if task is None:
                    raise RuntimeError("Submitted task is missing")
                status = task["status"]
                if status != last_status:
                    print("task_status", status, flush=True)
                    last_status = status
                if status in {"succeeded", "failed", "cancelled"}:
                    break
                await asyncio.sleep(5)
            else:
                print("timed_out_waiting_for_task", flush=True)
                return 2

            task_dir = Path(task["output"]["result_path"]).parent.parent
            calls_dir = task_dir / "logs" / "calls"
            calls = [json.loads(path.read_text(encoding="utf-8")) for path in calls_dir.glob("*.json")]
            trace_path = task_dir / "logs" / "trace.json"
            trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else {}
            result_path = Path(task["output"]["result_path"])
            comment_count = 0
            if result_path.is_file():
                with zipfile.ZipFile(result_path) as document:
                    if "word/comments.xml" in document.namelist():
                        comment_count = len(ElementTree.fromstring(document.read("word/comments.xml")))
            summary = {
                "task_id": task_id, "status": status,
                "result_exists": result_path.is_file(), "result_comments": comment_count,
                "call_count": len(calls),
                "pending_calls": sum(call.get("status") == "pending" for call in calls),
                "docxindex_call_count": sum(str(call.get("component", "")).startswith("docxindex") for call in calls),
                "agent_call_count": sum(call.get("component") == "agent" for call in calls),
                "H12_call_count": sum(call.get("criterion_id") == "H12" for call in calls),
                "H15_call_count": sum(call.get("criterion_id") == "H15" for call in calls),
                "trace_runs": len(trace.get("runs") or []),
                "route_events": len(list((task_dir / "logs" / "route_events").glob("*.json"))),
                "criterion_errors": len(list((task_dir / "state" / "criteria").glob("*.error.json"))),
                "task_dir": str(task_dir),
                "task_error": task.get("error"),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            return 0 if (status == "succeeded" and summary["result_exists"]
                         and summary["result_comments"] and not summary["pending_calls"]
                         and summary["docxindex_call_count"] and summary["agent_call_count"]
                         and summary["H12_call_count"] and summary["H15_call_count"]
                         and summary["trace_runs"] and summary["route_events"]) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=ROOT / "deploy" / "test_contract.docx")
    parser.add_argument("--max-seconds", type=int, default=1800)
    args = parser.parse_args()
    return asyncio.run(verify(args.contract.resolve(), args.max_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
