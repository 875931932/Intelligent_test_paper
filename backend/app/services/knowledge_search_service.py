"""知识域授权检索：词面召回 + 预留向量融合。

P2.1 的数据库召回：候选集合先由**授权与发布状态**限定（学校、空间、当前
有效发布版本、serving build），再只取这些版本的 chunk 正文评分。查询不再
装载整个知识库正文，也不会把无权空间的片段交给模型。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.retrieval.tokenization import query_token_coverage
from app.config import settings
from app.db.schema import (
    User,
    knowledge_blocks,
    knowledge_chunks,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_index_builds,
    knowledge_parse_runs,
    knowledge_retrieval_profiles,
)
from app.domain.knowledge_base.models import INDEX_NOT_READY
from app.services.knowledge_space_service import (
    KnowledgeError,
    _aware,
    load_actor,
    require_space_access,
)

TOKENIZER_VERSION = "cjk-ngram-v2"
MAX_SPACES_PER_QUERY = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_retrieval_profile(session: Session, *, school_id: str) -> dict:
    """每校一个 active 检索配置；未配置嵌入模型时明确标注 lexical-only。"""

    row = session.execute(
        select(knowledge_retrieval_profiles).where(
            knowledge_retrieval_profiles.c.school_id == school_id,
            knowledge_retrieval_profiles.c.state == "active",
        )
    ).mappings().one_or_none()
    if row is not None:
        return dict(row)

    version_no = session.scalar(
        select(func.coalesce(func.max(knowledge_retrieval_profiles.c.version_no), 0) + 1).where(
            knowledge_retrieval_profiles.c.school_id == school_id
        )
    )
    configured = bool(settings.embedding_base_url and settings.embedding_model)
    profile_id = uuid4().hex
    values = {
        "id": profile_id,
        "school_id": school_id,
        "version_no": int(version_no),
        "embedding_config_ref": settings.embedding_base_url if configured else "lexical-only",
        "embedding_model_id": settings.embedding_model if configured else "lexical-only",
        # 未配置嵌入时保留占位维度，只用于元数据；词面通道不依赖它。
        "embedding_dim": 1 if not configured else 1024,
        "tokenizer_version": TOKENIZER_VERSION,
        "chunking_config": {"mode": "block", "source": "knowledge_blocks"},
        "state": "active",
        "created_at": _now(),
    }
    try:
        session.execute(knowledge_retrieval_profiles.insert().values(**values))
        session.commit()
    except IntegrityError:
        session.rollback()
        row = session.execute(
            select(knowledge_retrieval_profiles).where(
                knowledge_retrieval_profiles.c.school_id == school_id,
                knowledge_retrieval_profiles.c.state == "active",
            )
        ).mappings().one()
        return dict(row)
    return values


def ensure_index_build(
    session: Session, *, school_id: str, space_id: str, version_id: str
) -> dict:
    """为已解析版本建立索引（块即 chunk）；重复调用返回现存 build。"""

    version = session.execute(
        select(knowledge_document_versions).where(
            knowledge_document_versions.c.id == version_id,
            knowledge_document_versions.c.school_id == school_id,
            knowledge_document_versions.c.space_id == space_id,
        )
    ).mappings().one_or_none()
    if version is None:
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
    if version.serving_build_id:
        existing = session.execute(
            select(knowledge_index_builds).where(
                knowledge_index_builds.c.id == version.serving_build_id
            )
        ).mappings().one_or_none()
        if existing is not None and existing.state == "ready":
            return dict(existing)

    parse_run = session.execute(
        select(knowledge_parse_runs)
        .where(
            knowledge_parse_runs.c.school_id == school_id,
            knowledge_parse_runs.c.space_id == space_id,
            knowledge_parse_runs.c.version_id == version_id,
            knowledge_parse_runs.c.state == "ready",
        )
        .order_by(knowledge_parse_runs.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if parse_run is None:
        raise KnowledgeError(409, INDEX_NOT_READY, "该版本尚未解析完成")

    blocks = session.execute(
        select(knowledge_blocks)
        .where(
            knowledge_blocks.c.school_id == school_id,
            knowledge_blocks.c.space_id == space_id,
            knowledge_blocks.c.parse_run_id == parse_run.id,
        )
        .order_by(knowledge_blocks.c.block_index)
    ).mappings().all()
    if not blocks:
        raise KnowledgeError(409, INDEX_NOT_READY, "解析结果为空，不能发布")

    profile = ensure_retrieval_profile(session, school_id=school_id)
    now = _now()
    build_id = uuid4().hex
    session.execute(
        knowledge_index_builds.insert().values(
            id=build_id,
            school_id=school_id,
            space_id=space_id,
            version_id=version_id,
            parse_run_id=parse_run.id,
            retrieval_profile_id=profile["id"],
            pipeline_hash=f"{TOKENIZER_VERSION}:block-v1",
            embedding_model_id=profile["embedding_model_id"],
            embedding_dim=int(profile["embedding_dim"]),
            state="ready",
            expected_chunks=len(blocks),
            ready_chunks=len(blocks),
            created_at=now,
            updated_at=now,
        )
    )
    rows = []
    for index, block in enumerate(blocks):
        rows.append(
            {
                "id": uuid4().hex,
                "school_id": school_id,
                "space_id": space_id,
                "build_id": build_id,
                "chunk_index": index,
                "text": block.text or "",
                "source_blocks": [
                    {
                        "block_id": block.id,
                        "block_index": block.block_index,
                        "page_index": block.page_index,
                        "heading_path": list(block.heading_path or []),
                    }
                ],
                # 词面通道当前在服务层对授权候选打分；该列保留预分词文本，
                # 供 PostgreSQL GIN 表达式索引（init_db 已建）使用。
                "lexical_tsv": " ".join((block.text or "").split()),
                "content_hash": "",
                "embedding": None,
                "embedding_model_id": None,
                "embedding_dim": None,
            }
        )
    if rows:
        session.execute(knowledge_chunks.insert(), rows)
    session.execute(
        knowledge_document_versions.update()
        .where(knowledge_document_versions.c.id == version_id)
        .values(serving_build_id=build_id)
    )
    session.commit()
    return {
        "id": build_id,
        "state": "ready",
        "expected_chunks": len(blocks),
        "ready_chunks": len(blocks),
    }


# ── 检索 ────────────────────────────────────────────────────────────────────


def _authorized_versions(session: Session, actor, space_ids: list[str]) -> list[dict]:
    """只取当前账号可读空间的当前有效发布版本。"""

    versions = session.execute(
        select(
            knowledge_document_versions,
            knowledge_documents.c.id.label("doc_id"),
            knowledge_documents.c.title,
            knowledge_documents.c.classification,
        )
        .join(
            knowledge_documents,
            (knowledge_documents.c.id == knowledge_document_versions.c.document_id)
            & (knowledge_documents.c.school_id == knowledge_document_versions.c.school_id)
            & (knowledge_documents.c.space_id == knowledge_document_versions.c.space_id),
        )
        .where(
            knowledge_document_versions.c.school_id == actor.school_id,
            knowledge_document_versions.c.space_id.in_(space_ids),
            knowledge_documents.c.status == "published",
            knowledge_documents.c.published_version_id == knowledge_document_versions.c.id,
            knowledge_document_versions.c.state == "published",
        )
    ).mappings().all()
    now = datetime.now(timezone.utc)
    result = []
    for row in versions:
        starts = _aware(row.effective_from)
        ends = _aware(row.effective_to)
        if (starts is not None and starts > now) or (ends is not None and ends <= now):
            continue
        result.append(dict(row))
    return result


def search(
    session: Session,
    user: User,
    *,
    query: str,
    space_ids: list[str],
    limit: int = 8,
) -> dict:
    """在授权空间内做词面召回；返回带固定版本与 block 定位的来源。"""

    text = (query or "").strip()
    if not text:
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "查询不能为空")
    if not space_ids:
        return {"query": text, "hits": [], "searched_spaces": []}
    unique_space_ids = list(dict.fromkeys(space_ids))[:MAX_SPACES_PER_QUERY]
    actor = load_actor(session, user)
    if not actor.is_active or not actor.has_organization:
        return {"query": text, "hits": [], "searched_spaces": []}

    authorized = []
    for space_id in unique_space_ids:
        try:
            require_space_access(session, actor, space_id=space_id, action="read")
        except KnowledgeError:
            # 未授权空间静默排除：不泄露其存在性，也不把其片段交给模型。
            continue
        authorized.append(space_id)
    if not authorized:
        return {"query": text, "hits": [], "searched_spaces": []}

    versions = _authorized_versions(session, actor, authorized)
    if not versions:
        return {"query": text, "hits": [], "searched_spaces": authorized}
    build_ids = [row["serving_build_id"] for row in versions if row["serving_build_id"]]
    if not build_ids:
        return {"query": text, "hits": [], "searched_spaces": authorized}

    chunks = session.execute(
        select(knowledge_chunks).where(
            knowledge_chunks.c.school_id == actor.school_id,
            knowledge_chunks.c.build_id.in_(build_ids),
        )
    ).mappings().all()

    version_by_build = {row["serving_build_id"]: row for row in versions if row["serving_build_id"]}
    scored: list[tuple[float, object]] = []
    for chunk in chunks:
        coverage = query_token_coverage(text, chunk.text or "")
        if coverage <= 0:
            continue
        scored.append((coverage, chunk))
    scored.sort(key=lambda item: (-item[0], item[1].build_id, item[1].chunk_index))
    scored = scored[: max(1, min(limit, 20))]

    hits = []
    for score, chunk in scored:
        version = version_by_build.get(chunk.build_id)
        if version is None:
            continue
        source = (chunk.source_blocks or [{}])[0]
        hits.append(
            {
                "chunk_id": chunk.id,
                "text": chunk.text,
                "score": round(float(score), 4),
                "channel": "lexical",
                "space_id": version["space_id"],
                "document_id": version["doc_id"],
                "document_title": version["title"],
                "version_id": version["id"],
                "version_no": version["version_no"],
                "classification": version["classification"],
                "page_index": source.get("page_index"),
                "heading_path": source.get("heading_path") or [],
                "block_index": source.get("block_index"),
            }
        )
    return {"query": text, "hits": hits, "searched_spaces": authorized}
