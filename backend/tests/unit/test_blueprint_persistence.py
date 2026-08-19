"""Blueprint 持久化服务单元测试 (T2)。

独立 SQLite 引擎，手工插入最小 users/courses/framework_versions +
catalog_versions + assessment_units + knowledge_cards 数据。
"""
from __future__ import annotations

import inspect
import re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

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
)
from app.services import blueprint_persistence_service
from app.services.blueprint_persistence_service import (
    BlueprintPersistenceError,
    BlueprintValidationError,
    confirm_blueprint,
    create_draft_blueprint,
    list_plan_items,
    update_plan_item,
)


@pytest.fixture
def session(tmp_path):
    """临时 SQLite Session：含最小 course/project/framework/catalog/units/cards。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'bp.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()
        with s.begin():
            # framework version
            s.execute(framework_versions.insert().values(
                id="fv1", course_id="c1", version_no=1, status="published",
                payload={"anchors": [{"key": "A1"}, {"key": "A2"}]},
                published_at=None,
            ))
            # catalog version
            s.execute(knowledge_catalog_versions.insert().values(
                id="cv1", course_id="c1", framework_version_id="fv1",
                version_no=1, status="published", published_at=None,
            ))
            # exam_points (IDs match fallback unit IDs used by allocate_plan_items)
            s.execute(exam_points.insert(), [
                {"id": "au1", "course_id": "c1",
                 "framework_version_id": "fv1", "anchor_key": "A1",
                 "code": "EP1", "title": "考点1",
                 "assessment_requirement": "掌握概念A的定义和应用",
                 "weight_value": 40, "weight_source": "teacher_confirmed",
                 "weight_group_id": "A1", "priority": "normal",
                 "cognitive_targets": [], "assessment_orientations": [],
                 "allowed_question_types": [],
                 "operational_detail_policy": "supporting_only",
                 "scope_boundary": {}, "required_evidence_roles": [],
                 "retrieval_intent": "围绕概念A的定义与典型场景检索知识材料",
                 "teaching_anchor_keys": [],
                 "status": "active"},
                {"id": "au2", "course_id": "c1",
                 "framework_version_id": "fv1", "anchor_key": "A2",
                 "code": "EP2", "title": "考点2",
                 "assessment_requirement": "掌握概念B的定义和应用",
                 "weight_value": 60, "weight_source": "teacher_confirmed",
                 "weight_group_id": "A2", "priority": "normal",
                 "cognitive_targets": [], "assessment_orientations": [],
                 "allowed_question_types": [],
                 "operational_detail_policy": "supporting_only",
                 "scope_boundary": {}, "required_evidence_roles": [],
                 "retrieval_intent": "围绕概念B的定义与典型场景检索知识材料",
                 "teaching_anchor_keys": [],
                 "status": "active"},
            ])
            # content domains (anchors A1, A2)
            s.execute(content_domains.insert(), [
                {"id": "cd1", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A1", "code": "A1", "name": "章1", "status": "active"},
                {"id": "cd2", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A2", "code": "A2", "name": "章2", "status": "active"},
            ])
            # assessment_units (point to exam_points au1/au2)
            s.execute(assessment_units.insert(), [
                {"id": "au1", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd1", "exam_point_id": "au1",
                 "code": "U1", "title": "单元1",
                 "performance_statement": "掌握概念A", "weight": 40, "status": "active"},
                {"id": "au2", "course_id": "c1", "catalog_version_id": "cv1",
                 "content_domain_id": "cd2", "exam_point_id": "au2",
                 "code": "U2", "title": "单元2",
                 "performance_statement": "掌握概念B", "weight": 60, "status": "active"},
            ])
            # knowledge_cards (2 per unit)
            s.execute(knowledge_cards.insert(), [
                {"id": "c1a", "course_id": "c1", "catalog_version_id": "cv1",
                 "assessment_unit_id": "au1", "name": "卡1a",
                 "performance_statement": "掌握概念A细节1",
                 "assessable_content": ["概念A定义：X", "概念A特性：Y"],
                 "content_hash": "h1a", "status": "active"},
                {"id": "c1b", "course_id": "c1", "catalog_version_id": "cv1",
                 "assessment_unit_id": "au1", "name": "卡1b",
                 "performance_statement": "掌握概念A细节2",
                 "assessable_content": ["概念A应用1", "概念A应用2"],
                 "content_hash": "h1b", "status": "active"},
                {"id": "c2a", "course_id": "c1", "catalog_version_id": "cv1",
                 "assessment_unit_id": "au2", "name": "卡2a",
                 "performance_statement": "掌握概念B细节1",
                 "assessable_content": ["概念B定义：M", "概念B特性：N"],
                 "content_hash": "h2a", "status": "active"},
                {"id": "c2b", "course_id": "c1", "catalog_version_id": "cv1",
                 "assessment_unit_id": "au2", "name": "卡2b",
                 "performance_statement": "掌握概念B细节2",
                 "assessable_content": ["概念B应用1", "概念B应用2"],
                 "content_hash": "h2b", "status": "active"},
            ])
            # exam_project
            s.execute(exam_projects.insert().values(
                id="ep1", course_id="c1", name="Midterm", status="draft",
            ))
        yield s
    engine.dispose()


def _draft_params(score=100, count=10, per=10):
    return dict(
        course_id="c1",
        project_id="ep1",
        framework_version_id="fv1",
        catalog_version_id="cv1",
        type_rules={"single_choice": {"count": count, "score": per}},
        chapter_weights={"A1": 40, "A2": 60},
        units_payload=[
            {
                "unit_id": "au1", "exam_point_id": "",
                "anchor_key": "A1", "card_ids": ["c1a", "c1b"],
            },
            {
                "unit_id": "au2", "exam_point_id": "",
                "anchor_key": "A2", "card_ids": ["c2a", "c2b"],
            },
        ],
        card_semantic_profiles={
            "c1a": {"concept_cluster": "A", "answer_proposition": "A-a"},
            "c1b": {"concept_cluster": "A", "answer_proposition": "A-b"},
            "c2a": {"concept_cluster": "B", "answer_proposition": "B-a"},
            "c2b": {"concept_cluster": "B", "answer_proposition": "B-b"},
        },
        card_question_types={
            "c1a": ["single_choice"],
            "c1b": ["single_choice"],
            "c2a": ["single_choice"],
            "c2b": ["single_choice"],
        },
    )


# --- TR-2.1 ---

def test_confirm_creates_version_and_supersedes_old(session):
    # 创建第一个草稿并确认
    bv1, _ = create_draft_blueprint(session, **_draft_params(count=10, per=10))
    r1 = confirm_blueprint(session, course_id="c1", project_id="ep1", blueprint_version_id=bv1)
    assert r1["status"] == "confirmed"

    # 项目状态应为 contract
    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["status"] == "contract"
    assert proj._mapping["active_blueprint_version_id"] == bv1

    # 创建第二个草稿
    bv2, _ = create_draft_blueprint(session, **_draft_params(count=5, per=20))
    r2 = confirm_blueprint(session, course_id="c1", project_id="ep1", blueprint_version_id=bv2)
    assert r2["status"] == "confirmed"

    # 统计 status
    rows = session.execute(
        select(blueprint_versions.c.status).where(
            blueprint_versions.c.exam_project_id == "ep1",
            blueprint_versions.c.course_id == "c1",
        )
    ).all()
    statuses = [r._mapping["status"] for r in rows]
    assert statuses.count("confirmed") == 1
    assert statuses.count("superseded") == 1
    # 项目 active 指向第二个
    proj2 = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj2._mapping["active_blueprint_version_id"] == bv2
    assert proj2._mapping["status"] == "contract"


# --- TR-2.2 ---

def test_update_plan_item_score_causes_total_mismatch_raises_and_rolls_back(session):
    bv_id, plan = create_draft_blueprint(session, **_draft_params(count=10, per=10))
    items = list_plan_items(session, bv_id)
    assert len(items) == 10
    # 找第一个 plan_item_id
    pi_id = items[0]["id"]
    # 把 score 改成 10.3（非 0.5 步进） → 立即 raise
    with pytest.raises(BlueprintValidationError, match=r"0\.5"):
        update_plan_item(session, pi_id, {"score": 10.3})

    # 查 DB，原 score 仍应是 10（rollback 生效）
    refreshed = session.execute(
        select(plan_items.c.score).where(plan_items.c.id == pi_id)
    ).one()
    assert abs(float(refreshed._mapping["score"]) - 10.0) < 0.001

    # 通过 SQL 直接绕过服务把 pi_id 的 score 改成 10.7（非法 0.5 步进），
    # 之后再尝试 update 另一项时，服务的总分重校验会触发 "总分校验失败"
    pi_id2 = items[1]["id"]
    session.execute(
        plan_items.update()
        .where(plan_items.c.id == pi_id)
        .values(score=10.7)
    )
    session.commit()
    with pytest.raises(BlueprintValidationError, match=r"总分校验失败"):
        update_plan_item(session, pi_id2, {"score": 9.5})


# --- TR-2.3 ---

def test_module_only_imports_allocate_plan_items_from_blueprint_service():
    """静态检查：blueprint_persistence_service 只导入 allocate_plan_items
    与 BlueprintValidationError；不依赖蓝图引擎内部细节。"""
    source = inspect.getsource(blueprint_persistence_service)
    # 禁止直接引用 blueprint_service 的其他私有函数名
    forbidden_patterns = [
        "_largest_remainder",
        "_assign_slots_to_anchors",
        "_validated_distribution",
        "_difficulty_distribution",
        "_cognitive_level_distribution",
        "_assessment_mode_distribution",
    ]
    for pat in forbidden_patterns:
        assert pat not in source, f"不应引用蓝图引擎内部符号: {pat}"

    # 查找导入语句块：匹配 from app.services.blueprint_service import ( ... )
    # 只允许 allocate_plan_items 与 BlueprintValidationError 两个符号
    import_match = re.search(
        r"from\s+app\.services\.blueprint_service\s+import\s*\(([^)]+)\)",
        source,
        re.DOTALL,
    )
    assert import_match is not None, "未找到 blueprint_service 导入"
    imported_symbols = {
        s.strip().rstrip(",")
        for s in import_match.group(1).splitlines()
        if s.strip()
    }
    # 可能存在多行导入和别名，过滤空项
    imported_symbols = {s for s in imported_symbols if s}
    assert imported_symbols <= {"allocate_plan_items", "BlueprintValidationError"}, (
        f"blueprint_service 只允许导入 2 个符号，实际: {sorted(imported_symbols)}"
    )
    assert "allocate_plan_items" in imported_symbols
    assert "BlueprintValidationError" in imported_symbols


def test_create_draft_blueprint_persists_items_with_correct_fields(session):
    bv_id, plan = create_draft_blueprint(session, **_draft_params(count=5, per=20))
    items = list_plan_items(session, bv_id)
    assert len(items) == 5
    scores = [float(it["score"]) for it in items]
    assert sum(scores) == 100
    # 检查每个 item 都有 difficulty / cognitive_level / knowledge_card_id
    for it in items:
        assert it.get("difficulty") in {"low", "medium", "high"}
        assert it.get("cognitive_level")
        assert it.get("knowledge_card_id") in {"c1a", "c1b", "c2a", "c2b"}
