"""考核大纲考试规则：归一化与按比例推导题型分布。"""
import pytest

from app.domain.framework.exam_rules import (
    DEFAULT_TYPE_RULES,
    canonical_question_type,
    normalize_exam_rules,
    rules_have_type_ratios,
    type_rules_from_ratios,
)
from app.services.framework_service import _exam_rules_of


def test_canonical_question_type_maps_chinese_names():
    assert canonical_question_type("单选题") == "single_choice"
    assert canonical_question_type("多选题") == "multiple_choice"
    assert canonical_question_type("判断题") == "true_false"
    assert canonical_question_type("填空题") == "fill_blank"
    assert canonical_question_type("简答题") == "short_answer"
    assert canonical_question_type("综合题") == "comprehensive"
    assert canonical_question_type("论述题") == "essay"
    assert canonical_question_type("single_choice") == "single_choice"
    assert canonical_question_type("变态题") is None
    assert canonical_question_type("") is None


def test_normalize_exam_rules_keeps_syllabus_structure():
    rules = normalize_exam_rules(
        {
            "exam_form": "闭卷笔试",
            "duration_minutes": 90,
            "total_score": 100,
            "question_type_ratios": [
                {"question_type": "选择题", "ratio": 20},
                {"question_type": "判断题", "ratio": 20},
                {"question_type": "填空题", "ratio": 10},
                {"question_type": "简答题", "ratio": 20},
                {"question_type": "综合题", "ratio": 30},
            ],
            "chapter_weights": [
                {"anchor_key": "第1章", "weight": 5},
                {"anchor_key": "第2章", "weight": 25},
                {"anchor_key": "第3章", "weight": 35},
            ],
        },
        anchor_keys=["第1章", "第2章", "第3章", "第4章"],
    )
    assert rules["exam_form"] == "闭卷笔试"
    assert rules["duration_minutes"] == 90
    assert rules["total_score"] == 100
    assert [e["question_type"] for e in rules["question_type_ratios"]] == [
        "single_choice", "true_false", "fill_blank", "short_answer", "comprehensive",
    ]
    assert abs(sum(e["ratio"] for e in rules["question_type_ratios"]) - 100) < 0.01
    # 未在考纲里声明的章节权重归零，仍保留锚点，保证蓝图章权重能覆盖全部单元
    assert [e["anchor_key"] for e in rules["chapter_weights"]] == ["第1章", "第2章", "第3章", "第4章"]
    assert abs(sum(e["weight"] for e in rules["chapter_weights"]) - 100) < 0.01


def test_normalize_exam_rules_fills_undeclared_anchors_with_zero():
    """考纲只声明部分章节时，未声明的锚点补 0，保证蓝图章权重覆盖全部单元。"""
    rules = normalize_exam_rules(
        {"chapter_weights": [{"anchor_key": "第1章", "weight": 30}, {"anchor_key": "第2章", "weight": 70}]},
        anchor_keys=["第1章", "第2章", "第3章"],
    )
    assert [e["anchor_key"] for e in rules["chapter_weights"]] == ["第1章", "第2章", "第3章"]
    assert rules["chapter_weights"][2]["weight"] == 0.0
    assert abs(sum(e["weight"] for e in rules["chapter_weights"]) - 100) < 0.01


def test_normalize_exam_rules_empty_chapter_weights_falls_back():
    """考纲没有命题权重表时返回空列表，由消费方回退到考点权重，不凭空均分。"""
    assert normalize_exam_rules({}, anchor_keys=["第1章", "第2章"])["chapter_weights"] == []
    assert normalize_exam_rules(
        {"chapter_weights": [{"anchor_key": "第1章", "weight": 0}]}, anchor_keys=["第1章", "第2章"]
    )["chapter_weights"] == []


def test_normalize_exam_rules_drops_unknown_types_and_scales_ratios():
    rules = normalize_exam_rules(
        {
            "question_type_ratios": [
                {"question_type": "单选题", "ratio": 10},
                {"question_type": "变态题", "ratio": 90},
            ]
        },
        anchor_keys=[],
    )
    assert [e["question_type"] for e in rules["question_type_ratios"]] == ["single_choice"]
    assert rules["question_type_ratios"][0]["ratio"] == 100


