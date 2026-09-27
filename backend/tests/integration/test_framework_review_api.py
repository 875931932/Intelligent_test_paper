"""框架候选 AI 评审端点集成测试（HTTP 层 + 任务落库 + outbox 派发）。

镜像 tests/integration/test_exam_rules_ai_api.py 的环境模式（SQLite 内存库 +
dependency_overrides + 直插已发布框架），LLM 配置与 outbox 派发在测试里打桩，
保证不发真实模型请求、不连 Redis。端点只写 task_runs，报告由前端轮询展示——
教师照常走既有确认/拒绝流程，集成层断言任务行与派发，不碰框架数据。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.schema import Base, Course, User, framework_versions, task_runs
from app.db.session import get_session
from app.main import app
from app.services import framework_review_ai_service

PATH = "/api/v1/courses/course/framework-versions/current/ai-review"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        setup.add(User(id="owner-dev", display_name="Owner", role="teacher"))
        setup.flush()
        setup.add(Course(id="course", owner_id="owner-dev", slug="course", name="Course"))
        setup.flush()
        setup.execute(
            framework_versions.insert().values(
                id="fv1",
                course_id="course",
                version_no=1,
                status="published",
                payload={
                    "anchors": [{"key": "A1", "title": "第1章 绪论"}],
                    "final_exam_rules": {},
                },
            )
        )
        # 第二门课没有框架，用来验证 404
        setup.add(Course(id="nofx", owner_id="owner-dev", slug="nofx", name="NoFX"))
        setup.commit()

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override

    # outbox 派发打桩：记录调用，避免构造/连接 Celery 与 Redis
    dispatched: list[str] = []

    def fake_dispatch(session, publisher, *, course_id=None, limit=0):  # noqa: A002
        dispatched.append(str(course_id))

    monkeypatch.setattr(
        "app.infrastructure.tasks.outbox.dispatch_pending_events", fake_dispatch
    )
    monkeypatch.setattr(
        "app.infrastructure.tasks.celery_app.CeleryPublisher", lambda: object()
    )
    monkeypatch.setattr(framework_review_ai_service, "llm_configured", lambda: True)

    try:
        with TestClient(app) as client:
            yield client, engine, dispatched
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_review_returns_task_row_and_dispatches(env):
    client, engine, dispatched = env

    resp = client.post(PATH, json={"instruction": "重点看权重与覆盖"})
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_run_id"]

    with Session(engine) as session:
        row = session.execute(
            select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
            .where(task_runs.c.id == task_id, task_runs.c.course_id == "course")
        ).one()
    assert row.task_type == "review_framework_candidate"
    assert row.status == "queued"
    assert row.payload == {"course_id": "course", "instruction": "重点看权重与覆盖"}
    # outbox 派发必须发生且带 course_id（事务性投递，失败会保持 pending）
    assert dispatched == ["course"]

    # 同要求的在途请求复用同一任务（双击不重复烧模型）
    again = client.post(PATH, json={"instruction": "重点看权重与覆盖"})
    assert again.status_code == 202
    assert again.json()["task_run_id"] == task_id


def test_review_allows_empty_instruction(env):
    # 常规评审不强制一句话要求：空 instruction 也是合法任务
    client, _engine, dispatched = env
    resp = client.post(PATH, json={})
    assert resp.status_code == 202, resp.text
    assert dispatched == ["course"]


def test_review_503_when_llm_unconfigured(env, monkeypatch):
    client, _engine, dispatched = env
    monkeypatch.setattr(framework_review_ai_service, "llm_configured", lambda: False)
    resp = client.post(PATH, json={"instruction": "重点看权重"})
    assert resp.status_code == 503
    assert dispatched == []  # 503 在建任务之前，不应产生任何派发


def test_review_404_without_framework(env):
    client, _engine, _dispatched = env
    resp = client.post(
        "/api/v1/courses/nofx/framework-versions/current/ai-review",
        json={"instruction": "重点看权重"},
    )
    assert resp.status_code == 404, resp.text
    assert "命题框架" in resp.json()["detail"]
