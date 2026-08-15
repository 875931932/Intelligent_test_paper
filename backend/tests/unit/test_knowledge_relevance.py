from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    ExamPointKnowledgeConsolidator,
    KnowledgeCardDraft,
    KnowledgeTreeCandidate,
)
from app.domain.knowledge.relevance import (
    EvidenceDecision,
    ExamPointCoverage,
    RelevanceClass,
    admit_evidence_decision,
)


def _exam_point(
    *,
    code: str = "EP-1",
    policy: OperationalDetailPolicy = OperationalDetailPolicy.SUPPORTING_ONLY,
) -> ExamPoint:
    return ExamPoint(
        code=code,
        anchor_key="retrieval-quality",
        title="检索质量分析",
        assessment_requirement="分析检索质量对生成结果的影响",
        weight_value=20,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="rag",
        operational_detail_policy=policy,
        scope_boundary={"exclude": ["超出课程要求的厂商实现"]},
        required_evidence_roles=["fact_or_constraint", "answer_or_rubric_basis"],
        retrieval_intent="检索课程中关于召回质量、约束和诊断依据的内容",
    )


def _decision(**overrides) -> EvidenceDecision:
    payload = {
        "exam_point_code": "EP-1",
        "evidence_chunk_id": "chunk-1",
        "relevance_class": RelevanceClass.DIRECT,
        "support_claim": "该证据给出了能够评分的检索质量判断依据",
        "evidence_role": "fact_or_constraint",
        "content_kind": "conceptual_fact",
        "candidate_assessment_unit": {
            "code": "unit-retrieval-quality",
            "title": "分析检索质量",
            "performance_statement": "能够根据现象分析检索质量",
        },
        "candidate_card_content": {
            "name": "检索质量的影响",
            "assessable_content": ["召回遗漏会削弱回答的事实覆盖"],
        },
        "confidence": 90,
    }
    payload.update(overrides)
    return EvidenceDecision.model_validate(payload)


def test_relevance_contracts_expose_stable_classes_and_coverage_counts():
    coverage = ExamPointCoverage(
        exam_point_code="EP-1",
        direct_count=2,
        supporting_count=1,
        background_count=3,
        out_of_scope_count=4,
        status="sufficient",
        reasons=["事实和评分依据齐全"],
    )

    assert {member.value for member in RelevanceClass} == {
        "direct",
        "supporting",
        "background",
        "out_of_scope",
    }
    assert coverage.direct_count == 2


def test_supporting_only_operational_detail_downgrades_direct_and_clears_products():
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.SUPPORTING_ONLY),
        _decision(
            content_kind="operational_detail",
            prompt_material="给定一个模型配置场景，比较参数选择的影响",
        ),
    )

    assert admitted.relevance_class is RelevanceClass.SUPPORTING
    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None
    assert admitted.prompt_material == "给定一个模型配置场景，比较参数选择的影响"


def test_operational_policy_downgrade_does_not_require_forbidden_direct_products():
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.SUPPORTING_ONLY),
        _decision(
            content_kind="operational_detail",
            candidate_assessment_unit=None,
            candidate_card_content=None,
        ),
    )

    assert admitted.relevance_class is RelevanceClass.SUPPORTING


def test_directly_assessable_operational_detail_remains_direct():
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.DIRECTLY_ASSESSABLE),
        _decision(content_kind="operational_detail"),
    )

    assert admitted.relevance_class is RelevanceClass.DIRECT
    assert admitted.candidate_assessment_unit is not None
    assert admitted.candidate_card_content is not None


def test_forbidden_operational_detail_becomes_out_of_scope_without_products():
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.FORBIDDEN),
        _decision(content_kind="operational_detail", prompt_material="配置场景"),
    )

    assert admitted.relevance_class is RelevanceClass.OUT_OF_SCOPE
    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None
    assert admitted.prompt_material is None


