"""蓝图题位 AI 调整建议服务单元测试。

镜像 tests/unit/test_exam_rules_ai_service.py 的组织方式，蓝图播种同构
（framework 考核规则 + 蓝图版本 + 两个题位）。覆盖：上下文装配（题位瘦身、
题型分值期望对照、难度/认知/章节分布、项目缺失/蓝图已确认/无题位拒绝）、
prompt 装配（真实数据进 payload + JSON schema 硬规则 + 纠错反馈字段）、建议
归一（题号在册/字段在词表/值域别名归一/中文题型名归一/去重/丢无操作/丢空
理由 + from_value 原值快照）、校验收口（总评长度 + 调分成对总分不变 + 难度
目标达标门禁）、教师指令难度比例的目标换算、带反馈的一次纠错重试与
如实报错、任务入队幂等与状态门禁、worker 入口。LLM 用同接口桩注入。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, delete, select, update
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
    task_runs,
)
from app.domain.blueprint.models import ASSESSMENT_MODES
from app.services import blueprint_suggest_service
from app.services.blueprint_suggest_service import (
    SUGGESTED_FIELDS,
    TASK_TYPE,
    BlueprintSuggestError,
    build_suggest_prompt,
    enqueue_suggest,
    load_suggest_context,
    normalize_suggestions,
    run_suggest,
    validate_suggestions,
)

# 考核规则：单选 70 / 简答 30，与题位实际（6/4）差 ±1 分，制造可见的期望对照
_FINAL_RULES = {
    "exam_form": "闭卷笔试",
    "duration_minutes": 90,
    "total_score": 100,
    "question_type_ratios": [
        {"question_type": "single_choice", "ratio": 70},
        {"question_type": "short_answer", "ratio": 30},
    ],
    "chapter_weights": [
        {"anchor_key": "A1", "weight": 60},
        {"anchor_key": "A2", "weight": 40},
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
        "summary": "蓝图整体结构合理，建议把题位1调难、两个题位分值对调，以贴合考核规则的难度层次与题型比例。",
        "suggestions": [
            {"item_index": 1, "field": "difficulty", "value": "high",
             "reason": "该考点权重最高却标为易，建议提到难。"},
            {"item_index": 1, "field": "score", "value": 7.5,
             "reason": "单选低于期望分值，为该题加 1.5 分。"},
            {"item_index": 2, "field": "score", "value": 2.5,
             "reason": "同步减 1.5 分，保持全卷总分不变。"},
        ],
    }


def _raw_bad_summary() -> dict:
    return {"summary": "不行", "suggestions": []}


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'blueprint_suggest.db'}")
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
        s.execute(knowledge_catalog_versions.insert().values(
            id="cv1", course_id="c1", framework_version_id="fv1",
            version_no=1, status="published",
        ))
        s.execute(exam_points.insert().values(
            id="ep1", course_id="c1", framework_version_id="fv1", anchor_key="A1",
            code="EP1", title="考点1", assessment_requirement="掌握A", weight_value=30.0,
            weight_source="teacher_confirmed", weight_group_id="A1", priority="normal",
            cognitive_targets=[], assessment_orientations=[],
            allowed_question_types=["single_choice", "简答题"],
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
            assessable_content=["A1-原子1定义"], content_hash="hca1",
            status="active", concept_cluster="A", answer_proposition="A1-边界",
        ))
        # 先建项目后建蓝图：active_blueprint_version_id 是指向 blueprint_versions
        # 的复合 FK（同课程），父行必须先落库
        s.execute(exam_projects.insert().values(
            id="proj1", course_id="c1", name="Midterm", status="draft",
        ))
        s.execute(blueprint_versions.insert().values(
            id="bv1", course_id="c1", exam_project_id="proj1",
            framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
            status="draft",
            type_rules={"single_choice": {"count": 1, "score": 6},
                        "short_answer": {"count": 1, "score": 4}},
            chapter_weights={"A1": 60, "A2": 40},
        ))
        s.execute(
            exam_projects.update()
            .where(exam_projects.c.id == "proj1")
            .values(active_blueprint_version_id="bv1")
        )
        for idx, qtype, score, difficulty, cognitive in (
            (1, "single_choice", 6.0, "low", "understand"),
            (2, "short_answer", 4.0, "medium", "analyze"),
        ):
            s.execute(plan_items.insert().values(
                id=f"pi{idx}", course_id="c1", blueprint_version_id="bv1",
                assessment_unit_id="au1", question_type=qtype, item_index=idx,
                score=score, difficulty=difficulty, cognitive_level=cognitive,
                assessment_mode="conceptual", exam_point_id="ep1",
                knowledge_card_id="kc1",
            ))
        s.commit()
        yield s


# ─── 上下文装配 ───


def test_load_suggest_context_assembles_items_and_stats(session):
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    assert context["known_indices"] == [1, 2]
    assert context["total_score"] == 10

    # 题型分值期望对照：期望=比例×总分，后端算好作为事实数据（单选 70%→7 分）
    assert context["type_diff"] == [
        {"question_type": "single_choice", "expected": 7.0, "actual": 6.0, "delta": -1.0},
        {"question_type": "short_answer", "expected": 3.0, "actual": 4.0, "delta": 1.0},
    ]
    # 难度分布按词表顺序（low 在前）
    assert context["difficulty_dist"] == [
        {"difficulty": "low", "count": 1, "score": 6.0},
        {"difficulty": "medium", "count": 1, "score": 4.0},
    ]
    # 章节：声明 60/40，实际全部落在 A1（ep1.anchor_key=A1）
    assert context["chapter_dist"] == [
        {"anchor_key": "A1", "expected_pct": 60, "actual_pct": 100.0, "delta_pct": 40.0},
        {"anchor_key": "A2", "expected_pct": 40, "actual_pct": 0.0, "delta_pct": -40.0},
    ]
    # 当前考核规则 + 考点允许题型（中文名规整为英文枚举）
    assert len(context["rules"]["question_type_ratios"]) == 2
    assert context["allowed_question_types"] == ["single_choice", "short_answer"]
    assert context["known_exam_point_ids"] == ["ep1"]
    assert context["known_card_ids"] == ["kc1"]
    # 题位入 prompt 前瘦身：不带任务库内部字段
    assert context["items"][0]["item_index"] == 1
    assert "id" not in context["items"][0]


def test_load_suggest_context_rejects_missing_project(session):
    with pytest.raises(BlueprintSuggestError, match="不存在"):
        load_suggest_context(session, course_id="c1", project_id="nope")


def test_load_suggest_context_rejects_confirmed_blueprint(session):
    # 冻结纪律：已确认蓝图只能新建版本，入队时就提前拒绝（文案含"不可原地修改"→ 409）
    session.execute(
        blueprint_versions.update()
        .where(blueprint_versions.c.id == "bv1")
        .values(status="confirmed")
    )
    session.commit()
    with pytest.raises(BlueprintSuggestError, match="不可原地修改"):
        load_suggest_context(session, course_id="c1", project_id="proj1")


def test_load_suggest_context_rejects_empty_blueprint(session):
    session.execute(delete(plan_items).where(plan_items.c.blueprint_version_id == "bv1"))
    session.commit()
    with pytest.raises(BlueprintSuggestError, match="没有题位"):
        load_suggest_context(session, course_id="c1", project_id="proj1")


# ─── prompt 装配 ───


def test_build_suggest_prompt_puts_real_data_into_payload(session):
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    system, payload = build_suggest_prompt(context, "难题调多一点")
    assert payload["instruction"] == "难题调多一点"
    assert payload["total_score"] == 10
    assert payload["type_diff"][0]["delta"] == -1.0
    assert len(payload["items"]) == 2
    assert payload["suggested_fields"] == list(SUGGESTED_FIELDS)
    # 逐题换考法：字段进词表、值域随 payload 下发、prompt 硬规则点名
    assert "assessment_mode" in payload["suggested_fields"]
    assert list(payload["vocab"]["assessment_mode"]) == list(ASSESSMENT_MODES)
    assert "assessment_mode" in system
    assert payload["vocab"]["difficulty"] == ["low", "medium", "high"]
    assert "short_answer" in payload["allowed_question_types"]
    # 建议清单是给教师确认的 JSON，schema 硬规则必须在 system prompt 里
    assert "只返回严格 JSON 对象" in system
    assert "previous_validation_error" not in payload


def test_build_suggest_prompt_carries_previous_error_on_retry(session):
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    _, payload = build_suggest_prompt(context, "", previous_error="调分成对")
    assert payload["previous_validation_error"] == "调分成对"


# ─── 建议归一与校验 ───


def test_normalize_suggestions_filters_and_coerces(session):
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    raw = {
        "summary": "混合输入：合法、越界、非法字段、同值与缺理由的建议都会被确定性收口。",
        "suggestions": [
            {"item_index": 1, "field": "difficulty", "value": "hard", "reason": "别名归一"},
            {"item_index": 1, "field": "difficulty", "value": "medium", "reason": "同题位同字段重复"},
            {"item_index": 99, "field": "score", "value": 5, "reason": "题号越界"},
            {"item_index": 2, "field": "material_form", "value": "case_text", "reason": "字段不在词表"},
            {"item_index": 2, "field": "cognitive_level", "value": "understand", "reason": "改认知层级"},
            {"item_index": 1, "field": "question_type", "value": "简答题", "reason": "中文题型名要归一"},
            {"item_index": 1, "field": "assessment_mode", "value": "problem_solving", "reason": "逐题换考法合法枚举"},
            {"item_index": 2, "field": "assessment_mode", "value": "brainstorm", "reason": "考法非法枚举要丢弃"},
            {"item_index": 1, "field": "assessment_mode", "value": "conceptual", "reason": "与当前同值要丢弃"},
            {"item_index": 2, "field": "difficulty", "value": "困难", "reason": "非法难度词"},
            {"item_index": 2, "field": "score", "value": 4.0, "reason": "与当前同值"},
            {"item_index": 1, "field": "cognitive_level", "value": "apply", "reason": ""},
            {"item_index": "2", "field": "difficulty", "value": "easy", "reason": "题号字符串转整数"},
            "not-a-dict",
        ],
    }
    result = normalize_suggestions(raw, context)
    got = [(s["item_index"], s["field"], s["value"]) for s in result["suggestions"]]
    assert got == [
        (1, "difficulty", "high"),       # 别名 hard → high
        (2, "cognitive_level", "understand"),
        (1, "question_type", "short_answer"),  # 中文名归一
        (1, "assessment_mode", "problem_solving"),  # 逐题换考法：合法枚举入表
        (2, "difficulty", "low"),        # 字符串题号 "2" + 别名 easy → low
    ]
    assert result["summary"].startswith("混合输入")


def test_normalize_suggestions_rejects_non_dict(session):
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    with pytest.raises(BlueprintSuggestError, match="JSON 对象"):
        normalize_suggestions(["not", "a", "dict"], context)


def test_validate_suggestions_gates(session):
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    good = normalize_suggestions(_raw_good(), context)
    assert validate_suggestions(good, context)["passed"] is True

    # 总评太短：教师决定看不看清单全靠它
    result = dict(good, summary="太短")
    validation = validate_suggestions(result, context)
    assert validation["passed"] is False and validation["code"] == "summary_short"

    # 调分成对由代码校验：只有加没有减 → 总分会变
    result = {
        "summary": "足够长的总评文本，用于通过总评门禁检查。",
        "suggestions": [
            {"item_index": 1, "field": "score", "value": 7.5, "reason": "单方面加分"},
        ],
    }
    validation = validate_suggestions(result, context)
    assert validation["passed"] is False and validation["code"] == "score_deltas"
    assert "不为 0" in validation["message"]

    # 空清单是合法结果（蓝图无需调整），总评说明即可
    result = {"summary": "蓝图结构与比例均合理，无需调整任何题位。", "suggestions": []}
    assert validate_suggestions(result, context)["passed"] is True


# ─── 难度目标换算与整卷清单门禁 ───


def test_difficulty_target_parses_word_ratio():
    # 「按5简单3中等2难」→ 8 题按最大余数法配平为 4/2/2，gap 由代码算好
    items = [{"item_index": i, "difficulty": "medium"} for i in range(1, 9)]
    target = blueprint_suggest_service._difficulty_target(
        "难度按5简单3中等2难的比例来分", items
    )
    assert target["ratio"] == [5, 3, 2]
    assert target["target_counts"] == {"low": 4, "medium": 2, "high": 2}
    assert target["current_counts"] == {"low": 0, "medium": 8, "high": 0}
    assert target["gap"] == {"low": 4, "medium": -6, "high": 2}


def test_difficulty_target_parses_colon_ratio_in_difficulty_context():
    items = [{"item_index": i, "difficulty": "low"} for i in range(1, 11)]
    target = blueprint_suggest_service._difficulty_target(
        "难度分布按 5:3:2 来分", items
    )
    assert target["target_counts"] == {"low": 5, "medium": 3, "high": 2}


def test_difficulty_target_ignores_ambiguous_or_non_difficulty_text():
    items = [{"item_index": 1, "difficulty": "medium"}]
    # 冒号比例不在难度语境（章节比例不误触）、无比例、空指令都不产出目标
    assert blueprint_suggest_service._difficulty_target(
        "章节权重按5:3:2分配", items
    ) is None
    assert blueprint_suggest_service._difficulty_target("难题调多一点", items) is None
    assert blueprint_suggest_service._difficulty_target("", items) is None


def test_difficulty_target_per_type_scope():
    # 「每个题型」→ 逐题型分别配平；顶层 target_counts = 各题型之和（整卷再算
    # 一次会与逐题型目标打架，取和保证两层门禁数学一致）
    items = (
        [
            {"item_index": i, "question_type": "single_choice", "difficulty": "high"}
            for i in range(1, 7)
        ]
        + [
            {"item_index": i, "question_type": "true_false", "difficulty": "low"}
            for i in range(7, 17)
        ]
    )
    target = blueprint_suggest_service._difficulty_target(
        "每个题型难度按5简单3中等2难的比例来分", items
    )
    assert target["scope"] == "question_type"
    assert target["ratio"] == [5, 3, 2]
    sc = target["by_type"]["single_choice"]
    assert sc["count"] == 6
    assert sc["target_counts"] == {"low": 3, "medium": 2, "high": 1}  # 6 题最大余数法
    assert sc["current_counts"] == {"low": 0, "medium": 0, "high": 6}
    assert sc["gap"] == {"low": 3, "medium": 2, "high": -5}
    assert target["by_type"]["true_false"]["target_counts"] == {
        "low": 5, "medium": 3, "high": 2,
    }
    assert target["target_counts"] == {"low": 8, "medium": 5, "high": 3}
    assert target["gap"] == {"low": -2, "medium": 5, "high": -3}

    # 没有范围词时维持整卷换算
    paper = blueprint_suggest_service._difficulty_target(
        "难度按5简单3中等2难的比例来分", items
    )
    assert paper["scope"] == "paper"
    assert "by_type" not in paper


def test_normalize_suggestions_snapshots_from_value(session):
    # 提案时原值必须快照：教师应用后面板仍显示「原值 → 新值」，不漂移成「易→易」
    context = load_suggest_context(session, course_id="c1", project_id="proj1")
    raw = {
        "summary": "提案原值快照后面板应用前后都能显示真实的调整起点。",
        "suggestions": [
            {"item_index": 1, "field": "difficulty", "value": "high", "reason": "调难"},
            {"item_index": 1, "field": "score", "value": 7.5, "reason": "加分"},
        ],
    }
    result = normalize_suggestions(raw, context)
    by_key = {(s["item_index"], s["field"]): s for s in result["suggestions"]}
    assert by_key[(1, "difficulty")]["from_value"] == "low"
    assert by_key[(1, "score")]["from_value"] == 6.0


def test_validate_suggestions_gates_difficulty_target():
    context = {
        "current_by_index": {
            1: {"difficulty": "low", "score": 6.0},
            2: {"difficulty": "medium", "score": 4.0},
        },
        "difficulty_target": {
            "ratio": [1, 0, 0],
            "target_counts": {"low": 2, "medium": 0, "high": 0},
            "current_counts": {"low": 1, "medium": 1, "high": 0},
            "gap": {"low": 1, "medium": -1, "high": 0},
        },
    }
    long_summary = "整体按目标比例铺满低难度，两道题全部调到低难度后才算达标。"

    # 清单不全（只回总评不给调整）→ 门禁打回，消息给出现状、目标与缺口
    validation = validate_suggestions(
        {"summary": long_summary, "suggestions": []}, context
    )
    assert validation["passed"] is False
    assert validation["code"] == "difficulty_target"
    assert "未达到目标" in validation["message"] and "gap" in validation["message"]

    # 补全缺口（题位2 调到 low）→ 模拟应用后恰好达标
    validation = validate_suggestions(
        {
            "summary": long_summary,
            "suggestions": [
                {"item_index": 2, "field": "difficulty", "value": "low",
                 "reason": "补缺口"},
            ],
        },
        context,
    )
    assert validation["passed"] is True


def test_validate_suggestions_gates_per_type_target():
    # 两个题型各 2 题，逐题型目标都是 1 易 1 中；当前单选全易、判断全中——
    # 全卷恰好 2 易 2 中（顶层 counts == target_counts），逐题型却各自全错
    context = {
        "current_by_index": {
            1: {"difficulty": "low", "question_type": "single_choice", "score": 2.0},
            2: {"difficulty": "low", "question_type": "single_choice", "score": 2.0},
            3: {"difficulty": "medium", "question_type": "true_false", "score": 1.0},
            4: {"difficulty": "medium", "question_type": "true_false", "score": 1.0},
        },
        "difficulty_target": {
            "ratio": [5, 3, 2],
            "scope": "question_type",
            "target_counts": {"low": 2, "medium": 2, "high": 0},
            "current_counts": {"low": 2, "medium": 2, "high": 0},
            "gap": {"low": 0, "medium": 0, "high": 0},
            "by_type": {
                "single_choice": {
                    "count": 2,
                    "target_counts": {"low": 1, "medium": 1, "high": 0},
                    "current_counts": {"low": 2, "medium": 0, "high": 0},
                    "gap": {"low": -1, "medium": 1, "high": 0},
                },
                "true_false": {
                    "count": 2,
                    "target_counts": {"low": 1, "medium": 1, "high": 0},
                    "current_counts": {"low": 0, "medium": 2, "high": 0},
                    "gap": {"low": 1, "medium": -1, "high": 0},
                },
            },
        },
    }
    long_summary = "逐题型核对后两个题型的简单与中等配比互有缺口，补齐后每个题型各自达标。"

    # 整卷 counts 已等于顶层目标也不放行——逐题型必须各自达标（实测场景的缩影）
    validation = validate_suggestions(
        {"summary": long_summary, "suggestions": []}, context
    )
    assert validation["passed"] is False
    assert validation["code"] == "difficulty_target"
    assert "by_type" in validation["message"]
    assert "single_choice" in validation["message"] and "true_false" in validation["message"]

    # 靠改 question_type 凑分布 → 打回（题型占比不在该指令范围内）
    validation = validate_suggestions(
        {
            "summary": long_summary,
            "suggestions": [
                {"item_index": 1, "field": "question_type", "value": "true_false",
                 "reason": "挪题凑分布"},
            ],
        },
        context,
    )
    assert validation["passed"] is False
    assert "question_type" in validation["message"]

    # 每个题型各补一条 → 逐题型恰好达标
    validation = validate_suggestions(
        {
            "summary": long_summary,
            "suggestions": [
                {"item_index": 2, "field": "difficulty", "value": "medium",
                 "reason": "单选调出一道易补中等"},
                {"item_index": 3, "field": "difficulty", "value": "low",
                 "reason": "判断调入一道易"},
            ],
        },
        context,
    )
    assert validation["passed"] is True


def test_run_suggest_completes_whole_paper_target_with_retry(session):
    # 整体比例指令端到端：首版只回示范条目（清单不全）→ 达标门禁打回带反馈，
    # 二版按 gap 补全才过——「我要求整体调整，不能只给几条」由代码兜底
    first = {
        "summary": "当前难度分布不均，先示范性调整其中一道以接近目标比例。",
        "suggestions": [
            {"item_index": 1, "field": "difficulty", "value": "high",
             "reason": "示范性调整"},
        ],
    }
    second = {
        "summary": "按整体目标重排：题位1 由低调整为中等，两道中等即达到目标分布。",
        "suggestions": [
            {"item_index": 1, "field": "difficulty", "value": "medium",
             "reason": "补足中等缺口"},
        ],
    }
    client = StubClient([first, second])
    result = run_suggest(
        session, course_id="c1", project_id="proj1",
        instruction="难度按0简单10中等0难的比例来分", client=client,
    )
    assert len(client.calls) == 2
    # 比例换算成目标分布随 payload 进 prompt（代码算好，模型不自己算）
    target = client.calls[0]["payload"]["difficulty_target"]
    assert target["target_counts"] == {"low": 0, "medium": 2, "high": 0}
    assert target["gap"] == {"low": -1, "medium": 1, "high": 0}
    # 首版不达标 → 带 previous_validation_error 纠错一次
    assert "previous_validation_error" in client.calls[1]["payload"]
    assert [(s["item_index"], s["value"]) for s in result["suggestions"]] == [
        (1, "medium")
    ]


def test_run_suggest_rejects_no_adjustment_under_per_type_target(session):
    """实测场景：教师问「每个题型按5:3:2」，模型答「完全匹配无需调整」必须被打回。

    fixture 两个题型各 1 题（单选 low、简答 medium），逐题型目标都是 1 易 0 中
    0 难——简答那道不改就永远不达标；全卷 1 易 1 中 的现状也骗不过逐题型门禁。
    """
    first = {
        "summary": "经核查，当前试卷蓝图在难度比例上完全匹配，无调整必要。",
        "suggestions": [],
    }
    second = {
        "summary": "逐题型核对后简答题难度与目标不符，调整为简单档后每个题型各自达标。",
        "suggestions": [
            {"item_index": 2, "field": "difficulty", "value": "low",
             "reason": "简答题单题型目标为简单档"},
        ],
    }
    client = StubClient([first, second])
    result = run_suggest(
        session, course_id="c1", project_id="proj1",
        instruction="每个题型难度按5简单3中等2难的比例来分", client=client,
    )
    assert len(client.calls) == 2
    target = client.calls[0]["payload"]["difficulty_target"]
    assert target["scope"] == "question_type"
    assert target["by_type"]["single_choice"]["target_counts"] == {
        "low": 1, "medium": 0, "high": 0,
    }
    assert target["by_type"]["short_answer"]["target_counts"] == {
        "low": 1, "medium": 0, "high": 0,
    }
    # 「无需调整」没骗过门禁：带反馈纠错一次，补齐简答题难度才放行
    assert "previous_validation_error" in client.calls[1]["payload"]
    assert [(s["item_index"], s["value"]) for s in result["suggestions"]] == [
        (2, "low")
    ]


# ─── 建议执行（纠错重试） ───


def test_run_suggest_happy_path(session):
    client = StubClient([_raw_good()])
    result = run_suggest(
        session, course_id="c1", project_id="proj1", instruction="难题多一点", client=client
    )
    assert len(result["suggestions"]) == 3
    assert result["total_score"] == 10
    assert result["summary"]
    assert len(client.calls) == 1


def test_run_suggest_retries_once_with_feedback_then_raises(session):
    client = StubClient([_raw_bad_summary(), _raw_bad_summary()])
    with pytest.raises(BlueprintSuggestError, match="未通过结构校验"):
        run_suggest(session, course_id="c1", project_id="proj1", instruction="", client=client)
    assert len(client.calls) == 2
    # 第二轮必须带上一轮的校验反馈（带反馈纠错一次是既定套路）
    assert "previous_validation_error" in client.calls[1]["payload"]


def test_run_suggest_retry_recovers(session):
    client = StubClient([_raw_bad_summary(), _raw_good()])
    result = run_suggest(
        session, course_id="c1", project_id="proj1", instruction="", client=client
    )
    assert result["suggestions"]
    assert len(client.calls) == 2


# ─── worker 入口 ───


def test_execute_suggest_task_requires_configured_llm(session, monkeypatch):
    monkeypatch.setattr(blueprint_suggest_service, "llm_configured", lambda: False)
    with pytest.raises(BlueprintSuggestError, match="not configured"):
        blueprint_suggest_service.execute_suggest_task(
            session, payload={"course_id": "c1", "project_id": "proj1", "instruction": ""}
        )


def test_execute_suggest_task_builds_client_and_runs(session, monkeypatch):
    monkeypatch.setattr(blueprint_suggest_service, "llm_configured", lambda: True)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def request_json(self, **kwargs):
            return _raw_good()

    monkeypatch.setattr("app.adapters.model.llm_gateway.LLMJsonClient", FakeClient)
    result = blueprint_suggest_service.execute_suggest_task(
        session,
        payload={"course_id": "c1", "project_id": "proj1", "instruction": "难题多一点"},
    )
    assert result["suggestions"] and result["summary"]


def test_execute_suggest_task_uses_configured_read_timeout(session, monkeypatch):
    """回归：读超时曾写死 45s，而该调用实测单次就要 40~50s（大 prompt + 思考
    模型），于是每次调用都在刀口上掷硬币——两次尝试都超时就 llm_transport_error，
    任务失败、助手卡片停在「可重试」，表现为「AI 发起的蓝图建议无法执行」。
    """
    from app.config import settings

    monkeypatch.setattr(blueprint_suggest_service, "llm_configured", lambda: True)
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def request_json(self, **kwargs):
            return _raw_good()

    monkeypatch.setattr("app.adapters.model.llm_gateway.LLMJsonClient", FakeClient)
    blueprint_suggest_service.execute_suggest_task(
        session, payload={"course_id": "c1", "project_id": "proj1", "instruction": ""}
    )
    assert captured["timeout"] == settings.blueprint_suggest_model_timeout
    assert captured["timeout"] > 45.0, "必须显著高于实测耗时，不能停在旧写死值"
    assert captured["timeout"] * captured["max_attempts"] <= 300, "不得超出任务租约"


# ─── 任务入队 ───


def test_enqueue_creates_task_with_type_and_payload(session):
    task_id = enqueue_suggest(
        session, course_id="c1", project_id="proj1", instruction="整体调难"
    )
    row = session.execute(
        select(task_runs.c.task_type, task_runs.c.status, task_runs.c.payload)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
    ).one()
    assert row.task_type == TASK_TYPE
    assert row.status == "queued"
    assert row.payload == {
        "course_id": "c1",
        "project_id": "proj1",
        "instruction": "整体调难",
    }


def test_enqueue_dedupes_in_flight_and_mints_new_key_after_terminal(session):
    first = enqueue_suggest(session, course_id="c1", project_id="proj1", instruction="调难")
    again = enqueue_suggest(session, course_id="c1", project_id="proj1", instruction="调难")
    assert first == again  # 在途任务复用，不重复烧模型

    session.execute(
        update(task_runs).where(task_runs.c.id == first).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_suggest(session, course_id="c1", project_id="proj1", instruction="调难")
    assert fresh != first  # 已终态：同要求重发要拿到新任务


def test_enqueue_rejects_missing_project(session):
    with pytest.raises(BlueprintSuggestError, match="不存在"):
        enqueue_suggest(session, course_id="c1", project_id="nope", instruction="")


def test_enqueue_rejects_confirmed_blueprint(session):
    session.execute(
        blueprint_versions.update()
        .where(blueprint_versions.c.id == "bv1")
        .values(status="confirmed")
    )
    session.commit()
    with pytest.raises(BlueprintSuggestError, match="不可原地修改"):
        enqueue_suggest(session, course_id="c1", project_id="proj1", instruction="调难")
