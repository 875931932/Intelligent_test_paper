"""新增题目 AI 生成端点集成测试（HTTP 层 + 任务落库 + outbox 派发）。

镜像 tests/integration/test_ai_revise_api.py 的环境模式（SQLite 内存库 +
dependency_overrides + 真实登录），再直插一条最小试卷链路。
LLM 配置与 outbox 派发在测试里打桩，保证不发真实模型请求、不连 Redis。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import (
    Base,
    User,
    assessment_units,
    blueprint_versions,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    generated_questions,
    generation_runs,
    knowledge_cards,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.db.session import get_session
from app.main import app
from app.services import ai_create_service
from app.services.auth_service import hash_password

C = "/api/v1/courses/{cid}"
AI_PATH = "/api/v1/courses/{cid}/paper-versions/pv1/items/ai-generate"


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

    try:
        runner = TestClient(app)
        login = runner.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})
        yield auth, factory, dispatched
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_paper(factory, cid: str) -> None:
    """直插最小试卷链路（与 unit/test_ai_create_service 的 fixture 同构）。"""
    with factory() as session:
        with session.begin():
            session.execute(framework_versions.insert().values(
                id="fv1", course_id=cid, version_no=1, status="published", payload={},
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
            session.execute(exam_projects.insert().values(
                id="proj1", course_id=cid, name="Midterm", status="draft",
            ))
            session.execute(blueprint_versions.insert().values(
                id="bv1", course_id=cid, exam_project_id="proj1",
                framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
            ))
            session.execute(generation_runs.insert().values(
                id="gr1", course_id=cid, framework_version_id="fv1",
                catalog_version_id="cv1", blueprint_version_id="bv1",
                prompt_template_version="v1", run_type="full", status="completed",
                contract_snapshot={},
            ))
            session.execute(plan_items.insert().values(
                id="pi1", course_id=cid, blueprint_version_id="bv1",
                assessment_unit_id="au1", question_type="single_choice", item_index=1,
                score=2.0, difficulty="medium", cognitive_level="understand",
                exam_point_id="ep1", knowledge_card_id="kc1",
            ))
            session.execute(generated_questions.insert().values(
                id="gq1", course_id=cid, generation_run_id="gr1", plan_item_id="pi1",
                knowledge_card_id="kc1", revision_no=1, status="candidate",
                payload={
                    "stem": "原题干？",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "A",
                    "explanation": "原解析",
                    "question_type": "single_choice",
                    "difficulty": "medium",
                    "cognitive_level": "understand",
                    "score": 2.0,
                },
            ))
            session.execute(paper_versions.insert().values(
                id="pv1", course_id=cid, exam_project_id="proj1",
                generation_run_id="gr1", version_no=1, status="candidate",
            ))
            session.execute(paper_items.insert().values(
                id="item1", course_id=cid, paper_version_id="pv1",
                generated_question_id="gq1", display_order=1,
            ))


def _prepare(env, monkeypatch, *, llm: bool = True):
    """建课程 + 播种试卷链路；返回 (client, cid, dispatched)。"""
    auth, factory, dispatched = env
    course = auth.post("/api/v1/courses", json={"name": "AI出题课", "slug": "ai-cre"})
    assert course.status_code == 201, course.text
    cid = course.json()["id"]
    _seed_paper(factory, cid)
    monkeypatch.setattr(ai_create_service, "llm_configured", lambda: llm)
    return auth, cid, dispatched


def test_ai_generate_returns_task_row_and_dispatches(env, monkeypatch):
    auth, cid, dispatched = _prepare(env, monkeypatch)

    resp = auth.post(AI_PATH.format(cid=cid), json={"instruction": "出一道填空题，考查术语定义"})
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_run_id"]

    _auth2, factory, _ = env
    with factory() as session:
        row = session.execute(
            select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
            .where(task_runs.c.id == task_id, task_runs.c.course_id == cid)
        ).one()
    assert row.task_type == "ai_create_item"
    assert row.status == "queued"
    assert row.payload["instruction"] == "出一道填空题，考查术语定义"
    assert row.payload["paper_version_id"] == "pv1"
    # outbox 派发必须发生且带 course_id（事务性投递，失败会保持 pending）
    assert dispatched == [cid]

    # 同卷同指令的在途请求复用同一任务（双击不重复烧模型）
    again = auth.post(AI_PATH.format(cid=cid), json={"instruction": "出一道填空题，考查术语定义"})
    assert again.status_code == 202
    assert again.json()["task_run_id"] == task_id


def test_ai_generate_rejects_blank_instruction(env, monkeypatch):
    auth, cid, _ = _prepare(env, monkeypatch)
    resp = auth.post(AI_PATH.format(cid=cid), json={"instruction": "   "})
    assert resp.status_code == 422


def test_ai_generate_503_when_llm_unconfigured(env, monkeypatch):
    auth, cid, _ = _prepare(env, monkeypatch, llm=False)
    resp = auth.post(AI_PATH.format(cid=cid), json={"instruction": "出题"})
    assert resp.status_code == 503


def test_ai_generate_404_for_missing_paper_version(env, monkeypatch):
    auth, cid, _ = _prepare(env, monkeypatch)
    resp = auth.post(
        "/api/v1/courses/{cid}/paper-versions/pv-x/items/ai-generate".format(cid=cid),
        json={"instruction": "出题"},
    )
    assert resp.status_code == 404


def test_ai_generate_404_for_foreign_paper_version(env, monkeypatch):
    # 试卷存在但不属于该课程：同样按 404 处理（课程隔离）
    auth, cid, _ = _prepare(env, monkeypatch)
    other = auth.post("/api/v1/courses", json={"name": "别的课", "slug": "other-c"})
    assert other.status_code == 201, other.text
    other_cid = other.json()["id"]
    resp = auth.post(AI_PATH.format(cid=other_cid), json={"instruction": "出题"})
    assert resp.status_code == 404


def test_ai_generate_409_when_finalized(env, monkeypatch):
    auth, cid, dispatched = _prepare(env, monkeypatch)
    _auth2, factory, _ = env
    with factory() as session:
        session.execute(
            update(paper_versions)
            .where(paper_versions.c.id == "pv1", paper_versions.c.course_id == cid)
            .values(status="finalized")
        )
        session.commit()
    resp = auth.post(AI_PATH.format(cid=cid), json={"instruction": "出题"})
    assert resp.status_code == 409
    assert dispatched == []  # 状态门禁在建任务之前，不应产生任何派发
