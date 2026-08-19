"""Build a source-separated catalogue candidate per confirmed exam point."""

from __future__ import annotations

import re
from collections import defaultdict

from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    FileKnowledgeCandidate,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
)
from app.domain.knowledge.relevance import (
    EvidenceDecision,
    ExamPointCoverage,
    ExamPointFileDecision,
    RelevanceClass,
    admit_evidence_decision,
    all_facts_supported,
    assessable_fact_keys,
    fact_key_supported,
    semantic_text_key,
)
from app.services.knowledge_tree_service import (
    KnowledgeTreeValidationError,
    merge_file_candidates,
    validate_publishable_tree,
)


_ANSWER_BASIS_ROLES = frozenset(
    {
        "answer",
        "answer_basis",
        "rubric",
        "rubric_basis",
        "answer_or_rubric_basis",
        "scoring",
        "scoring_basis",
    }
)
_NEGATION_PATTERN = re.compile(
    r"(?<![A-Za-z])(not|never|without)(?![A-Za-z])|禁止|并非|不是|不(?=[\u3400-\u9fff])|非",
    re.IGNORECASE,
)


def _claim_polarity_key(value: str) -> tuple[str, bool]:
    normalized = semantic_text_key(value)
    negative = bool(_NEGATION_PATTERN.search(normalized))
    base = _NEGATION_PATTERN.sub("", normalized)
    return base, negative


def _has_conflicting_direct_claims(decisions: list[EvidenceDecision]) -> bool:
    polarities: dict[str, set[bool]] = defaultdict(set)
    answer_boundaries: dict[tuple[str, str], set[str]] = defaultdict(set)
    for decision in decisions:
        if decision.relevance_class is not RelevanceClass.DIRECT:
            continue
        card = decision.candidate_card_content
        unit = decision.candidate_assessment_unit
        if card is None or unit is None:
            continue
        for fact in card.assessable_content:
            base, negative = _claim_polarity_key(fact)
            if base:
                polarities[base].add(negative)
        boundary = card.scope_boundary.get("answer_boundary")
        if boundary is not None:
            answer_boundaries[(semantic_text_key(unit.title), semantic_text_key(card.name))].add(
                semantic_text_key(str(boundary))
            )
    return any(len(values) > 1 for values in polarities.values()) or any(
        len(values) > 1 for values in answer_boundaries.values()
    )


def _coverage(
    point: ExamPoint,
    decisions: list[EvidenceDecision],
    *,
    additional_reasons: list[str],
) -> ExamPointCoverage:
    counts = {member: 0 for member in RelevanceClass}
    for decision in decisions:
        counts[decision.relevance_class] += 1
    reasons = list(dict.fromkeys(additional_reasons))
    direct = [item for item in decisions if item.relevance_class is RelevanceClass.DIRECT]
    if not decisions and "no_recalled_evidence" not in reasons:
        reasons.append("no_recalled_evidence")
    if not direct:
        reasons.append("no_direct_evidence")
    if not any(
        (item.evidence_role or "").strip().casefold() in _ANSWER_BASIS_ROLES
        for item in direct
    ):
        reasons.append("missing_answer_or_rubric_basis")
    conflicting = _has_conflicting_direct_claims(direct)
    if conflicting:
        reasons.append("conflicting_direct_claims")
    reasons = list(dict.fromkeys(reasons))
    if conflicting:
        status = "conflicting"
    elif direct and "missing_answer_or_rubric_basis" not in reasons and not any(
        reason.endswith("_failed") for reason in reasons
    ):
        status = "sufficient"
    else:
        status = "insufficient"
    return ExamPointCoverage(
        exam_point_code=point.code,
        direct_count=counts[RelevanceClass.DIRECT],
        supporting_count=counts[RelevanceClass.SUPPORTING],
        background_count=counts[RelevanceClass.BACKGROUND],
        out_of_scope_count=counts[RelevanceClass.OUT_OF_SCOPE],
        status=status,
        reasons=reasons,
    )


def validate_consolidated_units(
    point: ExamPoint,
    units: list[AssessmentUnitDraft],
    admitted_direct: list[EvidenceDecision],
) -> None:
    decisions_by_evidence: dict[str, list[EvidenceDecision]] = defaultdict(list)
    for decision in admitted_direct:
        decisions_by_evidence[decision.evidence_chunk_id].append(decision)
    for unit in units:
        if unit.exam_point_code != point.code:
            raise KnowledgeTreeValidationError(
                "consolidated assessment unit must remain bound to its exam point"
            )
        for card in unit.cards:
            published_facts = assessable_fact_keys(card.assessable_content)
            supported_facts: set[str] = set()
            for evidence_id in card.evidence_chunk_ids:
                evidence_facts: set[str] = set()
                for decision in decisions_by_evidence.get(evidence_id, []):
                    if decision.candidate_card_content is not None:
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
                        "knowledge card requires direct evidence admitted for the same exam point"
                    )
                supported_facts.update(evidence_facts)
            if not all_facts_supported(published_facts, supported_facts):
                raise KnowledgeTreeValidationError(
                    "knowledge card requires direct evidence admitted for the same exam point"
                )


