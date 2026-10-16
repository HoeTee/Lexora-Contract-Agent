from __future__ import annotations

from uuid import uuid4

from endpoints.review.task_store import fail_active_task, mark_queued, write_task_log_event
from task_queue.review_tasks import run_review_job_task


def enqueue_review_job(client_dir: str, task_id: str) -> str:
    queue_id = uuid4().hex
    if mark_queued(client_dir, task_id, queue_id) is None:
        raise ValueError("Only a pending review job can be submitted to the queue.")
    try:
        # Persist identity before publishing: a fast worker may finish before publish returns.
        run_review_job_task.apply_async(args=(client_dir, task_id), task_id=queue_id)
    except Exception as exc:
        fail_active_task(client_dir, task_id, code="QUEUE_SUBMIT_FAILED",
                         detail="Review job queue submission failed.", queue_id=queue_id)
        write_task_log_event(client_dir, task_id, "review_queue_submit_failed", error=repr(exc))
        raise
    write_task_log_event(client_dir, task_id, "review_queued", queue_task_id=queue_id)
    return queue_id
