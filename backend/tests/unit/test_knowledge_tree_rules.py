from __future__ import annotations

import pytest

from app.domain.framework.exam_points import (
    ExamPoint,
    OperationalDetailPolicy,
    WeightSource,
)
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    FileKnowledgeCandidate,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
    TreeOperation,
)
from app.services.knowledge_tree_service import (
    KnowledgeTreeValidationError,
    apply_tree_operations,
    merge_file_candidates,
    sanitize_file_candidate,
    validate_publishable_tree,
)


def _card(name="检索增强生成的基本流程", *, evidence=("e1",), content=None):
    return KnowledgeCardDraft(
        name=name,
        performance_statement="能够说明该概念并用于解决问题",
        assessable_content=content or ["检索增强生成包括检索、上下文构造和生成三个阶段"],
        scope_boundary={"exclude": ["具体文件路径"]},
        cognitive_targets=["understand", "apply"],
        allowed_question_types=["single_choice", "short_answer"],
        evidence_chunk_ids=list(evidence),
    )


def _file_candidate(
    *,
    anchor="rag",
    topic="检索增强生成",
    unit="分析RAG流程",
    exam_point_code="",
    cards=None,
):
    return FileKnowledgeCandidate(
        material_version_id="material-v1",
        topics=[
            KnowledgeTopicDraft(
                code="rag-topic",
                name=topic,
                framework_anchor_key=anchor,
                units=[
                    AssessmentUnitDraft(
                        code="rag-flow",
                        title=unit,
                        performance_statement="能够分析检索增强生成流程",
                        scope_boundary={},
                        exam_point_code=exam_point_code,
                        cards=cards or [_card()],
                    )
                ],
            )
        ],
    )


def _exam_point(policy: OperationalDetailPolicy) -> ExamPoint:
    return ExamPoint(
        code="EP-1",
        anchor_key="rag",
        title="检索增强生成流程",
        assessment_requirement="说明并分析检索增强生成流程",
        weight_value=20,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id="rag",
        operational_detail_policy=policy,
        retrieval_intent="检索流程、事实依据和应用条件",
    )


def test_unknown_framework_anchor_is_quarantined_as_unmatched():
    accepted = sanitize_file_candidate(_file_candidate(anchor="outside-exam"), allowed_anchor_keys={"rag"})

    assert accepted.topics == []
    assert len(accepted.unmatched) == 1
    assert accepted.unmatched[0].reason == "framework_anchor_not_allowed"


@pytest.mark.parametrize("label", ["实验报告封面", "config.json", "model.safetensors", "最终提交截图"])
def test_legacy_sanitizer_does_not_apply_course_specific_text_blacklists(label):
    candidate = _file_candidate(unit=label, cards=[_card(name=label)])

    accepted = sanitize_file_candidate(candidate, allowed_anchor_keys={"rag"})

    assert accepted.topics[0].units[0].title == label
    assert accepted.topics[0].units[0].cards[0].name == label
    assert accepted.unmatched == []


def test_synonymous_cross_file_cards_are_merged_without_losing_evidence():
    first = _file_candidate(cards=[_card(evidence=("e1",))])
    second = _file_candidate(cards=[_card(name="RAG的基本流程", evidence=("e2",))])
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    cards = merged.topics[0].units[0].cards
    assert len(cards) == 1
    assert set(cards[0].evidence_chunk_ids) == {"e1", "e2"}


def test_synonymous_cross_file_cards_merge_source_free_prompt_material():
    first = _file_candidate(
        cards=[_card().model_copy(update={"prompt_material": ["场景一"]})]
    )
    second = _file_candidate(
        cards=[
            _card(name="RAG的基本流程").model_copy(
                update={"prompt_material": ["场景二"]}
            )
        ]
    )
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    assert merged.topics[0].units[0].cards[0].prompt_material == ["场景一", "场景二"]


