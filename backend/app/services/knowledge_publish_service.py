"""Persistence and atomic publication for the knowledge catalogue boundary."""

from __future__ import annotations

import json
from hashlib import sha256
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    assessment_units,
    content_domains,
    framework_versions,
    index_memberships,
    index_versions,
    knowledge_cards,
    knowledge_catalog_versions,
    knowledge_evidence_links,
    organization_runs,
    content_blocks,
    material_versions,
    materials,
)
from app.domain.knowledge.models import KnowledgeTreeCandidate, KnowledgeTreeConfirmation
from app.services.knowledge_tree_service import validate_publishable_tree, KnowledgeTreeValidationError


class KnowledgePublishError(Exception):
    pass


class DatabaseKnowledgeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def persist_candidate(self, state: dict, tree: KnowledgeTreeCandidate) -> str:
        course_id = state["course_id"]
        run_id = state["run_id"]
        existing = self.session.execute(
            select(knowledge_catalog_versions.c.id).where(
                knowledge_catalog_versions.c.course_id == course_id,
                knowledge_catalog_versions.c.organization_run_id == run_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        framework_version_id = tree.framework_version_id
        version_no = self.session.scalar(
            select(func.coalesce(func.max(knowledge_catalog_versions.c.version_no), 0) + 1).where(
                knowledge_catalog_versions.c.course_id == course_id
            )
        )
        catalog_id = uuid4().hex
        self.session.execute(
            knowledge_catalog_versions.insert().values(
                id=catalog_id,
                course_id=course_id,
                organization_run_id=run_id,
                framework_version_id=framework_version_id,
                version_no=version_no,
                status="candidate",
                payload=tree.model_dump(mode="json"),
            )
        )
        self.session.execute(
            update(organization_runs)
            .where(organization_runs.c.id == run_id, organization_runs.c.course_id == course_id)
            .values(status="awaiting_teacher_confirmation", updated_at=datetime.now(UTC))
        )
        self.session.commit()
        return catalog_id

    def publish(self, state: dict, tree: KnowledgeTreeCandidate, confirmation: KnowledgeTreeConfirmation) -> dict:
        course_id = state["course_id"]
        catalog_id = state["candidate_id"]
        row = self.session.execute(
            select(knowledge_catalog_versions).where(
                knowledge_catalog_versions.c.id == catalog_id,
                knowledge_catalog_versions.c.course_id == course_id,
                knowledge_catalog_versions.c.status == "candidate",
            )
        ).mappings().one_or_none()
        if row is None:
            raise KnowledgePublishError("knowledge catalogue candidate is not available")
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
        try:
            validate_publishable_tree(tree, allowed_anchor_keys=allowed)
        except KnowledgeTreeValidationError as exc:
            raise KnowledgePublishError(str(exc)) from exc
        self._insert_tree(course_id, catalog_id, tree, allowed)
        index_no = self.session.scalar(select(func.coalesce(func.max(index_versions.c.version_no), 0) + 1).where(index_versions.c.course_id == course_id))
        index_id = uuid4().hex
        self.session.execute(
            index_versions.insert().values(
                id=index_id, course_id=course_id, catalog_version_id=catalog_id,
                version_no=index_no, status="published",
            )
        )
        for card_id in self.session.scalars(select(knowledge_cards.c.id).where(knowledge_cards.c.course_id == course_id, knowledge_cards.c.catalog_version_id == catalog_id)):
            self.session.execute(index_memberships.insert().values(id=uuid4().hex, course_id=course_id, index_version_id=index_id, knowledge_card_id=card_id))
        self.session.execute(update(knowledge_catalog_versions).where(knowledge_catalog_versions.c.course_id == course_id, knowledge_catalog_versions.c.status == "published").values(status="superseded"))
        self.session.execute(update(index_versions).where(index_versions.c.course_id == course_id, index_versions.c.status == "published", index_versions.c.id != index_id).values(status="superseded"))
        now = datetime.now(UTC)
        self.session.execute(update(knowledge_catalog_versions).where(knowledge_catalog_versions.c.id == catalog_id, knowledge_catalog_versions.c.course_id == course_id).values(status="published", published_at=now, payload=tree.model_dump(mode="json")))
        self.session.execute(update(organization_runs).where(organization_runs.c.id == state["run_id"], organization_runs.c.course_id == course_id).values(status="published", updated_at=now, completed_at=now))
        self.session.commit()
        return {"catalog_version_id": catalog_id, "index_version_id": index_id}

    def _insert_tree(self, course_id: str, catalog_id: str, tree: KnowledgeTreeCandidate, allowed: set[str]) -> None:
        roots = {}
        for anchor_key in allowed:
            root_id = uuid4().hex
            roots[anchor_key] = root_id
            self.session.execute(content_domains.insert().values(id=root_id, course_id=course_id, catalog_version_id=catalog_id, parent_domain_id=None, level=1, framework_anchor_key=anchor_key, code=anchor_key, name=anchor_key, status="active"))
        for topic in tree.topics:
            topic_id = uuid4().hex
            self.session.execute(content_domains.insert().values(id=topic_id, course_id=course_id, catalog_version_id=catalog_id, parent_domain_id=roots[topic.framework_anchor_key], level=2, framework_anchor_key=topic.framework_anchor_key, code=topic.code, name=topic.name, status=topic.status))
            for unit in topic.units:
                unit_id = uuid4().hex
                self.session.execute(assessment_units.insert().values(id=unit_id, course_id=course_id, catalog_version_id=catalog_id, content_domain_id=topic_id, code=unit.code, title=unit.title, performance_statement=unit.performance_statement, scope_boundary=unit.scope_boundary, status=unit.status))
                for card in unit.cards:
                    card_id = uuid4().hex
                    material = json.dumps({"name": card.name, "performance_statement": card.performance_statement, "assessable_content": card.assessable_content, "scope_boundary": card.scope_boundary}, ensure_ascii=False, sort_keys=True)
                    self.session.execute(knowledge_cards.insert().values(id=card_id, course_id=course_id, catalog_version_id=catalog_id, assessment_unit_id=unit_id, name=card.name, performance_statement=card.performance_statement, assessable_content=card.assessable_content, scope_boundary=card.scope_boundary, cognitive_targets=card.cognitive_targets, allowed_question_types=card.allowed_question_types, importance=card.importance, content_hash=sha256(material.encode()).hexdigest(), status=card.status, version=1))
                    for evidence_id in card.evidence_chunk_ids:
                        self.session.execute(knowledge_evidence_links.insert().values(id=uuid4().hex, course_id=course_id, knowledge_card_id=card_id, evidence_chunk_id=evidence_id, evidence_role="fact", confidence=100, teacher_confirmed=True, lifecycle_status="active"))


def create_organization_state(session: Session, *, course_id: str, material_version_ids: list[str]) -> dict:
    from app.services.course_service import get_course

    get_course(session, course_id)
    framework = session.execute(
        select(framework_versions).where(framework_versions.c.course_id == course_id, framework_versions.c.status == "published").order_by(framework_versions.c.version_no.desc()).limit(1)
    ).mappings().one_or_none()
    if framework is None:
        raise KnowledgePublishError("published framework is required")
    if not material_version_ids or len(material_version_ids) != len(set(material_version_ids)):
        raise KnowledgePublishError("at least one unique material version is required")
    files = []
    for version_id in material_version_ids:
        material_type = session.execute(select(materials.c.material_type).join(material_versions, material_versions.c.material_id == materials.c.id).where(material_versions.c.id == version_id, material_versions.c.course_id == course_id)).scalar_one_or_none()
        if material_type not in {"teaching_material", "exercise"}:
            raise KnowledgePublishError("only teaching materials or exercises can be organized")
        blocks = session.scalars(select(content_blocks.c.text).where(content_blocks.c.course_id == course_id, content_blocks.c.material_version_id == version_id).order_by(content_blocks.c.reading_order)).all()
        if not blocks:
            raise KnowledgePublishError("selected material has no parsed content")
        files.append({"material_version_id": version_id, "blocks": [item for item in blocks if item.strip()]})
    run_id = uuid4().hex
    session.execute(organization_runs.insert().values(id=run_id, course_id=course_id, framework_version_id=framework["id"], status="running", input_snapshot={"material_version_ids": material_version_ids}))
    session.commit()
    return {"course_id": course_id, "run_id": run_id, "framework_version_id": framework["id"], "framework_anchors": framework["payload"].get("anchors", []), "files": files}


def get_organization_run(session: Session, *, course_id: str, run_id: str) -> dict:
    row = session.execute(select(organization_runs).where(organization_runs.c.id == run_id, organization_runs.c.course_id == course_id)).mappings().one_or_none()
    if row is None:
        raise KnowledgePublishError("organization run not found")
    return dict(row)


def get_organization_candidate(session: Session, *, course_id: str, run_id: str) -> dict:
    row = session.execute(select(knowledge_catalog_versions).where(knowledge_catalog_versions.c.course_id == course_id, knowledge_catalog_versions.c.organization_run_id == run_id).order_by(knowledge_catalog_versions.c.version_no.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        raise KnowledgePublishError("knowledge catalogue candidate not found")
    return dict(row)
