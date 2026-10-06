"""User authentication: password hashing, token signing/verification, current-user resolution.

Uses only the standard library (pbkdf2_hmac + HMAC-SHA256) to keep the
authentication layer dependency-free and deterministic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.schema import User

PBKDF2_ITERATIONS = 260_000


class AuthenticationError(Exception):
    """Raised when credentials are wrong or a token is invalid/expired."""


class UserNotFoundError(Exception):
    """Raised when the user implied by a valid token no longer exists."""


class UsernameTakenError(Exception):
    """Raised when the username is already occupied."""


# ── Password hashing ─────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        PBKDF2_ITERATIONS,
        salt.hex(),
        digest.hex(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ── Token signing ────────────────────────────────────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_token(user: User) -> str:
    now = int(time.time())
    payload = {
        "sub": user.id,
        "username": user.username,
        "iat": now,
        "exp": now + settings.auth_token_ttl_seconds,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(settings.auth_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def decode_token(token: str) -> dict:
    """Return the verified payload or raise AuthenticationError."""
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(settings.auth_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise AuthenticationError("invalid token")
        payload = json.loads(_unb64(body).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise AuthenticationError("token expired")
        return payload
    except AuthenticationError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed token is invalid
        raise AuthenticationError("invalid token") from exc


def authenticate_user(session: Session, *, username: str, password: str) -> User:
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        raise AuthenticationError("用户名或密码错误")
    return user


def user_from_payload(session: Session, payload: dict) -> User:
    user = session.get(User, payload.get("sub"))
    if user is None:
        raise UserNotFoundError
    return user


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    display_name: str,
    role: str = "teacher",
) -> User:
    """创建账号。唯一入口是管理员建号端点（本系统不开放自助注册）。

    重名由「先查后插 + 唯一约束兜底」双重保证：查询挡住常规重名，并发下
    两个管理员同时建同名账号时唯一约束报 IntegrityError，同样归一为
    UsernameTakenError，避免把 500 抛给调用方。
    """
    if session.scalar(select(User).where(User.username == username)) is not None:
        raise UsernameTakenError(username)
    user = User(
        id=str(uuid4()),
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        role=role,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise UsernameTakenError(username) from exc
    session.refresh(user)
    return user


def user_to_public_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "name": user.display_name,
        "role": user.role,
    }