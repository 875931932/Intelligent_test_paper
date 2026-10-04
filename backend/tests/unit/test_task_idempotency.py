from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User, outbox_events, task_runs
from app.infrastructure.tasks import worker as task_worker
from app.infrastructure.tasks.models import cancel_task, claim_task, complete_task, create_task_run, fail_task, refresh_lease
from app.infrastructure.tasks.outbox import FakePublisher, dispatch_pending_events
from app.infrastructure.tasks.worker import execute_task, register_task_handler


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


def test_expired_lease_owner_can_still_write_terminal_state(session):
    """租约过期但 owner 未被抢占时仍可写终态（complete/fail 同口径）。

    生成实测跑 41~49 分钟，远超 generation_run 的 1800s 租约：过期门曾让
    complete_task 静默 0 行，任务永远卡在 running、前端计时器永不停
    （2026-10-04 两单实测）。owner 门已覆盖抢占（claim_task 易主）与
    重排（recovery 清 owner），过期不再阻断收尾。
    """
    now = datetime.now(UTC)
    finished = now + timedelta(minutes=49)  # 租约(30min)早已过期

    ok_id = create_task_run(session, course_id="course-a", task_type="generation_run", idempotency_key="expired-ok", input_version="v1", payload={})
    assert claim_task(session, course_id="course-a", task_id=ok_id, worker_id="worker-1", now=now, lease_seconds=1800)
    assert complete_task(session, course_id="course-a", task_id=ok_id, worker_id="worker-1", result={"ok": True}, now=finished)
    row = session.execute(select(task_runs).where(task_runs.c.id == ok_id)).one()._mapping
    assert row["status"] == "succeeded"
    assert row["result"] == {"ok": True}
    assert row["lease_owner"] is None

    bad_id = create_task_run(session, course_id="course-a", task_type="generation_run", idempotency_key="expired-bad", input_version="v1", payload={})
    assert claim_task(session, course_id="course-a", task_id=bad_id, worker_id="worker-1", now=now, lease_seconds=1800)
    assert fail_task(session, course_id="course-a", task_id=bad_id, worker_id="worker-1", error_code="handler_error", error_message="boom", now=finished)
    row = session.execute(select(task_runs).where(task_runs.c.id == bad_id)).one()._mapping
    assert row["status"] == "failed"
    assert row["error_code"] == "handler_error"

    # 语义边界不变：owner 已被抢占的过期任务，旧 worker 依然写不进终态。
    taken_id = create_task_run(session, course_id="course-a", task_type="generation_run", idempotency_key="expired-taken", input_version="v1", payload={})
    assert claim_task(session, course_id="course-a", task_id=taken_id, worker_id="worker-1", now=now, lease_seconds=1800)
    assert claim_task(session, course_id="course-a", task_id=taken_id, worker_id="worker-2", now=finished, lease_seconds=1800)
    assert not complete_task(session, course_id="course-a", task_id=taken_id, worker_id="worker-1", result={"ok": True}, now=finished)


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


def test_worker_entrypoint_claims_and_completes_registered_task(session, monkeypatch):
    task_id = create_task_run(session, course_id="course-a", task_type="unit.test", idempotency_key="worker", input_version="v1", payload={"answer": 42})
    session.commit()
    monkeypatch.setattr("app.infrastructure.tasks.worker.get_session_factory", lambda: lambda: session)
    register_task_handler("unit.test", lambda context: {"received": context.payload["answer"]})

    assert execute_task(task_id, worker_id="worker")
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()._mapping
    assert row["status"] == "succeeded"
    assert row["result"] == {"received": 42}


def test_worker_failure_surfaces_real_exception_instead_of_generic_text(session, monkeypatch):
    task_id = create_task_run(session, course_id="course-a", task_type="unit.boom", idempotency_key="boom", input_version="v1", payload={})
    session.commit()
    monkeypatch.setattr("app.infrastructure.tasks.worker.get_session_factory", lambda: lambda: session)

    def boom_handler(_context):
        raise AttributeError("'Settings' object has no attribute 'llm_generation_disable_thinking'")

    register_task_handler("unit.boom", boom_handler)

    assert not execute_task(task_id, worker_id="worker")
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()._mapping
    assert row["status"] == "failed"
    assert row["error_code"] == "handler_error"
    assert row["error_message"].startswith("task handler failed:")
    assert "llm_generation_disable_thinking" in row["error_message"]


def test_worker_failure_prefers_persisted_run_error(session, monkeypatch):
    task_id = create_task_run(
        session,
        course_id="course-a",
        task_type="unit.boom",
        idempotency_key="boom-persisted",
        input_version="v1",
        payload={"generation_run_id": "run-1"},
    )
    session.commit()
    monkeypatch.setattr("app.infrastructure.tasks.worker.get_session_factory", lambda: lambda: session)
    monkeypatch.setattr("app.infrastructure.tasks.worker._persisted_run_error", lambda _payload: "persisted detail")

    def boom_handler(_context):
        raise ValueError("boom")

    register_task_handler("unit.boom", boom_handler)

    assert not execute_task(task_id, worker_id="worker")
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()._mapping
    assert row["error_message"] == "task handler failed: persisted detail"


def test_worker_context_can_pause_long_external_task(session, monkeypatch):
    task_id = create_task_run(session, course_id="course-a", task_type="unit.poll", idempotency_key="poll-worker", input_version="v1", payload={})
    session.commit()
    monkeypatch.setattr("app.infrastructure.tasks.worker.get_session_factory", lambda: lambda: session)

    def pause_handler(context):
        assert context.report_progress(stage="submitted", progress=20)
        assert context.heartbeat(lease_seconds=120)
        assert context.pause_until(datetime.now(UTC) + timedelta(seconds=30))
        return {}

    register_task_handler("unit.poll", pause_handler)

    assert execute_task(task_id, worker_id="worker")
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()._mapping
    assert row["status"] == "waiting_external"
    assert row["stage"] == "waiting_external"
    assert row["progress"] == 20


def test_generation_worker_uses_extended_lease(session, monkeypatch):
    task_id = create_task_run(
        session,
        course_id="course-a",
        task_type="generation_run",
        idempotency_key="generation-lease",
        input_version="v1",
        payload={},
    )
    session.commit()
    monkeypatch.setattr("app.infrastructure.tasks.worker.get_session_factory", lambda: lambda: session)
    monkeypatch.setitem(task_worker._HANDLERS, "generation_run", lambda _context: {"ok": True})

    assert execute_task(task_id, worker_id="worker")
    row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).one()._mapping
    assert row["status"] == "succeeded"
