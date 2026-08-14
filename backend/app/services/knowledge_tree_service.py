"""Deterministic knowledge-tree admission, merging, and teacher edits."""

from __future__ import annotations

import re

from app.domain.knowledge.models import (
    FileKnowledgeCandidate,
    KnowledgeCardDraft,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
    TreeOperation,
    UnmatchedCandidate,
)


class KnowledgeTreeValidationError(Exception):
    pass


_NON_ASSESSABLE_LABELS = (
    re.compile(r"封面", re.IGNORECASE),
    re.compile(r"(?:最终)?提交", re.IGNORECASE),
    re.compile(r"截图", re.IGNORECASE),
    re.compile(r"\.(?:json|safetensors|bin|ckpt)$", re.IGNORECASE),
)


def _is_non_assessable_label(value: str) -> bool:
    label = value.strip()
    return not label or any(pattern.search(label) for pattern in _NON_ASSESSABLE_LABELS)


def _unmatched(material_version_id: str, label: str, reason: str) -> UnmatchedCandidate:
    return UnmatchedCandidate(material_version_id=material_version_id, label=label, reason=reason)


def sanitize_file_candidate(candidate: FileKnowledgeCandidate, *, allowed_anchor_keys: set[str]) -> FileKnowledgeCandidate:
    accepted_topics = []
    unmatched = list(candidate.unmatched)
    for topic in candidate.topics:
        if topic.framework_anchor_key not in allowed_anchor_keys:
            unmatched.append(_unmatched(candidate.material_version_id, topic.name, "framework_anchor_not_allowed"))
            continue
        if _is_non_assessable_label(topic.name):
            unmatched.append(_unmatched(candidate.material_version_id, topic.name, "non_assessable_document_metadata"))
            continue
        clean_units = []
        for unit in topic.units:
            if _is_non_assessable_label(unit.title):
                unmatched.append(_unmatched(candidate.material_version_id, unit.title, "non_assessable_document_metadata"))
                continue
            clean_cards = []
            for card in unit.cards:
                if _is_non_assessable_label(card.name):
                    unmatched.append(_unmatched(candidate.material_version_id, card.name, "non_assessable_document_metadata"))
                    continue
                clean_cards.append(card.model_copy(deep=True))
            if clean_cards:
                clean_units.append(unit.model_copy(update={"cards": clean_cards}, deep=True))
        if clean_units:
            accepted_topics.append(topic.model_copy(update={"units": clean_units}, deep=True))
    return FileKnowledgeCandidate(material_version_id=candidate.material_version_id, topics=accepted_topics, unmatched=unmatched)


def _canonical(value: str) -> str:
    normalized = value.casefold().replace("检索增强生成", "rag").replace("的", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _merge_card(existing: KnowledgeCardDraft, incoming: KnowledgeCardDraft) -> None:
    existing.evidence_chunk_ids = list(dict.fromkeys([*existing.evidence_chunk_ids, *incoming.evidence_chunk_ids]))
    existing.assessable_content = list(dict.fromkeys([*existing.assessable_content, *incoming.assessable_content]))


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
                    and _canonical(existing.name) == _canonical(incoming_topic.name)
                ),
                None,
            )
            if topic is None:
                topics.append(incoming_topic.model_copy(deep=True))
                continue
            for incoming_unit in incoming_topic.units:
                unit = next((existing for existing in topic.units if _canonical(existing.title) == _canonical(incoming_unit.title)), None)
                if unit is None:
                    topic.units.append(incoming_unit.model_copy(deep=True))
                    continue
                for incoming_card in incoming_unit.cards:
                    card = next((existing for existing in unit.cards if _canonical(existing.name) == _canonical(incoming_card.name)), None)
                    if card is None:
                        unit.cards.append(incoming_card.model_copy(deep=True))
                    else:
                        _merge_card(card, incoming_card)
    return KnowledgeTreeCandidate(framework_version_id="", topics=topics, unmatched=unmatched)


def validate_publishable_tree(tree: KnowledgeTreeCandidate, *, allowed_anchor_keys: set[str]) -> None:
    if not tree.topics:
        raise KnowledgeTreeValidationError("knowledge tree has no assessable topics")
    for topic in tree.topics:
        if topic.framework_anchor_key not in allowed_anchor_keys:
            raise KnowledgeTreeValidationError("topic is outside confirmed framework scope")
        if topic.status == "excluded":
            continue
        for unit in topic.units:
            if unit.status == "excluded":
                continue
            active_cards = [card for card in unit.cards if card.status == "active"]
            if not active_cards:
                raise KnowledgeTreeValidationError("active assessment unit requires a knowledge card")
            for card in active_cards:
                if not card.evidence_chunk_ids:
                    raise KnowledgeTreeValidationError("knowledge card requires fact evidence")


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
                if not operation.value or _is_non_assessable_label(operation.value):
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
