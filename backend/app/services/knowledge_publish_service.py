"""Persistence and atomic publication for exam-point-led knowledge catalogues."""

from __future__ import annotations

import json
import math
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

    def load_evidence_chunks(
        self,
        *,
        course_id: str,
        run_id: str,
        evidence_chunk_ids: list[str],
    ) -> list[StagingChunk]:
        if not evidence_chunk_ids:
            return []
        rows = self.session.execute(
            select(evidence_chunks).where(
                evidence_chunks.c.course_id == course_id,
                evidence_chunks.c.organization_run_id == run_id,
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
                            evidence_chunks.c.organization_run_id == run_id,
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
                try:
                    validate_publishable_tree(
                        tree,
                        allowed_anchor_keys=allowed,
                        allowed_exam_point_codes=set(points_by_code),
                        exam_points_by_code=points_by_code,
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

        point_ids = {item["code"]: item["id"] for item in point_rows}
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
                evidence_chunks.c.organization_run_id == run_id,
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


def create_organization_state(
    session: Session,
    course_id: str,
    material_version_ids: list[str],
    embedder,
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
        if not blocks:
            raise KnowledgePublishError("selected material has no ready parsed content")
        selected_blocks.append((version_id, blocks))

    embedded_blocks: list[tuple[str, dict, list[float]]] = []
    embedding_dimension: int | None = None
    for version_id, blocks in selected_blocks:
        texts = [block["text"].strip() for block in blocks]
        try:
            vectors = _validated_embeddings(
                embedder.embed(texts),
                expected=len(texts),
                expected_dimension=embedding_dimension,
            )
        except KnowledgePublishError:
            raise
        except Exception as exc:
            raise KnowledgePublishError("embedding service is unavailable") from exc
        embedding_dimension = len(vectors[0])
        embedded_blocks.extend(zip([version_id] * len(blocks), blocks, vectors, strict=True))

    run_id = uuid4().hex
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
        for chunk_index, (version_id, block, vector) in enumerate(embedded_blocks):
            evidence_id = uuid4().hex
            evidence_ids.append(evidence_id)
            locator = {
                "page_index": block["page_index"],
                "bbox": block["bbox"],
                "heading_path": block["heading_path"] or [],
                "reading_order": block["reading_order"],
                "block_type": block["block_type"],
            }
            session.execute(
                evidence_chunks.insert().values(
                    id=evidence_id,
                    course_id=course_id,
                    organization_run_id=run_id,
                    material_version_id=version_id,
                    content_block_id=block["id"],
                    chunk_index=chunk_index,
                    content=block["text"].strip(),
                    content_hash=block["content_hash"] or sha256(block["text"].encode()).hexdigest(),
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
            )
            .order_by(
                exam_point_evidence_links.c.exam_point_id,
                exam_point_evidence_links.c.evidence_chunk_id,
            )
        ).mappings()
    ]
    result["payload"] = payload
    return result
