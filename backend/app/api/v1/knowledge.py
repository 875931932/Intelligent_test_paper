"""Knowledge-tree organization endpoints."""

from __future__ import annotations

import logging
import threading
import time
from threading import RLock
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.model.llm_gateway import LLMJsonClient, LLMModelError
from app.adapters.model.llm_semantic_extractors import (
    LLMExamPointEvidenceClassifier,
    LLMExamPointKnowledgeConsolidator,
    LLMKnowledgePointExtractor,
    LLMSupplementRecommender,
)
from app.adapters.model.embedding_gateway import OpenAICompatibleEmbeddingGateway
from app.config import settings
from app.db.session import get_session, get_session_factory
from app.domain.knowledge.models import (
    ExamPointKnowledgeConsolidator,
    KnowledgeTreeCandidate,
    KnowledgeTreeConfirmation,
    TreeOperation,
)
from app.domain.knowledge.relevance import ExamPointEvidenceClassifier
from app.services import course_service, knowledge_publish_service
from app.services.model_call_service import DatabaseModelCallRecorder
from app.services.staging_retrieval_service import EmbeddingClient, HybridStagingRetriever
from app.workflows.organization_graph import build_organization_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["knowledge"])
_organization_state_lock = RLock()
_logger = logging.getLogger("organization")


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
        if not _llm_configured():
            raise HTTPException(status_code=503, detail="semantic classifier is not configured")
        client = _get_semantic_json_client(
            request, settings.llm_classify_model or settings.llm_model
        )
        classifier = LLMExamPointEvidenceClassifier(client)
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
        if not _llm_configured():
            raise HTTPException(status_code=503, detail="knowledge consolidator is not configured")
        client = _get_semantic_json_client(
            request, settings.llm_consolidate_model or settings.llm_model
        )
        consolidator = LLMExamPointKnowledgeConsolidator(client)
        request.app.state.exam_point_knowledge_consolidator = consolidator
        return consolidator


def get_knowledge_point_extractor(request: Request) -> LLMKnowledgePointExtractor:
    extractor = getattr(request.app.state, "knowledge_point_extractor", None)
    if extractor is not None:
        return extractor
    with _organization_state_lock:
        extractor = getattr(request.app.state, "knowledge_point_extractor", None)
        if extractor is not None:
            return extractor
        if not _llm_configured():
            raise HTTPException(status_code=503, detail="knowledge point extractor is not configured")
        client = _get_semantic_json_client(
            request, settings.llm_extract_model or settings.llm_model
        )
        extractor = LLMKnowledgePointExtractor(
            client,
            reasoning_effort=settings.organization_extraction_reasoning_effort,
        )
        request.app.state.knowledge_point_extractor = extractor
        return extractor


def get_supplement_recommender(request: Request) -> LLMSupplementRecommender:
    """补证据推荐器：复用分类模型客户端，独立缓存实例。"""
    recommender = getattr(request.app.state, "supplement_recommender", None)
    if recommender is not None:
        return recommender
    with _organization_state_lock:
        recommender = getattr(request.app.state, "supplement_recommender", None)
        if recommender is not None:
            return recommender
        if not _llm_configured():
            raise HTTPException(status_code=503, detail="semantic recommender is not configured")
        client = _get_semantic_json_client(
            request, settings.llm_classify_model or settings.llm_model
        )
        recommender = LLMSupplementRecommender(client)
        request.app.state.supplement_recommender = recommender
        return recommender


def _get_semantic_json_client(request: Request, model: str) -> LLMJsonClient:
    clients = getattr(request.app.state, "semantic_json_clients", None)
    if clients is None:
        clients = {}
        request.app.state.semantic_json_clients = clients
    client = clients.get(model)
    if client is not None:
        return client
    with _organization_state_lock:
        client = clients.get(model)
        if client is not None:
            return client
        client = LLMJsonClient(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=model,
            disable_thinking=settings.llm_disable_thinking,
            timeout=settings.organization_model_timeout,
            recorder=DatabaseModelCallRecorder(get_session_factory()),
        )
        clients[model] = client
        return client


