"""考核规则 AI 助手服务单元测试。

镜像 tests/unit/test_paper_review_service.py 的组织方式，框架播种同构。覆盖：
上下文装配（当前规则 / 章节锚点 / 考点允许题型并集与默认回退 / 框架缺失拒绝）、
prompt 装配（真实数据进 payload + JSON schema 硬规则 + 纠错反馈字段）、提案归一
（未提及字段照抄当前规则、未知锚点与未知考查方式剔除、比例归一 100）、校验收口
（题型比例非空 + 教师已有字段不被清空）、带反馈的一次纠错重试与如实报错、任务
入队幂等与空要求/无框架拒绝。LLM 用同接口桩注入，不发真实请求。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    exam_points,
    framework_versions,
    task_runs,
)
from app.domain.blueprint.models import ASSESSMENT_MODES
from app.domain.framework.exam_rules import DEFAULT_TYPE_RULES
from app.services import exam_rules_ai_service
from app.services.exam_rules_ai_service import (
    TASK_TYPE,
    ExamRulesAIError,
    build_propose_prompt,
    enqueue_propose,
    load_propose_context,
    normalize_proposal,
    run_propose,
    validate_proposal,
)

_FINAL_RULES = {
    "exam_form": "闭卷笔试",
    "duration_minutes": 90,
    "total_score": 100,
    "question_type_ratios": [
        {"question_type": "single_choice", "ratio": 60},
        {"question_type": "short_answer", "ratio": 40},
    ],
    "chapter_weights": [
        {"anchor_key": "A1", "weight": 50},
        {"anchor_key": "A2", "weight": 50},
    ],
    "assessment_focus": [],
}


class StubClient:
    """LLMJsonClient 同接口桩：按序返回预置响应并记录每次调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "payload": payload})
        return self._responses.pop(0)


def _raw_good() -> dict:
    return {
        "exam_form": "闭卷笔试",
        "duration_minutes": 90,
        "total_score": 100,
        "question_type_ratios": [
            {"question_type": "single_choice", "ratio": 50},
            {"question_type": "true_false", "ratio": 20},
            {"question_type": "short_answer", "ratio": 30},
        ],
        "chapter_weights": [
            {"anchor_key": "A1", "weight": 40},
            {"anchor_key": "A2", "weight": 60},
        ],
        "assessment_focus": [
            {"assessment_mode": "conceptual", "weight": 70},
            {"assessment_mode": "practical_operation", "weight": 30},
        ],
        "explanation": "偏重概念理解并保留实操。",
    }


def _raw_bad_ratios() -> dict:
    return {
        "question_type_ratios": [],
        "chapter_weights": [{"anchor_key": "A1", "weight": 100}],
        "explanation": "没给比例。",
    }


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exam_rules_ai.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.commit()
        s.execute(framework_versions.insert().values(
            id="fv1", course_id="c1", version_no=1, status="published",
            payload={
                "anchors": [
                    {"key": "A1", "title": "第1章 绪论"},
                    {"key": "A2", "title": "第2章 模型"},
                ],
                "final_exam_rules": dict(_FINAL_RULES),
            },
        ))
        s.execute(exam_points.insert().values(
            id="ep1", course_id="c1", framework_version_id="fv1", anchor_key="A1",
            code="EP1", title="考点1", assessment_requirement="掌握A", weight_value=30.0,
            weight_source="teacher_confirmed", weight_group_id="A1", priority="normal",
            cognitive_targets=[], assessment_orientations=[],
            # 中文题型名要被规整成英文枚举
            allowed_question_types=["single_choice", "简答题"],
            operational_detail_policy="supporting_only", scope_boundary={},
            required_evidence_roles=[], retrieval_intent="围绕A检索",
            teaching_anchor_keys=[], status="active",
        ))
        s.commit()
        yield s


# ─── 上下文装配 ───


