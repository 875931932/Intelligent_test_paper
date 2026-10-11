"""单题 AI 改题服务单元测试。

覆盖：上下文装配（合同槽位/知识卡进 prompt）、提案形态规整、
校验收口（blocker 不可确认）、带反馈的一次纠错重试、任务入队幂等
与状态门禁。LLM 用同接口桩注入，不发真实请求。
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
    generated_questions,
    generation_runs,
    knowledge_cards,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
    blueprint_versions,
)
from app.services.ai_revise_service import (
    AiReviseConflict,
    AiReviseError,
    TASK_TYPE,
    build_revise_prompt,
    enqueue_ai_revise,
    execute_ai_revise_task,
    list_revise_targets,
    load_revise_context,
    normalize_proposal,
    resolve_revise_targets,
    run_ai_revise,
    validate_proposal,
)

_GOOD_OPTIONS = {"A": "同步训练需要交换梯度", "B": "推理阶段必须同步", "C": "与批大小完全无关", "D": "只影响内存占用"}

_SLOT = {
    "item_index": 1,
    "question_type": "single_choice",
    "score": 2.0,
    "difficulty": "medium",
    "cognitive_level": "understand",
    "assessment_mode": "conceptual",
    "exam_point_id": "ep1",
    "anchor_key": "A1",
    "unit_id": "au1",
    "card_id": "kc1",
    "coverage_atom": "梯度同步会引入通信开销",
    "answer_boundary": "围绕梯度同步的开销与作用",
    "forbidden_context": {"atoms": ["梯度压缩可降低开销"], "answer_cores": ["压缩"]},
    "performance_statement": "掌握同步训练的通信开销",
    "preferred_terms": ["梯度同步"],
}

_PAYLOAD = {
    "stem": "关于分布式训练中的梯度同步，下列说法正确的是？",
    "options": dict(_GOOD_OPTIONS),
    "answer": "A",
    "explanation": "同步梯度需要通信，因此引入开销。",
    "question_type": "single_choice",
    "difficulty": "medium",
    "cognitive_level": "understand",
    "score": 2.0,
    "exam_point_id": "ep1",
}


class StubClient:
    """LLMJsonClient 同接口桩：按序返回预置响应并记录每次调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "payload": payload})
        return self._responses.pop(0)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ai_revise.db'}")
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
            s.execute(exam_points.insert().values(
                id="ep1", course_id="c1", framework_version_id="fv1", anchor_key="A1",
                code="EP1", title="考点1", assessment_requirement="掌握A", weight_value=30.0,
                weight_source="teacher_confirmed", weight_group_id="A1", priority="normal",
                cognitive_targets=[], assessment_orientations=[], allowed_question_types=[],
                operational_detail_policy="supporting_only", scope_boundary={},
                required_evidence_roles=[], retrieval_intent="围绕A检索",
                teaching_anchor_keys=[], status="active",
            ))
            s.execute(content_domains.insert().values(
                id="cd1", course_id="c1", catalog_version_id="cv1", parent_domain_id=None,
                level=1, framework_anchor_key="A1", code="A1", name="章1", status="active",
            ))
            s.execute(assessment_units.insert().values(
                id="au1", course_id="c1", catalog_version_id="cv1", content_domain_id="cd1",
                exam_point_id="ep1", code="U1", title="单元1", performance_statement="ps1",
                weight=30, status="active",
            ))
            s.execute(knowledge_cards.insert().values(
                id="kc1", course_id="c1", catalog_version_id="cv1", assessment_unit_id="au1",
                name="卡A1", performance_statement="掌握A1",
                assessable_content=["A1-原子1定义", "A1-原子2应用"],
                content_hash="hca1", status="active", concept_cluster="A",
                answer_proposition="A1-边界",
            ))
            s.execute(exam_projects.insert().values(
                id="proj1", course_id="c1", name="Midterm", status="draft",
            ))
            s.execute(blueprint_versions.insert().values(
                id="bv1", course_id="c1", exam_project_id="proj1",
                framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
            ))
            s.execute(generation_runs.insert().values(
                id="gr1", course_id="c1", framework_version_id="fv1",
                catalog_version_id="cv1", blueprint_version_id="bv1",
                prompt_template_version="v1", run_type="full", status="completed",
                contract_snapshot={"slots": [_SLOT]},
            ))
            s.execute(plan_items.insert().values(
                id="pi1", course_id="c1", blueprint_version_id="bv1",
                assessment_unit_id="au1", question_type="single_choice", item_index=1,
                score=2.0, difficulty="medium", cognitive_level="understand",
                exam_point_id="ep1", knowledge_card_id="kc1",
            ))
            s.execute(generated_questions.insert().values(
                id="gq1", course_id="c1", generation_run_id="gr1", plan_item_id="pi1",
                knowledge_card_id="kc1", revision_no=1, status="candidate",
                payload=dict(_PAYLOAD),
            ))
            s.execute(paper_versions.insert().values(
                id="pv1", course_id="c1", exam_project_id="proj1",
                generation_run_id="gr1", version_no=1, status="candidate",
            ))
            s.execute(paper_items.insert().values(
                id="item1", course_id="c1", paper_version_id="pv1",
                generated_question_id="gq1", display_order=1,
            ))
        yield s
    engine.dispose()


