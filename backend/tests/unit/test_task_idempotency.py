from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User, outbox_events, task_runs
from app.infrastructure.tasks.models import cancel_task, claim_task, complete_task, create_task_run, fail_task, refresh_lease
from app.infrastructure.tasks.outbox import FakePublisher, dispatch_pending_events


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="owner", display_name="Owner", role="teacher"))
        session.flush()
        session.add_all([
            Course(id="course-a", owner_id="owner", slug="a", name="A"),
            Course(id="course-b", owner_id="owner", slug="b", name="B"),
        ])
        session.commit()
        yield session
    engine.dispose()


def test_same_course_idempotency_key_returns_one_task_and_one_pending_event(session):
    first = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="request-1", input_version="v1", payload={"x": 1})
    second = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="request-1", input_version="v1", payload={"x": 2})

    assert first == second
    assert session.execute(select(task_runs).where(task_runs.c.course_id == "course-a")).all()[0]._mapping["id"] == first
    events = session.execute(select(outbox_events).where(outbox_events.c.course_id == "course-a")).all()
    assert len(events) == 1
    assert events[0]._mapping["status"] == "pending"


def test_task_and_outbox_roll_back_together(session):
    with pytest.raises(RuntimeError):
        with session.begin():
            create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="rollback", input_version="v1", payload={}, task_id="rollback-task")
            raise RuntimeError("abort")

    assert not session.execute(select(task_runs).where(task_runs.c.idempotency_key == "rollback")).all()
    assert not session.execute(select(outbox_events).where(outbox_events.c.task_run_id == "rollback-task")).all()


def test_publisher_failure_leaves_event_pending_for_safe_retry(session):
    task_id = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="publish", input_version="v1", payload={})
    failed = FakePublisher(failures=1)

    assert dispatch_pending_events(session, failed, course_id="course-a", now=datetime.now(UTC)) == 0
    assert dispatch_pending_events(session, failed, course_id="course-a", now=datetime.now(UTC)) == 1
    assert failed.published == [("task.dispatch", {"task_id": task_id})]
    assert session.execute(select(outbox_events.c.status)).scalar_one() == "published"


def test_expired_lease_can_be_reclaimed_but_stale_worker_cannot_complete(session):
    task_id = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="lease", input_version="v1", payload={})
    now = datetime.now(UTC)
    assert claim_task(session, course_id="course-a", task_id=task_id, worker_id="worker-1", now=now, lease_seconds=1)
    assert claim_task(session, course_id="course-a", task_id=task_id, worker_id="worker-2", now=now + timedelta(seconds=2), lease_seconds=30)
    assert not complete_task(session, course_id="course-a", task_id=task_id, worker_id="worker-1", result={"ok": True}, now=now + timedelta(seconds=2))
    assert complete_task(session, course_id="course-a", task_id=task_id, worker_id="worker-2", result={"ok": True}, now=now + timedelta(seconds=2))


def test_cancelled_task_rejects_late_completion_and_lease_refresh_is_owner_only(session):
    task_id = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="cancel", input_version="v1", payload={})
    now = datetime.now(UTC)
    assert claim_task(session, course_id="course-a", task_id=task_id, worker_id="worker", now=now, lease_seconds=30)
    assert not refresh_lease(session, course_id="course-a", task_id=task_id, worker_id="other", now=now, lease_seconds=30)
    assert cancel_task(session, course_id="course-a", task_id=task_id, now=now)
    assert not complete_task(session, course_id="course-a", task_id=task_id, worker_id="worker", result={"late": True}, now=now)
    assert not fail_task(session, course_id="course-a", task_id=task_id, worker_id="worker", error_code="late", error_message="late", now=now)
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()._mapping
    assert row["status"] == "cancelled"
    assert row["result"] is None


def test_course_scoped_task_operations_do_not_cross_tenants(session):
    task_id = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="scoped", input_version="v1", payload={})

    assert not claim_task(session, course_id="course-b", task_id=task_id, worker_id="worker", now=datetime.now(UTC), lease_seconds=30)
    assert session.execute(select(task_runs.c.status).where(task_runs.c.id == task_id)).scalar_one() == "queued"


def test_two_sessions_reuse_committed_course_idempotency_key(session):
    engine = session.get_bind()
    first = create_task_run(session, course_id="course-a", task_type="parse", idempotency_key="two-sessions", input_version="v1", payload={})
    session.commit()
    with Session(engine) as second_session:
        second = create_task_run(second_session, course_id="course-a", task_type="parse", idempotency_key="two-sessions", input_version="v1", payload={})
        second_session.commit()
        assert second == first
    assert session.execute(select(task_runs.c.id).where(task_runs.c.course_id == "course-a", task_runs.c.idempotency_key == "two-sessions")).scalars().all() == [first]
