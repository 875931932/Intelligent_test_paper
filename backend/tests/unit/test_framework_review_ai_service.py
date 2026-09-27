"""框架候选 AI 评审服务单元测试。

镜像 tests/unit/test_exam_rules_ai_service.py 的组织方式，播种同构（一门课 +
一份已发布框架 payload + 考点行 + 冲突行）。覆盖：上下文装配（候选优先、锚点/
考点瘦身、考试规则归一、待裁决冲突读表、覆盖/权重/认知统计、框架缺失拒绝）、
prompt 装配（真实数据进 payload + JSON schema 硬规则 + 纠错反馈字段）、报告归一
（verdict/severity 别名、未知 area 收口、短 message 丢弃、去重、限 12 条）、
校验收口（verdict 词表 + 总评长度）、带反馈的一次纠错重试与如实报错、任务入队
幂等与状态门禁、worker 入口。LLM 用同接口桩注入。
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
    framework_conflicts,
    framework_versions,
    task_runs,
)
from app.services import framework_review_ai_service
from app.services.framework_review_ai_service import (
    TASK_TYPE,
    FrameworkReviewError,
    build_review_prompt,
    enqueue_review,
    load_review_context,
    normalize_report,
    run_review,
    validate_report,
)

_PAYLOAD = {
    "anchors": [
        {
            "key": "A1", "title": "第1章 绪论", "exam_weight": 60.0,
            "ability_requirements": ["理解概念"],
            "allowed_question_types": ["single_choice"],
            "excluded_content": [], "alignment_keys": [],
        },
        {
            "key": "A2", "title": "第2章 模型", "exam_weight": 40.0,
            "ability_requirements": [], "allowed_question_types": [],
            "excluded_content": [], "alignment_keys": [],
        },
    ],
    "exam_points": [
        {
            "code": "EP1", "title": "考点1", "anchor_key": "A1",
            "assessment_requirement": "掌握A1", "weight_value": 30.0,
            "weight_source": "teacher_confirmed", "weight_group_id": "A1",
            "priority": "normal",
            "cognitive_targets": ["remember", "understand"],
            "assessment_orientations": ["conceptual"],
            "allowed_question_types": ["single_choice", "简答题"],
            "operational_detail_policy": "supporting_only",
            "scope_boundary": {}, "required_evidence_roles": [],
            "retrieval_intent": "", "teaching_anchor_keys": [],
            "status": "candidate",
        },
        {
            "code": "EP2", "title": "考点2", "anchor_key": "A2",
            "assessment_requirement": "会分析A2", "weight_value": 70.0,
            "weight_source": "teacher_confirmed", "weight_group_id": "A2",
            "priority": "normal",
            "cognitive_targets": ["analyze"],
            "assessment_orientations": ["application"],
            "allowed_question_types": ["short_answer"],
            "operational_detail_policy": "supporting_only",
            "scope_boundary": {}, "required_evidence_roles": [],
            "retrieval_intent": "", "teaching_anchor_keys": [],
            "status": "candidate",
        },
    ],
    "teaching_topics": [
        {"key": "T1", "title": "主题1", "depth": "basic", "requirements": []},
    ],
    "conflicts": [],
    "final_exam_rules": {
        "exam_form": "闭卷笔试",
        "duration_minutes": 90,
        "total_score": 100,
        "question_type_ratios": [
            {"question_type": "single_choice", "ratio": 60},
            {"question_type": "short_answer", "ratio": 40},
        ],
        "chapter_weights": [
            {"anchor_key": "A1", "weight": 60},
            {"anchor_key": "A2", "weight": 40},
        ],
        "assessment_focus": [],
    },
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
        "verdict": "ready",
        "summary": "框架覆盖与权重分配整体合理，两条待裁决冲突已给出处理建议，可确认发布。",
        "findings": [
            {"severity": "info", "area": "coverage",
             "message": "两个考核范围均有考点覆盖，权重合计 100。", "suggestion": ""},
            {"severity": "warning", "area": "question_type",
             "message": "EP2 只允许简答题，题型略单一。",
             "suggestion": "确认 EP2 是否也允许单选。"},
        ],
    }


def _raw_bad_verdict() -> dict:
    return {
        "verdict": "maybe",
        "summary": "总评足够长的一句话文本，用于通过长度门禁检查。",
        "findings": [],
    }


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'framework_review.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.add(Course(id="c2", owner_id="u1", slug="cs102", name="CS102"))
        s.commit()
        s.execute(
            framework_versions.insert().values(
                id="fv1", course_id="c1", version_no=1,
                status="published", payload=dict(_PAYLOAD),
            )
        )
        for idx, code, anchor, allowed in (
            (1, "EP1", "A1", ["single_choice", "简答题"]),
            (2, "EP2", "A2", ["short_answer"]),
        ):
            s.execute(
                exam_points.insert().values(
                    id=f"ep{idx}", course_id="c1", framework_version_id="fv1",
                    anchor_key=anchor, code=code, title=f"考点{idx}",
                    assessment_requirement=f"掌握{anchor}", weight_value=30.0,
                    weight_source="teacher_confirmed", weight_group_id=anchor,
                    priority="normal", cognitive_targets=[], assessment_orientations=[],
                    allowed_question_types=allowed,
                    operational_detail_policy="supporting_only", scope_boundary={},
                    required_evidence_roles=[], retrieval_intent="",
                    teaching_anchor_keys=[], status="confirmed",
                )
            )
        for cid, status, details in (
            ("cf1", "open", {"key": "weight_total", "kind": "weight_total",
                             "severity": "blocking", "message": "章节权重合计不为 100"}),
            ("cf2", "open", {"key": "depth1", "kind": "teaching_depth_conflict",
                             "severity": "advisory", "message": "教学深度措辞差异"}),
            ("cf3", "resolved", {"key": "old1", "kind": "scope_conflict",
                                 "severity": "blocking", "message": "早已裁决"}),
        ):
            s.execute(
                framework_conflicts.insert().values(
                    id=cid, course_id="c1", framework_version_id="fv1",
                    status=status, details=details,
                )
            )
        s.commit()
        yield s


# ─── 上下文装配 ───


def test_load_review_context_assembles_facts(session):
    context = load_review_context(session, course_id="c1")
    assert context["framework_version_id"] == "fv1"
    assert context["framework_status"] == "published"
    assert context["version_no"] == 1

    # 锚点/考点瘦身入 prompt
    assert [a["key"] for a in context["anchors"]] == ["A1", "A2"]
    assert context["anchors"][0]["exam_weight"] == 60.0
    assert [p["code"] for p in context["exam_points"]] == ["EP1", "EP2"]
    assert "id" not in context["exam_points"][0]

    # 考试规则确定性归一（比例/章节权重齐全）
    rules = context["exam_rules"]
    assert rules["exam_form"] == "闭卷笔试"
    assert {r["question_type"]: r["ratio"] for r in rules["question_type_ratios"]} == {
        "single_choice": 60, "short_answer": 40,
    }
    assert len(rules["chapter_weights"]) == 2

    # 待裁决冲突读表（已 resolved 的不进报告），按 key 断言规避顺序依赖
    assert sorted(c["key"] for c in context["open_conflicts"]) == ["depth1", "weight_total"]
    severities = {c["key"]: c["severity"] for c in context["open_conflicts"]}
    assert severities == {"weight_total": "blocking", "depth1": "advisory"}

    # 后端算好的事实统计：模型只解读不重算
    stats = context["stats"]
    assert stats["anchor_count"] == 2 and stats["point_count"] == 2
    assert stats["points_per_anchor"] == [
        {"anchor_key": "A1", "count": 1}, {"anchor_key": "A2", "count": 1},
    ]
    assert stats["anchor_weight_sum"] == 100.0
    assert stats["cognitive_coverage"] == [
        {"level": "remember", "count": 1},
        {"level": "understand", "count": 1},
        {"level": "analyze", "count": 1},
    ]
    # 考点允许题型并集（中文名规整为英文枚举）
    assert stats["allowed_question_types"] == ["single_choice", "short_answer"]


def test_load_review_context_prefers_candidate(session):
    # 教师正决定发不发布：pending 候选才是要评审的那份（与 get_current_framework 的
    # published 优先相反）
    session.execute(
        framework_versions.insert().values(
            id="fv2", course_id="c1", version_no=2,
            status="candidate", payload=dict(_PAYLOAD),
        )
    )
    session.commit()
    context = load_review_context(session, course_id="c1")
    assert context["framework_version_id"] == "fv2"
    assert context["framework_status"] == "candidate"


def test_load_review_context_rejects_missing_framework(session):
    with pytest.raises(FrameworkReviewError, match="不存在"):
        load_review_context(session, course_id="c2")


# ─── prompt 装配 ───


def test_build_review_prompt_puts_real_data_into_payload(session):
    context = load_review_context(session, course_id="c1")
    system, payload = build_review_prompt(context, "重点看权重")
    assert payload["instruction"] == "重点看权重"
    assert payload["framework_status"] == "published"
    assert len(payload["anchors"]) == 2
    assert len(payload["exam_points"]) == 2
    assert len(payload["open_conflicts"]) == 2
    assert payload["stats"]["anchor_weight_sum"] == 100.0
    assert payload["vocab"]["verdict"] == ["ready", "revise_first"]
    assert payload["vocab"]["severity"] == ["info", "warning", "critical"]
    # 只读评审 + verdict 结论是给教师看的 JSON schema 硬规则，必须在 system prompt 里
    assert "只返回严格 JSON 对象" in system
    assert "ready" in system
    assert "previous_validation_error" not in payload


def test_build_review_prompt_carries_previous_error_on_retry(session):
    context = load_review_context(session, course_id="c1")
    _, payload = build_review_prompt(context, "", previous_error="缺少 verdict")
    assert payload["previous_validation_error"] == "缺少 verdict"


# ─── 报告归一与校验 ───


def test_normalize_report_filters_and_coerces(session):
    context = load_review_context(session, course_id="c1")
    raw = {
        "verdict": "可确认",  # 别名 → ready
        "summary": "整体结构合理，两条待裁决冲突已在报告中给出处理建议。",
        "findings": [
            {"severity": "warn", "area": "weight",
             "message": "A2 章权重偏低但考点最重。", "suggestion": "上调 A2 权重。"},
            {"severity": "warn", "area": "weight",
             "message": "A2 章权重偏低但考点最重。", "suggestion": "重复条目"},
            {"severity": "error", "area": "mystery",
             "message": "未知领域也要能渲染。", "suggestion": ""},
            {"severity": "info", "area": "coverage",
             "message": "短", "suggestion": ""},  # message < 5 字丢弃
            {"severity": "fatal", "area": "rules",
             "message": "严重级别不合法要整条丢弃。", "suggestion": ""},
            {"severity": "info", "area": "cognitive",
             "message": 123, "suggestion": None},  # message "123" 过短丢弃
            "not-a-dict",
        ],
    }
    report = normalize_report(raw, context)
    assert report["verdict"] == "ready"
    assert report["findings"] == [
        {"severity": "warning", "area": "weight",
         "message": "A2 章权重偏低但考点最重。", "suggestion": "上调 A2 权重。"},
        {"severity": "critical", "area": "other",
         "message": "未知领域也要能渲染。", "suggestion": ""},
    ]
    assert report["summary"].startswith("整体结构合理")


def test_normalize_report_caps_findings(session):
    context = load_review_context(session, course_id="c1")
    raw = {
        "verdict": "revise_first",
        "summary": "总评足够长的一句话文本，用于通过长度门禁检查。",
        "findings": [
            {"severity": "info", "area": "other",
             "message": f"第 {i} 条问题，内容各不相同。", "suggestion": ""}
            for i in range(15)
        ],
    }
    report = normalize_report(raw, context)
    assert len(report["findings"]) == 12  # 报告限条数，教师不被清单淹没


def test_normalize_report_rejects_non_dict(session):
    context = load_review_context(session, course_id="c1")
    with pytest.raises(FrameworkReviewError, match="JSON 对象"):
        normalize_report(["not", "a", "dict"], context)


def test_validate_report_gates(session):
    context = load_review_context(session, course_id="c1")
    good = normalize_report(_raw_good(), context)
    assert validate_report(good, context)["passed"] is True

    # verdict 不在词表：教师的确认/修改参考必须有一句话结论
    report = normalize_report(_raw_bad_verdict(), context)
    validation = validate_report(report, context)
    assert validation["passed"] is False and validation["code"] == "verdict_missing"

    # 总评太短：教师先读总评再决定看不看清单
    report = {"verdict": "ready", "summary": "短", "findings": []}
    validation = validate_report(report, context)
    assert validation["passed"] is False and validation["code"] == "summary_short"


# ─── 报告执行（纠错重试） ───


def test_run_review_happy_path(session):
    client = StubClient([_raw_good()])
    result = run_review(session, course_id="c1", instruction="", client=client)
    assert result["verdict"] == "ready"
    assert len(result["findings"]) == 2
    assert result["framework_version_id"] == "fv1"
    assert result["framework_status"] == "published"
    assert len(client.calls) == 1


def test_run_review_retries_once_with_feedback_then_raises(session):
    client = StubClient([_raw_bad_verdict(), _raw_bad_verdict()])
    with pytest.raises(FrameworkReviewError, match="未通过结构校验"):
        run_review(session, course_id="c1", instruction="", client=client)
    assert len(client.calls) == 2
    # 第二轮必须带上一轮的校验反馈（带反馈纠错一次是既定套路）
    assert "previous_validation_error" in client.calls[1]["payload"]


def test_run_review_retry_recovers(session):
    client = StubClient([_raw_bad_verdict(), _raw_good()])
    result = run_review(session, course_id="c1", instruction="", client=client)
    assert result["verdict"] == "ready"
    assert len(client.calls) == 2


# ─── worker 入口 ───


def test_execute_review_task_requires_configured_llm(session, monkeypatch):
    monkeypatch.setattr(framework_review_ai_service, "llm_configured", lambda: False)
    with pytest.raises(FrameworkReviewError, match="not configured"):
        framework_review_ai_service.execute_review_task(
            session, payload={"course_id": "c1", "instruction": ""}
        )


def test_execute_review_task_builds_client_and_runs(session, monkeypatch):
    monkeypatch.setattr(framework_review_ai_service, "llm_configured", lambda: True)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def request_json(self, **kwargs):
            return _raw_good()

    monkeypatch.setattr("app.adapters.model.llm_gateway.LLMJsonClient", FakeClient)
    result = framework_review_ai_service.execute_review_task(
        session, payload={"course_id": "c1", "instruction": "重点看权重"}
    )
    assert result["verdict"] == "ready" and result["findings"]


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    task_id = enqueue_review(
        session, course_id="c1", instruction="重点看权重"
    )
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload == {"course_id": "c1", "instruction": "重点看权重"}


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    first = enqueue_review(session, course_id="c1", instruction="看权重")
    again = enqueue_review(session, course_id="c1", instruction="看权重")
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs).where(task_runs.c.id == first).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_review(session, course_id="c1", instruction="看权重")
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_rejects_missing_framework(session):
    with pytest.raises(FrameworkReviewError, match="不存在"):
        enqueue_review(session, course_id="c2", instruction="")
