"""Knowledge-tree organization endpoints."""

from __future__ import annotations

from threading import RLock

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.model.deepseek_gateway import DeepSeekJsonClient
from app.adapters.model.deepseek_semantic_extractors import (
    DeepSeekExamPointEvidenceClassifier,
    DeepSeekExamPointKnowledgeConsolidator,
)
from app.adapters.model.embedding_gateway import OpenAICompatibleEmbeddingGateway
from app.config import settings
from app.db.session import get_session, get_session_factory
from app.domain.knowledge.models import (
    ExamPointKnowledgeConsolidator,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
)
from app.domain.knowledge.relevance import ExamPointEvidenceClassifier
from app.services import course_service, knowledge_publish_service
from app.services.model_call_service import DatabaseModelCallRecorder
from app.services.staging_retrieval_service import EmbeddingClient, HybridStagingRetriever
from app.workflows.organization_graph import build_organization_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["knowledge"])
_organization_state_lock = RLock()


class OrganizationRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_version_ids: list[str] = Field(min_length=1)


def get_organization_embedder(request: Request) -> EmbeddingClient:
    embedder = getattr(request.app.state, "organization_embedder", None)
    if embedder is not None:
        return embedder
    with _organization_state_lock:
        embedder = getattr(request.app.state, "organization_embedder", None)
        if embedder is not None:
            return embedder
        if not (
            settings.embedding_api_key.strip()
            and settings.embedding_base_url.strip()
            and settings.embedding_model.strip()
        ):
            raise HTTPException(status_code=503, detail="embedding client is not configured")
        embedder = OpenAICompatibleEmbeddingGateway(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            api_format=settings.embedding_api_format,
        )
        request.app.state.organization_embedder = embedder
        return embedder


def get_exam_point_classifier(request: Request) -> ExamPointEvidenceClassifier:
    classifier = getattr(request.app.state, "exam_point_evidence_classifier", None)
    if classifier is not None:
        return classifier
    with _organization_state_lock:
        classifier = getattr(request.app.state, "exam_point_evidence_classifier", None)
        if classifier is not None:
            return classifier
        if not _deepseek_configured():
            raise HTTPException(status_code=503, detail="semantic classifier is not configured")
        client = _get_semantic_json_client(request)
        classifier = DeepSeekExamPointEvidenceClassifier(client)
        request.app.state.exam_point_evidence_classifier = classifier
        return classifier


def get_exam_point_consolidator(request: Request) -> ExamPointKnowledgeConsolidator:
    consolidator = getattr(request.app.state, "exam_point_knowledge_consolidator", None)
    if consolidator is not None:
        return consolidator
    with _organization_state_lock:
        consolidator = getattr(request.app.state, "exam_point_knowledge_consolidator", None)
        if consolidator is not None:
            return consolidator
        if not _deepseek_configured():
            raise HTTPException(status_code=503, detail="knowledge consolidator is not configured")
        client = _get_semantic_json_client(request)
        consolidator = DeepSeekExamPointKnowledgeConsolidator(client)
        request.app.state.exam_point_knowledge_consolidator = consolidator
        return consolidator


def _get_semantic_json_client(request: Request) -> DeepSeekJsonClient:
    client = getattr(request.app.state, "semantic_json_client", None)
    if client is not None:
        return client
    with _organization_state_lock:
        client = getattr(request.app.state, "semantic_json_client", None)
        if client is not None:
            return client
        client = DeepSeekJsonClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            recorder=DatabaseModelCallRecorder(get_session_factory()),
        )
        request.app.state.semantic_json_client = client
        return client


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


