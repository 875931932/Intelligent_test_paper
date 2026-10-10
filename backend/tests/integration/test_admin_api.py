"""后台管理端点的准入测试：匿名 401 / 教师 403 / 单位管理员与平台管理员 200。

照抄 tests/integration/test_auth_users_api.py 的既有模式：
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
                    display_name="系统管理员",
                    role="admin",
                ),
                User(
                    id="orgadmin1",
                    username="orgadmin1",
                    password_hash=hash_password("123456"),
                    display_name="学院管理员",
                    role="org_admin",
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


def _client(username: str) -> TestClient:
    runner = TestClient(app)
    login = runner.post("/api/v1/auth/login", json={"username": username, "password": "123456"})
    assert login.status_code == 200, login.text
    return TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})


def test_anonymous_gets_401(env):
    resp = TestClient(app).get("/api/v1/admin/overview")
    assert resp.status_code == 401, resp.text


def test_teacher_gets_403(env):
    teacher = _client("teacher1")
    resp = teacher.get("/api/v1/admin/overview")
    assert resp.status_code == 403, resp.text


def test_org_admin_and_platform_admin_can_access_overview(env):
    for username in ("orgadmin1", "admin"):
        client = _client(username)
        resp = client.get("/api/v1/admin/overview")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["users"]["total"] == 3
        assert body["users"]["by_role"]["teacher"] == 1


# ── 组织 ─────────────────────────────────────────────────────────────────────


def test_platform_admin_creates_school_and_college_tree(env):
    admin = _client("admin")
    school = admin.post(
        "/api/v1/admin/organizations",
        json={"name": "示例大学", "kind": "school", "code": "S-001"},
    )
    assert school.status_code == 201, school.text
    school_id = school.json()["id"]

    college = admin.post(
        "/api/v1/admin/organizations",
        json={"name": "计算机学院", "kind": "college", "parent_id": school_id},
    )
    assert college.status_code == 201, college.text

    tree = admin.get("/api/v1/admin/organizations").json()
    assert len(tree) == 1
    assert tree[0]["name"] == "示例大学"
    assert [c["name"] for c in tree[0]["children"]] == ["计算机学院"]


def test_college_cannot_attach_to_college(env):
    admin = _client("admin")
    school = admin.post("/api/v1/admin/organizations", json={"name": "校A", "kind": "school"}).json()
    college = admin.post(
        "/api/v1/admin/organizations",
        json={"name": "院A", "kind": "college", "parent_id": school["id"]},
    ).json()
    bad = admin.post(
        "/api/v1/admin/organizations",
        json={"name": "院B", "kind": "college", "parent_id": college["id"]},
    )
    assert bad.status_code == 409, bad.text


def test_org_admin_cannot_create_organization(env):
    orgadmin = _client("orgadmin1")
    resp = orgadmin.post("/api/v1/admin/organizations", json={"name": "野鸡大学", "kind": "school"})
    assert resp.status_code == 403, resp.text


def test_duplicate_org_name_conflicts(env):
    admin = _client("admin")
    admin.post("/api/v1/admin/organizations", json={"name": "同名校", "kind": "school"})
    dup = admin.post("/api/v1/admin/organizations", json={"name": "同名校", "kind": "school"})
    assert dup.status_code == 409, dup.text


def test_disable_org_with_active_users_conflicts(env):
    admin = _client("admin")
    school = admin.post("/api/v1/admin/organizations", json={"name": "占位校", "kind": "school"}).json()
    admin.post(
        "/api/v1/admin/users",
        json={"username": "u_orgtest", "password": "123456", "name": "归属人", "organization_id": school["id"]},
    )
    resp = admin.patch(
        f"/api/v1/admin/organizations/{school['id']}", json={"status": "disabled"}
    )
    assert resp.status_code == 409, resp.text


# ── 账号 ─────────────────────────────────────────────────────────────────────


def test_platform_admin_creates_teacher_with_org_and_login(env):
    admin = _client("admin")
    school = admin.post("/api/v1/admin/organizations", json={"name": "登录校", "kind": "school"}).json()
    created = admin.post(
        "/api/v1/admin/users",
        json={
            "username": "newteacher",
            "password": "secret6",
            "name": "新教师",
            "role": "teacher",
            "organization_id": school["id"],
            "title": "讲师",
            "employee_no": "T-042",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["organization_id"] == school["id"]
    assert body["title"] == "讲师"

    # 新账号可登录（status 默认 active），/me 带组织字段
    runner = TestClient(app)
    login = runner.post("/api/v1/auth/login", json={"username": "newteacher", "password": "secret6"})
    assert login.status_code == 200, login.text
    me = runner.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer " + login.json()["token"]}
    )
    assert me.status_code == 200
    assert me.json()["organization_id"] == school["id"]
    assert me.json()["status"] == "active"


def test_duplicate_username_conflicts(env):
    admin = _client("admin")
    payload = {"username": "dupuser", "password": "123456", "name": "甲"}
    admin.post("/api/v1/admin/users", json=payload)
    dup = admin.post("/api/v1/admin/users", json=payload)
    assert dup.status_code == 409, dup.text


def test_teacher_cannot_list_or_create_users(env):
    teacher = _client("teacher1")
    assert teacher.get("/api/v1/admin/users").status_code == 403
    resp = teacher.post("/api/v1/admin/users", json={"username": "x123456", "password": "123456", "name": "X"})
    assert resp.status_code == 403


def test_org_admin_scoped_visibility_and_role_ceiling(env):
    """org_admin 只见本单位账号、只能建 teacher；跨单位操作 404。"""
    admin = _client("admin")
    school_a = admin.post("/api/v1/admin/organizations", json={"name": "甲校", "kind": "school"}).json()
    school_b = admin.post("/api/v1/admin/organizations", json={"name": "乙校", "kind": "school"}).json()
    college_a = admin.post(
        "/api/v1/admin/organizations",
        json={"name": "甲院", "kind": "college", "parent_id": school_a["id"]},
    ).json()

    # 把 orgadmin1 归到甲校
    admin.patch("/api/v1/admin/users/orgadmin1", json={"organization_id": school_a["id"]})

    # 甲校建教师 a_teach、甲院建教师 c_teach、乙校建教师 b_teach
    a_teach = admin.post(
        "/api/v1/admin/users",
        json={"username": "a_teach", "password": "123456", "name": "甲师", "organization_id": school_a["id"]},
    ).json()
    admin.post(
        "/api/v1/admin/users",
        json={"username": "c_teach", "password": "123456", "name": "院师", "organization_id": college_a["id"]},
    ).json()
    b_teach = admin.post(
        "/api/v1/admin/users",
        json={"username": "b_teach", "password": "123456", "name": "乙师", "organization_id": school_b["id"]},
    ).json()

    orgadmin = _client("orgadmin1")
    listing = orgadmin.get("/api/v1/admin/users").json()
    names = {item["username"] for item in listing["items"]}
    # 本单位（学校+学院）可见；乙校不可见；无单位的 admin1/orgadmin1 自身可见
    assert "a_teach" in names
    assert "c_teach" in names
    assert "b_teach" not in names

    # 跨单位读取 404（不泄露存在性）
    resp = orgadmin.patch(f"/api/v1/admin/users/{b_teach['id']}", json={"name": "改名"})
    assert resp.status_code == 404, resp.text

    # org_admin 不能建 admin/org_admin 角色
    resp = orgadmin.post(
        "/api/v1/admin/users",
        json={"username": "evil_admin", "password": "123456", "name": "越权", "role": "admin"},
    )
    assert resp.status_code == 400, resp.text

    # org_admin 不能改自己的资料/停用自己
    resp = orgadmin.put("/api/v1/admin/users/orgadmin1/status", json={"status": "disabled"})
    assert resp.status_code == 400, resp.text

    # org_admin 不能动平台管理员
    resp = orgadmin.put("/api/v1/admin/users/admin/status", json={"status": "disabled"})
    assert resp.status_code == 404, resp.text

    # 范围内停用教师 → 该教师无法登录
    resp = orgadmin.put(f"/api/v1/admin/users/{a_teach['id']}/status", json={"status": "disabled"})
    assert resp.status_code == 200, resp.text
    runner = TestClient(app)
    blocked = runner.post("/api/v1/auth/login", json={"username": "a_teach", "password": "123456"})
    assert blocked.status_code == 401, blocked.text


def test_disabled_admin_cannot_login_but_data_kept(env):
    admin = _client("admin")
    admin.put("/api/v1/admin/users/t1/status", json={"status": "disabled"})
    runner = TestClient(app)
    resp = runner.post("/api/v1/auth/login", json={"username": "teacher1", "password": "123456"})
    assert resp.status_code == 401, resp.text

    # 重新启用后可登录
    admin.put("/api/v1/admin/users/t1/status", json={"status": "active"})
    resp = runner.post("/api/v1/auth/login", json={"username": "teacher1", "password": "123456"})
    assert resp.status_code == 200, resp.text


def test_reset_password_takes_effect(env):
    admin = _client("admin")
    resp = admin.put("/api/v1/admin/users/t1/password", json={"password": "newpass1"})
    assert resp.status_code == 204, resp.text
    runner = TestClient(app)
    assert runner.post("/api/v1/auth/login", json={"username": "teacher1", "password": "newpass1"}).status_code == 200
    assert runner.post("/api/v1/auth/login", json={"username": "teacher1", "password": "123456"}).status_code == 401
