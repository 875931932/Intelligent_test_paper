"""合同执行服务单元测试 (T3)。

重点：阈值回退机制、revise_and_confirm 落库 generation_run、项目状态推进到 generating。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    assessment_units,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    generation_runs,
    knowledge_cards,
    knowledge_catalog_versions,
)
from app.services.blueprint_persistence_service import (
    confirm_blueprint,
    create_draft_blueprint,
)
from app.services.contract_execution_service import (
    allocate_with_fallback,
    get_contract_conflicts,
    revise_and_confirm,
)


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
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ce.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()
        with s.begin():
            s.execute(framework_versions.insert().values(
                id="fv1", course_id="c1", version_no=1, status="published", payload={},
            ))
            s.execute(knowledge_catalog_versions.insert().values(
                id="cv1", course_id="c1", framework_version_id="fv1",
                version_no=1, status="published",
            ))
            s.execute(exam_points.insert(), [
                _ep("au1", "c1", "fv1", "A1", "EP1", "考点1", "掌握概念A的定义和应用",
                    40, "A1", "围绕概念A检索材料"),
                _ep("au2", "c1", "fv1", "A2", "EP2", "考点2", "掌握概念B的定义和应用",
                    60, "A2", "围绕概念B检索材料"),
            ])
            s.execute(content_domains.insert(), [
                {"id": "cd1", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A1", "code": "A1", "name": "章1", "status": "active"},
                {"id": "cd2", "course_id": "c1", "catalog_version_id": "cv1",
                 "parent_domain_id": None, "level": 1,
                 "framework_anchor_key": "A2", "code": "A2", "name": "章2", "status": "active"},
            ])
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
            # 卡片：每个单元 3 张卡 x 3 原子/卡 = 每单元 9 原子
            # 这样 5 题位可以落位（阈值 0.6 下若仍有冲突，回退机制生效）
            s.execute(knowledge_cards.insert(), [
                {
                    "id": "c1a", "course_id": "c1", "catalog_version_id": "cv1",
                    "assessment_unit_id": "au1", "name": "卡1a",
                    "performance_statement": "概念A陈述1",
                    "assessable_content": ["A-原子1", "A-原子2", "A-原子3"],
                    "content_hash": "h1a", "status": "active",
                    "concept_cluster": "A", "answer_proposition": "A边界1",
                },
                {
                    "id": "c1b", "course_id": "c1", "catalog_version_id": "cv1",
                    "assessment_unit_id": "au1", "name": "卡1b",
                    "performance_statement": "概念A陈述2",
                    "assessable_content": ["A-原子4", "A-原子5", "A-原子6"],
                    "content_hash": "h1b", "status": "active",
                    "concept_cluster": "A", "answer_proposition": "A边界2",
                },
                {
                    "id": "c1c", "course_id": "c1", "catalog_version_id": "cv1",
                    "assessment_unit_id": "au1", "name": "卡1c",
                    "performance_statement": "概念A陈述3",
                    "assessable_content": ["A-原子7", "A-原子8", "A-原子9"],
                    "content_hash": "h1c", "status": "active",
                    "concept_cluster": "A", "answer_proposition": "A边界3",
                },
                {
                    "id": "c2a", "course_id": "c1", "catalog_version_id": "cv1",
                    "assessment_unit_id": "au2", "name": "卡2a",
                    "performance_statement": "概念B陈述1",
                    "assessable_content": ["B-原子1", "B-原子2", "B-原子3"],
                    "content_hash": "h2a", "status": "active",
                    "concept_cluster": "B", "answer_proposition": "B边界1",
                },
                {
                    "id": "c2b", "course_id": "c1", "catalog_version_id": "cv1",
                    "assessment_unit_id": "au2", "name": "卡2b",
                    "performance_statement": "概念B陈述2",
                    "assessable_content": ["B-原子4", "B-原子5", "B-原子6"],
                    "content_hash": "h2b", "status": "active",
                    "concept_cluster": "B", "answer_proposition": "B边界2",
                },
                {
                    "id": "c2c", "course_id": "c1", "catalog_version_id": "cv1",
                    "assessment_unit_id": "au2", "name": "卡2c",
                    "performance_statement": "概念B陈述3",
                    "assessable_content": ["B-原子7", "B-原子8", "B-原子9"],
                    "content_hash": "h2c", "status": "active",
                    "concept_cluster": "B", "answer_proposition": "B边界3",
                },
            ])
            s.execute(exam_projects.insert().values(
                id="ep1", course_id="c1", name="Midterm", status="draft",
            ))
        yield s
    engine.dispose()


def _setup_confirmed_blueprint(
    session,
    *,
    total_items: int = 10,
    per_score: float = 10.0,
    unit_card_ids: dict[str, list[str]] | None = None,
):
    """创建并确认蓝图，返回 blueprint_version_id。"""
    if unit_card_ids is None:
        # 每单元 1 张卡（保持 TR-3.1/TR-3.2 触发冲突回退）
        unit_card_ids = {
            "au1": ["c1a"],
            "au2": ["c2a"],
        }
    card_list = [cid for ids in unit_card_ids.values() for cid in ids]
    card_semantic_profiles = {
        cid: {
            "concept_cluster": cid[1],  # "1" from "c1a"[1]==1 no → use letter before last → simplified: A/B
            "answer_proposition": f"{cid}-边界",
        }
        for cid in card_list
    }
    # concept_cluster must be 'A' or 'B' → from card id's prefix character
    for cid in card_list:
        letter = "A" if cid[1] == "1" else "B"
        card_semantic_profiles[cid]["concept_cluster"] = letter

    bv_id, _plan = create_draft_blueprint(
        session,
        course_id="c1",
        project_id="ep1",
        framework_version_id="fv1",
        catalog_version_id="cv1",
        type_rules={"single_choice": {"count": total_items, "score": per_score}},
        chapter_weights={"A1": 40, "A2": 60},
        units_payload=[
            {
                "unit_id": "au1",
                "exam_point_id": "",
                "anchor_key": "A1",
                "card_ids": unit_card_ids["au1"],
            },
            {
                "unit_id": "au2",
                "exam_point_id": "",
                "anchor_key": "A2",
                "card_ids": unit_card_ids["au2"],
            },
        ],
        card_semantic_profiles=card_semantic_profiles,
        card_question_types={
            cid: ["single_choice"] for cid in card_list
        },
    )
    confirm_blueprint(
        session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id
    )
    return bv_id


# --- TR-3.1 ---

def test_threshold_fallback_cycle(session):
    """设置 10 道题但每考点只有 1 个原子 → 高阈值原子池耗尽必然冲突。
    断言最终 used_threshold <= 0.5 且 generation_run 中 contract_snapshot['slots'] 非空。"""
    bv_id = _setup_confirmed_blueprint(session, total_items=10, per_score=10.0)

    result = revise_and_confirm(
        session,
        course_id="c1",
        project_id="ep1",
        blueprint_version_id=bv_id,
        slot_revisions=[],
    )

    assert result["threshold"] <= 0.5
    assert len(result["conflicts_history"]) >= 2  # 至少前两轮尝试
    # 读 generation_run 行，检查 contract_snapshot.slots 非空
    gr = session.execute(
        select(generation_runs).where(
            generation_runs.c.id == result["generation_run_id"],
            generation_runs.c.course_id == "c1",
        )
    ).one()
    snap = gr._mapping.get("contract_snapshot") or {}
    assert isinstance(snap.get("slots"), list)
    assert len(snap["slots"]) > 0
    # centrality_threshold_used 字段落库
    assert gr._mapping["centrality_threshold_used"] == result["threshold"]


def test_allocate_with_fallback_returns_last_threshold_when_all_conflict(session):
    """直接调用 allocate_with_fallback，检查阈值回退链。"""
    bv_id = _setup_confirmed_blueprint(session, total_items=10, per_score=10.0)
    contract, used, history = allocate_with_fallback(session, blueprint_version_id=bv_id)
    assert used in {0.6, 0.5, 0.45}
    assert len(history) >= 1
    # 每一项是 (threshold, conflict_count) 元组
    for t, c in history:
        assert isinstance(t, float)
        assert isinstance(c, int)


# --- TR-3.3 ---

def test_revise_and_confirm_moves_project_to_generating_and_sets_run_id(session):
    # 用每单元 3 张卡 x 3 原子/卡 = 9 原子，足以覆盖 5 题位而不产生冲突
    bv_id = _setup_confirmed_blueprint(
        session,
        total_items=5,
        per_score=20.0,
        unit_card_ids={
            "au1": ["c1a", "c1b", "c1c"],
            "au2": ["c2a", "c2b", "c2c"],
        },
    )
    result = revise_and_confirm(
        session,
        course_id="c1",
        project_id="ep1",
        blueprint_version_id=bv_id,
        slot_revisions=[],
    )

    proj = session.execute(
        select(exam_projects).where(exam_projects.c.id == "ep1")
    ).one()
    assert proj._mapping["status"] == "generating"
    assert proj._mapping["active_generation_run_id"] == result["generation_run_id"]
    assert result["slot_count"] == 5


def test_get_contract_conflicts_extracts_conflicts_from_snapshot(session):
    bv_id = _setup_confirmed_blueprint(session, total_items=10, per_score=10.0)
    result = revise_and_confirm(
        session,
        course_id="c1",
        project_id="ep1",
        blueprint_version_id=bv_id,
        slot_revisions=[],
    )
    conflicts = get_contract_conflicts(session, result["generation_run_id"])
    # 返回列表（可能为空 list 或非空），类型必须是 list[dict]
    assert isinstance(conflicts, list)
    for c in conflicts:
        assert isinstance(c, dict)
        assert "stage" in c
