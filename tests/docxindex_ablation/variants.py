"""Single-operation treatments, scoped to an isolated experiment process."""

from bisect import bisect_left, bisect_right
from contextlib import ExitStack, contextmanager, nullcontext
import importlib
from unittest.mock import patch

from tools.docxindex import retriever as retrieval
from tools.docxindex.retrieval import content


RERANK_MODULE = importlib.import_module("tools.docxindex.retrieval.rerank")
ORIGINAL_FORMAT = retrieval._format_anchor_context
ORIGINAL_RERANK = retrieval.rerank_matches
ORIGINAL_MERGE = retrieval._merge_semantic_matches


class IndexedFormatter:
    """Retain identical ordering/text while selecting anchors by index range."""

    def __init__(self, index):
        self.anchors = sorted(index["anchor_map"].items(), key=lambda item: item[1].get("body_child_index") or 0)
        self.positions = [r.get("body_child_index") or 0 for _, r in self.anchors]

    def __call__(self, index, nodes):
        # Reuse the production renderer on only the selected anchors. Sorting
        # one small subset remains to preserve exact production tie behaviour.
        selected = {}
        for node in nodes:
            start, end = node.get("start_index"), node.get("end_index")
            if start is not None and end is not None:
                lo, hi = bisect_left(self.positions, start), bisect_right(self.positions, end)
                selected.update(self.anchors[lo:hi])
        return ORIGINAL_FORMAT({**index, "anchor_map": selected}, nodes)


@contextmanager
def treatment(name, index):
    """Apply one treatment without changing the source or service config."""
    with ExitStack() as stack:
        if name in {"E05", "combined"}:
            by_id = {n["node_id"]: n for n in index["nodes"]}
            stack.enter_context(patch.object(content, "get_node", lambda idx, key: by_id[key]))
        if name in {"E06", "combined"}:
            stack.enter_context(patch.object(retrieval, "_format_anchor_context", IndexedFormatter(index)))
        if name == "E04":
            stack.enter_context(patch.object(content, "_prefer_specific_nodes", lambda idx, matches: matches))
        if name == "E03":
            stack.enter_context(
                patch.object(
                    retrieval, "_merge_semantic_matches", lambda s, v, **kw: ORIGINAL_MERGE(s, v, max_unique_nodes=None)
                )
            )
        if name == "E07":
            # Only the single-query, cache_dir=None reranker is bypassed.
            # llm_query can reuse cache entries within a multi-query call.
            stack.enter_context(patch.object(RERANK_MODULE, "cache_key", lambda *parts: "uncached"))
            stack.enter_context(patch.object(RERANK_MODULE, "text_hash", lambda text: "uncached"))
            stack.enter_context(patch.object(RERANK_MODULE, "JsonlCache", NoCache))
        if name == "E08":
            stack.enter_context(patch.object(RERANK_MODULE, "BoundedSemaphore", lambda n: nullcontext()))
        yield


class NoCache:
    def __init__(self, path):
        if path is not None:
            raise ValueError("E07 only supports online cache_dir=None")

    def get(self, key):
        return None

    def set(self, key, value):
        pass


def skip_single_rerank(index, query, candidates, *args, **kwargs):
    if len(candidates) == 1:
        return candidates
    return ORIGINAL_RERANK(index, query, candidates, *args, **kwargs)


def field_removal(field):
    original = RERANK_MODULE._fit_candidates

    def fit(*args, **kwargs):
        return [{k: v for k, v in row.items() if k != field} for row in original(*args, **kwargs)]

    return patch.object(RERANK_MODULE, "_fit_candidates", fit)
