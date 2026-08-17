from __future__ import annotations

import pytest

from app.domain.blueprint.models import PlanItem
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.coverage import CoveragePlanError, build_coverage_directives


def _items(*, question_type: str = "comprehensive", assessment_mode: str = "problem_solving") -> list[PlanItem]:
    return [
        PlanItem(
            item_index=index,
            question_type=question_type,
            score=10,
            anchor_key="rag",
            exam_point_id=f"ep-{index}",
            unit_id=f"unit-{index}",
            card_id=f"card-{index}",
            difficulty="high",
            cognitive_level="analyze",
            assessment_mode=assessment_mode,
        )
        for index in (1, 2, 3)
    ]


def _cards() -> dict[str, dict]:
    return {
        f"card-{index}": {
            "performance_statement": f"能够完成综合任务{index}",
            "assessable_content": [f"综合任务{index}的课程事实"],
            "prompt_material": [f"任务{index}的条件与现象"],
            "scope_boundary": {},
        }
        for index in (1, 2, 3)
    }


def _row(
    item_index: int,
    *,
    archetype: str,
    material_form: str,
    cognitive_sequence: list[str] | None = None,
    subquestion_count_range: list[int] | None = None,
) -> dict:
    return {
        "item_index": item_index,
        "coverage_atom": f"综合考查原子{item_index}",
        "answer_boundary": f"综合答案边界{item_index}",
        "cognitive_level": "analyze",
        "novelty_contract": f"只考查综合任务{item_index}",
        "comprehensive_archetype": archetype,
        "material_form": material_form,
        "cognitive_sequence": cognitive_sequence or ["analyze", "apply"],
        "subquestion_count_range": subquestion_count_range or [2, 3],
    }


def test_archetype_catalog_contains_all_eight_strict_contracts():
    assert set(ARCHETYPE_CONTRACTS) == {
        "code_completion_scenario",
        "case_analysis",
        "fault_diagnosis",
        "comparative_decision",
        "solution_design",
        "process_optimization",
        "critique_correction",
        "integrated_explanation",
    }
    assert ARCHETYPE_CONTRACTS["fault_diagnosis"].allowed_modes == {"problem_solving", "practical_operation"}
    assert ARCHETYPE_CONTRACTS["comparative_decision"].material_forms == {"constraint_table", "option_matrix"}
    assert ARCHETYPE_CONTRACTS["code_completion_scenario"].material_forms == {"code_skeleton", "config_template", "command_script"}
    assert len({contract.question_template for contract in ARCHETYPE_CONTRACTS.values()}) == 8


def test_code_completion_scenario_normalizes_two_fixed_subquestions():
    raw = {
        "directives": [
            _row(
                1,
                archetype="code_completion_scenario",
                material_form="code_skeleton",
                cognitive_sequence=["understand", "apply"],
                subquestion_count_range=[2, 4],
            )
        ]
    }

    directives = build_coverage_directives(_items(assessment_mode="application")[:1], _cards(), raw)

    assert directives[0].subquestion_count_range == [2, 2]
    assert directives[0].subquestion_actions == ["补全代码", "结合场景分析问题并给出改进方向"]


def test_three_comprehensive_slots_require_distinct_structure_contracts():
    raw = {
        "directives": [
            _row(1, archetype="case_analysis", material_form="case_text", cognitive_sequence=["understand", "analyze"]),
            _row(2, archetype="fault_diagnosis", material_form="symptom_list", cognitive_sequence=["analyze", "apply"]),
            _row(3, archetype="comparative_decision", material_form="constraint_table", cognitive_sequence=["analyze", "evaluate"]),
        ]
    }

    directives = build_coverage_directives(_items(), _cards(), raw)

    assert [item.comprehensive_archetype for item in directives] == [
        "case_analysis",
        "fault_diagnosis",
        "comparative_decision",
    ]
    assert len({(item.comprehensive_archetype, item.material_form, tuple(item.cognitive_sequence)) for item in directives}) == 3
    assert all(item.assessment_mode == "problem_solving" for item in directives)
    assert all(item.prompt_material for item in directives)


def test_duplicate_comprehensive_structure_key_is_rejected():
    raw = {
        "directives": [
            _row(1, archetype="case_analysis", material_form="case_text", cognitive_sequence=["understand", "analyze"]),
            _row(2, archetype="case_analysis", material_form="case_text", cognitive_sequence=["understand", "analyze"]),
            _row(3, archetype="fault_diagnosis", material_form="symptom_list"),
        ]
    }

    with pytest.raises(CoveragePlanError, match="综合题结构重复"):
        build_coverage_directives(_items(), _cards(), raw)


@pytest.mark.parametrize(
    ("assessment_mode", "archetype", "material_form", "message"),
    [
        ("conceptual", "fault_diagnosis", "symptom_list", "考查方式不兼容"),
        ("problem_solving", "fault_diagnosis", "constraint_table", "材料形式不兼容"),
    ],
)
def test_archetype_must_match_assessment_mode_and_material_form(assessment_mode, archetype, material_form, message):
    item = _items(assessment_mode=assessment_mode)[0]
    raw = {"directives": [_row(1, archetype=archetype, material_form=material_form)]}

    with pytest.raises(CoveragePlanError, match=message):
        build_coverage_directives([item], _cards(), raw)


@pytest.mark.parametrize("count_range", [[1, 2], [2, 5], [4, 3]])
def test_comprehensive_subquestion_range_must_stay_between_two_and_four(count_range):
    raw = {
        "directives": [
            _row(1, archetype="case_analysis", material_form="case_text", subquestion_count_range=count_range)
        ]
    }

    with pytest.raises(CoveragePlanError, match="分问范围"):
        build_coverage_directives([_items()[0]], _cards(), raw)


@pytest.mark.parametrize("cognitive_sequence", [[], ["analyze", "invent"]])
def test_comprehensive_cognitive_sequence_must_be_non_empty_and_recognized(cognitive_sequence):
    row = _row(1, archetype="case_analysis", material_form="case_text")
    row["cognitive_sequence"] = cognitive_sequence

    with pytest.raises(CoveragePlanError, match="认知序列"):
        build_coverage_directives([_items()[0]], _cards(), {"directives": [row]})


def test_non_comprehensive_question_rejects_comprehensive_contract_fields():
    item = _items(question_type="short_answer")[0]
    raw = {"directives": [_row(1, archetype="case_analysis", material_form="case_text")]}

    with pytest.raises(CoveragePlanError, match="非综合题"):
        build_coverage_directives([item], _cards(), raw)