def _llm_configured() -> bool:
    return all(
        value.strip()
        for value in (
            settings.llm_api_key,
            settings.llm_base_url,
            settings.llm_model,
        )
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="knowledge resource not found")


def _mark_organization_run_failed(
    session_factory, *, course_id: str, run_id: str, message: str
) -> None:
    """用全新会话标记 run 失败。

    主流程会话在失败时可能已处于失效事务（PendingRollbackError），若复用
    该会话写失败状态会静默失败，导致 run 永远停留在 running。
    """
    try:
        session = session_factory()
        try:
            updated = session.execute(
                knowledge_publish_service.organization_runs.update()
                .where(
                    knowledge_publish_service.organization_runs.c.id == run_id,
                    knowledge_publish_service.organization_runs.c.course_id == course_id,
                )
                .values(
                    status="failed",
                    error_code="organization_invariant_error",
                    error_message=message,
                )
            )
            if updated.rowcount == 0:
                # run 行尚未落库（失败发生在 create_organization_state 插入行
                # 之前，如框架/材料校验的竞态）：UPDATE 静默落空会让前端对已
                # 发放的 run_id 永远轮询 404，因此补插一条 failed 记录兜底。
                session.execute(
                    knowledge_publish_service.organization_runs.insert().values(
                        id=run_id,
                        course_id=course_id,
                        status="failed",
                        error_code="organization_invariant_error",
                        error_message=message,
                        input_snapshot={},
                    )
                )
            session.commit()
        finally:
            session.close()
    except Exception:
        _logger.exception("failed to mark organization run %s failed", run_id)


def _run_organization_pipeline(
    *,
    course_id: str,
    run_id: str,
    material_version_ids: list[str],
    embedder: EmbeddingClient,
    extractor: LLMKnowledgePointExtractor,
    classifier: ExamPointEvidenceClassifier,
    consolidator: ExamPointKnowledgeConsolidator,
    session_factory,
) -> None:
    """后台执行知识目录构建；失败时把 run 标记为 failed，不影响 HTTP 响应。"""
    _logger.info("run %s started: course=%s materials=%d", run_id, course_id, len(material_version_ids))
    started = time.monotonic()
    session = session_factory()
    try:
        state = knowledge_publish_service.create_organization_state(
            session,
            course_id=course_id,
            material_version_ids=material_version_ids,
            embedder=embedder,
            run_id=run_id,
        )
        _logger.info(
            "run %s state ready: exam_points=%d evidence_chunks=%d",
            run_id,
            len(state["exam_points"]),
            len(state["evidence_chunk_ids"]),
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
            extractor=extractor,
            embedder=embedder,
            checkpointer=InMemorySaver(),
        )
        graph.invoke(state, {"configurable": {"thread_id": state["run_id"]}})
        _logger.info("run %s pipeline finished in %.1fs", run_id, time.monotonic() - started)
    except knowledge_publish_service.KnowledgePublishError as exc:
        _logger.error("run %s failed (publish error): %s", run_id, str(exc)[:500])
        _mark_organization_run_failed(session_factory, course_id=course_id, run_id=run_id, message=str(exc)[:500])
    except Exception as exc:
        message = "knowledge organization stopped because an invariant failed"
        if str(exc).strip():
            message = f"{message}: {str(exc)[:300]}"
        _logger.exception("run %s failed", run_id)
        _mark_organization_run_failed(session_factory, course_id=course_id, run_id=run_id, message=message)
    finally:
        session.close()


