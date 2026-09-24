"""合同槽位 AI 解释服务单元测试。

镜像 tests/unit/test_ai_create_service.py 的组织方式，合同数据播种照抄
tests/unit/test_contract_execution.py 的已验证 fixture。覆盖：上下文装配
（槽位真实字段/知识卡/蓝图题位/章权重/同卷其他槽位概览、越界与缺蓝图拒绝）、
prompt 装配（真实数据进 payload + 「不得绕过」硬规则）、结果规整与校验收口、
带反馈的一次纠错重试、任务入队幂等。LLM 用同接口桩注入，不发真实请求。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, update
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
    knowledge_cards,
    knowledge_catalog_versions,
    task_runs,
)
from app.services.blueprint_persistence_service import (
    confirm_blueprint,
    create_draft_blueprint,
)
from app.services.contract_explain_service import (
    TASK_TYPE,
    ContractExplainError,
    build_explain_prompt,
    enqueue_explain,
    load_explain_context,
    normalize_result,
    run_explain,
    validate_result,
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
    engine = create_engine(f"sqlite:///{tmp_path / 'explain.db'}")
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


def _setup_confirmed_blueprint(session, *, total_items: int = 5, per_score: float = 20.0):
    """创建并确认蓝图（每单元 3 张卡 × 3 原子，5 题位无冲突），返回 bv_id。

    照抄 test_contract_execution._setup_confirmed_blueprint 的已验证参数。
    """
    card_ids = {
        "au1": ["c1a", "c1b", "c1c"],
        "au2": ["c2a", "c2b", "c2c"],
    }
    card_list = [cid for ids in card_ids.values() for cid in ids]
    card_semantic_profiles = {
        cid: {
            "concept_cluster": "A" if cid[1] == "1" else "B",
            "answer_proposition": f"{cid}-边界",
        }
        for cid in card_list
    }
    bv_id, _plan = create_draft_blueprint(
        session,
        course_id="c1",
        project_id="ep1",
        framework_version_id="fv1",
        catalog_version_id="cv1",
        type_rules={"single_choice": {"count": total_items, "score": per_score}},
        chapter_weights={"A1": 40, "A2": 60},
        units_payload=[
            {"unit_id": "au1", "exam_point_id": "", "anchor_key": "A1", "card_ids": card_ids["au1"]},
            {"unit_id": "au2", "exam_point_id": "", "anchor_key": "A2", "card_ids": card_ids["au2"]},
        ],
        card_semantic_profiles=card_semantic_profiles,
        card_question_types={cid: ["single_choice"] for cid in card_list},
    )
    confirm_blueprint(
        session, course_id="c1", project_id="ep1", blueprint_version_id=bv_id
    )
    return bv_id


def _ctx(session, *, bv_id: str, item_index: int = 1, allocation_seed: int | None = None) -> dict:
    return load_explain_context(
        session,
        course_id="c1",
        project_id="ep1",
        item_index=item_index,
        allocation_seed=allocation_seed,
        blueprint_version_id=bv_id,
    )


class StubClient:
    """LLMJsonClient 同接口桩：按序返回预置响应并记录每次调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "payload": payload})
        return self._responses.pop(0)


# ─── 上下文装配 ───


