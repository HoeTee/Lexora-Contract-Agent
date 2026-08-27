from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
import socket
import traceback
import uuid
from pathlib import Path

from config import MCP_SERVER_TARGET
from agents.base_agent import Settings
from endpoints.review.task_store import (
    cleanup_runtime_input,
    is_cancel_requested,
    mark_cancelled,
    mark_queued,
    mark_failed,
    mark_running,
    mark_succeeded,
    mark_worker_heartbeat,
    mark_worker_started,
    read_task,
    read_task_model_config,
    task_api_events_path,
    task_conversation_log_dir,
    task_mcp_log_file,
    task_review_outputs_path,
    task_trace_path,
    write_task_log_event,
)
from endpoints.runtime.errors import ModelCallError
from endpoints.runtime.review_runtime import review_semaphore
from loggers.agent_logger import reset_conversation_log_dir, set_conversation_log_dir
from endpoints.review.review_state import ReviewStateStore
from workflow.workflow import ContractReviewWorkflow


HEARTBEAT_INTERVAL_SECONDS = 10
logger = logging.getLogger(__name__)


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def heartbeat_worker(client_dir: str, task_id: str, worker_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            mark_worker_heartbeat(client_dir, task_id, worker_id)
        except Exception as exc:
            logger.exception("Worker heartbeat update failed for task %s", task_id)
            try:
                write_task_log_event(
                    client_dir,
                    task_id,
                    "worker_heartbeat_failed",
                    worker_id=worker_id,
                    error=repr(exc),
                )
            except Exception:
                logger.exception("Worker heartbeat failure event could not be written for task %s", task_id)


async def run_async_review_job(client_dir: str, task_id: str) -> None:
    task = read_task(client_dir, task_id)
    if task is None:
        return

    worker_id = new_worker_id()
    heartbeat_task = None
    try:
        mark_worker_started(client_dir, task_id, worker_id)
        heartbeat_task = asyncio.create_task(heartbeat_worker(client_dir, task_id, worker_id))
        if is_cancel_requested(client_dir, task_id):
            mark_cancelled(client_dir, task_id)
            return

        model_config = read_task_model_config(client_dir, task_id)
        agent_settings = None
        mcp_env = None
        if model_config:
            agent_settings = Settings(
                api_key=model_config["llm_api_key"],
                model=model_config["llm_model_name"],
            )
            mcp_env = {
                "LLM_API_KEY": model_config["llm_api_key"],
                "LLM_MODEL_NAME": model_config["llm_model_name"],
                "EMBED_API_KEY": model_config["embedding_api_key"],
                "EMBEDDING_MODEL_NAME": model_config["embedding_model_name"],
                "RERANK_API_KEY": model_config["reranker_api_key"],
                "RERANKER_MODEL_NAME": model_config["reranker_model_name"],
            }

        workflow = ContractReviewWorkflow(
            server_target=MCP_SERVER_TARGET,
            conversation_log_dir=(
                str(task_conversation_log_dir(client_dir, task_id)) if task_conversation_log_dir(client_dir, task_id) else None
            ),
            mcp_log_file=str(task_mcp_log_file(client_dir, task_id)) if task_mcp_log_file(client_dir, task_id) else None,
            api_events_path=(
                str(task_api_events_path(client_dir, task_id)) if task_api_events_path(client_dir, task_id) else None
            ),
            trace_path=str(task_trace_path(client_dir, task_id)) if task_trace_path(client_dir, task_id) else None,
            review_outputs_path=(
                str(task_review_outputs_path(client_dir, task_id)) if task_review_outputs_path(client_dir, task_id) else None
            ),
            settings=agent_settings,
            mcp_env=mcp_env,
            state_store=ReviewStateStore(client_dir, task_id),
        )

        token = set_conversation_log_dir(task_conversation_log_dir(client_dir, task_id))
        try:
            if review_semaphore is None:
                if is_cancel_requested(client_dir, task_id):
                    mark_cancelled(client_dir, task_id)
                    write_task_log_event(client_dir, task_id, "review_cancelled")
                    return
                mark_running(client_dir, task_id)
                write_task_log_event(client_dir, task_id, "review_started")
                result = await workflow.run(
                    contract_path=task["input"]["contract_path"],
                    criteria_path=task["input"]["criteria_path"],
                    output_path=task["output"]["result_path"],
                )
            else:
                if review_semaphore.locked():
                    mark_queued(client_dir, task_id)
                async with review_semaphore:
                    if is_cancel_requested(client_dir, task_id):
                        mark_cancelled(client_dir, task_id)
                        write_task_log_event(client_dir, task_id, "review_cancelled")
                        return
                    mark_running(client_dir, task_id)
                    write_task_log_event(client_dir, task_id, "review_started")
                    result = await workflow.run(
                        contract_path=task["input"]["contract_path"],
                        criteria_path=task["input"]["criteria_path"],
                        output_path=task["output"]["result_path"],
                    )
        finally:
            reset_conversation_log_dir(token)

        if is_cancel_requested(client_dir, task_id):
            mark_cancelled(client_dir, task_id)
            write_task_log_event(client_dir, task_id, "review_cancelled")
            return

        output_path = Path(result["report_docx"])
        if not output_path.exists():
            raise RuntimeError("Output DOCX file was not found.")

        mark_succeeded(client_dir, task_id)
        write_task_log_event(client_dir, task_id, "review_completed", result_file=str(output_path))
    except Exception as exc:
        if isinstance(exc, ModelCallError):
            write_task_log_event(
                client_dir,
                task_id,
                exc.event_type,
                component=exc.component,
                http_status=exc.http_status,
                error=str(exc),
            )
            mark_failed(
                client_dir,
                task_id,
                code=exc.event_type,
                message=exc.user_message,
                component=exc.component,
                error=exc.as_task_error(),
            )
        else:
            mark_failed(client_dir, task_id, code="REVIEW_FAILED", message="Review failed. Check task logs.")
        write_task_log_event(
            client_dir,
            task_id,
            "review_failed",
            error=repr(exc),
            error_type=type(exc).__name__,
            error_message=str(exc),
            traceback=traceback.format_exc(),
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        cleanup_runtime_input(client_dir, task_id)