@router.post("/organization-runs", status_code=status.HTTP_202_ACCEPTED)
def create_organization_run(
    course_id: str,
    payload: OrganizationRunCreate,
    session: Session = Depends(get_session),
    embedder: EmbeddingClient = Depends(get_organization_embedder),
    extractor: LLMKnowledgePointExtractor = Depends(get_knowledge_point_extractor),
    classifier: ExamPointEvidenceClassifier = Depends(get_exam_point_classifier),
    consolidator: ExamPointKnowledgeConsolidator = Depends(get_exam_point_consolidator),
) -> dict:
    # 轻量前置校验快速失败；耗时（嵌入、检索、分类、归并、建卡）全部在后台执行，
    # HTTP 立即返回，避免长时间占用连接被网关按 504 掐断、前端误判为启动失败。
    try:
        knowledge_publish_service.validate_organization_run_inputs(
            session,
            course_id=course_id,
            material_version_ids=payload.material_version_ids,
        )
    except course_service.CourseNotFoundError:
        raise _not_found()
    except knowledge_publish_service.KnowledgePublishError as exc:
        if "embedding" in str(exc).casefold():
            raise HTTPException(status_code=503, detail="embedding service is unavailable")
        raise HTTPException(status_code=422, detail=str(exc))
    run_id = uuid4().hex
    _logger.info("run %s queued: course=%s materials=%d", run_id, course_id, len(payload.material_version_ids))
    thread = threading.Thread(
        target=_run_organization_pipeline,
        kwargs={
            "course_id": course_id,
            "run_id": run_id,
            "material_version_ids": payload.material_version_ids,
            "embedder": embedder,
            "extractor": extractor,
            "classifier": classifier,
            "consolidator": consolidator,
            "session_factory": get_session_factory(),
        },
        daemon=True,
    )
    thread.start()
    return {"run_id": run_id, "candidate_id": None, "status": "queued"}


