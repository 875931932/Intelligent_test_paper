"""Celery transport configuration; task state remains in PostgreSQL."""

from __future__ import annotations

import os

from celery import Celery


celery_app = Celery("exam_system", broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


class CeleryPublisher:
    """Transport adapter satisfying the outbox Publisher protocol."""

    def __init__(self, app: Celery = celery_app) -> None:
        self._app = app

    def publish(self, event_type: str, payload: dict) -> None:
        self._app.send_task(event_type, args=[payload])
