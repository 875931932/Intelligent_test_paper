from __future__ import annotations

import json

from pydantic import ValidationError

from app.domain.blueprint.models import PlanItem
from app.domain.generation.coverage import build_coverage_directives
from app.schemas.generation import QuestionGenerationPayload, compile_question_generation_payload


_GENERATION_PAYLOAD_FIELDS = {
    "question_type",
    "score",
    "difficulty",
    "cognitive_level",
    "assessment_mode",
    "performance_statement",
    "scope_boundary",
    "assessable_content",
    "prompt_material",
    "coverage_atom",
    "answer_boundary",
    "preferred_terms",
    "novelty_contract",
    "generation_policy",
    "comprehensive_archetype",
    "material_form",
    "cognitive_sequence",
    "subquestion_count_range",
    "subquestion_actions",
    "answer_boundaries",
    "expression_policy",
    "question_template",
    "output_schema",
    "teacher_revision_instruction",
}
_FORBIDDEN_SOURCE_KEYS = {
    "filename",
    "page",
    "page_index",
    "evidence",
    "evidence_ids",
    "material_version_id",
    "exam_point_id",
    "framework_anchor",
    "source_path",
}


def _mapping_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for nested in value.values()
            for key in _mapping_keys(nested)
        }
    if isinstance(value, list):
        return {key for nested in value for key in _mapping_keys(nested)}
    return set()


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
    serialized = payload.model_dump(mode="json")

    assert set(serialized) == _GENERATION_PAYLOAD_FIELDS
    assert _mapping_keys(serialized).isdisjoint(_FORBIDDEN_SOURCE_KEYS)


def test_generation_payload_schema_rejects_source_metadata_even_when_terms_are_allowed():
    payload = compile_question_generation_payload(
        PlanItem(
            item_index=1,
            question_type="single_choice",
            score=2,
            anchor_key="rag",
            unit_id="u1",
            card_id="c1",
        ),
        {
            "performance_statement": "能够说明课程中的文件命名概念",
            "assessable_content": ["文件名可以作为课程概念本身被考查"],
            "preferred_terms": ["文件名", "证据链"],
            "scope_boundary": {},
        },
    )
    serialized = payload.model_dump(mode="json")

    assert serialized["preferred_terms"] == ["文件名", "证据链"]
    assert _mapping_keys(serialized).isdisjoint(_FORBIDDEN_SOURCE_KEYS)
    try:
        QuestionGenerationPayload.model_validate(
            {**serialized, "evidence_ids": ["must-not-enter-generation"]}
        )
    except ValidationError as exc:
        assert "extra_forbidden" in str(exc)
    else:
        raise AssertionError("source metadata must be rejected by the payload schema")


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


def test_comprehensive_payloads_use_distinct_archetype_templates_and_source_free_contracts():
    items = [
        PlanItem(
            item_index=index,
            question_type="comprehensive",
            score=10,
            anchor_key="rag",
            unit_id=f"u{index}",
            card_id=f"c{index}",
            cognitive_level="analyze",
            assessment_mode="problem_solving",
        )
        for index in (1, 2)
    ]
    cards = {
        "c1": {
            "performance_statement": "能够依据异常表现诊断故障",
            "assessable_content": ["故障表现、原因与修正方法"],
            "prompt_material": "异常表现：检索结果与问题无关",
            "scope_boundary": {},
        },
        "c2": {
            "performance_statement": "能够在约束下比较并选择方案",
            "assessable_content": ["候选方案的性能与资源约束"],
            "prompt_material": ["候选方案A与B", "资源上限和质量目标"],
            "scope_boundary": {},
        },
    }
    directives = build_coverage_directives(
        items,
        cards,
        {
            "directives": [
                {
                    "item_index": 1,
                    "coverage_atom": "根据异常表现定位原因并修正",
                    "answer_boundary": "诊断依据、原因与修正措施",
                    "cognitive_level": "analyze",
                    "comprehensive_archetype": "fault_diagnosis",
                    "material_form": "symptom_list",
                    "cognitive_sequence": ["analyze", "apply"],
                    "subquestion_count_range": [2, 3],
                },
                {
                    "item_index": 2,
                    "coverage_atom": "在约束下比较候选方案",
                    "answer_boundary": "比较依据、选择结论与理由",
                    "cognitive_level": "evaluate",
                    "comprehensive_archetype": "comparative_decision",
                    "material_form": "constraint_table",
                    "cognitive_sequence": ["analyze", "evaluate"],
                    "subquestion_count_range": [2, 4],
                },
            ]
        },
    )

    payloads = [compile_question_generation_payload(directive) for directive in directives]
    serialized = json.dumps([payload.model_dump() for payload in payloads], ensure_ascii=False)

    assert payloads[0].prompt_material == ["异常表现：检索结果与问题无关"]
    assert payloads[1].prompt_material == ["候选方案A与B", "资源上限和质量目标"]
    assert "异常表现" in payloads[0].question_template
    assert "候选方案" in payloads[1].question_template
    assert payloads[0].question_template != payloads[1].question_template
    assert payloads[0].output_schema["subquestions"]["items"]["required"] == [
        "action",
        "prompt",
        "answer_boundary",
        "answer",
        "rubric",
        "score",
    ]
    for forbidden in ("card_id", "exam_point_id", "filename", "page", "evidence", "material_version_id"):
        assert forbidden not in serialized.lower()


def test_non_comprehensive_plan_item_remains_backward_compatible_with_new_fields():
    payload = compile_question_generation_payload(
        PlanItem(item_index=1, question_type="true_false", score=2, anchor_key="rag", unit_id="u1", card_id="c1"),
        {
            "performance_statement": "能够判断基本概念",
            "assessable_content": ["基本概念"],
            "scope_boundary": {},
        },
    )

    assert payload.assessment_mode == "conceptual"
    assert payload.prompt_material == []
    assert payload.comprehensive_archetype is None
    assert payload.material_form is None
    assert payload.cognitive_sequence == []
    assert payload.subquestion_count_range is None
