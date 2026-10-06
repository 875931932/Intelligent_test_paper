"""Authentication endpoints: login, current user introspection, admin user creation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.db.schema import User
from app.services import auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=255)


class CreateUserRequest(BaseModel):
    """管理员建号入参：本系统不开放自助注册，账号一律由管理员创建。"""

    username: str = Field(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=6, max_length=255)
    name: str = Field(min_length=1, max_length=200)


class UserResponse(BaseModel):
    id: str
    username: str
    name: str
    role: str


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


def _credentials_failed() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = auth_service.decode_token(token)
        return auth_service.user_from_payload(session, payload)
    except auth_service.AuthenticationError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录")
    except auth_service.UserNotFoundError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    try:
        user = auth_service.authenticate_user(session, username=payload.username.strip(), password=payload.password)
    except auth_service.AuthenticationError:
        raise _credentials_failed()
    return LoginResponse(token=auth_service.create_token(user), user=auth_service.user_to_public_dict(user))


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> dict:
    return auth_service.user_to_public_dict(current_user)


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """管理员建号（唯一建号入口：不开放自助注册）。新账号固定为教师角色。"""
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可创建账号")
    try:
        user = auth_service.create_user(
            session,
            username=payload.username.strip(),
            password=payload.password,
            display_name=payload.name.strip(),
        )
    except auth_service.UsernameTakenError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")
    return auth_service.user_to_public_dict(user)