def _ctx(session) -> dict:
    return load_revise_context(
        session, course_id="c1", paper_version_id="pv1", item_index=1
    )


# ─── 上下文与 prompt ───


def test_load_context_includes_contract_slot_and_card(session):
    ctx = _ctx(session)
    assert ctx["current"]["stem"] == _PAYLOAD["stem"]
    assert ctx["slot"]["coverage_atom"] == _SLOT["coverage_atom"]
    assert ctx["card"]["name"] == "卡A1"


def test_build_prompt_grounded_in_contract(session):
    ctx = _ctx(session)
    system, payload = build_revise_prompt(ctx, "把选项改得更平行")
    assert payload["instruction"] == "把选项改得更平行"
    assert payload["contract"]["coverage_atom"] == _SLOT["coverage_atom"]
    assert payload["contract"]["forbidden_context"]["atoms"] == ["梯度压缩可降低开销"]
    assert payload["knowledge_card"]["name"] == "卡A1"
    assert "禁用上下文" in system
    # 未纠错时不应带上次错误字段
    assert "previous_validation_error" not in payload


def test_build_prompt_carries_previous_error_on_retry(session):
    ctx = _ctx(session)
    _, payload = build_revise_prompt(ctx, "改", previous_error="单选题必须有四个选项")
    assert payload["previous_validation_error"] == "单选题必须有四个选项"


# ─── 形态规整 ───


def test_normalize_aligns_dict_answer_to_bool():
    current = {
        "question_type": "true_false", "stem": "原题干", "options": None,
        "answer": True, "explanation": "原解析", "difficulty": "medium",
    }
    proposal = normalize_proposal(
        {"stem": "新题干", "answer": "正确"}, current=current
    )
    assert proposal["answer"] is True
    assert proposal["stem"] == "新题干"
    # 未改动字段回填当前值
    assert proposal["explanation"] == "原解析"


def test_normalize_aligns_options_dict_to_list():
    current = {
        "question_type": "single_choice", "stem": "s", "options": ["甲", "乙", "丙", "丁"],
        "answer": "A", "explanation": None, "difficulty": "medium",
    }
    proposal = normalize_proposal(
        {"stem": "s", "options": {"A": "x", "B": "y", "C": "z", "D": "w"}, "answer": "B",
         "explanation": None},
        current=current,
    )
    assert proposal["options"] == ["x", "y", "z", "w"]
    assert proposal["answer"] == "B"


def test_normalize_rejects_non_object():
    with pytest.raises(AiReviseError):
        normalize_proposal(["not", "an", "object"], current={"stem": "s"})


# ─── 校验收口 ───


def test_validate_blocks_single_choice_with_three_options(session):
    current = _ctx(session)["current"]
    bad = {"stem": "改后的题干", "options": {"A": "1", "B": "2", "C": "3"},
           "answer": "A", "explanation": "e"}
    result = validate_proposal(bad, current=current, atom_text="梯度同步会引入通信开销")
    assert result["passed"] is False
    assert result["code"] == "single_choice_schema"


def test_validate_blocks_source_language(session):
    current = _ctx(session)["current"]
    bad = {"stem": "根据课件第3章，下列说法正确的是？",
           "options": dict(_GOOD_OPTIONS), "answer": "A", "explanation": "e"}
    result = validate_proposal(bad, current=current)
    assert result["passed"] is False
    assert result["code"] == "source_language"