@router.get("/organization-runs/latest")
def get_latest_run(course_id: str, session: Session = Depends(get_session)) -> dict:
    from sqlalchemy import select

    row = session.execute(
        select(knowledge_publish_service.organization_runs)
        .where(knowledge_publish_service.organization_runs.c.course_id == course_id)
        .order_by(knowledge_publish_service.organization_runs.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if row is None:
        raise _not_found()
    result = dict(row)
    result["run_id"] = result.get("id")
    return result


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


class SupplementRecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_point_code: str = Field(min_length=1)


@router.post("/organization-runs/{run_id}/supplement-recommendations")
def recommend_supplements(
    course_id: str,
    run_id: str,
    body: SupplementRecommendationRequest,
    session: Session = Depends(get_session),
    recommender: LLMSupplementRecommender = Depends(get_supplement_recommender),
) -> dict:
    """AI 推荐某覆盖不足考点可改判为直接证据的间接证据。

    模型只做预选建议；改判仍由教师在发布确认时提交，不绕过教师确认权。
    推荐失败降级为空推荐列表（HTTP 200，recommended 为空数组），
    教师仍可手动挑选，不阻塞补证据流程。
    """
    try:
        candidate = knowledge_publish_service.get_organization_candidate(
            session, course_id=course_id, run_id=run_id
        )
    except knowledge_publish_service.KnowledgePublishError:
        raise _not_found()

    payload = candidate["payload"]
    coverage = {
        item.get("exam_point_code"): item
        for item in payload.get("coverage") or []
    }
    target = coverage.get(body.exam_point_code)
    if target is None:
        raise HTTPException(status_code=404, detail="exam point is not in this candidate")
    if target.get("status") == "sufficient":
        return {
            "exam_point_code": body.exam_point_code,
            "recommended": [],
            "note": "该考点覆盖已充足，无需补充",
        }

    from sqlalchemy import select as _sa_select

    from app.db.schema import exam_points as exam_points_table
    from app.domain.framework.exam_points import ExamPoint

    point_row = session.execute(
        _sa_select(exam_points_table)
        .where(
            exam_points_table.c.course_id == course_id,
            exam_points_table.c.code == body.exam_point_code,
        )
        .limit(1)
    ).mappings().first()
    if point_row is None:
        raise HTTPException(status_code=404, detail="exam point not found")
    point = ExamPoint.model_validate(dict(point_row))

    # 候选 = 该考点的 supporting/background 链接 + 对应 chunk 原文。
    from app.db.schema import evidence_chunks as chunks_table

    sources = {
        item.get("evidence_chunk_id"): item
        for item in payload.get("evidence_sources") or []
        if item.get("exam_point_code") == body.exam_point_code
        and item.get("relevance_class") in ("supporting", "background")
    }
    candidates: list[dict] = []
    if sources:
        rows = session.execute(
            _sa_select(
                chunks_table.c.id,
                chunks_table.c.content,
            ).where(
                chunks_table.c.course_id == course_id,
                chunks_table.c.id.in_(list(sources)),
            )
        ).mappings()
        content_by_id = {row["id"]: row["content"] for row in rows}
        for chunk_id, item in sources.items():
            candidates.append(
                {
                    "evidence_chunk_id": chunk_id,
                    "relevance_class": item.get("relevance_class"),
                    "support_claim": item.get("support_claim"),
                    "confidence": item.get("confidence"),
                    "content": content_by_id.get(chunk_id, ""),
                }
            )
    if not candidates:
        return {
            "exam_point_code": body.exam_point_code,
            "recommended": [],
            "note": "该考点暂无可补充的间接证据",
        }

    try:
        recommended = recommender.recommend(exam_point=point, candidates=candidates)
    except LLMModelError as exc:
        _logger.warning("supplement recommendation failed for %s: %s", body.exam_point_code, exc)
        recommended = []
    return {
        "exam_point_code": body.exam_point_code,
        "point_title": point.title,
        "recommended": recommended,
        "candidate_count": len(candidates),
    }


@router.post("/organization-runs/{run_id}/publish")
def publish_tree(
    course_id: str,
    run_id: str,
    confirmation: KnowledgeTreeConfirmation,
    session: Session = Depends(get_session),
    recommender: LLMSupplementRecommender = Depends(get_supplement_recommender),
) -> dict:
    try:
        candidate = knowledge_publish_service.get_organization_candidate(session, course_id=course_id, run_id=run_id)
        confirmation = _apply_auto_supplement(
            course_id=course_id,
            run_id=run_id,
            candidate=candidate,
            confirmation=confirmation,
            session=session,
            recommender=recommender,
        )
        result = knowledge_publish_service.DatabaseKnowledgeRepository(session).publish({"course_id": course_id, "run_id": run_id, "candidate_id": candidate["id"]}, KnowledgeTreeCandidate.model_validate(candidate["payload"]), confirmation)
        _logger.info("publish ok: course=%s run=%s", course_id, run_id)
        return result
    except knowledge_publish_service.KnowledgePublishError as exc:
        _logger.error(
            "publish failed: course=%s run=%s detail=%s",
            course_id,
            run_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc))


def _apply_auto_supplement(
    *,
    course_id: str,
    run_id: str,
    candidate: dict,
    confirmation: KnowledgeTreeConfirmation,
    session: Session,
    recommender: LLMSupplementRecommender,
) -> KnowledgeTreeConfirmation:
    """一键补证据：为所有覆盖不足考点自动应用 AI 推荐的直接证据改判。

    仅当 confirmation.auto_supplement_direct_evidence 为真时生效。按考点情况
    分三类自动处理，彻底解放教师：
      A. "缺直接证据"且无失败原因、有间接候选 → 调推荐器生成补充操作；
      B. "有失败残因但因已有活跃卡链而可发布" → 由发布端的补证据覆盖重算
         去 fail 原因救回，本函数不动；
      C. "有失败残因且树内无活跃卡链"（归并/分类失败且内容未产出卡）→
         补证据无法重建，自动并入 teacher_exclusions 排除。
    单点推荐失败或无可推荐条目时跳过，不阻塞其余考点。自动补充/排除过的
    考点并入 reviewed_exam_point_codes，满足发布审阅要求。
    """
    if not confirmation.auto_supplement_direct_evidence:
        return confirmation
    payload = candidate["payload"] or {}
    sources_by_point: dict[str, list[dict]] = {}
    for item in payload.get("evidence_sources") or []:
        code = item.get("exam_point_code")
        if code and item.get("evidence_chunk_id"):
            sources_by_point.setdefault(code, []).append(item)
    coverage_by_code = {
        item.get("exam_point_code"): item for item in payload.get("coverage") or []
    }
    # 树内活跃 topic-unit-card 链（用于区分 B 类可救回 vs C 类真无卡）。
    active_card_chains = {
        unit.get("exam_point_code")
        for topic in payload.get("topics") or []
        if topic.get("status") == "active"
        for unit in topic.get("units") or []
        if unit.get("status") == "active"
        and unit.get("exam_point_code")
        and any(card.get("status") == "active" for card in unit.get("cards") or [])
    }
    def _has_supporting(code: str) -> bool:
        return any(
            str(item.get("relevance_class") or "") == "supporting"
            for item in sources_by_point.get(code, [])
        )

    # A 类：靠补证据能解决的考点（缺直接证据、无 *_failed 阻断、有 supporting 候选）。
    # 注意必须判定 supporting 而非"存在任意候选"：若考点只有 background 候选，
    # 候选过滤后为空，推荐器必然返回空，考点既补不上也不会被排除，掉进缝隙拦发布。
    targets = sorted(
        code
        for code, cov in coverage_by_code.items()
        if cov.get("status") != "sufficient"
        and "no_direct_evidence" in (cov.get("reasons") or [])
        and not any(
            str(reason).endswith("_failed")
            for reason in (cov.get("reasons") or [])
        )
        and _has_supporting(code)
    )
    # C 类：补证据无法解决 且 树内无活跃卡链 → 自动排除。
    # 无法解决有两种情况：
    #   1) *_failed 残因（归并/分类失败，补证据重建不了卡片链路）
    #   2) 无 supporting 候选（材料里没有可升级为直接证据的间接证据，
    #      补证据无从下手；无卡链说明知识也未落地，只能排除）
    auto_exclusions = {
        code
        for code, cov in coverage_by_code.items()
        if cov.get("status") != "sufficient"
        and code not in active_card_chains
        and (
            any(str(reason).endswith("_failed") for reason in (cov.get("reasons") or []))
            or not _has_supporting(code)
        )
    }
    # B 类：failed 残因但已有活跃卡链 → 不排除，交由发布端救回；
    # 救回后转 sufficient，必须并入已审集合，否则被"requires teacher review"拦下。
    rescue_candidates = {
        code
        for code, cov in coverage_by_code.items()
        if cov.get("status") != "sufficient"
        and any(str(reason).endswith("_failed") for reason in (cov.get("reasons") or []))
        and code in active_card_chains
    }
    if not targets and not auto_exclusions and not rescue_candidates:
        return confirmation
    _logger.info(
        "auto supplement: course=%s run=%s targets(A)=%s auto_exclusions(C)=%s "
        "rescue_candidates(B)=%s",
        course_id,
        run_id,
        targets,
        sorted(auto_exclusions),
        sorted(rescue_candidates),
    )

    from sqlalchemy import select as _sa_select

    from app.db.schema import exam_points as exam_points_table
    from app.domain.framework.exam_points import ExamPoint

    point_rows = session.execute(
        _sa_select(exam_points_table).where(
            exam_points_table.c.course_id == course_id,
            exam_points_table.c.code.in_(targets),
        )
    ).mappings()
    points_by_code = {
        row["code"]: ExamPoint.model_validate(dict(row)) for row in point_rows
    }

    # 保留教师已提交的操作（手填补证据、排除、改名等），只追加自动推荐。
    existing = [
        op
        for op in confirmation.operations
        if not (
            op.operation == "supplement_direct_evidence"
            and op.target_code.strip()
            and (op.value or "").strip()
        )
    ]
    added_by_point: dict[str, set[str]] = {}
    for code in targets:
        point = points_by_code.get(code)
        if point is None:
            continue
        # 仅 supporting 可作为改判直接证据的候选：direct 已成为直接证据，
        # 而 background 按定义不含可考核知识（其 support_claim 是"（未提供说明）"
        # 占位符），改判后卡片将无支撑事实，触发发布质量闸。只下发可落地候选，
        # 避免把无意义背景喂给推荐器、污染补证据选项。
        candidates = [
            {
                "evidence_chunk_id": item["evidence_chunk_id"],
                "relevance_class": item.get("relevance_class"),
                "support_claim": item.get("support_claim"),
                "confidence": item.get("confidence"),
                "content": item.get("content"),
            }
            for item in sources_by_point[code]
            if str(item.get("relevance_class") or "") == "supporting"
        ]
        try:
            recommended = recommender.recommend(exam_point=point, candidates=candidates)
        except LLMModelError as exc:
            _logger.warning(
                "auto supplement recommendation failed for %s: %s", code, exc
            )
            continue
        _logger.info(
            "auto supplement recommend: course=%s run=%s point=%s candidates=%d "
            "accepted=%s",
            course_id,
            run_id,
            code,
            len(candidates),
            sorted(str(item.get("evidence_chunk_id") or "") for item in recommended),
        )
        if not recommended:
            # 推荐器对 A 类考点 0 采纳：改判后仍拿不到可考核（answer/rubric 依据）
            # 的直接证据，等同于"无 supporting 候选"的材料覆盖缺口，自动排除，
            # 避免该考点既补不上又在 answer/rubric 闸处永久拦死发布。
            auto_exclusions.add(code)
            _logger.info(
                "auto supplement no adoption for %s -> auto exclude course=%s run=%s",
                code,
                course_id,
                run_id,
            )
            continue
        for item in recommended:
            chunk_id = str(item.get("evidence_chunk_id") or "").strip()
            if not chunk_id or chunk_id in added_by_point.setdefault(code, set()):
                continue
            existing.append(
                TreeOperation(
                    operation="supplement_direct_evidence",
                    target_code=code,
                    value=chunk_id,
                )
            )
            added_by_point[code].add(chunk_id)
    auto_reviewed = sorted({*added_by_point.keys(), *auto_exclusions, *rescue_candidates})
    if not auto_reviewed:
        return confirmation
    _logger.info(
        "auto supplement applied: course=%s run=%s added_by_point=%s exclusions=%s",
        course_id,
        run_id,
        {code: sorted(chunks) for code, chunks in added_by_point.items()},
        sorted(auto_exclusions),
    )
    # 自动排除的无卡考点并入 teacher_exclusions，teacher_exclusions 已包含
    # 教师手填排除，并集去重后一并返回；排除后的考点不再要求覆盖/审阅。
    new_exclusions = sorted(set(confirmation.teacher_exclusions) | auto_exclusions)
    return confirmation.model_copy(
        update={
            "operations": existing,
            "teacher_exclusions": new_exclusions,
            "reviewed_exam_point_codes": list(
                dict.fromkeys(
                    [*confirmation.reviewed_exam_point_codes, *auto_reviewed]
                )
            ),
        }
    )


@router.post("/organization-runs/{run_id}/reject")
def reject_tree(course_id: str, run_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return knowledge_publish_service.reject_organization_run(session, course_id=course_id, run_id=run_id)
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
        return {"published": False, "knowledge_cards": {}, "assessment_units": [], "content_domains": [], "exam_points": [], "units": []}
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
                knowledge_evidence_links.c.course_id == course_id,
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
            knowledge_evidence_links.c.course_id == course_id,
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