@pytest.mark.parametrize(
    "relevance_class",
    [RelevanceClass.BACKGROUND, RelevanceClass.OUT_OF_SCOPE],
)
def test_background_and_out_of_scope_decisions_never_keep_generation_products(
    relevance_class,
):
    admitted = admit_evidence_decision(
        _exam_point(),
        _decision(
            relevance_class=relevance_class,
            candidate_assessment_unit={"title": "不应保留"},
            candidate_card_content={"name": "不应保留"},
            prompt_material="不应进入生成载荷",
        ),
    )

    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None
    assert admitted.prompt_material is None


def test_supporting_decision_keeps_only_source_free_prompt_material():
    admitted = admit_evidence_decision(
        _exam_point(),
        _decision(
            relevance_class=RelevanceClass.SUPPORTING,
            evidence_role=None,
            candidate_assessment_unit={"title": "不能形成单元"},
            candidate_card_content={"name": "不能形成知识卡"},
            prompt_material="比较两种检索结果在事实覆盖上的差异",
        ),
    )

    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None
    assert admitted.prompt_material == "比较两种检索结果在事实覆盖上的差异"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"candidate_assessment_unit": None}, "assessment unit"),
        ({"candidate_assessment_unit": {}}, "assessment unit"),
        ({"candidate_card_content": None}, "card"),
        ({"candidate_card_content": {}}, "card"),
        ({"evidence_role": None}, "evidence role"),
        ({"evidence_role": "context_only"}, "evidence role"),
        ({"exam_point_code": "EP-OTHER"}, "exam point"),
        ({"confidence": 49}, "confidence"),
    ],
)
def test_direct_decision_rejects_incomplete_out_of_scope_or_low_confidence_input(
    overrides, message
):
    with pytest.raises(ValueError, match=message):
        admit_evidence_decision(_exam_point(), _decision(**overrides))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exam_point_code", " "),
        ("evidence_chunk_id", " "),
        ("support_claim", " "),
        ("content_kind", " "),
        ("confidence", -1),
        ("confidence", 101),
    ],
)
def test_evidence_decision_rejects_blank_identity_fields_and_invalid_confidence(
    field, value
):
    payload = _decision().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        EvidenceDecision.model_validate(payload)


def test_config_filename_is_governed_by_operational_policy_not_a_text_blacklist():
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.SUPPORTING_ONLY),
        _decision(
            content_kind="operational_detail",
            support_claim="config.json 中的参数可作为场景条件",
            candidate_assessment_unit={"code": "config.json", "title": "config.json"},
            candidate_card_content={"name": "config.json"},
        ),
    )

    assert admitted.relevance_class is RelevanceClass.SUPPORTING
    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None


def test_exam_point_declared_custom_fact_role_can_admit_direct_evidence():
    point = _exam_point()
    point.required_evidence_roles = ["formula_or_derivation"]

    admitted = admit_evidence_decision(
        point,
        _decision(evidence_role="formula_or_derivation"),
    )

    assert admitted.relevance_class is RelevanceClass.DIRECT


def test_knowledge_tree_candidate_carries_coverage_and_evidence_decisions():
    decision = _decision()
    coverage = ExamPointCoverage(
        exam_point_code="EP-1",
        direct_count=1,
        supporting_count=0,
        background_count=0,
        out_of_scope_count=0,
        status="sufficient",
    )

    tree = KnowledgeTreeCandidate(
        framework_version_id="framework-1",
        topics=[],
        coverage=[coverage],
        evidence_decisions=[decision],
    )

    assert tree.coverage == [coverage]
    assert tree.evidence_decisions == [decision]


def test_draft_models_keep_legacy_defaults_and_source_free_prompt_material():
    card = KnowledgeCardDraft(
        name="检索质量",
        performance_statement="能够分析检索质量",
        assessable_content=["召回质量影响事实覆盖"],
        prompt_material=["比较两个检索结果"],
    )
    unit = AssessmentUnitDraft(
        code="unit-1",
        title="分析检索质量",
        performance_statement="能够分析检索质量",
    )

    assert card.prompt_material == ["比较两个检索结果"]
    assert unit.exam_point_code == ""


def test_exam_point_knowledge_consolidator_is_a_protocol_contract():
    assert "consolidate" in ExamPointKnowledgeConsolidator.__dict__