def test_validate_passes_good_proposal(session):
    current = _ctx(session)["current"]
    good = {"stem": "改后仍规范的题干？", "options": dict(_GOOD_OPTIONS),
            "answer": "B", "explanation": "改后解析"}
    result = validate_proposal(good, current=current, atom_text="梯度同步会引入通信开销")
    assert result["passed"] is True


# ─── 提案执行（桩客户端）───


def _raw_good() -> dict:
    return {
        "stem": "关于同步梯度训练，下列哪一项描述是正确的？",
        "options": dict(_GOOD_OPTIONS),
        "answer": "B",
        "explanation": "改后解析：通信开销来自梯度同步。",
        "change_summary": "换种问法，考点不变。",
    }


def _raw_bad_choice() -> dict:
    return {
        "stem": "新题干？",
        "options": {"A": "1", "B": "2", "C": "3"},
        "answer": "A",
        "explanation": "e",
        "change_summary": "x",
    }


def test_run_ai_revise_retries_with_validation_feedback(session):
    client = StubClient([_raw_bad_choice(), _raw_good()])
    result = run_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="重写选项", client=client,
    )
    assert len(client.calls) == 2
    assert "previous_validation_error" in client.calls[1]["payload"]
    assert result["attempts"] == 2
    assert result["validation"]["passed"] is True
    assert result["proposal"]["answer"] == "B"
    assert result["item_index"] == 1
    # current 必须回传，前端 diff 的“原题侧”依赖它
    assert result["current"]["stem"] == _PAYLOAD["stem"]


def test_run_ai_revise_reports_failure_when_retry_still_blocks(session):
    client = StubClient([_raw_bad_choice(), _raw_bad_choice()])
    result = run_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="随便改", client=client,
    )
    assert result["attempts"] == 2
    assert result["validation"]["passed"] is False
    assert result["validation"]["code"] == "single_choice_schema"


def test_run_ai_revise_single_shot_when_first_passes(session):
    client = StubClient([_raw_good()])
    result = run_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="优化表述", client=client,
    )
    assert len(client.calls) == 1
    assert result["attempts"] == 1
    assert result["validation"]["passed"] is True


def test_execute_ai_revise_task_pins_low_reasoning_effort(session, monkeypatch):
    """worker 入口思考档显式钉 low（不依赖全局 llm_disable_thinking 的缺省档）。"""
    from app.config import settings

    monkeypatch.setattr("app.services.ai_revise_service.llm_configured", lambda: True)
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def request_json(self, **kwargs):
            return _raw_good()

    monkeypatch.setattr("app.adapters.model.llm_gateway.LLMJsonClient", FakeClient)
    result = execute_ai_revise_task(
        session,
        payload={
            "course_id": "c1", "paper_version_id": "pv1",
            "item_index": 1, "instruction": "重写选项",
        },
    )
    assert result["validation"]["passed"] is True
    assert captured["reasoning_effort"] == "low"
    assert captured["reasoning_effort"] == settings.ai_tool_reasoning_effort


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    task_id = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1",
        item_index=1, instruction="降低选项相似度",
    )
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload["instruction"] == "降低选项相似度"
    assert row.payload["item_index"] == 1


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    first = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1, instruction="改",
    )
    again = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1, instruction="改",
    )
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs)
        .where(task_runs.c.id == first)
        .values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1, instruction="改",
    )
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_rejects_empty_instruction(session):
    with pytest.raises(AiReviseError):
        enqueue_ai_revise(
            session, course_id="c1", paper_version_id="pv1", item_index=1,
            instruction="   ",
        )


def test_enqueue_rejects_missing_item(session):
    with pytest.raises(AiReviseError, match="不在该试卷版本中"):
        enqueue_ai_revise(
            session, course_id="c1", paper_version_id="pv1", item_index=99,
            instruction="改",
        )


def test_enqueue_rejects_finalized_paper(session):
    session.execute(
        update(paper_versions)
        .where(paper_versions.c.id == "pv1")
        .values(status="finalized")
    )
    session.commit()
    with pytest.raises(AiReviseConflict):
        enqueue_ai_revise(
            session, course_id="c1", paper_version_id="pv1", item_index=1,
            instruction="改",
        )


# ─── 目标属性解析（换难度/认知/考点：确定性预检 + 系统盖章） ───