def test_card_merge_key_preserves_language_name_operators():
    first = _file_candidate(cards=[_card(name="C", evidence=("e1",))])
    second = _file_candidate(cards=[_card(name="C++", evidence=("e2",))])
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    assert [card.name for card in merged.topics[0].units[0].cards] == ["C", "C++"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("CO", "Co"),
        ("M", "m"),
        ("X", "x"),
        ("Ａ", "a"),
    ],
)
def test_card_merge_key_preserves_case_sensitive_scientific_semantics(left, right):
    first = _file_candidate(cards=[_card(name=left, evidence=("e1",))])
    second = _file_candidate(cards=[_card(name=right, evidence=("e2",))])
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    assert [card.name for card in merged.topics[0].units[0].cards] == [left, right]


def test_card_merge_key_unifies_fullwidth_ascii_with_same_case():
    first = _file_candidate(cards=[_card(name="Ａ", evidence=("e1",))])
    second = _file_candidate(cards=[_card(name="A", evidence=("e2",))])
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    cards = merged.topics[0].units[0].cards
    assert len(cards) == 1
    assert set(cards[0].evidence_chunk_ids) == {"e1", "e2"}


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("not able", "notable"),
        ("a b", "ab"),
        ("C + +", "C++"),
        ("x²", "x2"),
        ("Fe²⁺", "Fe2+"),
        ("①", "1"),
        ("H₂O", "H2O"),
        ("Ⅰ", "I"),
    ],
)
def test_card_merge_key_preserves_lexical_and_operator_token_boundaries(
    left, right
):
    first = _file_candidate(cards=[_card(name=left, evidence=("e1",))])
    second = _file_candidate(cards=[_card(name=right, evidence=("e2",))])
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    assert [card.name for card in merged.topics[0].units[0].cards] == [left, right]


def test_unit_merge_key_preserves_comparison_direction():
    first = _file_candidate(
        unit="a < b",
        exam_point_code="EP-1",
        cards=[_card(name="less", evidence=("e1",))],
    )
    second = _file_candidate(
        unit="a > b",
        exam_point_code="EP-1",
        cards=[_card(name="greater", evidence=("e2",))],
    )
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    assert [unit.title for unit in merged.topics[0].units] == ["a < b", "a > b"]


def test_merge_keys_unify_width_whitespace_and_explicit_rag_alias():
    first = _file_candidate(
        unit="Ａ ∧ Ｂ",
        exam_point_code="EP-1",
        cards=[_card(name="检索增强生成的基本流程", evidence=("e1",))],
    )
    second = _file_candidate(
        unit="A&&B",
        exam_point_code="EP-1",
        cards=[_card(name="RAG的基本流程", evidence=("e2",))],
    )
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    assert len(merged.topics[0].units) == 1
    assert len(merged.topics[0].units[0].cards) == 1
    assert set(merged.topics[0].units[0].cards[0].evidence_chunk_ids) == {
        "e1",
        "e2",
    }


def test_same_named_units_from_different_exam_points_are_not_merged():
    first = _file_candidate(
        exam_point_code="EP-1",
        cards=[_card(evidence=("ep1-evidence",))],
    )
    second = _file_candidate(
        exam_point_code="EP-2",
        cards=[_card(evidence=("ep2-evidence",))],
    )
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    units = merged.topics[0].units
    assert len(units) == 2
    assert {
        unit.exam_point_code: set(unit.cards[0].evidence_chunk_ids) for unit in units
    } == {
        "EP-1": {"ep1-evidence"},
        "EP-2": {"ep2-evidence"},
    }


def test_tree_without_fact_evidence_cannot_be_published():
    tree = KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=_file_candidate(cards=[_card(evidence=())]).topics,
    )

    with pytest.raises(KnowledgeTreeValidationError, match="evidence"):
        validate_publishable_tree(tree, allowed_anchor_keys={"rag"})


