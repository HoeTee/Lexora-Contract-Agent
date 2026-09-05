"""Isolated launcher for the actual app and actual MCP server.

No review implementation is copied. Local API/MCP processes share Redis limits
with Docker via its published port; production config.yaml is never rewritten.
"""

import argparse
import importlib
import os
from pathlib import Path
import sys
from contextlib import ExitStack, nullcontext, redirect_stdout
from unittest.mock import patch

from tests.docxindex_ablation.variants import (
    IndexedFormatter,
    RERANK_MODULE,
    NoCache,
    skip_single_rerank,
)
from tools.docxindex import retriever as retrieval
from tools.docxindex.retrieval import content
from loggers.model_event_context import append_model_event


ROOT = Path(__file__).resolve().parents[2]


def configure(variant):
    # config prints diagnostics; stdout is reserved for the MCP protocol.
    with redirect_stdout(sys.stderr):
        config = importlib.import_module("config")
    for value in config.MODEL_SEMAPHORE_CONFIGS.values():
        value["redis_url"] = "redis://127.0.0.1:6380/0"
    config.QUEUE_ENABLED = False
    config.DATA_DIR = str(ROOT / "data" / "api" / f"docxindex_ablation_{variant}")
    config.MCP_SERVER_TARGET = "module:tests.docxindex_ablation.service"
    os.environ["DOCXINDEX_ABLATION_VARIANT"] = variant
    return config


def serve_mcp(variant):
    configure(variant)
    print(
        f"[Ablation] variant={variant}; production workflow; isolated intervention process", file=sys.stderr, flush=True
    )
    if variant == "baseline":
        importlib.import_module("mcp_service.server.server").mcp.run()
        return
    # One permanent patch set per MCP process. No per-query global patch races.
    original_build = retrieval.DocxIndexRetriever._build_index_sync
    original_get = content.get_node
    original_format = retrieval._format_anchor_context
    indices = {}

    def build(engine, path):
        result = original_build(engine, path)
        index = engine._index
        indices[id(index)] = (index, {n["node_id"]: n for n in index["nodes"]}, IndexedFormatter(index))
        return result

    def get_node(index, key):
        saved = indices.get(id(index))
        return saved[1][key] if saved else original_get(index, key)

    def format_context(index, nodes):
        saved = indices.get(id(index))
        return saved[2](index, nodes) if saved else original_format(index, nodes)

    def rerank(index, query, candidates, *args, **kwargs):
        if len(candidates) == 1:
            append_model_event(
                "docxindex_ablation",
                experiment="E01",
                operation="skip_single_rerank",
                node_id=candidates[0].get("node_id"),
            )
        return skip_single_rerank(index, query, candidates, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(retrieval.DocxIndexRetriever, "_build_index_sync", build))
        stack.enter_context(patch.object(content, "get_node", get_node))
        stack.enter_context(patch.object(retrieval, "_format_anchor_context", format_context))
        stack.enter_context(patch.object(retrieval, "rerank_matches", rerank))
        # This server only calls online rerank(cache_dir=None). NoCache rejects
        # accidental disk-cache use instead of silently corrupting its keys.
        stack.enter_context(patch.object(RERANK_MODULE, "JsonlCache", NoCache))
        stack.enter_context(patch.object(RERANK_MODULE, "cache_key", lambda *parts: "uncached"))
        stack.enter_context(patch.object(RERANK_MODULE, "text_hash", lambda text: "uncached"))
        stack.enter_context(patch.object(RERANK_MODULE, "BoundedSemaphore", lambda n: nullcontext()))
        # Do not include E02, E03, E04 or prompt field removals in this candidate.
        importlib.import_module("mcp_service.server.server").mcp.run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", action="store_true")
    parser.add_argument(
        "--variant", choices=["baseline", "safe"], default=os.environ.get("DOCXINDEX_ABLATION_VARIANT", "baseline")
    )
    parser.add_argument("--port", type=int, default=5101)
    args = parser.parse_args()
    if args.api:
        configure(args.variant)
        application = importlib.import_module("app").app
        importlib.import_module("uvicorn").run(application, host="127.0.0.1", port=args.port, log_level="warning")
    else:
        serve_mcp(args.variant)


if __name__ == "__main__":
    main()