def _seed_second_exam_point(session, *, atom="反向传播的链式法则", boundary="围绕链式法则"):
    """目录 cv1 下补考点 ep2 + 单元 au2 + 卡 kc2；ep1 提升为 confirmed（菜单按
    confirmed 口径取考点元信息，见 api/v1/knowledge 的 published-knowledge）。"""
    with session.begin():
        session.execute(
            exam_points.update().where(exam_points.c.id == "ep1").values(status="confirmed")
        )
        session.execute(exam_points.insert().values(
            id="ep2", course_id="c1", framework_version_id="fv1", anchor_key="A2",
            code="EP2", title="考点2", assessment_requirement="掌握A2", weight_value=20.0,
            weight_source="teacher_confirmed", weight_group_id="A2", priority="normal",
            cognitive_targets=[], assessment_orientations=[], allowed_question_types=[],
            operational_detail_policy="supporting_only", scope_boundary={},
            required_evidence_roles=[], retrieval_intent="围绕A2检索",
            teaching_anchor_keys=[], status="confirmed",
        ))
        session.execute(assessment_units.insert().values(
            id="au2", course_id="c1", catalog_version_id="cv1", content_domain_id="cd1",
            exam_point_id="ep2", code="U2", title="单元2", performance_statement="ps2",
            weight=20, status="active",
        ))
        session.execute(knowledge_cards.insert().values(
            id="kc2", course_id="c1", catalog_version_id="cv1", assessment_unit_id="au2",
            name="卡A2", performance_statement="掌握A2",
            assessable_content=[atom], content_hash="hca2", status="active",
            concept_cluster="B", answer_proposition=boundary,
        ))


def _resolve(session, targets, *, ctx=None):
    return resolve_revise_targets(
        session, course_id="c1", paper_version_id="pv1",
        context=ctx or _ctx(session), targets=targets,
    )


def test_resolve_returns_none_without_targets(session):
    assert _resolve(session, None) is None
    assert _resolve(session, {}) is None
    assert _resolve(session, {"difficulty": "", "cognitive_level": None}) is None


def test_resolve_normalizes_difficulty_alias_with_pairing(session):
    # hard 是题目词表别名，落库归一为蓝图词表 high；high 不允许 understand，须同调认知
    out = _resolve(session, {"difficulty": "hard", "cognitive_level": "analyze"})
    assert out == {"difficulty": "high", "cognitive_level": "analyze"}


def test_resolve_rejects_difficulty_cognitive_mismatch(session):
    # 低档只允许 remember/understand：配对校验先于模型调用确定性拒绝（422）
    with pytest.raises(AiReviseError, match="不允许认知层级"):
        _resolve(session, {"difficulty": "low", "cognitive_level": "analyze"})


def test_resolve_rejects_invalid_target_values(session):
    with pytest.raises(AiReviseError, match="非法"):
        _resolve(session, {"difficulty": "impossible"})
    with pytest.raises(AiReviseError, match="布鲁姆六级"):
        _resolve(session, {"cognitive_level": "hallucinate"})
    with pytest.raises(AiReviseError, match="不支持的目标字段"):
        _resolve(session, {"score": 5})
    with pytest.raises(AiReviseError, match="同时给出目标考点"):
        _resolve(session, {"card_id": "kc2"})


def test_resolve_exam_point_change_picks_atom_and_backfills(session):
    _seed_second_exam_point(session)
    out = _resolve(session, {"exam_point_id": "ep2"})
    assert out["exam_point_id"] == "ep2"
    assert out["exam_point_title"] == "考点2"
    assert out["knowledge_card_id"] == "kc2"
    assert out["coverage_atom"] == "反向传播的链式法则"
    # 跨考点：回补标记指向原考点（合同配比计回，评审可见）
    assert out["backfilled_from"]["from_exam_point_id"] == "ep1"
    assert out["backfilled_from"]["to_exam_point_id"] == "ep2"
    # 换考点后禁用上下文按目标考点重算
    assert out["forbidden_context"]["atoms"] == []


def test_resolve_same_exam_point_keeps_no_backfill(session):
    _seed_second_exam_point(session)
    # 目标 = 当前考点（ep1）：同考点换卡不写回补标记
    out = _resolve(session, {"exam_point_id": "ep1"})
    assert out["exam_point_id"] == "ep1"
    assert out["knowledge_card_id"] == "kc1"
    assert out["backfilled_from"] is None


