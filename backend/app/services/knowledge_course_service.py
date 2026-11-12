"""课程资料与共享资料的显式连接：选用既有版本、保留来源、追加更新。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import StoragePort
from app.config import settings
from app.db.schema import (
    Course, User, course_knowledge_imports, knowledge_documents,
    knowledge_document_versions, knowledge_jobs, knowledge_spaces,
    material_versions, materials,
)
from app.services import knowledge_document_service as documents
from app.services.material_service import _knowledge_origins
from app.services.knowledge_space_service import (
    KnowledgeError, _not_found, list_spaces, load_actor, require_space_access,
)

SHAREABLE_TYPES = {"teaching_syllabus", "teaching_material", "exercise"}
_MIME_BY_SUFFIX = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _mime(version) -> str:
    # 课程旧上传对 Markdown 可能记录 octet-stream；按已存文件名归一化。
    if version.mime_type in documents.ALLOWED_MIME_TYPES:
        return version.mime_type
    return _MIME_BY_SUFFIX.get(PurePosixPath(version.object_key).suffix.lower(), "")


def list_course_materials(session: Session, user: User, *, course_id: str | None = None) -> list[dict]:
    """统一入口直接读取本人既有资料，不复制、不改变共享范围。"""

    courses_query = select(Course).where(Course.owner_id == user.id)
    if course_id:
        courses_query = courses_query.where(Course.id == course_id)
    courses = {course.id: course for course in session.scalars(courses_query)}
    if course_id and not courses:
        raise _not_found()
    if not courses:
        return []
    rows = session.execute(
        select(materials, Course.name.label("course_name"))
        .join(Course, Course.id == materials.c.course_id)
        .where(materials.c.course_id.in_(courses), materials.c.status != "deleted", Course.owner_id == user.id)
        .order_by(Course.name, materials.c.logical_name)
    ).mappings().all()
    latest = {}
    for row in session.execute(
        select(material_versions).where(material_versions.c.course_id.in_(courses))
        .order_by(material_versions.c.version_no.desc())
    ).mappings():
        latest.setdefault((row.course_id, row.material_id), row)
    imported = set(session.scalars(
        select(material_versions.c.material_id).join(course_knowledge_imports,
            (course_knowledge_imports.c.target_material_version_id == material_versions.c.id)
            & (course_knowledge_imports.c.course_id == material_versions.c.course_id))
        .where(material_versions.c.course_id.in_(courses))
    ))
    spaces = {space["id"]: space for space in list_spaces(session, user)}
    origins = {}
    for owned_course_id in courses:
        origins.update(_knowledge_origins(session, course_id=owned_course_id, version_ids=[
            version.id for (cid, _), version in latest.items() if cid == owned_course_id
        ]))
    shared: dict[tuple[str, str], list[dict]] = {}
    actor = load_actor(session, user)
    if spaces:
        shared_rows = session.execute(
            select(knowledge_document_versions, knowledge_documents.c.status.label("document_status"))
            .join(knowledge_documents, knowledge_documents.c.id == knowledge_document_versions.c.document_id)
            .where(
                knowledge_document_versions.c.school_id == actor.school_id,
                knowledge_document_versions.c.space_id.in_(spaces),
                knowledge_document_versions.c.import_origin["course_id"].as_string().in_(courses),
            ).order_by(knowledge_document_versions.c.version_no.desc())
        ).mappings()
        seen = set()
        for row in shared_rows:
            if row.document_id in seen or spaces[row.space_id]["role"] == "reader":
                continue
            seen.add(row.document_id)
            origin = row.import_origin
            shared.setdefault((origin["course_id"], origin["material_id"]), []).append({
                "space_id": row.space_id, "space_name": spaces[row.space_id]["name"],
                "document_id": row.document_id, "version_id": row.id, "version_no": row.version_no,
                "state": row.state, "document_status": row.document_status,
                "source_version_id": origin["material_version_id"],
            })
    result = []
    for material in rows:
        version = latest.get((material.course_id, material.id))
        if version is None:
            continue
        reason = None
        if material.material_type not in SHAREABLE_TYPES:
            reason = "考核大纲与考试保密材料不进入共享库"
        elif version.status != "staged":
            reason = "该资料版本当前不可用"
        elif material.id in imported:
            reason = "该资料来自共享库，请从原共享库选用，不能重新发布扩大范围"
        elif not _mime(version):
            reason = "此文件格式暂不支持共享"
        result.append({
            "course_id": material.course_id, "course_name": material.course_name,
            "material_id": material.id, "title": material.logical_name,
            "material_type": material.material_type, "version_id": version.id,
            "version_no": version.version_no, "size_bytes": version.size_bytes,
            "can_share": reason is None, "share_blocked_reason": reason,
            "knowledge_origin": origins.get(version.id),
            "shared_versions": shared.get((material.course_id, material.id), []),
        })
    return result


def import_course_version(
    session: Session, storage: StoragePort, user: User, *, space_id: str,
    course_id: str, material_version_id: str, expected_policy_version: int,
    confirm_shareable: bool, classification: str = "shareable_teaching",
) -> dict:
    """复制已存在的私有资料版本为待发布版本；从不自动发布或覆盖源。"""

    if not confirm_shareable:
        raise KnowledgeError(422, "SHARE_CONFIRMATION_REQUIRED", "请确认资料可在目标范围内共享")
    try:
        session.execute(select(User.id).where(User.id == user.id).with_for_update())
        actor = load_actor(session, user)
        session.execute(select(knowledge_spaces.c.id).where(
            knowledge_spaces.c.id == space_id, knowledge_spaces.c.school_id == actor.school_id,
        ).with_for_update())
        space, _role = require_space_access(session, actor, space_id=space_id, action="edit")
        if space.status != "active":
            raise KnowledgeError(409, "VERSION_CONFLICT", "知识库已归档")
        if space.policy_version != expected_policy_version:
            raise KnowledgeError(409, "VERSION_CONFLICT", "共享范围已变化，请刷新并重新确认")
        course = session.scalar(select(Course).where(Course.id == course_id, Course.owner_id == user.id).with_for_update())
        if course is None:
            raise _not_found()
        version = session.execute(select(material_versions).where(
            material_versions.c.id == material_version_id, material_versions.c.course_id == course_id,
            material_versions.c.status == "staged",
        ).with_for_update()).mappings().one_or_none()
        if version is None:
            raise _not_found()
        material = session.execute(select(materials).where(
            materials.c.id == version.material_id, materials.c.course_id == course_id,
            materials.c.status != "deleted",
        ).with_for_update()).mappings().one_or_none()
        if material is None:
            raise _not_found()
        if material.material_type not in SHAREABLE_TYPES:
            raise KnowledgeError(422, "CLASSIFICATION_FORBIDDEN", "考核大纲与考试保密材料不进入共享库")
        if session.scalar(select(course_knowledge_imports.c.id).join(material_versions,
            (material_versions.c.id == course_knowledge_imports.c.target_material_version_id)
            & (material_versions.c.course_id == course_knowledge_imports.c.course_id)).where(
            course_knowledge_imports.c.course_id == course_id,
            material_versions.c.material_id == material.id,
        )):
            raise KnowledgeError(403, "ACTION_FORBIDDEN", "共享库导入副本不能重新发布扩大范围，请使用原共享版本")
        # 只匹配该学校/空间内的同一源资料；空间行锁序列化重复点击。
        previous = session.execute(select(knowledge_document_versions).where(
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id == space_id,
            knowledge_document_versions.c.import_origin["course_id"].as_string() == course_id,
            knowledge_document_versions.c.import_origin["material_id"].as_string() == material.id,
        ).order_by(knowledge_document_versions.c.version_no.desc())).mappings().all()
        duplicate = next((row for row in previous if row.import_origin["material_version_id"] == material_version_id), None)
        if duplicate is not None:
            job_id = session.scalar(select(knowledge_jobs.c.id).where(
                knowledge_jobs.c.school_id == actor.school_id, knowledge_jobs.c.space_id == space_id,
                knowledge_jobs.c.idempotency_key == f"parse:{duplicate.id}",
            ))
            session.commit()
            return {"document_id": duplicate.document_id, "version_id": duplicate.id,
                    "version_no": duplicate.version_no, "job_id": job_id,
                    "state": duplicate.state, "idempotent": True}
        mime = _mime(version)
        if not mime or not 0 < version.size_bytes <= settings.upload_max_bytes:
            raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "源资料格式或大小不支持共享")
        content = bytearray()
        try:
            for chunk in storage.stream_object(version.object_key):
                content.extend(chunk)
                if len(content) > settings.upload_max_bytes:
                    raise KnowledgeError(413, "FILE_VALIDATION_FAILED", "源文件超过大小限制")
        except KnowledgeError:
            raise
        except Exception as exc:
            raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "源文件暂不可用，请稍后重试") from exc
        digest = hashlib.sha256(content).hexdigest()
        if digest != version.sha256 or len(content) != version.size_bytes:
            raise KnowledgeError(409, "FILE_VALIDATION_FAILED", "源资料与已保存版本不一致")
        origin = {
            "kind": "course_material", "course_id": course_id, "course_name": course.name,
            "material_id": material.id, "material_title": material.logical_name,
            "material_version_id": version.id, "version_no": version.version_no,
            "file_hash": digest, "imported_by": user.id,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        }
        result = documents.stage_document_version(
            session, user=user, space_id=space_id,
            filename=PurePosixPath(version.object_key).name, size_bytes=version.size_bytes,
            mime_type=mime, digest=digest, title=material.logical_name,
            classification=classification, document_id=previous[0].document_id if previous else None,
            store_object=lambda key: storage.put_bytes(key, bytes(content), mime), import_origin=origin,
        )
        session.commit()
        return {**result, "idempotent": False}
    except Exception:
        session.rollback()
        raise


def origin_view(session: Session, user: User, origin: dict | None) -> dict | None:
    """发布来源不授予私有课程访问权；仅课程 owner 可跳转并检查更新。"""

    if not origin or origin.get("kind") != "course_material":
        return None
    result = {"kind": "course_material", "material_title": origin.get("material_title"),
              "version_no": origin.get("version_no"), "imported_at": origin.get("imported_at")}
    course = session.scalar(select(Course).where(Course.id == origin["course_id"], Course.owner_id == user.id))
    if course is None:
        return result
    latest = session.execute(select(material_versions).join(
        materials, (materials.c.id == material_versions.c.material_id) & (materials.c.course_id == material_versions.c.course_id)
    ).where(
        material_versions.c.course_id == course.id, material_versions.c.material_id == origin["material_id"],
        material_versions.c.status == "staged", materials.c.status != "deleted",
    ).order_by(material_versions.c.version_no.desc())).mappings().first()
    return {**result, "course_id": course.id, "course_name": course.name,
            "material_id": origin["material_id"], "source_available": latest is not None,
            "latest_version_id": latest.id if latest else None,
            "latest_version_no": latest.version_no if latest else None,
            "update_available": bool(latest and latest.version_no > origin["version_no"])}
