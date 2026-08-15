"""Assessment-led syllabus framework endpoints."""

from __future__ import annotations

from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.adapters.model.deepseek_gateway import DeepSeekJsonClient
from app.adapters.model.deepseek_semantic_extractors import DeepSeekSyllabusExtractor
from app.config import settings
from app.db.session import get_session
from app.db.session import get_session_factory
from app.domain.framework.models import FrameworkConfirmation, SyllabusExtractor
from app.services import course_service, framework_service
from app.services.model_call_service import DatabaseModelCallRecorder
from app.workflows.framework_graph import build_framework_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["framework"])
_syllabus_extractor_lock = Lock()


class FrameworkRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teaching_material_version_id: str
    assessment_material_version_id: str


def get_syllabus_extractor(request: Request) -> SyllabusExtractor:
    extractor = getattr(request.app.state, "syllabus_extractor", None)
    if extractor is not None:
        return extractor
    with _syllabus_extractor_lock:
        extractor = getattr(request.app.state, "syllabus_extractor", None)
        if extractor is not None:
            return extractor
        if not _deepseek_configured():
            raise HTTPException(status_code=503, detail="syllabus semantic extractor is not configured")
        client = DeepSeekJsonClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            recorder=DatabaseModelCallRecorder(get_session_factory()),
        )
        extractor = DeepSeekSyllabusExtractor(client)
        request.app.state.syllabus_extractor = extractor
        return extractor


def _deepseek_configured() -> bool:
    return all(
        value.strip()
        for value in (
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.deepseek_model,
        )
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="framework resource not found")


@router.post("/framework-runs", status_code=status.HTTP_202_ACCEPTED)
def create_framework_run(
    course_id: str,
    payload: FrameworkRunCreate,
    session: Session = Depends(get_session),
    extractor: SyllabusExtractor = Depends(get_syllabus_extractor),
) -> dict:
    state = None
    try:
        state = framework_service.create_framework_run(session, course_id=course_id, **payload.model_dump())
        repository = framework_service.DatabaseFrameworkRepository(session)
        graph = build_framework_graph(extractor, repository, checkpointer=InMemorySaver())
        paused = graph.invoke(state, config={"configurable": {"thread_id": state["run_id"]}})
        return {
            "run_id": state["run_id"],
            "candidate_id": paused["candidate_id"],
            "status": "awaiting_teacher_confirmation",
        }
    except course_service.CourseNotFoundError:
        raise _not_found()
    except framework_service.FrameworkInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        if state is not None:
            framework_service.fail_framework_run(
                session,
                course_id=course_id,
                run_id=state["run_id"],
                error_code="model_output_error",
                error_message=str(exc),
            )
        raise HTTPException(status_code=502, detail="syllabus semantic extraction failed")


@router.get("/framework-runs/{run_id}")
def get_framework_run(course_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return framework_service.get_framework_run(session, course_id=course_id, run_id=run_id)
    except framework_service.FrameworkNotFoundError:
        raise _not_found()


@router.get("/framework-runs/{run_id}/candidate")
def get_framework_candidate(course_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return framework_service.get_framework_candidate(session, course_id=course_id, run_id=run_id)
    except framework_service.FrameworkNotFoundError:
        raise _not_found()


@router.post("/framework-runs/{run_id}/confirm")
def confirm_framework(
    course_id: str,
    run_id: str,
    confirmation: FrameworkConfirmation,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return framework_service.confirm_framework_run(
            session,
            course_id=course_id,
            run_id=run_id,
            confirmation=confirmation,
        )
    except framework_service.FrameworkNotFoundError:
        raise _not_found()
    except framework_service.FrameworkInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/framework-runs/{run_id}/reject")
def reject_framework(course_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return framework_service.reject_framework_run(session, course_id=course_id, run_id=run_id)
    except framework_service.FrameworkNotFoundError:
        raise _not_found()
    except framework_service.FrameworkInputError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/framework-versions/current")
def get_current_framework(course_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return framework_service.get_current_framework(session, course_id=course_id)
    except framework_service.FrameworkNotFoundError:
        raise _not_found()