def test_resolve_skips_atom_used_by_other_item(session):
    _seed_second_exam_point(session)
    # 其他题（归档存回式，无 plan 槽位）已用掉 ep2 的唯一原子：预检拒绝，不交模型
    with session.begin():
        session.execute(generated_questions.insert().values(
            id="gq2", course_id="c1", generation_run_id="gr1", plan_item_id=None,
            knowledge_card_id="kc2", revision_no=1, status="candidate",
            payload={"stem": "x", "answer": "y", "coverage_atom": "反向传播的链式法则"},
        ))
        session.execute(paper_items.insert().values(
            id="item2", course_id="c1", paper_version_id="pv1",
            generated_question_id="gq2", display_order=2,
        ))
    with pytest.raises(AiReviseError, match="已被其他题占用"):
        _resolve(session, {"exam_point_id": "ep2"})


def test_resolve_rejects_boundary_conflict(session):
    _seed_second_exam_point(session)
    # 另一道题（归档存回式）已占用与目标卡相同的答案域：同一答案域不能落两道题
    with session.begin():
        session.execute(generated_questions.insert().values(
            id="gq2", course_id="c1", generation_run_id="gr1", plan_item_id=None,
            knowledge_card_id="kc2", revision_no=1, status="candidate",
            payload={"stem": "x", "answer": "y", "answer_boundary": "围绕链式法则"},
        ))
        session.execute(paper_items.insert().values(
            id="item2", course_id="c1", paper_version_id="pv1",
            generated_question_id="gq2", display_order=2,
        ))
    with pytest.raises(AiReviseError, match="重叠或共享命题"):
        _resolve(session, {"exam_point_id": "ep2"})


def test_resolve_targeted_card_within_exam_point(session):
    _seed_second_exam_point(session)
    out = _resolve(session, {"exam_point_id": "ep2", "card_id": "kc2"})
    assert out["knowledge_card_id"] == "kc2"
    # 指定的卡不存在于该考点池：给出明确原因而不是静默换卡
    with pytest.raises(AiReviseError, match="没有不冲突的可用原子"):
        _resolve(session, {"exam_point_id": "ep1", "card_id": "kc999"})


def test_resolve_missing_catalog_rejects(session):
    # 手拟题/归档题没有 generation_run：换考点不可用，如实拒绝
    session.execute(
        update(paper_versions).where(paper_versions.c.id == "pv1").values(generation_run_id=None)
    )
    session.commit()
    with pytest.raises(AiReviseError, match="蓝图目录"):
        _resolve(session, {"exam_point_id": "ep2"})


# ─── 盖章与 prompt 目标块 ───


def test_normalize_stamps_target_attrs():
    current = {
        "question_type": "single_choice", "stem": "s", "options": None, "answer": "A",
        "explanation": "e", "difficulty": "medium", "cognitive_level": "understand",
        "coverage_atom": "老原子", "exam_point_id": "ep1", "unit_id": "au1",
    }
    proposal = normalize_proposal(
        {"stem": "s", "options": None, "answer": "A", "explanation": "e"},
        current=current,
        target={"difficulty": "high", "coverage_atom": "新原子"},
    )
    # 目标覆盖、未指定沿用——属性是系统盖章，模型只写了题面内容
    assert proposal["difficulty"] == "high"
    assert proposal["coverage_atom"] == "新原子"
    assert proposal["cognitive_level"] == "understand"
    assert proposal["exam_point_id"] == "ep1"


def test_normalize_without_target_keeps_attrs():
    current = {
        "question_type": "true_false", "stem": "s", "options": None,
        "answer": True, "explanation": "e", "difficulty": "medium",
    }
    proposal = normalize_proposal({"stem": "s2"}, current=current)
    assert proposal["stem"] == "s2"
    assert proposal["difficulty"] == "medium"
    assert proposal["exam_point_id"] is None


def test_build_prompt_target_overrides_contract(session):
    ctx = _ctx(session)
    target = {
        "difficulty": "high", "cognitive_level": "analyze",
        "exam_point_id": "ep2", "exam_point_title": "考点2",
        "coverage_atom": "新原子", "answer_boundary": "新边界",
        "forbidden_context": {"atoms": ["兄弟原子"], "answer_cores": ["兄弟核心"]},
    }
    _, payload = build_revise_prompt(ctx, "改", target=target)
    # 合同块按目标覆写：模型看到的原子/边界/难度即教师目标
    assert payload["contract"]["difficulty"] == "high"
    assert payload["contract"]["cognitive_level"] == "analyze"
    assert payload["contract"]["coverage_atom"] == "新原子"
    assert payload["contract"]["forbidden_context"]["atoms"] == ["兄弟原子"]
    # teacher_target 块明示哪些锁定被解禁
    assert payload["teacher_target"]["difficulty"] == "high"
    assert payload["teacher_target"]["exam_point"]["title"] == "考点2"
    assert "题型与分值仍锁定" in payload["teacher_target"]["note"]


