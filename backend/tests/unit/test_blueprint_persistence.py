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
from app.domain.blueprint.models import UnitCoverage
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


def test_confirm_blueprint_keeps_status_past_contract_stage(session):
    """状态只进不退：已越过合同阶段的项目补记蓝图确认不被打回 contract。

    回归：生成完成后（status=review）经助手补记 confirm_blueprint 曾把项目
    打回 contract，助手阶梯随之倒发 start_generation 提案。
    """
    bv, _ = create_draft_blueprint(session, **_draft_params(count=10, per=10))
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == "ep1")
        .values(status="review")
    )
    session.commit()

    r = confirm_blueprint(session, course_id="c1", project_id="ep1", blueprint_version_id=bv)
    assert r["status"] == "confirmed"

    proj = session.execute(select(exam_projects).where(exam_projects.c.id == "ep1")).one()
    assert proj._mapping["status"] == "review"
    assert proj._mapping["active_blueprint_version_id"] == bv
    bp_status = session.execute(
        select(blueprint_versions.c.status).where(blueprint_versions.c.id == bv)
    ).scalar_one()
    assert bp_status == "confirmed"


# --- TR-2.2 ---

def test_update_plan_item_score_causes_total_mismatch_raises_and_rolls_back(session):
    bv_id, plan = create_draft_blueprint(session, **_draft_params(count=10, per=10))
    items = list_plan_items(session, bv_id, course_id="c1")
    assert len(items) == 10
    # 找第一个 plan_item_id
    pi_id = items[0]["id"]
    # 把 score 改成 10.3（非 0.5 步进） → 立即 raise
    with pytest.raises(BlueprintValidationError, match=r"0\.5"):
        update_plan_item(session, pi_id, {"score": 10.3}, course_id="c1")

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
        update_plan_item(session, pi_id2, {"score": 9.5}, course_id="c1")


def test_update_plan_item_assessment_mode_enum_and_question_type_canonical(session):
    """逐题换考法：5 枚举之外拒绝（API 层 422）；题型中文别名归一、
    未知题型拒绝——落库必须是 generation_graph 认识的 canonical 值。"""
    bv_id, _plan = create_draft_blueprint(session, **_draft_params(count=5, per=20))
    items = list_plan_items(session, bv_id, course_id="c1")
    pi_id = items[0]["id"]

    # 合法考法：直接落库并回读
    out = update_plan_item(session, pi_id, {"assessment_mode": "problem_solving"}, course_id="c1")
    assert out["assessment_mode"] == "problem_solving"

    # 非法考法 → BlueprintValidationError（路由映射 422）
    with pytest.raises(BlueprintValidationError, match="assessment_mode"):
        update_plan_item(session, pi_id, {"assessment_mode": "brainstorm"}, course_id="c1")

    # 中文题型别名归一为英文枚举
    out = update_plan_item(session, pi_id, {"question_type": "判断题"}, course_id="c1")
    assert out["question_type"] == "true_false"

    # 未知题型 → 拒绝，且行未被改动
    with pytest.raises(BlueprintValidationError, match="question_type"):
        update_plan_item(session, pi_id, {"question_type": "brainstorm"}, course_id="c1")
    unchanged = session.execute(
        select(plan_items.c.question_type).where(plan_items.c.id == pi_id)
    ).one()
    assert unchanged._mapping["question_type"] == "true_false"


# --- TR-2.3 ---

def test_plan_item_reads_and_writes_are_scoped_to_course(session):
    """计划项读写必须带 course_id 过滤（多租户底线）。

    回归：PATCH 路由曾漏声明 course_id，服务层转而从被操作的行里反推租户，
    跨课程传 plan_item_id 即可改到别的课程的题位。现在租户由调用方给定，
    不匹配一律按不存在处理。
    """
    bv_id, _plan = create_draft_blueprint(session, **_draft_params(count=5, per=20))
    items = list_plan_items(session, bv_id, course_id="c1")
    assert len(items) == 5
    pi_id = items[0]["id"]

    # 跨课程读：查不到
    assert list_plan_items(session, bv_id, course_id="c_other") == []

    # 跨课程写：拒绝，且原行分毫未动
    with pytest.raises(BlueprintPersistenceError, match="不存在"):
        update_plan_item(session, pi_id, {"score": 8.0}, course_id="c_other")

    unchanged = session.execute(
        select(plan_items.c.score).where(plan_items.c.id == pi_id)
    ).one()
    assert abs(float(unchanged._mapping["score"]) - 20.0) < 0.001


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
    items = list_plan_items(session, bv_id, course_id="c1")
    assert len(items) == 5
    scores = [float(it["score"]) for it in items]
    assert sum(scores) == 100
    # 检查每个 item 都有 difficulty / cognitive_level / knowledge_card_id
    for it in items:
        assert it.get("difficulty") in {"low", "medium", "high"}
        assert it.get("cognitive_level")
        assert it.get("knowledge_card_id") in {"c1a", "c1b", "c2a", "c2b"}


