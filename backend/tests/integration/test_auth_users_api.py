"""管理员建号端点：创建成功可登录、非管理员拒绝、重名冲突、入参校验。

照抄 tests/integration/test_courses_api.py 的既有模式：
SQLite + dependency_overrides 替换 get_session，真实登录拿 Bearer token。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base, User
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                User(
                    id="admin",
                    username="admin",
                    password_hash=hash_password("123456"),
                    display_name="System",
                    role="admin",
                ),
                User(
                    id="t1",
                    username="teacher1",
                    password_hash=hash_password("123456"),
                    display_name="张老师",
                    role="teacher",
                ),
            ]
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _client(factory, username: str) -> TestClient:
    runner = TestClient(app)
    login = runner.post("/api/v1/auth/login", json={"username": username, "password": "123456"})
    assert login.status_code == 200, login.text
    return TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})


def test_admin_creates_account_and_new_user_can_login(env):
    admin = _client(env, "admin")
    created = admin.post(
        "/api/v1/auth/users",
        json={"username": "li_laoshi", "password": "pw123456", "name": "李老师"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["username"] == "li_laoshi"
    assert body["name"] == "李老师"
    assert body["role"] == "teacher"  # 建号固定教师角色，不接受调用方指定
    assert "password" not in body

    # 新账号立即可登录（哈希与登录链路打通）
    login = TestClient(app).post(
        "/api/v1/auth/login", json={"username": "li_laoshi", "password": "pw123456"}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["name"] == "李老师"


def test_teacher_cannot_create_account(env):
    teacher = _client(env, "teacher1")
    resp = teacher.post(
        "/api/v1/auth/users",
        json={"username": "another", "password": "pw123456", "name": "王老师"},
    )
    assert resp.status_code == 403, resp.text


def test_duplicate_username_conflicts(env):
    admin = _client(env, "admin")
    resp = admin.post(
        "/api/v1/auth/users",
        json={"username": "teacher1", "password": "pw123456", "name": "重名"},
    )
    assert resp.status_code == 409, resp.text


def test_create_user_validates_payload(env):
    admin = _client(env, "admin")
    # 用户名过短 / 含非法字符，密码过短，姓名缺失 → 422
    for bad in (
        {"username": "ab", "password": "pw123456", "name": "短名"},
        {"username": "bad name", "password": "pw123456", "name": "空格"},
        {"username": "okname", "password": "12345", "name": "密码太短"},
        {"username": "okname", "password": "pw123456"},
    ):
        resp = admin.post("/api/v1/auth/users", json=bad)
        assert resp.status_code == 422, f"{bad} -> {resp.status_code}"


def test_anonymous_cannot_create_account(env):
    resp = TestClient(app).post(
        "/api/v1/auth/users",
        json={"username": "ghost", "password": "pw123456", "name": "匿名"},
    )
    assert resp.status_code == 401, resp.text