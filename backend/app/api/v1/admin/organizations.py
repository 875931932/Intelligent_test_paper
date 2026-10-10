"""单位管理（学校→学院两级）：树查询 + 新建/编辑/停启用。

范围口径：org_admin 只能看到和操作本单位（学校 + 其下学院）；平台管理员全局。
写操作一律 require_platform_admin——单位结构变更属平台级动作。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.admin.deps import org_scope_org_ids, require_admin, require_platform_admin
from app.db.schema import User
from app.db.session import get_session
from app.services import admin_account_service as svc

router = APIRouter(prefix="/organizations", tags=["admin-organizations"], dependencies=[Depends(require_admin)])


class OrgCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    kind: str = Field(pattern="^(school|college)$")
    parent_id: str | None = None
    code: str | None = Field(default=None, max_length=60)


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    code: str | None = Field(default=None, max_length=60)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


def _is_platform(user: User) -> bool:
    return user.role == "admin"


def _scope(user: User, session: Session) -> set[str] | None:
    """可见组织范围：平台管理员 None=不过滤；org_admin 为其单位 subtree。"""
    return None if _is_platform(user) else org_scope_org_ids(session, user)


@router.get("")
def list_organizations(
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[dict]:
    return svc.list_org_tree(session, visible_org_ids=_scope(user, session))


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_platform_admin)])
def create_organization(
    payload: OrgCreate,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return svc.create_org(
            session,
            name=payload.name.strip(),
            kind=payload.kind,
            parent_id=payload.parent_id,
            code=payload.code.strip() if payload.code else None,
        )
    except svc.OrgConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.patch("/{org_id}", dependencies=[Depends(require_platform_admin)])
def update_organization(
    org_id: str,
    payload: OrgUpdate,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return svc.update_org(
            session,
            org_id,
            name=payload.name.strip() if payload.name else None,
            code=payload.code,
            status=payload.status,
        )
    except svc.OrgNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="组织不存在")
    except svc.OrgConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/scope")
def scope_orgs(
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
    kind: str | None = Query(default=None, pattern="^(school|college)$"),
) -> list[dict]:
    """范围内的组织平铺列表（下拉选择用）。org_admin 自动限定本单位。"""
    scope = _scope(user, session)
    return svc.list_org_tree(session, visible_org_ids=scope, flat=True, kind=kind)
