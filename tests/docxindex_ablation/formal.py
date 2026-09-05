"""Submit paired reviews to isolated instances of the real project API.

Uses existing submission/poll/download helpers. It does not treat issue counts
as recall and does not implement or replace the review workflow.
"""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx

from tests.criteria_formal_recall.run_cases import keep_system_awake, load_settings, run_one
from tests.docxindex_ablation.run import ROOT, SUITE, read_json, sha, write_json


async def formal(args):
    manifest = read_json(args.output / "manifest.json")
    target = args.output / "formal"
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for number, contract in enumerate(args.contracts):
        doc = SUITE / "cases" / f"{contract}.docx"
        if sha(doc) != manifest["contracts"][contract]["sha256"]:
            raise ValueError(f"Changed contract: {contract}")
        order = ("baseline", "safe") if number % 2 == 0 else ("safe", "baseline")
        for variant in order:
            directory = target / variant
            directory.mkdir(exist_ok=True)
            filename = directory / f"{contract}.json"
            if filename.exists():
                saved = read_json(filename)
                if saved["status"] != "succeeded":
                    if not args.retry_failed:
                        raise ValueError(f"Existing failed formal task requires explicit investigation: {filename}")
                    write_json(directory / "attempts" / f"{contract}-{saved['task_id']}.json", saved)
                else:
                    records.append(saved)
                    continue
            settings = load_settings(
                SimpleNamespace(
                    base_url=f"http://127.0.0.1:{5101 if variant == 'baseline' else 5102}",
                    cases_dir=SUITE / "cases",
                    expected_csv=SUITE / "expected_cases_10.csv",
                    criteria=ROOT / "resources" / "criteria" / "criteria-formal.docx",
                    results_dir=directory,
                    api_task_root=ROOT / "data" / "api" / f"docxindex_ablation_{variant}" / "api",
                    concurrency=1,
                    poll_interval=5.0,
                    task_timeout=args.task_timeout,
                    http_timeout=60.0,
                    contract_ids=[],
                )
            )
            print(f"{contract} {variant}: submitting actual API review", flush=True)
            async with httpx.AsyncClient(timeout=settings.http_timeout, trust_env=False) as client:
                record = await run_one(client, settings, args.output.name + "-" + variant, directory, doc)
            record["experiment_variant"] = variant
            record["scope"] = (
                "real app API, workflow, MCP, reviewer, reflector, summarizer, DOCX writer; isolated data root"
            )
            write_json(filename, record)
            records.append(record)
            write_json(target / "tasks.json", records)
            print(
                f"{contract} {variant}: {record['status']}, task={record['task_id']}, {record['duration_seconds']}s",
                flush=True,
            )
            if record["status"] != "succeeded":
                raise RuntimeError(f"Formal review did not succeed: {contract} {variant}; see {filename}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contracts", nargs="+", default=[f"D{i:02}" for i in range(1, 11)])
    parser.add_argument("--task-timeout", type=float, default=900.0)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Only after investigating the cause: archive failed records and resubmit; never silently retry",
    )
    args = parser.parse_args()
    with keep_system_awake():
        asyncio.run(formal(args))


if __name__ == "__main__":
    main()
