"""Measure real reranker local work using captured model responses; no network."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import re
from statistics import median
from threading import Lock
from time import perf_counter, sleep
from types import SimpleNamespace
from unittest.mock import patch

from tools.docxindex.llm.client import LLMClient, LLMSettings
from tools.docxindex.llm.schemas import RerankResponse
from tools.docxindex.retrieval import content
from tools.docxindex import retriever as retrieval
from tests.docxindex_ablation.run import ROOT, SUITE, read_json, write_csv, write_json, anchor_texts
from tests.docxindex_ablation.variants import ORIGINAL_RERANK, treatment


def concurrency_check():
    """Exercise the real LLMClient limiter with a fake HTTP transport only."""
    index = {"nodes": [{"node_id": "a", "text": "A"}]}
    candidates = [{"node_id": "a"}]
    client = LLMClient(LLMSettings(model="offline", api_key="offline"), max_concurrency=2)
    client._start_interval_seconds = 0
    active = maximum = 0
    lock = Lock()

    def transport(**kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            sleep(0.02)
            message = SimpleNamespace(content='{"nodes":[{"node_id":"a","score":1}]}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        finally:
            with lock:
                active -= 1

    results = {}
    with patch.object(client.client.chat.completions, "create", transport):
        for variant in ("baseline", "E08"):
            maximum = 0
            with treatment(variant, index), ThreadPoolExecutor(max_workers=6) as pool:
                outputs = list(
                    pool.map(
                        lambda _: ORIGINAL_RERANK(index, "q", candidates, client, 1000, None, concurrency=1), range(12)
                    )
                )
            results[variant] = {
                "requests": len(outputs),
                "max_transport_concurrency": maximum,
                "all_outputs_equal": all(r == outputs[0] for r in outputs),
            }
            if maximum != 2:
                raise AssertionError(f"Client concurrency changed: {variant}={maximum}")
    client.client.close()
    return results


def analyze(output, repeats):
    engine = retrieval.DocxIndexRetriever(ROOT / "config.yaml")
    with (SUITE / "expected_cases_10.csv").open(encoding="utf-8-sig", newline="") as handle:
        expected = {row["case_id"]: row for row in csv.DictReader(handle)}
    rows, budgets, quotes = [], [], []
    for directory in sorted(output.glob("D[0-9][0-9]")):
        if not (directory / "complete.json").exists():
            continue
        index = read_json(directory / "index.json")
        for file in sorted(directory.glob("C[0-9][0-9].json")):
            recorded = read_json(file)
            responses = [r["response"] for r in recorded["models"] if r.get("schema") == "RerankResponse"]
            calls = [r for r in recorded["calls"] if r["function"] == "rerank_matches"]
            if len(responses) != len(calls):
                raise AssertionError(f"Rerank response count mismatch: {file}")
            for n, (call, response) in enumerate(zip(calls, responses, strict=True)):
                client = SimpleNamespace(
                    settings=engine.llm_settings,
                    complete_model=lambda *a, response=response: RerankResponse.model_validate(response),
                )
                for variant in ("E07", "E08"):
                    timings = {"baseline": [], variant: []}
                    for repeat in range(repeats):
                        order = ("baseline", variant) if repeat % 2 == 0 else (variant, "baseline")
                        for name in order:
                            with treatment(name, index):
                                start = perf_counter()
                                result = ORIGINAL_RERANK(
                                    index,
                                    call["query"],
                                    call["candidates"],
                                    client,
                                    engine.retrieval_config.input_tokens,
                                    None,
                                )
                                timings[name].append(perf_counter() - start)
                            if result != call["output"]:
                                raise AssertionError(f"Real reranker replay mismatch: {file} {variant}")
                    rows.append(
                        {
                            "case_id": recorded["case_id"],
                            "rerank_number": n,
                            "experiment": variant,
                            "exact_equal": True,
                            "candidate_count": len(call["candidates"]),
                            "baseline_ms": median(timings["baseline"]) * 1000,
                            "variant_ms": median(timings[variant]) * 1000,
                        }
                    )

            anchors = anchor_texts(recorded["output"])
            quote = expected[recorded["case_id"]]["expected_quote"]
            quote_parts = [p.strip() for p in re.split(r"\s+(?:/|\|\|)\s+", quote) if p.strip()]
            quotes.append(
                {
                    "case_id": recorded["case_id"],
                    "expected_quote": quote,
                    "exact_quote_parts_present": sum(p in recorded["output"] for p in quote_parts),
                    "quote_parts": len(quote_parts),
                    "returned_anchors": len(anchors),
                    "note": "Literal quote coverage only; not issue recall or review precision",
                }
            )
            # Content formatter rereads raw anchor text instead of content_item.text.
            if file.name != "C01.json":
                continue
            for node in index["nodes"]:
                context = content.build_content_context(
                    index, [{"node_id": node["node_id"]}], engine.retrieval_config.output_tokens
                )
                selected = context["content_context"]
                if not selected:
                    continue
                internal = "\n".join(r.get("text") or "" for r in selected)
                rendered = retrieval._format_anchor_context(index, selected)
                raw_text = "\n".join(anchor_texts(rendered).values())
                if internal.strip() != raw_text.strip():
                    budgets.append(
                        {
                            "contract_id": directory.name,
                            "node_id": node["node_id"],
                            "internal_text": internal,
                            "rendered_raw_text": raw_text,
                            "internal_budget_used": context["budget"]["used_tokens"],
                            "scope": "single-node synthetic boundary check; not actual query budget",
                        }
                    )
        # All quote observations are deliberately separate from recall metrics.
    write_csv(
        output / "local_rerank.csv",
        rows,
        ["case_id", "rerank_number", "experiment", "exact_equal", "candidate_count", "baseline_ms", "variant_ms"],
    )
    write_csv(
        output / "content_boundary_diffs.csv",
        budgets,
        ["contract_id", "node_id", "internal_text", "rendered_raw_text", "internal_budget_used", "scope"],
    )
    write_csv(
        output / "quote_observations.csv",
        quotes,
        ["case_id", "expected_quote", "exact_quote_parts_present", "quote_parts", "returned_anchors", "note"],
    )
    write_json(output / "concurrency.json", concurrency_check())
    write_json(
        output / "local_summary.json",
        {"calls": len(rows), "all_equal": all(r["exact_equal"] for r in rows), "boundary_differences": len(budgets)},
    )
    print(json.dumps({"local_rerank_rows": len(rows), "boundary_differences": len(budgets)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=11)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    analyze(args.output, args.repeats)


if __name__ == "__main__":
    main()