def _draft_params_with_comprehensive(**overrides):
    """含综合题题型的参数：卡槽归属检查需要 comprehensive 可选卡片。"""
    params = _draft_params()
    params["type_rules"] = {
        "single_choice": {"count": 8, "score": 10},
        "comprehensive": {"count": 2, "score": 10},
    }
    params["card_question_types"] = {
        cid: ["single_choice", "comprehensive"]
        for cid in params["card_question_types"]
    }
    params.update(overrides)
    return params


def test_create_draft_merges_comprehensive_archetype_pool(session):
    """教师显式原型池落进 type_rules.comprehensive.archetypes（顺序保留）。"""
    bv_id, _ = create_draft_blueprint(
        session,
        **_draft_params_with_comprehensive(
            comprehensive_archetypes=["case_analysis", "solution_design"]
        ),
    )
    row = session.execute(
        select(blueprint_versions.c.type_rules).where(
            blueprint_versions.c.id == bv_id
        )
    ).one()
    rules = row[0]
    assert rules["comprehensive"]["archetypes"] == ["case_analysis", "solution_design"]
    # 原有 count/score 不被原型池覆盖
    assert rules["comprehensive"]["count"] == 2
    assert rules["comprehensive"]["score"] == 10
    assert "archetypes" not in rules["single_choice"]


def test_create_draft_archetype_pool_guards(session):
    # 考核规则没声明综合题：无法兑现原型池 → 显式报错而非静默忽略
    no_comp = _draft_params()
    no_comp["comprehensive_archetypes"] = ["case_analysis"]
    with pytest.raises(BlueprintValidationError, match="未声明 comprehensive"):
        create_draft_blueprint(session, **no_comp)
    # 全部非法名 → 报错（默认轮换池不接黑名）
    with pytest.raises(BlueprintValidationError, match="无合法原型名"):
        create_draft_blueprint(
            session,
            **_draft_params_with_comprehensive(
                comprehensive_archetypes=["not_an_archetype"]
            ),
        )


def test_list_plan_items_resolves_exam_point_name_and_anchor(session):
    """考点名/章节按蓝图自己的框架版本解析：id 精确取名，NULL 安全。

    plan_items.exam_point_id 有外键（→ exam_points.id），落库必是 id 口径；
    code 兜底分支针对无外键的历史 JSON 快照，由
    test_enrich_contract_snapshot_code_fallback_and_ghost_ids 覆盖。
    """
    bv_id, _ = create_draft_blueprint(session, **_draft_params(count=5, per=20))
    items = list_plan_items(session, bv_id, course_id="c1")
    assert len(items) == 5
    # 分配时题位已按单元带上 exam_point_id，join 应全部解析出名字与章节
    for it in items:
        assert it["exam_point_id"] in {"au1", "au2"}
        assert it["exam_point_title"] in {"考点1", "考点2"}
        assert it["exam_point_code"] in {"EP1", "EP2"}
        assert it["anchor_key"] in {"A1", "A2"}
    assert items[0]["exam_point_title"] == "考点1"
    assert items[0]["anchor_key"] == "A1"

    # NULL 分支：清空一个题位的考点引用，字段存在但为 None（前端显示 '-'，不报错）
    update_plan_item(session, items[0]["id"], {"exam_point_id": None}, course_id="c1")
    refreshed = list_plan_items(session, bv_id, course_id="c1")
    # join 不放大行数
    assert len(refreshed) == len(items)
    assert refreshed[0].get("exam_point_title") is None
    assert refreshed[0].get("anchor_key") is None
    assert refreshed[1]["exam_point_title"] in {"考点1", "考点2"}


def test_update_plan_item_rejected_when_blueprint_not_draft(session):
    """冻结纪律：只有 draft 蓝图可原地改；确认后只能新建版本。"""
    bv_id, _ = create_draft_blueprint(session, **_draft_params(count=5, per=20))
    items = list_plan_items(session, bv_id, course_id="c1")
    pi_id = items[0]["id"]
    # draft 阶段可改（难度/分值编辑能力的正路）
    update_plan_item(session, pi_id, {"difficulty": "high"}, course_id="c1")

    confirm_blueprint(session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id)
    with pytest.raises(BlueprintPersistenceError, match="不可原地修改"):
        update_plan_item(session, pi_id, {"difficulty": "low"}, course_id="c1")
    # 被拒绝后分毫未动
    row = session.execute(
        select(plan_items.c.difficulty).where(plan_items.c.id == pi_id)
    ).one()
    assert row._mapping["difficulty"] == "high"


# --- 考试侧重点（assessment_focus）：确定性折算成各题型的考查方式分布 ---


