"""模型调用监控端点（/api/v1/admin/monitoring）：概览/趋势/分组/明细（设计 §6.2）。

admin 与 org_admin 均可读（require_admin 守卫）；org_admin 自动收敛到本单位
课程产生的调用（§6.4），平台管理员看全局。所有端点必带 from/to 可选区间
（缺省近 7 天，上限 92 天，违规 400）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1.admin.deps import is_platform_admin, org_scope_org_ids, require_admin
from app.db.schema import User
from app.db.session import get_session
from app.services import monitoring_service as svc

router = APIRouter(
    prefix="/monitoring",
    tags=["admin-monitoring"],
    dependencies=[Depends(require_admin)],
)


def _range(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    try:
        return svc.parse_range(from_, to)
    except svc.RangeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _owner_scope(user: User, session: Session) -> list[str] | None:
    """平台管理员 None=全局；org_admin=单位内用户（其课程即其调用范围）。"""

    if is_platform_admin(user):
        return None
    return svc.scope_owner_ids(session, org_scope_org_ids(session, user))


def _bucket(value: str) -> str:
    if value not in svc.VALID_BUCKETS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"bucket 仅支持 {'/'.join(svc.VALID_BUCKETS)}")
    return value


def _group_by(value: str) -> str:
    if value not in svc.VALID_GROUP_BY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"group_by 仅支持 {'/'.join(svc.VALID_GROUP_BY)}",
        )
    return value


@router.get("/overview")
def overview(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    start, end = _range(from_, to)
    return svc.overview(session, start, end, _owner_scope(user, session))


@router.get("/timeseries")
def timeseries(
    bucket: str = Query(default="day"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[dict]:
    start, end = _range(from_, to)
    return svc.timeseries(session, start, end, _owner_scope(user, session), _bucket(bucket))


@router.get("/breakdown")
def breakdown(
    group_by: str = Query(default="model"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[dict]:
    start, end = _range(from_, to)
    return svc.breakdown(session, start, end, _owner_scope(user, session), _group_by(group_by))


@router.get("/calls")
def list_calls(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    model: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    error_code: str | None = Query(default=None),
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    start, end = _range(from_, to)
    return svc.list_calls(
        session,
        start,
        end,
        _owner_scope(user, session),
        page=page,
        page_size=page_size,
        model=model,
        stage=stage,
        status=status_filter,
        error_code=error_code,
    )


@router.get("/calls/{call_id}")
def get_call(
    call_id: str,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return svc.get_call(session, _owner_scope(user, session), call_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
