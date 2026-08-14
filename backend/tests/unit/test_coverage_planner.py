import json

import pytest

from app.domain.blueprint.models import PlanItem
from app.domain.generation.coverage import CoveragePlanError, build_coverage_directives, compile_coverage_planning_payload


def _cards():
    return {
        "c1": {
            "name": "检索增强生成",
            "performance_statement": "能够解释检索增强生成流程并分析检索质量的影响",
            "assessable_content": ["文本切分与向量化", "相似度检索", "上下文生成"],
            "preferred_terms": ["训练数据", "文本切分"],
            "scope_boundary": {},
        }
    }


def test_planning_payload_contains_only_pure_semantic_content():
    items = [PlanItem(item_index=1, question_type="short_answer", score=6, anchor_key="rag", unit_id="u1", card_id="c1")]
    payload = compile_coverage_planning_payload(items, _cards())

    text = json.dumps(payload.model_dump(), ensure_ascii=False)

    assert "card_id" not in text
    assert "unit_id" not in text
    assert "anchor_key" not in text
    assert "evidence" not in text.lower()
    assert "检索增强生成" in text


def test_duplicate_coverage_atoms_are_rejected():
    items = [
        PlanItem(item_index=1, question_type="single_choice", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
        PlanItem(item_index=2, question_type="true_false", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
    ]
    raw = {
        "directives": [
            {"item_index": 1, "coverage_atom": "文本切分的作用", "answer_boundary": "形成可检索片段", "preferred_terms": ["文本切分"], "cognitive_level": "understand", "novelty_contract": "只考作用"},
            {"item_index": 2, "coverage_atom": "文本切分的作用", "answer_boundary": "形成可检索片段", "preferred_terms": ["文本切分"], "cognitive_level": "understand", "novelty_contract": "只考作用"},
        ]
    }

    with pytest.raises(CoveragePlanError, match="重复考查原子"):
        build_coverage_directives(items, _cards(), raw)


def test_same_card_reuse_requires_distinct_cognitive_levels():
    items = [
        PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
        PlanItem(item_index=2, question_type="true_false", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
    ]
    raw = {
        "directives": [
            {"item_index": 1, "coverage_atom": "训练数据的定义", "answer_boundary": "训练数据", "cognitive_level": "understand"},
            {"item_index": 2, "coverage_atom": "特征表示的作用", "answer_boundary": "转换为可计算形式", "cognitive_level": "understand"},
        ]
    }

    with pytest.raises(CoveragePlanError, match="认知层次重复"):
        build_coverage_directives(items, _cards(), raw)


def test_fill_blank_is_forced_to_theory_cognitive_levels():
    items = [PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="rag", unit_id="u1", card_id="c1", cognitive_level="apply")]
    raw = {
        "directives": [
            {"item_index": 1, "coverage_atom": "向量化的基本定义", "answer_boundary": "将文本表示为向量", "preferred_terms": ["向量化"], "cognitive_level": "apply", "novelty_contract": "不设计应用场景"}
        ]
    }

    directives = build_coverage_directives(items, _cards(), raw)

    assert directives[0].cognitive_level == "understand"
    assert directives[0].generation_policy["mode"] == "theory_recall"


def test_deepseek_nested_slot_assignments_are_normalized_to_the_contract():
    items = [
        PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
        PlanItem(item_index=2, question_type="true_false", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
    ]
    raw = {
        "directives": {
            "slot_assignments": [
                {"item_index": 1, "question_type": "fill_blank", "cognitive_level": "remember", "assessable_content": "训练数据为模型提供学习样本", "answer": "训练数据", "answer_boundary": "空格处填写训练数据", "uniqueness_notes": "不考特征表示"},
                {"item_index": 2, "question_type": "true_false", "cognitive_level": "understand", "assessable_content": "特征表示将原始信息转换为可计算形式", "answer": "正确", "answer_boundary": "判断为正确", "uniqueness_notes": "不考训练数据"},
            ]
        }
    }

    directives = build_coverage_directives(items, _cards(), raw)

    assert directives[0].coverage_atom == "训练数据为模型提供学习样本"
    assert directives[0].answer_boundary == "训练数据"
    assert directives[0].novelty_contract == "不考特征表示"
    assert directives[1].coverage_atom == "特征表示将原始信息转换为可计算形式"
    assert directives[1].answer_boundary == "特征表示将原始信息转换为可计算形式"


def test_true_false_answer_boundary_is_semantic_not_just_correct_or_wrong():
    item = PlanItem(item_index=1, question_type="true_false", score=2, anchor_key="rag", unit_id="u1", card_id="c1")
    raw = {"directives": [{"item_index": 1, "coverage_atom": "特征表示将信息转换为可计算形式", "answer_boundary": "正确", "cognitive_level": "understand"}]}

    directive = build_coverage_directives([item], _cards(), raw)[0]

    assert directive.answer_boundary == "特征表示将信息转换为可计算形式"
