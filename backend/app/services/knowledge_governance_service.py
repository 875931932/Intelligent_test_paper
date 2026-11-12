"""知识域治理汇总（P3.1）。

只返回脱敏计数与状态，不返回资料正文、私人问答内容或他人身份明细。
管理身份不会因此获得内容阅读权；``org_admin`` 仅限本校/本院范围。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.schema import (
    User,
    knowledge_chunks,
    knowledge_documents,
    knowledge_document_versions,
    knowledge_index_builds,
    knowledge_jobs,
    knowledge_messages,
    knowledge_parse_runs,
    knowledge_sessions,
    knowledge_spaces,
)
from app.services.knowledge_space_service import KnowledgeError, load_actor


def _forbidden() -> KnowledgeError:
    return KnowledgeError(403, "ACTION_FORBIDDEN", "没有查看治理汇总的权限")


def _org_filter(session: Session, user: User):
    actor = load_actor(session, user)
    if not actor.is_active:
        raise _forbidden()
    if user.role == "admin":
        # 平台管理员看全校汇总，但仍不读取任何资料正文。
        if not actor.school_id:
            raise _forbidden()
        return actor.school_id, None
    if user.role == "org_admin":
        if not actor.school_id:
            raise _forbidden()
        return actor.school_id, actor.college_id or actor.school_id
    raise _forbidden()


def governance_summary(session: Session, user: User) -> dict:
    """本校/本院的空间、资料、解析、任务与反馈状态。"""

    school_id, org_id = _org_filter(session, user)

    space_stmt = select(knowledge_spaces.c.id, knowledge_spaces.c.status).where(
        knowledge_spaces.c.school_id == school_id
    )
    if org_id:
        space_stmt = space_stmt.where(knowledge_spaces.c.org_id == org_id)
    spaces = session.execute(space_stmt).mappings().all()
    space_ids = [row.id for row in spaces]

    def _count(table, *conditions) -> int:
        if not space_ids:
            return 0
        return int(
            session.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.school_id == school_id, table.c.space_id.in_(space_ids), *conditions)
            )
            or 0
        )

    documents_total = _count(knowledge_documents)
    documents_published = _count(knowledge_documents, knowledge_documents.c.status == "published")
    versions_total = _count(knowledge_document_versions)
    parse_failed = _count(knowledge_parse_runs, knowledge_parse_runs.c.state == "failed")
    index_ready = _count(knowledge_index_builds, knowledge_index_builds.c.state == "ready")
    chunks_total = _count(knowledge_chunks)

    jobs_by_state: dict[str, int] = {}
    if space_ids:
        for state, count in session.execute(
            select(knowledge_jobs.c.state, func.count())
            .where(
                knowledge_jobs.c.school_id == school_id,
                knowledge_jobs.c.space_id.in_(space_ids),
            )
            .group_by(knowledge_jobs.c.state)
        ):
            jobs_by_state[str(state)] = int(count)

    # 反馈只做聚合，不返回具体会话或提问内容；仅统计选定空间内的会话。
    feedback: dict[str, int] = {}
    session_ids: list[str] = []
    if space_ids:
        session_rows = session.execute(
            select(knowledge_sessions.c.id, knowledge_sessions.c.selected_space_ids)
            .where(knowledge_sessions.c.school_id == school_id)
            .order_by(knowledge_sessions.c.updated_at.desc())
            .limit(1000)
        ).mappings().all()
        allowed = set(space_ids)
        session_ids = [
            row.id
            for row in session_rows
            if allowed.intersection(row.selected_space_ids or [])
        ]
    if session_ids:
        rows = session.execute(
            select(knowledge_messages.c.feedback)
            .where(
                knowledge_messages.c.school_id == school_id,
                knowledge_messages.c.session_id.in_(session_ids),
            )
            .order_by(knowledge_messages.c.created_at.desc())
            .limit(1000)
        ).scalars().all()
        for value in rows:
            if not isinstance(value, dict) or not value:
                continue
            rating = value.get("rating") or "commented"
            feedback[str(rating)] = feedback.get(str(rating), 0) + 1

    return {
        "scope": {"school_id": school_id, "org_id": org_id},
        "spaces": {
            "total": len(spaces),
            "active": sum(1 for row in spaces if row.status == "active"),
            "archived": sum(1 for row in spaces if row.status != "active"),
        },
        "documents": {"total": documents_total, "published": documents_published},
        "versions": {"total": versions_total},
        "parsing": {"failed": parse_failed},
        "index": {"ready_builds": index_ready, "chunks": chunks_total},
        "jobs": jobs_by_state,
        "feedback": feedback,
        "model_cost": {
            "calls": 0,
            "spend": "unknown",
            "note": "本期问答为抽取式证据摘要，未产生模型调用费用。",
        },
    }