def test_publishable_tree_requires_active_unit_to_reference_allowed_exam_point():
    tree = KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=_file_candidate(exam_point_code="EP-OUTSIDE").topics,
    )

    with pytest.raises(KnowledgeTreeValidationError, match="exam point"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_publishable_tree_requires_direct_evidence_relation_in_strict_exam_point_mode():
    tree = KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=_file_candidate(exam_point_code="EP-1").topics,
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def _strict_tree_with_direct_role(
    evidence_role: str,
    *,
    content_kind: str = "fact",
    candidate_unit_code: str = "rag-flow",
    candidate_card_name: str = "检索增强生成的基本流程",
    candidate_card_content: list[str] | None = None,
) -> KnowledgeTreeCandidate:
    return KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=_file_candidate(exam_point_code="EP-1").topics,
        evidence_decisions=[
            {
                "exam_point_code": "EP-1",
                "evidence_chunk_id": "e1",
                "relevance_class": "direct",
                "support_claim": "该证据支撑知识卡中的可评分事实",
                "evidence_role": evidence_role,
                "content_kind": content_kind,
                "candidate_assessment_unit": {
                    "code": candidate_unit_code,
                    "title": "分析RAG流程",
                    "performance_statement": "能够分析检索增强生成流程",
                },
                "candidate_card_content": {
                    "name": candidate_card_name,
                    "performance_statement": "能够说明该概念并用于解决问题",
                    "assessable_content": candidate_card_content
                    or ["检索增强生成包括检索、上下文构造和生成三个阶段"],
                },
                "confidence": 90,
            }
        ],
    )


def _direct_decision(
    *,
    evidence_chunk_id: str,
    assessable_content: list[str],
    exam_point_code: str = "EP-1",
    unit_code: str = "source-unit-code",
    card_name: str = "来源候选知识卡",
    content_kind: str = "fact",
) -> dict:
    return {
        "exam_point_code": exam_point_code,
        "evidence_chunk_id": evidence_chunk_id,
        "relevance_class": "direct",
        "support_claim": "该证据支撑可评分事实",
        "evidence_role": "fact_or_constraint",
        "content_kind": content_kind,
        "candidate_assessment_unit": {
            "code": unit_code,
            "title": "来源候选单元",
            "performance_statement": "能够分析相关事实",
        },
        "candidate_card_content": {
            "name": card_name,
            "performance_statement": "能够说明并运用相关事实",
            "assessable_content": assessable_content,
        },
        "confidence": 90,
    }


def _strict_tree(
    *,
    card_content: list[str],
    evidence_ids: tuple[str, ...],
    decisions: list[dict],
    unit_code: str = "published-renamed-unit",
    card_name: str = "教师归并后的知识卡",
) -> KnowledgeTreeCandidate:
    candidate = _file_candidate(
        exam_point_code="EP-1",
        cards=[
            _card(
                name=card_name,
                evidence=evidence_ids,
                content=card_content,
            )
        ],
    )
    candidate.topics[0].units[0].code = unit_code
    return KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=candidate.topics,
        evidence_decisions=decisions,
    )


def test_publishable_tree_accepts_card_with_valid_direct_evidence_relation():
    tree = _strict_tree_with_direct_role("answer_or_rubric_basis")

    validate_publishable_tree(
        tree,
        allowed_anchor_keys={"rag"},
        allowed_exam_point_codes={"EP-1"},
    )


