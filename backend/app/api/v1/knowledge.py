"""Knowledge-tree organization endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_session
from app.domain.knowledge.models import (
    ExamPointKnowledgeConsolidator,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.domain.knowledge.relevance import ExamPointEvidenceClassifier
from app.services import course_service, knowledge_publish_service
from app.services.staging_retrieval_service import EmbeddingClient, HybridStagingRetriever
from app.workflows.organization_graph import build_organization_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["knowledge"])


class OrganizationRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_version_ids: list[str] = Field(min_length=1)


def get_organization_embedder(request: Request) -> EmbeddingClient:
    embedder = getattr(request.app.state, "organization_embedder", None)
    if embedder is None:
        raise HTTPException(status_code=503, detail="embedding client is not configured")
    return embedder


def get_exam_point_classifier(request: Request) -> ExamPointEvidenceClassifier:
    classifier = getattr(request.app.state, "exam_point_evidence_classifier", None)
    if classifier is None:
        raise HTTPException(status_code=503, detail="semantic classifier is not configured")
    return classifier


def get_exam_point_consolidator(request: Request) -> ExamPointKnowledgeConsolidator:
    consolidator = getattr(request.app.state, "exam_point_knowledge_consolidator", None)
    if consolidator is None:
        raise HTTPException(status_code=503, detail="knowledge consolidator is not configured")
    return consolidator


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="knowledge resource not found")


@router.post("/organization-runs", status_code=status.HTTP_202_ACCEPTED)
def create_organization_run(
    course_id: str,
    payload: OrganizationRunCreate,
    session: Session = Depends(get_session),
    embedder: EmbeddingClient = Depends(get_organization_embedder),
    classifier: ExamPointEvidenceClassifier = Depends(get_exam_point_classifier),
    consolidator: ExamPointKnowledgeConsolidator = Depends(get_exam_point_consolidator),
) -> dict:
    state = None
    try:
        state = knowledge_publish_service.create_organization_state(
            session,
            course_id=course_id,
            material_version_ids=payload.material_version_ids,
            embedder=embedder,
        )
        retriever = HybridStagingRetriever(
            embedder=embedder,
            top_k=settings.organization_retrieval_top_k,
            minimum_score=settings.organization_retrieval_min_score,
        )
        graph = build_organization_graph(
            retriever,
            classifier,
            consolidator,
            knowledge_publish_service.DatabaseKnowledgeRepository(session),
            checkpointer=InMemorySaver(),
        )
        paused = graph.invoke(state, config={"configurable": {"thread_id": state["run_id"]}})
        return {"run_id": state["run_id"], "candidate_id": paused["candidate_id"], "status": "awaiting_teacher_confirmation"}
    except course_service.CourseNotFoundError:
        raise _not_found()
    except knowledge_publish_service.KnowledgePublishError as exc:
        if "embedding" in str(exc).casefold():
            raise HTTPException(status_code=503, detail="embedding service is unavailable")
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        if state is not None:
            session.execute(
                knowledge_publish_service.organization_runs.update()
                .where(
                    knowledge_publish_service.organization_runs.c.id == state["run_id"],
                    knowledge_publish_service.organization_runs.c.course_id == course_id,
                )
                .values(
                    status="failed",
                    error_code="organization_invariant_error",
                    error_message="knowledge organization stopped because an invariant failed",
                )
            )
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
