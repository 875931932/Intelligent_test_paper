"""知识域文档生命周期：上传会话、哈希校验、解析产物、发布与撤回。

与课程资料的区别：本文件所有查询都带 ``school_id + space_id``，不虚构课程；
授权判定统一走 ``domain.knowledge_base.policies``（先读当前事实再判定，不做
正向授权缓存）。普通读者只能接触当前有效发布版本，草稿与解析中间态只对
可编辑成员开放。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Callable
from uuid import uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.document.local_text_parser import parse_local_text
from app.adapters.storage.minio_storage import ObjectInfo, StoragePort, StorageUnavailableError
from app.config import settings
from app.db.schema import (
    Organization,
    User,
    knowledge_audit_events,
    knowledge_blocks,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_jobs,
    knowledge_parse_runs,
    knowledge_spaces,
    knowledge_upload_sessions,
)
from app.domain.knowledge_base.models import (
    ACTION_FORBIDDEN,
    FILE_VALIDATION_FAILED,
    INDEX_NOT_READY,
    RESOURCE_NOT_FOUND,
    VERSION_CONFLICT,
    Actor,
)
from app.domain.knowledge_base.policies import validate_classification
from app.services.knowledge_space_service import (
    KnowledgeError,
    _forbidden,
    _not_found,
    load_actor,
    require_space_access,
)
from app.services.knowledge_space_service import _aware, _iso, _servable

# 允许入库的教学资料类型。考试保密材料不在白名单内，由发布者分类与人工确认
# 共同把关，不能靠扩展 MIME 绕过。
ALLOWED_MIME_TYPES = {
    "text/markdown",
    "text/x-markdown",
    "text/plain",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_LOCAL_TEXT_MIMES = {"text/markdown", "text/x-markdown", "text/plain"}
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

# D1 限额：防止单人/单库无限堆积上传会话与资料。后续可迁到学校级配置。
MAX_ACTIVE_UPLOADS_PER_USER = 5
MAX_UPLOADS_PER_HOUR = 30
MAX_DOCUMENTS_PER_SPACE = 500


def _too_many(message: str) -> KnowledgeError:
    return KnowledgeError(429, "RATE_LIMITED", message)


class KnowledgeValidationError(KnowledgeError):
    """上传/解析阶段的确定性校验失败（可安全返回给调用方）。"""

    def __init__(self, message: str, *, status_code: int = 422, code: str = FILE_VALIDATION_FAILED) -> None:
        super().__init__(status_code, code, message)


def _safe_filename(value: str) -> str:
    normalized = (value or "").replace("\\", "/")
    name = PurePosixPath(normalized).name.strip()
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise KnowledgeValidationError("文件名不合法")
    if len(name) > 200:
        raise KnowledgeValidationError("文件名过长")
    return name


def _digest_stream(storage: StoragePort, object_key: str) -> str:
    digest = hashlib.sha256()
    for chunk in storage.stream_object(object_key):
        digest.update(chunk)
    return digest.hexdigest()


def _info_value(info: ObjectInfo | dict, name: str):
    return info[name] if isinstance(info, dict) else getattr(info, name)


def _space_row(session: Session, *, school_id: str, space_id: str):
    return session.execute(
        select(knowledge_spaces).where(
            knowledge_spaces.c.id == space_id,
            knowledge_spaces.c.school_id == school_id,
        )
    ).mappings().one_or_none()


def _document_row(session: Session, *, school_id: str, space_id: str, document_id: str):
    return session.execute(
        select(knowledge_documents).where(
            knowledge_documents.c.id == document_id,
            knowledge_documents.c.school_id == school_id,
            knowledge_documents.c.space_id == space_id,
        )
    ).mappings().one_or_none()


def _version_row(session: Session, *, school_id: str, space_id: str, version_id: str):
    return session.execute(
        select(knowledge_document_versions).where(
            knowledge_document_versions.c.id == version_id,
            knowledge_document_versions.c.school_id == school_id,
            knowledge_document_versions.c.space_id == space_id,
        )
    ).mappings().one_or_none()


def _audit(
    session: Session,
    *,
    actor: Actor,
    space_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    after: dict | None = None,
) -> None:
    """只记元数据，禁止把资料正文写进审计表。"""

    session.execute(
        knowledge_audit_events.insert().values(
            id=uuid4().hex,
            school_id=actor.school_id,
            space_id=space_id,
            actor_id=actor.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before_metadata={},
            after_metadata=dict(after or {}),
            created_at=datetime.now(timezone.utc),
        )
    )


# ── 上传会话 ────────────────────────────────────────────────────────────────


def create_upload_session(
    session: Session,
    storage: StoragePort,
    user: User,
    space_id: str,
    *,
    filename: str,
    size_bytes: int,
    mime_type: str,
    sha256: str,
    expires_in: int = 900,
) -> dict:
    """创建浏览器直传会话；只允许可编辑成员，校验失败不产生任何行。"""

    actor = load_actor(session, user)
    space, _role = require_space_access(session, actor, space_id=space_id, action="edit")
    if space.status != "active":
        raise KnowledgeValidationError("知识库已归档，不能上传资料", status_code=409)

    name = _safe_filename(filename)
    digest = (sha256 or "").strip().lower()
    if not _HASH_RE.match(digest):
        raise KnowledgeValidationError("SHA-256 必须是 64 位十六进制")
    if size_bytes <= 0:
        raise KnowledgeValidationError("文件大小必须大于 0")
    if size_bytes > settings.upload_max_bytes:
        raise KnowledgeValidationError("文件超过上传大小限制", status_code=413)
    if mime_type not in ALLOWED_MIME_TYPES:
        raise KnowledgeValidationError("不支持的文件类型")

    now = datetime.now(timezone.utc)
    # 串行化同一账号的限额计数，避免并发请求同时通过检查。
    session.execute(select(User.id).where(User.id == actor.user_id).with_for_update())
    active_uploads = session.scalar(
        select(func.count()).where(
            knowledge_upload_sessions.c.uploader_id == actor.user_id,
            knowledge_upload_sessions.c.school_id == actor.school_id,
            knowledge_upload_sessions.c.status.in_(["created", "verified"]),
            knowledge_upload_sessions.c.expires_at > now,
        )
    )
    if int(active_uploads or 0) >= MAX_ACTIVE_UPLOADS_PER_USER:
        raise _too_many("待完成的上传过多，请先处理已有上传")
    recent_uploads = session.scalar(
        select(func.count()).where(
            knowledge_upload_sessions.c.uploader_id == actor.user_id,
            knowledge_upload_sessions.c.school_id == actor.school_id,
            knowledge_upload_sessions.c.created_at >= now - timedelta(hours=1),
        )
    )
    if int(recent_uploads or 0) >= MAX_UPLOADS_PER_HOUR:
        raise _too_many("一小时内上传次数过多，请稍后再试")
    # 资料数量上限统一在完成上传的新建事务中检查；这里不拦截，否则库满后
    # 连“给已有文档上传新版”的会话都无法申请。

    session_id = uuid4().hex
    object_key = f"knowledge/{actor.school_id}/spaces/{space_id}/uploads/{session_id}/{name}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    try:
        upload_url = storage.presign_put(
            object_key=object_key,
            content_type=mime_type,
            sha256=digest,
            expires_in=expires_in,
        )
    except Exception as exc:  # 存储不可用时不落半截会话
        raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "对象存储不可用") from exc

    session.execute(
        knowledge_upload_sessions.insert().values(
            id=session_id,
            school_id=actor.school_id,
            space_id=space_id,
            uploader_id=actor.user_id,
            object_key=object_key,
            expected_hash=digest,
            size_bytes=int(size_bytes),
            mime_type=mime_type,
            expires_at=expires_at,
            status="created",
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    return {
        "session_id": session_id,
        "object_key": object_key,
        "upload_url": upload_url,
        "expires_at": _iso(expires_at),
        "headers": {"Content-Type": mime_type, "x-amz-meta-sha256": digest},
    }


def complete_upload_session(
    session: Session,
    storage: StoragePort,
    user: User,
    space_id: str,
    session_id: str,
    *,
    title: str | None = None,
    classification: str = "shareable_teaching",
    document_id: str | None = None,
) -> dict:
    """校验对象后创建文档版本与解析任务（任务与 outbox 同事务）。"""

    actor = load_actor(session, user)
    space, _role = require_space_access(session, actor, space_id=space_id, action="edit")
    row = session.execute(
        select(knowledge_upload_sessions).where(
            knowledge_upload_sessions.c.id == session_id,
            knowledge_upload_sessions.c.school_id == actor.school_id,
            knowledge_upload_sessions.c.space_id == space_id,
        )
    ).mappings().one_or_none()
    if row is None:
        raise _not_found()
    if row.status == "completed":
        raise KnowledgeError(409, VERSION_CONFLICT, "该上传会话已完成")
    if row.status in {"expired", "failed"}:
        raise KnowledgeError(409, VERSION_CONFLICT, "该上传会话已失效")
    if int(row.uploader_id != actor.user_id):
        # 只有上传者本人能完成自己的会话；同级管理员也不能替他人接管。
        raise _forbidden("只能完成自己创建的上传会话")

    expires_at = _aware(row.expires_at)
    now = datetime.now(timezone.utc)
    if expires_at is not None and expires_at <= now:
        session.execute(
            update(knowledge_upload_sessions)
            .where(knowledge_upload_sessions.c.id == session_id)
            .values(status="expired")
        )
        session.commit()
        raise KnowledgeError(410, FILE_VALIDATION_FAILED, "上传会话已过期")

    target_document_id = document_id
    if target_document_id is not None:
        target = _document_row(
            session, school_id=actor.school_id, space_id=space_id, document_id=target_document_id
        )
        if target is None:
            raise _not_found()
        classification = target.classification

    error = validate_classification(classification, space.read_scope)
    if error is not None:
        raise KnowledgeValidationError(
            "限指定教师资料只能放在仅成员可读的知识库", code=error
        )

    # 哈希/大小/MIME 以对象存储当前内容为准，不能信任前端声明。
    try:
        info = storage.head_object(row.object_key)
        if info is None:
            raise KnowledgeValidationError("上传对象不存在", status_code=409)
        if int(_info_value(info, "size")) != int(row.size_bytes):
            raise KnowledgeValidationError("上传对象大小与声明不一致", status_code=409)
        content_type = _info_value(info, "content_type")
        if content_type and content_type != row.mime_type:
            raise KnowledgeValidationError("上传对象类型与声明不一致", status_code=409)
        upload_etag = _info_value(info, "etag")
        if not upload_etag:
            raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "对象存储未返回校验标识")
        digest = _digest_stream(storage, row.object_key)
        if digest != row.expected_hash:
            raise KnowledgeValidationError("文件哈希与声明不一致", status_code=409)
    except (KnowledgeError, KnowledgeValidationError):
        raise
    except StorageUnavailableError as exc:
        raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "对象存储不可用") from exc
    except Exception as exc:
        raise KnowledgeError(503, "STORAGE_UNAVAILABLE", "对象存储不可用") from exc

    claimed = session.execute(
        update(knowledge_upload_sessions)
        .where(
            knowledge_upload_sessions.c.id == session_id,
            knowledge_upload_sessions.c.school_id == actor.school_id,
            knowledge_upload_sessions.c.space_id == space_id,
            knowledge_upload_sessions.c.status == "created",
        )
        .values(status="verified")
    ).rowcount
    if claimed != 1:
        session.rollback()
        raise KnowledgeError(409, VERSION_CONFLICT, "上传会话已被其他请求处理")

    is_new_document = target_document_id is None
    if is_new_document:
        # 完成上传时重新核对额度（创建会话后可能已有其他资料入库），并锁定空间串行化。
        session.execute(
            select(knowledge_spaces.c.id)
            .where(
                knowledge_spaces.c.id == space_id,
                knowledge_spaces.c.school_id == actor.school_id,
            )
            .with_for_update()
        )
        document_count = session.scalar(
            select(func.count()).where(
                knowledge_documents.c.space_id == space_id,
                knowledge_documents.c.school_id == actor.school_id,
            )
        )
        if int(document_count or 0) >= MAX_DOCUMENTS_PER_SPACE:
            raise _too_many("该知识库资料数量已达上限")
        target_document_id = uuid4().hex
        doc_title = (title or "").strip() or PurePosixPath(_safe_filename(PurePosixPath(row.object_key).name)).stem
        doc_title = doc_title[:255] or "未命名资料"
        session.execute(
            knowledge_documents.insert().values(
                id=target_document_id,
                school_id=actor.school_id,
                space_id=space_id,
                title=doc_title,
                classification=classification,
                status="processing",
                published_version_id=None,
                access_version=1,
                created_by=actor.user_id,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        # 上传新版不能中断旧版服务：已有发布版本时保持 published，只更新草稿版本。
        session.execute(
            update(knowledge_documents)
            .where(
                knowledge_documents.c.id == target_document_id,
                knowledge_documents.c.school_id == actor.school_id,
                knowledge_documents.c.space_id == space_id,
            )
            .values(
                status=case(
                    (knowledge_documents.c.published_version_id.is_(None), "processing"),
                    else_=knowledge_documents.c.status,
                ),
                updated_at=now,
            )
        )

    version_no = session.scalar(
        select(func.coalesce(func.max(knowledge_document_versions.c.version_no), 0) + 1).where(
            knowledge_document_versions.c.document_id == target_document_id,
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id == space_id,
        )
    )
    version_id = uuid4().hex
    final_key = (
        f"knowledge/{actor.school_id}/spaces/{space_id}/documents/"
        f"{target_document_id}/versions/{version_id}/{PurePosixPath(row.object_key).name}"
    )
    try:
        storage.finalize_object(row.object_key, final_key, upload_etag)
    except Exception as exc:
        session.rollback()
        raise KnowledgeError(409, VERSION_CONFLICT, "上传对象在完成前发生变化，请重新上传") from exc

    session.execute(
        knowledge_document_versions.insert().values(
            id=version_id,
            school_id=actor.school_id,
            space_id=space_id,
            document_id=target_document_id,
            version_no=int(version_no),
            file_hash=digest,
            object_key=final_key,
            size_bytes=int(row.size_bytes),
            mime_type=row.mime_type,
            state="processing",
            publication_policy_snapshot={},
            created_by=actor.user_id,
            created_at=now,
        )
    )
    parse_run_id = _create_parse_run(
        session,
        school_id=actor.school_id,
        space_id=space_id,
        version_id=version_id,
        mime_type=row.mime_type,
    )
    from app.infrastructure.tasks.knowledge_tasks import enqueue_parse_job

    job_id = enqueue_parse_job(
        session,
        school_id=actor.school_id,
        space_id=space_id,
        actor_id=actor.user_id,
        version_id=version_id,
        parse_run_id=parse_run_id,
    )
    session.execute(
        update(knowledge_upload_sessions)
        .where(knowledge_upload_sessions.c.id == session_id)
        .values(status="completed")
    )
    _audit(
        session,
        actor=actor,
        space_id=space_id,
        action="document.version.create",
        resource_type="knowledge_document_version",
        resource_id=version_id,
        after={
            "document_id": target_document_id,
            "version_no": int(version_no),
            "classification": classification,
            "mime_type": row.mime_type,
            "size_bytes": int(row.size_bytes),
        },
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise KnowledgeError(409, VERSION_CONFLICT, "文档版本与已有记录冲突") from exc

    return {
        "document_id": target_document_id,
        "version_id": version_id,
        "version_no": int(version_no),
        "parse_run_id": parse_run_id,
        "job_id": job_id,
        "state": "processing",
    }


# ── 解析 ────────────────────────────────────────────────────────────────────


def _parser_profile_hash(mime_type: str) -> str:
    raw = (
        f"knowledge-local-text:v1:{mime_type}"
        if mime_type in _LOCAL_TEXT_MIMES
        else f"knowledge-mineru:{settings.mineru_model_version}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_parse_run(
    session: Session, *, school_id: str, space_id: str, version_id: str, mime_type: str
) -> str:
    run_id = uuid4().hex
    now = datetime.now(timezone.utc)
    session.execute(
        knowledge_parse_runs.insert().values(
            id=run_id,
            school_id=school_id,
            space_id=space_id,
            version_id=version_id,
            parser_profile_hash=_parser_profile_hash(mime_type),
            state="queued",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )
    return run_id


def _assert_job_fence(
    session: Session,
    *,
    job_id: str | None,
    worker_id: str | None,
    fencing_token: int | None,
) -> None:
    """锁定任务行并校验租约；令牌不匹配说明已被接管，禁止再写派生结果。

    ``SELECT ... FOR UPDATE`` 与接管方的 ``UPDATE`` 串行化：若接管先提交，
    旧 worker 读到新令牌后立即放弃；若旧 worker 先持有行锁，接管会等待。
    """

    if not job_id:
        return
    row = session.execute(
        select(knowledge_jobs).where(knowledge_jobs.c.id == job_id).with_for_update()
    ).mappings().one_or_none()
    if (
        row is None
        or row.state != "running"
        or row.lease_owner != worker_id
        or int(row.fencing_token) != int(fencing_token or 0)
    ):
        raise KnowledgeError(409, "LEASE_LOST", "任务租约已被接管，丢弃本次解析结果")


def run_parse(
    session: Session,
    storage: StoragePort,
    *,
    school_id: str,
    space_id: str,
    version_id: str,
    parse_run_id: str,
    job_id: str | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    authorize: Callable[[], bool] | None = None,
) -> str:
    """执行一次解析并写入 blocks；失败落 failed 并可重试。

    传入 ``job_id/worker_id/fencing_token`` 时，所有正文与状态提交都在持有
    租约的前提下完成；被接管后旧 worker 不得再写入。``authorize`` 会在提交
    正文与版本状态前、持有任务行锁时再次读取当前授权，撤权则整笔回滚。
    """

    version = _version_row(session, school_id=school_id, space_id=space_id, version_id=version_id)
    if version is None:
        raise KnowledgeValidationError("文档版本不存在", status_code=404)
    run = session.execute(
        select(knowledge_parse_runs).where(
            knowledge_parse_runs.c.id == parse_run_id,
            knowledge_parse_runs.c.school_id == school_id,
            knowledge_parse_runs.c.space_id == space_id,
        )
    ).mappings().one_or_none()
    if run is None:
        raise KnowledgeValidationError("解析任务不存在", status_code=404)

    now = datetime.now(timezone.utc)
    _assert_job_fence(
        session, job_id=job_id, worker_id=worker_id, fencing_token=fencing_token
    )
    session.execute(
        update(knowledge_parse_runs)
        .where(knowledge_parse_runs.c.id == parse_run_id)
        .values(state="running", attempts=knowledge_parse_runs.c.attempts + 1, updated_at=now)
    )
    session.execute(
        update(knowledge_document_versions)
        .where(knowledge_document_versions.c.id == version_id)
        .values(state="processing")
    )
    session.execute(
        update(knowledge_documents)
        .where(
            knowledge_documents.c.id == version.document_id,
            knowledge_documents.c.school_id == school_id,
            knowledge_documents.c.space_id == space_id,
        )
        .values(
            status=case(
                (knowledge_documents.c.published_version_id.is_(None), "processing"),
                else_=knowledge_documents.c.status,
            ),
            updated_at=now,
        )
    )
    session.commit()

    try:
        blocks = _extract_blocks(storage, version)
    except Exception as exc:
        message = str(exc)[:500] or exc.__class__.__name__
        failed_at = datetime.now(timezone.utc)
        session.rollback()
        _assert_job_fence(
            session, job_id=job_id, worker_id=worker_id, fencing_token=fencing_token
        )
        session.execute(
            update(knowledge_parse_runs)
            .where(knowledge_parse_runs.c.id == parse_run_id)
            .values(state="failed", error_code="parse_failed", updated_at=failed_at)
        )
        session.execute(
            update(knowledge_document_versions)
            .where(knowledge_document_versions.c.id == version_id)
            .values(state="failed")
        )
        session.execute(
            update(knowledge_documents)
            .where(
                knowledge_documents.c.id == version.document_id,
                knowledge_documents.c.school_id == school_id,
                knowledge_documents.c.space_id == space_id,
            )
            .values(
                status=case(
                    (knowledge_documents.c.published_version_id.is_(None), "draft"),
                    else_=knowledge_documents.c.status,
                ),
                updated_at=failed_at,
            )
        )
        session.execute(
            update(knowledge_parse_runs)
            .where(knowledge_parse_runs.c.id == parse_run_id)
            .values(error_code=("parse_failed:" + message)[:120])
        )
        session.execute(
            knowledge_audit_events.insert().values(
                id=uuid4().hex,
                school_id=school_id,
                space_id=space_id,
                actor_id=None,
                action="document.parse.failed",
                resource_type="knowledge_document_version",
                resource_id=version_id,
                before_metadata={},
                after_metadata={"error": message},
                created_at=failed_at,
            )
        )
        session.commit()
        raise KnowledgeValidationError(
            f"解析失败：{message}", status_code=422, code="PARSE_FAILED"
        ) from exc

    ready_at = datetime.now(timezone.utc)
    _assert_job_fence(
        session, job_id=job_id, worker_id=worker_id, fencing_token=fencing_token
    )
    if authorize is not None and not authorize():
        session.rollback()
        raise KnowledgeError(403, "access_revoked", "解析期间权限已撤销，丢弃派生结果")
    rows = []
    for block in blocks:
        rows.append(
            {
                "id": uuid4().hex,
                "school_id": school_id,
                "space_id": space_id,
                "parse_run_id": parse_run_id,
                "block_index": block.block_index,
                "block_type": block.block_type,
                "text": block.text or "",
                "markdown": block.markdown,
                "latex": block.latex,
                "page_index": block.page_index,
                "bbox": block.bbox,
                "heading_path": list(block.heading_path or []),
                "created_at": ready_at,
            }
        )
    if rows:
        session.execute(knowledge_blocks.insert(), rows)
    session.execute(
        update(knowledge_parse_runs)
        .where(knowledge_parse_runs.c.id == parse_run_id)
        .values(state="ready", error_code=None, updated_at=ready_at)
    )
    session.execute(
        update(knowledge_document_versions)
        .where(knowledge_document_versions.c.id == version_id)
        .values(state="ready")
    )
    session.execute(
        update(knowledge_documents)
        .where(
            knowledge_documents.c.id == version.document_id,
            knowledge_documents.c.school_id == school_id,
            knowledge_documents.c.space_id == space_id,
        )
        .values(
            status=case(
                (knowledge_documents.c.published_version_id.is_(None), "ready"),
                else_=knowledge_documents.c.status,
            ),
            updated_at=ready_at,
        )
    )
    session.commit()
    return "ready"


def _extract_blocks(storage: StoragePort, version) -> list:
    mime_type = version.mime_type or ""
    if mime_type in _LOCAL_TEXT_MIMES:
        content = b"".join(storage.stream_object(version.object_key))
        return parse_local_text(content, markdown=mime_type != "text/plain")
    return _parse_with_mineru(storage, version)


def _parse_with_mineru(storage: StoragePort, version) -> list:
    """PDF/DOCX 走 MinerU；未配置或超时都落明确失败，不留下永远“解析中”。"""

    import asyncio

    from app.adapters.document.mineru_client import MineruClient
    from app.adapters.document.protocol import ParseRequest, ParseState
    from app.services.document_processing_service import read_mineru_zip

    if not settings.mineru_api_token.strip():
        raise KnowledgeValidationError("未配置 MinerU 解析服务", status_code=503)
    client = MineruClient(
        base_url=settings.mineru_base_url,
        token=settings.mineru_api_token,
        timeout_seconds=90,
    )

    async def _run():
        async def _stream():
            for chunk in storage.stream_object(version.object_key):
                yield chunk

        request = ParseRequest(
            material_version_id=version.id,
            filename=PurePosixPath(version.object_key).name,
            content_type=version.mime_type,
            content_factory=lambda: _stream(),
            model_version=settings.mineru_model_version,
        )
        submission = await client.submit(request)
        waited = 0
        while True:
            progress = await client.poll(submission.provider_batch_id)
            if progress.state == ParseState.FAILED:
                raise RuntimeError(progress.error_summary or "MinerU 解析失败")
            if progress.state == ParseState.DONE:
                break
            if waited >= settings.mineru_max_poll_seconds:
                raise RuntimeError("MinerU 解析超时")
            await asyncio.sleep(settings.mineru_poll_interval_seconds)
            waited += settings.mineru_poll_interval_seconds
        artifact = await client.fetch(submission.provider_batch_id)
        return read_mineru_zip(artifact.content).blocks

    try:
        return asyncio.run(_run())
    finally:
        try:
            asyncio.run(client.close())
        except Exception:
            pass


# ── 文档详情与发布 ──────────────────────────────────────────────────────────


def _parse_status(session: Session, *, school_id: str, space_id: str, version_id: str) -> dict | None:
    row = session.execute(
        select(knowledge_parse_runs)
        .where(
            knowledge_parse_runs.c.school_id == school_id,
            knowledge_parse_runs.c.space_id == space_id,
            knowledge_parse_runs.c.version_id == version_id,
        )
        .order_by(knowledge_parse_runs.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "state": row.state,
        "error_code": row.error_code,
        "attempts": row.attempts,
        "updated_at": _iso(row.updated_at),
    }


def get_document(session: Session, user: User, space_id: str, document_id: str) -> dict:
    """文档详情。读者只看到当前可服务发布版本；维护者可见全部版本与解析状态。"""

    actor = load_actor(session, user)
    space, role = require_space_access(session, actor, space_id=space_id)
    doc = _document_row(session, school_id=actor.school_id, space_id=space_id, document_id=document_id)
    if doc is None:
        raise _not_found()

    versions = session.execute(
        select(knowledge_document_versions)
        .where(
            knowledge_document_versions.c.document_id == document_id,
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id == space_id,
        )
        .order_by(knowledge_document_versions.c.version_no.desc())
    ).mappings().all()

    if role == "reader":
        current = next((v for v in versions if v.id == doc.published_version_id), None)
        if (
            doc.status != "published"
            or current is None
            or current.state != "published"
            or not _servable(current, datetime.now(timezone.utc))
        ):
            raise _not_found()
        versions = [current]

    return {
        "id": doc.id,
        "title": doc.title,
        "classification": doc.classification,
        "status": doc.status,
        "published_version_id": doc.published_version_id,
        "access_version": doc.access_version,
        "updated_at": _iso(doc.updated_at),
        "versions": [
            {
                "id": version.id,
                "version_no": version.version_no,
                "state": version.state,
                "mime_type": version.mime_type,
                "size_bytes": version.size_bytes,
                "file_hash": version.file_hash,
                "effective_from": _iso(version.effective_from),
                "effective_to": _iso(version.effective_to),
                "published_at": _iso(version.published_at),
                "parse": _parse_status(
                    session,
                    school_id=actor.school_id,
                    space_id=space_id,
                    version_id=version.id,
                ),
            }
            for version in versions
        ],
    }


def publish_document(
    session: Session,
    user: User,
    space_id: str,
    document_id: str,
    *,
    expected_access_version: int | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> dict:
    """原子发布：旧版本转 superseded，新版本与文档指针同事务切换。"""

    actor = load_actor(session, user)
    space, _role = require_space_access(session, actor, space_id=space_id, action="publish")
    doc = _document_row(session, school_id=actor.school_id, space_id=space_id, document_id=document_id)
    if doc is None:
        raise _not_found()
    if expected_access_version is not None and int(expected_access_version) != int(doc.access_version):
        raise KnowledgeError(409, VERSION_CONFLICT, "文档已被其他操作修改，请刷新后重试")
    error = validate_classification(doc.classification, space.read_scope)
    if error is not None:
        raise KnowledgeError(409, VERSION_CONFLICT, "资料分类与当前共享范围冲突，不能发布")

    ready_version = session.execute(
        select(knowledge_document_versions)
        .where(
            knowledge_document_versions.c.document_id == document_id,
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id == space_id,
            knowledge_document_versions.c.state == "ready",
        )
        .order_by(knowledge_document_versions.c.version_no.desc())
        .limit(1)
    ).mappings().one_or_none()
    if ready_version is None:
        raise KnowledgeError(409, INDEX_NOT_READY, "没有可发布的已解析版本")

    # 发布前完成索引构建：新 build 就绪后才切换有效版本；失败保留旧版服务。
    from app.services import knowledge_search_service

    build = knowledge_search_service.ensure_index_build(
        session,
        school_id=actor.school_id,
        space_id=space_id,
        version_id=ready_version.id,
    )

    now = datetime.now(timezone.utc)
    starts = _aware(effective_from)
    ends = _aware(effective_to)
    if starts is not None and ends is not None and ends <= starts:
        raise KnowledgeError(422, VERSION_CONFLICT, "有效期结束时间必须晚于开始时间")

    org = session.get(Organization, space.org_id)
    snapshot = {
        "classification": doc.classification,
        "read_scope": space.read_scope,
        "org_id": space.org_id,
        "org_kind": getattr(org, "kind", None),
        "policy_version": int(space.policy_version),
        "published_at": now.isoformat(),
    }

    try:
        if doc.published_version_id and doc.published_version_id != ready_version.id:
            session.execute(
                update(knowledge_document_versions)
                .where(
                    knowledge_document_versions.c.id == doc.published_version_id,
                    knowledge_document_versions.c.school_id == actor.school_id,
                    knowledge_document_versions.c.space_id == space_id,
                )
                .values(state="superseded")
            )
        session.execute(
            update(knowledge_document_versions)
            .where(
                knowledge_document_versions.c.id == ready_version.id,
                knowledge_document_versions.c.school_id == actor.school_id,
                knowledge_document_versions.c.space_id == space_id,
            )
            .values(
                state="published",
                approved_by=actor.user_id,
                published_at=now,
                effective_from=starts,
                effective_to=ends,
                publication_policy_snapshot=snapshot,
                serving_build_id=build["id"],
            )
        )
        session.execute(
            update(knowledge_documents)
            .where(
                knowledge_documents.c.id == document_id,
                knowledge_documents.c.school_id == actor.school_id,
                knowledge_documents.c.space_id == space_id,
            )
            .values(
                status="published",
                published_version_id=ready_version.id,
                access_version=knowledge_documents.c.access_version + 1,
                updated_at=now,
            )
        )
        _audit(
            session,
            actor=actor,
            space_id=space_id,
            action="document.publish",
            resource_type="knowledge_document",
            resource_id=document_id,
            after={
                "version_id": ready_version.id,
                "version_no": ready_version.version_no,
                "read_scope": space.read_scope,
                "classification": doc.classification,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return get_document(session, user, space_id, document_id)


def withdraw_document(session: Session, user: User, space_id: str, document_id: str) -> dict:
    """撤回：立即停止对普通读者服务；旧版本内容保留但不可再读。"""

    actor = load_actor(session, user)
    space, _role = require_space_access(session, actor, space_id=space_id, action="publish")
    doc = _document_row(session, school_id=actor.school_id, space_id=space_id, document_id=document_id)
    if doc is None:
        raise _not_found()

    now = datetime.now(timezone.utc)
    if doc.published_version_id:
        session.execute(
            update(knowledge_document_versions)
            .where(
                knowledge_document_versions.c.id == doc.published_version_id,
                knowledge_document_versions.c.school_id == actor.school_id,
                knowledge_document_versions.c.space_id == space_id,
            )
            .values(state="withdrawn")
        )
    session.execute(
        update(knowledge_documents)
        .where(
            knowledge_documents.c.id == document_id,
            knowledge_documents.c.school_id == actor.school_id,
            knowledge_documents.c.space_id == space_id,
        )
        .values(
            status="withdrawn",
            published_version_id=None,
            access_version=knowledge_documents.c.access_version + 1,
            updated_at=now,
        )
    )
    _audit(
        session,
        actor=actor,
        space_id=space_id,
        action="document.withdraw",
        resource_type="knowledge_document",
        resource_id=document_id,
        after={"previous_version_id": doc.published_version_id},
    )
    session.commit()
    return get_document(session, user, space_id, document_id)


def get_version_content(
    session: Session, user: User, space_id: str, document_id: str, version_id: str
) -> dict:
    """授权代理读取解析正文：读者只能读当前可服务版本，维护者可预览草稿。"""

    actor = load_actor(session, user)
    space, role = require_space_access(session, actor, space_id=space_id)
    doc = _document_row(session, school_id=actor.school_id, space_id=space_id, document_id=document_id)
    if doc is None:
        raise _not_found()
    version = _version_row(session, school_id=actor.school_id, space_id=space_id, version_id=version_id)
    if version is None or version.document_id != document_id:
        raise _not_found()
    if role == "reader":
        if (
            doc.published_version_id != version_id
            or version.state != "published"
            or not _servable(version, datetime.now(timezone.utc))
        ):
            raise _not_found()

    run = session.execute(
        select(knowledge_parse_runs)
        .where(
            knowledge_parse_runs.c.school_id == actor.school_id,
            knowledge_parse_runs.c.space_id == space_id,
            knowledge_parse_runs.c.version_id == version_id,
            knowledge_parse_runs.c.state == "ready",
        )
        .order_by(knowledge_parse_runs.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if run is None:
        raise KnowledgeError(409, INDEX_NOT_READY, "该版本尚未解析完成")

    blocks = session.execute(
        select(knowledge_blocks)
        .where(
            knowledge_blocks.c.school_id == actor.school_id,
            knowledge_blocks.c.space_id == space_id,
            knowledge_blocks.c.parse_run_id == run.id,
        )
        .order_by(knowledge_blocks.c.block_index)
    ).mappings().all()
    return {
        "document_id": document_id,
        "version_id": version_id,
        "version_no": version.version_no,
        "state": version.state,
        "blocks": [
            {
                "block_index": block.block_index,
                "block_type": block.block_type,
                "text": block.text,
                "page_index": block.page_index,
                "heading_path": list(block.heading_path or []),
            }
            for block in blocks
        ],
    }