@router.get("/published-knowledge")
def get_published_knowledge(course_id: str, session: Session = Depends(get_session)) -> dict:
    """当前已发布知识目录的命题输入视图：考点 / 单元 / 知识卡。

    供教师控制台在蓝图与合同阶段直接读取——cards 键为卡片 DB id，
    units.card_ids 与之对齐；chapter_weights 可由 exam_points 的
    anchor_key → weight_value 聚合得到。
    """
    from sqlalchemy import select

    from app.db.schema import assessment_units, exam_points, knowledge_cards, knowledge_catalog_versions, knowledge_evidence_links

    catalog = session.execute(
        select(knowledge_catalog_versions)
        .where(
            knowledge_catalog_versions.c.course_id == course_id,
            knowledge_catalog_versions.c.status == "published",
        )
        .order_by(knowledge_catalog_versions.c.version_no.desc())
        .limit(1)
    ).mappings().one_or_none()
    if catalog is None:
        raise _not_found()
    catalog_id = catalog["id"]
    framework_version_id = catalog["framework_version_id"]

    point_rows = session.execute(
        select(exam_points)
        .where(
            exam_points.c.course_id == course_id,
            exam_points.c.framework_version_id == framework_version_id,
            exam_points.c.status == "confirmed",
        )
        .order_by(exam_points.c.code)
    ).mappings().all()
    points_by_id = {row["id"]: dict(row) for row in point_rows}

    unit_rows = session.execute(
        select(assessment_units)
        .where(
            assessment_units.c.course_id == course_id,
            assessment_units.c.catalog_version_id == catalog_id,
            assessment_units.c.status == "active",
        )
        .order_by(assessment_units.c.code)
    ).mappings().all()
    unit_ids = [row["id"] for row in unit_rows]

    card_rows = (
        session.execute(
            select(knowledge_cards)
            .where(
                knowledge_cards.c.course_id == course_id,
                knowledge_cards.c.catalog_version_id == catalog_id,
                knowledge_cards.c.status == "active",
                knowledge_cards.c.assessment_unit_id.in_(unit_ids),
            )
            .order_by(knowledge_cards.c.assessment_unit_id, knowledge_cards.c.name)
        ).mappings().all()
        if unit_ids else []
    )
    # 查询每张卡的直接证据数，用于未落地判定
    card_id_list = [row["id"] for row in card_rows] if card_rows else []
    grounded_card_ids: set[str] = set()
    if card_id_list:
        link_rows = session.execute(
            select(knowledge_evidence_links.c.knowledge_card_id).where(
                knowledge_evidence_links.c.knowledge_card_id.in_(card_id_list),
                knowledge_evidence_links.c.evidence_role == "direct",
                knowledge_evidence_links.c.lifecycle_status == "active",
            )
        ).mappings().all()
        grounded_card_ids = {r["knowledge_card_id"] for r in link_rows}
    cards_by_unit: dict[str, list[str]] = {}
    cards_payload: dict[str, dict] = {}
    for row in card_rows:
        card = dict(row)
        card_id = card["id"]
        cards_payload[card_id] = {
            "name": card["name"],
            "performance_statement": card["performance_statement"],
            "assessable_content": card["assessable_content"],
            "scope_boundary": card["scope_boundary"],
            "cognitive_targets": card["cognitive_targets"],
            "allowed_question_types": card["allowed_question_types"],
            "importance": card["importance"],
            "concept_cluster": card["concept_cluster"],
            "answer_proposition": card["answer_proposition"],
            "answer_boundary": card["answer_proposition"],
            "prompt_material": card["prompt_material"],
            "relation_edges": card.get("relation_edges", []),
            "grounded": card_id in grounded_card_ids,
        }
        cards_by_unit.setdefault(card["assessment_unit_id"], []).append(card_id)

    units_payload = []
    for row in unit_rows:
        point = points_by_id.get(row["exam_point_id"]) if row["exam_point_id"] else None
        units_payload.append({
            "unit_id": row["id"],
            "code": row["code"],
            "title": row["title"],
            "performance_statement": row["performance_statement"],
            "exam_point_id": row["exam_point_id"] or "",
            "exam_point_code": point["code"] if point else "",
            "anchor_key": point["anchor_key"] if point else "",
            "card_ids": cards_by_unit.get(row["id"], []),
        })

    exam_points_payload = [
        {
            "id": row["id"],
            "code": row["code"],
            "title": row["title"],
            "assessment_requirement": row["assessment_requirement"],
            "anchor_key": row["anchor_key"],
            "weight_value": row["weight_value"],
            "weight_source": row["weight_source"],
            "cognitive_targets": row["cognitive_targets"],
            "allowed_question_types": row["allowed_question_types"],
            "operational_detail_policy": row["operational_detail_policy"],
        }
        for row in point_rows
    ]
    return {
        "catalog_version_id": catalog_id,
        "framework_version_id": framework_version_id,
        "exam_points": exam_points_payload,
        "units": units_payload,
        "knowledge_cards": cards_payload,
    }


@router.get("/published-knowledge/cards/{card_id}/evidence")
def get_card_evidence(
    course_id: str,
    card_id: str,
    session: Session = Depends(get_session),
) -> list[dict]:
    """获取知识卡的证据链（direct/supporting/background）。"""
    from sqlalchemy import select

    from app.db.schema import evidence_chunks, knowledge_evidence_links

    rows = session.execute(
        select(
            knowledge_evidence_links.c.evidence_role,
            knowledge_evidence_links.c.confidence,
            knowledge_evidence_links.c.lifecycle_status,
            evidence_chunks.c.content,
            evidence_chunks.c.locator,
            evidence_chunks.c.material_version_id,
        ).join(
            evidence_chunks,
            evidence_chunks.c.id == knowledge_evidence_links.c.evidence_chunk_id,
        ).where(
            knowledge_evidence_links.c.knowledge_card_id == card_id,
            knowledge_evidence_links.c.lifecycle_status == "active",
        )
    ).mappings().all()
    return [
        {
            "evidence_role": r["evidence_role"],
            "confidence": r["confidence"],
            "content": r["content"],
            "locator": r["locator"],
            "material_version_id": r["material_version_id"],
        }
        for r in rows
    ]
