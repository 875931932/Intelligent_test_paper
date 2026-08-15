import pytest
from pydantic import ValidationError

from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy, WeightSource
from app.domain.model_calls import ModelCallContext


def _exam_point(**overrides) -> ExamPoint:
    payload = {
        "code": "ep-rag-retrieval",
        "anchor_key": "assessment-rag",
        "title": "RAG retrieval quality",
        "assessment_requirement": "Explain how retrieval quality affects grounded generation.",
        "weight_value": 20,
        "weight_source": WeightSource.ASSESSMENT_SYLLABUS,
        "weight_group_id": "rag",
        "cognitive_targets": ["understand", "apply"],
        "assessment_orientations": ["conceptual", "problem_solving"],
        "allowed_question_types": ["short_answer", "comprehensive"],
        "scope_boundary": {"exclude": ["vendor-specific installation steps"]},
        "required_evidence_roles": ["definition", "worked_example"],
        "retrieval_intent": "Retrieve course explanations and examples about retrieval quality.",
        "assessment_anchor_keys": ["assessment-rag"],
        "teaching_anchor_keys": ["teaching-rag"],
    }
    payload.update(overrides)
    return ExamPoint.model_validate(payload)


def test_exam_point_preserves_assessment_weight_source_and_operational_policy():
    point = _exam_point(operational_detail_policy=OperationalDetailPolicy.DIRECTLY_ASSESSABLE)

    assert point.weight_source is WeightSource.ASSESSMENT_SYLLABUS
    assert point.operational_detail_policy is OperationalDetailPolicy.DIRECTLY_ASSESSABLE
    assert point.priority == "normal"
    assert point.status == "candidate"


def test_exam_point_rejects_blank_assessment_requirement():
    with pytest.raises(ValidationError, match="assessment_requirement"):
        _exam_point(assessment_requirement="   \n")


def test_exam_point_strips_required_narrative_fields():
    point = _exam_point(
        assessment_requirement="  Explain retrieval quality.  ",
        retrieval_intent="  Retrieve grounded course evidence.  ",
    )

    assert point.assessment_requirement == "Explain retrieval quality."
    assert point.retrieval_intent == "Retrieve grounded course evidence."


def test_exam_point_declares_all_supported_weight_sources_and_operational_policies():
    assert {member.value for member in WeightSource} == {
        "assessment_syllabus",
        "inherited_group",
        "teacher_confirmed",
    }
    assert {member.value for member in OperationalDetailPolicy} == {
        "forbidden",
        "supporting_only",
        "directly_assessable",
    }


def test_model_call_context_carries_stage_and_optional_workflow_ids():
    context = ModelCallContext(
        course_id="course-1",
        framework_build_run_id="framework-run-1",
        organization_run_id=None,
        generation_attempt_id="attempt-1",
        stage="exam_point_extraction",
    )

    assert context.model_dump() == {
        "course_id": "course-1",
        "framework_build_run_id": "framework-run-1",
        "organization_run_id": None,
        "generation_attempt_id": "attempt-1",
        "stage": "exam_point_extraction",
    }
