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
    AssessmentUnitCandidate,
    ContentKind,
    EvidenceDecision,
    ExamPointCoverage,
    KnowledgeCardCandidate,
    RelevanceClass,
    admit_evidence_decision,
    assessable_fact_keys,
    is_transferable_fact,
)


def test_case_narrative_facts_bound_to_experiment_run_are_not_transferable():
    # 案例讲解的叙述背景（绑定特定实验运行）不是可迁移知识，入库前拒绝
    assert not is_transferable_fact("思考模式数据与非思考模式数据分布不均衡的问题出现在上一轮训练中。")
    assert not is_transferable_fact("本次 QLoRA 微调实验使用本地 Qwen3-0.6B 作为基座模型。")
    assert not is_transferable_fact("本次微调实验的训练数据以 JSONL 格式存放。")
    assert not is_transferable_fact("我们的实验中发现数据比例失衡。")


def test_general_mechanism_wording_remains_transferable():
    # 通用机制描述不含情境绑定，不得误伤
    assert is_transferable_fact("迭代式DPO中用上一轮迭代产生的策略进行采样。")
    assert is_transferable_fact("混合训练数据集的构建目标是解决思考模式数据与非思考模式数据分布不均衡的问题。")
    assert is_transferable_fact("LoRA 通过低秩矩阵分解减少可训练参数量。")


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


def _unit_candidate(**overrides):
    payload = {
        "code": "unit-retrieval-quality",
        "title": "分析检索质量",
        "performance_statement": "能够根据现象分析检索质量",
    }
    payload.update(overrides)
    return payload


def _card_candidate(**overrides):
    payload = {
        "name": "检索质量的影响",
        "performance_statement": "能够说明检索质量对事实覆盖的影响",
        "assessable_content": ["召回遗漏会削弱回答的事实覆盖"],
    }
    payload.update(overrides)
    return payload


def _decision(**overrides) -> EvidenceDecision:
    payload = {
        "exam_point_code": "EP-1",
        "evidence_chunk_id": "chunk-1",
        "relevance_class": RelevanceClass.DIRECT,
        "support_claim": "该证据给出了能够评分的检索质量判断依据",
        "evidence_role": "fact_or_constraint",
        "content_kind": "fact",
        "candidate_assessment_unit": _unit_candidate(),
        "candidate_card_content": _card_candidate(),
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
    assert ContentKind.OPERATIONAL_DETAIL.value == "operational_detail"
    assert coverage.direct_count == 2


@pytest.mark.parametrize(
    "content_kind",
    [
        "command",
        "command_or_configuration",
        "configuration",
        "installation_step",
        "installation_or_environment",
        "environment_setup",
        "path",
        "file",
        "filename",
        "file_or_path",
        "procedure",
        "procedural_step",
        "operation",
        "命令",
        "配置",
        "安装步骤",
        "环境配置",
        "路径",
        "文件名",
        "操作步骤",
    ],
)
def test_generic_operational_content_variants_follow_exam_point_policy(content_kind):
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.SUPPORTING_ONLY),
        _decision(content_kind=content_kind),
    )

    assert admitted.content_kind is ContentKind.OPERATIONAL_DETAIL
    assert admitted.relevance_class is RelevanceClass.SUPPORTING


def test_unknown_content_kind_fails_closed_before_direct_admission():
    payload = _decision().model_dump()
    payload["content_kind"] = "vendor_magic_step"

    with pytest.raises(ValidationError, match="content_kind"):
        EvidenceDecision.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "candidate"),
    [
        ("candidate_assessment_unit", {"code": "only-code"}),
        (
            "candidate_assessment_unit",
            _unit_candidate(performance_statement=" "),
        ),
        ("candidate_card_content", {"name": "only-name"}),
        (
            "candidate_card_content",
            _card_candidate(assessable_content=[]),
        ),
        (
            "candidate_card_content",
            _card_candidate(assessable_content=[" "]),
        ),
    ],
)
def test_malformed_candidate_payload_is_rejected_during_model_validation(
    field, candidate
):
    payload = _decision().model_dump()
    payload[field] = candidate

    with pytest.raises(ValidationError, match=field):
        EvidenceDecision.model_validate(payload)


