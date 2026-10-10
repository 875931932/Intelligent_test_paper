"""旧建号端点（POST /api/v1/auth/users）已下线：账号管理统一走 /api/v1/admin/users。

保留两类护栏：
1. 旧路由不再注册（404），防止旧客户端误依赖；
2. 新端点的入参校验（422 矩阵）与匿名准入（401）。
建号成功链路/重名 409/教师 403 已由 test_admin_api.py 覆盖，此处不重复。
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


def test_legacy_create_user_route_is_gone(env):
    """旧路由已注销：任何角色 POST /auth/users 一律 404。"""
    admin = _client(env, "admin")
    resp = admin.post(
        "/api/v1/auth/users",
        json={"username": "li_laoshi", "password": "pw123456", "name": "李老师"},
    )
    assert resp.status_code == 404, resp.text
    anon = TestClient(app).post(
        "/api/v1/auth/users",
        json={"username": "ghost", "password": "pw123456", "name": "匿名"},
    )
    assert anon.status_code == 404, anon.text


def test_admin_create_user_validates_payload(env):
    """入参校验矩阵迁移到 /api/v1/admin/users（约束与旧端点一致）。"""
    admin = _client(env, "admin")
    for bad in (
        {"username": "ab", "password": "pw123456", "name": "短名"},          # 用户名过短
        {"username": "bad name", "password": "pw123456", "name": "空格"},    # 非法字符
        {"username": "okname", "password": "12345", "name": "密码太短"},     # 密码过短
        {"username": "okname", "password": "pw123456"},                      # 缺姓名
    ):
        resp = admin.post("/api/v1/admin/users", json=bad)
        assert resp.status_code == 422, f"{bad} -> {resp.status_code}"


def test_anonymous_cannot_create_account_via_admin_route(env):
    resp = TestClient(app).post(
        "/api/v1/admin/users",
        json={"username": "ghost", "password": "pw123456", "name": "匿名"},
    )
    assert resp.status_code == 401, resp.text