def _merge_consolidated_units(units: list[AssessmentUnitDraft]) -> list[AssessmentUnitDraft]:
    merged: list[AssessmentUnitDraft] = []
    for incoming in units:
        unit = next(
            (
                existing
                for existing in merged
                if existing.code == incoming.code
                or semantic_text_key(existing.title) == semantic_text_key(incoming.title)
            ),
            None,
        )
        if unit is None:
            merged.append(incoming.model_copy(deep=True))
            continue
        for incoming_card in incoming.cards:
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
                continue
            card.evidence_chunk_ids = list(
                dict.fromkeys([*card.evidence_chunk_ids, *incoming_card.evidence_chunk_ids])
            )
            card.assessable_content = list(
                dict.fromkeys([*card.assessable_content, *incoming_card.assessable_content])
            )
            card.prompt_material = list(
                dict.fromkeys([*card.prompt_material, *incoming_card.prompt_material])
            )
            if card.scope_boundary != incoming_card.scope_boundary:
                card.status = "needs_teacher_review"
    return merged


def build_knowledge_catalog_candidate(
    *,
    framework_version_id: str,
    exam_points: list[ExamPoint],
    file_decisions: list[ExamPointFileDecision],
    consolidated_units: dict[str, list[AssessmentUnitDraft]],
    coverage_reasons: dict[str, list[str]] | None = None,
) -> KnowledgeTreeCandidate:
    """Build one deterministic candidate from already selected exam-point/file pairs."""

    points_by_code = {point.code: point for point in exam_points}
    if len(points_by_code) != len(exam_points):
        raise KnowledgeTreeValidationError("exam point codes must be unique")
    admitted_by_point: dict[str, list[EvidenceDecision]] = defaultdict(list)
    for file_decision in sorted(
        file_decisions,
        key=lambda item: (item.exam_point_code, item.material_version_id),
    ):
        point = points_by_code.get(file_decision.exam_point_code)
        if point is None:
            raise KnowledgeTreeValidationError("file decision references an unknown exam point")
        for decision in sorted(file_decision.decisions, key=lambda item: item.evidence_chunk_id):
            try:
                admitted = admit_evidence_decision(point, decision)
            except ValueError as exc:
                raise KnowledgeTreeValidationError(str(exc)) from exc
            admitted_by_point[point.code].append(admitted)

    candidates: list[FileKnowledgeCandidate] = []
    for point in sorted(exam_points, key=lambda item: item.code):
        units = _merge_consolidated_units(
            [item.model_copy(deep=True) for item in consolidated_units.get(point.code, [])]
        )
        direct = [
            item
            for item in admitted_by_point.get(point.code, [])
            if item.relevance_class is RelevanceClass.DIRECT
        ]
        validate_consolidated_units(point, units, direct)
        if not units:
            continue
        candidates.append(
            FileKnowledgeCandidate(
                material_version_id=f"exam-point:{point.code}",
                topics=[
                    KnowledgeTopicDraft(
                        code=f"topic-{point.anchor_key}",
                        name=point.anchor_key,
                        framework_anchor_key=point.anchor_key,
                        units=units,
                    )
                ],
            )
        )

    allowed_anchor_keys = {point.anchor_key for point in exam_points}
    if candidates:
        tree = merge_file_candidates(candidates, allowed_anchor_keys=allowed_anchor_keys)
        tree.framework_version_id = framework_version_id
    else:
        tree = KnowledgeTreeCandidate(framework_version_id=framework_version_id, topics=[])
    tree.evidence_decisions = sorted(
        (decision for decisions in admitted_by_point.values() for decision in decisions),
        key=lambda item: (
            item.exam_point_code,
            item.evidence_chunk_id,
            item.relevance_class.value,
        ),
    )
    reasons = coverage_reasons or {}
    tree.coverage = [
        _coverage(
            point,
            admitted_by_point.get(point.code, []),
            additional_reasons=reasons.get(point.code, []),
        )
        for point in sorted(exam_points, key=lambda item: item.code)
    ]
    if tree.topics:
        validate_publishable_tree(
            tree,
            allowed_anchor_keys=allowed_anchor_keys,
            allowed_exam_point_codes=set(points_by_code),
            exam_points_by_code=points_by_code,
        )
    return tree
