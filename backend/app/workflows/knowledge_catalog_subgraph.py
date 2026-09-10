"""Build a source-separated catalogue candidate per confirmed exam point."""

from __future__ import annotations

from collections import defaultdict

from app.adapters.model.deepseek_gateway import DeepSeekModelError
from app.adapters.model.deepseek_semantic_extractors import (
    validate_consolidated_units,
)
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    FileKnowledgeCandidate,
    KnowledgeTopicDraft,
    KnowledgeTreeCandidate,
)
from app.domain.knowledge.relevance import (
    EvidenceDecision,
    ExamPointFileDecision,
    RelevanceClass,
    StagingChunk,
    admit_evidence_decision,
    semantic_text_key,
)
from app.services.knowledge_tree_service import (
    KnowledgeTreeValidationError,
    compute_exam_point_coverage,
    merge_file_candidates,
    validate_publishable_tree,
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
    chunks_by_id: dict[str, StagingChunk] | None = None,
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
                # 低置信度决策在分类阶段已被保守降级为 out_of_scope（confidence
                # 字段保持原值），这里的准入校验只是复检：跳过即可，与发布侧
                # knowledge_tree_service 的「不可准入则 continue」口径一致。
                # 若在此整树判死，前面分类+归并的全部模型成本因一条边缘决策
                # 白费（生产事故：run 45ec7e13 崩于本节点）。
                if "confidence is below" in str(exc):
                    continue
                raise KnowledgeTreeValidationError(str(exc)) from exc
            admitted_by_point[point.code].append(admitted)

    reasons = coverage_reasons or {}
    candidates: list[FileKnowledgeCandidate] = []
    for point in sorted(exam_points, key=lambda item: item.code):
        units = _merge_consolidated_units(
            [item.model_copy(deep=True) for item in consolidated_units.get(point.code, [])]
        )
        # 准入素材 = direct + supporting（background/out_of_scope 不是知识点来源）
        admitted = [
            item
            for item in admitted_by_point.get(point.code, [])
            if item.relevance_class in {RelevanceClass.DIRECT, RelevanceClass.SUPPORTING}
        ]
        try:
            validate_consolidated_units(point, admitted, units, chunks_by_id=chunks_by_id)
        except DeepSeekModelError as exc:
            # 归并适配层失败（引用缺口、编造事实等）按树校验失败处理，
            # 保持构建器「确定性输入 → KnowledgeTreeValidationError」的契约。
            raise KnowledgeTreeValidationError(str(exc)) from exc
        if not units:
            if admitted:
                reasons.setdefault(point.code, []).append("no_cards_produced")
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
    tree.coverage = [
        compute_exam_point_coverage(
            point.code,
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
            chunks_by_id=chunks_by_id,
        )
    return tree
