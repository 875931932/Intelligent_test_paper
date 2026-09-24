"""PaperVersion 相关端点（课程作用域）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.db.schema import paper_versions
from app.db.session import get_session
from app.services import ai_create_service, ai_revise_service, paper_review_service
from app.services.ai_create_service import AiCreateConflict, AiCreateError
from app.services.ai_revise_service import AiReviseConflict, AiReviseError
from app.services.paper_review_service import PaperReviewError
from app.services.paper_version_service import (
    Conflict,
    PendingNeedsReview,
    PaperVersionError,
    confirm_paper_version,
    create_paper_item as create_paper_item_svc,
    delete_paper_item as delete_paper_item_svc,
    export_answer_card_html,
    export_answer_detail_json,
    export_answer_key_html,
    export_student_paper_html,
    get_paper_version,
    list_needs_review,
    reorder_paper_items as reorder_paper_items_svc,
    resolve_current_paper_version_id,
    revert_to_candidate,
    update_paper_item,
)

# 试卷链路（读/改/定稿/导出）全部要求登录：router 级依赖一次覆盖全部端点，
# 新增端点默认带鉴权，避免再出现裸奔端点。
router = APIRouter(
    prefix="/api/v1/courses/{course_id}",
    tags=["paper-versions"],
    dependencies=[Depends(get_current_user)],
)


class PaperReviewRequest(BaseModel):
    """整卷 AI 质量评审请求：可选关注点，纯只读、不携带任何写字段。"""

    model_config = ConfigDict(extra="forbid")

    instruction: str = ""


def _resolve_pv_for_project(
    session: Session, *, course_id: str, project_id: str
) -> str:
    """解析项目当前 paper_version：即项目的 ``active_paper_version_id``。

    解析规则集中在 paper_version_service（pick_current_paper_version_id），
    与项目摘要、导出共用同一语义，避免各处规则漂移。
    """
    try:
        return resolve_current_paper_version_id(
            session, course_id=course_id, project_id=project_id
        )
    except PaperVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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


@router.post(
    "/paper-versions/{pv_id}/items/{item_index}/ai-revise",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_ai_revise(
    course_id: str,
    pv_id: str,
    item_index: int,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """发起单题 AI 改题任务；提案经校验后由前端 diff 预览，教师确认才落库。"""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a dict")
    instruction = str(body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="instruction 不能为空")
    if not ai_revise_service.llm_configured():
        raise HTTPException(status_code=503, detail="LLM model is not configured")

    try:
        task_id = ai_revise_service.enqueue_ai_revise(
            session,
            course_id=course_id,
            paper_version_id=pv_id,
            item_index=item_index,
            instruction=instruction,
        )
        # 显式 commit：outbox 派发会用另一个事务/连接读取事件，任务行必须先落地
        session.commit()
    except AiReviseConflict as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except AiReviseError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg or "不在该试卷版本中" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)

    # 与 generate 同款：真实任务经 transactional outbox 投递给 Celery；
    # 投递暂时失败时事件保持 pending，任务不会丢失，后续 dispatcher 可重试。
    from app.infrastructure.tasks.celery_app import CeleryPublisher
    from app.infrastructure.tasks.outbox import dispatch_pending_events

    try:
        dispatch_pending_events(
            session,
            CeleryPublisher(),
            course_id=course_id,
            limit=5,
        )
        session.commit()
    except Exception:
        session.rollback()

    return {"task_run_id": task_id}


@router.post(
    "/paper-versions/{pv_id}/items/ai-generate",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_ai_generate(
    course_id: str,
    pv_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """发起整题 AI 生成任务；提案经校验后由前端回填新增表单，教师确认才落库。"""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a dict")
    instruction = str(body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="instruction 不能为空")
    if not ai_create_service.llm_configured():
        raise HTTPException(status_code=503, detail="LLM model is not configured")

    try:
        task_id = ai_create_service.enqueue_ai_create(
            session,
            course_id=course_id,
            paper_version_id=pv_id,
            instruction=instruction,
        )
        # 显式 commit：outbox 派发会用另一个事务/连接读取事件，任务行必须先落地
        session.commit()
    except AiCreateConflict as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except AiCreateError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)

    # 与 ai-revise 同款：真实任务经 transactional outbox 投递给 Celery；
    # 投递暂时失败时事件保持 pending，任务不会丢失，后续 dispatcher 可重试。
    from app.infrastructure.tasks.celery_app import CeleryPublisher
    from app.infrastructure.tasks.outbox import dispatch_pending_events

    try:
        dispatch_pending_events(
            session,
            CeleryPublisher(),
            course_id=course_id,
            limit=5,
        )
        session.commit()
    except Exception:
        session.rollback()

    return {"task_run_id": task_id}


@router.post(
    "/paper-versions/{pv_id}/ai-review",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_ai_review(
    course_id: str,
    pv_id: str,
    body: PaperReviewRequest | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """发起整卷 AI 质量评审任务：只读报告（难度分布/题面表述/答案与解析一致性/
    覆盖配额/风险题的解读），端点只写 task_runs，不碰试卷/合同/蓝图任何数据。

    不做状态禁令：报告是只读的，定稿（finalized）试卷照样可评审——这正是它的
    价值。措辞定位是试卷稿质量评审，不是学生答卷评分（在线阅卷是范围外需求）。
    """
    req = body or PaperReviewRequest()
    instruction = str(req.instruction or "").strip()

    if not paper_review_service.llm_configured():
        raise HTTPException(status_code=503, detail="LLM model is not configured")

    try:
        task_id = paper_review_service.enqueue_review(
            session,
            course_id=course_id,
            paper_version_id=pv_id,
            instruction=instruction,
        )
        # 显式 commit：outbox 派发会用另一个事务/连接读取事件，任务行必须先落地
        session.commit()
    except PaperReviewError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)

    # 与 ai-revise 同款：真实任务经 transactional outbox 投递给 Celery；
    # 投递暂时失败时事件保持 pending，任务不会丢失，后续 dispatcher 可重试。
    from app.infrastructure.tasks.celery_app import CeleryPublisher
    from app.infrastructure.tasks.outbox import dispatch_pending_events

    try:
        dispatch_pending_events(
            session,
            CeleryPublisher(),
            course_id=course_id,
            limit=5,
        )
        session.commit()
    except Exception:
        session.rollback()

    return {"task_run_id": task_id}


@router.put("/paper-versions/{pv_id}/items/reorder", response_model=dict)
def reorder_paper_items(
    course_id: str,
    pv_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """按新顺序重排题目（body 形如 {ordered_indices: [3,1,2]}）。"""
    if not isinstance(body, dict) or not isinstance(body.get("ordered_indices"), list):
        raise HTTPException(status_code=422, detail="body 需包含 ordered_indices 数组")
    try:
        return reorder_paper_items_svc(
            session, course_id=course_id, paper_version_id=pv_id,
            ordered_indices=body["ordered_indices"],
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaperVersionError as exc:
        if "不存在" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/paper-versions/{pv_id}/items", response_model=dict)
def create_paper_item(
    course_id: str,
    pv_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """在试卷末尾新增一道教师自拟题目。"""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a dict")
    if not str(body.get("stem", "")).strip():
        raise HTTPException(status_code=422, detail="stem 不能为空")
    # 判断题答案是布尔值：不能走 str(... or "")，否则 false 会变成空串被当成缺答案
    raw_answer = body.get("answer")
    answer_value = raw_answer if isinstance(raw_answer, bool) else str(raw_answer or "")
    try:
        return create_paper_item_svc(
            session, course_id=course_id, paper_version_id=pv_id,
            stem=str(body.get("stem", "")),
            question_type=str(body.get("question_type") or "short_answer"),
            options=body.get("options"),
            answer=answer_value,
            explanation=str(body.get("explanation") or ""),
            score=float(body.get("score") or 0),
            difficulty=str(body.get("difficulty") or "medium"),
            rubric=body.get("rubric"),
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaperVersionError as exc:
        if "不存在" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/paper-versions/{pv_id}/items/{item_index}", response_model=dict)
def delete_paper_item(
    course_id: str,
    pv_id: str,
    item_index: int,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return delete_paper_item_svc(
            session, course_id=course_id, paper_version_id=pv_id, item_index=item_index
        )
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PaperVersionError as exc:
        if "不在该试卷版本中" in str(exc) or "不存在" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))


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


@router.get("/exam-projects/{project_id}/paper-versions/{pv_id}/export/answer-card")
def export_answer_card(
    course_id: str,
    project_id: str,
    pv_id: str,
    session: Session = Depends(get_session),
):
    """答题卡 HTML（学生作答用空卷，可浏览器打印为 PDF）。"""
    try:
        html = export_answer_card_html(session, pv_id, course_id=course_id)
    except PaperVersionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return HTMLResponse(content=html)
