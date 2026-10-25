from __future__ import annotations

import asyncio
import time

from celery import Task
from config import QUEUE_TASK_TIME_LIMIT_SECONDS
from endpoints.runtime.model_budget import task_execution
from endpoints.runtime.model_limits import close_model_clients

from endpoints.review.job_worker import run_async_review_job
from endpoints.review.task_store import read_task
from task_queue.celery_app import celery_app


class ReviewTask(Task):
    Request = "task_queue.review_request:ReviewRequest"


@celery_app.task(name="review.run_job", base=ReviewTask)
def run_review_job_task(client_dir: str, task_id: str) -> dict[str, str]:
    async def execute() -> None:
        try:
            await run_async_review_job(client_dir, task_id)
        finally:
            await close_model_clients()

    deadline = (time.time() + QUEUE_TASK_TIME_LIMIT_SECONDS
                if QUEUE_TASK_TIME_LIMIT_SECONDS is not None else None)
    with task_execution(deadline, task_id=task_id):
        asyncio.run(execute())
    task = read_task(client_dir, task_id)
    return {
        "client_dir": client_dir,
        "task_id": task_id,
        "status": str(task["status"]) if task else "missing",
    }
