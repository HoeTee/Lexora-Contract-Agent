"""Replay 90 captured queries through the actual single-candidate implementation."""

from collections import deque
from contextlib import ExitStack
from copy import deepcopy
import json
from unittest.mock import patch

from tools.docxindex import retriever as retrieval
from tools.docxindex.llm.schemas import RouteResponse
from tests.review_timing.run import OUTPUT, ROOT


def run():
    engine = retrieval.DocxIndexRetriever(ROOT / "config.yaml")
    capture = ROOT / "tests/docxindex_ablation/results/20260905-execution"
    rows = []
    for number in range(1, 6):
        directory = capture / f"D{number:02}"
        engine._index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        engine._vector_index = object()
        for path in sorted(directory.glob("C[0-9][0-9].json")):
            recorded = json.loads(path.read_text(encoding="utf-8"))
            names = ("plan_route", "llm_query", "vector_search", "rerank_matches", "scan_matches")
            tapes = {name: deque(row for row in recorded["calls"] if row["function"] == name and not (
                name == "rerank_matches" and len(row["candidates"]) == 1)) for name in names}
            skips = sum(row["function"] == "rerank_matches" and len(row["candidates"]) == 1
                        for row in recorded["calls"])

            def wrapper(name, tapes=tapes, path=path):
                def call(*args, **kwargs):
                    row = tapes[name].popleft()
                    if name == "rerank_matches":
                        assert args[2] == row["candidates"], path
                    if name in ("llm_query", "vector_search"):
                        allowed = kwargs.get("allowed_node_ids")
                        assert (sorted(allowed) if allowed is not None else None) == row["allowed_node_ids"], path
                    if name == "plan_route":
                        return RouteResponse.model_validate(row["output"])
                    return deepcopy(row["output"])
                return call

            with ExitStack() as stack:
                stack.enter_context(patch.object(retrieval, "_route_event"))
                stack.enter_context(patch.object(retrieval, "_terminal"))
                for name in names:
                    stack.enter_context(patch.object(retrieval, name, wrapper(name)))
                output = engine._search_sync(recorded["query"])
            assert not any(tapes.values()), path
            assert output == recorded["output"], path
            rows.append({"case": recorded["case_id"], "exact_equal": True, "skipped_reranks": skips})
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "single_candidate_replay.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("Exact original text and anchors:", len(rows), "Skipped reranks:", sum(r["skipped_reranks"] for r in rows))


if __name__ == "__main__":
    run()
