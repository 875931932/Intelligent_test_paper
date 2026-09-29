"""课程端点：类别选择（创建带类别、回显）与类别清单（路由顺序回归）。

照抄 tests/integration/test_paper_chain_auth.py 的既有模式：
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
        session.add(
            User(
                id="admin",
                username="admin",
                password_hash=hash_password("123456"),
                display_name="System",
                role="admin",
            )
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        runner = TestClient(app)
        login = runner.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})
        yield auth
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_create_course_accepts_category_and_defaults_when_unknown(env):
    styled = env.post("/api/v1/courses", json={"name": "分布式系统", "slug": "dist-sys", "category": "computer"})
    assert styled.status_code == 201, styled.text
    assert styled.json()["category"] == "computer"

    # 未知类别回退默认档（不报错），缺省也是默认档
    bogus = env.post("/api/v1/courses", json={"name": "未知类别课", "slug": "bogus-cat", "category": "bogus"})
    assert bogus.status_code == 201, bogus.text
    assert bogus.json()["category"] == "general"

    plain = env.post("/api/v1/courses", json={"name": "无类别课", "slug": "no-cat"})
    assert plain.status_code == 201, plain.text
    assert plain.json()["category"] == "general"


def test_list_courses_returns_category(env):
    env.post("/api/v1/courses", json={"name": "人文导读", "slug": "hum-101", "category": "humanities"})
    listed = env.get("/api/v1/courses")
    assert listed.status_code == 200
    assert {c["category"] for c in listed.json()} >= {"humanities"}


def test_course_categories_endpoint_not_shadowed_by_path_param(env):
    """/courses/categories 必须注册在 /{course_id} 之前（否则被当作课程 id 吞掉）。"""
    resp = env.get("/api/v1/courses/categories")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    keys = [item["key"] for item in items]
    assert keys[0] == "general"
    assert {"computer", "science_engineering", "humanities"} <= set(keys)
    for item in items:
        assert item["label"] and item["description"]
        assert item["question_types"]

    # 裸客户端（无 token）同样 401，清单不绕过鉴权
    anon = TestClient(app)
    assert anon.get("/api/v1/courses/categories").status_code == 401