def _set_focus(session, focus: list[dict]):
    """往 fv1 的 payload 写入考试侧重点（模拟教师在考核规则卡保存）。"""
    payload = dict(session.execute(
        select(framework_versions.c.payload).where(framework_versions.c.id == "fv1")
    ).one()._mapping["payload"] or {})
    payload["final_exam_rules"] = {"assessment_focus": focus}
    session.execute(
        framework_versions.update()
        .where(framework_versions.c.id == "fv1")
        .values(payload=payload)
    )
    session.commit()


def _stored_type_rules(session, bv_id: str) -> dict:
    return session.execute(
        select(blueprint_versions.c.type_rules).where(blueprint_versions.c.id == bv_id)
    ).one()._mapping["type_rules"]


def test_focus_practical_converges_to_zero_without_directly_assessable_unit(session):
    """设了偏实操但无可直考单元：实操确定性收敛为 0，蓝图照常生成。

    实操可考性缺省为假（考点 policy 才是权威），不能因为教师声明了实操偏好
    就让出卷失败；收敛结果随 type_rules 持久化，题位表逐题可见。
    """
    _set_focus(session, [
        {"assessment_mode": "practical_operation", "weight": 60},
        {"assessment_mode": "conceptual", "weight": 40},
    ])
    bv_id, _ = create_draft_blueprint(session, **_draft_params(count=10, per=10))
    items = list_plan_items(session, bv_id, course_id="c1")
    assert items
    # 实操全部过滤后，剩余权重归一 → 单一概念理解
    assert {it["assessment_mode"] for it in items} == {"conceptual"}
    dist = _stored_type_rules(session, bv_id)["single_choice"]["assessment_mode_distribution"]
    assert "practical_operation" not in dist
    assert abs(sum(dist.values()) - 100) < 0.01


def test_focus_fully_ineligible_leaves_engine_defaults_untouched(session):
    """侧重点全是不可考方式时一档都不注入：不改引擎默认分布（均衡兜底）。"""
    _set_focus(session, [{"assessment_mode": "practical_operation", "weight": 100}])
    bv_id, _ = create_draft_blueprint(session, **_draft_params(count=10, per=10))
    assert "assessment_mode_distribution" not in _stored_type_rules(session, bv_id)["single_choice"]
    items = list_plan_items(session, bv_id, course_id="c1")
    # single_choice 默认分布只有理论记忆与概念理解
    assert {it["assessment_mode"] for it in items} <= {"theory_recall", "conceptual"}


def test_focus_practical_assigned_when_point_directly_assessable(session):
    """考点可直考时实操按侧重点落位；超容量收敛到可直考章权重上界。

    这是实操的首次可达路径：前端 units 载荷不含 policy，靠服务按考点行回填。
    """
    session.execute(
        exam_points.update()
        .where(exam_points.c.id == "au1")
        .values(operational_detail_policy="directly_assessable")
    )
    session.commit()
    _set_focus(session, [
        {"assessment_mode": "practical_operation", "weight": 50},
        {"assessment_mode": "theory_recall", "weight": 50},
    ])
    params = _draft_params(count=10, per=10)
    params["units_payload"][0]["exam_point_id"] = "au1"
    params["units_payload"][1]["exam_point_id"] = "au2"
    bv_id, _ = create_draft_blueprint(session, **params)
    items = list_plan_items(session, bv_id, course_id="c1")

    practical = [it for it in items if it["assessment_mode"] == "practical_operation"]
    practical_score = sum(float(it["score"]) for it in practical)
    # 实操需求 50 分 > 可直考章 A1 权重 40 → 收敛到上界 40
    assert practical_score > 0
    assert practical_score <= 40
    # 实操槽位只能落进可直考章 A1（A2 只是 supporting_only）
    assert all(it["anchor_key"] == "A1" for it in practical)
    assert abs(sum(float(it["score"]) for it in items) - 100) < 0.01


def test_focus_does_not_override_explicit_mode_distribution(session):
    """教师/脚本显式下发的考查方式分布优先，侧重点不覆盖。"""
    _set_focus(session, [{"assessment_mode": "practical_operation", "weight": 100}])
    params = _draft_params(count=10, per=10)
    params["type_rules"] = {
        "single_choice": {
            "count": 10, "score": 10,
            "assessment_mode_distribution": {"theory_recall": 100},
        }
    }
    bv_id, _ = create_draft_blueprint(session, **params)
    items = list_plan_items(session, bv_id, course_id="c1")
    assert {it["assessment_mode"] for it in items} == {"theory_recall"}
    dist = _stored_type_rules(session, bv_id)["single_choice"]["assessment_mode_distribution"]
    assert dist == {"theory_recall": 100}


