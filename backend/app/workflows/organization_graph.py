"""LangGraph orchestration for exam-point-led material organization."""

from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.config import settings
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.models import (
    AssessmentUnitDraft,
    ExamPointKnowledgeConsolidator,
    KnowledgeRepository,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.domain.knowledge.relevance import (
    ExamPointEvidenceClassifier,
    ExamPointFileDecision,
    RelevanceClass,
    StagingChunk,
    admit_evidence_decision,
)
from app.domain.model_calls import ModelCallContext
from app.services.knowledge_tree_service import apply_tree_operations, validate_publishable_tree
from app.services.staging_retrieval_service import HybridStagingRetriever
from app.workflows.knowledge_catalog_subgraph import (
    build_knowledge_catalog_candidate,
    validate_consolidated_units,
)


class OrganizationState(TypedDict, total=False):
    course_id: str
    run_id: str
    framework_version_id: str
    framework_anchors: list[dict]
    exam_points: list[dict]
    material_version_ids: list[str]
    evidence_chunk_ids: list[str]
    frozen_input: dict
    retrieval_pairs: list[dict]
    file_decisions: list[dict]
    failed_pairs: list[dict]
    coverage_reasons: dict[str, list[str]]
    consolidated_units: dict[str, list[dict]]
    tree: dict
    candidate_id: str
    confirmation: dict
    catalog_version_id: str
    index_version_id: str


_CREDENTIAL_KEY_PATTERN = (
    r"(?:api[ _-]?(?:key|token)|access[ _-]?token|client[ _-]?secret|"
    r"password|passwd|token|secret)"
)
_SECRET_KEY_PATTERN = rf"(?:{_CREDENTIAL_KEY_PATTERN}|authorization)"
_DOUBLE_QUOTED_SECRET_PATTERN = re.compile(
    rf'(?i)(?P<prefix>[\"\']?{_SECRET_KEY_PATTERN}[\"\']?\s*[:=]\s*)'
    r'"(?:\\.|[^"\\])*"'
)
_SINGLE_QUOTED_SECRET_PATTERN = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SECRET_KEY_PATTERN}[\"']?\s*[:=]\s*)"
    r"'(?:\\.|[^'\\])*'"
)
_UNTERMINATED_DOUBLE_QUOTED_SECRET_PATTERN = re.compile(
    rf'(?im)(?P<prefix>[\"\']?{_SECRET_KEY_PATTERN}[\"\']?\s*[:=]\s*)'
    r'"(?:\\.|[^"\\\r\n])*\\?$'
)
_UNTERMINATED_SINGLE_QUOTED_SECRET_PATTERN = re.compile(
    rf"(?im)(?P<prefix>[\"']?{_SECRET_KEY_PATTERN}[\"']?\s*[:=]\s*)"
    r"'(?:\\.|[^'\\\r\n])*\\?$"
)
_BEARER_SECRET_PATTERN = re.compile(r"(?i)(\bbearer\s+)[^\s,;}\]\"']+")
_UNQUOTED_SECRET_PATTERN = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SECRET_KEY_PATTERN}[\"']?\s*[:=]\s*)"
    r"(?![\"'])[^\r\n]*"
)


def _redacted_error_message(exc: Exception) -> str:
    raw_message = str(exc)
    redacted = _DOUBLE_QUOTED_SECRET_PATTERN.sub(
        lambda match: f'{match.group("prefix")}"[REDACTED]"',
        raw_message,
    )
    redacted = _SINGLE_QUOTED_SECRET_PATTERN.sub(
        lambda match: f"{match.group('prefix')}'[REDACTED]'",
        redacted,
    )
    redacted = _UNTERMINATED_DOUBLE_QUOTED_SECRET_PATTERN.sub(
        lambda match: f'{match.group("prefix")}"[REDACTED]',
        redacted,
    )
    redacted = _UNTERMINATED_SINGLE_QUOTED_SECRET_PATTERN.sub(
        lambda match: f"{match.group('prefix')}'[REDACTED]",
        redacted,
    )
    redacted = _BEARER_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        redacted,
    )
    redacted = _UNQUOTED_SECRET_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        redacted,
    )
    message = " ".join(redacted.split())[:500]
    if not message:
        return exc.__class__.__name__
    return message


