"""Submit five alternating version pairs; preserve tasks, logs and DOCX outputs."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
from dotenv import load_dotenv

from tests.criteria_formal_recall.run_cases import keep_system_awake, load_settings, run_one


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "tests/review_timing/outputs/20260906"


async def run():
    load_dotenv(ROOT / ".env")
    if not os.getenv("API_KEY"):
        raise RuntimeError("API_KEY must be supplied to the test driver")
    for number in range(1, 6):
        cid = f"D{number:02}"
        order = ("baseline", "latest") if number % 2 else ("latest", "baseline")
        for variant in order:
            directory = OUTPUT / variant
            directory.mkdir(parents=True, exist_ok=True)
            record_path = directory / f"{cid}.json"
            if record_path.exists():
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if record["status"] != "succeeded":
                    raise RuntimeError(f"Investigate saved failure: {record_path}")
                continue
            settings = load_settings(SimpleNamespace(
                base_url=f"http://127.0.0.1:{5111 if variant == 'baseline' else 5112}",
                cases_dir=ROOT / "tests/criteria_formal_recall/cases",
                expected_csv=ROOT / "tests/criteria_formal_recall/expected_cases_10.csv",
                results_dir=directory,
                api_task_root=ROOT / "data/api/review_timing" / variant / "api",
                concurrency=1, poll_interval=3.0, task_timeout=1200.0,
                http_timeout=60.0, contract_ids=[],
            ))
            settings = replace(settings, model_fields={})
            print(f"START {cid} {variant}", flush=True)
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                record = await run_one(client, settings, "timing-20260906", directory,
                                       settings.cases_dir / f"{cid}.docx")
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"END {cid} {variant} {record['status']} {record.get('task_id')}", flush=True)
            if record["status"] != "succeeded":
                raise RuntimeError(f"Formal review failed; inspect {record_path}")


if __name__ == "__main__":
    with keep_system_awake():
        asyncio.run(run())
