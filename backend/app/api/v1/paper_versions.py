"""PaperVersion 相关端点（课程作用域）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import exam_projects, paper_versions
from app.db.session import get_session
from app.services.paper_version_service import (
    Conflict,
    PendingNeedsReview,
    PaperVersionError,
    confirm_paper_version,
    export_answer_detail_json,
    export_answer_key_html,
    export_student_paper_html,
    get_paper_version,
    list_needs_review,
    revert_to_candidate,
    update_paper_item,
)

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["paper-versions"])


def _resolve_pv_for_project(
    session: Session, *, course_id: str, project_id: str
) -> str:
    """解析项目当前 paper_version：active_paper_version_id → 最新版本号。"""
    proj = session.execute(
        select(
            exam_projects.c.active_paper_version_id,
            exam_projects.c.id,
        ).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="exam project not found")
    active = proj._mapping["active_paper_version_id"]
    if active:
        return active
    latest = session.execute(
        select(paper_versions.c.id)
        .where(
            paper_versions.c.exam_project_id == project_id,
            paper_versions.c.course_id == course_id,
        )
        .order_by(paper_versions.c.version_no.desc())
        .limit(1)
    ).one_or_none()
    if latest is None:
        raise HTTPException(status_code=404, detail="no paper version exists for project")
    return latest._mapping["id"]


@router.get("/exam-projects/{project_id}/paper-versions/current", response_model=dict)
def get_current_paper_version(
    course_id: str,
    project_id: str,
    session: Session = Depends(get_session),
) -> dict:
    pv_id = _resolve_pv_for_project(session, course_id=course_id, project_id=project_id)
    try:
        return get_paper_version(session, pv_id, course_id=course_id)
    except PaperVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/paper-versions/{pv_id}/needs-review", response_model=list[dict])
def list_pending_review(
    course_id: str,
    pv_id: str,
    session: Session = Depends(get_session),
) -> list[dict]:
    # 校验 pv 存在且归属
    exists = session.execute(
        select(paper_versions.c.id).where(
            paper_versions.c.id == pv_id,
            paper_versions.c.course_id == course_id,
        )
    ).one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="paper version not found")
    return list_needs_review(session, pv_id, course_id=course_id)


@router.patch("/paper-versions/{pv_id}/items/{item_index}", response_model=dict)
def patch_paper_item(
    course_id: str,
    pv_id: str,
    item_index: int,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    allowed = {"teacher_override_patch", "clear_needs_review"}
    if not isinstance(body, dict) or not set(body.keys()).issubset(allowed):
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="body must be a dict")
        bad = sorted(set(body.keys()) - allowed)
        raise HTTPException(status_code=422, detail=f"unsupported keys: {bad}")
    teacher_override_patch = body.get("teacher_override_patch") or {}
    clear_needs_review = bool(body.get("clear_needs_review", False))
    try:
        return update_paper_item(
            session,
            course_id=course_id,
            paper_version_id=pv_id,
            item_index=item_index,
            teacher_override_patch=teacher_override_patch,
            clear_needs_review=clear_needs_review,
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaperVersionError as exc:
        msg = str(exc)
        if "不在该试卷版本中" in msg or "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)


@router.post("/paper-versions/{pv_id}/confirm", response_model=dict)
def confirm_pv(
    course_id: str,
    pv_id: str,
    body: dict | None = None,
    session: Session = Depends(get_session),
) -> dict:
    body = body or {}
    force_ignore = bool(body.get("force_ignore_needs_review", False)) if isinstance(body, dict) else False
    try:
        return confirm_paper_version(
            session,
            course_id=course_id,
            paper_version_id=pv_id,
            force_ignore_needs_review=force_ignore,
        )
    except PendingNeedsReview as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "item_indices": getattr(exc, "item_indices", []),
            },
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaperVersionError as exc:
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)


@router.post("/paper-versions/{pv_id}/revert", response_model=dict)
def revert_pv(
    course_id: str,
    pv_id: str,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return revert_to_candidate(
            session, course_id=course_id, paper_version_id=pv_id
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaperVersionError as exc:
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)


# ─── 导出端点 ────────────────────────────────────────────────

@router.get("/exam-projects/{project_id}/paper-versions/{pv_id}/export/json")
def export_json(
    course_id: str,
    project_id: str,
    pv_id: str,
    session: Session = Depends(get_session),
):
    """答案细则 JSON 下载。"""
    try:
        data = export_answer_detail_json(session, pv_id, course_id=course_id)
    except PaperVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": f'attachment; filename="answer_detail_v{data.get("version_no", 1)}.json"'
        },
    )


@router.get("/exam-projects/{project_id}/paper-versions/{pv_id}/export/student")
def export_student(
    course_id: str,
    project_id: str,
    pv_id: str,
    session: Session = Depends(get_session),
):
    """学生卷 HTML（无答案，可浏览器打印为 PDF）。"""
    try:
        html = export_student_paper_html(session, pv_id, course_id=course_id)
    except PaperVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return HTMLResponse(content=html)


@router.get("/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-key")
def export_answer_key(
    course_id: str,
    project_id: str,
    pv_id: str,
    session: Session = Depends(get_session),
):
    """答卷 HTML（含答案，可浏览器打印为 PDF）。"""
    try:
        html = export_answer_key_html(session, pv_id, course_id=course_id)
    except PaperVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return HTMLResponse(content=html)
