from __future__ import annotations

import pytest

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


def _file_candidate(*, anchor="rag", topic="检索增强生成", unit="分析RAG流程", cards=None):
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
                        cards=cards or [_card()],
                    )
                ],
            )
        ],
    )


def test_unknown_framework_anchor_is_quarantined_as_unmatched():
    accepted = sanitize_file_candidate(_file_candidate(anchor="outside-exam"), allowed_anchor_keys={"rag"})

    assert accepted.topics == []
    assert len(accepted.unmatched) == 1
    assert accepted.unmatched[0].reason == "framework_anchor_not_allowed"


@pytest.mark.parametrize("label", ["实验报告封面", "config.json", "model.safetensors", "最终提交截图"])
def test_document_metadata_and_submission_labels_cannot_become_l3_or_l4(label):
    candidate = _file_candidate(unit=label, cards=[_card(name=label)])

    accepted = sanitize_file_candidate(candidate, allowed_anchor_keys={"rag"})

    assert accepted.topics == []
    assert accepted.unmatched[0].reason == "non_assessable_document_metadata"


def test_synonymous_cross_file_cards_are_merged_without_losing_evidence():
    first = _file_candidate(cards=[_card(evidence=("e1",))])
    second = _file_candidate(cards=[_card(name="RAG的基本流程", evidence=("e2",))])
    second.material_version_id = "material-v2"

    merged = merge_file_candidates([first, second], allowed_anchor_keys={"rag"})

    cards = merged.topics[0].units[0].cards
    assert len(cards) == 1
    assert set(cards[0].evidence_chunk_ids) == {"e1", "e2"}


def test_tree_without_fact_evidence_cannot_be_published():
    tree = KnowledgeTreeCandidate(
        framework_version_id="framework-v1",
        topics=_file_candidate(cards=[_card(evidence=())]).topics,
    )

    with pytest.raises(KnowledgeTreeValidationError, match="evidence"):
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