def test_normalize_exam_rules_handles_empty_and_junk():
    assert normalize_exam_rules(None, anchor_keys=["a"])["question_type_ratios"] == []
    assert normalize_exam_rules({"question_type_ratios": "nope"}, anchor_keys=[])["question_type_ratios"] == []
    junk = normalize_exam_rules({"duration_minutes": "90", "total_score": -1}, anchor_keys=[])
    assert junk["duration_minutes"] is None and junk["total_score"] is None


def test_normalize_exam_rules_scales_difficulty_ratio():
    # 考纲原样写「难度 5:3:2」：归一到 50/30/20，键序固定 low→medium→high
    rules = normalize_exam_rules({"difficulty_distribution": {"low": 5, "medium": 3, "high": 2}})
    assert rules["difficulty_distribution"] == {"low": 50.0, "medium": 30.0, "high": 20.0}
    # 固定 100 的声明原样保留；负值剔除后剩余档归一
    assert normalize_exam_rules(
        {"difficulty_distribution": {"low": -10, "medium": 60, "high": 40}}
    )["difficulty_distribution"] == {"medium": 60.0, "high": 40.0}
    # 显式 0 档剔除，缺档不补
    assert normalize_exam_rules(
        {"difficulty_distribution": {"low": 60, "medium": 40, "high": 0}}
    )["difficulty_distribution"] == {"low": 60.0, "medium": 40.0}
    # 字符串值剔除后剩余档归一（与题型比例丢未知项再缩放同口径）
    assert normalize_exam_rules(
        {"difficulty_distribution": {"low": "50", "medium": 50}}
    )["difficulty_distribution"] == {"medium": 100.0}


def test_normalize_exam_rules_difficulty_omitted_when_absent_or_invalid():
    """未声明/非法难度一律省略键（与 type_formats 同约定）：蓝图保持全 medium 缺省。"""
    for raw in (
        None,
        {},
        {"difficulty_distribution": None},
        {"difficulty_distribution": "5:3:2"},
        {"difficulty_distribution": {"easy": 50, "hard": 50}},  # 未知档名全剔除
        {"difficulty_distribution": {"low": 0, "medium": 0, "high": 0}},  # 全零=未声明
    ):
        assert "difficulty_distribution" not in normalize_exam_rules(raw)


def test_type_rules_from_ratios_hits_total_score_exactly():
    rules = type_rules_from_ratios(
        [
            {"question_type": "single_choice", "ratio": 20},
            {"question_type": "true_false", "ratio": 20},
            {"question_type": "fill_blank", "ratio": 10},
            {"question_type": "short_answer", "ratio": 20},
            {"question_type": "comprehensive", "ratio": 30},
        ],
        total_score=100,
    )
    assert rules is not None
    assert sum(v["count"] * v["score"] for v in rules.values()) == 100
    # 教学大纲声明 30% 的综合题应拿到约 30 分，而不是默认分布的 20 分
    assert rules["comprehensive"]["count"] * rules["comprehensive"]["score"] == 30


def test_type_rules_from_ratios_returns_none_when_unusable():
    assert type_rules_from_ratios([], total_score=100) is None
    assert type_rules_from_ratios([{"question_type": "essay", "ratio": 100}], total_score=100) is None


def _syllabus_ratios():
    """线上课程实测比例：选择题20/判断20/填空10/简答20/综合30 → 42 题位。"""
    return [
        {"question_type": "single_choice", "ratio": 20},
        {"question_type": "true_false", "ratio": 20},
        {"question_type": "fill_blank", "ratio": 10},
        {"question_type": "short_answer", "ratio": 20},
        {"question_type": "comprehensive", "ratio": 30},
    ]


