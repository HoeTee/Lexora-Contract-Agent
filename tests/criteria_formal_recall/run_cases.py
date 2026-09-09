from __future__ import annotations

import argparse
import asyncio
import csv
import ctypes
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = Path(__file__).resolve().parent
DEFAULT_CASES_DIR = SUITE_ROOT / "cases"
DEFAULT_EXPECTED_CSV = SUITE_ROOT / "expected_cases_10.csv"
DEFAULT_RESULTS_DIR = SUITE_ROOT / "results"
DEFAULT_API_TASK_ROOT = PROJECT_ROOT / "data" / "api"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
CRITERION_NUMBER = re.compile(r"^\s*(\d{1,2})(?:\s*[.、．]\s*|\s+)")


@contextmanager
def keep_system_awake():
    """Prevent Windows sleep from invalidating end-to-end benchmark timings."""
    if sys.platform != "win32":
        yield
        return

    execution_state = ctypes.windll.kernel32.SetThreadExecutionState
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    if not execution_state(es_continuous | es_system_required):
        raise OSError("SetThreadExecutionState failed")
    try:
        yield
    finally:
        execution_state(es_continuous)


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    cases_dir: Path
    expected_csv: Path
    results_root: Path
    api_task_root: Path
    concurrency: int
    poll_interval: float
    task_timeout: float
    http_timeout: float
    model_fields: dict[str, str]
    contract_ids: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 criteria-formal 人工召回测试合同。")
    parser.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:5000"))
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--expected-csv", type=Path, default=DEFAULT_EXPECTED_CSV)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--api-task-root", type=Path, default=DEFAULT_API_TASK_ROOT)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--task-timeout", type=float, default=300.0)
    parser.add_argument("--http-timeout", type=float, default=300.0)
    parser.add_argument(
        "--contract-id",
        action="append",
        dest="contract_ids",
        help="只运行指定合同编号，可重复传入，例如 --contract-id D03。",
    )
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required in the environment or project .env")
    return value


def optional_model_fields() -> dict[str, str]:
    names = (
        "LLM_API_KEY",
        "LLM_MODEL_NAME",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL_NAME",
        "RERANKER_API_KEY",
        "RERANKER_MODEL_NAME",
    )
    return {name.lower(): os.getenv(name, "").strip() for name in names if os.getenv(name, "").strip()}


def load_settings(args: argparse.Namespace) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    if args.concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    return Settings(
        base_url=args.base_url.rstrip("/"),
        api_key=required_env("API_KEY"),
        cases_dir=args.cases_dir.resolve(),
        expected_csv=args.expected_csv.resolve(),
        results_root=args.results_dir.resolve(),
        api_task_root=args.api_task_root.resolve(),
        concurrency=args.concurrency,
        poll_interval=args.poll_interval,
        task_timeout=args.task_timeout,
        http_timeout=args.http_timeout,
        model_fields=optional_model_fields(),
        contract_ids=tuple(args.contract_ids or ()),
    )


def run_id() -> str:
    return datetime.now().strftime("run_%Y%m%d_%H%M%S")


def parse_json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"HTTP {response.status_code} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"HTTP response JSON is not an object: {payload!r}")
    return payload


async def submit_job(client: httpx.AsyncClient, settings: Settings, contract: Path, serial_no: str) -> str:
    with contract.open("rb") as contract_stream:
        files = {
            "file": (contract.name, contract_stream, DOCX_MEDIA_TYPE),
        }
        form = {
            "templateCode": "criteria-formal-v1",
            "serialNo": serial_no,
            **settings.model_fields,
        }
        response = await client.post(
            f"{settings.base_url}/api/review/jobs",
            headers={"Authorization": settings.api_key},
            data=form,
            files=files,
        )
    payload = parse_json_response(response)
    response.raise_for_status()
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError(f"submission did not return task_id: {payload}")
    return task_id


