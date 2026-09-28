"""资料库「试卷」文件夹端点：鉴权 + 存/列/详/删生命周期 + 课程隔离。

照抄 tests/integration/test_paper_chain_auth.py 的既有模式：SQLite 内存库 +
dependency_overrides 替换 get_session，真实登录拿 Bearer token。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base, User, paper_versions
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
        yield auth, factory
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


C = "/api/v1/courses/c1"
ARCHIVE_ENDPOINTS = [
    ("POST", f"{C}/exam-projects/p1/paper-archives", {}),
    ("GET", f"{C}/paper-archives", None),
    ("GET", f"{C}/paper-archives/a1", None),
    ("DELETE", f"{C}/paper-archives/a1", None),
]


def _call(client: TestClient, method: str, path: str, body):
    kwargs = {"json": body} if body is not None else {}
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ARCHIVE_ENDPOINTS,
    ids=[f"{m}-{p.split('/api/v1/')[1]}" for m, p, _ in ARCHIVE_ENDPOINTS],
)
def test_archive_endpoints_reject_missing_token(env, method, path, body):
    bare = TestClient(app)
    assert _call(bare, method, path, body).status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ARCHIVE_ENDPOINTS,
    ids=[f"{m}-{p.split('/api/v1/')[1]}" for m, p, _ in ARCHIVE_ENDPOINTS],
)
def test_archive_endpoints_reject_invalid_token(env, method, path, body):
    forged = TestClient(app, headers={"Authorization": "Bearer not-a-real-token"})
    assert _call(forged, method, path, body).status_code == 401


def test_archive_lifecycle_and_course_isolation(env):
    auth, factory = env
    course = auth.post("/api/v1/courses", json={"name": "数据结构", "slug": "ds"})
    assert course.status_code == 201, course.text
    cid = course.json()["id"]
    proj = auth.post(f"/api/v1/courses/{cid}/exam-projects", json={"name": "期末卷"})
    assert proj.status_code == 201, proj.text
    pid = proj.json()["id"]

    # 生成链路太重：直接落一条 paper_version（0 题目），接口测试只关心 HTTP 层
    with factory() as session:
        session.execute(
            paper_versions.insert().values(
                id="pv1", course_id=cid, exam_project_id=pid,
                version_no=1, status="candidate",
            )
        )
        session.commit()

    # 存：不给 source → 存当前卷；不给 name → 「项目名 v几」
    created = auth.post(f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-archives", json={})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "期末卷 v1"
    assert body["item_count"] == 0
    assert body["source_paper_version_id"] == "pv1"
    assert body["snapshot"]["version_no"] == 1
    aid = body["id"]

    listed = auth.get(f"/api/v1/courses/{cid}/paper-archives")
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()] == [aid]
    assert listed.json()[0]["project_name"] == "期末卷"
    # 列表不塞题目内容
    assert "questions" not in listed.json()[0]

    detail = auth.get(f"/api/v1/courses/{cid}/paper-archives/{aid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["questions"] == []

    # 课程隔离：换个课程既列不到也点不到这份归档
    other = auth.post("/api/v1/courses", json={"name": "操作系统", "slug": "os"})
    other_cid = other.json()["id"]
    assert auth.get(f"/api/v1/courses/{other_cid}/paper-archives").json() == []
    assert auth.get(f"/api/v1/courses/{other_cid}/paper-archives/{aid}").status_code == 404

    # 删：真正从库里删掉，再取 404、再删 404
    assert auth.delete(f"/api/v1/courses/{cid}/paper-archives/{aid}").status_code == 204
    assert auth.get(f"/api/v1/courses/{cid}/paper-archives/{aid}").status_code == 404
    assert auth.delete(f"/api/v1/courses/{cid}/paper-archives/{aid}").status_code == 404

    # 不存在的项目存卷 → 404
    missing = auth.post(f"/api/v1/courses/{cid}/exam-projects/nope/paper-archives", json={})
    assert missing.status_code == 404, missing.text
