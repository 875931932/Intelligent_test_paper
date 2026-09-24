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
    load_revise_context,
    normalize_proposal,
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