def test_load_propose_context_assembles_rules_anchors_and_types(session):
    context = load_propose_context(session, course_id="c1")
    assert context["current_rules"]["exam_form"] == "闭卷笔试"
    assert context["current_rules"]["duration_minutes"] == 90
    assert [a["key"] for a in context["anchors"]] == ["A1", "A2"]
    assert context["anchor_keys"] == ["A1", "A2"]
    # 考点允许题型并集：中文名规整为英文枚举、去重保序
    assert context["allowed_question_types"] == ["single_choice", "short_answer"]
    assert list(context["assessment_modes"]) == list(ASSESSMENT_MODES)


def test_load_propose_context_falls_back_to_default_types_when_no_points(session):
    session.execute(
        exam_points.update()
        .where(exam_points.c.id == "ep1")
        .values(allowed_question_types=[])
    )
    session.commit()
    context = load_propose_context(session, course_id="c1")
    assert context["allowed_question_types"] == sorted(DEFAULT_TYPE_RULES.keys())


def test_load_propose_context_rejects_missing_framework(session):
    # rejected 既不是 published 也不是候选草稿 → 当前框架查无
    session.execute(
        framework_versions.update()
        .where(framework_versions.c.id == "fv1")
        .values(status="rejected")
    )
    session.commit()
    with pytest.raises(ExamRulesAIError, match="命题框架"):
        load_propose_context(session, course_id="c1")


# ─── prompt 装配 ───


def test_build_propose_prompt_puts_real_data_into_payload(session):
    context = load_propose_context(session, course_id="c1")
    system, payload = build_propose_prompt(context, "90分钟闭卷，多考第2章")
    assert payload["instruction"] == "90分钟闭卷，多考第2章"
    assert payload["current_rules"]["exam_form"] == "闭卷笔试"
    assert payload["anchors"] == context["anchors"]
    assert "short_answer" in payload["allowed_question_types"]
    assert payload["assessment_modes"] == list(ASSESSMENT_MODES)
    # 提案是填入编辑草稿的 JSON，schema 硬规则必须在 system prompt 里
    assert "只返回严格 JSON 对象" in system
    assert "previous_validation_error" not in payload


def test_build_propose_prompt_carries_previous_error_on_retry(session):
    context = load_propose_context(session, course_id="c1")
    _, payload = build_propose_prompt(context, "侧重实操", previous_error="题型比例为空")
    assert payload["previous_validation_error"] == "题型比例为空"


# ─── 提案归一与校验 ───


def test_normalize_proposal_merges_omitted_fields_and_normalizes(session):
    context = load_propose_context(session, course_id="c1")
    proposal = normalize_proposal(
        {"question_type_ratios": [{"question_type": "选择题", "ratio": 100}]},
        context,
    )
    # 未提及字段照抄当前规则（prompt 明说 + 合并双保险，防清空）
    assert proposal["exam_form"] == "闭卷笔试"
    assert proposal["duration_minutes"] == 90
    assert proposal["total_score"] == 100
    assert proposal["chapter_weights"] == _FINAL_RULES["chapter_weights"]
    assert proposal["assessment_focus"] == []
    # 中文题型名规整成英文枚举
    assert proposal["question_type_ratios"] == [
        {"question_type": "single_choice", "ratio": 100.0}
    ]


def test_normalize_proposal_drops_unknown_anchor_and_mode(session):
    context = load_propose_context(session, course_id="c1")
    proposal = normalize_proposal(
        {
            "question_type_ratios": [{"question_type": "single_choice", "ratio": 100}],
            "chapter_weights": [{"anchor_key": "Z9", "weight": 100}],
            "assessment_focus": [
                {"assessment_mode": "主观题", "weight": 80},
                {"assessment_mode": "conceptual", "weight": 20},
            ],
        },
        context,
    )
    # 未知锚点全被过滤 → 章节权重回退为空（由校验收口拦住，不清空草稿）
    assert proposal["chapter_weights"] == []
    # 未知考查方式剔除，剩余归一到 100
    assert proposal["assessment_focus"] == [{"assessment_mode": "conceptual", "weight": 100.0}]


def test_normalize_proposal_rejects_non_dict(session):
    context = load_propose_context(session, course_id="c1")
    with pytest.raises(ExamRulesAIError, match="JSON 对象"):
        normalize_proposal(["not", "a", "dict"], context)


