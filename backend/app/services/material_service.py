"""Material upload lifecycle. Completion only stages durable records."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import ObjectInfo, StoragePort
from app.db.schema import material_versions, materials, upload_sessions
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
    if request.existing_material_id is not None:
        raise UploadValidationError("updating existing materials is not supported yet")
    if request.size_bytes > max_bytes:
        raise UploadValidationError("file is too large")
    session_id = str(uuid4())
    object_key = f"courses/{course_id}/uploads/{session_id}/{request.filename}"
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
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
        )
    )
    session.commit()
    upload_url = storage.presign_put(object_key=object_key, content_type=request.mime_type, sha256=request.sha256, expires_in=expires_in)
    return {
        "session_id": session_id,
        "object_key": object_key,
        "upload_url": upload_url,
        "expires_at": expires_at,
        "headers": {"Content-Type": request.mime_type, "x-amz-meta-sha256": request.sha256},
    }, object_key


def complete_upload_session(session: Session, storage: StoragePort, *, course_id: str, session_id: str) -> dict:
    get_course(session, course_id)
    row = session.execute(select(upload_sessions).where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id)).mappings().first()
    if row is None:
        raise UploadSessionNotFoundError
    if row["status"] == "completed":
        version = session.execute(
            select(material_versions).where(material_versions.c.id == row["material_version_id"], material_versions.c.course_id == course_id)
        ).mappings().one()
        return dict(version)
    try:
        info = storage.head_object(row["object_key"])
        if info is None:
            raise StorageMismatchError("uploaded object is missing")
        if _info_value(info, "size") != row["size_bytes"]:
            raise StorageMismatchError("uploaded object size does not match")
        if _info_value(info, "content_type") != row["mime_type"]:
            raise StorageMismatchError("uploaded object MIME type does not match")
        metadata = {str(key).lower(): str(value).lower() for key, value in _info_value(info, "metadata").items()}
        digest = metadata.get("sha256") or metadata.get("x-amz-meta-sha256")
        if digest is None:
            digest = _digest_stream(storage, row["object_key"])
        if digest != row["sha256"]:
            raise StorageMismatchError("uploaded object SHA-256 does not match")
    except Exception:
        session.rollback()
        raise

    # Object I/O is deliberately outside the write transaction.  Exactly one
    # contender then claims the pending session; all durable writes commit together.
    session.rollback()
    material_id = str(uuid4())
    version_id = str(uuid4())
    completed_version: dict | None = None
    with session.begin():
        claimed = session.execute(
            update(upload_sessions)
            .where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id, upload_sessions.c.status == "pending")
            .values(status="completing")
        ).rowcount == 1
        if claimed:
            session.execute(materials.insert().values(
                id=material_id, course_id=course_id, logical_name=row["filename"], material_type=row["material_type"], status="staged"
            ))
            session.execute(material_versions.insert().values(
                id=version_id, course_id=course_id, material_id=material_id, version_no=1, sha256=row["sha256"],
                mime_type=row["mime_type"], size_bytes=row["size_bytes"], object_key=row["object_key"], status="staged"
            ))
            session.execute(
                update(upload_sessions)
                .where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id, upload_sessions.c.status == "completing")
                .values(status="completed", material_id=material_id, material_version_id=version_id)
            )
            completed_version = dict(
                session.execute(
                    select(material_versions).where(material_versions.c.id == version_id, material_versions.c.course_id == course_id)
                ).mappings().one()
            )
    if claimed:
        return completed_version

    completed = session.execute(
        select(upload_sessions).where(upload_sessions.c.id == session_id, upload_sessions.c.course_id == course_id)
    ).mappings().one_or_none()
    if completed is not None and completed["status"] == "completed":
        version = session.execute(
            select(material_versions).where(
                material_versions.c.id == completed["material_version_id"], material_versions.c.course_id == course_id
            )
        ).mappings().one()
        return dict(version)
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
    session.execute(update(materials).where(materials.c.id == material_id, materials.c.course_id == course_id).values(status="deleted"))
    session.execute(update(material_versions).where(material_versions.c.material_id == material_id, material_versions.c.course_id == course_id).values(status="deleted"))
    session.commit()
