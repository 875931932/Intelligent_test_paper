"""知识空间 API 集成测试：创建、范围可见性、扩范围确认、owner 移交。

覆盖 P1.2 的关键行为：默认仅成员可读；扩大范围必须预览并显式确认；
普通读者不能改策略；owner 移交在单事务完成且不能移除最后 owner。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from app.db.schema import Base, Organization, User
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password

API = "/api/v1/knowledge"


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                Organization(id="school-1", parent_id=None, kind="school", name="示例大学"),
                Organization(id="college-1", parent_id="school-1", kind="college", name="计算机学院"),
                Organization(id="college-2", parent_id="school-1", kind="college", name="外国语学院"),
                Organization(id="school-2", parent_id=None, kind="school", name="另一所大学"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _user("teacher-a", "college-1"),
                _user("teacher-b", "college-1"),
                _user("teacher-c", "college-2"),
                _user("teacher-x", "school-2"),
                _user("teacher-no-org", None),
            ]
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _user(user_id: str, org_id: str | None) -> User:
    return User(
        id=user_id,
        username=user_id,
        password_hash=hash_password("123456"),
        display_name=user_id,
        role="teacher",
        organization_id=org_id,
        status="active",
    )


def _login(username: str) -> TestClient:
    runner = TestClient(app)
    response = runner.post(
        "/api/v1/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text
    return TestClient(app, headers={"Authorization": "Bearer " + response.json()["token"]})


def test_space_lifecycle_and_scope_expansion(env):
    a = _login("teacher-a")
    b = _login("teacher-b")
    c = _login("teacher-c")

    created = a.post(f"{API}/spaces", json={"name": "数据结构资料", "subject_tags": ["数据结构"]})
    assert created.status_code == 201, created.text
    space = created.json()
    assert space["role"] == "owner"
    assert space["read_scope"] == "members"
    space_id = space["id"]

    # 默认仅成员可读：同学院 B 也看不到。
    assert a.get(f"{API}/spaces").json()[0]["role"] == "owner"
    assert b.get(f"{API}/spaces").json() == []

    # 加 B 为读者后可见，但读者不能修改策略。
    added = a.put(f"{API}/spaces/{space_id}/members/teacher-b", json={"role": "reader"})
    assert added.status_code == 200, added.text
    listed_for_b = b.get(f"{API}/spaces").json()
    assert len(listed_for_b) == 1 and listed_for_b[0]["role"] == "reader"
    forbidden = b.patch(
        f"{API}/spaces/{space_id}",
        json={"expected_policy_version": 2, "name": "劫持命名"},
    )
    assert forbidden.status_code == 403, forbidden.text

    # 扩大范围不确认 -> 409；预览返回将新增可见文档与确认要求。
    version = a.get(f"{API}/spaces/{space_id}").json()["policy_version"]
    blocked = a.patch(
        f"{API}/spaces/{space_id}",
        json={"expected_policy_version": version, "read_scope": "school"},
    )
    assert blocked.status_code == 409, blocked.text
    preview = a.post(
        f"{API}/spaces/{space_id}/policy-preview", json={"new_read_scope": "school"}
    )
    assert preview.status_code == 200
    assert preview.json()["requires_confirmation"] is True

    expanded = a.patch(
        f"{API}/spaces/{space_id}",
        json={
            "expected_policy_version": version,
            "read_scope": "school",
            "confirm_scope_expansion": True,
        },
    )
    assert expanded.status_code == 200, expanded.text
    assert expanded.json()["read_scope"] == "school"

    # 全校范围：其他学院可读；跨校账号仍不可见。
    assert len(c.get(f"{API}/spaces").json()) == 1
    assert c.get(f"{API}/spaces").json()[0]["role"] == "reader"
    x = _login("teacher-x")
    assert x.get(f"{API}/spaces").json() == []

    # 无组织账号不能建库。
    no_org = _login("teacher-no-org")
    rejected = no_org.post(f"{API}/spaces", json={"name": "无组织库"})
    assert rejected.status_code == 403, rejected.text


def test_owner_transfer_and_last_owner_protection(env):
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "操作系统资料"}).json()
    space_id = space["id"]

    # 不能移除最后 owner。
    removed = a.delete(f"{API}/spaces/{space_id}/members/teacher-a")
    assert removed.status_code == 409, removed.text

    a.put(f"{API}/spaces/{space_id}/members/teacher-b", json={"role": "publisher"})
    version = a.get(f"{API}/spaces/{space_id}").json()["policy_version"]
    transferred = a.post(
        f"{API}/spaces/{space_id}/transfer-owner",
        json={"target_user_id": "teacher-b", "expected_policy_version": version},
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["owner_id"] == "teacher-b"

    b = _login("teacher-b")
    members = b.get(f"{API}/spaces/{space_id}/members").json()
    roles = {m["user_id"]: m["role"] for m in members}
    assert roles == {"teacher-a": "publisher", "teacher-b": "owner"}

    # 原 owner 降级后不能继续管成员。
    denied = a.put(f"{API}/spaces/{space_id}/members/teacher-c", json={"role": "reader"})
    assert denied.status_code == 403, denied.text


def test_cross_school_member_is_rejected_and_anonymous_blocked(env):
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "网络资料"}).json()
    rejected = a.put(f"{API}/spaces/{space['id']}/members/teacher-x", json={"role": "reader"})
    assert rejected.status_code == 422, rejected.text

    anon = TestClient(app)
    assert anon.get(f"{API}/spaces").status_code == 401
    assert anon.get(f"{API}/meta").status_code == 401
