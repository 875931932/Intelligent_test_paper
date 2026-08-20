"""Celery transport configuration; task state remains in PostgreSQL."""

from __future__ import annotations

from celery import Celery

from app.config import settings
from app.infrastructure.tasks.models import DISPATCH_EVENT_TYPE


celery_app = Celery("exam_system", broker=settings.redis_url)
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


@celery_app.task(name=DISPATCH_EVENT_TYPE, bind=True, ignore_result=True)
def dispatch_task(self, payload: dict) -> None:
    """Celery entrypoint for an outbox event."""

    from app.infrastructure.tasks.worker import execute_task

    execute_task(payload["task_id"], worker_id=f"celery:{self.request.id or 'unknown'}")
