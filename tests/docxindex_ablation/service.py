"""Run a baseline API, matching Celery worker, or marked MCP process.

API and worker must run on Linux/WSL with the same checkout, data directory and
config.yaml. Redis DB 0 and DB 2 remain mandatory; addresses and limits are not
rewritten. The separate task queue prevents production workers from taking test
tasks. Launch --api and --worker separately. --help does not load application
settings or call models. The retired combined safe treatment is not supported.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib
import os
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
BASELINE_QUEUE = "contract_agent_ablation_baseline"


def configure(variant):
    if variant != "baseline":
        raise ValueError("The combined safe treatment is retired; use a newly validated single-factor experiment.")
    # Import only after argument parsing; MCP stdout is reserved for its protocol.
    with redirect_stdout(sys.stderr):
        config = importlib.import_module("config")
    config.DATA_DIR = str(ROOT / "data" / "api" / "docxindex_ablation_baseline")
    config.MCP_SERVER_TARGET = "module:tests.docxindex_ablation.baseline_server"
    celery_app = importlib.import_module("task_queue.celery_app").celery_app
    celery_app.conf.task_default_queue = BASELINE_QUEUE
    os.environ["DOCXINDEX_ABLATION_VARIANT"] = variant
    return config


def serve_mcp(variant):
    configure(variant)
    # Load after configure so all consumers see the same task data root.
    with redirect_stdout(sys.stderr):
        retrieval = importlib.import_module("tools.docxindex.retriever")
        events = importlib.import_module("loggers.model_event_context")
    original = retrieval.DocxIndexRetriever._build_index_sync

    def baseline_build(engine, path):
        events.append_model_event("docxindex_ablation", variant="baseline", operation="installed")
        return original(engine, path)

    print("[Ablation] baseline; queued workflow; shared model limits remain active", file=sys.stderr, flush=True)
    with patch.object(retrieval.DocxIndexRetriever, "_build_index_sync", baseline_build):
        importlib.import_module("mcp_service.server.server").mcp.run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--api", action="store_true", help="Serve the actual API; requires a matching --worker process.")
    modes.add_argument(
        "--worker", action="store_true", help="Consume only baseline experiment tasks with prefork concurrency=1."
    )
    parser.add_argument("--variant", choices=["baseline"], default="baseline")
    parser.add_argument("--port", type=int, default=5101)
    args = parser.parse_args()
    if args.worker and os.name == "nt":
        parser.error("The hard task limit requires prefork; start API/worker together on Linux or WSL.")
    if args.api:
        configure(args.variant)
        application = importlib.import_module("app").app
        importlib.import_module("uvicorn").run(application, host="127.0.0.1", port=args.port, log_level="warning")
    elif args.worker:
        configure(args.variant)
        celery_app = importlib.import_module("task_queue.celery_app").celery_app
        celery_app.worker_main(
            [
                "worker",
                "--loglevel=info",
                "--pool=prefork",
                "--concurrency=1",
                "--queues",
                BASELINE_QUEUE,
                "--hostname",
                "ablation-baseline@%h",
            ]
        )
    else:
        serve_mcp(args.variant)


if __name__ == "__main__":
    main()
