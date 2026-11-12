"""知识 outbox 派发与过期认领回收测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.init_db import _migrate_knowledge_outbox_updated_at

from app.db.schema import Base, knowledge_outbox_events
from app.infrastructure.tasks import celery_app as celery_module
from app.infrastructure.tasks.knowledge_tasks import dispatch_pending_knowledge_events


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def publish(self, event_type: str, payload: dict) -> None:
        self.sent.append(event_type)


class _FailingPublisher:
    def publish(self, event_type: str, payload: dict) -> None:
        raise RuntimeError("broker down")


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _insert_event(session, *, event_id: str, **overrides):
    now = datetime.now(timezone.utc)
    values = {
        "id": event_id,
        "school_id": "sch-1",
        "job_id": "job-" + event_id,
        "event_type": "knowledge.job",
        "state": "pending",
        "attempts": 0,
        "available_at": now,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    session.execute(knowledge_outbox_events.insert().values(**values))
    session.commit()


def test_publish_marks_published_and_writes_updated_at(monkeypatch):
    publisher = _Publisher()
    monkeypatch.setattr(celery_module, "CeleryPublisher", lambda: publisher)
    session = _session()
    _insert_event(session, event_id="e1")

    assert dispatch_pending_knowledge_events(session) == 1
    row = session.execute(
        select(knowledge_outbox_events).where(knowledge_outbox_events.c.id == "e1")
    ).mappings().one()
    assert row.state == "published"
    assert row.updated_at is not None
    assert publisher.sent == ["knowledge.job"]


def test_expired_claim_is_recovered(monkeypatch):
    publisher = _Publisher()
    monkeypatch.setattr(celery_module, "CeleryPublisher", lambda: publisher)
    session = _session()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    _insert_event(
        session,
        event_id="e2",
        state="claimed",
        claim_owner="crashed",
        claim_until=past,
    )

    assert dispatch_pending_knowledge_events(session) == 1
    row = session.execute(
        select(knowledge_outbox_events).where(knowledge_outbox_events.c.id == "e2")
    ).mappings().one()
    assert row.state == "published"
    assert row.attempts == 1


def test_active_claim_is_not_stolen(monkeypatch):
    publisher = _Publisher()
    monkeypatch.setattr(celery_module, "CeleryPublisher", lambda: publisher)
    session = _session()
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    _insert_event(
        session,
        event_id="e3",
        state="claimed",
        claim_owner="alive",
        claim_until=future,
    )

    assert dispatch_pending_knowledge_events(session) == 0
    assert publisher.sent == []


def test_publish_failure_returns_to_pending(monkeypatch):
    monkeypatch.setattr(celery_module, "CeleryPublisher", _FailingPublisher)
    session = _session()
    _insert_event(session, event_id="e4")

    assert dispatch_pending_knowledge_events(session) == 0
    row = session.execute(
        select(knowledge_outbox_events).where(knowledge_outbox_events.c.id == "e4")
    ).mappings().one()
    assert row.state == "pending"
    assert row.attempts == 1


def test_stale_failure_does_not_override_published(monkeypatch):
    session = _session()
    _insert_event(session, event_id="e5")
    now = datetime.now(timezone.utc)

    class _TakeoverPublisher:
        def publish(self, event_type: str, payload: dict) -> None:
            # 模拟旧派发者发送期间新派发者已完成并标记 published。
            session.execute(
                knowledge_outbox_events.update()
                .where(knowledge_outbox_events.c.id == "e5")
                .values(
                    state="published",
                    claim_owner="new-owner",
                    claim_until=None,
                    updated_at=now,
                )
            )
            session.commit()
            raise RuntimeError("late failure")

    monkeypatch.setattr(celery_module, "CeleryPublisher", _TakeoverPublisher)

    assert dispatch_pending_knowledge_events(session) == 0
    row = session.execute(
        select(knowledge_outbox_events).where(knowledge_outbox_events.c.id == "e5")
    ).mappings().one()
    assert row.state == "published"
    assert row.claim_owner == "new-owner"
    assert row.attempts == 0


def test_migration_repairs_existing_nullable_column():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE knowledge_outbox_events ("
                "id VARCHAR(64) PRIMARY KEY, school_id VARCHAR(64) NOT NULL, "
                "job_id VARCHAR(64) NOT NULL, event_type VARCHAR(80) NOT NULL, "
                "state VARCHAR(20) NOT NULL, attempts INTEGER NOT NULL, "
                "available_at TIMESTAMP NOT NULL, claim_owner VARCHAR(160), "
                "claim_until TIMESTAMP, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_outbox_events "
                "(id, school_id, job_id, event_type, state, attempts, available_at, created_at) "
                "VALUES ('old2', 'sch-1', 'job-3', 'knowledge.job', 'pending', 0, "
                "'2026-01-03 00:00:00', '2026-01-03 00:00:00')"
            )
        )

    _migrate_knowledge_outbox_updated_at(engine)

    column = {
        c["name"]: c for c in inspect(engine).get_columns("knowledge_outbox_events")
    }["updated_at"]
    assert column["nullable"] is False
    assert column["default"] is not None
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT updated_at FROM knowledge_outbox_events WHERE id = 'old2'")
        ).scalar_one()
    assert value is not None


def test_updated_at_migration_is_idempotent():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE knowledge_outbox_events ("
                "id VARCHAR(64) PRIMARY KEY, school_id VARCHAR(64) NOT NULL, "
                "job_id VARCHAR(64) NOT NULL, event_type VARCHAR(80) NOT NULL, "
                "state VARCHAR(20) NOT NULL, attempts INTEGER NOT NULL, "
                "available_at TIMESTAMP NOT NULL, created_at TIMESTAMP NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_outbox_events "
                "(id, school_id, job_id, event_type, state, attempts, available_at, created_at) "
                "VALUES ('old', 'sch-1', 'job-1', 'knowledge.job', 'pending', 0, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )

    _migrate_knowledge_outbox_updated_at(engine)
    _migrate_knowledge_outbox_updated_at(engine)

    columns = {c["name"]: c for c in inspect(engine).get_columns("knowledge_outbox_events")}
    assert "updated_at" in columns
    assert columns["updated_at"]["nullable"] is False
    assert columns["updated_at"]["default"] is not None
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT updated_at FROM knowledge_outbox_events WHERE id = 'old'")
        ).scalar_one()
    assert value is not None

    # 迁移后的表必须能给未显式填写该列的新行补默认值。
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_outbox_events "
                "(id, school_id, job_id, event_type, state, attempts, available_at, created_at) "
                "VALUES ('new', 'sch-1', 'job-2', 'knowledge.job', 'pending', 0, "
                "'2026-01-02 00:00:00', '2026-01-02 00:00:00')"
            )
        )
        new_value = conn.execute(
            text("SELECT updated_at FROM knowledge_outbox_events WHERE id = 'new'")
        ).scalar_one()
    assert new_value is not None
