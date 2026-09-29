from __future__ import annotations

from celery import Celery

from config import QUEUE_BROKER_URL, QUEUE_WORKER_DISABLE_PREFETCH


celery_app = Celery(
    "contract_agent",
    broker=QUEUE_BROKER_URL,
    include=["task_queue.review_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_track_started=True,
    worker_disable_prefetch=QUEUE_WORKER_DISABLE_PREFETCH,
    worker_prefetch_multiplier=1,
)