def test_type_rules_shrink_to_capacity_keeps_score_budget_and_half_points():
    """卡池容量不足时按题型分值份额等比缩容：总分与占比不变、题型不归零。

    回归：42 题位对 29 个可出题位（答案域口径），合同阶段静默丢掉 14 题、
    综合题全灭（卷面 29 题 45 分）；缩容后题数收敛到容量内，题型比例不失真。
    """
    rules = type_rules_from_ratios(_syllabus_ratios(), total_score=100, capacity=29)
    assert rules is not None
    assert sum(v["count"] * v["score"] for v in rules.values()) == 100
    # 题数取"能整除 2·S"的最近合法值（回归：曾误取最远值把各题型压到 1 题）
    assert {qt: (v["count"], v["score"]) for qt, v in rules.items()} == {
        "single_choice": (8.0, 2.5), "true_false": (10.0, 2.0), "fill_blank": (4.0, 2.5),
        "short_answer": (4.0, 5.0), "comprehensive": (2.0, 15.0),
    }
    # 各题型分值预算（=题数×单题分值）与考纲份额一一对应：缩容只改题数
    assert {qt: v["count"] * v["score"] for qt, v in rules.items()} == {
        "single_choice": 20, "true_false": 20, "fill_blank": 10,
        "short_answer": 20, "comprehensive": 30,
    }
    for v in rules.values():
        # 单题分值必须是 0.5 的倍数（蓝图引擎强约束）；题数不增、不归零
        assert v["score"] * 2 == int(v["score"] * 2)
        assert 1 <= v["count"]
    assert rules["single_choice"]["count"] <= 10
    assert rules["true_false"]["count"] <= 20
    assert rules["comprehensive"]["count"] * rules["comprehensive"]["score"] == 30
    # 总题数收敛到容量内，且尽量贴近容量（28/29，不为保底牺牲题量）
    assert sum(v["count"] for v in rules.values()) == 28
    # 同输入同输出（确定性）
    assert type_rules_from_ratios(_syllabus_ratios(), total_score=100, capacity=29) == rules


def test_type_rules_shrink_divisor_correction_enforces_capacity():
    """逐题型取最近合法值时合计可能仍超容量：纠正循环必须把总题数压进容量。"""
    rules = type_rules_from_ratios(_syllabus_ratios(), total_score=100, capacity=15)
    assert rules is not None
    # 初始分配合计 16（超一题）→ 纠正循环降到 15，恰好填满容量
    assert sum(v["count"] for v in rules.values()) == 15
    assert sum(v["count"] * v["score"] for v in rules.values()) == 100
    for v in rules.values():
        assert v["score"] * 2 == int(v["score"] * 2)
        assert v["count"] >= 1


def test_type_rules_capacity_absent_or_sufficient_matches_legacy():
    """不传容量 / 容量够用 / 容量非正时，行为与旧实现完全一致（回归）。"""
    legacy = type_rules_from_ratios(_syllabus_ratios(), total_score=100)
    assert type_rules_from_ratios(_syllabus_ratios(), total_score=100, capacity=None) == legacy
    assert type_rules_from_ratios(_syllabus_ratios(), total_score=100, capacity=42) == legacy
    assert type_rules_from_ratios(_syllabus_ratios(), total_score=100, capacity=0) == legacy


def test_rules_have_type_ratios():
    assert rules_have_type_ratios({"question_type_ratios": [{"question_type": "single_choice", "ratio": 20}]})
    assert not rules_have_type_ratios({})
    assert not rules_have_type_ratios(None)


def test_default_type_rules_points_add_up():
    assert sum(v["count"] * v["score"] for v in DEFAULT_TYPE_RULES.values()) == 100


def test_exam_rules_of_returns_full_shape_for_legacy_empty_rules():
    """旧框架 payload 里 final_exam_rules 是空 dict，透传会让前端 .reduce 崩掉。"""
    rules = _exam_rules_of({"final_exam_rules": {}})
    assert rules["question_type_ratios"] == []
    assert rules["chapter_weights"] == []
    assert rules["exam_form"] == ""
    assert rules["total_score"] is None and rules["duration_minutes"] is None


def test_exam_rules_of_handles_missing_and_non_dict():
    for payload in (None, {}, {"final_exam_rules": None}, {"final_exam_rules": "nope"}, {"other": 1}):
        rules = _exam_rules_of(payload)
        assert rules["question_type_ratios"] == []
        assert rules["chapter_weights"] == []


