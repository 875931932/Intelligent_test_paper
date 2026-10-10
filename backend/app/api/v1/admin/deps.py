"""后台管理端的鉴权依赖：三级角色守卫 + 单位范围解析。

守卫分三层，逐级收紧（默认拒绝）：
- require_admin：教师一律 403，admin/org_admin 可进后台（router 级挂载）。
- require_platform_admin：org_admin 也 403（模型管理等平台专属操作）。
- org_scope_org_ids：org_admin 可见的组织 id 集（本校 + 其下学院），
  供账号/监控的范围过滤拼进查询。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.db.schema import Organization, User

ADMIN_ROLES = ("admin", "org_admin")


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """后台入口守卫：仅 admin / org_admin，教师 403。"""
    if current_user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可访问后台管理")
    return current_user


def require_platform_admin(current_user: User = Depends(require_admin)) -> User:
    """平台级操作（模型管理、组织增改等）：仅 admin。"""
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅平台管理员可执行此操作")
    return current_user


def is_platform_admin(user: User) -> bool:
    return user.role == "admin"


def org_scope_org_ids(session: Session, user: User) -> set[str]:
    """org_admin 的可见组织集合：其自身所在组织 + 全部下级学院。

    平台管理员调用方不应使用本函数（其范围是全局，直接不过滤）；
    用户未归属组织的 org_admin 返回空集 —— 无归属即无范围，默认拒绝。
    """
    if user.organization_id is None:
        return set()
    child_ids = session.scalars(
        select(Organization.id).where(Organization.parent_id == user.organization_id)
    ).all()
    return {user.organization_id, *child_ids}


def user_in_scope(user: User, target: User, org_ids: set[str]) -> bool:
    """目标账号是否在操作者的管理范围内（无单位的目标仅平台管理员可见）。"""
    if is_platform_admin(user):
        return True
    return target.organization_id is not None and target.organization_id in org_ids