# ─── 终检按盖章属性把关 ───


def test_validate_uses_stamped_attrs_for_difficulty_gate(session):
    current = _ctx(session)["current"]
    # 低档 + 创造：盖章后属性进难度门禁，允许集外即阻断（此前 cognitive 从未盖章）
    low_create = {
        "stem": "改后的题干", "options": dict(_GOOD_OPTIONS), "answer": "A",
        "explanation": "e", "difficulty": "low", "cognitive_level": "create",
    }
    assert validate_proposal(low_create, current=current)["passed"] is False
    high_analyze = {
        "stem": "改后的题干", "options": dict(_GOOD_OPTIONS), "answer": "A",
        "explanation": "e", "difficulty": "high", "cognitive_level": "analyze",
    }
    assert validate_proposal(high_analyze, current=current)["passed"] is True


# ─── 入队与执行链路的 targets 透传 ───


def test_enqueue_targets_in_idempotency_key(session):
    t1 = {"difficulty": "hard", "cognitive_level": "analyze"}
    id_a = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="改", targets=dict(t1),
    )
    session.commit()
    # 同题同要求同目标的在途任务复用
    id_b = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="改", targets=dict(t1),
    )
    assert id_a == id_b
    # 目标不同 = 两件事，必须换新任务（否则教师拿到旧目标的完成态）
    id_c = enqueue_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="改", targets={"difficulty": "low", "cognitive_level": "understand"},
    )
    assert id_c != id_a


def test_enqueue_fast_fails_on_bad_targets(session):
    with pytest.raises(AiReviseError):
        enqueue_ai_revise(
            session, course_id="c1", paper_version_id="pv1", item_index=1,
            instruction="改", targets={"difficulty": "low", "cognitive_level": "create"},
        )


def test_run_ai_revise_stamps_target_and_passes_prompt(session):
    client = StubClient([{
        "stem": "关于分布式训练中的梯度同步，下列说法正确的是？",
        "options": dict(_GOOD_OPTIONS), "answer": "B",
        "explanation": "推理不交换梯度，因此不同步。", "change_summary": "提高了难度",
    }])
    result = run_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="提高难度", client=client,
        targets={"difficulty": "hard", "cognitive_level": "analyze"},
    )
    assert result["proposal"]["difficulty"] == "high"
    assert result["proposal"]["cognitive_level"] == "analyze"
    assert result["target"]["difficulty"] == "high"
    # 模型 prompt 里带着目标块（写题围绕目标，不靠模型自觉换属性）
    assert client.calls[0]["payload"]["teacher_target"]["difficulty"] == "high"
    assert result["validation"]["passed"] is True


def test_run_ai_revise_without_targets_has_no_teacher_target(session):
    client = StubClient([{
        "stem": "关于分布式训练中的梯度同步，下列说法正确的是？",
        "options": dict(_GOOD_OPTIONS), "answer": "A",
        "explanation": "同步梯度需要通信，因此引入开销。", "change_summary": "微调表述",
    }])
    result = run_ai_revise(
        session, course_id="c1", paper_version_id="pv1", item_index=1,
        instruction="让题干更简洁", client=client,
    )
    assert "teacher_target" not in client.calls[0]["payload"]
    assert result["proposal"]["difficulty"] == "medium"
    assert result["target"] is None


# ─── 换考点目标菜单 ───


