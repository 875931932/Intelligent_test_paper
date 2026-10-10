"""账号管理：列表/建号/编辑/停启用/重置密码（范围过滤内）。

停用取代删除——无删除端点；课程外键不悬空（后台管理设计红线）。
org_admin 只能管理本单位内的 teacher，一切越权返回 404/403，不泄露存在性。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.admin.deps import org_scope_org_ids, require_admin
from app.db.schema import User
from app.db.session import get_session
from app.services import admin_account_service as svc

router = APIRouter(prefix="/users", tags=["admin-users"], dependencies=[Depends(require_admin)])


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=6, max_length=255)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="teacher", pattern="^(admin|org_admin|teacher)$")
    organization_id: str | None = None
    title: str | None = Field(default=None, max_length=40)
    employee_no: str | None = Field(default=None, max_length=60)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, pattern="^(admin|org_admin|teacher)$")
    organization_id: str | None = None
    title: str | None = Field(default=None, max_length=40)
    employee_no: str | None = Field(default=None, max_length=60)


class StatusPayload(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class ResetPasswordPayload(BaseModel):
    password: str = Field(min_length=6, max_length=255)


def _scope(user: User, session: Session) -> set[str] | None:
    """可见账号的组织范围：平台管理员 None=不过滤；org_admin 为其单位 subtree。"""
    if user.role == "admin":
        return None
    return org_scope_org_ids(session, user)


def _load_target(session: Session, operator: User, org_scope: set[str] | None, user_id: str) -> User:
    """取目标账号并做范围准入。越权/不存在统一 404，不泄露存在性。"""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    if operator.role != "admin":
        if target.role == "admin" or target.organization_id is None or target.organization_id not in (org_scope or set()):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    return target


@router.get("")
def list_users(
    q: str | None = Query(default=None, max_length=120),
    organization_id: str | None = None,
    role: str | None = None,
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|disabled)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    return svc.list_users(
        session,
        q=q,
        organization_id=organization_id,
        role=role,
        status=status_filter,
        org_ids_in_scope=_scope(user, session),
        page=page,
        page_size=page_size,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    try:
        return svc.create_user(
            session,
            user,
            username=payload.username.strip(),
            password=payload.password,
            display_name=payload.name.strip(),
            role=payload.role,
            organization_id=payload.organization_id,
            title=payload.title,
            employee_no=payload.employee_no,
            org_scope=set() if user.role == "admin" else org_scope_org_ids(session, user),
        )
    except svc.UsernameTakenError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")
    except svc.UserValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdate,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    org_scope = set() if user.role == "admin" else org_scope_org_ids(session, user)
    target = _load_target(session, user, org_scope, user_id)
    try:
        return svc.update_user(
            session,
            user,
            target,
            display_name=payload.name,
            role=payload.role,
            organization_id=payload.organization_id,
            title=payload.title,
            employee_no=payload.employee_no,
            org_scope=org_scope,
        )
    except svc.UserValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.put("/{user_id}/status")
def set_status(
    user_id: str,
    payload: StatusPayload,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    org_scope = set() if user.role == "admin" else org_scope_org_ids(session, user)
    target = _load_target(session, user, org_scope, user_id)
    try:
        return svc.set_user_status(session, user, target, status=payload.status)
    except svc.UserValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    user_id: str,
    payload: ResetPasswordPayload,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    org_scope = set() if user.role == "admin" else org_scope_org_ids(session, user)
    target = _load_target(session, user, org_scope, user_id)
    try:
        svc.reset_password(session, user, target, new_password=payload.password)
    except svc.UserValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
