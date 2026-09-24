"""合同槽位 AI 解释端点集成测试（HTTP 层 + 任务落库 + outbox 派发）。

镜像 tests/integration/test_ai_create_api.py 的环境模式（SQLite 内存库 +
dependency_overrides + 真实登录），再播种一条可分配的蓝图链路（照抄
tests/unit/test_contract_execution 的已验证参数）。LLM 配置与 outbox 派发
在测试里打桩，保证不发真实模型请求、不连 Redis。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import (
    Base,
    User,
    assessment_units,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    knowledge_cards,
    knowledge_catalog_versions,
    task_runs,
)
from app.db.session import get_session
from app.main import app
from app.services import contract_explain_service
from app.services.auth_service import hash_password
from app.services.blueprint_persistence_service import (
    confirm_blueprint,
    create_draft_blueprint,
)

C = "/api/v1/courses/{cid}"
EXPLAIN_PATH = "/api/v1/courses/{cid}/exam-projects/proj1/contract-slots/1/explain"


def _ep(id_, course, fv, anchor, code, title, req, w, group, intent):
    return {
        "id": id_, "course_id": course,
        "framework_version_id": fv, "anchor_key": anchor,
        "code": code, "title": title,
        "assessment_requirement": req,
        "weight_value": w, "weight_source": "teacher_confirmed",
        "weight_group_id": group, "priority": "normal",
        "cognitive_targets": [], "assessment_orientations": [],
        "allowed_question_types": [],
        "operational_detail_policy": "supporting_only",
        "scope_boundary": {}, "required_evidence_roles": [],
        "retrieval_intent": intent,
        "teaching_anchor_keys": [],
        "status": "active",
    }


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
        # exam_projects.active_blueprint_version_id 与 blueprint_versions 构成
        # 环状 FK（schema 里 use_alter），drop_all 排序无法自洽；拆库前关掉 FK。
        with engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_contract(factory, cid: str) -> str:
    """直插蓝图链路（与 unit/test_contract_explain_service 的 fixture 同构），返回 bv_id。"""
    with factory() as session:
        with session.begin():
            session.execute(framework_versions.insert().values(
                id="fv1", course_id=cid, version_no=1, status="published", payload={},
            ))
            session.execute(knowledge_catalog_versions.insert().values(
                id="cv1", course_id=cid, framework_version_id="fv1",
                version_no=1, status="published",
            ))
            session.execute(exam_points.insert(), [
                _ep("au1", cid, "fv1", "A1", "EP1", "考点1", "掌握概念A的定义和应用",
                    40, "A1", "围绕概念A检索材料"),
                _ep("au2", cid, "fv1", "A2", "EP2", "考点2", "掌握概念B的定义和应用",
                    60, "A2", "围绕概念B检索材料"),
            ])
            session.execute(content_domains.insert(), [
                {"id": "cd1", "course_id": cid, "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A1", "code": "A1", "name": "章1", "status": "active"},
                {"id": "cd2", "course_id": cid, "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A2", "code": "A2", "name": "章2", "status": "active"},
            ])
            session.execute(assessment_units.insert(), [
                {"id": "au1", "course_id": cid, "catalog_version_id": "cv1",
                 "content_domain_id": "cd1", "exam_point_id": "au1",
                 "code": "U1", "title": "单元1",
                 "performance_statement": "掌握概念A", "weight": 40, "status": "active"},
                {"id": "au2", "course_id": cid, "catalog_version_id": "cv1",
                 "content_domain_id": "cd2", "exam_point_id": "au2",
                 "code": "U2", "title": "单元2",
                 "performance_statement": "掌握概念B", "weight": 60, "status": "active"},
            ])
            cards = [
                ("c1a", "au1", "卡1a", ["A-原子1", "A-原子2", "A-原子3"], "A", "A边界1"),
                ("c1b", "au1", "卡1b", ["A-原子4", "A-原子5", "A-原子6"], "A", "A边界2"),
                ("c1c", "au1", "卡1c", ["A-原子7", "A-原子8", "A-原子9"], "A", "A边界3"),
                ("c2a", "au2", "卡2a", ["B-原子1", "B-原子2", "B-原子3"], "B", "B边界1"),
                ("c2b", "au2", "卡2b", ["B-原子4", "B-原子5", "B-原子6"], "B", "B边界2"),
                ("c2c", "au2", "卡2c", ["B-原子7", "B-原子8", "B-原子9"], "B", "B边界3"),
            ]
            session.execute(knowledge_cards.insert(), [
                {
                    "id": cid_card, "course_id": cid, "catalog_version_id": "cv1",
                    "assessment_unit_id": unit, "name": name,
                    "performance_statement": f"{name}陈述",
                    "assessable_content": atoms,
                    "content_hash": f"h{cid_card}", "status": "active",
                    "concept_cluster": cluster, "answer_proposition": prop,
                }
                for cid_card, unit, name, atoms, cluster, prop in cards
            ])
            session.execute(exam_projects.insert().values(
                id="proj1", course_id=cid, name="Midterm", status="draft",
            ))

        card_ids = ["c1a", "c1b", "c1c", "c2a", "c2b", "c2c"]
        bv_id, _plan = create_draft_blueprint(
            session,
            course_id=cid,
            project_id="proj1",
            framework_version_id="fv1",
            catalog_version_id="cv1",
            type_rules={"single_choice": {"count": 5, "score": 20.0}},
            chapter_weights={"A1": 40, "A2": 60},
            units_payload=[
                {"unit_id": "au1", "exam_point_id": "", "anchor_key": "A1", "card_ids": ["c1a", "c1b", "c1c"]},
                {"unit_id": "au2", "exam_point_id": "", "anchor_key": "A2", "card_ids": ["c2a", "c2b", "c2c"]},
            ],
            card_semantic_profiles={
                cid: {
                    "concept_cluster": "A" if cid[1] == "1" else "B",
                    "answer_proposition": f"{cid}-边界",
                }
                for cid in card_ids
            },
            card_question_types={cid: ["single_choice"] for cid in card_ids},
        )
        confirm_blueprint(
            session, course_id=cid, project_id="proj1", blueprint_version_id=bv_id
        )
        return bv_id


def _prepare(env, monkeypatch, *, llm: bool = True):
    """建课程 + 播种可分配蓝图链路；返回 (client, cid, dispatched, bv_id)。"""
    auth, factory, dispatched = env
    course = auth.post("/api/v1/courses", json={"name": "合同解释课", "slug": "expl-1"})
    assert course.status_code == 201, course.text
    cid = course.json()["id"]
    bv_id = _seed_contract(factory, cid)
    monkeypatch.setattr(contract_explain_service, "llm_configured", lambda: llm)
    return auth, cid, dispatched, bv_id


def test_explain_returns_task_row_and_dispatches(env, monkeypatch):
    auth, cid, dispatched, bv_id = _prepare(env, monkeypatch)

    resp = auth.post(
        EXPLAIN_PATH.format(cid=cid),
        json={"instruction": "为什么不是考另一个原子", "allocation_seed": 1},
    )
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_run_id"]

    _auth2, factory, _ = env
    with factory() as session:
        row = session.execute(
            select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
            .where(task_runs.c.id == task_id, task_runs.c.course_id == cid)
        ).one()
    assert row.task_type == "explain_contract_slot"
    assert row.status == "queued"
    assert row.payload["project_id"] == "proj1"
    assert row.payload["item_index"] == 1
    assert row.payload["allocation_seed"] == 1
    assert row.payload["blueprint_version_id"] == bv_id
    assert row.payload["instruction"] == "为什么不是考另一个原子"
    # outbox 派发必须发生且带 course_id（事务性投递，失败会保持 pending）
    assert dispatched == [cid]

    # 同槽位同方案同追问的在途请求复用同一任务（双击不重复烧模型）
    again = auth.post(
        EXPLAIN_PATH.format(cid=cid),
        json={"instruction": "为什么不是考另一个原子", "allocation_seed": 1},
    )
    assert again.status_code == 202
    assert again.json()["task_run_id"] == task_id


def test_explain_409_when_no_blueprint(env, monkeypatch):
    auth, cid, dispatched, _ = _prepare(env, monkeypatch)
    proj = auth.post(f"/api/v1/courses/{cid}/exam-projects", json={"name": "无蓝图项目"})
    assert proj.status_code == 201, proj.text
    pid = proj.json()["id"]

    resp = auth.post(
        f"/api/v1/courses/{cid}/exam-projects/{pid}/contract-slots/1/explain",
        json={"instruction": "为什么"},
    )
    assert resp.status_code == 409, resp.text
    assert dispatched == []  # 无蓝图先拦一道，不建任务不派发


def test_explain_404_for_missing_project(env, monkeypatch):
    auth, cid, dispatched, _ = _prepare(env, monkeypatch)
    resp = auth.post(
        "/api/v1/courses/{cid}/exam-projects/proj-x/contract-slots/1/explain".format(cid=cid),
        json={"instruction": "为什么"},
    )
    assert resp.status_code == 404
    assert dispatched == []


def test_explain_404_for_foreign_course(env, monkeypatch):
    # 项目存在但不属于该课程：同样按 404 处理（课程隔离）
    auth, cid, dispatched, _ = _prepare(env, monkeypatch)
    other = auth.post("/api/v1/courses", json={"name": "别的课", "slug": "other-e"})
    assert other.status_code == 201, other.text
    other_cid = other.json()["id"]
    resp = auth.post(EXPLAIN_PATH.format(cid=other_cid), json={"instruction": "为什么"})
    assert resp.status_code == 404
    assert dispatched == []


def test_explain_422_for_unknown_item_index(env, monkeypatch):
    auth, cid, dispatched, _ = _prepare(env, monkeypatch)
    resp = auth.post(
        "/api/v1/courses/{cid}/exam-projects/proj1/contract-slots/99/explain".format(cid=cid),
        json={"instruction": "为什么"},
    )
    assert resp.status_code == 422, resp.text
    assert dispatched == []  # 槽位校验在建任务之前，不应产生任何派发


def test_explain_422_for_bad_instruction_type(env, monkeypatch):
    auth, cid, dispatched, _ = _prepare(env, monkeypatch)
    resp = auth.post(EXPLAIN_PATH.format(cid=cid), json={"instruction": 123})
    assert resp.status_code == 422
    assert dispatched == []


def test_explain_422_for_unknown_body_key(env, monkeypatch):
    auth, cid, dispatched, _ = _prepare(env, monkeypatch)
    resp = auth.post(EXPLAIN_PATH.format(cid=cid), json={"slot_revisions": []})
    assert resp.status_code == 422
    assert dispatched == []


def test_explain_503_when_llm_unconfigured(env, monkeypatch):
    auth, cid, dispatched, _ = _prepare(env, monkeypatch, llm=False)
    resp = auth.post(EXPLAIN_PATH.format(cid=cid), json={"instruction": "为什么"})
    assert resp.status_code == 503
    assert dispatched == []
