"""知识库资料固定版本导入课程（P3.2）。

双边授权：源侧要求 ``read + allow_course_import`` 且版本为当前有效发布版本；
目标侧要求课程 owner 为当前账号。导入是**独立副本**：复制文件字节与新的课程
版本，不建立跨域硬外键；知识库撤回、源文档删除或课程删除都不会误伤另一域。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import StoragePort
from app.config import settings
from app.db.schema import (
    Course,
    User,
    course_knowledge_imports,
    knowledge_audit_events,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_spaces,
    material_versions,
    materials,
)
from app.domain.knowledge_base.models import ACTION_FORBIDDEN, RESOURCE_NOT_FOUND, VERSION_CONFLICT
from app.services.course_service import CourseNotFoundError, get_owned_course
from app.services.knowledge_space_service import (
    KnowledgeError,
    _aware,
    _servable,
    load_actor,
    require_space_access,
)

ALLOWED_MATERIAL_TYPES = {"teaching_syllabus", "assessment_syllabus", "teaching_material", "exercise"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _not_found() -> KnowledgeError:
    return KnowledgeError(404, RESOURCE_NOT_FOUND, "resource not found")


def _unique_material_name(session: Session, *, course_id: str, base: str) -> str:
    """课程内资料名唯一；重复导入自动加后缀，不覆盖已有资料。"""

    candidate = base[:200] or "知识库导入资料"
    index = 1
    while True:
        exists = session.execute(
            select(materials.c.id).where(
                materials.c.course_id == course_id,
                materials.c.logical_name == candidate,
            )
        ).scalar_one_or_none()
        if exists is None:
            return candidate
        index += 1
        candidate = f"{base[:180]}（知识库导入 {index}）"


def import_fixed_version(
    session: Session,
    storage: StoragePort,
    user: User,
    *,
    space_id: str,
    document_id: str,
    version_id: str,
    course_id: str,
    material_type: str = "teaching_material",
    title: str | None = None,
) -> dict:
    """把知识库某个已发布固定版本复制为课程独立资料版本。"""

    session.execute(select(User.id).where(User.id == user.id).with_for_update())
    actor = load_actor(session, user)
    session.execute(select(knowledge_spaces.c.id).where(
        knowledge_spaces.c.id == space_id, knowledge_spaces.c.school_id == actor.school_id,
    ).with_for_update())
    try:
        space, _role = require_space_access(session, actor, space_id=space_id, action="read")
    except KnowledgeError:
        raise _not_found()
    if not bool(space.allow_course_import):
        raise KnowledgeError(403, ACTION_FORBIDDEN, "该知识库未开启导入课程")
    if material_type not in ALLOWED_MATERIAL_TYPES:
        raise KnowledgeError(422, VERSION_CONFLICT, "非法的课程资料类型")

    document = session.execute(
        select(knowledge_documents).where(
            knowledge_documents.c.id == document_id,
            knowledge_documents.c.school_id == actor.school_id,
            knowledge_documents.c.space_id == space_id,
        ).with_for_update()
    ).mappings().one_or_none()
    version = session.execute(
        select(knowledge_document_versions).where(
            knowledge_document_versions.c.id == version_id,
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id == space_id,
        )
    ).mappings().one_or_none()
    if document is None or version is None or version.document_id != document_id:
        raise _not_found()
    if (
        document.status != "published"
        or document.published_version_id != version_id
        or version.state != "published"
        or not _servable(version, datetime.now(timezone.utc))
    ):
        raise KnowledgeError(409, VERSION_CONFLICT, "只能导入当前有效的已发布版本")

    try:
        course = get_owned_course(session, user.id, course_id)
    except CourseNotFoundError:
        raise _not_found()

    # 同一课程的重复选用/版本追加串行化，避免创建两份同源资料。
    session.execute(select(Course.id).where(Course.id == course_id, Course.owner_id == user.id).with_for_update())

    origin = version.import_origin or {}
    if origin.get("kind") == "course_material" and origin.get("course_id") == course_id:
        original = session.execute(select(material_versions).join(
            materials, (materials.c.id == material_versions.c.material_id)
            & (materials.c.course_id == material_versions.c.course_id),
        ).where(
            material_versions.c.course_id == course_id,
            material_versions.c.id == origin.get("material_version_id"),
            material_versions.c.status == "staged", materials.c.status != "deleted",
        )).mappings().one_or_none()
        if original is not None and original.sha256 == version.file_hash:
            return {"state": "succeeded", "idempotent": True, "reused_source": True,
                    "course_id": course_id, "material_id": original.material_id,
                    "material_version_id": original.id, "source_version_id": version_id}

    existing = session.execute(
        select(course_knowledge_imports).where(
            course_knowledge_imports.c.course_id == course_id,
            course_knowledge_imports.c.source_version_id == version_id,
        )
    ).mappings().one_or_none()
    if existing is not None and existing.state == "succeeded":
        retained = session.execute(select(material_versions.c.material_id).join(
            materials, (materials.c.id == material_versions.c.material_id) & (materials.c.course_id == material_versions.c.course_id)
        ).where(
            material_versions.c.id == existing.target_material_version_id,
            material_versions.c.course_id == course_id, materials.c.status != "deleted",
        )).scalar_one_or_none()
        if retained is None:
            raise KnowledgeError(409, VERSION_CONFLICT, "该版本的课程副本已删除；请选用其他资料或源资料的后续发布版本")
        return {
            "state": "succeeded",
            "idempotent": True,
            "course_id": course_id,
            "material_id": retained,
            "material_version_id": existing.target_material_version_id,
            "source_version_id": version_id,
        }

    # 读取源文件并校验哈希；导入的是内容快照，不依赖源对象后续是否存活。
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in storage.stream_object(version.object_key):
            total += len(chunk)
            if total > settings.upload_max_bytes:
                raise KnowledgeError(409, VERSION_CONFLICT, "源文件超过课程导入大小限制")
            digest.update(chunk)
            chunks.append(chunk)
    except KnowledgeError:
        raise
    except Exception as exc:
        raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "源文件暂不可用，请稍后重试") from exc
    content = b"".join(chunks)
    if digest.hexdigest() != version.file_hash:
        raise KnowledgeError(409, VERSION_CONFLICT, "源文件哈希校验失败，请重新发布后再导入")

    filename = PurePosixPath(version.object_key).name or "knowledge-import"
    base_name = (title or document.title or PurePosixPath(filename).stem).strip()
    prior = session.execute(select(
        material_versions.c.id, material_versions.c.material_id, materials.c.logical_name,
    ).join(
        course_knowledge_imports,
        (course_knowledge_imports.c.target_material_version_id == material_versions.c.id)
        & (course_knowledge_imports.c.course_id == material_versions.c.course_id),
    ).join(materials, (materials.c.id == material_versions.c.material_id) & (materials.c.course_id == material_versions.c.course_id)).where(
        course_knowledge_imports.c.course_id == course_id,
        course_knowledge_imports.c.source_document_id == document_id,
        course_knowledge_imports.c.space_id == space_id,
        course_knowledge_imports.c.school_id == actor.school_id,
        course_knowledge_imports.c.state == "succeeded",
        materials.c.status != "deleted",
    ).order_by(material_versions.c.version_no.desc())).mappings().first()
    if prior:
        latest_id = session.scalar(select(material_versions.c.id).where(
            material_versions.c.course_id == course_id, material_versions.c.material_id == prior.material_id,
        ).order_by(material_versions.c.version_no.desc()).limit(1))
        if latest_id != prior.id:
            raise KnowledgeError(409, VERSION_CONFLICT, "课程资料已有本地新版本，请先核对，不能直接更新共享副本")
    logical_name = prior.logical_name if prior else _unique_material_name(session, course_id=course_id, base=base_name)
    material_id = prior.material_id if prior else uuid4().hex
    new_version_id = uuid4().hex
    object_key = f"courses/{course_id}/materials/{material_id}/versions/{new_version_id}/{filename}"

    try:
        storage.put_bytes(object_key, content, version.mime_type)
    except Exception as exc:
        raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "对象存储不可用") from exc

    now = _now()
    try:
        if prior is None:
            session.execute(materials.insert().values(
                id=material_id,
                course_id=course_id,
                logical_name=logical_name,
                material_type=material_type,
                status="staged",
            ))
        version_no = session.scalar(
            select(func.coalesce(func.max(material_versions.c.version_no), 0) + 1).where(
                material_versions.c.course_id == course_id,
                material_versions.c.material_id == material_id,
            )
        )
        session.execute(
            material_versions.insert().values(
                id=new_version_id,
                course_id=course_id,
                material_id=material_id,
                version_no=int(version_no),
                sha256=digest.hexdigest(),
                mime_type=version.mime_type,
                size_bytes=total,
                object_key=object_key,
                status="staged",
                created_at=now,
            )
        )
        receipt = {
            "source_space_id": space_id,
            "read_scope": space.read_scope,
            "policy_version": int(space.policy_version),
            "classification": document.classification,
            "allow_course_import": True,
            "imported_by": user.id,
            "imported_at": now.isoformat(),
            "course_id": course.id,
            "independent_copy": True,
            "source_space_name": space.name,
            "source_version_no": version.version_no,
        }
        if existing is not None:
            session.execute(
                update(course_knowledge_imports)
                .where(course_knowledge_imports.c.id == existing.id)
                .values(
                    target_material_version_id=new_version_id,
                    source_hash=digest.hexdigest(),
                    permission_receipt=receipt,
                    state="succeeded",
                )
            )
        else:
            session.execute(
                course_knowledge_imports.insert().values(
                    id=uuid4().hex,
                    course_id=course_id,
                    school_id=actor.school_id,
                    space_id=space_id,
                    source_document_id=document_id,
                    source_version_id=version_id,
                    target_material_version_id=new_version_id,
                    source_hash=digest.hexdigest(),
                    permission_receipt=receipt,
                    state="succeeded",
                    created_at=now,
                )
            )
        session.execute(
            knowledge_audit_events.insert().values(
                id=uuid4().hex,
                school_id=actor.school_id,
                space_id=space_id,
                actor_id=user.id,
                action="document.import_to_course",
                resource_type="knowledge_document_version",
                resource_id=version_id,
                before_metadata={},
                after_metadata={
                    "course_id": course_id,
                    "material_id": material_id,
                    "course_material_version_id": new_version_id,
                },
                created_at=now,
            )
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise KnowledgeError(409, VERSION_CONFLICT, "导入记录冲突，请刷新后重试") from exc
    except Exception:
        session.rollback()
        raise

    return {
        "state": "succeeded",
        "idempotent": False,
        "appended_version": prior is not None,
        "course_id": course_id,
        "material_id": material_id,
        "material_version_id": new_version_id,
        "source_version_id": version_id,
        "logical_name": logical_name,
        "size_bytes": total,
        "sha256": digest.hexdigest(),
    }
