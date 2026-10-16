from __future__ import annotations

from celery import Celery

from config import QUEUE_BROKER_URL, QUEUE_WORKER_DISABLE_PREFETCH, QUEUE_TASK_TIME_LIMIT_SECONDS


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
    worker_pool="prefork",
    task_time_limit=QUEUE_TASK_TIME_LIMIT_SECONDS,
    task_soft_time_limit=None,
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    broker_transport_options={"visibility_timeout": 3600},
)
