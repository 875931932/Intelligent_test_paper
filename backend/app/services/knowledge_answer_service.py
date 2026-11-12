"""知识域个人问答：会话、证据约束回答与历史重新授权。

v1 不缓存授权，也不缓存最终答案：每次读取会话都重新校验依赖版本是否仍
可读；任一依赖撤权时整条答案隐藏（而不是只移除引用）。当前回答为抽取式
证据摘要，明确标注 ``channel=extractive``；模型问答接入后沿用同一来源
与依赖结构。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    User,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_messages,
    knowledge_sessions,
)
from app.services.knowledge_search_service import search
from app.services.knowledge_space_service import (
    KnowledgeError,
    _aware,
    load_actor,
    require_space_access,
)

INSUFFICIENT_TEXT = "未在可访问的资料中找到足够证据回答该问题。可以换一种说法，或确认所选知识库是否包含相关资料。"
MAX_HISTORY_MESSAGES = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_row(session: Session, *, school_id: str, owner_id: str, session_id: str):
    return session.execute(
        select(knowledge_sessions).where(
            knowledge_sessions.c.id == session_id,
            knowledge_sessions.c.school_id == school_id,
            knowledge_sessions.c.owner_id == owner_id,
            knowledge_sessions.c.status == "active",
        )
    ).mappings().one_or_none()


def _authorized_space_ids(session: Session, actor, space_ids: list[str]) -> list[str]:
    allowed: list[str] = []
    for space_id in dict.fromkeys(space_ids):
        try:
            require_space_access(session, actor, space_id=space_id, action="read")
        except KnowledgeError:
            continue
        allowed.append(space_id)
    return allowed


def create_session(session: Session, user: User, *, selected_space_ids: list[str], title: str | None = None) -> dict:
    actor = load_actor(session, user)
    if not actor.is_active or not actor.has_organization:
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
    allowed = _authorized_space_ids(session, actor, selected_space_ids)
    if not allowed:
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
    session_id = uuid4().hex
    now = _now()
    session.execute(
        knowledge_sessions.insert().values(
            id=session_id,
            school_id=actor.school_id,
            owner_id=actor.user_id,
            kind="conversation",
            title=(title or "").strip() or "新会话",
            selected_space_ids=allowed,
            status="active",
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    return get_session(session, user, session_id)


def _session_view(session: Session, user: User, row) -> dict:
    """读取会话并重新授权历史答案；不返回已被撤权的消息正文。"""

    actor = load_actor(session, user)
    messages = session.execute(
        select(knowledge_messages)
        .where(knowledge_messages.c.session_id == row.id)
        .order_by(knowledge_messages.c.created_at)
    ).mappings().all()

    hidden_ids = []
    for message in messages:
        if message.role != "assistant" or message.state != "complete":
            continue
        dependencies = list(message.dependency_versions or [])
        if dependencies and not _dependencies_readable(session, actor, dependencies):
            hidden_ids.append(message.id)
    if hidden_ids:
        session.execute(
            update(knowledge_messages)
            .where(knowledge_messages.c.id.in_(hidden_ids))
            .values(state="hidden")
        )
        session.commit()

    hidden = set(hidden_ids)
    visible = [
        message for message in messages if message.state not in {"hidden", "cancelled"} and message.id not in hidden
    ]
    return {
        "id": row.id,
        "title": row.title,
        "selected_space_ids": list(row.selected_space_ids or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "state": message.state,
                "sources": list(message.sources or []),
                "dependency_versions": list(message.dependency_versions or []),
                "retrieval_snapshot": dict(message.retrieval_snapshot or {}),
                "feedback": dict(message.feedback or {}),
                "created_at": message.created_at.isoformat() if message.created_at else None,
            }
            for message in visible
        ],
    }


def _dependencies_readable(session: Session, actor, version_ids: list[str]) -> bool:
    versions = session.execute(
        select(knowledge_document_versions).where(
            knowledge_document_versions.c.id.in_(version_ids),
            knowledge_document_versions.c.school_id == actor.school_id,
        )
    ).mappings().all()
    if len(versions) != len(set(version_ids)):
        return False
    document_ids = list({version.document_id for version in versions})
    documents = {
        document.id: document
        for document in session.execute(
            select(knowledge_documents).where(
                knowledge_documents.c.id.in_(document_ids),
                knowledge_documents.c.school_id == actor.school_id,
            )
        ).mappings().all()
    }
    now = datetime.now(timezone.utc)
    for version in versions:
        document = documents.get(version.document_id)
        if document is None:
            return False
        if version.state not in {"published", "superseded"}:
            return False
        if document.status not in {"published", "superseded"}:
            return False
        starts = _aware(version.effective_from)
        ends = _aware(version.effective_to)
        if (starts is not None and starts > now) or (ends is not None and ends <= now):
            return False
        try:
            require_space_access(session, actor, space_id=version.space_id, action="read")
        except KnowledgeError:
            return False
    return True


def list_sessions(session: Session, user: User) -> list[dict]:
    actor = load_actor(session, user)
    if not actor.is_active or not actor.has_organization:
        return []
    rows = session.execute(
        select(knowledge_sessions)
        .where(
            knowledge_sessions.c.school_id == actor.school_id,
            knowledge_sessions.c.owner_id == actor.user_id,
            knowledge_sessions.c.status == "active",
        )
        .order_by(knowledge_sessions.c.updated_at.desc())
        .limit(50)
    ).mappings().all()
    return [
        {
            "id": row.id,
            "title": row.title,
            "selected_space_ids": list(row.selected_space_ids or []),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


def get_session(session: Session, user: User, session_id: str) -> dict:
    actor = load_actor(session, user)
    row = _session_row(
        session, school_id=actor.school_id or "", owner_id=actor.user_id, session_id=session_id
    )
    if row is None:
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
    return _session_view(session, user, row)


def ask(session: Session, user: User, session_id: str, *, question: str) -> dict:
    text = (question or "").strip()
    if not text:
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "问题不能为空")
    actor = load_actor(session, user)
    row = _session_row(
        session, school_id=actor.school_id or "", owner_id=actor.user_id, session_id=session_id
    )
    if row is None:
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")

    space_ids = _authorized_space_ids(session, actor, list(row.selected_space_ids or []))
    now = _now()
    recent = session.scalar(
        select(func.count())
        .select_from(knowledge_messages)
        .join(knowledge_sessions, knowledge_sessions.c.id == knowledge_messages.c.session_id)
        .where(
            knowledge_sessions.c.owner_id == actor.user_id,
            knowledge_messages.c.role == "user",
            knowledge_messages.c.created_at >= now - timedelta(seconds=60),
        )
    )
    if int(recent or 0) >= 12:
        raise KnowledgeError(429, "RATE_LIMITED", "提问过于频繁，请稍后再试")
    session.execute(
        knowledge_messages.insert().values(
            id=uuid4().hex,
            school_id=actor.school_id,
            session_id=session_id,
            role="user",
            content=text,
            state="complete",
            sources=[],
            dependency_versions=[],
            retrieval_snapshot={},
            feedback={},
            created_at=now,
        )
    )
    session.commit()

    result = search(session, user, query=text, space_ids=space_ids, limit=6)
    hits = result["hits"]
    sources = []
    dependency_versions: list[str] = []
    if hits:
        lines = ["根据当前可访问的资料，可以确认以下内容："]
        for index, hit in enumerate(hits, start=1):
            quote = (hit["text"] or "").strip()
            if len(quote) > 240:
                quote = quote[:240] + "…"
            lines.append(f"[{index}] {hit['document_title']}：{quote}")
            sources.append(
                {
                    "source_id": index,
                    "chunk_id": hit["chunk_id"],
                    "space_id": hit["space_id"],
                    "document_id": hit["document_id"],
                    "document_title": hit["document_title"],
                    "version_id": hit["version_id"],
                    "version_no": hit["version_no"],
                    "page_index": hit["page_index"],
                    "heading_path": hit["heading_path"],
                    "quote": quote,
                }
            )
            if hit["version_id"] not in dependency_versions:
                dependency_versions.append(hit["version_id"])
        content = "\n\n".join(lines)
        state = "complete"
        snapshot = {"status": "answered", "channel": "extractive", "hit_count": len(hits)}
    else:
        content = INSUFFICIENT_TEXT
        state = "complete"
        snapshot = {"status": "insufficient_evidence", "channel": "extractive", "hit_count": 0}

    message_id = uuid4().hex
    session.execute(
        knowledge_messages.insert().values(
            id=message_id,
            school_id=actor.school_id,
            session_id=session_id,
            role="assistant",
            content=content,
            state=state,
            sources=sources,
            dependency_versions=dependency_versions,
            retrieval_snapshot=snapshot,
            feedback={},
            created_at=_now(),
        )
    )
    session.execute(
        update(knowledge_sessions)
        .where(knowledge_sessions.c.id == session_id)
        .values(updated_at=_now())
    )
    session.commit()
    return _session_view(session, user, _session_row(
        session, school_id=actor.school_id or "", owner_id=actor.user_id, session_id=session_id
    ))


def submit_feedback(
    session: Session,
    user: User,
    session_id: str,
    message_id: str,
    *,
    rating: str | None = None,
    comment: str | None = None,
    reason: str | None = None,
) -> dict:
    """本人反馈；不向资料维护者默认开放私人对话内容。"""

    actor = load_actor(session, user)
    row = _session_row(
        session, school_id=actor.school_id or "", owner_id=actor.user_id, session_id=session_id
    )
    if row is None:
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
    message = session.execute(
        select(knowledge_messages).where(
            knowledge_messages.c.id == message_id,
            knowledge_messages.c.session_id == session_id,
        )
    ).mappings().one_or_none()
    if message is None or message.role != "assistant":
        raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
    payload = dict(message.feedback or {})
    if rating is not None:
        payload["rating"] = rating
    if comment is not None:
        payload["comment"] = comment[:500]
    if reason is not None:
        payload["reason"] = reason
    payload["updated_at"] = _now().isoformat()
    session.execute(
        update(knowledge_messages)
        .where(knowledge_messages.c.id == message_id)
        .values(feedback=payload)
    )
    session.commit()
    return {"message_id": message_id, "feedback": payload}