def test_validate_proposal_gates(session):
    context = load_propose_context(session, course_id="c1")
    good = normalize_proposal(_raw_good(), context)
    assert validate_proposal(good, context)["passed"] is True

    no_ratios = dict(good, question_type_ratios=[])
    result = validate_proposal(no_ratios, context)
    assert result["passed"] is False and result["code"] == "ratios_empty"

    # 模型把时长答成带单位字符串 → 归一后变 None，等于清空教师已有设置
    lost = dict(good, duration_minutes=None, chapter_weights=[])
    result = validate_proposal(lost, context)
    assert result["passed"] is False and result["code"] == "fields_lost"
    assert "章节命题权重" in result["message"] and "考试时长" in result["message"]


# ─── 提案执行（纠错重试） ───


def test_run_propose_happy_path_returns_normalized_proposal(session):
    client = StubClient([_raw_good()])
    result = run_propose(session, course_id="c1", instruction="侧重理解", client=client)
    proposal = result["proposal"]
    assert abs(sum(r["ratio"] for r in proposal["question_type_ratios"]) - 100) < 0.01
    assert proposal["assessment_focus"][0]["assessment_mode"] == "conceptual"
    assert result["explanation"] == "偏重概念理解并保留实操。"
    assert len(client.calls) == 1


def test_run_propose_retries_once_with_feedback_then_raises(session):
    client = StubClient([_raw_bad_ratios(), _raw_bad_ratios()])
    with pytest.raises(ExamRulesAIError, match="未通过结构校验"):
        run_propose(session, course_id="c1", instruction="照旧", client=client)
    assert len(client.calls) == 2
    # 第二轮必须带上一轮的校验反馈（带反馈纠错一次是既定套路）
    assert "previous_validation_error" in client.calls[1]["payload"]


def test_run_propose_retry_recovers(session):
    client = StubClient([_raw_bad_ratios(), _raw_good()])
    result = run_propose(session, course_id="c1", instruction="照旧", client=client)
    assert result["proposal"]["question_type_ratios"]
    assert len(client.calls) == 2


def test_execute_propose_task_requires_configured_llm(session, monkeypatch):
    """worker 入口：LLM 未配置如实报错，不构造客户端。"""
    monkeypatch.setattr(exam_rules_ai_service, "llm_configured", lambda: False)
    with pytest.raises(ExamRulesAIError, match="not configured"):
        exam_rules_ai_service.execute_propose_task(
            session, payload={"course_id": "c1", "instruction": "照旧"}
        )


def test_execute_propose_task_builds_client_and_runs(session, monkeypatch):
    """worker 入口按 settings 构造真实客户端并跑通提案（客户端打桩不发请求）。"""
    monkeypatch.setattr(exam_rules_ai_service, "llm_configured", lambda: True)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def request_json(self, **kwargs):
            return _raw_good()

    monkeypatch.setattr("app.adapters.model.llm_gateway.LLMJsonClient", FakeClient)
    result = exam_rules_ai_service.execute_propose_task(
        session, payload={"course_id": "c1", "instruction": "侧重理解"}
    )
    assert result["proposal"]["question_type_ratios"]
    assert result["explanation"]


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    task_id = enqueue_propose(session, course_id="c1", instruction="90分钟闭卷")
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload == {"course_id": "c1", "instruction": "90分钟闭卷"}


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    first = enqueue_propose(session, course_id="c1", instruction="标准提案")
    again = enqueue_propose(session, course_id="c1", instruction="标准提案")
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs).where(task_runs.c.id == first).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_propose(session, course_id="c1", instruction="标准提案")
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_rejects_empty_instruction(session):
    with pytest.raises(ExamRulesAIError, match="一句话"):
        enqueue_propose(session, course_id="c1", instruction="   ")


def test_enqueue_rejects_missing_framework(session):
    session.execute(
        framework_versions.update()
        .where(framework_versions.c.id == "fv1")
        .values(status="rejected")
    )
    session.commit()
    with pytest.raises(ExamRulesAIError, match="命题框架"):
        enqueue_propose(session, course_id="c1", instruction="侧重实操")