def test_list_revise_targets_menu(session):
    _seed_second_exam_point(session)
    # ep3：有单元无卡 → 不可用并给出原因，教师不空跑模型
    with session.begin():
        session.execute(exam_points.insert().values(
            id="ep3", course_id="c1", framework_version_id="fv1", anchor_key="A3",
            code="EP3", title="考点3", assessment_requirement="掌握A3", weight_value=10.0,
            weight_source="teacher_confirmed", weight_group_id="A3", priority="normal",
            cognitive_targets=[], assessment_orientations=[], allowed_question_types=[],
            operational_detail_policy="supporting_only", scope_boundary={},
            required_evidence_roles=[], retrieval_intent="围绕A3检索",
            teaching_anchor_keys=[], status="confirmed",
        ))
        session.execute(assessment_units.insert().values(
            id="au3", course_id="c1", catalog_version_id="cv1", content_domain_id="cd1",
            exam_point_id="ep3", code="U3", title="单元3", performance_statement="ps3",
            weight=10, status="active",
        ))
    menu = list_revise_targets(session, course_id="c1", paper_version_id="pv1", item_index=1)
    eps = {e["id"]: e for e in menu["exam_points"]}
    assert eps["ep2"]["cards"][0]["card_id"] == "kc2"
    assert eps["ep2"]["cards"][0]["atom"] == "反向传播的链式法则"
    assert eps["ep1"]["current"] is True
    un = {u["id"]: u for u in menu["unavailable_exam_points"]}
    assert "没有知识卡" in un["ep3"]["reason"]


def test_list_revise_targets_menu_without_catalog(session):
    session.execute(
        update(paper_versions).where(paper_versions.c.id == "pv1").values(generation_run_id=None)
    )
    session.commit()
    menu = list_revise_targets(session, course_id="c1", paper_version_id="pv1", item_index=1)
    assert menu["exam_points"] == []
    assert "蓝图目录" in menu["note"]


# ─── 读端：override 回落与偏离合同徽标 ───


def test_paper_version_item_reads_override_attrs_and_deviation(session):
    from app.services.paper_version_service import get_paper_version

    with session.begin():
        session.execute(update(paper_items).where(paper_items.c.id == "item1").values(
            teacher_override={
                "difficulty": "high", "cognitive_level": "analyze",
                "exam_point_id": "ep2", "exam_point_title": "考点2", "exam_point_code": "EP2",
                "knowledge_card_id": "kc2", "coverage_atom": "新原子",
                "answer_boundary": "新边界", "unit_id": "au2",
                "backfilled_from": {"from_exam_point_id": "ep1", "to_exam_point_id": "ep2",
                                     "anchor_key": "A1"},
            }
        ))
    pv = get_paper_version(session, "pv1", course_id="c1")
    q = pv["questions"][0]
    assert q["difficulty"] == "high"
    assert q["cognitive_level"] == "analyze"
    assert q["exam_point_id"] == "ep2"
    assert q["exam_point_title"] == "考点2"
    assert q["knowledge_card_id"] == "kc2"
    assert q["coverage_atom"] == "新原子"
    assert q["backfilled_from"]["to_exam_point_id"] == "ep2"
    fields = {d["field"] for d in q["attr_deviation"]}
    assert fields == {"difficulty", "cognitive_level", "exam_point"}
    ep_diff = next(d for d in q["attr_deviation"] if d["field"] == "exam_point")
    assert ep_diff["plan"] == "ep1" and ep_diff["effective"] == "ep2"
    assert ep_diff["effective_title"] == "考点2"


def test_paper_version_attr_deviation_empty_without_override(session):
    from app.services.paper_version_service import get_paper_version

    pv = get_paper_version(session, "pv1", course_id="c1")
    assert pv["questions"][0]["attr_deviation"] == []
    assert pv["questions"][0]["difficulty"] == "medium"


def test_paper_version_explicit_null_override_falls_back(session):
    """撤销回滚：属性键显式 null（前端 undo 的 PATCH）视同未覆盖，回落原值。"""
    from app.services.paper_version_service import get_paper_version

    with session.begin():
        session.execute(update(paper_items).where(paper_items.c.id == "item1").values(
            teacher_override={"difficulty": None, "exam_point_id": None, "coverage_atom": None},
        ))
    q = get_paper_version(session, "pv1", course_id="c1")["questions"][0]
    assert q["difficulty"] == "medium"          # payload 原值
    assert q["exam_point_id"] == "ep1"          # payload 原值
    assert q["coverage_atom"] is None           # payload 本就没有，如实为空
    assert q["attr_deviation"] == []            # 显式 null 不算偏离


def test_needs_review_prefers_override_traces(session):
    from app.services.paper_version_service import list_needs_review

    with session.begin():
        session.execute(update(paper_items).where(paper_items.c.id == "item1").values(
            needs_review=True,
            teacher_override={"exam_point_id": "ep2", "knowledge_card_id": "kc2"},
        ))
    out = list_needs_review(session, "pv1", course_id="c1")
    assert out[0]["exam_point_id"] == "ep2"
    assert out[0]["card_id"] == "kc2"
