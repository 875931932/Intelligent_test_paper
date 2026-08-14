from __future__ import annotations

import json

from app.domain.blueprint.models import PlanItem
from app.domain.generation.coverage import build_coverage_directives
from app.schemas.generation import compile_question_generation_payload


def test_generation_payload_is_source_free():
    payload = compile_question_generation_payload(
        PlanItem(item_index=1, question_type="single_choice", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
        {
            "name": "RAG基本流程",
            "performance_statement": "能够说明RAG流程",
            "assessable_content": ["检索、上下文构造和生成"],
            "scope_boundary": {"exclude": ["文件路径"]},
            "cognitive_targets": ["understand"],
            "allowed_question_types": ["single_choice"],
        },
    )
    text = json.dumps(payload.model_dump(), ensure_ascii=False)
    assert "evidence" not in text.lower()
    assert "filename" not in text.lower()
    assert "page_index" not in text
    assert "assessment_unit_id" not in text


def test_generation_payload_contains_template_and_pure_content():
    payload = compile_question_generation_payload(
        PlanItem(item_index=1, question_type="short_answer", score=6, anchor_key="rag", unit_id="u1", card_id="c1"),
        {"name": "RAG", "performance_statement": "能够分析流程", "assessable_content": ["检索和生成"], "scope_boundary": {}, "cognitive_targets": ["apply"], "allowed_question_types": ["short_answer"]},
    )
    assert payload.question_type == "short_answer"
    assert payload.question_template
    assert payload.assessable_content == ["检索和生成"]


def test_generation_payload_contains_unique_atom_common_terms_and_expression_policy():
    item = PlanItem(item_index=1, question_type="fill_blank", score=2, anchor_key="rag", unit_id="u1", card_id="c1", cognitive_level="apply")
    card = {"performance_statement": "能够说明训练数据的作用", "assessable_content": ["训练数据为模型提供学习样本"], "preferred_terms": ["训练数据"], "scope_boundary": {}}
    directive = build_coverage_directives(
        [item],
        {"c1": card},
        {"directives": [{"item_index": 1, "coverage_atom": "训练数据的基本作用", "answer_boundary": "为模型提供学习样本", "preferred_terms": ["训练数据"], "cognitive_level": "understand", "novelty_contract": "只考查基本作用"}]},
    )[0]

    payload = compile_question_generation_payload(directive)

    assert payload.coverage_atom == "训练数据的基本作用"
    assert payload.answer_boundary == "为模型提供学习样本"
    assert payload.preferred_terms == ["训练数据"]
    assert payload.generation_policy["mode"] == "theory_recall"
    assert payload.expression_policy["max_parenthetical_pairs"] == 1
    assert "理论填空题" in payload.question_template
    assert "不设计实际场景" in payload.question_template
