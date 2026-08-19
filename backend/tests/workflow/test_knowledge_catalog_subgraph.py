from __future__ import annotations

import pytest

from app.domain.framework.exam_points import ExamPoint, WeightSource
from app.domain.knowledge.models import AssessmentUnitDraft, KnowledgeCardDraft
from app.domain.knowledge.relevance import (
    AssessmentUnitCandidate,
    ContentKind,
    EvidenceDecision,
    ExamPointFileDecision,
    KnowledgeCardCandidate,
    RelevanceClass,
)
from app.services.knowledge_tree_service import KnowledgeTreeValidationError
from app.workflows.knowledge_catalog_subgraph import build_knowledge_catalog_candidate


def _point(code="EP-1", anchor="rag"):
    return ExamPoint(
        code=code,
        anchor_key=anchor,
        title=f"考点{code}",
        assessment_requirement=f"理解{code}",
        weight_value=100,
        weight_source=WeightSource.ASSESSMENT_SYLLABUS,
        weight_group_id=anchor,
        required_evidence_roles=["answer_or_rubric_basis"],
        retrieval_intent=f"检索{code}的事实与评分依据",
    )


def _decision(
    evidence_id,
    *,
    point="EP-1",
    fact="RAG包括检索、上下文构造和生成",
    role="answer_or_rubric_basis",
    relevance=RelevanceClass.DIRECT,
):
    return EvidenceDecision(
        exam_point_code=point,
        evidence_chunk_id=evidence_id,
        relevance_class=relevance,
        support_claim=fact,
        evidence_role=role,
        content_kind=ContentKind.FACT,
        candidate_assessment_unit=AssessmentUnitCandidate(
            code=f"unit-{point}", title="分析RAG流程", performance_statement="能够分析RAG流程"
        ) if relevance is RelevanceClass.DIRECT else None,
        candidate_card_content=KnowledgeCardCandidate(
            name="RAG基本流程",
            performance_statement="能够说明RAG基本流程",
            assessable_content=[fact],
        ) if relevance is RelevanceClass.DIRECT else None,
        prompt_material="可用于设问的场景" if relevance is RelevanceClass.SUPPORTING else None,
        confidence=90,
    )


def _unit(
    evidence_ids,
    *,
    point="EP-1",
    title="分析RAG流程",
    fact="RAG包括检索、上下文构造和生成",
):
    return AssessmentUnitDraft(
        code=f"unit-{point}",
        title=title,
        performance_statement="能够分析RAG流程",
        exam_point_code=point,
        cards=[KnowledgeCardDraft(
            name="RAG基本流程",
            performance_statement="能够说明RAG基本流程",
            assessable_content=[fact],
            evidence_chunk_ids=list(evidence_ids),
        )],
    )


def test_catalog_subgraph_merges_same_point_units_across_files_and_keeps_direct_evidence():
    decisions = [
        ExamPointFileDecision(exam_point_code="EP-1", material_version_id="material-1", decisions=[_decision("e1")]),
        ExamPointFileDecision(exam_point_code="EP-1", material_version_id="material-2", decisions=[_decision("e2")]),
    ]

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[_point()],
        file_decisions=decisions,
        consolidated_units={"EP-1": [_unit(["e1"]), _unit(["e2"], title="分析检索增强生成流程")]},
    )

    assert len(tree.topics) == 1
    assert len(tree.topics[0].units) == 1
    assert tree.topics[0].units[0].cards[0].evidence_chunk_ids == ["e1", "e2"]
    assert tree.coverage[0].status == "sufficient"
    assert tree.coverage[0].direct_count == 2


def test_catalog_subgraph_rejects_card_evidence_not_admitted_direct_for_same_point():
    decisions = [
        ExamPointFileDecision(
            exam_point_code="EP-1",
            material_version_id="material-1",
            decisions=[_decision("e1", relevance=RelevanceClass.SUPPORTING)],
        )
    ]

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        build_knowledge_catalog_candidate(
            framework_version_id="framework-v1",
            exam_points=[_point()],
            file_decisions=decisions,
            consolidated_units={"EP-1": [_unit(["e1"])]},
        )


