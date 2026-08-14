"""Transactional outbox dispatching with an intentionally narrow publisher seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.schema import outbox_events
from app.infrastructure.tasks.models import DISPATCH_EVENT_TYPE


class Publisher(Protocol):
    def publish(self, event_type: str, payload: dict) -> None: ...


class FakePublisher:
    """Small in-memory publisher for tests and local adapter checks."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.published: list[tuple[str, dict]] = []

    def publish(self, event_type: str, payload: dict) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("publisher unavailable")
        self.published.append((event_type, payload))


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _insert_for(session: Session):
    return postgresql_insert(outbox_events) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(outbox_events)


def ensure_dispatch_event(session: Session, *, course_id: str, task_id: str, now: datetime | None = None) -> bool:
    """Create one active dispatch event, leaving published history intact."""

    current_time = _now(now)
    statement = (
        _insert_for(session)
        .values(
            id=uuid4().hex,
            course_id=course_id,
            task_run_id=task_id,
            event_type=DISPATCH_EVENT_TYPE,
            status="pending",
            payload={"task_id": task_id},
            attempts=0,
            available_at=current_time,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_nothing(
            index_elements=["course_id", "task_run_id", "event_type"],
            index_where=outbox_events.c.status.in_(("pending", "claimed")),
        )
    )
    return session.execute(statement).rowcount == 1


def dispatch_pending_events(
    session: Session,
    publisher: Publisher,
    *,
    course_id: str,
    now: datetime | None = None,
    limit: int = 100,
    claim_seconds: int = 60,
) -> int:
    """Publish eligible events, marking publication only after publisher success."""

    current_time = _now(now)
    eligible = [
        outbox_events.c.available_at <= current_time,
        or_(
            outbox_events.c.status == "pending",
            (outbox_events.c.status == "claimed") & (outbox_events.c.claim_expires_at <= current_time),
        ),
    ]
    eligible.append(outbox_events.c.course_id == course_id)
    event_ids = session.execute(select(outbox_events.c.id).where(*eligible).order_by(outbox_events.c.created_at).limit(limit)).scalars().all()
    published = 0
    for event_id in event_ids:
        owner = uuid4().hex
        claimed = session.execute(
            update(outbox_events)
            .where(outbox_events.c.id == event_id, *eligible)
            .values(status="claimed", claim_owner=owner, claim_expires_at=current_time + timedelta(seconds=claim_seconds), updated_at=current_time)
        )
        if claimed.rowcount != 1:
            continue
        event = session.execute(
            select(outbox_events.c.event_type, outbox_events.c.payload).where(outbox_events.c.id == event_id, outbox_events.c.claim_owner == owner)
        ).one()._mapping
        try:
            publisher.publish(event["event_type"], event["payload"])
        except Exception as exc:
            session.execute(
                update(outbox_events)
                .where(outbox_events.c.id == event_id, outbox_events.c.status == "claimed", outbox_events.c.claim_owner == owner)
                .values(
                    status="pending",
                    attempts=outbox_events.c.attempts + 1,
                    available_at=current_time,
                    claim_owner=None,
                    claim_expires_at=None,
                    error=str(exc),
                    updated_at=current_time,
                )
            )
            continue
        marked = session.execute(
            update(outbox_events)
            .where(outbox_events.c.id == event_id, outbox_events.c.status == "claimed", outbox_events.c.claim_owner == owner)
            .values(status="published", published_at=current_time, claim_owner=None, claim_expires_at=None, error=None, updated_at=current_time)
        )
        published += int(marked.rowcount == 1)
    return published
