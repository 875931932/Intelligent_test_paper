"""蓝图题位 AI 调整建议端点集成测试（HTTP 层 + 任务落库 + outbox 派发）。

镜像 tests/integration/test_paper_review_api.py 的环境模式（SQLite 内存库 +
dependency_overrides + 真实登录 + 直插蓝图链路）。LLM 配置与 outbox 派发在
测试里打桩，保证不发真实模型请求、不连 Redis。端点只写 task_runs；建议由
前端展示、教师确认后走既有 PATCH plan-items，集成层断言任务行与派发即可。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    blueprint_versions,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    knowledge_cards,
    knowledge_catalog_versions,
    plan_items,
    task_runs,
)
from app.db.session import get_session
from app.main import app
from app.services import blueprint_suggest_service
from app.services.auth_service import hash_password

SUGGEST_PATH = "/api/v1/courses/{cid}/exam-projects/proj1/blueprints/current/ai-suggest"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            User(
                id="admin",
                username="admin",
                password_hash=hash_password("123456"),
                display_name="System",
                role="admin",
            )
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session

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
    monkeypatch.setattr(blueprint_suggest_service, "llm_configured", lambda: True)

    try:
        runner = TestClient(app)
        login = runner.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})
        yield auth, factory, dispatched
    finally:
        app.dependency_overrides.clear()
        # active_* 是指向 blueprint/generation/paper 的复合 FK；SQLite 的
        # DROP TABLE 隐式 DELETE 父表时要求没有子行引用，先清指针再删表
        with factory() as session:
            session.execute(
                update(exam_projects).values(
                    active_blueprint_version_id=None,
                    active_generation_run_id=None,
                    active_paper_version_id=None,
                )
            )
            session.commit()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _create_course(env, *, name: str, slug: str) -> str:
    auth, _factory, _dispatched = env
    resp = auth.post("/api/v1/courses", json={"name": name, "slug": slug})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_blueprint(factory, cid: str) -> None:
    """直插最小蓝图链路（与 unit/test_blueprint_suggest_service 的 fixture 同构）。"""
    with factory() as session:
        with session.begin():
            session.execute(framework_versions.insert().values(
                id="fv1", course_id=cid, version_no=1, status="published",
                payload={
                    "anchors": [{"key": "A1", "title": "第1章 绪论"}],
                    "final_exam_rules": {
                        "exam_form": "闭卷笔试",
                        "duration_minutes": 90,
                        "total_score": 100,
                        "question_type_ratios": [
                            {"question_type": "single_choice", "ratio": 60},
                            {"question_type": "short_answer", "ratio": 40},
                        ],
                        "chapter_weights": [{"anchor_key": "A1", "weight": 100}],
                        "assessment_focus": [],
                    },
                },
            ))
            session.execute(knowledge_catalog_versions.insert().values(
                id="cv1", course_id=cid, framework_version_id="fv1",
                version_no=1, status="published",
            ))
            session.execute(exam_points.insert().values(
                id="ep1", course_id=cid, framework_version_id="fv1", anchor_key="A1",
                code="EP1", title="考点1", assessment_requirement="掌握A", weight_value=30.0,
                weight_source="teacher_confirmed", weight_group_id="A1", priority="normal",
                cognitive_targets=[], assessment_orientations=[], allowed_question_types=[],
                operational_detail_policy="supporting_only", scope_boundary={},
                required_evidence_roles=[], retrieval_intent="围绕A检索",
                teaching_anchor_keys=[], status="active",
            ))
            session.execute(content_domains.insert().values(
                id="cd1", course_id=cid, catalog_version_id="cv1", parent_domain_id=None,
                level=1, framework_anchor_key="A1", code="A1", name="章1", status="active",
            ))
            session.execute(assessment_units.insert().values(
                id="au1", course_id=cid, catalog_version_id="cv1", content_domain_id="cd1",
                exam_point_id="ep1", code="U1", title="单元1", performance_statement="ps1",
                weight=30, status="active",
            ))
            session.execute(knowledge_cards.insert().values(
                id="kc1", course_id=cid, catalog_version_id="cv1", assessment_unit_id="au1",
                name="卡A1", performance_statement="掌握A1",
                assessable_content=["A1-原子1定义"], content_hash="hca1",
                status="active", concept_cluster="A", answer_proposition="A1-边界",
            ))
            # 先建项目后建蓝图：active_blueprint_version_id 是复合 FK，父行先落库
            session.execute(exam_projects.insert().values(
                id="proj1", course_id=cid, name="Midterm", status="draft",
            ))
            session.execute(blueprint_versions.insert().values(
                id="bv1", course_id=cid, exam_project_id="proj1",
                framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
                status="draft", type_rules={}, chapter_weights={"A1": 100},
            ))
            session.execute(
                exam_projects.update()
                .where(exam_projects.c.id == "proj1")
                .values(active_blueprint_version_id="bv1")
            )
            for idx, qtype, score in ((1, "single_choice", 6.0), (2, "short_answer", 4.0)):
                session.execute(plan_items.insert().values(
                    id=f"pi{idx}", course_id=cid, blueprint_version_id="bv1",
                    assessment_unit_id="au1", question_type=qtype, item_index=idx,
                    score=score, difficulty="medium", cognitive_level="understand",
                    assessment_mode="conceptual", exam_point_id="ep1",
                    knowledge_card_id="kc1",
                ))


def test_suggest_returns_task_row_and_dispatches(env):
    auth, factory, dispatched = env
    cid = _create_course(env, name="蓝图建议课", slug="bp-suggest")
    _seed_blueprint(factory, cid)

    resp = auth.post(
        SUGGEST_PATH.format(cid=cid), json={"instruction": "难题调多一点"}
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_run_id"]

    with factory() as session:
        row = session.execute(
            select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
            .where(task_runs.c.id == task_id, task_runs.c.course_id == cid)
        ).one()
    assert row.task_type == "suggest_blueprint_adjustments"
    assert row.status == "queued"
    assert row.payload == {
        "course_id": cid,
        "project_id": "proj1",
        "instruction": "难题调多一点",
    }
    # outbox 派发必须发生且带 course_id（事务性投递，失败会保持 pending）
    assert dispatched == [cid]

    # 同要求的在途请求复用同一任务（双击不重复烧模型）
    again = auth.post(SUGGEST_PATH.format(cid=cid), json={"instruction": "难题调多一点"})
    assert again.status_code == 202
    assert again.json()["task_run_id"] == task_id


def test_suggest_503_when_llm_unconfigured(env, monkeypatch):
    auth, factory, dispatched = env
    cid = _create_course(env, name="蓝图建议课503", slug="bp-suggest-503")
    _seed_blueprint(factory, cid)
    monkeypatch.setattr(blueprint_suggest_service, "llm_configured", lambda: False)
    resp = auth.post(SUGGEST_PATH.format(cid=cid), json={"instruction": "x" * 20})
    assert resp.status_code == 503
    assert dispatched == []  # 503 在建任务之前，不应产生任何派发


def test_suggest_404_unknown_project(env):
    auth, _factory, _dispatched = env
    cid = _create_course(env, name="蓝图建议课404", slug="bp-suggest-404")
    resp = auth.post(
        f"/api/v1/courses/{cid}/exam-projects/nope/blueprints/current/ai-suggest",
        json={"instruction": "难题调多一点"},
    )
    assert resp.status_code == 404, resp.text


def test_suggest_409_confirmed_blueprint(env):
    auth, factory, _dispatched = env
    cid = _create_course(env, name="蓝图建议课409", slug="bp-suggest-409")
    _seed_blueprint(factory, cid)
    with factory() as session:
        session.execute(
            update(blueprint_versions)
            .where(blueprint_versions.c.id == "bv1", blueprint_versions.c.course_id == cid)
            .values(status="confirmed")
        )
        session.commit()

    resp = auth.post(SUGGEST_PATH.format(cid=cid), json={"instruction": "难题调多一点"})
    assert resp.status_code == 409, resp.text
    assert "不可原地修改" in resp.json()["detail"]
