"""Celery transport configuration; task state remains in PostgreSQL."""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging

from app.config import settings
from app.infrastructure.tasks.models import DISPATCH_EVENT_TYPE

# worker 是独立进程，不继承 main.py 的 basicConfig。历史上 worker 侧零日志
# 配置，导致 model.call / generation.runner 等 INFO 全部被丢弃，线上排查时
# "模型明明调用成功、日志里却什么都看不到"。这里显式装配 INFO 级根日志。
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


@celery_setup_logging.connect
def _configure_worker_logging(**_kwargs) -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)


logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)


celery_app = Celery("exam_system", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # 不让 Celery 重置根 logger，保留上面的 INFO 配置。
    worker_hijack_root_logger=False,
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

    logging.getLogger("celery.dispatch").info(
        "dispatch_task 收到事件 task_id=%s worker=%s",
        payload.get("task_id"), self.request.id or "unknown",
    )
    execute_task(payload["task_id"], worker_id=f"celery:{self.request.id or 'unknown'}")