def test_confirm_blueprint_succeeds_with_practical_focus(session):
    """确认阶段的防御性重跑必须看到同样的实操可考性。

    回归：创建时注入过实操分布，确认时重建单元若不回填 policy，
    会撞 "practical_operation has no eligible chapter"。
    """
    session.execute(
        exam_points.update()
        .where(exam_points.c.id == "au1")
        .values(operational_detail_policy="directly_assessable")
    )
    session.commit()
    _set_focus(session, [
        {"assessment_mode": "practical_operation", "weight": 30},
        {"assessment_mode": "theory_recall", "weight": 70},
    ])
    params = _draft_params(count=10, per=10)
    params["units_payload"][0]["exam_point_id"] = "au1"
    params["units_payload"][1]["exam_point_id"] = "au2"
    bv_id, _ = create_draft_blueprint(session, **params)
    dist = _stored_type_rules(session, bv_id)["single_choice"]["assessment_mode_distribution"]
    assert dist.get("practical_operation", 0) > 0

    result = confirm_blueprint(
        session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id
    )
    assert result["status"] == "confirmed"


def test_allocation_ladder_raises_last_error_when_all_attempts_fail(session):
    """分配阶梯三级全失败时把最后的校验错误抛给教师，不静默吞错。

    10 分一道题切不平 A1=45 的章容量 → 三次尝试（注入/去实操/回原分布）
    必然全部失败，验证逐级降级的出口是抛错而不是死循环或空计划。
    """
    _set_focus(session, [{"assessment_mode": "conceptual", "weight": 100}])
    params = _draft_params(count=10, per=10)
    params["chapter_weights"] = {"A1": 45, "A2": 55}
    with pytest.raises(BlueprintValidationError, match="jointly satisfied"):
        create_draft_blueprint(session, **params)


def test_without_practical_strips_and_falls_back_to_engine_default():
    """阶梯中间退路：剔除实操并归一；只剩实操的分布交回引擎默认。"""
    rules = {
        "single_choice": {
            "count": 10, "score": 10,
            "assessment_mode_distribution": {"practical_operation": 40, "theory_recall": 60},
        },
        "short_answer": {
            "count": 5, "score": 10,
            "assessment_mode_distribution": {"practical_operation": 100},
        },
        "true_false": {"count": 5, "score": 10},
    }
    out = blueprint_persistence_service._without_practical(
        rules, {"single_choice", "short_answer"}
    )
    dist = out["single_choice"]["assessment_mode_distribution"]
    assert "practical_operation" not in dist
    assert abs(sum(dist.values()) - 100) < 0.01
    assert "assessment_mode_distribution" not in out["short_answer"]
    assert out["true_false"] is rules["true_false"]


def test_enrich_units_respects_explicit_policy_and_looks_up_by_id_or_code(session):
    """回填只补缺失：显式值尊重、查不到考点的单元保持缺省、code 兜底可匹配。"""
    units = [
        # 显式声明 → 不动
        {"unit_id": "au1", "exam_point_id": "au1", "operational_detail_policy": "forbidden"},
        # 查无此考点 → 保持缺省（引擎按 supporting_only 处理）
        {"unit_id": "ghost", "exam_point_id": "no-such-point"},
        # 只有 code（历史/脚本口径）→ 按 fv 内唯一 code 匹配；非可直考不放开实操
        {"unit_id": "u3", "exam_point_id": "EP1"},
    ]
    blueprint_persistence_service._enrich_units_with_policy(
        session, units, course_id="c1", framework_version_id="fv1"
    )
    assert units[0]["operational_detail_policy"] == "forbidden"
    assert "operational_detail_policy" not in units[1]
    assert units[2]["operational_detail_policy"] == "supporting_only"
    assert "allowed_assessment_modes" not in units[2]


def test_mode_eligible_mirrors_engine_guards():
    """可考性判定与引擎 _eligible_units 同口径（三道闸门逐一拦截）。"""
    unit = UnitCoverage(
        unit_id="u1",
        anchor_key="A1",
        card_ids=["c1"],
        allowed_assessment_modes=["theory_recall", "practical_operation"],
        operational_detail_policy="supporting_only",
    )
    # 实操在允许清单里，但政策不是可直考 → 拦
    assert not blueprint_persistence_service._mode_eligible(
        unit, mode="practical_operation", question_type="single_choice", card_question_types={}
    )
    # 同政策下理论记忆可考（政策只管实操）
    assert blueprint_persistence_service._mode_eligible(
        unit, mode="theory_recall", question_type="single_choice", card_question_types={}
    )
    # 不在允许清单的方式 → 拦
    assert not blueprint_persistence_service._mode_eligible(
        unit, mode="conceptual", question_type="single_choice", card_question_types={}
    )
    # 卡片限定题型，单元里没有卡能出该题型 → 拦
    assert not blueprint_persistence_service._mode_eligible(
        unit, mode="theory_recall", question_type="essay",
        card_question_types={"c1": ["single_choice"]},
    )
