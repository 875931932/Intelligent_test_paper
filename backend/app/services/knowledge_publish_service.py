"""Persistence and atomic publication for exam-point-led knowledge catalogues."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    assessment_units,
    content_blocks,
    content_domains,
    document_parse_runs,
    evidence_chunks,
    exam_point_evidence_links,
    exam_points,
    framework_versions,
    index_memberships,
    index_versions,
    knowledge_cards,
    knowledge_catalog_versions,
    knowledge_evidence_links,
    material_versions,
    materials,
    organization_runs,
)
from app.domain.framework.exam_points import ExamPoint
from app.domain.knowledge.models import (
    LEGACY_ORGANIZATION_SCHEMA_VERSION,
    ORGANIZATION_SCHEMA_VERSION,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.domain.knowledge.relevance import (
    EvidenceDecision,
    ExamPointFileDecision,
    RelevanceClass,
    StagingChunk,
)
from app.services.knowledge_tree_service import (
    KnowledgeTreeValidationError,
    apply_tree_operations,
    compute_exam_point_coverage,
    validate_publishable_tree,
)


class KnowledgePublishError(Exception):
    pass


_ANSWER_OR_RUBRIC_ROLES = frozenset(
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


def _exam_point_from_row(row) -> ExamPoint:
    return ExamPoint.model_validate(
        {
            "code": row["code"],
            "anchor_key": row["anchor_key"],
            "title": row["title"],
            "assessment_requirement": row["assessment_requirement"],
            "weight_value": row["weight_value"],
            "weight_source": row["weight_source"],
            "weight_group_id": row["weight_group_id"],
            "priority": row["priority"],
            "cognitive_targets": row["cognitive_targets"] or [],
            "assessment_orientations": row["assessment_orientations"] or [],
            "allowed_question_types": row["allowed_question_types"] or [],
            "operational_detail_policy": row["operational_detail_policy"],
            "scope_boundary": row["scope_boundary"] or {},
            "required_evidence_roles": row["required_evidence_roles"] or [],
            "retrieval_intent": row["retrieval_intent"],
            "assessment_anchor_keys": [row["anchor_key"]],
            "teaching_anchor_keys": row["teaching_anchor_keys"] or [],
            "status": row["status"],
        }
    )


def _confirmed_exam_point_rows(
    session: Session,
    *,
    course_id: str,
    framework_version_id: str,
    lock: bool = False,
) -> list[dict]:
    statement = (
        select(exam_points)
        .where(
            exam_points.c.course_id == course_id,
            exam_points.c.framework_version_id == framework_version_id,
            exam_points.c.status == "confirmed",
        )
        .order_by(exam_points.c.code)
    )
    if lock:
        statement = statement.with_for_update()
    return [
        dict(row)
        for row in session.execute(statement).mappings()
    ]


def _exam_point_snapshot(point_rows: list[dict]) -> list[dict[str, str]]:
    return [{"id": row["id"], "code": row["code"]} for row in point_rows]


def _frozen_material_version_ids(frozen_input: dict) -> set[str]:
    raw_ids = frozen_input.get("material_version_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(item, str) or not item.strip() for item in raw_ids)
        or len(raw_ids) != len(set(raw_ids))
    ):
        raise KnowledgePublishError("organization frozen material snapshot is invalid")
    return set(raw_ids)


class DatabaseKnowledgeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _reset_connection(self) -> None:
        """宽容结束当前事务：连接被服务端掐断时 rollback 同样抛错，丢弃旧连接即可。

        随后的 execute 由连接池 pool_pre_ping 校验后换新连接，调用方无需感知。
        """
        try:
            self.session.rollback()
        except Exception:
            self.session.close()

    def load_evidence_chunks(
        self,
        *,
        course_id: str,
        run_id: str,
        evidence_chunk_ids: list[str],
    ) -> list[StagingChunk]:
        if not evidence_chunk_ids:
            return []
        # 先结束上一个事务再读取：图执行期间长模型调用阶段（分类/归并，可达
        # 十余分钟）会一直占着这条连接且无 DB 流量，远程 PostgreSQL 会掐断
        # 空闲连接；下次查询即报 "server closed the connection unexpectedly"。
        # 对已死连接 rollback 同样抛错，_reset_connection 宽容处理并丢弃旧连接，
        # 随后的 execute 由 pool_pre_ping 从池中换取全新连接。读取均为无状态快照，
        # 跨语句无需保持同一事务。
        self._reset_connection()
        rows = self.session.execute(
            select(evidence_chunks).where(
                evidence_chunks.c.course_id == course_id,
                evidence_chunks.c.id.in_(evidence_chunk_ids),
            )
        ).mappings().all()
        by_id = {row["id"]: row for row in rows}
        if set(by_id) != set(evidence_chunk_ids):
            raise KnowledgePublishError("evidence snapshot is unavailable or outside this course")
        return [
            StagingChunk(
                id=evidence_id,
                material_version_id=by_id[evidence_id]["material_version_id"],
                content=by_id[evidence_id]["content"],
                locator=by_id[evidence_id]["locator"] or {},
                embedding=by_id[evidence_id]["embedding"],
            )
            for evidence_id in evidence_chunk_ids
        ]

    def persist_candidate(self, state: dict, tree: KnowledgeTreeCandidate) -> str:
        course_id = state["course_id"]
        run_id = state["run_id"]
        self._reset_connection()
        existing = self.session.execute(
            select(
                knowledge_catalog_versions.c.id,
                knowledge_catalog_versions.c.framework_version_id,
                knowledge_catalog_versions.c.status,
                knowledge_catalog_versions.c.payload,
            )
            .where(
                knowledge_catalog_versions.c.course_id == course_id,
                knowledge_catalog_versions.c.organization_run_id == run_id,
            )
            .with_for_update()
        ).mappings().one_or_none()
        expected_run_status = "running"
        if existing is not None:
            expected_run_status = {
                "candidate": "awaiting_teacher_confirmation",
                "published": "published",
            }.get(existing["status"], "")
            if not expected_run_status:
                raise KnowledgePublishError("existing knowledge candidate status is unavailable")
        run = self.session.execute(
            select(organization_runs)
            .where(
                organization_runs.c.id == run_id,
                organization_runs.c.course_id == course_id,
                organization_runs.c.framework_version_id == tree.framework_version_id,
                organization_runs.c.status == expected_run_status,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if run is None:
            raise KnowledgePublishError("organization run is unavailable")
        frozen_input = state.get("frozen_input")
        if (
            not isinstance(frozen_input, dict)
            or frozen_input.get("organization_schema_version")
            != ORGANIZATION_SCHEMA_VERSION
            or run["input_snapshot"] != frozen_input
        ):
            raise KnowledgePublishError("organization run frozen snapshot does not match")
        point_rows = _confirmed_exam_point_rows(
            self.session,
            course_id=course_id,
            framework_version_id=tree.framework_version_id,
            lock=True,
        )
        if frozen_input.get("exam_points") != _exam_point_snapshot(point_rows):
            raise KnowledgePublishError("confirmed exam point snapshot does not match")
        frozen_material_ids = _frozen_material_version_ids(frozen_input)
        if existing is not None:
            existing_payload = existing["payload"] or {}
            if (
                existing["framework_version_id"] != tree.framework_version_id
                or existing_payload.get("organization_schema_version")
                != ORGANIZATION_SCHEMA_VERSION
                or existing_payload.get("frozen_input") != frozen_input
                or existing_payload.get("failed_pairs", [])
                != list(state.get("failed_pairs") or [])
                or KnowledgeTreeCandidate.model_validate(existing_payload).model_dump(mode="json")
                != tree.model_dump(mode="json")
            ):
                raise KnowledgePublishError(
                    "existing knowledge candidate content or frozen snapshot does not match"
                )
            return existing["id"]
        point_ids = {row["code"]: row["id"] for row in point_rows}
        payload = tree.model_dump(mode="json")
        payload["organization_schema_version"] = ORGANIZATION_SCHEMA_VERSION
        payload["frozen_input"] = frozen_input
        payload["failed_pairs"] = list(state.get("failed_pairs") or [])
        version_no = self.session.scalar(
            select(func.coalesce(func.max(knowledge_catalog_versions.c.version_no), 0) + 1).where(
                knowledge_catalog_versions.c.course_id == course_id
            )
        )
        catalog_id = uuid4().hex
        try:
            self.session.execute(
                knowledge_catalog_versions.insert().values(
                    id=catalog_id,
                    course_id=course_id,
                    organization_run_id=run_id,
                    framework_version_id=tree.framework_version_id,
                    version_no=version_no,
                    status="candidate",
                    payload=payload,
                )
            )
            seen: set[tuple[str, str]] = set()
            for raw in sorted(
                state.get("file_decisions") or [],
                key=lambda item: (item["exam_point_code"], item["material_version_id"]),
            ):
                file_decision = ExamPointFileDecision.model_validate(raw)
                if file_decision.material_version_id not in frozen_material_ids:
                    raise KnowledgePublishError(
                        "evidence decision references a material outside the frozen material snapshot"
                    )
                point_id = point_ids.get(file_decision.exam_point_code)
                if point_id is None:
                    raise KnowledgePublishError("evidence decision references an unconfirmed exam point")
                for decision in sorted(file_decision.decisions, key=lambda item: item.evidence_chunk_id):
                    key = (file_decision.exam_point_code, decision.evidence_chunk_id)
                    if key in seen:
                        raise KnowledgePublishError("duplicate exam-point evidence decision")
                    seen.add(key)
                    evidence_exists = self.session.execute(
                        select(evidence_chunks.c.id).where(
                            evidence_chunks.c.id == decision.evidence_chunk_id,
                            evidence_chunks.c.course_id == course_id,
                            evidence_chunks.c.material_version_id == file_decision.material_version_id,
                        )
                    ).scalar_one_or_none()
                    if evidence_exists is None:
                        raise KnowledgePublishError("evidence decision is outside the frozen snapshot")
                    self.session.execute(
                        exam_point_evidence_links.insert().values(
                            id=uuid4().hex,
                            course_id=course_id,
                            organization_run_id=run_id,
                            exam_point_id=point_id,
                            evidence_chunk_id=decision.evidence_chunk_id,
                            relevance_class=decision.relevance_class.value,
                            support_claim=decision.support_claim,
                            evidence_role=decision.evidence_role,
                            confidence=decision.confidence,
                            prompt_material=decision.prompt_material,
                            status="candidate",
                        )
                    )
            self.session.execute(
                update(organization_runs)
                .where(
                    organization_runs.c.id == run_id,
                    organization_runs.c.course_id == course_id,
                )
                .values(status="awaiting_teacher_confirmation", updated_at=datetime.now(UTC))
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return catalog_id

    def publish(
        self,
        state: dict,
        tree: KnowledgeTreeCandidate,
        confirmation: KnowledgeTreeConfirmation,
    ) -> dict:
        course_id = state["course_id"]
        catalog_id = state["candidate_id"]
        row = self.session.execute(
            select(knowledge_catalog_versions)
            .where(
                knowledge_catalog_versions.c.id == catalog_id,
                knowledge_catalog_versions.c.course_id == course_id,
                knowledge_catalog_versions.c.organization_run_id == state["run_id"],
                knowledge_catalog_versions.c.status == "candidate",
            )
            .with_for_update()
        ).mappings().one_or_none()
        if row is None:
            raise KnowledgePublishError("knowledge catalogue candidate is not available")
        if tree.framework_version_id != row["framework_version_id"]:
            raise KnowledgePublishError("candidate framework version cannot be changed")
        candidate_payload = row["payload"] or {}
        tree = KnowledgeTreeCandidate.model_validate(candidate_payload)
        if tree.framework_version_id != row["framework_version_id"]:
            raise KnowledgePublishError("persisted candidate framework version is invalid")
        framework = self.session.execute(
            select(framework_versions.c.payload).where(
                framework_versions.c.id == row["framework_version_id"],
                framework_versions.c.course_id == course_id,
                framework_versions.c.status == "published",
            )
        ).scalar_one_or_none()
        if not framework:
            raise KnowledgePublishError("published framework is required")
        allowed = {anchor["key"] for anchor in framework.get("anchors", [])}
        if confirmation.operations:
            try:
                tree = apply_tree_operations(
                    tree,
                    confirmation.operations,
                    allowed_anchor_keys=allowed,
                )
            except KnowledgeTreeValidationError as exc:
                raise KnowledgePublishError(str(exc)) from exc
        active_topics = {topic.code for topic in tree.topics if topic.status == "active"}
        if active_topics - set(confirmation.reviewed_topic_codes):
            raise KnowledgePublishError("every active topic requires teacher review")
        schema_version = candidate_payload.get("organization_schema_version")
        if type(schema_version) is not int or schema_version not in {
            LEGACY_ORGANIZATION_SCHEMA_VERSION,
            ORGANIZATION_SCHEMA_VERSION,
        }:
            raise KnowledgePublishError("knowledge candidate schema version is missing or unsupported")
        point_rows: list[dict] = []
        if schema_version == ORGANIZATION_SCHEMA_VERSION:
            run = self.session.execute(
                select(organization_runs)
                .where(
                    organization_runs.c.id == state["run_id"],
                    organization_runs.c.course_id == course_id,
                    organization_runs.c.framework_version_id == row["framework_version_id"],
                    organization_runs.c.status == "awaiting_teacher_confirmation",
                )
                .with_for_update()
            ).mappings().one_or_none()
            frozen_input = candidate_payload.get("frozen_input")
            if run is None or not isinstance(frozen_input, dict):
                raise KnowledgePublishError("organization run frozen snapshot is unavailable")
            if (
                frozen_input.get("organization_schema_version")
                != ORGANIZATION_SCHEMA_VERSION
                or run["input_snapshot"] != frozen_input
            ):
                raise KnowledgePublishError("organization run frozen snapshot does not match")
            point_rows = _confirmed_exam_point_rows(
                self.session,
                course_id=course_id,
                framework_version_id=row["framework_version_id"],
                lock=True,
            )
            if frozen_input.get("exam_points") != _exam_point_snapshot(point_rows):
                raise KnowledgePublishError("confirmed exam point snapshot does not match")
            frozen_material_ids = _frozen_material_version_ids(frozen_input)
            linked_material_rows = self.session.execute(
                select(
                    exam_point_evidence_links.c.id,
                    evidence_chunks.c.material_version_id,
                )
                .join(
                    evidence_chunks,
                    evidence_chunks.c.id == exam_point_evidence_links.c.evidence_chunk_id,
                )
                .where(
                    exam_point_evidence_links.c.course_id == course_id,
                    exam_point_evidence_links.c.organization_run_id == state["run_id"],
                )
                .with_for_update()
            ).mappings()
            if any(
                item["material_version_id"] not in frozen_material_ids
                for item in linked_material_rows
            ):
                raise KnowledgePublishError(
                    "persisted evidence references a material outside the frozen material snapshot"
                )
        points_by_code = {item["code"]: _exam_point_from_row(item) for item in point_rows}
        point_ids = {item["code"]: item["id"] for item in point_rows}
        publishable_exam_point_codes: set[str] | None = None
        try:
            if schema_version == ORGANIZATION_SCHEMA_VERSION:
                excluded_codes = set(confirmation.teacher_exclusions)
                if excluded_codes - set(points_by_code):
                    raise KnowledgePublishError(
                        "teacher exclusion references an unknown exam point"
                    )
                tree = tree.model_copy(deep=True)
                for topic in tree.topics:
                    for unit in topic.units:
                        if unit.exam_point_code in excluded_codes:
                            unit.status = "excluded"
                            for card in unit.cards:
                                card.status = "excluded"
                self._apply_supplement_operations(
                    course_id=course_id,
                    run_id=state["run_id"],
                    point_ids=point_ids,
                    tree=tree,
                    confirmation=confirmation,
                )
                coverage_by_code = {
                    item.exam_point_code: item for item in tree.coverage
                }
                if len(coverage_by_code) != len(tree.coverage):
                    raise KnowledgePublishError("exam point coverage entries must be unique")
                if set(coverage_by_code) - set(points_by_code):
                    raise KnowledgePublishError(
                        "exam point coverage references an unknown exam point"
                    )
                publishable_exam_point_codes = set(points_by_code) - excluded_codes
                unresolved = sorted(
                    code
                    for code in publishable_exam_point_codes
                    if code not in coverage_by_code
                    or coverage_by_code[code].status != "sufficient"
                )
                if unresolved:
                    raise KnowledgePublishError(
                        "exam point coverage must be sufficient or explicitly excluded"
                    )
                required_reviews = publishable_exam_point_codes
                if required_reviews - set(confirmation.reviewed_exam_point_codes):
                    raise KnowledgePublishError(
                        "every sufficient exam point requires teacher review"
                    )
                active_chain_codes = {
                    unit.exam_point_code
                    for topic in tree.topics
                    if topic.status == "active"
                    for unit in topic.units
                    if unit.status == "active"
                    and any(card.status == "active" for card in unit.cards)
                }
                excluded_topic_codes = {
                    operation.target_code
                    for operation in confirmation.operations
                    if operation.operation == "exclude_topic"
                }
                excluded_unit_codes = {
                    operation.target_code
                    for operation in confirmation.operations
                    if operation.operation == "exclude_unit"
                }
                units_by_point: dict[str, list[tuple[str, str]]] = {}
                for topic in tree.topics:
                    for unit in topic.units:
                        units_by_point.setdefault(unit.exam_point_code, []).append(
                            (topic.code, unit.code)
                        )
                explicitly_excluded_chain_codes = {
                    point_code
                    for point_code, paths in units_by_point.items()
                    if paths
                    and all(
                        topic_code in excluded_topic_codes
                        or unit_code in excluded_unit_codes
                        for topic_code, unit_code in paths
                    )
                }
                required_chain_codes = (
                    publishable_exam_point_codes - explicitly_excluded_chain_codes
                )
                if required_chain_codes - active_chain_codes:
                    raise KnowledgePublishError(
                        "every sufficient exam point requires an active topic-unit-card chain"
                    )
                tree = self._filter_live_direct_evidence(
                    course_id=course_id,
                    run_id=state["run_id"],
                    tree=tree,
                    point_rows=point_rows,
                    publishable_exam_point_codes=publishable_exam_point_codes,
                )
                chunk_ids = sorted(
                    {decision.evidence_chunk_id for decision in tree.evidence_decisions}
                )
                chunks_by_id: dict[str, StagingChunk] = {}
                if chunk_ids:
                    chunk_rows = self.session.execute(
                        select(
                            evidence_chunks.c.id,
                            evidence_chunks.c.material_version_id,
                            evidence_chunks.c.content,
                        ).where(
                            evidence_chunks.c.course_id == course_id,
                            evidence_chunks.c.id.in_(chunk_ids),
                        )
                    ).mappings()
                    chunks_by_id = {
                        row["id"]: StagingChunk(
                            id=row["id"],
                            material_version_id=row["material_version_id"],
                            content=row["content"],
                        )
                        for row in chunk_rows
                    }
                try:
                    validate_publishable_tree(
                        tree,
                        allowed_anchor_keys=allowed,
                        allowed_exam_point_codes=set(points_by_code),
                        exam_points_by_code=points_by_code,
                        chunks_by_id=chunks_by_id,
                    )
                except KnowledgeTreeValidationError as exc:
                    raise KnowledgePublishError(
                        "active source evidence is no longer sufficient for publish"
                    ) from exc
            else:
                # Compatibility is allowed only for candidates explicitly marked legacy.
                validate_publishable_tree(tree, allowed_anchor_keys=allowed)
        except KnowledgeTreeValidationError as exc:
            raise KnowledgePublishError(str(exc)) from exc

        try:
            active_direct_card_ids = self._insert_tree(
                course_id,
                catalog_id,
                tree,
                allowed,
                point_ids,
                publishable_exam_point_codes,
            )
            index_no = self.session.scalar(
                select(func.coalesce(func.max(index_versions.c.version_no), 0) + 1).where(
                    index_versions.c.course_id == course_id
                )
            )
            index_id = uuid4().hex
            self.session.execute(
                index_versions.insert().values(
                    id=index_id,
                    course_id=course_id,
                    catalog_version_id=catalog_id,
                    version_no=index_no,
                    status="published",
                )
            )
            for card_id in active_direct_card_ids:
                self.session.execute(
                    index_memberships.insert().values(
                        id=uuid4().hex,
                        course_id=course_id,
                        index_version_id=index_id,
                        knowledge_card_id=card_id,
                    )
                )
            self.session.execute(
                update(knowledge_catalog_versions)
                .where(
                    knowledge_catalog_versions.c.course_id == course_id,
                    knowledge_catalog_versions.c.status == "published",
                )
                .values(status="superseded")
            )
            self.session.execute(
                update(index_versions)
                .where(
                    index_versions.c.course_id == course_id,
                    index_versions.c.status == "published",
                    index_versions.c.id != index_id,
                )
                .values(status="superseded")
            )
            now = datetime.now(UTC)
            payload = tree.model_dump(mode="json")
            payload["failed_pairs"] = list(row["payload"].get("failed_pairs", []))
            payload["organization_schema_version"] = schema_version
            if schema_version == ORGANIZATION_SCHEMA_VERSION:
                payload["frozen_input"] = candidate_payload["frozen_input"]
            self.session.execute(
                update(knowledge_catalog_versions)
                .where(
                    knowledge_catalog_versions.c.id == catalog_id,
                    knowledge_catalog_versions.c.course_id == course_id,
                )
                .values(status="published", published_at=now, payload=payload)
            )
            self.session.execute(
                update(exam_point_evidence_links)
                .where(
                    exam_point_evidence_links.c.course_id == course_id,
                    exam_point_evidence_links.c.organization_run_id == state["run_id"],
                    exam_point_evidence_links.c.status == "candidate",
                )
                .values(status="published")
            )
            self.session.execute(
                update(organization_runs)
                .where(
                    organization_runs.c.id == state["run_id"],
                    organization_runs.c.course_id == course_id,
                )
                .values(status="published", updated_at=now, completed_at=now)
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return {"catalog_version_id": catalog_id, "index_version_id": index_id}

    def _insert_tree(
        self,
        course_id: str,
        catalog_id: str,
        tree: KnowledgeTreeCandidate,
        allowed: set[str],
        point_ids: dict[str, str],
        publishable_exam_point_codes: set[str] | None,
    ) -> list[str]:
        roots: dict[str, str] = {}
        for anchor_key in sorted(allowed):
            root_id = uuid4().hex
            roots[anchor_key] = root_id
            self.session.execute(
                content_domains.insert().values(
                    id=root_id,
                    course_id=course_id,
                    catalog_version_id=catalog_id,
                    parent_domain_id=None,
                    level=1,
                    framework_anchor_key=anchor_key,
                    code=anchor_key,
                    name=anchor_key,
                    status="active",
                )
            )
        decisions: dict[tuple[str, str], EvidenceDecision] = {}
        for decision in tree.evidence_decisions:
            if decision.relevance_class is RelevanceClass.DIRECT:
                decisions[(decision.exam_point_code, decision.evidence_chunk_id)] = decision
        active_direct_card_ids: list[str] = []
        for topic in tree.topics:
            topic_id = uuid4().hex
            self.session.execute(
                content_domains.insert().values(
                    id=topic_id,
                    course_id=course_id,
                    catalog_version_id=catalog_id,
                    parent_domain_id=roots[topic.framework_anchor_key],
                    level=2,
                    framework_anchor_key=topic.framework_anchor_key,
                    code=topic.code,
                    name=topic.name,
                    status=topic.status,
                )
            )
            for unit in topic.units:
                unit_id = uuid4().hex
                exam_point_id = point_ids.get(unit.exam_point_code)
                if point_ids and exam_point_id is None:
                    raise KnowledgePublishError(
                        "assessment unit does not reference a confirmed exam point"
                    )
                self.session.execute(
                    assessment_units.insert().values(
                        id=unit_id,
                        course_id=course_id,
                        catalog_version_id=catalog_id,
                        content_domain_id=topic_id,
                        exam_point_id=exam_point_id,
                        code=unit.code,
                        title=unit.title,
                        performance_statement=unit.performance_statement,
                        scope_boundary=unit.scope_boundary,
                        status=unit.status,
                    )
                )
                for card in unit.cards:
                    card_id = uuid4().hex
                    material = json.dumps(
                        {
                            "exam_point_code": unit.exam_point_code,
                            "name": card.name,
                            "performance_statement": card.performance_statement,
                            "assessable_content": card.assessable_content,
                            "scope_boundary": card.scope_boundary,
                            "concept_cluster": card.concept_cluster,
                            "answer_proposition": card.answer_proposition,
                            "prompt_material": card.prompt_material,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    self.session.execute(
                        knowledge_cards.insert().values(
                            id=card_id,
                            course_id=course_id,
                            catalog_version_id=catalog_id,
                            assessment_unit_id=unit_id,
                            name=card.name,
                            performance_statement=card.performance_statement,
                            assessable_content=card.assessable_content,
                            scope_boundary=card.scope_boundary,
                            cognitive_targets=card.cognitive_targets,
                            allowed_question_types=card.allowed_question_types,
                            importance=card.importance,
                            concept_cluster=card.concept_cluster,
                            answer_proposition=card.answer_proposition,
                            prompt_material=card.prompt_material,
                            relation_edges=[
                                {"kind": e.kind, "target": e.target}
                                for e in card.relation_edges
                            ],
                            content_hash=sha256(material.encode()).hexdigest(),
                            status=card.status,
                            version=1,
                        )
                    )
                    direct_count = 0
                    for evidence_id in card.evidence_chunk_ids:
                        decision = decisions.get((unit.exam_point_code, evidence_id))
                        if decision is None:
                            continue
                        direct_count += 1
                        self.session.execute(
                            knowledge_evidence_links.insert().values(
                                id=uuid4().hex,
                                course_id=course_id,
                                knowledge_card_id=card_id,
                                evidence_chunk_id=evidence_id,
                                evidence_role=decision.evidence_role or "fact",
                                confidence=decision.confidence,
                                teacher_confirmed=True,
                                lifecycle_status="active",
                            )
                        )
                    hierarchy_is_publishable = (
                        topic.status == "active"
                        and unit.status == "active"
                        and card.status == "active"
                        and (
                            publishable_exam_point_codes is None
                            or unit.exam_point_code in publishable_exam_point_codes
                        )
                    )
                    if hierarchy_is_publishable and direct_count:
                        active_direct_card_ids.append(card_id)
        return active_direct_card_ids

    def _apply_supplement_operations(
        self,
        *,
        course_id: str,
        run_id: str,
        point_ids: dict[str, str],
        tree: KnowledgeTreeCandidate,
        confirmation: KnowledgeTreeConfirmation,
    ) -> None:
        """把教师补证据操作落库并同步改判候选树。

        REST 发布路径是唯一的教师确认入口（后台图的中断恢复从不被调用），
        因此 supplement_direct_evidence 必须在这里生效：改判证据链接分类、
        重算覆盖、并同步内存中的 evidence_decisions，使后续的活证据过滤
        与发布校验基于改判后的状态运行。
        """
        supplement_ops = [
            op
            for op in confirmation.operations
            if op.operation == "supplement_direct_evidence"
            and op.target_code.strip()
            and (op.value or "").strip()
        ]
        if not supplement_ops:
            return
        valid_codes = set(point_ids)
        for op in supplement_ops:
            if op.target_code not in valid_codes:
                raise KnowledgePublishError(
                    "supplement operation references an unknown exam point"
                )
            chunk_id = (op.value or "").strip()
            chunk_exists = self.session.execute(
                select(evidence_chunks.c.id).where(
                    evidence_chunks.c.id == chunk_id,
                    evidence_chunks.c.course_id == course_id,
                )
            ).scalar_one_or_none()
            if chunk_exists is None:
                raise KnowledgePublishError(
                    "supplement operation references evidence outside the frozen snapshot"
                )
            updated = self.session.execute(
                exam_point_evidence_links.update()
                .where(
                    exam_point_evidence_links.c.course_id == course_id,
                    exam_point_evidence_links.c.organization_run_id == run_id,
                    exam_point_evidence_links.c.exam_point_id == point_ids[op.target_code],
                    exam_point_evidence_links.c.evidence_chunk_id == chunk_id,
                    exam_point_evidence_links.c.relevance_class.in_(
                        ["supporting", "background"]
                    ),
                )
                .values(relevance_class="direct")
            )
            if updated.rowcount == 0:
                # 可能是同一操作的重复提交（已改判过 direct）；确认目标链接
                # 以 direct 存在则幂等跳过，否则说明目标不存在，拒绝发布。
                still_there = self.session.execute(
                    select(exam_point_evidence_links.c.id).where(
                        exam_point_evidence_links.c.course_id == course_id,
                        exam_point_evidence_links.c.organization_run_id == run_id,
                        exam_point_evidence_links.c.exam_point_id == point_ids[op.target_code],
                        exam_point_evidence_links.c.evidence_chunk_id == chunk_id,
                        exam_point_evidence_links.c.relevance_class == "direct",
                    )
                ).scalar_one_or_none()
                if still_there is None:
                    raise KnowledgePublishError(
                        "supplement operation targets an evidence link that does not exist"
                    )
        self.session.flush()
        # 同步内存中的 evidence_decisions 与 coverage，供 _filter_live_direct_evidence
        # 与 validate_publishable_tree 基于改判后的状态校验。
        updated_keys = {
            (op.target_code, (op.value or "").strip()) for op in supplement_ops
        }
        tree.evidence_decisions = [
            (
                decision.model_copy(update={"relevance_class": RelevanceClass.DIRECT})
                if (decision.exam_point_code, decision.evidence_chunk_id) in updated_keys
                and decision.relevance_class
                in {RelevanceClass.SUPPORTING, RelevanceClass.BACKGROUND}
                else decision
            )
            for decision in tree.evidence_decisions
        ]
        touched_codes = {code for code, _ in updated_keys}
        for code in touched_codes:
            original = next(
                (c for c in tree.coverage if c.exam_point_code == code), None
            )
            # 补证据只改变证据数量，无法重建已归并失败的单元/卡片链路：
            # 保留原始 *_failed 原因，避免误判 sufficient 后触发 active-chain 校验。
            irrecoverable = [
                r
                for r in (original.reasons if original else [])
                if r.endswith("_failed")
            ]
            tree.coverage = [
                item for item in tree.coverage if item.exam_point_code != code
            ] + [
                compute_exam_point_coverage(
                    code,
                    [d for d in tree.evidence_decisions if d.exam_point_code == code],
                    additional_reasons=irrecoverable,
                )
            ]

    def _filter_live_direct_evidence(
        self,
        *,
        course_id: str,
        run_id: str,
        tree: KnowledgeTreeCandidate,
        point_rows: list[dict],
        publishable_exam_point_codes: set[str],
    ) -> KnowledgeTreeCandidate:
        point_codes_by_id = {row["id"]: row["code"] for row in point_rows}
        rows = self.session.execute(
            select(
                exam_point_evidence_links.c.exam_point_id,
                exam_point_evidence_links.c.evidence_chunk_id,
            )
            .join(
                evidence_chunks,
                evidence_chunks.c.id == exam_point_evidence_links.c.evidence_chunk_id,
            )
            .join(
                material_versions,
                material_versions.c.id == evidence_chunks.c.material_version_id,
            )
            .join(materials, materials.c.id == material_versions.c.material_id)
            .where(
                exam_point_evidence_links.c.course_id == course_id,
                exam_point_evidence_links.c.organization_run_id == run_id,
                exam_point_evidence_links.c.relevance_class == "direct",
                exam_point_evidence_links.c.status.in_(["candidate", "published"]),
                evidence_chunks.c.course_id == course_id,
                material_versions.c.course_id == course_id,
                material_versions.c.status == "staged",
                materials.c.course_id == course_id,
                materials.c.status == "staged",
            )
            .with_for_update()
        ).all()
        live_keys = {
            (point_codes_by_id[point_id], evidence_id)
            for point_id, evidence_id in rows
            if point_id in point_codes_by_id
        }
        filtered = tree.model_copy(deep=True)
        filtered.evidence_decisions = [
            decision
            for decision in filtered.evidence_decisions
            if decision.relevance_class is not RelevanceClass.DIRECT
            or (decision.exam_point_code, decision.evidence_chunk_id) in live_keys
        ]
        live_direct_by_point: dict[str, list[EvidenceDecision]] = {}
        for decision in filtered.evidence_decisions:
            if decision.relevance_class is RelevanceClass.DIRECT:
                live_direct_by_point.setdefault(decision.exam_point_code, []).append(decision)
        for point_code in publishable_exam_point_codes:
            direct = live_direct_by_point.get(point_code, [])
            if not any(
                (decision.evidence_role or "").strip().casefold()
                in _ANSWER_OR_RUBRIC_ROLES
                for decision in direct
            ):
                raise KnowledgePublishError(
                    "active source evidence no longer contains an answer or rubric basis"
                )
            coverage = next(
                (
                    item
                    for item in filtered.coverage
                    if item.exam_point_code == point_code
                ),
                None,
            )
            if coverage is not None:
                coverage.direct_count = len(direct)
        for topic in filtered.topics:
            for unit in topic.units:
                for card in unit.cards:
                    card.evidence_chunk_ids = [
                        evidence_id
                        for evidence_id in card.evidence_chunk_ids
                        if (unit.exam_point_code, evidence_id) in live_keys
                    ]
        return filtered


def _validated_embeddings(
    raw: object,
    *,
    expected: int,
    expected_dimension: int | None = None,
) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) != expected:
        raise KnowledgePublishError("embedding response count does not match parsed blocks")
    vectors: list[list[float]] = []
    for value in raw:
        if not isinstance(value, list) or not value:
            raise KnowledgePublishError("embedding response contains an empty vector")
        try:
            vector = [float(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise KnowledgePublishError("embedding response contains non-numeric values") from exc
        if not all(math.isfinite(item) for item in vector):
            raise KnowledgePublishError("embedding response contains non-finite values")
        norm = math.hypot(*vector)
        if norm == 0:
            raise KnowledgePublishError("embedding response contains a zero norm vector")
        if not math.isfinite(norm):
            raise KnowledgePublishError("embedding response contains a non-finite norm")
        if expected_dimension is None:
            expected_dimension = len(vector)
        elif len(vector) != expected_dimension:
            raise KnowledgePublishError("embedding response dimension is inconsistent")
        vectors.append(vector)
    return vectors


# 小 block 聚合目标：把解析产出的碎块（标题/短段落，实测平均仅 54 字符）
# 合并到该长度附近再作为一个 evidence chunk。碎块的 JSON 包装开销
# （evidence_chunk_id/material_version_id/locator ≈ 50-100 token/chunk）
# 会超过其内容本身，导致分类阶段的输入 token 被结构性放大。
_EVIDENCE_MERGE_TARGET_CHARS = 1200
# 短于该值的 block 视为碎块，与后续 block 聚合；超长 block 保持独立。
_EVIDENCE_MERGE_MIN_CHARS = 200

# 实验报告/作业封面、表单区与声明区的识别特征。这些块不含可迁移知识、
# 只是教学资料的封面或页面元信息（课程名/学生信息/提交日期/装订表单等），
# 一旦作为证据入库会既污染分类/归并上下文，又成为无意义的补证据候选。
# 精准特征（下划线填空占位、封面/申明表单项），避免误伤正文中的真实内容
# 与表格（正文偶尔含下划线，但不会同时具备"学生姓名+学号+递交日期"组合）。
_COVER_FORM_STRONG_SIGNALS = (
    "实验报告封面",
    "课程名称",
    "课程代码",
    "任课老师",
    "实验指导老师",
    "学生姓名",
    "学号",
    "教学班",
    "递交日期",
    "签收人",
    "我申明",
    "申明人",
    "实验报告评语与评分",
    "评阅老师签名",
)
# 封面表单区往往由多个含下划线填空的短块连排出现；单个下划线块不判死，
# 需配合强信号定位后，在起始区统一剔除，避免误杀正文里独立的示例代码块。
_COVER_FORM_UNDERSCORE_PATTERN = re.compile(r"_{6,}|__{2,}__")


def _is_cover_form_block(text: str) -> bool:
    """判定一个 block 是否属于封面/表单/声明区（非知识内容）。"""
    return any(signal in text for signal in _COVER_FORM_STRONG_SIGNALS)


def _drop_cover_form_blocks(blocks: list[dict]) -> list[dict]:
    """剔除文档开头的封面/表单/声明块，保留其后真正的正文块。

    封面区指文档最前面连续命中表单特征的块及其随后的填空占位块；
    一旦进入真实正文（非表单且非填空占位）即停止收缩，避免把后续
    page 0/第 1 页上的实验目的、步骤等真实内容一并误删。
    """
    text = [block.get("text") or "" for block in blocks]

    # 定位起始封面区结束位置：从头扫描，遇到"强信号表单块"后，
    # 其后若仍是表单/填空占位块则一并剔除，直到首个既非表单也非
    # 填空占位的正文块为止。仅处理文档最前部的这些块。
    cut_at: int | None = None
    saw_cover = False
    for index, raw in enumerate(text):
        content = raw.strip()
        if not content:
            continue
        if _is_cover_form_block(content):
            saw_cover = True
            cut_at = index + 1
            continue
        if saw_cover and _COVER_FORM_UNDERSCORE_PATTERN.search(content):
            cut_at = index + 1
            continue
        # 封面区已结束：停在首个正文块前
        break
    if not saw_cover or cut_at is None:
        return blocks
    return blocks[cut_at:]


def _merged_evidence_blocks(blocks: list[dict]) -> list[tuple[dict, list[dict]]]:
    """把相邻碎 block 聚合为更长的 evidence chunk。

    返回 [(merged_block, source_blocks)]。聚合沿用首个 block 的定位信息，
    并保留 heading_path 的并集，供教师溯源与模型归属推断。
    """

    merged: list[tuple[dict, list[dict]]] = []
    buffer: list[dict] = []
    buffer_chars = 0

    def flush() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        heading_path: list = []
        for block in buffer:
            for part in block.get("heading_path") or []:
                if part not in heading_path:
                    heading_path.append(part)
        head = buffer[0]
        merged.append(
            (
                {
                    **head,
                    "text": "\n".join(block["text"].strip() for block in buffer),
                    "heading_path": heading_path,
                    "content_hash": None,
                },
                list(buffer),
            )
        )
        buffer = []
        buffer_chars = 0

    for block in blocks:
        text = block["text"].strip()
        text_len = len(text)
        if text_len >= _EVIDENCE_MERGE_MIN_CHARS:
            # 长块自成 chunk：先把已聚合的碎块冲刷出去，再独立冲刷本块，
            # 避免长语义块被前面的碎块标题污染。
            flush()
            buffer.append(block)
            flush()
            continue
        buffer.append(block)
        buffer_chars += text_len
        if buffer_chars >= _EVIDENCE_MERGE_TARGET_CHARS:
            flush()
    flush()
    return merged


def validate_organization_run_inputs(
    session: Session,
    *,
    course_id: str,
    material_version_ids: list[str],
) -> None:
    """轻量前置校验：课程/框架/考点/材料可组织性，不读取内容块、不嵌入。

    供异步创建组织 run 的端点快速失败，把耗时（嵌入、模型调用）全部放到后台。
    """
    from app.services.course_service import get_course

    get_course(session, course_id)
    framework = session.execute(
        select(framework_versions)
        .where(
            framework_versions.c.course_id == course_id,
            framework_versions.c.status == "published",
        )
        .order_by(framework_versions.c.version_no.desc())
        .limit(1)
    ).mappings().one_or_none()
    if framework is None:
        raise KnowledgePublishError("published framework is required")
    point_rows = _confirmed_exam_point_rows(
        session,
        course_id=course_id,
        framework_version_id=framework["id"],
    )
    if not point_rows:
        raise KnowledgePublishError("published framework has no confirmed exam points")
    if not material_version_ids or len(material_version_ids) != len(set(material_version_ids)):
        raise KnowledgePublishError("at least one unique material version is required")
    for version_id in material_version_ids:
        version = session.execute(
            select(material_versions.c.id, materials.c.material_type)
            .join(materials, material_versions.c.material_id == materials.c.id)
            .where(
                material_versions.c.id == version_id,
                material_versions.c.course_id == course_id,
                material_versions.c.status == "staged",
                materials.c.course_id == course_id,
                materials.c.status == "staged",
            )
        ).mappings().one_or_none()
        if version is None:
            raise KnowledgePublishError("selected material version is not available")
        if version["material_type"] not in {"teaching_material", "exercise"}:
            raise KnowledgePublishError("only teaching materials or exercises can be organized")
        parse_run_id = session.execute(
            select(document_parse_runs.c.id)
            .where(
                document_parse_runs.c.course_id == course_id,
                document_parse_runs.c.material_version_id == version_id,
                document_parse_runs.c.status == "ready",
            )
            .order_by(document_parse_runs.c.completed_at.desc(), document_parse_runs.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if parse_run_id is None:
            raise KnowledgePublishError("selected material has no ready parsed content")


def create_organization_state(
    session: Session,
    *,
    course_id: str,
    material_version_ids: list[str],
    embedder,
    run_id: str | None = None,
) -> dict:
    from app.services.course_service import get_course

    get_course(session, course_id)
    framework = session.execute(
        select(framework_versions)
        .where(
            framework_versions.c.course_id == course_id,
            framework_versions.c.status == "published",
        )
        .order_by(framework_versions.c.version_no.desc())
        .limit(1)
    ).mappings().one_or_none()
    if framework is None:
        raise KnowledgePublishError("published framework is required")
    point_rows = _confirmed_exam_point_rows(
        session,
        course_id=course_id,
        framework_version_id=framework["id"],
    )
    if not point_rows:
        raise KnowledgePublishError("published framework has no confirmed exam points")
    if not material_version_ids or len(material_version_ids) != len(set(material_version_ids)):
        raise KnowledgePublishError("at least one unique material version is required")

    selected_blocks: list[tuple[str, list[dict]]] = []
    for version_id in material_version_ids:
        version = session.execute(
            select(material_versions.c.id, materials.c.material_type)
            .join(materials, material_versions.c.material_id == materials.c.id)
            .where(
                material_versions.c.id == version_id,
                material_versions.c.course_id == course_id,
                material_versions.c.status == "staged",
                materials.c.course_id == course_id,
                materials.c.status == "staged",
            )
        ).mappings().one_or_none()
        if version is None:
            raise KnowledgePublishError("selected material version is not available")
        if version["material_type"] not in {"teaching_material", "exercise"}:
            raise KnowledgePublishError("only teaching materials or exercises can be organized")
        parse_run_id = session.execute(
            select(document_parse_runs.c.id)
            .where(
                document_parse_runs.c.course_id == course_id,
                document_parse_runs.c.material_version_id == version_id,
                document_parse_runs.c.status == "ready",
            )
            .order_by(document_parse_runs.c.completed_at.desc(), document_parse_runs.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if parse_run_id is None:
            raise KnowledgePublishError("selected material has no ready parsed content")
        blocks = [
            dict(row)
            for row in session.execute(
                select(content_blocks)
                .where(
                    content_blocks.c.course_id == course_id,
                    content_blocks.c.material_version_id == version_id,
                    content_blocks.c.document_parse_run_id == parse_run_id,
                )
                .order_by(content_blocks.c.reading_order, content_blocks.c.block_index)
            ).mappings()
            if row["text"].strip()
        ]
        # 剔除文档开头封面/表单/声明块（实验报告封面、学生信息、装订表单、
        # 下划线填空占位等），从源头避免这些非知识块嵌入入库。既减少向量
        # 调用与分类/归并 token，也让补证据候选不再出现封面类无效项。
        blocks = _drop_cover_form_blocks(blocks)
        if not blocks:
            raise KnowledgePublishError("selected material has no ready parsed content")
        selected_blocks.append((version_id, blocks))

    # 先按阅读顺序聚合碎块，再对聚合后的文本嵌入：
    # 1) 嵌入对 1200 字符级别的语义块更稳定，短碎块（如孤立标题）向量噪声大；
    # 2) evidence chunk 数量从上千降到数百，分类阶段的 JSON 包装开销同步缩小。
    # 3) 合并是确定性的：同一资料版本产出同一批文本，向量可跨 run 复用
    #    （ix_evidence_chunks_course_material_hash 索引即为此设计）。
    #    仅对新增/变化的块调用嵌入 API；嵌入模型更换（维度不一致）时整批重嵌。
    embedded_blocks: list[tuple[str, dict, list[float], list[dict]]] = []
    embedding_dimension: int | None = None
    for version_id, blocks in selected_blocks:
        merged_blocks = _merged_evidence_blocks(blocks)
        texts = [merged_block["text"].strip() for merged_block, _ in merged_blocks]
        hashes = [sha256(text.encode()).hexdigest() for text in texts]
        cached: dict[str, list[float]] = {}
        if hashes:
            for row in session.execute(
                select(evidence_chunks.c.content_hash, evidence_chunks.c.embedding)
                .where(
                    evidence_chunks.c.course_id == course_id,
                    evidence_chunks.c.material_version_id == version_id,
                    evidence_chunks.c.content_hash.in_(hashes),
                    evidence_chunks.c.embedding.is_not(None),
                )
            ).mappings():
                cached.setdefault(row["content_hash"], row["embedding"])
        vectors: list[list[float]] = []
        missing: list[tuple[int, str]] = []
        for index, (text, digest) in enumerate(zip(texts, hashes, strict=True)):
            hit = cached.get(digest)
            if hit is None:
                missing.append((index, text))
                vectors.append([])
            else:
                vectors.append(hit)
        if missing:
            try:
                fresh = _validated_embeddings(
                    embedder.embed([text for _, text in missing]),
                    expected=len(missing),
                    expected_dimension=embedding_dimension,
                )
            except KnowledgePublishError:
                raise
            except Exception as exc:
                raise KnowledgePublishError("embedding service is unavailable") from exc
            for (index, _text), vector in zip(missing, fresh, strict=True):
                vectors[index] = vector
            embedding_dimension = len(fresh[0])
        if vectors and embedding_dimension is None:
            embedding_dimension = len(vectors[0])
        if vectors and any(len(vector) != embedding_dimension for vector in vectors):
            # 嵌入模型更换导致缓存向量维度不一致：放弃缓存整批重嵌。
            try:
                vectors = _validated_embeddings(
                    embedder.embed(texts),
                    expected=len(texts),
                    expected_dimension=None,
                )
            except KnowledgePublishError:
                raise
            except Exception as exc:
                raise KnowledgePublishError("embedding service is unavailable") from exc
            embedding_dimension = len(vectors[0]) if vectors else embedding_dimension
        embedded_blocks.extend(
            (version_id, merged_block, vector, source_blocks)
            for (merged_block, source_blocks), vector in zip(merged_blocks, vectors, strict=True)
        )

    run_id = run_id or uuid4().hex

    frozen_input = {
        "organization_schema_version": ORGANIZATION_SCHEMA_VERSION,
        "framework_version_id": framework["id"],
        "exam_points": _exam_point_snapshot(point_rows),
        "material_version_ids": list(material_version_ids),
    }
    evidence_ids: list[str] = []
    try:
        session.execute(
            organization_runs.insert().values(
                id=run_id,
                course_id=course_id,
                framework_version_id=framework["id"],
                status="running",
                input_snapshot=frozen_input,
            )
        )
        for chunk_index, (version_id, merged_block, vector, source_blocks) in enumerate(embedded_blocks):
            # chunk id 从内容哈希+资料版本确定性派生（而非随机 UUID）：同一资料
            # 快照在重复 run 中产出相同 id，分类/归并 prompt 随之稳定，模型响应
            # 缓存（temperature=0）才能真正命中，重建 run 的模型成本趋近于零。
            # 冲突域按 (material_version_id, content_hash) 划分，同内容跨资料不串。
            digest = sha256(
                f"{version_id}\n{merged_block['text'].strip()}".encode()
            ).hexdigest()
            evidence_id = digest[:32]
            locator = {
                "page_index": merged_block["page_index"],
                "bbox": merged_block["bbox"],
                "heading_path": merged_block["heading_path"] or [],
                "reading_order": merged_block["reading_order"],
                "block_type": merged_block["block_type"],
                "source_block_count": len(source_blocks),
            }
            merged_text = merged_block["text"].strip()
            existing = session.execute(
                select(evidence_chunks.c.id, evidence_chunks.c.embedding).where(
                    evidence_chunks.c.id == evidence_id,
                    evidence_chunks.c.course_id == course_id,
                    evidence_chunks.c.material_version_id == version_id,
                )
            ).mappings().first()
            if existing is not None:
                # 内容寻址复用：id 相同即同资料同内容，旧行就是同一条知识块，
                # 不再插入（表为 append-only，重复插入必撞主键）。历史行早于
                # 确定性 id 方案时缺 embedding，此处补齐以保证新 run 检索可用。
                if existing["embedding"] is None:
                    session.execute(
                        evidence_chunks.update()
                        .where(evidence_chunks.c.id == evidence_id)
                        .values(embedding=vector)
                    )
                evidence_ids.append(evidence_id)
                continue
            evidence_ids.append(evidence_id)
            session.execute(
                evidence_chunks.insert().values(
                    id=evidence_id,
                    course_id=course_id,
                    organization_run_id=run_id,
                    material_version_id=version_id,
                    content_block_id=merged_block["id"],
                    chunk_index=chunk_index,
                    content=merged_text,
                    content_hash=sha256(merged_text.encode()).hexdigest(),
                    locator=locator,
                    embedding=vector,
                )
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return {
        "course_id": course_id,
        "run_id": run_id,
        "framework_version_id": framework["id"],
        "framework_anchors": framework["payload"].get("anchors", []),
        "exam_points": [_exam_point_from_row(row).model_dump(mode="json") for row in point_rows],
        "material_version_ids": list(material_version_ids),
        "evidence_chunk_ids": evidence_ids,
        "frozen_input": frozen_input,
    }


def get_organization_run(session: Session, *, course_id: str, run_id: str) -> dict:
    row = session.execute(
        select(organization_runs).where(
            organization_runs.c.id == run_id,
            organization_runs.c.course_id == course_id,
        )
    ).mappings().one_or_none()
    if row is None:
        raise KnowledgePublishError("organization run not found")
    return dict(row)


def get_organization_candidate(session: Session, *, course_id: str, run_id: str) -> dict:
    row = session.execute(
        select(knowledge_catalog_versions)
        .where(
            knowledge_catalog_versions.c.course_id == course_id,
            knowledge_catalog_versions.c.organization_run_id == run_id,
        )
        .order_by(knowledge_catalog_versions.c.version_no.desc())
        .limit(1)
    ).mappings().one_or_none()
    if row is None:
        raise KnowledgePublishError("knowledge catalogue candidate not found")
    result = dict(row)
    payload = dict(result["payload"])
    counts = Counter(
        session.scalars(
            select(exam_point_evidence_links.c.relevance_class).where(
                exam_point_evidence_links.c.course_id == course_id,
                exam_point_evidence_links.c.organization_run_id == run_id,
            )
        )
    )
    payload["relevance_counts"] = {
        relevance.value: counts.get(relevance.value, 0) for relevance in RelevanceClass
    }
    payload["evidence_sources"] = [
        {
            "evidence_chunk_id": item["evidence_chunk_id"],
            "exam_point_code": item["exam_point_code"],
            "material_version_id": item["material_version_id"],
            "locator": item["locator"] or {},
            "relevance_class": item["relevance_class"],
            "support_claim": item["support_claim"],
            "evidence_role": item["evidence_role"],
            "confidence": item["confidence"],
            "content": (item["content"] or "")[:1200],
        }
        for item in session.execute(
            select(
                exam_point_evidence_links.c.evidence_chunk_id,
                exam_points.c.code.label("exam_point_code"),
                exam_point_evidence_links.c.relevance_class,
                exam_point_evidence_links.c.support_claim,
                exam_point_evidence_links.c.evidence_role,
                exam_point_evidence_links.c.confidence,
                evidence_chunks.c.material_version_id,
                evidence_chunks.c.locator,
                evidence_chunks.c.content,
            )
            .join(
                evidence_chunks,
                evidence_chunks.c.id == exam_point_evidence_links.c.evidence_chunk_id,
            )
            .join(
                exam_points,
                exam_points.c.id == exam_point_evidence_links.c.exam_point_id,
            )
            .where(
                exam_point_evidence_links.c.course_id == course_id,
                exam_point_evidence_links.c.organization_run_id == run_id,
                evidence_chunks.c.course_id == course_id,
                exam_points.c.course_id == course_id,
                # 补证据只允许改判 supporting/background（发布侧 update 的
                # where 范围）；out_of_scope 不注入——前端列为可选项会导致
                # 发布时 rowcount=0 报错，且它占链接总量九成以上，注入只会
                # 膨胀候选 payload。
                exam_point_evidence_links.c.relevance_class.in_(
                    ["supporting", "background"]
                ),
            )
            .order_by(
                exam_point_evidence_links.c.exam_point_id,
                exam_point_evidence_links.c.evidence_chunk_id,
            )
        ).mappings()
        if not _is_cover_form_block(item["content"] or "")
    ]
    # 注入考点元信息：覆盖不足的考点往往没有候选考核单元，前端从
    # topics[].units[].title 匹配不到标题，列表只能显示裸编码，教师无从
    # 判断该补哪条证据。这里从框架版本查考点标题，供前端列表/弹窗兜底展示。
    framework_version_id = payload.get("framework_version_id") or result.get("framework_version_id")
    if framework_version_id:
        payload["exam_point_labels"] = {
            row["code"]: {
                "title": row["title"],
                "assessment_requirement": row["assessment_requirement"],
            }
            for row in session.execute(
                select(
                    exam_points.c.code,
                    exam_points.c.title,
                    exam_points.c.assessment_requirement,
                )
                .where(
                    exam_points.c.course_id == course_id,
                    exam_points.c.framework_version_id == framework_version_id,
                )
                .order_by(exam_points.c.code)
            ).mappings()
        }
    result["payload"] = payload
    return result


def reject_organization_run(session: Session, *, course_id: str, run_id: str) -> dict:
    """驳回（取消）一个仍在待确认态的知识目录候选。

    教师确认前放弃构建结果时，把候选版本与组织 run 一并标记为 rejected，
    避免不想要的知识目录永远停留在 awaiting_teacher_confirmation 而囤积数据库。
    """
    candidate = get_organization_candidate(session, course_id=course_id, run_id=run_id)
    if candidate["status"] != "candidate":
        raise KnowledgePublishError(
            "knowledge catalogue candidate is no longer awaiting confirmation"
        )
    now = datetime.now(UTC)
    session.execute(
        update(knowledge_catalog_versions)
        .where(
            knowledge_catalog_versions.c.id == candidate["id"],
            knowledge_catalog_versions.c.course_id == course_id,
        )
        .values(status="rejected")
    )
    session.execute(
        update(organization_runs)
        .where(
            organization_runs.c.id == run_id,
            organization_runs.c.course_id == course_id,
        )
        .values(status="rejected", updated_at=now, completed_at=now)
    )
    session.commit()
    return get_organization_run(session, course_id=course_id, run_id=run_id)
