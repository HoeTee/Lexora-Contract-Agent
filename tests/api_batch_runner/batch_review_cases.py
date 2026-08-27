from __future__ import annotations

import asyncio
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from dotenv import load_dotenv


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class SubmitJobError(Exception):
    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(json.dumps(data, ensure_ascii=False))
        self.data = data
        self.task_id = str(data.get("task_id") or "")


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    concurrency: int
    poll_interval_seconds: float
    task_timeout_seconds: float
    http_timeout_seconds: float
    http_connect_timeout_seconds: float
    cases_dir: Path
    output_dir: Path
    log_dir: Path
    overwrite: bool
    llm_api_key: str
    llm_model_name: str
    embedding_api_key: str
    embedding_model_name: str
    reranker_api_key: str
    reranker_model_name: str


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required in .env.")
    return value.strip()


def optional_env(name: str) -> str:
    value = os.getenv(name)
    if value is None:
        return ""
    return value.strip()


def env_int(name: str, *, default: int, minimum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}.")
    return value


def load_settings() -> Settings:
    load_dotenv()
    root = Path(__file__).resolve().parent
    base_url = require_env("BASE_URL").rstrip("/")
    api_key = require_env("API_KEY")
    return Settings(
        base_url=base_url,
        api_key=api_key,
        concurrency=env_int("CONCURRENCY", default=3, minimum=1),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", default=5, minimum=1),
        task_timeout_seconds=env_int("TASK_TIMEOUT_SECONDS", default=1800, minimum=1),
        http_timeout_seconds=env_int("HTTP_TIMEOUT_SECONDS", default=300, minimum=1),
        http_connect_timeout_seconds=env_int("HTTP_CONNECT_TIMEOUT_SECONDS", default=10, minimum=1),
        cases_dir=(root / os.getenv("CASES_DIR", "cases")).resolve(),
        output_dir=(root / os.getenv("OUTPUT_DIR", "outputs")).resolve(),
        log_dir=(root / os.getenv("LOG_DIR", "logs")).resolve(),
        overwrite=parse_bool(os.getenv("OVERWRITE"), default=False),
        llm_api_key=optional_env("LLM_API_KEY"),
        llm_model_name=optional_env("LLM_MODEL_NAME"),
        embedding_api_key=optional_env("EMBEDDING_API_KEY"),
        embedding_model_name=optional_env("EMBEDDING_MODEL_NAME"),
        reranker_api_key=optional_env("RERANKER_API_KEY"),
        reranker_model_name=optional_env("RERANKER_MODEL_NAME"),
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_stamp() -> str:
    return datetime.now().strftime("run_%Y%m%d_%H%M%S")


def format_elapsed(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s"


def output_name(input_path: Path) -> str:
    stem = input_path.stem or "审核结果"
    return f"【已AI审查】{stem}.docx"


def request_model_config_fields(settings: Settings) -> dict[str, str]:
    fields = {
        "llm_api_key": settings.llm_api_key,
        "llm_model_name": settings.llm_model_name,
        "embedding_api_key": settings.embedding_api_key,
        "embedding_model_name": settings.embedding_model_name,
        "reranker_api_key": settings.reranker_api_key,
        "reranker_model_name": settings.reranker_model_name,
    }
    return {key: value for key, value in fields.items() if value}


def list_case_files(cases_dir: Path) -> list[Path]:
    if not cases_dir.exists():
        raise FileNotFoundError(f"CASES_DIR does not exist: {cases_dir}")
    return sorted(path for path in cases_dir.glob("*.docx") if path.is_file())


async def submit_job(client: httpx.AsyncClient, settings: Settings, input_path: Path) -> str:
    with input_path.open("rb") as f:
        files = {
            "file": (
                input_path.name,
                f,
                DOCX_MEDIA_TYPE,
            )
        }
        response = await client.post(
            f"{settings.base_url}/api/review/jobs",
            headers={"Authorization": settings.api_key},
            data=request_model_config_fields(settings),
            files=files,
        )
    data = parse_json_response(response)
    if response.status_code >= 400 and data.get("task_id"):
        raise SubmitJobError(data)
    response.raise_for_status()
    status = data.get("status")
    if status == "failed":
        raise SubmitJobError(data)
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError(f"Submit response did not include task_id: {data}")
    return task_id


async def poll_status(client: httpx.AsyncClient, settings: Settings, task_id: str) -> dict[str, Any]:
    deadline = monotonic() + settings.task_timeout_seconds
    while True:
        response = await client.post(
            f"{settings.base_url}/api/review/jobs/status",
            headers={"Authorization": settings.api_key, "Content-Type": "application/json"},
            json={"task_id": task_id},
        )
        data = parse_json_response(response)
        response.raise_for_status()
        status = data.get("status")
        if status in TERMINAL_STATUSES:
            return data
        if monotonic() >= deadline:
            raise TimeoutError(f"Task timed out: {task_id}, last_status={status}")
        await asyncio.sleep(settings.poll_interval_seconds)


async def download_result(
    client: httpx.AsyncClient,
    settings: Settings,
    task_id: str,
    output_path: Path,
) -> None:
    response = await client.post(
        f"{settings.base_url}/api/review/jobs/result",
        headers={"Authorization": settings.api_key, "Content-Type": "application/json"},
        json={"task_id": task_id, "output_type": "file"},
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if DOCX_MEDIA_TYPE not in content_type:
        raise RuntimeError(f"Result response is not DOCX: content-type={content_type}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(response.content)


def parse_json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Response body is not JSON: HTTP {response.status_code}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Response JSON is not an object: {data}")
    return data


async def run_one_case(
    client: httpx.AsyncClient,
    settings: Settings,
    run_dir: Path,
    input_path: Path,
) -> dict[str, Any]:
    started_at = now_iso()
    start = monotonic()
    task_id = ""
    output_path = settings.output_dir / output_name(input_path)
    record: dict[str, Any] = {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "task_id": task_id,
        "status": "failed",
        "submitted_at": started_at,
        "finished_at": None,
        "elapsed": "0m00s",
        "error": None,
    }
    try:
        if output_path.exists() and not settings.overwrite:
            record["status"] = "skipped"
            record["error"] = "Output file already exists."
            return finish_record(run_dir, record, start)

        task_id = await submit_job(client, settings, input_path)
        record["task_id"] = task_id
        status_data = await poll_status(client, settings, task_id)
        final_status = str(status_data.get("status") or "")
        record["status"] = final_status
        if final_status != "succeeded":
            record["error"] = json.dumps(status_data.get("error") or status_data, ensure_ascii=False)
            return finish_record(run_dir, record, start)
        await download_result(client, settings, task_id, output_path)
        return finish_record(run_dir, record, start)
    except SubmitJobError as exc:
        record["task_id"] = exc.task_id
        record["status"] = "failed"
        record["error"] = json.dumps(exc.data.get("error") or exc.data, ensure_ascii=False)
        return finish_record(run_dir, record, start)
    except Exception as exc:
        record["task_id"] = task_id
        record["status"] = "failed"
        record["error"] = repr(exc)
        return finish_record(run_dir, record, start)


def finish_record(run_dir: Path, record: dict[str, Any], start: float) -> dict[str, Any]:
    record["finished_at"] = now_iso()
    record["elapsed"] = format_elapsed(monotonic() - start)
    write_task_log(run_dir, record)
    print(
        f"{record['status']}: {Path(record['input_file']).name} "
        f"task_id={record['task_id'] or '-'} elapsed={record['elapsed']}"
    )
    if record.get("status") == "failed" and record.get("error"):
        print(f"error: {record['error']}")
    return record


def write_task_log(run_dir: Path, record: dict[str, Any]) -> None:
    task_id = str(record.get("task_id") or "no_task_id")
    task_dir = run_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_summary(run_dir: Path, records: list[dict[str, Any]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "batch_summary.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (run_dir / "batch_summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["input_file", "output_file", "task_id", "status", "elapsed", "error"],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "input_file": record.get("input_file", ""),
                    "output_file": record.get("output_file", ""),
                    "task_id": record.get("task_id", ""),
                    "status": record.get("status", ""),
                    "elapsed": record.get("elapsed", ""),
                    "error": record.get("error", ""),
                }
            )


async def run_batch() -> int:
    settings = load_settings()
    cases = list_case_files(settings.cases_dir)
    if not cases:
        print(f"No DOCX files found in: {settings.cases_dir}")
        return 1

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = settings.log_dir / run_stamp()
    run_dir.mkdir(parents=True, exist_ok=True)

    limits = httpx.Limits(max_connections=max(settings.concurrency * 2, 10))
    timeout = httpx.Timeout(
        settings.http_timeout_seconds,
        connect=settings.http_connect_timeout_seconds,
    )
    semaphore = asyncio.Semaphore(settings.concurrency)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def run_with_limit(path: Path) -> dict[str, Any]:
            async with semaphore:
                return await run_one_case(client, settings, run_dir, path)

        records = await asyncio.gather(*(run_with_limit(path) for path in cases))

    write_summary(run_dir, list(records))
    succeeded = sum(1 for item in records if item.get("status") == "succeeded")
    failed = sum(1 for item in records if item.get("status") == "failed")
    skipped = sum(1 for item in records if item.get("status") == "skipped")
    print(f"Summary: succeeded={succeeded}, failed={failed}, skipped={skipped}, total={len(records)}")
    print(f"Logs: {run_dir}")
    return 1 if failed else 0


def main() -> None:
    raise SystemExit(asyncio.run(run_batch()))


if __name__ == "__main__":
    main()
