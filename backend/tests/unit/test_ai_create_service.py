"""新增题目 AI 生成服务单元测试。

镜像 tests/unit/test_ai_revise_service.py 的组织方式。覆盖：上下文装配
（状态门禁、现有题清单/知识卡进 prompt）、提案形态规整、校验收口
（非白名单题型/形态缺陷不通过）、带反馈的一次纠错重试、任务入队幂等
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
from app.services.ai_create_service import (
    AiCreateConflict,
    AiCreateError,
    TASK_TYPE,
    build_create_prompt,
    enqueue_ai_create,
    load_create_context,
    normalize_proposal,
    run_ai_create,
    validate_proposal,
)

_GOOD_OPTIONS = {"A": "同步训练需要交换梯度", "B": "推理阶段必须同步", "C": "与批大小完全无关", "D": "只影响内存占用"}

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
    engine = create_engine(f"sqlite:///{tmp_path / 'ai_create.db'}")
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
                contract_snapshot={},
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
    return load_create_context(session, course_id="c1", paper_version_id="pv1")


# ─── 上下文与 prompt ───


def test_load_context_includes_existing_items_and_cards(session):
    ctx = _ctx(session)
    assert ctx["items"] == [
        {"item_index": 1, "question_type": "single_choice", "stem": _PAYLOAD["stem"]}
    ]
    assert ctx["cards"][0]["name"] == "卡A1"
    assert ctx["cards"][0]["assessable_content"] == ["A1-原子1定义", "A1-原子2应用"]


def test_load_context_truncates_long_stems(session):
    long_stem = "长" * 80
    session.execute(
        update(generated_questions).where(generated_questions.c.id == "gq1")
        .values(payload={**_PAYLOAD, "stem": long_stem})
    )
    session.commit()
    ctx = _ctx(session)
    assert ctx["items"][0]["stem"] == "长" * 40


def test_load_context_rejects_missing_paper(session):
    with pytest.raises(AiCreateError, match="不存在"):
        load_create_context(session, course_id="c1", paper_version_id="pv-x")


def test_load_context_rejects_finalized_paper(session):
    session.execute(
        update(paper_versions).where(paper_versions.c.id == "pv1").values(status="finalized")
    )
    session.commit()
    with pytest.raises(AiCreateConflict):
        _ctx(session)


def test_load_context_without_generation_run_returns_no_cards(session):
    # 拿不到知识卡时纯指令生成，不报错
    session.execute(paper_versions.insert().values(
        id="pv2", course_id="c1", exam_project_id="proj1", version_no=2, status="candidate",
    ))
    session.commit()
    ctx = load_create_context(session, course_id="c1", paper_version_id="pv2")
    assert ctx["cards"] == []
    assert ctx["items"] == []


def test_build_prompt_grounded_in_existing_items(session):
    ctx = _ctx(session)
    system, payload = build_create_prompt("出一道单选题，考查梯度同步", ctx)
    assert payload["instruction"] == "出一道单选题，考查梯度同步"
    assert payload["existing_items"][0]["item_index"] == 1
    assert payload["existing_items"][0]["stem"] == _PAYLOAD["stem"]
    assert payload["knowledge_cards"][0]["name"] == "卡A1"
    # 系统提示词：白名单、综合题不支持、来源话术禁令、形态规则都要在
    assert "single_choice" in system
    assert "综合题" in system
    assert "来源话术" in system
    assert "连续4个以上下划线" in system
    # 未纠错时不应带上次错误字段
    assert "previous_validation_error" not in payload


def test_build_prompt_carries_previous_error_on_retry(session):
    ctx = _ctx(session)
    _, payload = build_create_prompt("改", ctx, previous_error="单选题必须有四个选项")
    assert payload["previous_validation_error"] == "单选题必须有四个选项"


# ─── 形态规整 ───


def test_normalize_aligns_choice_options_to_dict():
    proposal = normalize_proposal({
        "question_type": "single_choice",
        "stem": "题干？",
        "options": ["甲", "乙", "丙", "丁"],
        "answer": "B",
        "explanation": "e",
        "difficulty": "medium",
    })
    assert proposal["options"] == {"A": "甲", "B": "乙", "C": "丙", "D": "丁"}
    assert proposal["answer"] == "B"


def test_normalize_coerces_true_false_answer_to_bool():
    proposal = normalize_proposal({
        "question_type": "true_false",
        "stem": "题干。",
        "options": None,
        "answer": "正确",
        "explanation": "e",
        "difficulty": "easy",
    })
    assert proposal["answer"] is True
    assert proposal["options"] is None


def test_normalize_drops_options_for_non_choice():
    proposal = normalize_proposal({
        "question_type": "fill_blank",
        "stem": "梯度同步会引入____。",
        "options": {"A": "噪声"},
        "answer": "通信开销",
        "difficulty": "low",
    })
    assert proposal["options"] is None
    assert proposal["difficulty"] == "easy"  # low → 表单口径 easy


def test_normalize_rejects_non_object():
    with pytest.raises(AiCreateError):
        normalize_proposal(["not", "an", "object"])


# ─── 校验收口 ───


def _good_choice_proposal() -> dict:
    return {
        "question_type": "single_choice",
        "stem": "同步梯度训练中，通信开销主要来自哪里？",
        "options": dict(_GOOD_OPTIONS),
        "answer": "A",
        "explanation": "同步梯度需要交换参数，因此引入通信开销。",
        "difficulty": "medium",
    }


def test_validate_blocks_choice_with_three_options():
    bad = dict(_good_choice_proposal(), options={"A": "1", "B": "2", "C": "3"})
    result = validate_proposal(bad)
    assert result["passed"] is False
    assert result["code"] == "single_choice_schema"


def test_validate_blocks_non_whitelist_question_type():
    bad = dict(_good_choice_proposal(), question_type="comprehensive")
    result = validate_proposal(bad)
    assert result["passed"] is False
    assert result["code"] == "question_type_unsupported"


def test_validate_blocks_short_answer_without_rubric():
    bad = {
        "question_type": "short_answer",
        "stem": "简述梯度同步的作用。",
        "options": None,
        "answer": "梯度同步保证各节点参数一致。",
        "explanation": "e",
        "difficulty": "medium",
    }
    result = validate_proposal(bad)
    assert result["passed"] is False
    assert result["code"] == "rubric_missing"


def test_validate_blocks_source_language():
    bad = dict(_good_choice_proposal(), stem="根据课件第3章，通信开销来自哪里？")
    result = validate_proposal(bad)
    assert result["passed"] is False
    assert result["code"] == "source_language"


def test_validate_passes_good_proposal():
    result = validate_proposal(_good_choice_proposal())
    assert result["passed"] is True


# ─── 提案执行（桩客户端）───


def _raw_good() -> dict:
    return {**_good_choice_proposal(), "change_summary": "新出一道考查通信开销的单选题。"}


def _raw_bad_choice() -> dict:
    return {
        "question_type": "single_choice",
        "stem": "新题干？",
        "options": {"A": "1", "B": "2", "C": "3"},
        "answer": "A",
        "explanation": "e",
        "difficulty": "medium",
        "change_summary": "x",
    }


def test_run_ai_create_retries_with_validation_feedback(session):
    client = StubClient([_raw_bad_choice(), _raw_good()])
    result = run_ai_create(
        session, course_id="c1", paper_version_id="pv1",
        instruction="出一道单选题", client=client,
    )
    assert len(client.calls) == 2
    assert "previous_validation_error" in client.calls[1]["payload"]
    assert client.calls[0]["payload"]["existing_items"][0]["item_index"] == 1
    assert result["attempts"] == 2
    assert result["validation"]["passed"] is True
    assert result["proposal"]["question_type"] == "single_choice"
    assert result["instruction"] == "出一道单选题"


def test_run_ai_create_reports_failure_when_retry_still_blocks(session):
    client = StubClient([_raw_bad_choice(), _raw_bad_choice()])
    result = run_ai_create(
        session, course_id="c1", paper_version_id="pv1",
        instruction="随便出", client=client,
    )
    assert result["attempts"] == 2
    assert result["validation"]["passed"] is False
    assert result["validation"]["code"] == "single_choice_schema"


def test_run_ai_create_single_shot_when_first_passes(session):
    client = StubClient([_raw_good()])
    result = run_ai_create(
        session, course_id="c1", paper_version_id="pv1",
        instruction="出题", client=client,
    )
    assert len(client.calls) == 1
    assert result["attempts"] == 1
    assert result["validation"]["passed"] is True
    assert result["change_summary"] == "新出一道考查通信开销的单选题。"


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    task_id = enqueue_ai_create(
        session, course_id="c1", paper_version_id="pv1",
        instruction="出一道填空题",
    )
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload["instruction"] == "出一道填空题"
    assert row.payload["paper_version_id"] == "pv1"
    assert "item_index" not in row.payload


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    first = enqueue_ai_create(
        session, course_id="c1", paper_version_id="pv1", instruction="出",
    )
    again = enqueue_ai_create(
        session, course_id="c1", paper_version_id="pv1", instruction="出",
    )
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs).where(task_runs.c.id == first).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_ai_create(
        session, course_id="c1", paper_version_id="pv1", instruction="出",
    )
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_rejects_empty_instruction(session):
    with pytest.raises(AiCreateError):
        enqueue_ai_create(
            session, course_id="c1", paper_version_id="pv1", instruction="   ",
        )


def test_enqueue_rejects_missing_paper(session):
    with pytest.raises(AiCreateError, match="不存在"):
        enqueue_ai_create(
            session, course_id="c1", paper_version_id="pv-x", instruction="出",
        )


def test_enqueue_rejects_finalized_paper(session):
    session.execute(
        update(paper_versions).where(paper_versions.c.id == "pv1").values(status="finalized")
    )
    session.commit()
    with pytest.raises(AiCreateConflict):
        enqueue_ai_create(
            session, course_id="c1", paper_version_id="pv1", instruction="出",
        )
