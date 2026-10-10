"""后台概览：账号/组织/模型/调用的核心统计（仅管理员）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.admin.deps import require_admin
from app.db.schema import Organization, User, model_calls
from app.db.session import get_session

router = APIRouter(prefix="/overview", tags=["admin-overview"], dependencies=[Depends(require_admin)])


@router.get("")
def overview(session: Session = Depends(get_session)) -> dict:
    """概览统计：账号按角色/状态计数、组织计数、近 24h 调用快查。

    调用统计只是概览卡片的轻量摘要；完整看板在 /admin/monitoring（阶段 3）。
    """
    users_total = session.scalar(select(func.count()).select_from(User)) or 0
    users_by_role = dict(
        session.execute(select(User.role, func.count()).group_by(User.role)).all()
    )
    users_active = (
        session.scalar(select(func.count()).select_from(User).where(User.status == "active")) or 0
    )
    orgs_total = session.scalar(select(func.count()).select_from(Organization)) or 0

    since = datetime.now(UTC) - timedelta(hours=24)
    calls_24h = (
        session.scalar(
            select(func.count()).select_from(model_calls).where(model_calls.c.created_at >= since)
        )
        or 0
    )
    failures_24h = (
        session.scalar(
            select(func.count())
            .select_from(model_calls)
            .where(model_calls.c.created_at >= since, model_calls.c.status != "succeeded")
        )
        or 0
    )

    return {
        "users": {
            "total": users_total,
            "active": users_active,
            "by_role": {k: int(v) for k, v in users_by_role.items()},
        },
        "organizations": {"total": orgs_total},
        "model_calls_24h": {"total": int(calls_24h), "failed": int(failures_24h)},
    }
