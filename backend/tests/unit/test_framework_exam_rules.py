"""考核大纲考试规则：归一化与按比例推导题型分布。"""
from app.domain.framework.exam_rules import (
    DEFAULT_TYPE_RULES,
    canonical_question_type,
    normalize_exam_rules,
    rules_have_type_ratios,
    type_rules_from_ratios,
)


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


def test_rules_have_type_ratios():
    assert rules_have_type_ratios({"question_type_ratios": [{"question_type": "single_choice", "ratio": 20}]})
    assert not rules_have_type_ratios({})
    assert not rules_have_type_ratios(None)


def test_default_type_rules_points_add_up():
    assert sum(v["count"] * v["score"] for v in DEFAULT_TYPE_RULES.values()) == 100
