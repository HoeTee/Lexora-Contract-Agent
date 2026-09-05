"""Paired real reranks for changed prompt inputs on a frozen index and route.

Upstream route/structure/vector responses stay frozen. Only reranking uses LLM.
Field screening uses the largest rerank payload per contract, not all queries.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from tools.docxindex import retriever as retrieval
from tools.docxindex.llm.schemas import RerankResponse
from tools.docxindex.indexing import estimate_tokens
from tests.docxindex_ablation.run import ROOT, read_json, write_json, write_csv, replay, anchor_texts
from tests.docxindex_ablation.variants import ORIGINAL_MERGE, ORIGINAL_RERANK, RERANK_MODULE


def interventions(recorded, limit):
    structure, vector, allowed = [], [], None
    number = 0
    for call in recorded["calls"]:
        name = call["function"]
        if name == "llm_query":
            structure, vector = call["output"][:limit], []
            allowed = call["allowed_node_ids"]
        elif name == "vector_search":
            vector = call["output"]
        elif name == "rerank_matches":
            variants = {}
            expanded = ORIGINAL_MERGE(structure, vector, max_unique_nodes=None)[0]
            if expanded != call["candidates"]:
                variants["E03"] = expanded
            if allowed is not None and set(allowed).issubset({r["node_id"] for r in structure}) and vector:
                variants["E02"] = ORIGINAL_MERGE(structure, [], max_unique_nodes=None)[0]
            yield number, call, variants
            number += 1


def request(client, index, query, candidates, budget, field=None):
    view = RERANK_MODULE._fit_candidates(index, candidates, budget)
    if field:
        view = [{k: v for k, v in row.items() if k != field} for row in view]
    prompt = RERANK_MODULE._rerank_prompt(query, json.dumps(view, ensure_ascii=False))
    start = perf_counter()
    response = client.complete_model(prompt, RerankResponse)
    seconds = perf_counter() - start
    frozen = SimpleNamespace(settings=client.settings, complete_model=lambda *a: response)
    ranked = ORIGINAL_RERANK(index, query, candidates, frozen, budget, None)
    return {
        "prompt": prompt,
        "input_tokens_estimate": estimate_tokens(prompt),
        "response": response.model_dump(mode="json"),
        "ranked": ranked,
        "seconds": seconds,
    }


def run_pair(task, client, output, budget, repeats):
    index, recorded, number, call, variants = task
    directory = output / "live" / f"{recorded['case_id']}-R{number}"
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for repetition in range(repeats):
        order = ["baseline", *variants] if repetition % 2 == 0 else [*reversed(variants), "baseline"]
        for variant in order:
            filename = directory / f"{variant}-{repetition}.json"
            files.append(filename)
            if filename.exists():
                continue
            candidates = call["candidates"] if variant == "baseline" else variants[variant]
            field = {"F01": "node_type", "F02": "token_estimate", "F03": "sources"}.get(variant)
            result = request(client, index, call["query"], candidates, budget, field)
            result.update(case_id=recorded["case_id"], rerank_number=number, experiment=variant, repetition=repetition)
            write_json(filename, result)
    return task, files


def live(args):
    engine = retrieval.DocxIndexRetriever(ROOT / "config.yaml")
    tasks = []
    for directory in sorted(args.output.glob("D[0-9][0-9]")):
        if not (directory / "complete.json").exists():
            continue
        index = read_json(directory / "index.json")
        contract_calls = []
        for file in sorted(directory.glob("C[0-9][0-9].json")):
            recorded = read_json(file)
            for number, call, variants in interventions(
                recorded, engine.retrieval_config.semantic.structure_candidate_limit
            ):
                if len(call["candidates"]) > 1:
                    contract_calls.append([index, recorded, number, call, variants])
                elif variants:
                    # Single-node ordering cannot change; covered by E01 proof.
                    write_json(
                        args.output / "live" / f"{recorded['case_id']}-R{number}-singleton.json",
                        {
                            "case_id": recorded["case_id"],
                            "variants": list(variants),
                            "status": "no live rerank required: only one possible selected node",
                        },
                    )
        if contract_calls:
            largest = max(contract_calls, key=lambda t: len(json.dumps(t[3]["candidates"], ensure_ascii=False)))
            for field in ("F01", "F02", "F03"):
                largest[4][field] = largest[3]["candidates"]
            tasks.extend(t for t in contract_calls if t[4])
    write_json(
        args.output / "live_plan.json",
        {
            "repeats": args.repeats,
            "workers": args.workers,
            "field_scope": "one largest candidate payload per completed contract; screening only",
            "tasks": [{"case_id": t[1]["case_id"], "rerank_number": t[2], "experiments": list(t[4])} for t in tasks],
        },
    )
    rows = []
    # Model work is concurrent; replay uses global function patches and runs serially.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [
            pool.submit(
                run_pair, task, engine.llm_client, args.output, engine.retrieval_config.input_tokens, args.repeats
            )
            for task in tasks
        ]
        completed = []
        for future in as_completed(pending):
            task, files = future.result()
            completed.append((task, files))
            print(f"{task[1]['case_id']} R{task[2]}: paired reranks complete", flush=True)
    for task, files in completed:
        index, recorded, number, call, variants = task
        engine._index, engine._vector_index = index, object()
        results = {(r["experiment"], r["repetition"]): r for f in files if (r := read_json(f))}
        for repetition in range(args.repeats):
            baseline = results["baseline", repetition]
            before = replay(engine, recorded, rerank_overrides={number: baseline["ranked"]})[0]
            for variant in variants:
                result = results[variant, repetition]
                # E03 may affect several semantic steps; freeze other reranks.
                # Apply candidate override only at this selected rerank boundary.
                after = replay(engine, recorded, rerank_overrides={number: result["ranked"]})[0]
                old, new = anchor_texts(before), anchor_texts(after)
                rows.append(
                    {
                        "case_id": recorded["case_id"],
                        "rerank_number": number,
                        "experiment": variant,
                        "repetition": repetition,
                        "same_final_text": before == after,
                        "same_anchor_evidence": old == new,
                        "removed_anchors": json.dumps(sorted(old.keys() - new.keys())),
                        "added_anchors": json.dumps(sorted(new.keys() - old.keys())),
                        "baseline_prompt_tokens_estimate": baseline["input_tokens_estimate"],
                        "variant_prompt_tokens_estimate": result["input_tokens_estimate"],
                        "baseline_seconds": baseline["seconds"],
                        "variant_seconds": result["seconds"],
                    }
                )
                write_json(
                    args.output / "live" / f"{recorded['case_id']}-R{number}" / f"{variant}-{repetition}-evidence.json",
                    {"baseline": before, "variant": after},
                )
    if rows:
        write_csv(args.output / "live_summary.csv", rows, list(rows[0]))
    print(f"Live paired comparison rows: {len(rows)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 3 or not 1 <= args.workers <= 3:
        parser.error("Use at least three repeats and one to three model workers")
    live(args)


if __name__ == "__main__":
    main()