def test_load_context_returns_slot_card_plan_and_other_slots(session):
    bv_id = _setup_confirmed_blueprint(session)
    ctx = load_explain_context(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=None, blueprint_version_id=bv_id,
    )

    # 目标槽位：确定性分配的真实产出（字段全集来自 allocate 只读重算）
    slot = ctx["slot"]
    assert slot["item_index"] == 1
    assert slot["question_type"] == "single_choice"
    assert slot["coverage_atom"] in {
        "A-原子1", "A-原子2", "A-原子3", "A-原子4", "A-原子5", "A-原子6",
        "A-原子7", "A-原子8", "A-原子9",
        "B-原子1", "B-原子2", "B-原子3", "B-原子4", "B-原子5", "B-原子6",
        "B-原子7", "B-原子8", "B-原子9",
    }
    assert slot["answer_boundary"] is not None
    assert isinstance(slot["forbidden_context"], dict)
    assert slot["difficulty"] in {"low", "medium", "high"}
    assert slot["cognitive_level"]
    assert float(slot["score"]) == 20.0

    # 关联知识卡 grounding（带 course_id）
    card = ctx["card"]
    assert card["name"] in {"卡1a", "卡1b", "卡1c", "卡2a", "卡2b", "卡2c"}
    assert card["assessable_content"]
    assert card["answer_proposition"]

    # 蓝图该题位信息 + 章与章权重
    assert ctx["plan_item"]["score"] == 20.0
    assert ctx["chapter_weights"] == {"A1": 40, "A2": 60}
    assert ctx["chapter"]["anchor_key"] in {"A1", "A2"}
    assert ctx["chapter"]["name"] in {"章1", "章2"}
    assert ctx["allocation"]["centrality_threshold_used"] in {0.6, 0.5, 0.45}

    # 同卷其他槽位紧凑概览：4 条、不含目标题位、原子截断 20 字
    others = ctx["other_slots"]
    assert len(others) == 4
    assert all(o["item_index"] != 1 for o in others)
    for o in others:
        assert set(o) == {"item_index", "question_type", "chapter", "coverage_atom"}
        assert o["question_type"] == "single_choice"
        assert o["chapter"] in {"章1", "章2"}
        assert len(o["coverage_atom"]) <= 20


def test_load_context_rejects_unknown_item_index(session):
    bv_id = _setup_confirmed_blueprint(session)
    with pytest.raises(ContractExplainError, match="不在"):
        load_explain_context(
            session, course_id="c1", project_id="ep1", item_index=99,
            allocation_seed=None, blueprint_version_id=bv_id,
        )


def test_load_context_rejects_missing_project(session):
    bv_id = _setup_confirmed_blueprint(session)
    with pytest.raises(ContractExplainError, match="不存在"):
        load_explain_context(
            session, course_id="c1", project_id="proj-x", item_index=1,
            allocation_seed=None, blueprint_version_id=bv_id,
        )


def test_load_context_rejects_missing_blueprint(session):
    # 无蓝图（或蓝图不属于课程）→ 拒绝，端点侧映射为 409/404
    with pytest.raises(ContractExplainError, match="不存在"):
        load_explain_context(
            session, course_id="c1", project_id="ep1", item_index=1,
            allocation_seed=None, blueprint_version_id="bv-x",
        )


def test_load_context_rejects_foreign_course_project(session):
    bv_id = _setup_confirmed_blueprint(session)
    with pytest.raises(ContractExplainError, match="不存在"):
        load_explain_context(
            session, course_id="other-course", project_id="ep1", item_index=1,
            allocation_seed=None, blueprint_version_id=bv_id,
        )


# ─── prompt 装配 ───


def test_build_prompt_grounded_in_real_slot_data(session):
    ctx = _ctx(session, bv_id=_setup_confirmed_blueprint(session))
    system, payload = build_explain_prompt(ctx, "为什么不是考另一个原子")
    assert payload["instruction"] == "为什么不是考另一个原子"

    # 槽位真实字段（覆盖原子/答案域/禁用上下文/难度/认知层级/分值）原样进 prompt
    slot = payload["slot"]
    assert slot["coverage_atom"] == ctx["slot"]["coverage_atom"]
    assert slot["answer_boundary"] == ctx["slot"]["answer_boundary"]
    assert slot["forbidden_context"] == ctx["slot"]["forbidden_context"]
    assert slot["difficulty"] == ctx["slot"]["difficulty"]
    assert slot["cognitive_level"] == ctx["slot"]["cognitive_level"]
    assert slot["score"] == ctx["slot"]["score"]

    # 知识卡 + 蓝图题位 + 章权重 + 同卷其他槽位概览
    assert payload["knowledge_card"]["name"] == ctx["card"]["name"]
    assert payload["knowledge_card"]["assessable_content"] == ctx["card"]["assessable_content"]
    assert payload["knowledge_card"]["answer_proposition"] == ctx["card"]["answer_proposition"]
    assert payload["plan_item"]["score"] == 20.0
    assert payload["chapter_weights"] == {"A1": 40, "A2": 60}
    assert len(payload["other_slots"]) == 4

    # 系统提示词硬规则：只解读给定数据 / 不编造 / 不重做分配 / 不得绕过全局约束 /
    # 建议只能走合同修订或换方案重跑
    assert "不得编造" in system
    assert "不要重新做分配" in system
    assert "绕过" in system
    assert "合同修订" in system
    assert "重新运行分配算法" in system
    assert "previous_validation_error" not in payload


