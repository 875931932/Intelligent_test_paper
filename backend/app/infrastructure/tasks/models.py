"""Course-scoped durable task state transitions.

Celery is deliberately not a source of task state: these rows are the business
truth and every worker transition is a conditional database update.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.schema import outbox_events, task_runs

DISPATCH_EVENT_TYPE = "task.dispatch"


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _insert_for(session: Session, table):
    return postgresql_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)


def create_task_run(
    session: Session,
    *,
    course_id: str,
    task_type: str,
    idempotency_key: str,
    input_version: str,
    payload: dict,
    max_attempts: int = 3,
    task_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Create task and its initial event in the caller's transaction.

    The caller owns commit/rollback; therefore a rollback can never leave one
    half of the task/outbox pair committed.
    """

    existing = session.execute(
        select(task_runs.c.id).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    current_time = _now(now)
    new_id = task_id or uuid4().hex
    inserted = session.execute(
        _insert_for(session, task_runs)
        .values(
            id=new_id,
            course_id=course_id,
            task_type=task_type,
            input_version=input_version,
            idempotency_key=idempotency_key,
            status="queued",
            stage="queued",
            progress=0,
            attempt=0,
            max_attempts=max_attempts,
            payload=payload,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_nothing(index_elements=["course_id", "idempotency_key"])
    )
    if inserted.rowcount == 0:
        return session.execute(
            select(task_runs.c.id).where(
                task_runs.c.course_id == course_id,
                task_runs.c.idempotency_key == idempotency_key,
            )
        ).scalar_one()

    session.execute(
        _insert_for(session, outbox_events)
        .values(
            id=uuid4().hex,
            course_id=course_id,
            task_run_id=new_id,
            event_type=DISPATCH_EVENT_TYPE,
            status="pending",
            payload={"task_id": new_id},
            attempts=0,
            available_at=current_time,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_nothing(index_elements=["course_id", "task_run_id", "event_type"], index_where=outbox_events.c.status.in_(("pending", "claimed")))
    )
    return new_id


def claim_task(session: Session, *, course_id: str, task_id: str, worker_id: str, now: datetime | None = None, lease_seconds: int = 60) -> bool:
    current_time = _now(now)
    result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.attempt < task_runs.c.max_attempts,
            ((task_runs.c.status == "queued") | ((task_runs.c.status == "running") & (task_runs.c.lease_expires_at <= current_time))),
        )
        .values(
            status="running",
            stage="running",
            lease_owner=worker_id,
            lease_expires_at=current_time + timedelta(seconds=lease_seconds),
            attempt=task_runs.c.attempt + 1,
            updated_at=current_time,
        )
    )
    return result.rowcount == 1


def refresh_lease(session: Session, *, course_id: str, task_id: str, worker_id: str, now: datetime | None = None, lease_seconds: int = 60) -> bool:
    current_time = _now(now)
    result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(lease_expires_at=current_time + timedelta(seconds=lease_seconds), updated_at=current_time)
    )
    return result.rowcount == 1


def cancel_task(session: Session, *, course_id: str, task_id: str, now: datetime | None = None) -> bool:
    current_time = _now(now)
    result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status.in_(("queued", "running", "waiting_external")),
        )
        .values(status="cancelled", stage="cancelled", lease_owner=None, lease_expires_at=None, updated_at=current_time, completed_at=current_time)
    )
    return result.rowcount == 1


def complete_task(session: Session, *, course_id: str, task_id: str, worker_id: str, result: dict, now: datetime | None = None) -> bool:
    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(status="succeeded", stage="completed", progress=100, result=result, lease_owner=None, lease_expires_at=None, updated_at=current_time, completed_at=current_time)
    )
    return update_result.rowcount == 1


def fail_task(session: Session, *, course_id: str, task_id: str, worker_id: str, error_code: str, error_message: str, now: datetime | None = None) -> bool:
    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(status="failed", stage="failed", error_code=error_code, error_message=error_message, lease_owner=None, lease_expires_at=None, updated_at=current_time, completed_at=current_time)
    )
    return update_result.rowcount == 1


def wait_for_external(
    session: Session,
    *,
    course_id: str,
    task_id: str,
    worker_id: str,
    next_poll_at: datetime,
    now: datetime | None = None,
) -> bool:
    """Persist a provider polling pause only while the worker still owns its lease."""

    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(status="waiting_external", stage="waiting_external", next_poll_at=next_poll_at, lease_owner=None, lease_expires_at=None, updated_at=current_time)
    )
    return update_result.rowcount == 1


def update_task_progress(
    session: Session,
    *,
    course_id: str,
    task_id: str,
    worker_id: str,
    stage: str,
    progress: int,
    now: datetime | None = None,
) -> bool:
    """Update visible worker progress without allowing a stale lease to overwrite it."""

    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(stage=stage, progress=progress, updated_at=current_time)
    )
    return update_result.rowcount == 1
