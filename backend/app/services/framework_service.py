"""Framework persistence and syllabus input preparation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    content_blocks,
    document_parse_runs,
    framework_anchors,
    framework_build_runs,
    framework_conflicts,
    framework_versions,
    material_versions,
    materials,
)
from app.domain.framework.models import FrameworkCandidate, FrameworkConfirmation
from app.services.course_service import get_course


class FrameworkNotFoundError(Exception):
    pass


class FrameworkInputError(Exception):
    pass


def create_framework_run(
    session: Session,
    *,
    course_id: str,
    teaching_material_version_id: str,
    assessment_material_version_id: str,
) -> dict:
    get_course(session, course_id)
    teaching_blocks = _ready_blocks(session, course_id, teaching_material_version_id, "teaching_syllabus")
    assessment_blocks = _ready_blocks(session, course_id, assessment_material_version_id, "assessment_syllabus")
    run_id = uuid4().hex
    snapshot = {
        "teaching_material_version_id": teaching_material_version_id,
        "assessment_material_version_id": assessment_material_version_id,
    }
    session.execute(
        framework_build_runs.insert().values(
            id=run_id,
            course_id=course_id,
            status="running",
            input_snapshot=snapshot,
        )
    )
    session.commit()
    return {
        "course_id": course_id,
        "run_id": run_id,
        **snapshot,
        "teaching_blocks": teaching_blocks,
        "assessment_blocks": assessment_blocks,
    }


class DatabaseFrameworkRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def persist_candidate(self, state: dict, candidate: FrameworkCandidate) -> str:
        course_id = state["course_id"]
        run_id = state["run_id"]
        existing_version_id = self.session.execute(
            select(framework_versions.c.id).where(
                framework_versions.c.framework_build_run_id == run_id,
                framework_versions.c.course_id == course_id,
            )
        ).scalar_one_or_none()
        if existing_version_id is not None:
            return existing_version_id
        run = self.session.execute(
            select(framework_build_runs.c.id).where(
                framework_build_runs.c.id == run_id,
                framework_build_runs.c.course_id == course_id,
            )
        ).scalar_one_or_none()
        if run is None:
            raise FrameworkNotFoundError
        version_no = self.session.scalar(
            select(func.coalesce(func.max(framework_versions.c.version_no), 0) + 1).where(framework_versions.c.course_id == course_id)
        )
        version_id = uuid4().hex
        self.session.execute(
            framework_versions.insert().values(
                id=version_id,
                course_id=course_id,
                framework_build_run_id=run_id,
                version_no=version_no,
                status="candidate",
                payload=candidate.model_dump(mode="json"),
            )
        )
        self._replace_candidate_rows(course_id, version_id, candidate)
        self.session.execute(
            update(framework_build_runs)
            .where(framework_build_runs.c.id == run_id, framework_build_runs.c.course_id == course_id)
            .values(status="awaiting_teacher_confirmation", updated_at=datetime.now(UTC))
        )
        self.session.commit()
        return version_id

    def publish(self, state: dict, confirmation: FrameworkConfirmation) -> str:
        course_id = state["course_id"]
        version_id = state["candidate_id"]
        row = self.session.execute(
            select(framework_versions).where(
                framework_versions.c.id == version_id,
                framework_versions.c.course_id == course_id,
                framework_versions.c.status == "candidate",
            )
        ).mappings().one_or_none()
        if row is None:
            raise FrameworkNotFoundError
        conflicts = self.session.execute(
            select(framework_conflicts).where(
                framework_conflicts.c.framework_version_id == version_id,
                framework_conflicts.c.course_id == course_id,
                framework_conflicts.c.status == "open",
            )
        ).mappings().all()
        open_keys = {conflict["details"].get("key") for conflict in conflicts}
        missing_resolutions = open_keys - set(confirmation.conflict_resolutions)
        if missing_resolutions:
            raise FrameworkInputError("every open conflict requires a teacher resolution")
        unknown_resolutions = set(confirmation.conflict_resolutions) - open_keys
        if unknown_resolutions:
            raise FrameworkInputError("confirmation contains an unknown conflict resolution")
        payload = dict(row["payload"])
        payload["anchors"] = [anchor.model_dump(mode="json") for anchor in confirmation.anchors]
        payload["teacher_exclusions"] = confirmation.teacher_exclusions
        payload["conflict_resolutions"] = confirmation.conflict_resolutions
        self.session.execute(
            update(framework_versions)
            .where(framework_versions.c.course_id == course_id, framework_versions.c.status == "published")
            .values(status="superseded")
        )
        self.session.execute(
            update(framework_versions)
            .where(framework_versions.c.id == version_id, framework_versions.c.course_id == course_id)
            .values(status="published", payload=payload, published_at=datetime.now(UTC))
        )
        self.session.execute(
            delete(framework_anchors).where(
                framework_anchors.c.framework_version_id == version_id,
                framework_anchors.c.course_id == course_id,
                framework_anchors.c.anchor_type == "assessment_scope",
            )
        )
        for anchor in confirmation.anchors:
            self.session.execute(
                framework_anchors.insert().values(
                    id=uuid4().hex,
                    course_id=course_id,
                    framework_version_id=version_id,
                    anchor_type="assessment_scope",
                    anchor_key=anchor.key,
                    payload=anchor.model_dump(mode="json"),
                )
            )
        for conflict in conflicts:
            details = dict(conflict["details"])
            details["teacher_resolution"] = confirmation.conflict_resolutions[details["key"]]
            self.session.execute(
                update(framework_conflicts)
                .where(framework_conflicts.c.id == conflict["id"], framework_conflicts.c.course_id == course_id)
                .values(status="resolved", details=details)
            )
        if row["framework_build_run_id"]:
            self.session.execute(
                update(framework_build_runs)
                .where(framework_build_runs.c.id == row["framework_build_run_id"], framework_build_runs.c.course_id == course_id)
                .values(status="published", updated_at=datetime.now(UTC), completed_at=datetime.now(UTC))
            )
        self.session.commit()
        return version_id

    def _replace_candidate_rows(self, course_id: str, version_id: str, candidate: FrameworkCandidate) -> None:
        for anchor in candidate.anchors:
            self.session.execute(
                framework_anchors.insert().values(
                    id=uuid4().hex,
                    course_id=course_id,
                    framework_version_id=version_id,
                    anchor_type="assessment_scope",
                    anchor_key=anchor.key,
                    payload=anchor.model_dump(mode="json"),
                )
            )
        for topic in candidate.teaching_topics:
            self.session.execute(
                framework_anchors.insert().values(
                    id=uuid4().hex,
                    course_id=course_id,
                    framework_version_id=version_id,
                    anchor_type="teaching_topic",
                    anchor_key=topic.key,
                    payload=topic.model_dump(mode="json"),
                )
            )
        for conflict in candidate.conflicts:
            self.session.execute(
                framework_conflicts.insert().values(
                    id=uuid4().hex,
                    course_id=course_id,
                    framework_version_id=version_id,
                    status=conflict.status,
                    details=conflict.model_dump(mode="json"),
                )
            )


def get_framework_candidate(session: Session, *, course_id: str, run_id: str) -> dict:
    row = session.execute(
        select(framework_versions).where(
            framework_versions.c.course_id == course_id,
            framework_versions.c.framework_build_run_id == run_id,
        ).order_by(framework_versions.c.version_no.desc()).limit(1)
    ).mappings().one_or_none()
    if row is None:
        raise FrameworkNotFoundError
    return dict(row)


def get_framework_run(session: Session, *, course_id: str, run_id: str) -> dict:
    row = session.execute(
        select(framework_build_runs).where(
            framework_build_runs.c.id == run_id,
            framework_build_runs.c.course_id == course_id,
        )
    ).mappings().one_or_none()
    if row is None:
        raise FrameworkNotFoundError
    return dict(row)


def confirm_framework_run(
    session: Session,
    *,
    course_id: str,
    run_id: str,
    confirmation: FrameworkConfirmation,
) -> dict:
    candidate = get_framework_candidate(session, course_id=course_id, run_id=run_id)
    if candidate["status"] != "candidate":
        raise FrameworkInputError("framework candidate is no longer awaiting confirmation")
    repository = DatabaseFrameworkRepository(session)
    repository.publish(
        {"course_id": course_id, "run_id": run_id, "candidate_id": candidate["id"]},
        confirmation,
    )
    return get_current_framework(session, course_id=course_id)


def reject_framework_run(session: Session, *, course_id: str, run_id: str) -> dict:
    candidate = get_framework_candidate(session, course_id=course_id, run_id=run_id)
    if candidate["status"] != "candidate":
        raise FrameworkInputError("framework candidate is no longer awaiting confirmation")
    now = datetime.now(UTC)
    session.execute(
        update(framework_versions)
        .where(framework_versions.c.id == candidate["id"], framework_versions.c.course_id == course_id)
        .values(status="rejected")
    )
    session.execute(
        update(framework_build_runs)
        .where(framework_build_runs.c.id == run_id, framework_build_runs.c.course_id == course_id)
        .values(status="rejected", updated_at=now, completed_at=now)
    )
    session.commit()
    return get_framework_run(session, course_id=course_id, run_id=run_id)


def fail_framework_run(
    session: Session,
    *,
    course_id: str,
    run_id: str,
    error_code: str,
    error_message: str,
) -> None:
    now = datetime.now(UTC)
    session.execute(
        update(framework_build_runs)
        .where(framework_build_runs.c.id == run_id, framework_build_runs.c.course_id == course_id)
        .values(
            status="failed",
            error_code=error_code,
            error_message=error_message[:2000],
            updated_at=now,
            completed_at=now,
        )
    )
    session.commit()


def get_current_framework(session: Session, *, course_id: str) -> dict:
    row = session.execute(
        select(framework_versions).where(
            framework_versions.c.course_id == course_id,
            framework_versions.c.status == "published",
        ).order_by(framework_versions.c.version_no.desc()).limit(1)
    ).mappings().one_or_none()
    if row is None:
        raise FrameworkNotFoundError
    return dict(row)


def _ready_blocks(session: Session, course_id: str, material_version_id: str, expected_type: str) -> list[str]:
    material_type = session.execute(
        select(materials.c.material_type)
        .join(material_versions, material_versions.c.material_id == materials.c.id)
        .where(material_versions.c.id == material_version_id, material_versions.c.course_id == course_id)
    ).scalar_one_or_none()
    if material_type != expected_type:
        raise FrameworkInputError(f"{expected_type} material version is required")
    run_id = session.execute(
        select(document_parse_runs.c.id)
        .where(
            document_parse_runs.c.course_id == course_id,
            document_parse_runs.c.material_version_id == material_version_id,
            document_parse_runs.c.status == "ready",
        )
        .order_by(document_parse_runs.c.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_id is None:
        raise FrameworkInputError("syllabus document is not parsed and ready")
    blocks = session.execute(
        select(content_blocks.c.text)
        .where(content_blocks.c.course_id == course_id, content_blocks.c.document_parse_run_id == run_id)
        .order_by(content_blocks.c.reading_order)
    ).scalars().all()
    values = [value for value in blocks if value.strip()]
    if not values:
        raise FrameworkInputError("syllabus parsed content is empty")
    return values
