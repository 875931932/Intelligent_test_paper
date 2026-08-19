"""Deterministic knowledge-tree admission, merging, and teacher edits."""

from __future__ import annotations

from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.models import (
    FileKnowledgeCandidate,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
    TreeOperation,
    UnmatchedCandidate,
)
from app.domain.knowledge.relevance import (
    EvidenceDecision,
    RelevanceClass,
    admit_evidence_decision,
    all_facts_supported,
    assessable_fact_keys,
    fact_key_supported,
    semantic_text_key,
    validate_direct_evidence_decision,
)


class KnowledgeTreeValidationError(Exception):
    pass


def _unmatched(material_version_id: str, label: str, reason: str) -> UnmatchedCandidate:
    return UnmatchedCandidate(material_version_id=material_version_id, label=label, reason=reason)


def sanitize_file_candidate(candidate: FileKnowledgeCandidate, *, allowed_anchor_keys: set[str]) -> FileKnowledgeCandidate:
    accepted_topics = []
    unmatched = list(candidate.unmatched)
    for topic in candidate.topics:
        if topic.framework_anchor_key not in allowed_anchor_keys:
            unmatched.append(_unmatched(candidate.material_version_id, topic.name, "framework_anchor_not_allowed"))
            continue
        if not topic.name.strip():
            unmatched.append(_unmatched(candidate.material_version_id, topic.name, "blank_assessable_label"))
            continue
        clean_units = []
        for unit in topic.units:
            if not unit.title.strip():
                unmatched.append(_unmatched(candidate.material_version_id, unit.title, "blank_assessable_label"))
                continue
            clean_cards = []
            for card in unit.cards:
                if not card.name.strip():
                    unmatched.append(_unmatched(candidate.material_version_id, card.name, "blank_assessable_label"))
                    continue
                clean_cards.append(card.model_copy(deep=True))
            if clean_cards:
                clean_units.append(unit.model_copy(update={"cards": clean_cards}, deep=True))
        if clean_units:
            accepted_topics.append(topic.model_copy(update={"units": clean_units}, deep=True))
    return FileKnowledgeCandidate(material_version_id=candidate.material_version_id, topics=accepted_topics, unmatched=unmatched)


def _merge_card(existing: KnowledgeCardDraft, incoming: KnowledgeCardDraft) -> None:
    existing.evidence_chunk_ids = list(dict.fromkeys([*existing.evidence_chunk_ids, *incoming.evidence_chunk_ids]))
    existing.assessable_content = list(dict.fromkeys([*existing.assessable_content, *incoming.assessable_content]))
    existing.prompt_material = list(
        dict.fromkeys([*existing.prompt_material, *incoming.prompt_material])
    )


def merge_file_candidates(
    candidates: list[FileKnowledgeCandidate], *, allowed_anchor_keys: set[str]
) -> KnowledgeTreeCandidate:
    sanitized = [sanitize_file_candidate(item, allowed_anchor_keys=allowed_anchor_keys) for item in candidates]
    topics: list[KnowledgeTopicDraft] = []
    unmatched = []
    for item in sanitized:
        unmatched.extend(item.unmatched)
        for incoming_topic in item.topics:
            topic = next(
                (
                    existing
                    for existing in topics
                    if existing.framework_anchor_key == incoming_topic.framework_anchor_key
                    and semantic_text_key(existing.name)
                    == semantic_text_key(incoming_topic.name)
                ),
                None,
            )
            if topic is None:
                topics.append(incoming_topic.model_copy(deep=True))
                continue
            for incoming_unit in incoming_topic.units:
                unit = next(
                    (
                        existing
                        for existing in topic.units
                        if existing.exam_point_code == incoming_unit.exam_point_code
                        and semantic_text_key(existing.title)
                        == semantic_text_key(incoming_unit.title)
                    ),
                    None,
                )
                if unit is None:
                    topic.units.append(incoming_unit.model_copy(deep=True))
                    continue
                for incoming_card in incoming_unit.cards:
                    card = next(
                        (
                            existing
                            for existing in unit.cards
                            if semantic_text_key(existing.name)
                            == semantic_text_key(incoming_card.name)
                        ),
                        None,
                    )
                    if card is None:
                        unit.cards.append(incoming_card.model_copy(deep=True))
                    else:
                        _merge_card(card, incoming_card)
    return KnowledgeTreeCandidate(framework_version_id="", topics=topics, unmatched=unmatched)


