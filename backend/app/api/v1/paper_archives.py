"""资料库「试卷」文件夹端点：存卷归档 / 列表 / 详情 / 删除（课程作用域）。

归档与 paper_versions 无外键牵连，因此不受「每个项目只留最近 3 份」的保留
策略影响——存进文件夹的卷是教师自己留的，不会被生成新卷的自动清理删掉。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.db.schema import User
from app.db.session import get_session
from app.services.answer_card_docx import export_answer_card_docx
from app.services.exam_paper_docx import export_exam_paper_docx
from app.services.paper_archive_service import (
    PaperArchiveError,
    archive_paper_version,
    delete_archive_question,
    delete_paper_archive,
    get_paper_archive,
    list_paper_archives,
    load_archive_export_paper,
    rename_paper_archive,
    reorder_archive_questions,
    restore_archive_to_paper,
    update_archive_question,
)
from app.services.paper_export_bundle import bundle_paper_exports
from app.services.paper_version_service import (
    export_answer_card_html,
    export_answer_detail_json,
    export_answer_key_html,
    export_student_paper_html,
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


# ─── 快照编辑 ────────────────────────────────────────────────────────


class ArchiveRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ArchiveQuestionPatchRequest(BaseModel):
    """单题补丁：键域与试卷页 teacher_override 相同，浅合并进快照题。"""

    patch: dict[str, Any]


class ArchiveReorderRequest(BaseModel):
    ordered_indices: list[int]


@router.patch("/paper-archives/{archive_id}", response_model=dict)
def rename_course_paper_archive(
    course_id: str,
    archive_id: str,
    payload: ArchiveRenameRequest,
    session: Session = Depends(get_session),
) -> dict:
    """改归档名称（文件夹里的辨识名）。"""
    try:
        return rename_paper_archive(
            session, course_id=course_id, archive_id=archive_id, name=payload.name
        )
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.patch("/paper-archives/{archive_id}/questions/{item_index}", response_model=dict)
def patch_archive_question(
    course_id: str,
    archive_id: str,
    item_index: int,
    payload: ArchiveQuestionPatchRequest,
    session: Session = Depends(get_session),
) -> dict:
    """改归档里的一道题（题干/选项/答案/分值…），只动副本不动原试卷。"""
    try:
        return update_archive_question(
            session,
            course_id=course_id,
            archive_id=archive_id,
            item_index=item_index,
            patch=payload.patch,
        )
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.delete("/paper-archives/{archive_id}/questions/{item_index}", response_model=dict)
def remove_archive_question(
    course_id: str,
    archive_id: str,
    item_index: int,
    session: Session = Depends(get_session),
) -> dict:
    """删归档里的一道题（其后题号前移），只动副本。"""
    try:
        return delete_archive_question(
            session, course_id=course_id, archive_id=archive_id, item_index=item_index
        )
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.put("/paper-archives/{archive_id}/questions/reorder", response_model=dict)
def reorder_archive_questions_endpoint(
    course_id: str,
    archive_id: str,
    payload: ArchiveReorderRequest,
    session: Session = Depends(get_session),
) -> dict:
    """按给定题号顺序重排归档题目。"""
    try:
        return reorder_archive_questions(
            session,
            course_id=course_id,
            archive_id=archive_id,
            ordered_indices=payload.ordered_indices,
        )
    except PaperArchiveError as exc:
        raise _error(exc) from exc


# ─── 存回试卷区 ──────────────────────────────────────────────────────


@router.post("/paper-archives/{archive_id}/restore", response_model=dict)
def restore_course_paper_archive(
    course_id: str,
    archive_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """存回试卷区：按快照新建一版并设为项目当前卷（走「只留最近 3 份」策略）。"""
    try:
        pv = restore_archive_to_paper(
            session, course_id=course_id, archive_id=archive_id, created_by=current_user.id
        )
    except PaperArchiveError as exc:
        raise _error(exc) from exc
    return {
        "paper_version_id": pv.get("id"),
        "version_no": pv.get("version_no"),
        "exam_project_id": pv.get("exam_project_id"),
        "item_count": len(pv.get("questions") or []),
    }


# ─── 导出（与试卷页四件套同规则，数据源换成归档快照） ─────────────────


def _archive_export_paper(course_id: str, archive_id: str, session: Session) -> dict:
    try:
        return load_archive_export_paper(session, course_id=course_id, archive_id=archive_id)
    except PaperArchiveError as exc:
        raise _error(exc) from exc


@router.get("/paper-archives/{archive_id}/export/json")
def export_archive_json(
    course_id: str,
    archive_id: str,
    session: Session = Depends(get_session),
):
    """归档的答案细则 JSON 下载。"""
    pv = _archive_export_paper(course_id, archive_id, session)
    data = export_answer_detail_json(session, archive_id, course_id=course_id, pv=pv)
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": f'attachment; filename="answer_detail_v{data.get("version_no", 1)}.json"'
        },
    )


@router.get("/paper-archives/{archive_id}/export/student")
def export_archive_student(
    course_id: str,
    archive_id: str,
    export_format: str = Query("html", alias="format", pattern="^(html|docx)$"),
    session: Session = Depends(get_session),
):
    """归档学生卷：format=html 正式卷面；format=docx 可编辑 Word。"""
    pv = _archive_export_paper(course_id, archive_id, session)
    if export_format == "docx":
        data = export_exam_paper_docx(session, archive_id, course_id=course_id, pv=pv)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": 'attachment; filename="exam-paper.docx"'},
        )
    return HTMLResponse(content=export_student_paper_html(session, archive_id, course_id=course_id, pv=pv))


@router.get("/paper-archives/{archive_id}/export/answer-key")
def export_archive_answer_key(
    course_id: str,
    archive_id: str,
    session: Session = Depends(get_session),
):
    """归档答卷 HTML（含答案）。"""
    pv = _archive_export_paper(course_id, archive_id, session)
    return HTMLResponse(content=export_answer_key_html(session, archive_id, course_id=course_id, pv=pv))


@router.get("/paper-archives/{archive_id}/export/answer-card")
def export_archive_answer_card(
    course_id: str,
    archive_id: str,
    export_format: str = Query("html", alias="format", pattern="^(html|docx)$"),
    session: Session = Depends(get_session),
):
    """归档答题卡：format=html 空卷；format=docx 可编辑 Word。"""
    pv = _archive_export_paper(course_id, archive_id, session)
    if export_format == "docx":
        data = export_answer_card_docx(session, archive_id, course_id=course_id, pv=pv)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": 'attachment; filename="answer-card.docx"'},
        )
    return HTMLResponse(content=export_answer_card_html(session, archive_id, course_id=course_id, pv=pv))


@router.get("/paper-archives/{archive_id}/export/bundle")
def export_archive_bundle(
    course_id: str,
    archive_id: str,
    session: Session = Depends(get_session),
):
    """归档一键打包：全部导出产物打成一个 zip。"""
    pv = _archive_export_paper(course_id, archive_id, session)
    data, filename = bundle_paper_exports(session, archive_id, course_id=course_id, pv=pv)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