def test_exam_rules_of_preserves_declared_rules():
    payload = {
        "final_exam_rules": {
            "exam_form": "闭卷笔试",
            "duration_minutes": 90,
            "total_score": 100,
            "question_type_ratios": [
                {"question_type": "选择题", "ratio": 20},
                {"question_type": "综合题", "ratio": 30},
            ],
            "chapter_weights": [{"anchor_key": "第1章", "weight": 100}],
        }
    }
    rules = _exam_rules_of(payload)
    assert rules["exam_form"] == "闭卷笔试"
    assert rules["duration_minutes"] == 90
    assert [e["question_type"] for e in rules["question_type_ratios"]] == ["single_choice", "comprehensive"]
    assert abs(sum(e["ratio"] for e in rules["question_type_ratios"]) - 100) < 0.01
    assert rules["chapter_weights"][0]["anchor_key"] == "第1章"


def test_exam_rules_of_preserves_assessment_focus():
    """考试侧重点随考核规则回显：教师改完能在卡片上看到自己的声明。"""
    payload = {
        "final_exam_rules": {
            "assessment_focus": [
                {"assessment_mode": "practical_operation", "weight": 60},
                {"assessment_mode": "conceptual", "weight": 40},
            ]
        }
    }
    rules = _exam_rules_of(payload)
    focus = rules["assessment_focus"]
    assert [e["assessment_mode"] for e in focus] == ["practical_operation", "conceptual"]
    assert abs(sum(e["weight"] for e in focus) - 100) < 0.01


def test_normalize_assessment_focus_filters_unknown_negative_and_normalizes():
    """侧重点：未知考查方式剔除、负值剔除、剩余权重归一到 100。"""
    rules = normalize_exam_rules(
        {
            "assessment_focus": [
                {"assessment_mode": "practical_operation", "weight": 40},
                {"assessment_mode": "theory_recall", "weight": 20},
                {"assessment_mode": "主观题", "weight": 40},
                {"assessment_mode": "conceptual", "weight": -5},
            ]
        },
        anchor_keys=[],
    )
    focus = rules["assessment_focus"]
    assert [e["assessment_mode"] for e in focus] == ["practical_operation", "theory_recall"]
    assert abs(sum(e["weight"] for e in focus) - 100) < 0.01
    assert focus[0]["weight"] > focus[1]["weight"]


def test_normalize_assessment_focus_empty_junk_and_all_zero_mean_balanced():
    """未声明/坏形态/全零都是"均衡"：空表即不干预，蓝图走题型默认分布。"""
    assert normalize_exam_rules(None, anchor_keys=[])["assessment_focus"] == []
    assert normalize_exam_rules({}, anchor_keys=[])["assessment_focus"] == []
    assert normalize_exam_rules({"assessment_focus": "nope"}, anchor_keys=[])["assessment_focus"] == []
    assert normalize_exam_rules(
        {"assessment_focus": [5, {"assessment_mode": "conceptual", "weight": 0}]},
        anchor_keys=[],
    )["assessment_focus"] == []
    # dict 形态同样接受（与题型比例的自由形态输入一致）
    dict_rules = normalize_exam_rules(
        {"assessment_focus": {"conceptual": 100}}, anchor_keys=[]
    )
    assert dict_rules["assessment_focus"] == [{"assessment_mode": "conceptual", "weight": 100.0}]


def test_normalize_exam_rules_keeps_type_formats_for_registered_types():
    rules = normalize_exam_rules(
        {"type_formats": {"单选题": "  新的单选格式  ", "fill_blank": "填空新格式"}},
        anchor_keys=[],
    )
    assert rules["type_formats"] == {
        "single_choice": "新的单选格式",
        "fill_blank": "填空新格式",
    }


def test_normalize_exam_rules_type_formats_drops_junk_and_comprehensive():
    """综合题归原型档案、未知题型/非字符串/空串剔除；全空则不产出键。"""
    rules = normalize_exam_rules(
        {
            "type_formats": {
                "comprehensive": "原型驱动不接受覆盖",
                "bogus_type": "x",
                "essay": "",
                "true_false": 42,
            }
        },
        anchor_keys=[],
    )
    assert "type_formats" not in rules
    # 无覆盖的常规规则同样不产出该键（消费方一律 or {}）
    assert "type_formats" not in normalize_exam_rules(None, anchor_keys=[])


def test_normalize_exam_rules_type_formats_truncates_oversize_template():
    rules = normalize_exam_rules({"type_formats": {"essay": "x" * 3000}}, anchor_keys=[])
    assert rules["type_formats"]["essay"] == "x" * 2000
