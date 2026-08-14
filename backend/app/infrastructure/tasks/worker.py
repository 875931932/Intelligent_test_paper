"""Generic Celery worker bridge for durable task runs.

Business workflows register handlers by ``task_type`` in later tasks.  This
module owns only lease acquisition, handler dispatch, and durable completion;
Celery remains a transport and never becomes the source of task state.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import task_runs
from app.db.session import get_session_factory
from app.infrastructure.tasks.models import (
    claim_task,
    complete_task,
    fail_task,
    refresh_lease,
    update_task_progress,
    wait_for_external,
)


@dataclass(frozen=True)
class TaskContext:
    session: Session
    course_id: str
    task_id: str
    worker_id: str
    payload: dict

    def heartbeat(self, *, lease_seconds: int = 60) -> bool:
        return refresh_lease(
            self.session,
            course_id=self.course_id,
            task_id=self.task_id,
            worker_id=self.worker_id,
            lease_seconds=lease_seconds,
        )

    def report_progress(self, *, stage: str, progress: int) -> bool:
        return update_task_progress(
            self.session,
            course_id=self.course_id,
            task_id=self.task_id,
            worker_id=self.worker_id,
            stage=stage,
            progress=progress,
        )

    def pause_until(self, next_poll_at: datetime) -> bool:
        return wait_for_external(
            self.session,
            course_id=self.course_id,
            task_id=self.task_id,
            worker_id=self.worker_id,
            next_poll_at=next_poll_at,
        )


TaskHandler = Callable[[TaskContext], dict]
_HANDLERS: dict[str, TaskHandler] = {}


def register_task_handler(task_type: str, handler: TaskHandler) -> None:
    if not task_type.strip():
        raise ValueError("task_type is required")
    _HANDLERS[task_type] = handler


def execute_task(task_id: str, *, worker_id: str | None = None) -> bool:
    """Claim and execute one durable task, returning whether it was handled."""

    worker_id = worker_id or f"{os.getpid()}:{uuid4().hex}"
    session = get_session_factory()()
    try:
        row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).mappings().one_or_none()
        if row is None:
            return False
        course_id = row["course_id"]
        if not claim_task(session, course_id=course_id, task_id=task_id, worker_id=worker_id):
            session.rollback()
            return False
        handler = _HANDLERS.get(row["task_type"])
        if handler is None:
            fail_task(
                session,
                course_id=course_id,
                task_id=task_id,
                worker_id=worker_id,
                error_code="handler_not_registered",
                error_message="no worker handler is registered for this task type",
            )
            session.commit()
            return False
        try:
            context = TaskContext(
                session=session,
                course_id=course_id,
                task_id=task_id,
                worker_id=worker_id,
                payload=dict(row["payload"] or {}),
            )
            result = handler(context)
        except Exception as exc:
            fail_task(
                session,
                course_id=course_id,
                task_id=task_id,
                worker_id=worker_id,
                error_code="handler_error",
                error_message="task handler failed",
            )
            session.commit()
            return False
        completed = complete_task(
            session,
            course_id=course_id,
            task_id=task_id,
            worker_id=worker_id,
            result=result,
        )
        if completed:
            session.commit()
            return True
        status = session.execute(select(task_runs.c.status).where(task_runs.c.id == task_id)).scalar_one()
        session.commit()
        return status == "waiting_external"
    finally:
        session.close()
