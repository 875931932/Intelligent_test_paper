"""Recovery sweeps for durable task state; Redis/Celery is never consulted."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.schema import task_runs
from app.infrastructure.tasks.outbox import ensure_dispatch_event


def recover_tasks(session: Session, *, course_id: str, now: datetime | None = None) -> int:
    """Requeue expired/due work and ensure each task has one active event."""

    current_time = now or datetime.now(UTC)
    candidates = session.execute(
        select(task_runs.c.id).where(
            task_runs.c.course_id == course_id,
            or_(
                (task_runs.c.status == "queued"),
                (task_runs.c.status == "running") & (task_runs.c.lease_expires_at <= current_time),
                (task_runs.c.status == "waiting_external") & (task_runs.c.next_poll_at <= current_time),
            ),
        )
    ).scalars().all()
    recovered = 0
    for task_id in candidates:
        transition = session.execute(
            update(task_runs)
            .where(
                task_runs.c.course_id == course_id,
                task_runs.c.id == task_id,
                or_(
                    task_runs.c.status == "queued",
                    (task_runs.c.status == "running") & (task_runs.c.lease_expires_at <= current_time),
                    (task_runs.c.status == "waiting_external") & (task_runs.c.next_poll_at <= current_time),
                ),
            )
            .values(status="queued", stage="queued", lease_owner=None, lease_expires_at=None, next_poll_at=None, updated_at=current_time)
        )
        if transition.rowcount == 1 and ensure_dispatch_event(session, course_id=course_id, task_id=task_id, now=current_time):
            recovered += 1
    return recovered
