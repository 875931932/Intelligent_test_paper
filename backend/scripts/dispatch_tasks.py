"""Dispatch pending outbox events and recover stale durable tasks.

Run this command periodically from cron or a systemd timer. Celery transports
messages only; PostgreSQL remains authoritative for task state, so this command
does not need Redis access to decide which work requires recovery.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.schema import outbox_events, task_runs
from app.db.session import get_session_factory
from app.infrastructure.tasks.outbox import dispatch_pending_events
from app.infrastructure.tasks.recovery import recover_tasks


def _course_ids(session) -> list[str]:
    """Return every course that may have recoverable work or pending delivery."""

    statement = select(task_runs.c.course_id).union(
        select(outbox_events.c.course_id).where(
            outbox_events.c.status.in_(("pending", "claimed"))
        )
    )
    return list(session.execute(statement).scalars())


def main() -> int:
    # Import at execution time so static inspection does not require the Celery
    # runtime; production installs it from backend/pyproject.toml.
    from app.infrastructure.tasks.celery_app import CeleryPublisher

    session = get_session_factory()()
    try:
        recovered = 0
        published = 0
        publisher = CeleryPublisher()
        for course_id in _course_ids(session):
            recovered += recover_tasks(session, course_id=course_id)
            published += dispatch_pending_events(
                session,
                publisher,
                course_id=course_id,
            )
        session.commit()
        print({"recovered": recovered, "published": published})
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
