"""本人助手会话摘要聚合：课程助手 + 共享库问答。

只返回标题、领域、可展示目标与原生会话引用，不返回消息正文；每个领域仍由
原生入口重新授权。管理员身份不扩大结果。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Course,
    User,
    assistant_sessions,
    knowledge_sessions,
)
from app.services.knowledge_space_service import (
    KnowledgeError,
    _aware,
    list_spaces,
    load_actor,
)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _encode_cursor(item: dict) -> str:
    raw = json.dumps(
        {
            "updated_at": item["updated_at"],
            "domain": item["domain"],
            "id": item["session_id"],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        updated_at = datetime.fromisoformat(str(data["updated_at"]))
        domain = str(data["domain"])
        session_id = str(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "分页游标无效") from exc
    if domain not in {"course", "knowledge"} or not session_id:
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "分页游标无效")
    return {"updated_at": _aware(updated_at), "domain": domain, "id": session_id}


def _after_cursor(updated_at_col, id_col, company: str, cursor: dict):
    """按 (updated_at desc, domain asc, id desc) 生成某一侧的续页条件。"""

    ts = cursor["updated_at"]
    cid = cursor["id"]
    if company == cursor["domain"]:
        return or_(updated_at_col < ts, and_(updated_at_col == ts, id_col < cid))
    # domain 排序：course 在 knowledge 之前，所以同时间戳下知识域续页可取等号。
    return updated_at_col < ts if company == "course" else updated_at_col <= ts


def list_history(
    session: Session,
    user: User,
    *,
    domain: str | None = None,
    course_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict:
    if domain not in {None, "course", "knowledge"}:
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "不支持的领域筛选")
    if course_id and domain == "knowledge":
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "知识会话不支持课程筛选")
    if limit < 1 or limit > MAX_LIMIT:
        raise KnowledgeError(422, "FILE_VALIDATION_FAILED", "分页大小超出范围")
    cursor_data = _decode_cursor(cursor) if cursor else None

    items: list[dict] = []
    if domain in {None, "course"}:
        items.extend(
            _course_items(
                session,
                user,
                course_id=course_id,
                limit=limit + 1,
                cursor=cursor_data,
            )
        )
    if domain in {None, "knowledge"} and not course_id:
        items.extend(
            _knowledge_items(session, user, limit=limit + 1, cursor=cursor_data)
        )

    # 稳定排序：updated_at desc → domain asc → session_id desc
    items.sort(key=lambda item: item["session_id"], reverse=True)
    items.sort(key=lambda item: 0 if item["domain"] == "course" else 1)
    items.sort(key=lambda item: item["_updated_at"], reverse=True)

    has_more = len(items) > limit
    page = items[:limit]
    for item in page:
        item.pop("_updated_at", None)
    return {
        "items": page,
        "next_cursor": _encode_cursor(items[limit - 1]) if has_more else None,
    }


def _course_items(
    session: Session,
    user: User,
    *,
    course_id: str | None,
    limit: int,
    cursor: dict | None,
) -> list[dict]:
    statement = (
        select(
            assistant_sessions.c.id,
            assistant_sessions.c.title,
            assistant_sessions.c.updated_at,
            Course.id.label("course_id"),
            Course.name.label("course_name"),
        )
        .join(Course, Course.id == assistant_sessions.c.course_id)
        .where(Course.owner_id == user.id)
    )
    if course_id:
        # 非法课程不成为读取他人会话的通道；与课程接口一致返回 404。
        owned = session.scalar(
            select(Course.id).where(Course.id == course_id, Course.owner_id == user.id)
        )
        if owned is None:
            raise KnowledgeError(404, "RESOURCE_NOT_FOUND", "resource not found")
        statement = statement.where(assistant_sessions.c.course_id == course_id)
    if cursor is not None:
        statement = statement.where(
            _after_cursor(assistant_sessions.c.updated_at, assistant_sessions.c.id, "course", cursor)
        )
    rows = session.execute(
        statement.order_by(
            assistant_sessions.c.updated_at.desc(), assistant_sessions.c.id.desc()
        ).limit(limit)
    ).mappings()
    return [
        {
            "domain": "course",
            "session_id": row["id"],
            "title": row["title"],
            "target": {
                "kind": "course",
                "course_id": row["course_id"],
                "name": row["course_name"],
            },
            "updated_at": _aware(row["updated_at"]).isoformat(),
            "_updated_at": _aware(row["updated_at"]),
        }
        for row in rows
    ]


def _knowledge_items(
    session: Session, user: User, *, limit: int, cursor: dict | None
) -> list[dict]:
    spaces = {space["id"]: space["name"] for space in list_spaces(session, user)}
    actor = load_actor(session, user)
    if not actor.is_active or not actor.has_organization or not actor.school_id:
        return []
    statement = select(
        knowledge_sessions.c.id,
        knowledge_sessions.c.title,
        knowledge_sessions.c.selected_space_ids,
        knowledge_sessions.c.updated_at,
    ).where(
        knowledge_sessions.c.school_id == actor.school_id,
        knowledge_sessions.c.owner_id == user.id,
        knowledge_sessions.c.status == "active",
    )
    if cursor is not None:
        statement = statement.where(
            _after_cursor(
                knowledge_sessions.c.updated_at, knowledge_sessions.c.id, "knowledge", cursor
            )
        )
    rows = session.execute(
        statement.order_by(
            knowledge_sessions.c.updated_at.desc(), knowledge_sessions.c.id.desc()
        ).limit(limit)
    ).mappings()
    items: list[dict] = []
    for row in rows:
        selected = [str(item) for item in (row["selected_space_ids"] or [])]
        readable = [space_id for space_id in selected if space_id in spaces]
        items.append(
            {
                "domain": "knowledge",
                "session_id": row["id"],
                "title": row["title"],
                "target": {
                    "kind": "knowledge",
                    "space_ids": readable,
                    "names": [spaces[space_id] for space_id in readable],
                    "complete": len(readable) == len(selected),
                },
                "updated_at": _aware(row["updated_at"]).isoformat(),
                "_updated_at": _aware(row["updated_at"]),
            }
        )
    return items
