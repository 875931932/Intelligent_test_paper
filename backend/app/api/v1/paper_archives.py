"""资料库「试卷」文件夹端点：存卷归档 / 列表 / 详情 / 删除（课程作用域）。

归档与 paper_versions 无外键牵连，因此不受「每个项目只留最近 3 份」的保留
策略影响——存进文件夹的卷是教师自己留的，不会被生成新卷的自动清理删掉。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.db.schema import User
from app.db.session import get_session
from app.services.paper_archive_service import (
    PaperArchiveError,
    archive_paper_version,
    delete_paper_archive,
    get_paper_archive,
    list_paper_archives,
)

router = APIRouter(
    prefix="/api/v1/courses/{course_id}",
    tags=["paper-archives"],
    # 与试卷链路同口径：router 级鉴权一次覆盖全部端点
    dependencies=[Depends(get_current_user)],
)


class ArchivePaperRequest(BaseModel):
    """存卷入文件夹；不给 source 就存项目当前卷，不给 name 用「项目名 v几」。"""

    source_paper_version_id: str | None = None
    name: str | None = Field(default=None, max_length=255)


def _error(exc: PaperArchiveError) -> HTTPException:
    msg = str(exc)
    if "不存在" in msg or "not found" in msg or "no paper version" in msg:
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=422, detail=msg)


@router.post(
    "/exam-projects/{project_id}/paper-archives",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
def archive_project_paper(
    course_id: str,
    project_id: str,
    payload: ArchivePaperRequest | None = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """把一份试卷存进资料库「试卷」文件夹（存的是快照副本）。"""
    body = payload or ArchivePaperRequest()
    try:
        return archive_paper_version(
            session,
            course_id=course_id,
            project_id=project_id,
            source_paper_version_id=body.source_paper_version_id,
            name=body.name,
            created_by=current_user.id,
        )
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.get("/paper-archives", response_model=list[dict])
def list_course_paper_archives(
    course_id: str,
    session: Session = Depends(get_session),
) -> list[dict]:
    """课程下全部归档（新 → 旧），供资料库「试卷」文件夹列表。"""
    try:
        return list_paper_archives(session, course_id=course_id)
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.get("/paper-archives/{archive_id}", response_model=dict)
def get_course_paper_archive(
    course_id: str,
    archive_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """归档详情：档案字段 + 快照内全部题目。"""
    try:
        return get_paper_archive(session, course_id=course_id, archive_id=archive_id)
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.delete(
    "/paper-archives/{archive_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_course_paper_archive(
    course_id: str,
    archive_id: str,
    session: Session = Depends(get_session),
) -> None:
    """真正从库里删除归档（不做软删）。"""
    try:
        delete_paper_archive(session, course_id=course_id, archive_id=archive_id)
    except PaperArchiveError as exc:
        raise _error(exc) from exc
