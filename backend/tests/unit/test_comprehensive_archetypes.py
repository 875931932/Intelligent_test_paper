from __future__ import annotations

import pytest

from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.coverage import CoveragePlanError, _validate_comprehensive_contract


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


def _validate(
    *,
    question_type: str = "comprehensive",
    assessment_mode: str = "problem_solving",
    archetype: str | None = "fault_diagnosis",
    material_form: str | None = "symptom_list",
    cognitive_sequence: list[str] | None = None,
    subquestion_count_range: list[int] | None = None,
    subquestion_actions: list[str] | None = None,
    answer_boundaries: list[str] | None = None,
) -> None:
    _validate_comprehensive_contract(
        question_type=question_type,
        assessment_mode=assessment_mode,
        comprehensive_archetype=archetype,
        material_form=material_form,
        cognitive_sequence=cognitive_sequence if cognitive_sequence is not None else ["analyze", "apply"],
        subquestion_count_range=subquestion_count_range if subquestion_count_range is not None else [2, 3],
        subquestion_actions=subquestion_actions if subquestion_actions is not None else ["定位原因", "提出修正"],
        answer_boundaries=answer_boundaries if answer_boundaries is not None else ["原因", "修正措施"],
    )


@pytest.mark.parametrize(
    ("assessment_mode", "archetype", "material_form", "message"),
    [
        ("conceptual", "fault_diagnosis", "symptom_list", "考查方式不兼容"),
        ("problem_solving", "fault_diagnosis", "constraint_table", "材料形式不兼容"),
    ],
)
def test_archetype_must_match_assessment_mode_and_material_form(assessment_mode, archetype, material_form, message):
    with pytest.raises(CoveragePlanError, match=message):
        _validate(assessment_mode=assessment_mode, archetype=archetype, material_form=material_form)


@pytest.mark.parametrize("count_range", [[1, 2], [2, 5], [4, 3]])
def test_comprehensive_subquestion_range_must_stay_between_two_and_four(count_range):
    with pytest.raises(CoveragePlanError, match="分问范围"):
        _validate(subquestion_count_range=count_range)


@pytest.mark.parametrize("cognitive_sequence", [[], ["analyze", "invent"]])
def test_comprehensive_cognitive_sequence_must_be_non_empty_and_recognized(cognitive_sequence):
    with pytest.raises(CoveragePlanError, match="认知序列"):
        _validate(cognitive_sequence=cognitive_sequence)


def test_non_comprehensive_question_rejects_comprehensive_contract_fields():
    with pytest.raises(CoveragePlanError, match="非综合题"):
        _validate(question_type="short_answer")


def test_code_completion_scenario_requires_exactly_two_subquestions():
    with pytest.raises(CoveragePlanError, match="固定两个分问"):
        _validate(
            archetype="code_completion_scenario",
            material_form="code_skeleton",
            subquestion_count_range=[2, 4],
        )
