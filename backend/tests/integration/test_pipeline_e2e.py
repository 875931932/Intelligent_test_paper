"""TR-6.1 端到端集成测试：HTTP API 全链路。

流程：创建项目 → blueprint → contract → generate → paper version review → confirm。
使用 SQLite + FastAPI TestClient，不访问任何外部模型/网络。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    content_domains,
    exam_points,
    framework_versions,
    knowledge_cards,
    knowledge_catalog_versions,
)
from app.db.session import get_session, get_session_factory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine_and_factory(tmp_path):
    """SQLite engine + sessionmaker，所有表已创建 + 种子数据已落库。"""
    db_path = tmp_path / "e2e.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    with factory() as s:
        # Users + course
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()

        with s.begin():
            # framework version (published)
            s.execute(framework_versions.insert().values(
                id="fv1", course_id="c1", version_no=1, status="published",
                payload={"anchors": [{"key": "A1"}, {"key": "A2"}, {"key": "A3"}]},
            ))
            # catalog version (published)
            s.execute(knowledge_catalog_versions.insert().values(
                id="cv1", course_id="c1", framework_version_id="fv1",
                version_no=1, status="published",
            ))
            # exam_points：3 个，对应 A1/A2/A3
            def _ep(id_, anchor, code, title, req, w, group, intent):
                return {
                    "id": id_, "course_id": "c1",
                    "framework_version_id": "fv1", "anchor_key": anchor,
                    "code": code, "title": title, "assessment_requirement": req,
                    "weight_value": w, "weight_source": "teacher_confirmed",
                    "weight_group_id": group, "priority": "normal",
                    "cognitive_targets": [], "assessment_orientations": [],
                    "allowed_question_types": ["single_choice", "fill_blank", "true_false"],
                    "operational_detail_policy": "supporting_only",
                    "scope_boundary": {}, "required_evidence_roles": [],
                    "retrieval_intent": intent, "teaching_anchor_keys": [],
                    "status": "active",
                }
            s.execute(exam_points.insert(), [
                _ep("ep1", "A1", "EP1", "考点1", "掌握概念A的定义与典型场景",
                    34, "A1", "围绕概念A检索教材与讲义"),
                _ep("ep2", "A2", "EP2", "考点2", "掌握概念B的定义与典型场景",
                    33, "A2", "围绕概念B检索教材与讲义"),
                _ep("ep3", "A3", "EP3", "考点3", "掌握概念C的定义与典型场景",
                    33, "A3", "围绕概念C检索教材与讲义"),
            ])
            # content_domains
            s.execute(content_domains.insert(), [
                {"id": "cd1", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A1", "code": "A1", "name": "章1",
                 "status": "active"},
                {"id": "cd2", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A2", "code": "A2", "name": "章2",
                 "status": "active"},
                {"id": "cd3", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A3", "code": "A3", "name": "章3",
                 "status": "active"},
            ])
            # assessment_units：每章 1 个
            s.execute(assessment_units.insert(), [
                {"id": "au1", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd1", "exam_point_id": "ep1",
                 "code": "U1", "title": "单元1",
                 "performance_statement": "学生能够在具体场景下应用概念A",
                 "weight": 34, "status": "active"},
                {"id": "au2", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd2", "exam_point_id": "ep2",
                 "code": "U2", "title": "单元2",
                 "performance_statement": "学生能够在具体场景下应用概念B",
                 "weight": 33, "status": "active"},
                {"id": "au3", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd3", "exam_point_id": "ep3",
                 "code": "U3", "title": "单元3",
                 "performance_statement": "学生能够在具体场景下应用概念C",
                 "weight": 33, "status": "active"},
            ])
            # knowledge_cards：每单元至少 3 张卡（3 单元 × 3 = 9 张），
            # concept_cluster 以及 answer_proposition 填好，保证合同分配可用
            cards = []
            for au_id, letter, anchor in [("au1", "A", "A1"),
                                            ("au2", "B", "A2"),
                                            ("au3", "C", "A3")]:
                for idx in (1, 2, 3):
                    cid = f"c{letter.lower()}{idx}"
                    cards.append({
                        "id": cid, "course_id": "c1",
                        "catalog_version_id": "cv1", "assessment_unit_id": au_id,
                        "name": f"卡{letter}{idx}",
                        "performance_statement": f"掌握{letter}{idx}的典型命题形式",
                        "assessable_content": [
                            f"{letter}{idx}-原子1：核心定义",
                            f"{letter}{idx}-原子2：典型应用",
                            f"{letter}{idx}-原子3：易混辨析",
                        ],
                        "scope_boundary": {"chapter": anchor},
                        "cognitive_targets": ["记忆", "理解", "应用"],
                        "allowed_question_types": ["single_choice", "fill_blank", "true_false"],
                        "importance": 1,
                        "concept_cluster": f"Cluster-{letter}",
                        "answer_proposition": f"{letter}{idx} 的答案边界表述 #{idx}",
                        "prompt_material": [f"{letter}{idx}提示语"],
                        "relation_edges": [],
                        "content_hash": f"hash-{cid}",
                        "status": "active",
                        "version": 1,
                    })
            s.execute(knowledge_cards.insert(), cards)

    yield engine, factory
    engine.dispose()


@pytest.fixture
def client(engine_and_factory):
    """TestClient：通过 dependency_overrides 替换 get_session / get_session_factory，
    并在 app.state 注入 mock graph_invoke。"""
    from app.main import app

    engine, factory = engine_and_factory

    def _override_session():
        sess = factory()
        try:
            yield sess
        finally:
            sess.close()

    def _override_factory():
        return factory

    # 清理模块级缓存（get_session_factory / get_engine 都是 lru_cache）
    get_session_factory.cache_clear()
    from app.db.session import get_engine
    get_engine.cache_clear()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_session_factory] = _override_factory

    # Mock graph_invoke：每 plan_item 一题，质量 OK（第 1 个 needs_review=True，
    # 用于断言 needs_review 流程）；其余正常。
    def _mock_graph_invoke(session: Session, generation_run: dict, contract_snapshot: dict):
        from app.db.schema import plan_items as _pi
        bv_id = generation_run.get("blueprint_version_id")
        course_id = generation_run.get("course_id")
        rows = session.execute(
            select(_pi.c.id, _pi.c.question_type, _pi.c.score, _pi.c.difficulty,
                   _pi.c.cognitive_level, _pi.c.knowledge_card_id, _pi.c.item_index)
            .where(_pi.c.blueprint_version_id == bv_id, _pi.c.course_id == course_id)
            .order_by(_pi.c.item_index)
        ).all()
        out = []
        for i, r in enumerate(rows):
            qtype = r._mapping["question_type"] or "single_choice"
            needs_review = (i == 0)  # 第 1 道标记 needs_review，测试确认路径
            if qtype == "single_choice":
                options = [f"选项A-{i}", f"选项B-{i}", f"选项C-{i}", f"选项D-{i}"]
                answer = "A"
            elif qtype == "true_false":
                options = []
                answer = "true"
            elif qtype == "fill_blank":
                options = []
                answer = f"填空答案-{i}"
            else:
                options = []
                answer = f"参考-{i}"
            quality_checks = [
                {"check_type": "semantic", "status": "pass", "details": {"score": 0.9}},
                {"check_type": "answerable", "status": "pass", "details": {"score": 0.9}},
            ]
            if needs_review:
                quality_checks.append({
                    "check_type": "diversity",
                    "status": "warn",
                    "details": {"message": "与同组其他题相似度偏高，请教师复核", "score": 0.4},
                })
            out.append({
                "plan_item_id": r._mapping["id"],
                "knowledge_card_id": r._mapping["knowledge_card_id"],
                "stem": f"E2E mock 题干 [{i + 1}]，类型={qtype}",
                "options": options,
                "answer": answer,
                "question_type": qtype,
                "difficulty": r._mapping["difficulty"] or "medium",
                "cognitive_level": r._mapping["cognitive_level"] or "understand",
                "score": float(r._mapping["score"] or 0),
                "quality": {
                    "needs_review": needs_review,
                    "message": "teacher check" if needs_review else "ok",
                    "quality_checks": quality_checks,
                },
            })
        return out

    app.state.mock_graph_invoke = _mock_graph_invoke

    with TestClient(app) as c:
        yield c

    # 清理 overrides / state
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_session_factory, None)
    if hasattr(app.state, "mock_graph_invoke"):
        delattr(app.state, "mock_graph_invoke")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COURSE = "c1"
PREFIX = f"/api/v1/courses/{COURSE}"


def _units_and_cards() -> tuple[list[dict], dict[str, dict], dict[str, list[str]]]:
    """返回 units/card_semantic_profiles/card_question_types 载荷。"""
    units = [
        {"unit_id": "au1", "exam_point_id": "ep1", "anchor_key": "A1",
         "card_ids": ["ca1", "ca2", "ca3"]},
        {"unit_id": "au2", "exam_point_id": "ep2", "anchor_key": "A2",
         "card_ids": ["cb1", "cb2", "cb3"]},
        {"unit_id": "au3", "exam_point_id": "ep3", "anchor_key": "A3",
         "card_ids": ["cc1", "cc2", "cc3"]},
    ]
    profiles = {}
    qtypes = {}
    for cid, letter in [("ca1", "A"), ("ca2", "A"), ("ca3", "A"),
                        ("cb1", "B"), ("cb2", "B"), ("cb3", "B"),
                        ("cc1", "C"), ("cc2", "C"), ("cc3", "C")]:
        profiles[cid] = {
            "concept_cluster": f"Cluster-{letter}",
            "answer_proposition": f"{cid} 的答案边界表述",
        }
        qtypes[cid] = ["single_choice", "fill_blank", "true_false"]
    return units, profiles, qtypes


def _wait_for_task(client: TestClient, task_run_id: str, max_attempts: int = 20, interval: float = 0.05) -> dict:
    for _ in range(max_attempts):
        r = client.get(f"{PREFIX}/exam-projects/task-runs/{task_run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(interval)
    return body


# ---------------------------------------------------------------------------
# 主测试
# ---------------------------------------------------------------------------


def test_pipeline_e2e_tr6_1(client: TestClient):
    """TR-6.1：全链路 HTTP 集成测试。"""
    # 1. 创建 exam_project P1
    r = client.post(f"{PREFIX}/exam-projects", json={"name": "P1"})
    assert r.status_code == 201, r.text
    proj = r.json()
    project_id = proj["id"]
    assert project_id
    assert proj["name"] == "P1"
    assert proj["status"] == "draft"

    # 2. 创建 draft blueprint
    units, profiles, qtypes = _units_and_cards()
    # 题型规则：3 单选 × 2 填空 = 5 题，总分 5*10 = 50；
    # 章节权 40/40/20：5 题拆分 {2, 2, 1}，每章 × score=10 = {20, 20, 10}，
    # 占比恰好为 40/40/20，避免 "cannot be jointly satisfied"
    type_rules = {
        "single_choice": {"count": 3, "score": 10},
        "fill_blank": {"count": 2, "score": 10},
    }
    chapter_weights = {"A1": 40, "A2": 40, "A3": 20}
    r = client.post(
        f"{PREFIX}/exam-projects/{project_id}/blueprints",
        json={
            "framework_version_id": "fv1",
            "catalog_version_id": "cv1",
            "type_rules": type_rules,
            "chapter_weights": chapter_weights,
            "units": units,
            "card_semantic_profiles": profiles,
            "card_question_types": qtypes,
        },
    )
    assert r.status_code == 201, r.text
    bpres = r.json()
    bv_id = bpres["blueprint_version_id"]
    assert bv_id
    plan = bpres["plan"]
    assert isinstance(plan, list) and len(plan) >= 5, f"plan 长度不足: {len(plan)}"
    total = sum(float(p.get("score") or 0) for p in plan)
    assert abs(total - 50) < 0.001, f"总分应为 50，实际 {total}"

    # 3. GET plan-items
    r = client.get(f"{PREFIX}/exam-projects/{project_id}/blueprints/current/plan-items")
    assert r.status_code == 200, r.text
    plan2 = r.json()
    assert len(plan2) >= 5

    # 4. PATCH plan-items：调整第 1 题分值，确保仍是 0.5 的整数倍，
    #    总分保持 50（我们在相邻题做镜像加减，或不改总分 —— 这里选同题不变总分，
    #    直接将 score 设为原值以 0.5 步长合法。
    first = plan2[0]
    second = plan2[1]
    # 选择两个分值相同的题：把第一题 -0.5，第二题 +0.5，总分保持不变
    s1 = float(first["score"])
    s2 = float(second["score"])
    r = client.patch(
        f"{PREFIX}/exam-projects/plan-items/{first['id']}",
        json={"score": s1 - 0.5},
    )
    # 这步会让总分变成 49.5 —— 还必须再改一道。因此我们需要两道都改。
    # 先拿到第一道修改成功/失败。
    if r.status_code != 200:
        # 如果第一题 -0.5 导致总分非 0.5 整数倍（49.5 是 0.5 整数倍），
        # BlueprintPersistence 校验只验证 "总分是 0.5 的整数倍"，所以允许 49.5，
        # 但这里为了总分仍保持 50，我们做两题互调。先回滚思路：
        # 实际上总分 49.5 也是 0.5 整数倍，服务端会允许。但我们在断言 50 的情况下
        # 做两次调整：第一题+0.5，第二题-0.5 或反过来。
        # 这里先做一次回退：给第一题加回去。
        assert False, f"PATCH plan-item 1 返回 {r.status_code}: {r.text}"

    r2 = client.patch(
        f"{PREFIX}/exam-projects/plan-items/{second['id']}",
        json={"score": s2 + 0.5},
    )
    assert r2.status_code == 200, r2.text

    # 5. POST confirm blueprint
    r = client.post(
        f"{PREFIX}/exam-projects/{project_id}/blueprints/current/confirm",
        json={},
    )
    assert r.status_code == 200, r.text
    conf = r.json()
    assert conf["status"] == "confirmed"
    # 项目状态应为 contract
    proj = client.get(f"{PREFIX}/exam-projects/{project_id}").json()
    assert proj["status"] == "contract"
    assert proj["active_blueprint_version_id"] == bv_id

    # 6. POST contracts/allocate
    r = client.post(
        f"{PREFIX}/exam-projects/{project_id}/contracts/allocate",
        json={},
    )
    assert r.status_code == 200, r.text
    alloc = r.json()
    assert alloc["used_threshold"] <= 0.6
    snap = alloc["contract_snapshot"]
    assert isinstance(snap, dict)
    slots = snap.get("slots") or []
    assert len(slots) >= 5, f"slots 不足: {len(slots)}"

    # 7. POST contracts/confirm (slot_revisions empty)
    r = client.post(
        f"{PREFIX}/exam-projects/{project_id}/contracts/confirm",
        json={"slot_revisions": []},
    )
    assert r.status_code == 201, r.text
    conf_result = r.json()
    generation_run_id = conf_result["generation_run_id"]
    assert generation_run_id
    # 项目状态推进到 generating
    proj = client.get(f"{PREFIX}/exam-projects/{project_id}").json()
    assert proj["status"] == "generating"
    assert proj["active_generation_run_id"] == generation_run_id

    # 8. POST generate → 202 + task_run_id
    r = client.post(
        f"{PREFIX}/exam-projects/{project_id}/generate",
        json={"mock_graph": True},
    )
    assert r.status_code == 202, r.text
    task_run_id = r.json()["task_run_id"]
    assert task_run_id

    # 9. GET task-runs 最终应 succeeded
    task_status = _wait_for_task(client, task_run_id)
    assert task_status["status"] == "succeeded", (
        f"task 状态={task_status['status']} "
        f"error={task_status.get('error_message')}"
    )

    # 10. GET paper-versions/current → candidate + items 非空
    r = client.get(
        f"{PREFIX}/exam-projects/{project_id}/paper-versions/current"
    )
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["status"] == "candidate", f"paper version 状态={pv['status']}"
    pv_id = pv["id"]
    questions = pv.get("questions") or []
    assert len(questions) >= 5, f"questions 数量={len(questions)}"

    # 11. GET needs-review
    r = client.get(f"{PREFIX}/paper-versions/{pv_id}/needs-review")
    assert r.status_code == 200, r.text
    needs = r.json()
    assert isinstance(needs, list)
    # 因为 mock_graph_invoke 把第 1 道标为 needs_review，应该不为空
    assert len(needs) >= 1, f"needs_review 列表为空（第1题预期为待审）"

    # 12. PATCH paper item 1：覆写 stem + clear_needs_review
    r = client.patch(
        f"{PREFIX}/paper-versions/{pv_id}/items/1",
        json={
            "teacher_override_patch": {
                "stem": "[教师修订] 改进后的第一题题干",
            },
            "clear_needs_review": True,
        },
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert patched["needs_review"] is False or patched["needs_review"] is None, (
        f"needs_review 未清除: {patched}"
    )

    # 13. POST confirm (force=false)
    # 先看是否还有 needs_review：如果 needs_review 全部清除，应 200；否则 409
    needs_remaining = client.get(f"{PREFIX}/paper-versions/{pv_id}/needs-review").json()
    r = client.post(f"{PREFIX}/paper-versions/{pv_id}/confirm", json={"force_ignore_needs_review": False})
    if len(needs_remaining) > 0:
        assert r.status_code == 409, f"预期 409，实际 {r.status_code}: {r.text}"
        # 再试 force=true
        r2 = client.post(
            f"{PREFIX}/paper-versions/{pv_id}/confirm",
            json={"force_ignore_needs_review": True},
        )
        assert r2.status_code == 200, r2.text
    else:
        assert r.status_code == 200, f"预期 200，实际 {r.status_code}: {r.text}"

    # 14. 确认后 paper version = finalized
    r = client.get(f"{PREFIX}/exam-projects/{project_id}/paper-versions/current")
    assert r.status_code == 200
    pv_after = r.json()
    assert pv_after["status"] == "finalized", f"期望 finalized，实际 {pv_after['status']}"

    # 15. 尝试再次 PATCH 同一题 → 409
    r = client.patch(
        f"{PREFIX}/paper-versions/{pv_id}/items/1",
        json={
            "teacher_override_patch": {"stem": "should fail"},
        },
    )
    assert r.status_code == 409, f"finalized 后 patch 应 409，实际 {r.status_code}: {r.text}"
