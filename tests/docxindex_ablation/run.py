"""Capture real docxindex calls, replay local treatments, and report evidence.

Run from repository root: python -m tests.docxindex_ablation.run --help
"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from statistics import median
import subprocess
from threading import Lock
from time import perf_counter
from unittest.mock import patch

from tools.docxindex import retriever as retrieval
from tools.docxindex.llm.client import LLMClient
from tools.docxindex.llm.schemas import RouteResponse
from tools.docxindex.retrieval.route import load_criteria
from tools.docxindex.indexing import estimate_tokens
from tests.docxindex_ablation.variants import treatment


ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "criteria_formal_recall"
OPERATIONS = ("plan_route", "llm_query", "vector_search", "rerank_matches", "scan_matches")
ORIGINALS = {name: getattr(retrieval, name) for name in OPERATIONS}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def capture_query(engine, query):
    calls = []

    def wrapper(name):
        def call(*args, **kwargs):
            start = perf_counter()
            result = ORIGINALS[name](*args, **kwargs)
            record = {
                "function": name,
                "seconds": perf_counter() - start,
                "output": result.model_dump(mode="json") if hasattr(result, "model_dump") else deepcopy(result),
            }
            if name == "rerank_matches":
                record["query"], record["candidates"] = args[1], deepcopy(args[2])
            if name in {"llm_query", "vector_search"}:
                allowed = kwargs.get("allowed_node_ids")
                record["allowed_node_ids"] = sorted(allowed) if allowed is not None else None
            calls.append(record)
            return result

        return call

    with ExitStack() as stack:
        for name in OPERATIONS:
            stack.enter_context(patch.object(retrieval, name, wrapper(name)))
        start = perf_counter()
        output = engine._search_sync(query)
    return {"query": query, "calls": calls, "output": output, "seconds": perf_counter() - start}


def capture(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    csv_path = SUITE / "expected_cases_10.csv"
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        expected = list(csv.DictReader(handle))
    engine = retrieval.DocxIndexRetriever(ROOT / "config.yaml")
    criteria = load_criteria(SUITE.parents[1] / "resources/criteria/criteria-formal.docx", 18)
    manifest = {
        "git": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "expected_sha256": sha(csv_path),
        "criteria_sha256": sha(ROOT / "resources/criteria/criteria-formal.docx"),
        "config_sha256": sha(ROOT / "config.yaml"),
        "model": engine.llm_settings.model,
        "retrieval": engine.retrieval_config.model_dump(),
        "indexing": engine.indexing_config.model_dump(),
        "contracts": {},
        "started": datetime.now().isoformat(),
        "scope": "docxindex tool calls, not end-to-end review",
    }
    if (out / "manifest.json").exists():
        old = read_json(out / "manifest.json")
        for key in ("git", "config_sha256", "expected_sha256", "criteria_sha256"):
            if old[key] != manifest[key]:
                raise ValueError(f"Resume baseline mismatch: {key}")
        manifest = old
    write_json(out / "manifest.json", manifest)
    original_model = LLMClient.complete_model
    original_request = engine.llm_client.client.chat.completions.__class__.create
    model_rows = []
    lock = Lock()

    def model_call(client, prompt, schema, *a, **kw):
        start = perf_counter()
        try:
            result = original_model(client, prompt, schema, *a, **kw)
            row = {
                "schema": schema.__name__,
                "prompt": prompt,
                "response": result.model_dump(mode="json"),
                "seconds": perf_counter() - start,
            }
        except Exception as exc:
            row = {
                "schema": schema.__name__,
                "prompt": prompt,
                "error_type": type(exc).__name__,
                "seconds": perf_counter() - start,
            }
            with lock:
                model_rows.append(row)
            raise
        with lock:
            model_rows.append(row)
        return result

    # Capture provider usage as returned, without recording credentials or URLs.
    def request_call(client, *a, **kw):
        response = original_request(client, *a, **kw)
        usage = response.usage.model_dump() if response.usage else None
        with lock:
            model_rows.append({"provider_usage": usage, "model": response.model})
        return response

    with (
        patch.object(LLMClient, "complete_model", model_call),
        patch.object(engine.llm_client.client.chat.completions.__class__, "create", request_call),
    ):
        for contract in args.contracts:
            doc = SUITE / "cases" / f"{contract}.docx"
            records = [r for r in expected if r["contract_id"] == contract]
            if len(records) != 18 or sha(doc) != records[0]["modified_sha256"]:
                raise ValueError(f"Contract/expected hash mismatch: {contract}")
            directory = out / contract
            directory.mkdir(exist_ok=True)
            if (directory / "complete.json").exists():
                print(f"{contract}: already captured", flush=True)
                continue
            if (directory / "index.json").exists():
                raise ValueError(
                    f"Partial capture cannot reuse a rebuilt LLM index: {contract}; use a new output directory"
                )
            model_rows.clear()
            print(f"{contract}: building configured production index", flush=True)
            start = perf_counter()
            try:
                build = engine._build_index_sync(doc)
                write_json(directory / "index.json", engine._index)
                manifest["contracts"][contract] = {
                    "sha256": sha(doc),
                    "build_seconds": perf_counter() - start,
                    "build": build,
                }
                write_json(out / "manifest.json", manifest)
                write_json(directory / "build_models.json", model_rows)
                for number, criterion in sorted(criteria.items()):
                    model_rows.clear()
                    filename = directory / f"C{number:02}.json"
                    print(f"{contract} C{number:02}: real retrieval", flush=True)
                    try:
                        query_result = capture_query(engine, criterion)
                        query_result["models"] = list(model_rows)
                        query_result["case_id"] = f"{contract}-C{number:02}"
                        write_json(filename, query_result)
                    except Exception as exc:
                        write_json(
                            directory / f"C{number:02}.error.json",
                            {"error_type": type(exc).__name__, "models": model_rows},
                        )
                        raise
                write_json(directory / "complete.json", {"queries": len(criteria)})
            except Exception as exc:
                write_json(directory / "error.json", {"error_type": type(exc).__name__})
                raise


def replay(engine, recorded, variant="baseline", rerank_overrides=None):
    positions = {name: 0 for name in OPERATIONS}
    tapes = {name: [r for r in recorded["calls"] if r["function"] == name] for name in OPERATIONS}
    metrics = {"skipped_calls": 0, "affected": 0, "rerank_input_changed": 0}
    structures = []

    def wrapper(name):
        def call(*args, **kwargs):
            nonlocal structures
            pos = positions[name]
            positions[name] += 1
            row = tapes[name][pos]
            if name == "llm_query":
                structures = [r["node_id"] for r in row["output"]][
                    : engine.retrieval_config.semantic.structure_candidate_limit
                ]
            if name == "vector_search" and variant in {"E02", "combined"}:
                allowed = kwargs.get("allowed_node_ids")
                if allowed is not None and allowed.issubset(set(structures)):
                    metrics["affected"] += 1
                    metrics["skipped_calls"] += 1
                    return []
            if name == "rerank_matches":
                if args[2] != row["candidates"]:
                    metrics["rerank_input_changed"] += 1
                if variant in {"E01", "combined"} and len(args[2]) == 1:
                    metrics["affected"] += 1
                    metrics["skipped_calls"] += 1
                    return deepcopy(args[2])
                if rerank_overrides is not None and pos in rerank_overrides:
                    metrics["affected"] += 1
                    return deepcopy(rerank_overrides[pos])
                if args[2] != row["candidates"] and variant == "E03":
                    raise ValueError("E03 changed candidates require live paired rerank")
            if name == "plan_route":
                return RouteResponse.model_validate(row["output"])
            return deepcopy(row["output"])

        return call

    start = perf_counter()
    with ExitStack() as stack:
        stack.enter_context(treatment(variant, engine._index))
        stack.enter_context(patch.object(retrieval, "_terminal", lambda message: None))
        for name in OPERATIONS:
            stack.enter_context(patch.object(retrieval, name, wrapper(name)))
        result = engine._search_sync(recorded["query"])
    if any(positions[name] != len(tapes[name]) for name in OPERATIONS):
        raise AssertionError("Replay did not consume all captured operations")
    return result, metrics, perf_counter() - start


def anchor_texts(text):
    pattern = r"xml_anchor_id: ([^\n]+)\n内容：\n(.*?)(?=\n\n---\n\n|\Z)"
    return dict(re.findall(pattern, text, flags=re.S))


def offline(args):
    engine = retrieval.DocxIndexRetriever(ROOT / "config.yaml")
    rows, differences = [], []
    for contract in args.contracts:
        directory = args.output / contract
        if not (directory / "complete.json").exists():
            continue
        engine._index = read_json(directory / "index.json")
        engine._vector_index = object()  # The vector operation itself is strictly replayed.
        for file in sorted(directory.glob("C[0-9][0-9].json")):
            recorded = read_json(file)
            baseline, _, _ = replay(engine, recorded)
            if baseline != recorded["output"]:
                raise AssertionError(f"Baseline replay mismatch: {file}")
            for variant in ("E01", "E02", "E04", "E05", "E06", "combined"):
                times_b, times_v = [], []
                for repeat in range(args.repeats):
                    # Alternate execution order to reduce warm-up/order bias.
                    order = ("baseline", variant) if repeat % 2 == 0 else (variant, "baseline")
                    for name in order:
                        output, metric, seconds = replay(engine, recorded, name)
                        if name == "baseline":
                            times_b.append(seconds)
                        else:
                            times_v.append(seconds)
                            result, effect = output, metric
                before, after = anchor_texts(baseline), anchor_texts(result)
                changed = {a for a in before.keys() | after.keys() if before.get(a) != after.get(a)}
                for anchor in sorted(changed):
                    differences.append(
                        {
                            "experiment": variant,
                            "case_id": recorded["case_id"],
                            "anchor": anchor,
                            "before": before.get(anchor, ""),
                            "after": after.get(anchor, ""),
                        }
                    )
                rows.append(
                    {
                        "experiment": variant,
                        "case_id": recorded["case_id"],
                        "exact_equal": baseline == result,
                        "anchor_equal": before == after,
                        "changed_anchors": len(changed),
                        "skipped_calls": effect["skipped_calls"],
                        "rerank_input_changed": effect["rerank_input_changed"],
                        "baseline_ms": round(median(times_b) * 1000, 3),
                        "variant_ms": round(median(times_v) * 1000, 3),
                        "baseline_output_tokens_estimate": estimate_tokens(baseline),
                        "variant_output_tokens_estimate": estimate_tokens(result),
                    }
                )
        print(f"{contract}: replay complete", flush=True)
    if rows:
        write_csv(args.output / "ablation_summary.csv", rows, list(rows[0]))
    write_csv(args.output / "evidence_diffs.csv", differences, ["experiment", "case_id", "anchor", "before", "after"])
    write_json(
        args.output / "offline_summary.json",
        {
            name: {
                "queries": len(selected),
                "exact_equal": sum(r["exact_equal"] for r in selected),
                "anchor_equal": sum(r["anchor_equal"] for r in selected),
                "skipped_calls": sum(r["skipped_calls"] for r in selected),
                "rerank_input_changed": sum(r["rerank_input_changed"] for r in selected),
                "baseline_ms": sum(r["baseline_ms"] for r in selected),
                "variant_ms": sum(r["variant_ms"] for r in selected),
            }
            for name in sorted({r["experiment"] for r in rows})
            if (selected := [r for r in rows if r["experiment"] == name])
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["capture", "replay"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contracts", nargs="+", default=[f"D{i:02}" for i in range(1, 11)])
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.command == "capture":
        capture(args)
    else:
        offline(args)


if __name__ == "__main__":
    main()
