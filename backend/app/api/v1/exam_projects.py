"""exam_projects CRUD 端点（课程作用域），以及 blueprint / contract / generation 子端点。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.schema import (
    blueprint_versions,
    exam_projects,
    paper_versions,
    task_runs,
)
from app.db.session import get_session, get_session_factory
from app.config import settings
from app.services import exam_project_service
from app.services.blueprint_persistence_service import (
    BlueprintPersistenceError,
    BlueprintValidationError,
    confirm_blueprint as service_confirm_blueprint,
    create_draft_blueprint,
    list_plan_items,
    update_plan_item,
)
from app.services.contract_execution_service import (
    ContractExecutionError,
    allocate_with_fallback,
    revise_and_confirm,
)
from app.services.contract_service import apply_slot_revisions
from app.services.generation_runner_service import (
    GenerationRunnerError,
    enqueue_generation,
)

router = APIRouter(prefix="/api/v1/courses/{course_id}/exam-projects", tags=["exam-projects"])


class ExamProjectCreate(BaseModel):
    name: str


class ExamProjectStatusUpdate(BaseModel):
    status: str


class GenerationStartRequest(BaseModel):
    """Mock generation is test-only; production requests use the real graph."""

    model_config = ConfigDict(extra="forbid")

    mock_graph: bool = False


def _not_found(detail: str = "exam project not found") -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def _get_project_or_404(session: Session, *, course_id: str, project_id: str) -> dict:
    try:
        return exam_project_service.get_project(
            session, course_id=course_id, project_id=project_id
        )
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()


def _resolve_blueprint_version_id(
    session: Session, *, course_id: str, project_id: str, override: str | None = None
) -> str:
    """按优先级返回 blueprint_version_id：override → active_blueprint_version_id → 最新 draft。"""
    if override:
        # 校验归属
        bv = session.execute(
            select(blueprint_versions.c.id).where(
                blueprint_versions.c.id == override,
                blueprint_versions.c.course_id == course_id,
                blueprint_versions.c.exam_project_id == project_id,
            )
        ).one_or_none()
        if bv is None:
            raise HTTPException(status_code=404, detail="blueprint version not found")
        return override
    proj = session.execute(
        select(
            exam_projects.c.active_blueprint_version_id,
            exam_projects.c.id,
        ).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).one_or_none()
    if proj is None:
        raise _not_found()
    active = proj._mapping["active_blueprint_version_id"]
    if active:
        return active
    # fallback: 最新版本
    latest = session.execute(
        select(blueprint_versions.c.id)
        .where(
            blueprint_versions.c.exam_project_id == project_id,
            blueprint_versions.c.course_id == course_id,
        )
        .order_by(blueprint_versions.c.version_no.desc())
        .limit(1)
    ).one_or_none()
    if latest is None:
        raise HTTPException(status_code=404, detail="no blueprint version exists")
    return latest._mapping["id"]


# ===========================================================================
# 原有 4 个端点
# ===========================================================================

@router.get("", response_model=list[dict])
def list_projects(course_id: str, session: Session = Depends(get_session)) -> list[dict]:
    return exam_project_service.list_projects(session, course_id=course_id)


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
def create(course_id: str, payload: ExamProjectCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.create_project(session, course_id=course_id, name=payload.name)
    except exam_project_service.ExamProjectConflictError:
        raise HTTPException(status_code=409, detail="project name already exists in this course")


@router.get("/{project_id}", response_model=dict)
def get_one(course_id: str, project_id: str, session: Session = Depends(get_session)) -> dict:
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    try:
        return exam_project_service.get_project(session, course_id=course_id, project_id=project_id)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()


@router.patch("/{project_id}", response_model=dict)
def patch(course_id: str, project_id: str, payload: ExamProjectStatusUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        return exam_project_service.update_status(session, course_id=course_id, project_id=project_id, status=payload.status)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()


# ===========================================================================
# Blueprint 子端点
# ===========================================================================

@router.post("/{project_id}/blueprints", response_model=dict, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    course_id: str,
    project_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    # 校验项目存在
    _get_project_or_404(session, course_id=course_id, project_id=project_id)

    required = [
        "framework_version_id", "catalog_version_id",
        "type_rules", "chapter_weights", "units",
    ]
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing keys: {missing}")

    units_payload = body["units"]
    card_semantic_profiles = body.get("card_semantic_profiles") or {}
    card_question_types = body.get("card_question_types") or {}

    try:
        bv_id, _plan = create_draft_blueprint(
            session,
            course_id=course_id,
            project_id=project_id,
            framework_version_id=body["framework_version_id"],
            catalog_version_id=body["catalog_version_id"],
            type_rules=body["type_rules"],
            chapter_weights=body["chapter_weights"],
            units_payload=list(units_payload),
            card_semantic_profiles=card_semantic_profiles,
            card_question_types=card_question_types,
        )
    except BlueprintValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BlueprintPersistenceError as exc:
        msg = str(exc)
        if "不存在" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)
    except exam_project_service.ExamProjectNotFoundError:
        raise _not_found()

    plan_items = list_plan_items(session, blueprint_version_id=bv_id)
    return {"blueprint_version_id": bv_id, "plan": plan_items}


@router.get("/{project_id}/blueprints/current/plan-items", response_model=list[dict])
def get_current_plan_items(
    course_id: str,
    project_id: str,
    session: Session = Depends(get_session),
) -> list[dict]:
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    bv_id = _resolve_blueprint_version_id(session, course_id=course_id, project_id=project_id)
    return list_plan_items(session, blueprint_version_id=bv_id)


@router.patch("/plan-items/{plan_item_id}", response_model=dict)
def patch_plan_item(
    plan_item_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    # 只允许的字段
    allowed = {"score", "question_type", "difficulty", "cognitive_level", "exam_point_id", "card_id"}
    if not set(body.keys()).issubset(allowed):
        bad = sorted(set(body.keys()) - allowed)
        raise HTTPException(status_code=422, detail=f"unsupported keys: {bad}")
    try:
        return update_plan_item(session, plan_item_id=plan_item_id, changes=body)
    except BlueprintValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BlueprintPersistenceError as exc:
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)


@router.post("/{project_id}/blueprints/current/confirm", response_model=dict)
def confirm_current_blueprint(
    course_id: str,
    project_id: str,
    body: dict | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    body = body or {}
    override = body.get("blueprint_version_id") if isinstance(body, dict) else None
    bv_id = _resolve_blueprint_version_id(
        session, course_id=course_id, project_id=project_id, override=override
    )
    try:
        return service_confirm_blueprint(
            session, course_id=course_id, project_id=project_id, blueprint_version_id=bv_id
        )
    except BlueprintValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BlueprintPersistenceError as exc:
        msg = str(exc)
        if "只能确认" in msg or "draft" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)


# ===========================================================================
# Contract 子端点
# ===========================================================================

def _contract_snapshot(contract) -> dict:
    slots = [s.model_dump(mode="json") for s in contract.slots]
    return {"slots": slots}


@router.post("/{project_id}/contracts/allocate", response_model=dict)
def allocate_contract(
    course_id: str,
    project_id: str,
    body: dict | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    body = body or {}
    bv_id = _resolve_blueprint_version_id(
        session,
        course_id=course_id,
        project_id=project_id,
        override=body.get("blueprint_version_id") if isinstance(body, dict) else None,
    )
    seed: int | None = body.get("allocation_seed") if isinstance(body, dict) else None
    try:
        contract, used_threshold, history = allocate_with_fallback(
            session, blueprint_version_id=bv_id, allocation_seed=seed
        )
    except ContractExecutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        snap = contract.model_dump(mode="json")
    except Exception:
        snap = _contract_snapshot(contract)
    return {
        "used_threshold": used_threshold,
        "conflicts_history": [list(h) for h in history],
        "contract_snapshot": snap,
    }


@router.patch("/{project_id}/contracts/revise", response_model=dict)
def revise_contract_preview(
    course_id: str,
    project_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    """只预览修订结果，不落库。"""
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    bv_id = _resolve_blueprint_version_id(
        session,
        course_id=course_id,
        project_id=project_id,
        override=body.get("blueprint_version_id"),
    )
    slot_revisions = body.get("slot_revisions") or []
    seed = body.get("allocation_seed")
    try:
        contract, _used, _hist = allocate_with_fallback(
            session, blueprint_version_id=bv_id, allocation_seed=seed
        )
        if slot_revisions:
            from app.services.contract_execution_service import _build_contract_request_from_db
            _req, units_payload, cards_dict = _build_contract_request_from_db(
                session, blueprint_version_id=bv_id, centrality_threshold=_used, allocation_seed=seed
            )
            from app.domain.blueprint.models import UnitCoverage
            contract = apply_slot_revisions(
                contract,
                slot_revisions,
                units=[UnitCoverage(**u) for u in units_payload],
                knowledge_cards=cards_dict,
            )
    except ContractExecutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"revision error: {exc}")
    try:
        snap = contract.model_dump(mode="json")
    except Exception:
        snap = _contract_snapshot(contract)
    return {"revised_contract_snapshot": snap}


@router.post(
    "/{project_id}/contracts/confirm",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
def confirm_contract(
    course_id: str,
    project_id: str,
    body: dict,
    session: Session = Depends(get_session),
) -> dict:
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    bv_id = _resolve_blueprint_version_id(
        session,
        course_id=course_id,
        project_id=project_id,
        override=body.get("blueprint_version_id"),
    )
    slot_revisions = body.get("slot_revisions") or []
    seed = body.get("allocation_seed")
    try:
        result = revise_and_confirm(
            session,
            course_id=course_id,
            project_id=project_id,
            blueprint_version_id=bv_id,
            slot_revisions=list(slot_revisions),
            allocation_seed=seed,
        )
    except ContractExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


# ===========================================================================
# Generation 子端点
# ===========================================================================

def _graph_invoke_factory(request: Request):
    """只返回测试进程显式注入的 mock，生产环境没有合成题回退。"""
    return getattr(request.app.state, "mock_graph_invoke", None)


@router.post(
    "/{project_id}/generate",
    response_model=dict,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate(
    request: Request,
    course_id: str,
    project_id: str,
    body: GenerationStartRequest | None = None,
    session: Session = Depends(get_session),
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> dict:
    _get_project_or_404(session, course_id=course_id, project_id=project_id)
    mock_graph = bool(body.mock_graph) if body is not None else False
    if not mock_graph and not all(value.strip() for value in (
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        settings.deepseek_model,
    )):
        raise HTTPException(status_code=503, detail="LLM model is not configured")

    try:
        task_run_id = enqueue_generation(
            session, course_id=course_id, project_id=project_id
        )
        # 显式 commit：接下来 inline_runner 会打开另一个 session 去抢任务，
        # 若当前事务尚未落地，SQLite/PostgreSQL 默认隔离级别会让 runner
        # 看不见该任务（或加锁等待）导致超时。
        session.commit()
    except GenerationRunnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if mock_graph:
        # 使用注入的 session_factory（可能来自 dependency_overrides，比如测试场景）
        graph_invoke = _graph_invoke_factory(request)
        if graph_invoke is None:
            raise HTTPException(status_code=403, detail="mock generation is not enabled")

        def _handler(sess: Session, task_run_row):
            from app.services.generation_runner_service import (
                execute_generation_task_handler,
            )
            execute_generation_task_handler(
                sess, task_run_row, graph_invoke=graph_invoke, write_paper_version=True
            )

        from app.infrastructure.tasks import inline_runner
        # 最多尝试几次，直到任务被取出或返回 False
        for _ in range(3):
            ok = inline_runner.run_once(
                session_factory,
                handlers={"generation_run": _handler},
            )
            if not ok:
                break
    else:
        # 真实任务经 transactional outbox 投递给 Celery；投递暂时失败时事件保持
        # pending，任务不会丢失，后续 dispatcher 可安全重试。
        from app.infrastructure.tasks.celery_app import CeleryPublisher
        from app.infrastructure.tasks.outbox import dispatch_pending_events

        try:
            dispatch_pending_events(
                session,
                CeleryPublisher(),
                course_id=course_id,
                limit=1,
            )
            session.commit()
        except Exception:
            session.rollback()

    return {"task_run_id": task_run_id}


@router.get("/task-runs/{task_run_id}", response_model=dict)
def get_task_run(
    course_id: str,
    task_run_id: str,
    session: Session = Depends(get_session),
) -> dict:
    row = session.execute(
        select(task_runs).where(
            task_runs.c.id == task_run_id,
            task_runs.c.course_id == course_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task run not found")
    d = dict(row._mapping)
    return {
        "id": d.get("id"),
        "course_id": d.get("course_id"),
        "task_type": d.get("task_type"),
        "status": d.get("status"),
        "stage": d.get("stage"),
        "progress": d.get("progress"),
        "attempt": d.get("attempt"),
        "payload": d.get("payload"),
        "result": d.get("result"),
        "error_code": d.get("error_code"),
        "error_message": d.get("error_message"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "completed_at": d.get("completed_at"),
    }
