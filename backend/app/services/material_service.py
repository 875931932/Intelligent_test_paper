"""Material upload lifecycle. Completion only stages durable records."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import ObjectInfo, StoragePort, StoragePreconditionError, StorageUnavailableError
from app.db.schema import (
    evidence_chunks,
    exam_point_evidence_links,
    index_memberships,
    index_versions,
    knowledge_cards,
    knowledge_evidence_links,
    material_versions,
    materials,
    upload_sessions,
)
from app.domain.material.models import UploadSessionCreate
from app.services.course_service import get_course


class MaterialNotFoundError(Exception):
    pass


class UploadSessionNotFoundError(Exception):
    pass


class UploadValidationError(Exception):
    pass


class StorageMismatchError(Exception):
    pass


class UploadCompletionConflictError(Exception):
    pass


class UploadExpiredError(Exception):
    pass


class MaterialConflictError(Exception):
    pass


def _info_value(info: ObjectInfo | dict, name: str):
    return info[name] if isinstance(info, dict) else getattr(info, name)


def _digest_stream(storage: StoragePort, object_key: str) -> str:
    digest = hashlib.sha256()
    for chunk in storage.stream_object(object_key):
        digest.update(chunk)
    return digest.hexdigest()


def create_upload_session(
    session: Session, storage: StoragePort, *, course_id: str, request: UploadSessionCreate, max_bytes: int, expires_in: int = 900
) -> tuple[dict, str]:
    get_course(session, course_id)
    if request.size_bytes > max_bytes:
        raise UploadValidationError("file is too large")
    target_material_id = request.existing_material_id
    if target_material_id is not None:
        target = session.execute(
            select(materials).where(materials.c.id == target_material_id, materials.c.course_id == course_id)
        ).mappings().first()
        if target is None:
            raise MaterialNotFoundError
    else:
        same_name = session.execute(
            select(materials).where(materials.c.course_id == course_id, materials.c.logical_name == request.filename)
        ).mappings().first()
        if same_name is not None:
            if same_name["status"] != "deleted":
                raise MaterialConflictError("an active material already uses this filename")
            target_material_id = same_name["id"]
    session.rollback()
    session_id = str(uuid4())
    object_key = f"courses/{course_id}/uploads/{session_id}/{request.filename}"
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    try:
        upload_url = storage.presign_put(object_key=object_key, content_type=request.mime_type, sha256=request.sha256, expires_in=expires_in)
    except Exception as exc:
        raise StorageUnavailableError from exc
    session.execute(
        upload_sessions.insert().values(
            id=session_id,
            course_id=course_id,
            session_key=session_id,
            filename=request.filename,
            material_type=request.material_type,
            size_bytes=request.size_bytes,
            sha256=request.sha256,
            mime_type=request.mime_type,
            object_key=object_key,
            expires_at=expires_at,
            status="pending",
            material_id=target_material_id,
        )
    )
    session.commit()
    return {
        "session_id": session_id,
        "object_key": object_key,
        "upload_url": upload_url,
        "expires_at": expires_at,
        "headers": {"Content-Type": request.mime_type, "x-amz-meta-sha256": request.sha256},
    }, object_key


def complete_upload_session(session: Session, storage: StoragePort, *, course_id: str, session_id: str) -> dict:
    get_course(session, course_id)
    row_result = session.execute(select(upload_sessions).where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id)).mappings().first()
    row = dict(row_result) if row_result is not None else None
    if row is None:
        raise UploadSessionNotFoundError
    if row["status"] == "completed":
        version = session.execute(
            select(material_versions).where(material_versions.c.id == row["material_version_id"], material_versions.c.course_id == course_id)
        ).mappings().one()
        return dict(version)
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    session.rollback()
    if expires_at <= now:
        raise UploadExpiredError("upload session has expired")
    try:
        info = storage.head_object(row["object_key"])
        if info is None:
            raise StorageMismatchError("uploaded object is missing")
        if _info_value(info, "size") != row["size_bytes"]:
            raise StorageMismatchError("uploaded object size does not match")
        if _info_value(info, "content_type") != row["mime_type"]:
            raise StorageMismatchError("uploaded object MIME type does not match")
        digest = _digest_stream(storage, row["object_key"])
        if digest != row["sha256"]:
            raise StorageMismatchError("uploaded object SHA-256 does not match")
        source_etag = _info_value(info, "etag")
        if not source_etag:
            raise StorageUnavailableError
    except (StorageMismatchError, StorageUnavailableError):
        raise
    except Exception as exc:
        raise StorageUnavailableError from exc

    material_id = row["material_id"] or str(uuid4())
    version_id = str(uuid4())
    final_object_key = f"courses/{course_id}/materials/{material_id}/versions/{version_id}/{row['filename']}"
    try:
        storage.finalize_object(row["object_key"], final_object_key, source_etag)
    except StoragePreconditionError:
        raise
    except Exception as exc:
        raise StorageUnavailableError from exc

    completed_version: dict | None = None
    claim_now = datetime.now(UTC)
    try:
        with session.begin():
            claimed = session.execute(
                update(upload_sessions)
                .where(
                    upload_sessions.c.id == session_id,
                    upload_sessions.c.course_id == course_id,
                    upload_sessions.c.status == "pending",
                    upload_sessions.c.expires_at > claim_now,
                )
                .values(status="completing")
            ).rowcount == 1
            if not claimed:
                return _completed_or_expired(session, course_id=course_id, session_id=session_id, now=claim_now)

            target = session.execute(
                select(materials).where(materials.c.id == material_id, materials.c.course_id == course_id)
            ).mappings().first()
            if target is None:
                same_name = session.execute(
                    select(materials).where(materials.c.course_id == course_id, materials.c.logical_name == row["filename"])
                ).mappings().first()
                if same_name is not None:
                    if same_name["status"] != "deleted":
                        raise MaterialConflictError("an active material already uses this filename")
                    material_id = same_name["id"]
                else:
                    session.execute(materials.insert().values(
                        id=material_id, course_id=course_id, logical_name=row["filename"], material_type=row["material_type"], status="staged"
                    ))
            else:
                session.execute(
                    update(materials)
                    .where(materials.c.id == material_id, materials.c.course_id == course_id)
                    .values(status="staged", material_type=row["material_type"])
                )
            version_no = session.scalar(
                select(func.coalesce(func.max(material_versions.c.version_no), 0) + 1).where(
                    material_versions.c.course_id == course_id, material_versions.c.material_id == material_id
                )
            )
            session.execute(material_versions.insert().values(
                id=version_id, course_id=course_id, material_id=material_id, version_no=version_no, sha256=row["sha256"],
                mime_type=row["mime_type"], size_bytes=row["size_bytes"], object_key=final_object_key, status="staged"
            ))
            session.execute(
                update(upload_sessions)
                .where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id, upload_sessions.c.status == "completing")
                .values(status="completed", material_id=material_id, material_version_id=version_id, completed_at=claim_now)
            )
            completed_version = dict(
                session.execute(
                    select(material_versions).where(material_versions.c.id == version_id, material_versions.c.course_id == course_id)
                ).mappings().one()
            )
        return completed_version
    except IntegrityError as exc:
        session.rollback()
        raise MaterialConflictError("material version conflicts with an existing record") from exc


def _completed_or_expired(session: Session, *, course_id: str, session_id: str, now: datetime) -> dict:
    current = session.execute(
        select(upload_sessions).where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id)
    ).mappings().one_or_none()
    if current is not None and current["status"] == "completed":
        return dict(
            session.execute(
                select(material_versions).where(
                    material_versions.c.id == current["material_version_id"], material_versions.c.course_id == course_id
                )
            ).mappings().one()
        )
    if current is not None:
        expires_at = current["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise UploadExpiredError("upload session has expired")
    raise UploadCompletionConflictError("upload session completion did not finish")


def _version_response(session: Session, *, course_id: str, material_id: str) -> dict | None:
    value = session.execute(
        select(material_versions).where(material_versions.c.course_id == course_id, material_versions.c.material_id == material_id)
        .order_by(material_versions.c.version_no.desc())
    ).mappings().first()
    return dict(value) if value else None


def _material_response(session: Session, row) -> dict:
    value = dict(row)
    value["latest_version"] = _version_response(session, course_id=value["course_id"], material_id=value["id"])
    return value


def list_materials(session: Session, *, course_id: str, include_deleted: bool) -> list[dict]:
    get_course(session, course_id)
    statement = select(materials).where(materials.c.course_id == course_id)
    if not include_deleted:
        statement = statement.where(materials.c.status != "deleted")
    return [_material_response(session, row) for row in session.execute(statement.order_by(materials.c.logical_name)).mappings()]


def get_material(session: Session, *, course_id: str, material_id: str) -> dict:
    get_course(session, course_id)
    row = session.execute(select(materials).where(materials.c.id == material_id, materials.c.course_id == course_id)).mappings().first()
    if row is None:
        raise MaterialNotFoundError
    return _material_response(session, row)


def delete_material(session: Session, *, course_id: str, material_id: str) -> None:
    get_material(session, course_id=course_id, material_id=material_id)
    version_ids = list(
        session.scalars(
            select(material_versions.c.id).where(
                material_versions.c.material_id == material_id,
                material_versions.c.course_id == course_id,
            )
        )
    )
    evidence_ids = list(
        session.scalars(
            select(evidence_chunks.c.id).where(
                evidence_chunks.c.course_id == course_id,
                evidence_chunks.c.material_version_id.in_(version_ids),
            )
        )
    ) if version_ids else []
    affected_candidates = list(
        session.scalars(
            select(knowledge_evidence_links.c.knowledge_card_id)
            .where(
                knowledge_evidence_links.c.course_id == course_id,
                knowledge_evidence_links.c.evidence_chunk_id.in_(evidence_ids),
            )
            .distinct()
        )
    ) if evidence_ids else []
    try:
        if evidence_ids:
            session.execute(
                update(exam_point_evidence_links)
                .where(
                    exam_point_evidence_links.c.course_id == course_id,
                    exam_point_evidence_links.c.evidence_chunk_id.in_(evidence_ids),
                )
                .values(status="source_deleted")
            )
            session.execute(
                update(knowledge_evidence_links)
                .where(
                    knowledge_evidence_links.c.course_id == course_id,
                    knowledge_evidence_links.c.evidence_chunk_id.in_(evidence_ids),
                )
                .values(lifecycle_status="source_deleted")
            )
        affected_cards = []
        for card_id in affected_candidates:
            active_direct = session.execute(
                select(knowledge_evidence_links.c.id).where(
                    knowledge_evidence_links.c.course_id == course_id,
                    knowledge_evidence_links.c.knowledge_card_id == card_id,
                    knowledge_evidence_links.c.lifecycle_status == "active",
                ).limit(1)
            ).scalar_one_or_none()
            if active_direct is None:
                affected_cards.append(card_id)
        if affected_cards:
            session.execute(
                update(knowledge_cards)
                .where(
                    knowledge_cards.c.course_id == course_id,
                    knowledge_cards.c.id.in_(affected_cards),
                )
                .values(status="affected_by_source_deletion")
            )
            current_index_ids = select(index_versions.c.id).where(
                index_versions.c.course_id == course_id,
                index_versions.c.status == "published",
            )
            session.execute(
                delete(index_memberships).where(
                    index_memberships.c.course_id == course_id,
                    index_memberships.c.index_version_id.in_(current_index_ids),
                    index_memberships.c.knowledge_card_id.in_(affected_cards),
                )
            )
        session.execute(
            update(materials)
            .where(materials.c.id == material_id, materials.c.course_id == course_id)
            .values(status="deleted")
        )
        session.execute(
            update(material_versions)
            .where(
                material_versions.c.material_id == material_id,
                material_versions.c.course_id == course_id,
            )
            .values(status="deleted")
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
