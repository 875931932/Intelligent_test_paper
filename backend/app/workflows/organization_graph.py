"""LangGraph orchestration for exam-point-led material organization."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict

from langgraph.errors import GraphInterrupt
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
    ORGANIZATION_SCHEMA_VERSION,
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

log = logging.getLogger("organization")

# 分类单次调用可稳定覆盖的 (考点, chunk) 对数上限；超过则拆批，避免模型
# 输出规模违规，同时杜绝“每考点一次调用”带来的重复 chunk 传递与 token 爆炸。
_CLASSIFY_MAX_PAIRS_PER_CALL = 40
# 分类单次调用允许的最大去重 chunk 数。按 MiMo 输入额度与 chunk 平均长度
# 综合估算：每 chunk 300-800 token，60 个 chunk 对应的 prompt 通常可控在
# 25k token 以内，避免单次分类调用输入超过 20 万 token。
_CLASSIFY_MAX_CHUNKS_PER_CALL = 60


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
    r"subscription[ _-]?key|password|passwd|token|secret)"
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
_AUTH_SCHEME_SECRET_PATTERN = re.compile(
    r"(?im)(?P<scheme>\b(?:basic|digest|aws4-hmac-sha256|token|api[ _-]?key)\b)"
    r"\s+[^\r\n]*"
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
    redacted = _AUTH_SCHEME_SECRET_PATTERN.sub(
        lambda match: f"{match.group('scheme')} [REDACTED]",
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
            "organization_schema_version": ORGANIZATION_SCHEMA_VERSION,
            "framework_version_id": state["framework_version_id"],
            "exam_points": list(state["frozen_input"].get("exam_points") or []),
            "material_version_ids": list(state["material_version_ids"]),
        }
        if state.get("frozen_input") != expected:
            raise ValueError("organization input snapshot is not frozen")
        frozen_points = expected["exam_points"]
        if (
            any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"].strip()
                or not isinstance(item.get("code"), str)
                or not item["code"].strip()
                for item in frozen_points
            )
            or len({item["id"] for item in frozen_points}) != len(frozen_points)
            or {item["code"] for item in frozen_points}
            != {point.code for point in _points(state)}
        ):
            raise ValueError("organization exam point snapshot is invalid")
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
            embed_query = getattr(retriever, "embed_query", None)
            query_vector = embed_query(point) if callable(embed_query) else None
            for material_version_id in sorted(chunks_by_material):
                material_chunks = chunks_by_material[material_version_id]
                allowed_ids = {chunk.id for chunk in material_chunks}
                if query_vector is None:
                    retrieved = retriever.retrieve(point, material_chunks)
                else:
                    retrieved = retriever.retrieve(
                        point,
                        material_chunks,
                        query_vector=query_vector,
                    )
                ranked = sorted(
                    retrieved,
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
        pairs_by_material: dict[str, list[dict]] = defaultdict(list)
        for pair in sorted(
            state.get("retrieval_pairs", []),
            key=lambda item: (item["exam_point_code"], item["material_version_id"]),
        ):
            pairs_by_material[pair["material_version_id"]].append(pair)

        def classify_material(
            material_version_id: str, material_pairs: list[dict]
        ) -> tuple[list[ExamPointFileDecision], list[tuple[str, Exception]]]:
            # 把同资料内的考点按 expected_pairs(考点数×chunk 并集) 做贪心打包，
            # 每批控制在分类模型单次可稳定覆盖的规模内：
            #   - 若像最初那样“整份资料一次调用”，expected_pairs 会达到几百上千，
            #     模型单次输出无法覆盖每一个 (考点, chunk) 对 → model_output_scope_violation；
            #   - 若像逐考点那样“一次只放 1 个考点”，调用次数会爆炸(资料×考点)，
            #     且同一 chunk 文本在多个批次里被重复传递 → token 消耗剧增。
            # 打包可在避免 scope violation 的同时复用 chunks、削减调用次数与重复 token。
            pairs_by_point: dict[str, list[dict]] = defaultdict(list)
            for pair in material_pairs:
                pairs_by_point[pair["exam_point_code"]].append(pair)

            # 预处理每个考点的召回 chunk 并集
            point_chunk_ids: dict[str, list[str]] = {
                code: sorted(
                    {
                        chunk_id
                        for pair in point_pairs
                        for chunk_id in pair["evidence_chunk_ids"]
                    }
                )
                for code, point_pairs in pairs_by_point.items()
            }

            # 贪心分批：累计的 chunk 去重并集 × 批内考点数 = expected_pairs
            batches: list[list[str]] = []
            for code in sorted(pairs_by_point):
                if len(point_chunk_ids[code]) > _CLASSIFY_MAX_CHUNKS_PER_CALL:
                    # 单个考点自身的 chunk 数就已超限，独立成批，交由模型尽力覆盖
                    batches.append([code])
                    continue
                if batches:
                    head = batches[-1]
                    head_chunks = set().union(
                        *(point_chunk_ids[c] for c in head)
                    )
                    head_chunks.update(point_chunk_ids[code])
                    if (
                        len(head_chunks) * (len(head) + 1)
                        <= _CLASSIFY_MAX_PAIRS_PER_CALL
                    ):
                        head.append(code)
                        continue
                batches.append([code])

            def classify_batch(batch_codes: list[str]) -> list[ExamPointFileDecision]:
                # 调用一次分类并完成校验与 scope 过滤；失败时由调用方决定是否拆批重试。
                batch_points = [points[code] for code in batch_codes]
                batch_chunk_ids = sorted(
                    set().union(*(point_chunk_ids[c] for c in batch_codes))
                )
                chunks = [chunks_by_id[chunk_id] for chunk_id in batch_chunk_ids]
                if any(
                    chunk.material_version_id != material_version_id for chunk in chunks
                ):
                    raise ValueError(
                        "classification pair contains chunks from another material"
                    )
                file_decisions = classifier.classify_file(
                    exam_points=batch_points,
                    material_version_id=material_version_id,
                    chunks=chunks,
                    call_context=ModelCallContext(
                        course_id=state["course_id"],
                        organization_run_id=state["run_id"],
                        stage="classify_exam_point_file_pair",
                    ),
                )
                by_point = {
                    item.exam_point_code: ExamPointFileDecision.model_validate(item)
                    for item in file_decisions
                }
                if set(by_point) != set(batch_codes):
                    raise ValueError(
                        "classification response does not match its material points"
                    )
                results: list[ExamPointFileDecision] = []
                for code in batch_codes:
                    validated = by_point[code]
                    if validated.material_version_id != material_version_id:
                        raise ValueError(
                            "classification response does not match its pair"
                        )
                    for pair in pairs_by_point[code]:
                        allowed_ids = set(pair["evidence_chunk_ids"])
                        scoped = [
                            item
                            for item in validated.decisions
                            if item.evidence_chunk_id in allowed_ids
                        ]
                        admitted = [
                            admit_evidence_decision(points[code], item)
                            for item in scoped
                        ]
                        decision_ids = [item.evidence_chunk_id for item in scoped]
                        if len(decision_ids) != len(set(decision_ids)):
                            raise ValueError(
                                "classification response contains duplicate evidence decisions"
                            )
                        if set(decision_ids) != allowed_ids:
                            raise ValueError(
                                "classification response must cover every recalled evidence chunk"
                            )
                        results.append(
                            validated.model_copy(update={"decisions": admitted})
                        )
                return results

            scoped_decisions: list[ExamPointFileDecision] = []
            point_failures: list[tuple[str, Exception]] = []
            for batch_codes in batches:
                try:
                    scoped_decisions.extend(classify_batch(batch_codes))
                except Exception as exc:
                    if len(batch_codes) == 1:
                        point_failures.append((batch_codes[0], exc))
                        continue
                    # 多考点批次失败时拆成单考点重试：expected_pairs 与输入规模
                    # 同时缩小，可显著降低 model_output_scope_violation / 传输超时；
                    # 单考点重试失败的考点才记失败，不再整份材料整体放弃。
                    for code in batch_codes:
                        try:
                            scoped_decisions.extend(classify_batch([code]))
                        except Exception as single_exc:
                            point_failures.append((code, single_exc))
            return scoped_decisions, point_failures

        decisions: list[ExamPointFileDecision] = []
        failures: list[dict] = list(state.get("failed_pairs") or [])
        coverage_reasons = {
            code: list(values)
            for code, values in (state.get("coverage_reasons") or {}).items()
        }
        with ThreadPoolExecutor(max_workers=settings.organization_max_workers) as executor:
            future_materials = {
                executor.submit(classify_material, material_version_id, material_pairs): (
                    material_version_id,
                    material_pairs,
                )
                for material_version_id, material_pairs in sorted(pairs_by_material.items())
            }
            for future in as_completed(future_materials):
                material_version_id, _ = future_materials[future]
                scoped, point_failures = future.result()
                decisions.extend(scoped)
                for code, exc in point_failures:
                    failures.append(
                        _failure(
                            stage="classification",
                            point_code=code,
                            material_version_id=material_version_id,
                            exc=exc,
                        )
                    )
                    coverage_reasons.setdefault(code, []).append("classification_failed")
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
            evidence_chunk_ids = sorted({item.evidence_chunk_id for item in admitted})
            chunks_by_id = {
                chunk.id: chunk for chunk in _chunks(state, evidence_chunk_ids)
            }
            # 分类对部分考点判不出 direct 时，聚焦该考点做一次 direct 复核：
            # 对召回但被判为 supporting/background 的 chunks 逐个布尔判定，
            # 把内容本身就是考点考核知识的 chunk 提升回 direct，避免整考点无卡。
            if not any(
                item.relevance_class is RelevanceClass.DIRECT for item in admitted
            ):
                candidate_chunks = list(chunks_by_id.values())
                promoted_ids = consolidator.recheck_direct(
                    exam_point=point,
                    candidate_chunks=candidate_chunks,
                    call_context=ModelCallContext(
                        course_id=state["course_id"],
                        organization_run_id=state["run_id"],
                        stage="consolidate_exam_point",
                    ),
                )
                promoted_decisions = [
                    decision.model_copy(
                        update={"relevance_class": RelevanceClass.DIRECT}
                    )
                    for decision in admitted
                    if decision.evidence_chunk_id in promoted_ids
                ]
                admitted = [*promoted_decisions, *admitted]
            if not any(
                item.relevance_class is RelevanceClass.DIRECT for item in admitted
            ):
                # 复核后仍无 direct：无直接证据可支撑知识卡，跳过归并，
                # 由覆盖审计标记 no_direct_evidence，避免空跑模型调用。
                return point.code, []
            units = consolidator.consolidate(
                exam_point=point,
                admitted_decisions=admitted,
                chunks_by_id=chunks_by_id,
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
        allowed = {item["key"] for item in state["framework_anchors"]}
        points = _points(state)
        points_by_code = {point.code: point for point in points}
        revised = apply_tree_operations(tree, confirmation.operations, allowed_anchor_keys=allowed)
        active_topic_codes = {item.code for item in revised.topics if item.status == "active"}
        if active_topic_codes - set(confirmation.reviewed_topic_codes):
            raise ValueError("every active topic requires teacher review")
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
            "confirmation": confirmation.model_dump(mode="json"),
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

    def _logged(name: str, fn):
        def wrapped(state):
            started = time.monotonic()
            try:
                result = fn(state)
                log.info("node %s ok (%.1fs)", name, time.monotonic() - started)
                return result
            except GraphInterrupt:
                log.info("node %s interrupted (%.1fs): waiting for teacher review", name, time.monotonic() - started)
                raise
            except Exception:
                log.exception("node %s failed (%.1fs)", name, time.monotonic() - started)
                raise
        return wrapped

    graph = StateGraph(OrganizationState)
    graph.add_node("validate_inputs", _logged("validate_inputs", validate_inputs))
    graph.add_node("freeze_selected_materials", _logged("freeze_selected_materials", freeze_selected_materials))
    graph.add_node("retrieve_per_exam_point", _logged("retrieve_per_exam_point", retrieve_per_exam_point))
    graph.add_node("classify_exam_point_file_pairs", _logged("classify_exam_point_file_pairs", classify_exam_point_file_pairs))
    graph.add_node("consolidate_per_exam_point", _logged("consolidate_per_exam_point", consolidate_per_exam_point))
    graph.add_node("build_catalog_candidate", _logged("build_catalog_candidate", build_catalog_candidate))
    graph.add_node("audit_exam_point_coverage", _logged("audit_exam_point_coverage", audit_exam_point_coverage))
    graph.add_node("persist_candidate", _logged("persist_candidate", persist_candidate))
    graph.add_node("interrupt_teacher_review", _logged("interrupt_teacher_review", interrupt_teacher_review))
    graph.add_node("publish_catalog_and_index", _logged("publish_catalog_and_index", publish_catalog_and_index))
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