def _failure(*, stage: str, point_code: str, material_version_id: str | None, exc: Exception) -> dict:
    fallback_code = "classification_failed" if stage == "classification" else "consolidation_failed"
    adapter_code = getattr(exc, "error_code", None)
    error_code = (
        adapter_code
        if isinstance(adapter_code, str)
        and re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", adapter_code)
        else fallback_code
    )
    return {
        "stage": stage,
        "exam_point_code": point_code,
        "material_version_id": material_version_id,
        "error_code": error_code,
        "error_message": _redacted_error_message(exc),
    }


def build_organization_graph(
    retriever: HybridStagingRetriever,
    classifier: ExamPointEvidenceClassifier,
    consolidator: ExamPointKnowledgeConsolidator,
    repository: KnowledgeRepository,
    *,
    checkpointer=None,
):
    def _points(state: OrganizationState) -> list[ExamPoint]:
        return sorted(
            (ExamPoint.model_validate(item) for item in state["exam_points"]),
            key=lambda item: item.code,
        )

    def _chunks(state: OrganizationState, ids: list[str] | None = None) -> list[StagingChunk]:
        requested = ids if ids is not None else state["evidence_chunk_ids"]
        chunks = repository.load_evidence_chunks(
            course_id=state["course_id"],
            run_id=state["run_id"],
            evidence_chunk_ids=requested,
        )
        return sorted(chunks, key=lambda item: (item.material_version_id, item.id))

    def validate_inputs(state: OrganizationState):
        if not state.get("framework_version_id") or not state.get("framework_anchors"):
            raise ValueError("a published framework with anchors is required")
        if not state.get("exam_points"):
            raise ValueError("at least one confirmed exam point is required")
        material_ids = state.get("material_version_ids") or []
        if not material_ids or len(material_ids) != len(set(material_ids)):
            raise ValueError("material versions must be present and unique")
        evidence_ids = state.get("evidence_chunk_ids") or []
        if not evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence snapshot must be present and unique")
        if len({point.code for point in _points(state)}) != len(state["exam_points"]):
            raise ValueError("exam point codes must be unique")
        return {}

    def freeze_selected_materials(state: OrganizationState):
        expected = {
            "framework_version_id": state["framework_version_id"],
            "exam_point_ids": list(state["frozen_input"].get("exam_point_ids") or []),
            "material_version_ids": list(state["material_version_ids"]),
        }
        if state.get("frozen_input") != expected:
            raise ValueError("organization input snapshot is not frozen")
        return {"frozen_input": expected}

    def retrieve_per_exam_point(state: OrganizationState):
        chunks = _chunks(state)
        chunks_by_material: dict[str, list[StagingChunk]] = defaultdict(list)
        for chunk in chunks:
            chunks_by_material[chunk.material_version_id].append(chunk)
        pairs: list[dict] = []
        coverage_reasons: dict[str, list[str]] = defaultdict(list)
        for point in _points(state):
            point_has_recall = False
            for material_version_id in sorted(chunks_by_material):
                material_chunks = chunks_by_material[material_version_id]
                allowed_ids = {chunk.id for chunk in material_chunks}
                ranked = sorted(
                    retriever.retrieve(point, material_chunks),
                    key=lambda item: (-item.score, item.chunk.id),
                )
                recalled_ids: list[str] = []
                for item in ranked:
                    chunk = item.chunk
                    if (
                        chunk.id not in allowed_ids
                        or chunk.material_version_id != material_version_id
                    ):
                        raise ValueError(
                            "retriever returned evidence outside its exam-point material pair"
                        )
                    if chunk.id not in recalled_ids:
                        recalled_ids.append(chunk.id)
                    if len(recalled_ids) == settings.organization_retrieval_top_k:
                        break
                if recalled_ids:
                    point_has_recall = True
                    pairs.append(
                        {
                            "exam_point_code": point.code,
                            "material_version_id": material_version_id,
                            "evidence_chunk_ids": recalled_ids,
                        }
                    )
            if not point_has_recall:
                coverage_reasons[point.code].append("no_recalled_evidence")
        return {
            "retrieval_pairs": pairs,
            "coverage_reasons": dict(coverage_reasons),
        }

    def classify_exam_point_file_pairs(state: OrganizationState):
        points = {point.code: point for point in _points(state)}
        chunk_ids = sorted(
            {
                chunk_id
                for pair in state.get("retrieval_pairs", [])
                for chunk_id in pair["evidence_chunk_ids"]
            }
        )
        chunks_by_id = {chunk.id: chunk for chunk in _chunks(state, chunk_ids)}
        pairs = sorted(
            state.get("retrieval_pairs", []),
            key=lambda item: (item["exam_point_code"], item["material_version_id"]),
        )

        def classify_pair(pair: dict) -> ExamPointFileDecision:
            point = points[pair["exam_point_code"]]
            chunks = [chunks_by_id[chunk_id] for chunk_id in pair["evidence_chunk_ids"]]
            if any(chunk.material_version_id != pair["material_version_id"] for chunk in chunks):
                raise ValueError("classification pair contains chunks from another material")
            decision = classifier.classify(
                exam_point=point,
                material_version_id=pair["material_version_id"],
                chunks=chunks,
                call_context=ModelCallContext(
                    course_id=state["course_id"],
                    organization_run_id=state["run_id"],
                    stage="classify_exam_point_file_pair",
                ),
            )
            validated = ExamPointFileDecision.model_validate(decision)
            if (
                validated.exam_point_code != point.code
                or validated.material_version_id != pair["material_version_id"]
            ):
                raise ValueError("classification response does not match its pair")
            admitted = [admit_evidence_decision(point, item) for item in validated.decisions]
            allowed_ids = set(pair["evidence_chunk_ids"])
            if any(item.evidence_chunk_id not in allowed_ids for item in admitted):
                raise ValueError("classification response references evidence outside its pair")
            decision_ids = [item.evidence_chunk_id for item in admitted]
            if len(decision_ids) != len(set(decision_ids)):
                raise ValueError("classification response contains duplicate evidence decisions")
            return validated.model_copy(update={"decisions": admitted})

        decisions: list[ExamPointFileDecision] = []
        failures: list[dict] = list(state.get("failed_pairs") or [])
        coverage_reasons = {
            code: list(values)
            for code, values in (state.get("coverage_reasons") or {}).items()
        }
        with ThreadPoolExecutor(max_workers=settings.organization_max_workers) as executor:
            future_pairs = {executor.submit(classify_pair, pair): pair for pair in pairs}
            for future in as_completed(future_pairs):
                pair = future_pairs[future]
                try:
                    decisions.append(future.result())
                except Exception as exc:
                    failures.append(
                        _failure(
                            stage="classification",
                            point_code=pair["exam_point_code"],
                            material_version_id=pair["material_version_id"],
                            exc=exc,
                        )
                    )
                    coverage_reasons.setdefault(pair["exam_point_code"], []).append(
                        "classification_failed"
                    )
        decisions.sort(key=lambda item: (item.exam_point_code, item.material_version_id))
        failures.sort(
            key=lambda item: (
                item["exam_point_code"],
                item.get("material_version_id") or "",
                item["stage"],
            )
        )
        return {
            "file_decisions": [item.model_dump(mode="json") for item in decisions],
            "failed_pairs": failures,
            "coverage_reasons": coverage_reasons,
        }

    def consolidate_per_exam_point(state: OrganizationState):
        points = _points(state)
        decisions = [
            ExamPointFileDecision.model_validate(item)
            for item in state.get("file_decisions", [])
        ]
        admitted_by_point: dict[str, list] = defaultdict(list)
        for file_decision in decisions:
            admitted_by_point[file_decision.exam_point_code].extend(
                item
                for item in file_decision.decisions
                if item.relevance_class in {RelevanceClass.DIRECT, RelevanceClass.SUPPORTING}
            )

        def consolidate_point(point: ExamPoint) -> tuple[str, list[AssessmentUnitDraft]]:
            admitted = sorted(
                admitted_by_point.get(point.code, []),
                key=lambda item: (item.relevance_class.value, item.evidence_chunk_id),
            )
            if not admitted:
                return point.code, []
            units = consolidator.consolidate(
                exam_point=point,
                admitted_decisions=admitted,
                call_context=ModelCallContext(
                    course_id=state["course_id"],
                    organization_run_id=state["run_id"],
                    stage="consolidate_exam_point",
                ),
            )
            validated_units = [AssessmentUnitDraft.model_validate(item) for item in units]
            direct = [
                item for item in admitted if item.relevance_class is RelevanceClass.DIRECT
            ]
            if direct and not validated_units:
                raise ValueError(
                    "consolidator returned no assessment units for admitted direct evidence"
                )
            validate_consolidated_units(point, validated_units, direct)
            return point.code, validated_units

        consolidated: dict[str, list[dict]] = {}
        failures = list(state.get("failed_pairs") or [])
        coverage_reasons = {
            code: list(values)
            for code, values in (state.get("coverage_reasons") or {}).items()
        }
        with ThreadPoolExecutor(max_workers=settings.organization_max_workers) as executor:
            future_points = {executor.submit(consolidate_point, point): point for point in points}
            for future in as_completed(future_points):
                point = future_points[future]
                try:
                    code, units = future.result()
                    consolidated[code] = [item.model_dump(mode="json") for item in units]
                except Exception as exc:
                    consolidated[point.code] = []
                    failures.append(
                        _failure(
                            stage="consolidation",
                            point_code=point.code,
                            material_version_id=None,
                            exc=exc,
                        )
                    )
                    coverage_reasons.setdefault(point.code, []).append("consolidation_failed")
        return {
            "consolidated_units": dict(sorted(consolidated.items())),
            "failed_pairs": sorted(
                failures,
                key=lambda item: (
                    item["exam_point_code"],
                    item.get("material_version_id") or "",
                    item["stage"],
                ),
            ),
            "coverage_reasons": coverage_reasons,
        }

    def build_catalog_candidate(state: OrganizationState):
        tree = build_knowledge_catalog_candidate(
            framework_version_id=state["framework_version_id"],
            exam_points=_points(state),
            file_decisions=[
                ExamPointFileDecision.model_validate(item)
                for item in state.get("file_decisions", [])
            ],
            consolidated_units={
                code: [AssessmentUnitDraft.model_validate(item) for item in units]
                for code, units in state.get("consolidated_units", {}).items()
            },
            coverage_reasons=state.get("coverage_reasons") or {},
        )
        return {"tree": tree.model_dump(mode="json")}

    def audit_exam_point_coverage(state: OrganizationState):
        tree = KnowledgeTreeCandidate.model_validate(state["tree"])
        expected = {point.code for point in _points(state)}
        actual = {item.exam_point_code for item in tree.coverage}
        if actual != expected:
            raise ValueError("candidate coverage does not include every frozen exam point")
        tree.coverage.sort(key=lambda item: item.exam_point_code)
        return {"tree": tree.model_dump(mode="json")}

    def persist_candidate(state: OrganizationState):
        tree = KnowledgeTreeCandidate.model_validate(state["tree"])
        return {"candidate_id": repository.persist_candidate(dict(state), tree)}

    def interrupt_teacher_review(state: OrganizationState):
        decision = interrupt(
            {
                "candidate_id": state["candidate_id"],
                "tree": state["tree"],
                "failed_pairs": state.get("failed_pairs", []),
            }
        )
        confirmation = KnowledgeTreeConfirmation.model_validate(decision)
        tree = KnowledgeTreeCandidate.model_validate(state["tree"])
        active_topic_codes = {item.code for item in tree.topics if item.status == "active"}
        if active_topic_codes - set(confirmation.reviewed_topic_codes):
            raise ValueError("every active topic requires teacher review")
        allowed = {item["key"] for item in state["framework_anchors"]}
        points = _points(state)
        points_by_code = {point.code: point for point in points}
        revised = apply_tree_operations(tree, confirmation.operations, allowed_anchor_keys=allowed)
        excluded_codes = set(confirmation.teacher_exclusions)
        if excluded_codes - set(points_by_code):
            raise ValueError("teacher exclusion references an unknown exam point")
        for topic in revised.topics:
            for unit in topic.units:
                if unit.exam_point_code in excluded_codes:
                    unit.status = "excluded"
                    for card in unit.cards:
                        card.status = "excluded"
        coverage_by_code = {item.exam_point_code: item for item in revised.coverage}
        unresolved = {
            code
            for code in points_by_code
            if code not in excluded_codes
            and (
                code not in coverage_by_code
                or coverage_by_code[code].status != "sufficient"
            )
        }
        if unresolved:
            raise ValueError("exam point coverage must be sufficient or explicitly excluded")
        required_reviews = set(points_by_code) - excluded_codes
        if required_reviews - set(confirmation.reviewed_exam_point_codes):
            raise ValueError("every publishable exam point requires teacher review")
        if revised.topics:
            validate_publishable_tree(
                revised,
                allowed_anchor_keys=allowed,
                allowed_exam_point_codes=set(points_by_code),
                exam_points_by_code=points_by_code,
            )
        return {
            "tree": revised.model_dump(mode="json"),
            "confirmation": confirmation.model_copy(
                update={"operations": []}
            ).model_dump(mode="json"),
        }

    def publish_catalog_and_index(state: OrganizationState):
        result = repository.publish(
            dict(state),
            KnowledgeTreeCandidate.model_validate(state["tree"]),
            KnowledgeTreeConfirmation.model_validate(state["confirmation"]),
        )
        return {
            "catalog_version_id": result["catalog_version_id"],
            "index_version_id": result["index_version_id"],
        }

    graph = StateGraph(OrganizationState)
    graph.add_node("validate_inputs", validate_inputs)
    graph.add_node("freeze_selected_materials", freeze_selected_materials)
    graph.add_node("retrieve_per_exam_point", retrieve_per_exam_point)
    graph.add_node("classify_exam_point_file_pairs", classify_exam_point_file_pairs)
    graph.add_node("consolidate_per_exam_point", consolidate_per_exam_point)
    graph.add_node("build_catalog_candidate", build_catalog_candidate)
    graph.add_node("audit_exam_point_coverage", audit_exam_point_coverage)
    graph.add_node("persist_candidate", persist_candidate)
    graph.add_node("interrupt_teacher_review", interrupt_teacher_review)
    graph.add_node("publish_catalog_and_index", publish_catalog_and_index)
    graph.add_edge(START, "validate_inputs")
    graph.add_edge("validate_inputs", "freeze_selected_materials")
    graph.add_edge("freeze_selected_materials", "retrieve_per_exam_point")
    graph.add_edge("retrieve_per_exam_point", "classify_exam_point_file_pairs")
    graph.add_edge("classify_exam_point_file_pairs", "consolidate_per_exam_point")
    graph.add_edge("consolidate_per_exam_point", "build_catalog_candidate")
    graph.add_edge("build_catalog_candidate", "audit_exam_point_coverage")
    graph.add_edge("audit_exam_point_coverage", "persist_candidate")
    graph.add_edge("persist_candidate", "interrupt_teacher_review")
    graph.add_edge("interrupt_teacher_review", "publish_catalog_and_index")
    graph.add_edge("publish_catalog_and_index", END)
    return graph.compile(checkpointer=checkpointer)
