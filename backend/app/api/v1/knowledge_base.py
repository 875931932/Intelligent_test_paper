"""校级教师共享知识库路由：空间、成员、共享策略与只读资料列表。

与命题“知识目录”（``/courses/{id}/knowledge``）是两个概念：本文件是新增的
教师资料共享域，路由前缀 ``/api/v1/knowledge``，不挂课程作用域。

授权以后端为准：全部端点要求登录，空间级权限由 service + 纯函数判定；
无权与资源不存在统一 404。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import StoragePort
from app.api.v1.auth import get_current_user
from app.api.v1.materials import get_storage
from app.db.schema import User, knowledge_jobs
from app.db.session import get_session
from app.infrastructure.tasks import knowledge_tasks
from app.services import (
    knowledge_answer_service,
    knowledge_document_service,
    knowledge_governance_service,
    knowledge_import_service,
    knowledge_search_service,
    knowledge_space_service,
)
from app.services.knowledge_space_service import KnowledgeError

router = APIRouter(
    prefix="/api/v1/knowledge",
    tags=["knowledge-base"],
    dependencies=[Depends(get_current_user)],
)


class SpaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    org_id: str | None = None
    read_scope: str = "members"
    subject_tags: list[str] = Field(default_factory=list)
    allow_course_import: bool = False
    # 仅管理员可指派；普通教师忽略（服务层校验）。
    owner_id: str | None = None


class SpaceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_policy_version: int
    name: str | None = None
    read_scope: str | None = None
    allow_course_import: bool | None = None
    subject_tags: list[str] | None = None
    confirm_scope_expansion: bool = False


class PolicyPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_read_scope: str


class MemberUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class TransferOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_user_id: str
    expected_policy_version: int


class UploadSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(gt=0)
    mime_type: str
    sha256: str


class UploadCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=255)
    classification: str = "shareable_teaching"
    # 追加新版本到已有文档；为空则创建新文档。
    document_id: str | None = None


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_access_version: int | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    space_ids: list[str] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=8, ge=1, le=20)


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_space_ids: list[str] = Field(min_length=1, max_length=5)
    title: str | None = Field(default=None, max_length=120)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: str | None = None
    comment: str | None = Field(default=None, max_length=500)
    reason: str | None = None


class ImportToCourseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str
    material_type: str = "teaching_material"
    title: str | None = Field(default=None, max_length=200)


def _http(exc: KnowledgeError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _raise(exc: KnowledgeError) -> None:
    raise _http(exc)


@router.get("/admin/summary")
def admin_summary(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """脱敏治理汇总；仅 admin/org_admin，且不返回资料正文或他人会话内容。"""

    try:
        return knowledge_governance_service.governance_summary(session, current_user)
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/meta")
def knowledge_meta() -> dict:
    """前端选项：共享范围与资料分类说明（不涉及任何租户数据）。"""

    return {
        "read_scopes": [
            {"value": "members", "label": "仅指定教师", "description": "默认；只有成员可读。"},
            {"value": "organization", "label": "单位内教师", "description": "责任单位内教师可读。"},
            {"value": "school", "label": "本校教师", "description": "本校有效教职工账号可读。"},
        ],
        "classifications": knowledge_space_service.classification_options(),
    }


@router.get("/spaces")
def list_spaces(
    subject_tag: str | None = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    return knowledge_space_service.list_spaces(session, current_user, subject_tag=subject_tag)


@router.post("/spaces", status_code=status.HTTP_201_CREATED)
def create_space(
    payload: SpaceCreateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.create_space(
            session,
            current_user,
            org_id=payload.org_id,
            name=payload.name,
            read_scope=payload.read_scope,
            subject_tags=payload.subject_tags,
            allow_course_import=payload.allow_course_import,
            owner_id=payload.owner_id,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/spaces/{space_id}")
def get_space(
    space_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.get_space(session, current_user, space_id)
    except KnowledgeError as exc:
        _raise(exc)


@router.patch("/spaces/{space_id}")
def update_space(
    space_id: str,
    payload: SpaceUpdateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.update_space(
            session,
            current_user,
            space_id,
            expected_policy_version=payload.expected_policy_version,
            name=payload.name,
            read_scope=payload.read_scope,
            allow_course_import=payload.allow_course_import,
            subject_tags=payload.subject_tags,
            confirm_scope_expansion=payload.confirm_scope_expansion,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/spaces/{space_id}/policy-preview")
def preview_policy(
    space_id: str,
    payload: PolicyPreviewRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.policy_preview(
            session, current_user, space_id, new_read_scope=payload.new_read_scope
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/spaces/{space_id}/archive")
def archive_space(
    space_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.archive_space(session, current_user, space_id)
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/spaces/{space_id}/documents")
def list_documents(
    space_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    try:
        return knowledge_space_service.list_documents(session, current_user, space_id)
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/spaces/{space_id}/members")
def list_members(
    space_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    try:
        return knowledge_space_service.list_members(session, current_user, space_id)
    except KnowledgeError as exc:
        _raise(exc)


@router.put("/spaces/{space_id}/members/{user_id}")
def upsert_member(
    space_id: str,
    user_id: str,
    payload: MemberUpsertRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.upsert_member(
            session, current_user, space_id, target_user_id=user_id, role=payload.role
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.delete("/spaces/{space_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    space_id: str,
    user_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Response:
    try:
        knowledge_space_service.remove_member(session, current_user, space_id, user_id)
    except KnowledgeError as exc:
        _raise(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/spaces/{space_id}/transfer-owner")
def transfer_owner(
    space_id: str,
    payload: TransferOwnerRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_space_service.transfer_owner(
            session,
            current_user,
            space_id,
            target_user_id=payload.target_user_id,
            expected_policy_version=payload.expected_policy_version,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/spaces/{space_id}/documents/{document_id}")
def get_document(
    space_id: str,
    document_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_document_service.get_document(
            session, current_user, space_id, document_id
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/spaces/{space_id}/documents/{document_id}/versions/{version_id}/content")
def get_version_content(
    space_id: str,
    document_id: str,
    version_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """经授权的正文读取：不向客户端发长期私有对象 URL。"""

    try:
        return knowledge_document_service.get_version_content(
            session, current_user, space_id, document_id, version_id
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post(
    "/spaces/{space_id}/upload-sessions",
    status_code=status.HTTP_201_CREATED,
)
def create_upload_session(
    space_id: str,
    payload: UploadSessionCreateRequest,
    session: Session = Depends(get_session),
    storage: StoragePort = Depends(get_storage),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_document_service.create_upload_session(
            session,
            storage,
            current_user,
            space_id,
            filename=payload.filename,
            size_bytes=payload.size_bytes,
            mime_type=payload.mime_type,
            sha256=payload.sha256,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/spaces/{space_id}/upload-sessions/{session_id}/complete")
def complete_upload_session(
    space_id: str,
    session_id: str,
    payload: UploadCompleteRequest,
    session: Session = Depends(get_session),
    storage: StoragePort = Depends(get_storage),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        result = knowledge_document_service.complete_upload_session(
            session,
            storage,
            current_user,
            space_id,
            session_id,
            title=payload.title,
            classification=payload.classification,
            document_id=payload.document_id,
        )
    except KnowledgeError as exc:
        _raise(exc)

    # 先提交业务数据，再尽力派发；broker 不可用时事件保持 pending 可重试，
    # 不把上传失败误报给用户。页面通过任务状态端点轮询结果。
    try:
        knowledge_tasks.publish_job_event(session, result["job_id"])
    except Exception:
        session.rollback()
    return result


@router.get("/spaces/{space_id}/jobs/{job_id}")
def get_job(
    space_id: str,
    job_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """读取任务进度；仅同校且有该空间读权限的账号可见。"""

    actor = knowledge_space_service.load_actor(session, current_user)
    row = session.execute(
        select(knowledge_jobs).where(knowledge_jobs.c.id == job_id)
    ).mappings().one_or_none()
    if row is None or row.space_id != space_id or row.school_id != actor.school_id:
        raise HTTPException(
            status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "resource not found"}
        )
    try:
        knowledge_space_service.require_space_access(
            session, actor, space_id=space_id, action="read"
        )
    except KnowledgeError as exc:
        _raise(exc)
    view = knowledge_tasks.job_view(session, job_id) or {}
    if view.get("state") == "queued":
        # 前端轮询同时承担 outbox 重试：broker 恢复后排队任务可再次派发。
        try:
            knowledge_tasks.publish_job_event(session, job_id)
        except Exception:
            session.rollback()
    return view


@router.post("/spaces/{space_id}/jobs/{job_id}/run")
def run_job_inline(
    space_id: str,
    job_id: str,
    session: Session = Depends(get_session),
    storage: StoragePort = Depends(get_storage),
    current_user: User = Depends(get_current_user),
) -> dict:
    """进程内执行一次任务（开发/演示与测试）。

    正式部署由 ``knowledge`` 队列的 Celery worker 执行；该端点仍需通过
    空间编辑权限校验，且只能执行属于该空间的任务。
    """

    actor = knowledge_space_service.load_actor(session, current_user)
    row = session.execute(
        select(knowledge_jobs).where(knowledge_jobs.c.id == job_id)
    ).mappings().one_or_none()
    if row is None or row.space_id != space_id or row.school_id != actor.school_id:
        raise HTTPException(
            status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "resource not found"}
        )
    try:
        knowledge_space_service.require_space_access(
            session, actor, space_id=space_id, action="edit"
        )
    except KnowledgeError as exc:
        _raise(exc)

    knowledge_tasks.execute_knowledge_job(
        job_id,
        worker_id=f"inline-api:{current_user.id}",
        storage=storage,
        session=session,
    )
    return knowledge_tasks.job_view(session, job_id) or {
        "id": job_id,
        "state": "unknown",
    }


@router.post("/spaces/{space_id}/documents/{document_id}/publish")
def publish_document(
    space_id: str,
    document_id: str,
    payload: PublishRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_document_service.publish_document(
            session,
            current_user,
            space_id,
            document_id,
            expected_access_version=payload.expected_access_version,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/spaces/{space_id}/documents/{document_id}/withdraw")
def withdraw_document(
    space_id: str,
    document_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_document_service.withdraw_document(
            session, current_user, space_id, document_id
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/search")
def search(
    payload: SearchRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """授权检索：只返回当前账号可读空间的当前有效发布版本片段。"""

    try:
        return knowledge_search_service.search(
            session,
            current_user,
            query=payload.query,
            space_ids=payload.space_ids,
            limit=payload.limit,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/sessions")
def list_sessions(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    return knowledge_answer_service.list_sessions(session, current_user)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_answer_service.create_session(
            session,
            current_user,
            selected_space_ids=payload.selected_space_ids,
            title=payload.title,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.get("/sessions/{session_id}")
def get_knowledge_session(
    session_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_answer_service.get_session(session, current_user, session_id)
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/sessions/{session_id}/ask")
def ask(
    session_id: str,
    payload: AskRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_answer_service.ask(
            session, current_user, session_id, question=payload.question
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post("/sessions/{session_id}/messages/{message_id}/feedback")
def feedback(
    session_id: str,
    message_id: str,
    payload: FeedbackRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    try:
        return knowledge_answer_service.submit_feedback(
            session,
            current_user,
            session_id,
            message_id,
            rating=payload.rating,
            comment=payload.comment,
            reason=payload.reason,
        )
    except KnowledgeError as exc:
        _raise(exc)


@router.post(
    "/spaces/{space_id}/documents/{document_id}/versions/{version_id}/import-to-course"
)
def import_to_course(
    space_id: str,
    document_id: str,
    version_id: str,
    payload: ImportToCourseRequest,
    session: Session = Depends(get_session),
    storage: StoragePort = Depends(get_storage),
    current_user: User = Depends(get_current_user),
) -> dict:
    """把固定发布版本复制到自己的课程；源撤回/删除不影响已导入副本。"""

    try:
        return knowledge_import_service.import_fixed_version(
            session,
            storage,
            current_user,
            space_id=space_id,
            document_id=document_id,
            version_id=version_id,
            course_id=payload.course_id,
            material_type=payload.material_type,
            title=payload.title,
        )
    except KnowledgeError as exc:
        _raise(exc)