def test_catalog_subgraph_accepts_owner_qualified_atom_wrapping_evidence_fact():
    """归并为碎片事实补全归属限定后仍须通过证据落地校验。"""

    decisions = [
        ExamPointFileDecision(
            exam_point_code="EP-1",
            material_version_id="material-1",
            decisions=[_decision("e1", fact="eval_batch_size参数用于控制评测批大小")],
        )
    ]

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[_point()],
        file_decisions=decisions,
        consolidated_units={
            "EP-1": [
                _unit(
                    ["e1"],
                    fact="ms-swift框架中，eval_batch_size参数用于控制评测批大小",
                )
            ]
        },
    )

    assert tree.coverage[0].status == "sufficient"
    atom = tree.topics[0].units[0].cards[0].assessable_content[0]
    assert atom.startswith("ms-swift框架中")


def test_catalog_subgraph_still_rejects_atom_without_evidence_core():
    """编造内容（证据核心未逐字出现）依旧被拒。"""

    decisions = [
        ExamPointFileDecision(
            exam_point_code="EP-1",
            material_version_id="material-1",
            decisions=[_decision("e1", fact="eval_batch_size参数用于控制评测批大小")],
        )
    ]

    with pytest.raises(KnowledgeTreeValidationError, match="direct evidence"):
        build_knowledge_catalog_candidate(
            framework_version_id="framework-v1",
            exam_points=[_point()],
            file_decisions=decisions,
            consolidated_units={
                "EP-1": [
                    _unit(["e1"], fact="vLLM框架中，tensor_parallel_size参数用于控制张量并行数")
                ]
            },
        )


def test_catalog_subgraph_marks_missing_answer_or_rubric_basis_insufficient():
    decision = _decision("e1", role="fact")
    point = _point().model_copy(update={"required_evidence_roles": []})

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[point],
        file_decisions=[ExamPointFileDecision(exam_point_code="EP-1", material_version_id="m1", decisions=[decision])],
        consolidated_units={"EP-1": [_unit(["e1"])]},
    )

    assert tree.coverage[0].status == "insufficient"
    assert "missing_answer_or_rubric_basis" in tree.coverage[0].reasons


def test_catalog_subgraph_detects_generic_opposite_direct_claims():
    positive = _decision("e1", fact="系统允许并行检索")
    negative = _decision("e2", fact="系统不允许并行检索")

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[_point()],
        file_decisions=[
            ExamPointFileDecision(exam_point_code="EP-1", material_version_id="m1", decisions=[positive]),
            ExamPointFileDecision(exam_point_code="EP-1", material_version_id="m2", decisions=[negative]),
        ],
        consolidated_units={
            "EP-1": [
                _unit(["e1"], fact="系统允许并行检索"),
                _unit(["e2"], fact="系统不允许并行检索"),
            ]
        },
    )

    assert tree.coverage[0].status == "conflicting"
    assert "conflicting_direct_claims" in tree.coverage[0].reasons


def test_catalog_subgraph_merges_duplicate_unit_codes_without_dropping_answer_boundaries():
    first = _decision("e1", fact="条件A时答案为甲")
    second = _decision("e2", fact="条件B时答案为乙")

    tree = build_knowledge_catalog_candidate(
        framework_version_id="framework-v1",
        exam_points=[_point()],
        file_decisions=[
            ExamPointFileDecision(exam_point_code="EP-1", material_version_id="m1", decisions=[first]),
            ExamPointFileDecision(exam_point_code="EP-1", material_version_id="m2", decisions=[second]),
        ],
        consolidated_units={
            "EP-1": [
                _unit(["e1"], title="判断条件A", fact="条件A时答案为甲"),
                _unit(["e2"], title="判断条件B", fact="条件B时答案为乙"),
            ]
        },
    )

    assert len(tree.topics[0].units) == 1
    card = tree.topics[0].units[0].cards[0]
    assert card.evidence_chunk_ids == ["e1", "e2"]
    assert card.assessable_content == ["条件A时答案为甲", "条件B时答案为乙"]