def test_candidate_dicts_are_parsed_into_structured_models():
    decision = _decision()

    assert isinstance(decision.candidate_assessment_unit, AssessmentUnitCandidate)
    assert isinstance(decision.candidate_card_content, KnowledgeCardCandidate)


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


def test_forbidden_operational_detail_overrides_supporting_class_and_clears_prompt():
    admitted = admit_evidence_decision(
        _exam_point(policy=OperationalDetailPolicy.FORBIDDEN),
        _decision(
            relevance_class=RelevanceClass.SUPPORTING,
            content_kind="command",
            candidate_assessment_unit=None,
            candidate_card_content=None,
            prompt_material="运行安装命令",
        ),
    )

    assert admitted.relevance_class is RelevanceClass.OUT_OF_SCOPE
    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None
    assert admitted.prompt_material is None


def test_background_content_kind_overrides_claimed_direct_class_and_clears_products():
    admitted = admit_evidence_decision(
        _exam_point(),
        _decision(
            relevance_class=RelevanceClass.DIRECT,
            content_kind=ContentKind.BACKGROUND,
            prompt_material="课程发展历史",
        ),
    )

    assert admitted.relevance_class is RelevanceClass.BACKGROUND
    assert admitted.candidate_assessment_unit is None
    assert admitted.candidate_card_content is None
    assert admitted.prompt_material is None


def test_background_content_kind_does_not_promote_out_of_scope_evidence():
    admitted = admit_evidence_decision(
        _exam_point(),
        _decision(
            relevance_class=RelevanceClass.OUT_OF_SCOPE,
            content_kind=ContentKind.BACKGROUND,
            prompt_material="课程外发展历史",
        ),
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
            candidate_assessment_unit=_unit_candidate(title="不应保留"),
            candidate_card_content=_card_candidate(name="不应保留"),
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
            candidate_assessment_unit=_unit_candidate(title="不能形成单元"),
            candidate_card_content=_card_candidate(name="不能形成知识卡"),
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
        ({"candidate_card_content": None}, "card"),
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
            candidate_assessment_unit=_unit_candidate(
                code="config.json",
                title="config.json",
            ),
            candidate_card_content=_card_candidate(name="config.json"),
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


def test_fact_keys_unify_equivalent_symbolic_and_operators():
    assert assessable_fact_keys(["A&&B"]) == assessable_fact_keys(["A∧B"])


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a-(b+c)", "a-b+c"),
        ("!(a&&b)", "!a&&b"),
        ("版本1.2", "版本12"),
        ("a＞b", "a＜b"),
        ("C++", "C"),
        ("not able", "notable"),
        ("a b", "ab"),
        ("C + +", "C++"),
        ("a - - b", "a--b"),
        ("a⊕b", "ab"),
        ("x²", "x2"),
        ("Fe²⁺", "Fe2+"),
        ("①", "1"),
        ("H₂O", "H2O"),
        ("Ⅰ", "I"),
        ("CO", "Co"),
        ("M", "m"),
        ("X", "x"),
        ("Ａ", "a"),
        ("RAGFlow", "ragflow"),
    ],
)
def test_fact_keys_preserve_semantically_significant_structure(left, right):
    assert assessable_fact_keys([left]) != assessable_fact_keys([right])


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Ａ　∧（Ｂ ≥ 1.2）", "A&&(B>=1.2)"),
        ("A∨B", "A || B"),
        ("¬A", "! A"),
        ("A⇒B", "A -> B"),
        ("A⇔B", "A <=> B"),
        ("a < b", "a<b"),
        ("a＜b", "a<b"),
        ("( a )", "(a)"),
        ("Ａ", "A"),
        ("检索增强生成", "RAG"),
        ("rag", "RAG"),
    ],
)
def test_fact_keys_unify_whitespace_width_and_explicit_operator_aliases(left, right):
    assert assessable_fact_keys([left]) == assessable_fact_keys([right])