def test_build_prompt_carries_previous_error_on_retry(session):
    ctx = _ctx(session, bv_id=_setup_confirmed_blueprint(session))
    _, payload = build_explain_prompt(ctx, "", previous_error="解释正文不足 20 字")
    assert payload["previous_validation_error"] == "解释正文不足 20 字"


# ─── 结果规整 ───


def test_normalize_rejects_non_object():
    with pytest.raises(ContractExplainError):
        normalize_result(["not", "an", "object"], {"slot": {"item_index": 1}})


def test_normalize_converges_malformed_raw():
    ctx = {"slot": {"item_index": 1}, "other_slots": [{"item_index": 2}]}
    out = normalize_result(
        {"explanation": "  这是一段解释  ", "suggestions": "oops", "instruction_response": 42},
        ctx,
    )
    assert out["explanation"] == "这是一段解释"
    assert out["suggestions"] == []  # 非 list 置空
    assert out["instruction_response"] == "42"


def test_normalize_drops_invalid_suggestion_entries():
    ctx = {"slot": {"item_index": 1}, "other_slots": [{"item_index": 2}]}
    out = normalize_result(
        {
            "explanation": "解释" * 30,
            "suggestions": [
                "字符串项被丢弃",
                {"concern": "缺 suggestion 文本被丢弃"},
                {"concern": "想换原子", "suggestion": "走合同修订预览换考查原子", "target_item_index": 999},
                {"concern": "想换方案", "suggestion": "切换分配方案重新分配", "target_item_index": 2},
                {"concern": "bool 指向", "suggestion": "调整蓝图后重跑", "target_item_index": True},
            ],
            "instruction_response": "",
        },
        ctx,
    )
    assert out["suggestions"] == [
        # 不在给定槽位题位号里的 target_item_index 归 null，不放行臆测指向
        {"concern": "想换原子", "suggestion": "走合同修订预览换考查原子", "target_item_index": None},
        {"concern": "想换方案", "suggestion": "切换分配方案重新分配", "target_item_index": 2},
        {"concern": "bool 指向", "suggestion": "调整蓝图后重跑", "target_item_index": None},
    ]


# ─── 校验收口 ───


def test_validate_blocks_empty_explanation():
    result = validate_result({"explanation": "", "suggestions": []})
    assert result["passed"] is False
    assert result["code"] == "explanation_too_short"


def test_validate_blocks_short_explanation():
    result = validate_result({"explanation": "太短了", "suggestions": []})
    assert result["passed"] is False
    assert result["code"] == "explanation_too_short"


def test_validate_blocks_suggestion_without_text():
    result = validate_result({
        "explanation": "这是一段足够长的解释正文。" * 2,
        "suggestions": [{"concern": "c", "suggestion": ""}],
    })
    assert result["passed"] is False
    assert result["code"] == "suggestion_missing"


def test_validate_passes_good_result():
    result = validate_result({
        "explanation": "这是一段足够长的解释正文。" * 2,
        "suggestions": [{"concern": "c", "suggestion": "走合同修订", "target_item_index": None}],
    })
    assert result["passed"] is True


# ─── 解释执行（桩客户端）───


def _raw_good() -> dict:
    return {
        "explanation": (
            "本题位分到这个考查原子，是确定性分配算法在同章答案域互斥与全卷不重复约束"
            "下选出的结果。蓝图在本章的权重决定了这里安排题位，知识卡的可考查内容划定原子范围。"
        ),
        "suggestions": [
            {"concern": "想考另一个原子", "suggestion": "走合同修订预览换考查原子，确认后重新生成",
             "target_item_index": 1},
        ],
        "instruction_response": "因为同章答案域需要互斥",
    }


def _raw_bad() -> dict:
    return {"explanation": "短", "suggestions": [], "instruction_response": ""}