def validate_publishable_tree(
    tree: KnowledgeTreeCandidate,
    *,
    allowed_anchor_keys: set[str],
    allowed_exam_point_codes: set[str] | None = None,
    exam_points_by_code: dict[str, ExamPoint] | None = None,
) -> None:
    if not tree.topics:
        raise KnowledgeTreeValidationError("knowledge tree has no assessable topics")

    strict_exam_point_codes = (
        set(allowed_exam_point_codes)
        if allowed_exam_point_codes is not None
        else None
    )
    if exam_points_by_code is not None:
        for code, point in exam_points_by_code.items():
            if point.code != code:
                raise KnowledgeTreeValidationError(
                    "exam point mapping key does not match exam point code"
                )
        mapping_codes = set(exam_points_by_code)
        strict_exam_point_codes = (
            mapping_codes
            if strict_exam_point_codes is None
            else strict_exam_point_codes & mapping_codes
        )

    admitted_direct_decisions: list[EvidenceDecision] = []
    if strict_exam_point_codes is not None:
        for decision in tree.evidence_decisions:
            normalized_decision = decision
            if exam_points_by_code is not None:
                point = exam_points_by_code.get(decision.exam_point_code)
                if point is None:
                    continue
                try:
                    normalized_decision = admit_evidence_decision(point, decision)
                except ValueError:
                    continue
                if normalized_decision.relevance_class is not RelevanceClass.DIRECT:
                    continue
            elif decision.relevance_class is not RelevanceClass.DIRECT:
                if (
                    decision.candidate_assessment_unit is not None
                    or decision.candidate_card_content is not None
                ):
                    raise KnowledgeTreeValidationError(
                        "non-direct evidence cannot produce a publishable unit or card"
                    )
                continue
            if normalized_decision.exam_point_code not in strict_exam_point_codes:
                continue
            try:
                validate_direct_evidence_decision(
                    normalized_decision,
                    exam_point_code=normalized_decision.exam_point_code,
                )
            except ValueError:
                continue
            admitted_direct_decisions.append(normalized_decision)

    for topic in tree.topics:
        if topic.framework_anchor_key not in allowed_anchor_keys:
            raise KnowledgeTreeValidationError("topic is outside confirmed framework scope")
        if topic.status == "excluded":
            continue
        for unit in topic.units:
            if unit.status == "excluded":
                continue
            if strict_exam_point_codes is not None and (
                not unit.exam_point_code
                or unit.exam_point_code not in strict_exam_point_codes
            ):
                raise KnowledgeTreeValidationError(
                    "active assessment unit requires an allowed exam point"
                )
            active_cards = [card for card in unit.cards if card.status == "active"]
            if not active_cards:
                raise KnowledgeTreeValidationError("active assessment unit requires a knowledge card")
            for card in active_cards:
                if not card.evidence_chunk_ids:
                    raise KnowledgeTreeValidationError("knowledge card requires fact evidence")
                if strict_exam_point_codes is not None:
                    published_facts = assessable_fact_keys(card.assessable_content)
                    supported_facts: set[str] = set()
                    for evidence_id in card.evidence_chunk_ids:
                        evidence_facts: set[str] = set()
                        for decision in admitted_direct_decisions:
                            if (
                                decision.exam_point_code != unit.exam_point_code
                                or decision.evidence_chunk_id != evidence_id
                                or decision.candidate_card_content is None
                            ):
                                continue
                            evidence_facts.update(
                                assessable_fact_keys(
                                    decision.candidate_card_content.assessable_content
                                )
                            )
                        if not evidence_facts or not any(
                            fact_key_supported(published, evidence_facts)
                            for published in published_facts
                        ):
                            raise KnowledgeTreeValidationError(
                                "knowledge card requires a valid direct evidence relation"
                            )
                        supported_facts.update(evidence_facts)
                    if not all_facts_supported(published_facts, supported_facts):
                        raise KnowledgeTreeValidationError(
                            "knowledge card requires a valid direct evidence relation"
                        )


def apply_tree_operations(
    tree: KnowledgeTreeCandidate,
    operations: list[TreeOperation],
    *,
    allowed_anchor_keys: set[str],
) -> KnowledgeTreeCandidate:
    revised = tree.model_copy(deep=True)
    for operation in operations:
        topic = next((item for item in revised.topics if item.code == operation.target_code), None)
        if operation.operation in {"rename_topic", "exclude_topic", "move_topic"}:
            if topic is None:
                raise KnowledgeTreeValidationError("tree operation target was not found")
            if operation.operation == "rename_topic":
                if not operation.value or not operation.value.strip():
                    raise KnowledgeTreeValidationError("topic name is not assessable")
                topic.name = operation.value
            elif operation.operation == "exclude_topic":
                topic.status = "excluded"
            else:
                if operation.value not in allowed_anchor_keys:
                    raise KnowledgeTreeValidationError("cannot move topic outside confirmed framework scope")
                topic.framework_anchor_key = operation.value
            continue
        for parent in revised.topics:
            unit = next((item for item in parent.units if item.code == operation.target_code), None)
            if unit is not None:
                unit.status = "excluded"
                break
        else:
            raise KnowledgeTreeValidationError("tree operation target was not found")
    return revised
