"""统一助手历史摘要入口（本人课程会话 + 共享库问答）。

只读聚合，不返回消息正文；详情、提问与执行仍走各自领域端点重新授权。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.db.schema import User
from app.db.session import get_session
from app.services import assistant_history_service
from app.services.knowledge_space_service import KnowledgeError

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant-history"])


@router.get("/sessions")
def list_assistant_sessions(
    domain: str | None = Query(default=None, pattern="^(course|knowledge)$"),
    course_id: str | None = Query(default=None),
    limit: int = Query(default=assistant_history_service.DEFAULT_LIMIT, ge=1, le=50),
    cursor: str | None = Query(default=None),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """跨课程与知识域的本人会话摘要，按更新时间游标分页。"""

    try:
        return assistant_history_service.list_history(
            session,
            current_user,
            domain=domain,
            course_id=course_id,
            limit=limit,
            cursor=cursor,
        )
    except KnowledgeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
