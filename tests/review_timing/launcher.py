"""Launch pinned production API/MCP modules with identical local infrastructure."""

import argparse
from contextlib import redirect_stdout
import importlib
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[2]


def launch(variant, api=False):
    source = ROOT / "tests/review_timing/sources" / variant
    sys.path.insert(0, str(source))
    with redirect_stdout(sys.stderr):
        config = importlib.import_module("config")
    for settings in config.MODEL_SEMAPHORE_CONFIGS.values():
        settings["redis_url"] = "redis://127.0.0.1:6380/2"
    config.DATA_DIR = str(ROOT / "data/api/review_timing" / variant)
    config.QUEUE_ENABLED = False
    config.MCP_SERVER_TARGET = f"module:tests.review_timing.{variant}_mcp"
    if api:
        application = importlib.import_module("app")
        importlib.import_module("uvicorn").run(
            application.app, host="127.0.0.1", port=5111 if variant == "baseline" else 5112,
            log_level="warning",
        )
        return
    retrieval = importlib.import_module("tools.docxindex.retriever")
    events = importlib.import_module("loggers.model_event_context")
    for name in ("build_document_index", "build_vector_index"):
        original = getattr(retrieval, name)

        def timed(*args, _original=original, _name=name, **kwargs):
            started = time.monotonic()
            try:
                return _original(*args, **kwargs)
            finally:
                events.append_model_event(
                    "version_timing", variant=variant, operation=_name,
                    seconds=round(time.monotonic() - started, 6), source=str(source),
                )

        setattr(retrieval, name, timed)
    importlib.import_module("mcp_service.server.server").mcp.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", choices=("baseline", "latest"))
    parser.add_argument("--api", action="store_true")
    args = parser.parse_args()
    launch(args.variant, args.api)