async def wait_for_task(client: httpx.AsyncClient, settings: Settings, task_id: str) -> dict[str, Any]:
    deadline = monotonic() + settings.task_timeout
    while True:
        try:
            response = await client.post(
                f"{settings.base_url}/api/review/jobs/status",
                headers={"Authorization": settings.api_key},
                json={"task_id": task_id},
            )
        except httpx.RequestError:
            if monotonic() >= deadline:
                raise
            await asyncio.sleep(settings.poll_interval)
            continue
        if response.status_code >= 500:
            if monotonic() >= deadline:
                response.raise_for_status()
            await asyncio.sleep(settings.poll_interval)
            continue
        payload = parse_json_response(response)
        response.raise_for_status()
        status = str(payload.get("status") or "")
        if status in TERMINAL_STATUSES:
            return payload
        if monotonic() >= deadline:
            raise TimeoutError(f"task timed out: {task_id}; last_status={status}")
        await asyncio.sleep(settings.poll_interval)


async def download_result(client: httpx.AsyncClient, settings: Settings, task_id: str, target: Path) -> None:
    response = await client.post(
        f"{settings.base_url}/api/review/jobs/result",
        headers={"Authorization": settings.api_key},
        json={"task_id": task_id, "output_type": "file"},
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if DOCX_MEDIA_TYPE not in content_type:
        raise RuntimeError(f"task result is not DOCX: task_id={task_id}, content-type={content_type}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)


def criterion_number(payload: dict[str, Any]) -> int | None:
    criterion_id = str(payload.get("criterion_id") or "").strip()
    id_match = re.fullmatch(r"C(\d{1,2})", criterion_id, flags=re.IGNORECASE)
    if id_match:
        value = int(id_match.group(1))
        return value if 1 <= value <= 18 else None

    text = str(payload.get("criterion") or "")
    match = CRITERION_NUMBER.match(text)
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 18 else None
    return None


def collect_outputs(api_task_root: Path, task_id: str) -> dict[int, list[dict[str, Any]]]:
    criteria_dir = api_task_root / task_id / "state" / "criteria"
    if not criteria_dir.is_dir():
        raise FileNotFoundError(f"criterion state directory was not found: {criteria_dir}")
    grouped: dict[int, list[dict[str, Any]]] = {}
    for path in sorted(criteria_dir.glob("*.output.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        number = criterion_number(payload)
        if number is not None:
            grouped.setdefault(number, []).append(payload)
    return grouped


def result_for_criterion(outputs: list[dict[str, Any]]) -> tuple[bool, int, str]:
    issues: list[dict[str, Any]] = []
    for output in outputs:
        local_issues = output.get("issues")
        if isinstance(local_issues, list):
            issues.extend(issue for issue in local_issues if isinstance(issue, dict))
    comments = [str(issue.get("issue_comment") or "").strip() for issue in issues]
    comments = [comment for comment in comments if comment]
    return bool(issues), len(issues), " | ".join(comments)


async def run_one(
    client: httpx.AsyncClient,
    settings: Settings,
    current_run_id: str,
    run_dir: Path,
    contract: Path,
) -> dict[str, Any]:
    contract_id = contract.stem
    started = monotonic()
    record: dict[str, Any] = {
        "contract_id": contract_id,
        "contract_file": contract.name,
        "task_id": "",
        "status": "failed",
        "error": "",
        "output_docx": "",
        "criterion_outputs": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": "",
        "duration_seconds": 0.0,
        "within_5_minutes": False,
    }
    try:
        task_id = await submit_job(client, settings, contract, f"{current_run_id}-{contract_id}")
        record["task_id"] = task_id
        print(f"submitted {contract_id}: task_id={task_id}")
        status = await wait_for_task(client, settings, task_id)
        record["status"] = str(status.get("status") or "")
        if record["status"] != "succeeded":
            record["error"] = json.dumps(status.get("error") or status, ensure_ascii=False)
            return record
        output_path = run_dir / "annotated" / f"{contract_id}_已审查.docx"
        await download_result(client, settings, task_id, output_path)
        record["output_docx"] = str(output_path)
        record["criterion_outputs"] = collect_outputs(settings.api_task_root, task_id)
        print(f"succeeded {contract_id}: {output_path}")
        return record
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"failed {contract_id}: {record['error']}", file=sys.stderr)
        return record
    finally:
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        record["duration_seconds"] = round(monotonic() - started, 3)
        record["within_5_minutes"] = record["duration_seconds"] < 300.0


def read_expected_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError(f"expected cases CSV is empty: {path}")
    counts: dict[str, set[int]] = {}
    for row in rows:
        counts.setdefault(row["contract_id"], set()).add(int(row["criterion_id"]))
    expected_criteria = set(range(1, 19))
    invalid = {contract_id: sorted(values) for contract_id, values in counts.items() if values != expected_criteria}
    if invalid:
        raise ValueError(f"each contract must contain criteria 1..18: {invalid}")
    return rows


def write_recall_csv(path: Path, expected: list[dict[str, str]], records: list[dict[str, Any]]) -> tuple[int, int]:
    by_contract = {str(record["contract_id"]): record for record in records}
    fieldnames = (
        "case_id",
        "contract_id",
        "contract_file",
        "criterion_id",
        "expected_problem",
        "expected_location",
        "expected_quote",
        "system_reported",
        "issue_count",
        "system_comments",
        "task_id",
        "task_status",
        "output_docx",
        "manual_correct",
        "manual_note",
    )
    detected_count = 0
    rows = []
    for expected_row in expected:
        contract_id = expected_row["contract_id"]
        criterion_id = int(expected_row["criterion_id"])
        record = by_contract.get(contract_id, {})
        outputs = record.get("criterion_outputs", {}).get(criterion_id, [])
        detected, issue_count, comments = result_for_criterion(outputs)
        detected_count += int(detected)
        rows.append(
            {
                "case_id": expected_row["case_id"],
                "contract_id": contract_id,
                "contract_file": expected_row["contract_file"],
                "criterion_id": criterion_id,
                "expected_problem": expected_row["expected_problem"],
                "expected_location": expected_row["expected_location"],
                "expected_quote": expected_row["expected_quote"],
                "system_reported": "yes" if detected else "no",
                "issue_count": issue_count,
                "system_comments": comments,
                "task_id": record.get("task_id", ""),
                "task_status": record.get("status", "missing"),
                "output_docx": record.get("output_docx", ""),
                "manual_correct": "",
                "manual_note": "",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return detected_count, len(rows)


async def run_all(settings: Settings) -> int:
    expected = read_expected_rows(settings.expected_csv)
    if settings.contract_ids:
        requested = set(settings.contract_ids)
        expected = [row for row in expected if row["contract_id"] in requested]
        found = {row["contract_id"] for row in expected}
        missing = sorted(requested - found)
        if missing:
            raise ValueError(f"requested contract IDs were not found in expected CSV: {missing}")
    contract_ids = sorted({row["contract_id"] for row in expected})
    contracts = [settings.cases_dir / f"{contract_id}.docx" for contract_id in contract_ids]
    missing = [str(path) for path in contracts if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"test contract files were not found: {missing}")
    current_run_id = run_id()
    run_dir = settings.results_root / current_run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    timeout = httpx.Timeout(settings.http_timeout)
    semaphore = asyncio.Semaphore(settings.concurrency)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def limited(contract: Path) -> dict[str, Any]:
            async with semaphore:
                return await run_one(client, settings, current_run_id, run_dir, contract)

        records = await asyncio.gather(*(limited(contract) for contract in contracts))

    (run_dir / "tasks.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    detected, total = write_recall_csv(run_dir / "recall.csv", expected, records)
    failed = sum(record.get("status") != "succeeded" for record in records)
    print(f"Raw criterion recall: {detected}/{total} = {detected / total:.2%}")
    print(f"Failed contracts: {failed}")
    print(f"Results: {run_dir}")
    return 1 if failed else 0


def main() -> None:
    args = parse_args()
    settings = load_settings(args)
    with keep_system_awake():
        raise SystemExit(asyncio.run(run_all(settings)))


if __name__ == "__main__":
    main()
