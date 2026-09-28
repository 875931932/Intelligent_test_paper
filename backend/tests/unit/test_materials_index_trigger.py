"""materials 解析端点的语料索引触发（_maybe_enqueue_index）。

覆盖：ready 才入队 + 派发、任务已存在不重复入队/派发、未 ready 与无 run_id 跳过、
入队/派发失败被吞（不阻塞解析响应）。outbox 派发打桩，不连 Celery/Redis。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.v1 import materials as materials_api
from app.db.schema import Base, task_runs


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trigger.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
        s.close()


def _stub_outbox(monkeypatch):
    dispatched: list[str] = []

    def fake_dispatch(session, publisher, *, course_id=None, limit=0):  # noqa: A002
        dispatched.append(str(course_id))

    monkeypatch.setattr(
        "app.infrastructure.tasks.outbox.dispatch_pending_events", fake_dispatch
    )
    monkeypatch.setattr(
        "app.infrastructure.tasks.celery_app.CeleryPublisher", lambda: object()
    )
    return dispatched


def test_ready_parse_enqueues_index_once_and_dispatches(session, monkeypatch):
    dispatched = _stub_outbox(monkeypatch)

    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="r1", parse_status="ready"
    )

    rows = session.execute(
        select(task_runs.c.payload, task_runs.c.idempotency_key).where(
            task_runs.c.task_type == "material_index"
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].payload == {"course_id": "c1", "run_id": "r1"}
    assert rows[0].idempotency_key == "material_index:r1"
    assert dispatched == ["c1"]

    # 重复轮询返回 ready：幂等键命中 → 不重复入队、不重复派发
    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="r1", parse_status="ready"
    )
    rows = session.execute(
        select(task_runs.c.id).where(task_runs.c.task_type == "material_index")
    ).all()
    assert len(rows) == 1
    assert dispatched == ["c1"]


def test_not_ready_or_missing_run_skips_index(session, monkeypatch):
    dispatched = _stub_outbox(monkeypatch)
    enqueued: list[tuple] = []

    def fake_enqueue(session, *, course_id, run_id):
        enqueued.append((course_id, run_id))
        return "task-x"

    monkeypatch.setattr(
        "app.services.content_index_service.enqueue_index_task", fake_enqueue
    )

    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="r1", parse_status="running"
    )
    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="r1", parse_status="failed"
    )
    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="", parse_status="ready"
    )

    assert enqueued == []
    assert dispatched == []


def test_enqueue_failure_is_swallowed_without_dispatch(session, monkeypatch):
    """入队异常 → 回滚并跳过，不向解析响应抛出。"""
    dispatched = _stub_outbox(monkeypatch)

    def boom(session, *, course_id, run_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.content_index_service.enqueue_index_task", boom)

    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="r1", parse_status="ready"
    )

    assert dispatched == []
    assert session.execute(select(task_runs.c.id)).all() == []


def test_dispatch_failure_is_swallowed(session, monkeypatch):
    """派发异常 → 回滚派发侧状态，任务行已提交不丢，不阻塞解析响应。"""
    _stub_outbox(monkeypatch)

    def boom_dispatch(session, publisher, *, course_id=None, limit=0):  # noqa: A002
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        "app.infrastructure.tasks.outbox.dispatch_pending_events", boom_dispatch
    )

    materials_api._maybe_enqueue_index(
        session, course_id="c1", run_id="r1", parse_status="ready"
    )

    rows = session.execute(
        select(task_runs.c.id).where(task_runs.c.task_type == "material_index")
    ).all()
    assert len(rows) == 1  # 任务行在显式 commit 中先落地