def test_run_explain_retries_with_validation_feedback(session):
    bv_id = _setup_confirmed_blueprint(session)
    client = StubClient([_raw_bad(), _raw_good()])
    result = run_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=None, blueprint_version_id=bv_id,
        instruction="为什么考这个原子", client=client,
    )
    assert len(client.calls) == 2
    assert "previous_validation_error" in client.calls[1]["payload"]
    # 首次调用的 payload 必须带真实槽位字段、知识卡与同卷其他槽位概览
    first = client.calls[0]["payload"]
    assert first["slot"]["coverage_atom"]
    assert first["knowledge_card"]["name"]
    assert len(first["other_slots"]) == 4
    assert first["instruction"] == "为什么考这个原子"
    # 结果载荷形状与 spec 一致（7 个键，validated 为布尔）
    assert set(result.keys()) == {
        "project_id", "item_index", "instruction",
        "explanation", "suggestions", "instruction_response", "validated",
    }
    assert result["validated"] is True
    assert result["project_id"] == "ep1"
    assert result["item_index"] == 1
    assert result["suggestions"][0]["target_item_index"] == 1


def test_run_explain_reports_failure_when_retry_still_blocks(session):
    bv_id = _setup_confirmed_blueprint(session)
    client = StubClient([_raw_bad(), _raw_bad()])
    result = run_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=None, blueprint_version_id=bv_id,
        instruction="", client=client,
    )
    assert len(client.calls) == 2
    assert result["validated"] is False
    assert result["explanation"] == "短"


def test_run_explain_single_shot_when_first_passes(session):
    bv_id = _setup_confirmed_blueprint(session)
    client = StubClient([_raw_good()])
    result = run_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=2, blueprint_version_id=bv_id,
        instruction="", client=client,
    )
    assert len(client.calls) == 1
    assert result["validated"] is True
    assert result["instruction"] == ""
    assert result["instruction_response"] == "因为同章答案域需要互斥"
    assert client.calls[0]["payload"]["allocation"]["allocation_seed"] == 2


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    bv_id = _setup_confirmed_blueprint(session)
    task_id = enqueue_explain(
        session, course_id="c1", project_id="ep1", item_index=2,
        allocation_seed=1, blueprint_version_id=bv_id, instruction="为什么",
    )
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload == {
        "course_id": "c1",
        "project_id": "ep1",
        "item_index": 2,
        "allocation_seed": 1,
        "blueprint_version_id": bv_id,
        "instruction": "为什么",
    }


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    bv_id = _setup_confirmed_blueprint(session)
    first = enqueue_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=0, blueprint_version_id=bv_id, instruction="为什么",
    )
    again = enqueue_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=0, blueprint_version_id=bv_id, instruction="为什么",
    )
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs).where(task_runs.c.id == first).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=0, blueprint_version_id=bv_id, instruction="为什么",
    )
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_allows_empty_instruction(session):
    # instruction 可空：空追问即标准解释，不拒绝
    bv_id = _setup_confirmed_blueprint(session)
    task_id = enqueue_explain(
        session, course_id="c1", project_id="ep1", item_index=1,
        allocation_seed=None, blueprint_version_id=bv_id, instruction="   ",
    )
    row = session.execute(
        select(task_runs.c.payload).where(task_runs.c.id == task_id)
    ).one()
    assert row.payload["instruction"] == ""


def test_enqueue_rejects_unknown_item_index(session):
    bv_id = _setup_confirmed_blueprint(session)
    with pytest.raises(ContractExplainError, match="不在"):
        enqueue_explain(
            session, course_id="c1", project_id="ep1", item_index=99,
            allocation_seed=None, blueprint_version_id=bv_id, instruction="",
        )


def test_enqueue_rejects_missing_project(session):
    bv_id = _setup_confirmed_blueprint(session)
    with pytest.raises(ContractExplainError, match="不存在"):
        enqueue_explain(
            session, course_id="c1", project_id="proj-x", item_index=1,
            allocation_seed=None, blueprint_version_id=bv_id, instruction="",
        )


def test_enqueue_rejects_missing_blueprint(session):
    # 无蓝图可分配：在建任务之前就拒绝（端点侧映射 409/404）
    with pytest.raises(ContractExplainError, match="不存在"):
        enqueue_explain(
            session, course_id="c1", project_id="ep1", item_index=1,
            allocation_seed=None, blueprint_version_id="bv-x", instruction="",
        )
