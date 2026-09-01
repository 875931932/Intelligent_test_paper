"""Authentication endpoints: login and current user introspection."""

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