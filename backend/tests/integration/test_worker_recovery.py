from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User, outbox_events, task_runs
from app.infrastructure.tasks.models import claim_task, create_task_run, wait_for_external
from app.infrastructure.tasks.recovery import recover_tasks


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="owner", display_name="Owner", role="teacher"))
    session.flush()
    session.add(Course(id="course", owner_id="owner", slug="course", name="Course"))
    session.commit()
    return engine, session


def test_recovery_requeues_expired_and_due_external_without_duplicate_active_events(tmp_path):
    engine, session = _session(tmp_path)
    try:
        now = datetime.now(UTC)
        expired = create_task_run(session, course_id="course", task_type="parse", idempotency_key="expired", input_version="v1", payload={})
        waiting = create_task_run(session, course_id="course", task_type="poll", idempotency_key="waiting", input_version="v1", payload={})
        session.execute(update(task_runs).where(task_runs.c.id == expired).values(status="running", lease_owner="dead", lease_expires_at=now - timedelta(seconds=1)))
        assert claim_task(session, course_id="course", task_id=waiting, worker_id="worker", now=now - timedelta(seconds=2), lease_seconds=30)
        assert wait_for_external(
            session,
            course_id="course",
            task_id=waiting,
            worker_id="worker",
            next_poll_at=now - timedelta(seconds=1),
            now=now - timedelta(seconds=2),
        )
        session.execute(update(outbox_events).values(status="published"))
        session.commit()

        assert recover_tasks(session, course_id="course", now=now) == 2
        assert recover_tasks(session, course_id="course", now=now) == 0
        statuses = dict(session.execute(select(task_runs.c.id, task_runs.c.status)).all())
        assert statuses == {expired: "queued", waiting: "queued"}
        active = session.execute(select(outbox_events).where(outbox_events.c.status == "pending")).all()
        assert len(active) == 2
    finally:
        session.close()
        engine.dispose()