def test_publishable_tree_rejects_context_only_raw_direct_relation():
    tree = _strict_tree_with_direct_role("context_only")

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_publishable_tree_rejects_unrelated_candidate_fact():
    tree = _strict_tree_with_direct_role(
        "fact_or_constraint",
        candidate_card_content=["与发布卡片无关的事实"],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_accepts_teacher_renamed_and_recoded_consolidation():
    fact = "检索增强生成包括检索、上下文构造和生成三个阶段"
    tree = _strict_tree(
        card_content=[fact],
        evidence_ids=("e1",),
        decisions=[_direct_decision(evidence_chunk_id="e1", assessable_content=[fact])],
        unit_code="teacher-recoded-unit",
        card_name="教师重命名后的知识卡",
    )

    validate_publishable_tree(
        tree,
        allowed_anchor_keys={"rag"},
        allowed_exam_point_codes={"EP-1"},
    )


def test_strict_publish_rejects_when_any_referenced_evidence_id_is_not_direct():
    fact = "召回遗漏会削弱回答的事实覆盖"
    tree = _strict_tree(
        card_content=[fact],
        evidence_ids=("valid", "bogus"),
        decisions=[
            _direct_decision(evidence_chunk_id="valid", assessable_content=[fact])
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_rejects_extra_unsubstantiated_fact():
    valid_fact = "召回遗漏会削弱回答的事实覆盖"
    tree = _strict_tree(
        card_content=[valid_fact, "提高温度参数必然提升事实准确率"],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=[valid_fact],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_rejects_unsubstantiated_clause_in_one_fact_string():
    valid_fact = "召回遗漏会削弱回答的事实覆盖"
    tree = _strict_tree(
        card_content=[f"{valid_fact}；提高温度参数必然提升事实准确率"],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=[valid_fact],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_rejects_direct_evidence_unrelated_to_all_card_facts():
    valid_fact = "召回遗漏会削弱回答的事实覆盖"
    tree = _strict_tree(
        card_content=[valid_fact],
        evidence_ids=("relevant", "unrelated"),
        decisions=[
            _direct_decision(
                evidence_chunk_id="relevant",
                assessable_content=[valid_fact],
            ),
            _direct_decision(
                evidence_chunk_id="unrelated",
                assessable_content=["向量维度由嵌入模型决定"],
            ),
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_preserves_comparison_direction_during_fact_matching():
    tree = _strict_tree(
        card_content=["a < b"],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=["a > b"],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_does_not_treat_separate_words_as_one_word():
    tree = _strict_tree(
        card_content=["notable"],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=["not able"],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


@pytest.mark.parametrize(
    ("card_fact", "evidence_fact"),
    [
        ("x²", "x2"),
        ("Fe²⁺", "Fe2+"),
        ("①", "1"),
        ("H₂O", "H2O"),
        ("Ⅰ", "I"),
    ],
)
def test_strict_publish_preserves_scientific_and_enumeration_notation(
    card_fact, evidence_fact
):
    tree = _strict_tree(
        card_content=[card_fact],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=[evidence_fact],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


@pytest.mark.parametrize(
    ("card_fact", "evidence_fact"),
    [
        ("CO", "Co"),
        ("M", "m"),
        ("X", "x"),
        ("Ａ", "a"),
    ],
)
def test_strict_publish_preserves_case_sensitive_scientific_semantics(
    card_fact, evidence_fact
):
    tree = _strict_tree(
        card_content=[card_fact],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=[evidence_fact],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_unifies_fullwidth_ascii_with_same_case():
    tree = _strict_tree(
        card_content=["Ａ"],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=["A"],
            )
        ],
    )

    validate_publishable_tree(
        tree,
        allowed_anchor_keys={"rag"},
        allowed_exam_point_codes={"EP-1"},
    )


@pytest.mark.parametrize(
    ("card_fact", "evidence_fact"),
    [
        ("A||B", "A&&B"),
        ("A", "¬A"),
        ("B→A", "A→B"),
    ],
)
def test_strict_publish_preserves_logical_operators_during_fact_matching(
    card_fact, evidence_fact
):
    tree = _strict_tree(
        card_content=[card_fact],
        evidence_ids=("e1",),
        decisions=[
            _direct_decision(
                evidence_chunk_id="e1",
                assessable_content=[evidence_fact],
            )
        ],
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            allowed_exam_point_codes={"EP-1"},
        )


def test_strict_publish_allows_multiple_direct_facts_to_support_one_merged_card():
    first_fact = "召回遗漏会削弱回答的事实覆盖"
    second_fact = "重排可以改善候选上下文的相关性顺序"
    tree = _strict_tree(
        card_content=[first_fact, second_fact],
        evidence_ids=("e1", "e2"),
        decisions=[
            _direct_decision(evidence_chunk_id="e1", assessable_content=[first_fact]),
            _direct_decision(evidence_chunk_id="e2", assessable_content=[second_fact]),
        ],
    )

    validate_publishable_tree(
        tree,
        allowed_anchor_keys={"rag"},
        allowed_exam_point_codes={"EP-1"},
    )


def test_strict_publish_allows_supported_facts_joined_by_safe_semicolon_boundary():
    first_fact = "召回遗漏会削弱回答的事实覆盖"
    second_fact = "重排可以改善候选上下文的相关性顺序"
    tree = _strict_tree(
        card_content=[f"{first_fact}；{second_fact}"],
        evidence_ids=("e1", "e2"),
        decisions=[
            _direct_decision(evidence_chunk_id="e1", assessable_content=[first_fact]),
            _direct_decision(evidence_chunk_id="e2", assessable_content=[second_fact]),
        ],
    )

    validate_publishable_tree(
        tree,
        allowed_anchor_keys={"rag"},
        allowed_exam_point_codes={"EP-1"},
    )


def test_strict_publish_reapplies_supporting_only_operational_policy():
    tree = _strict_tree_with_direct_role(
        "fact_or_constraint",
        content_kind="command",
    )

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        validate_publishable_tree(
            tree,
            allowed_anchor_keys={"rag"},
            exam_points_by_code={
                "EP-1": _exam_point(OperationalDetailPolicy.SUPPORTING_ONLY)
            },
        )


def test_strict_publish_allows_directly_assessable_operational_binding():
    tree = _strict_tree_with_direct_role(
        "fact_or_constraint",
        content_kind="command",
    )

    validate_publishable_tree(
        tree,
        allowed_anchor_keys={"rag"},
        exam_points_by_code={
            "EP-1": _exam_point(OperationalDetailPolicy.DIRECTLY_ASSESSABLE)
        },
    )


def test_legacy_publish_validation_does_not_require_exam_point_relevance_metadata():
    tree = KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=_file_candidate().topics,
        evidence_decisions=[
            {
                "exam_point_code": "legacy-unmapped",
                "evidence_chunk_id": "e1",
                "relevance_class": "direct",
                "support_claim": "旧候选尚未进行考点相关性归并",
                "evidence_role": "context_only",
                "content_kind": "fact",
                "candidate_assessment_unit": {
                    "code": "rag-flow",
                    "title": "分析RAG流程",
                    "performance_statement": "能够分析检索增强生成流程",
                },
                "candidate_card_content": {
                    "name": "检索增强生成的基本流程",
                    "performance_statement": "能够说明该概念并用于解决问题",
                    "assessable_content": [
                        "检索增强生成包括检索、上下文构造和生成三个阶段"
                    ],
                },
                "confidence": 90,
            }
        ],
    )

    validate_publishable_tree(tree, allowed_anchor_keys={"rag"})


def test_teacher_cannot_move_topic_outside_confirmed_framework_scope():
    tree = KnowledgeTreeCandidate(framework_version_id="framework-v1", topics=_file_candidate().topics)

    with pytest.raises(KnowledgeTreeValidationError, match="outside confirmed framework"):
        apply_tree_operations(
            tree,
            [TreeOperation(operation="move_topic", target_code="rag-topic", value="outside-exam")],
            allowed_anchor_keys={"rag"},
        )


def test_teacher_can_rename_and_exclude_tree_nodes():
    tree = KnowledgeTreeCandidate(framework_version_id="framework-v1", topics=_file_candidate().topics)

    revised = apply_tree_operations(
        tree,
        [
            TreeOperation(operation="rename_topic", target_code="rag-topic", value="RAG方法"),
            TreeOperation(operation="exclude_unit", target_code="rag-flow"),
        ],
        allowed_anchor_keys={"rag"},
    )

    assert revised.topics[0].name == "RAG方法"
    assert revised.topics[0].units[0].status == "excluded"
