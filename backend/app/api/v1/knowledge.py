"""Knowledge-tree organization endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.domain.knowledge.models import KnowledgeFileExtractor, KnowledgeTreeCandidate, KnowledgeTreeConfirmation
from app.services import course_service, knowledge_publish_service
from app.workflows.organization_graph import build_organization_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["knowledge"])


class OrganizationRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_version_ids: list[str] = Field(min_length=1)


def get_knowledge_extractor(request: Request) -> KnowledgeFileExtractor:
    extractor = getattr(request.app.state, "knowledge_extractor", None)
    if extractor is None:
        raise HTTPException(status_code=503, detail="knowledge semantic extractor is not configured")
    return extractor


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="knowledge resource not found")


@router.post("/organization-runs", status_code=status.HTTP_202_ACCEPTED)
def create_organization_run(course_id: str, payload: OrganizationRunCreate, session: Session = Depends(get_session), extractor: KnowledgeFileExtractor = Depends(get_knowledge_extractor)) -> dict:
    state = None
    try:
        state = knowledge_publish_service.create_organization_state(session, course_id=course_id, material_version_ids=payload.material_version_ids)
        graph = build_organization_graph(extractor, knowledge_publish_service.DatabaseKnowledgeRepository(session), checkpointer=InMemorySaver())
        paused = graph.invoke(state, config={"configurable": {"thread_id": state["run_id"]}})
        return {"run_id": state["run_id"], "candidate_id": paused["candidate_id"], "status": "awaiting_teacher_confirmation"}
    except course_service.CourseNotFoundError:
        raise _not_found()
    except knowledge_publish_service.KnowledgePublishError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        if state is not None:
            session.execute(knowledge_publish_service.organization_runs.update().where(knowledge_publish_service.organization_runs.c.id == state["run_id"]).values(status="failed", error_code="model_output_error", error_message=str(exc)[:2000]))
            session.commit()
        raise HTTPException(status_code=502, detail="knowledge organization failed")


@router.get("/organization-runs/{run_id}")
def get_run(course_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return knowledge_publish_service.get_organization_run(session, course_id=course_id, run_id=run_id)
    except knowledge_publish_service.KnowledgePublishError:
        raise _not_found()


@router.get("/organization-runs/{run_id}/candidate")
def get_candidate(course_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return knowledge_publish_service.get_organization_candidate(session, course_id=course_id, run_id=run_id)
    except knowledge_publish_service.KnowledgePublishError:
        raise _not_found()


@router.post("/organization-runs/{run_id}/publish")
def publish_tree(course_id: str, run_id: str, confirmation: KnowledgeTreeConfirmation, session: Session = Depends(get_session)) -> dict:
    try:
        candidate = knowledge_publish_service.get_organization_candidate(session, course_id=course_id, run_id=run_id)
        result = knowledge_publish_service.DatabaseKnowledgeRepository(session).publish({"course_id": course_id, "run_id": run_id, "candidate_id": candidate["id"]}, KnowledgeTreeCandidate.model_validate(candidate["payload"]), confirmation)
        return result
    except knowledge_publish_service.KnowledgePublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
