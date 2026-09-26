"""试卷生成→导出链路（exam_projects / paper_versions）鉴权回归。

照抄 tests/integration/test_material_upload.py 的既有模式：
SQLite 内存库 + dependency_overrides 替换 get_session，真实登录拿 Bearer token。
裸客户端必须 401，带 token 才 200。
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


# (method, path, json body)：覆盖 paper_versions 全部 15 个端点 + exam_projects 全部 16 个
C = "/api/v1/courses/c1"
PAPER_ENDPOINTS = [
    ("GET", f"{C}/exam-projects/p1/paper-versions/current", None),
    ("GET", f"{C}/paper-versions/pv1/needs-review", None),
    ("PATCH", f"{C}/paper-versions/pv1/items/1", {"clear_needs_review": True}),
    ("POST", f"{C}/paper-versions/pv1/items/1/ai-revise", {"instruction": "改"}),
    ("POST", f"{C}/paper-versions/pv1/items/ai-generate", {"instruction": "出题"}),
    ("POST", f"{C}/paper-versions/pv1/ai-review", {"instruction": "评审"}),
    ("PUT", f"{C}/paper-versions/pv1/items/reorder", {"ordered_indices": [1]}),
    ("POST", f"{C}/paper-versions/pv1/items", {"stem": "x"}),
    ("DELETE", f"{C}/paper-versions/pv1/items/1", None),
    ("POST", f"{C}/paper-versions/pv1/confirm", {}),
    ("POST", f"{C}/paper-versions/pv1/revert", {}),
    ("GET", f"{C}/exam-projects/p1/paper-versions/pv1/export/json", None),
    ("GET", f"{C}/exam-projects/p1/paper-versions/pv1/export/student", None),
    ("GET", f"{C}/exam-projects/p1/paper-versions/pv1/export/answer-key", None),
    ("GET", f"{C}/exam-projects/p1/paper-versions/pv1/export/answer-card", None),
    # 同一端点的 docx 变体（可编辑 Word），查询参数不同但鉴权同规则
    ("GET", f"{C}/exam-projects/p1/paper-versions/pv1/export/answer-card?format=docx", None),
]
EXAM_PROJECT_ENDPOINTS = [
    ("GET", f"{C}/exam-projects", None),
    ("POST", f"{C}/exam-projects", {"name": "P1"}),
    ("GET", f"{C}/exam-projects/p1", None),
    ("PATCH", f"{C}/exam-projects/p1", {"status": "review"}),
    ("DELETE", f"{C}/exam-projects/p1", None),
    ("POST", f"{C}/exam-projects/p1/blueprints", {"framework_version_id": "fv"}),
    ("GET", f"{C}/exam-projects/p1/blueprints/current/plan-items", None),
    ("PATCH", f"{C}/exam-projects/plan-items/pi1", {"score": 5}),
    ("POST", f"{C}/exam-projects/p1/blueprints/current/confirm", {}),
    ("POST", f"{C}/exam-projects/p1/contracts/allocate", {}),
    ("PATCH", f"{C}/exam-projects/p1/contracts/revise", {}),
    ("POST", f"{C}/exam-projects/p1/contracts/confirm", {}),
    ("POST", f"{C}/exam-projects/p1/contract-slots/1/explain", {}),
    ("GET", f"{C}/exam-projects/p1/contracts/current", None),
    ("POST", f"{C}/exam-projects/p1/generate", {}),
    ("GET", f"{C}/exam-projects/task-runs/tr1", None),
]


def _call(client: TestClient, method: str, path: str, body):
    kwargs = {"json": body} if body is not None else {}
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    PAPER_ENDPOINTS + EXAM_PROJECT_ENDPOINTS,
    ids=[f"{m}-{p.split('/api/v1/')[1]}" for m, p, _ in PAPER_ENDPOINTS + EXAM_PROJECT_ENDPOINTS],
)
def test_paper_chain_endpoints_reject_missing_token(env, method, path, body):
    _auth, _factory = env
    bare = TestClient(app)
    assert _call(bare, method, path, body).status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    PAPER_ENDPOINTS + EXAM_PROJECT_ENDPOINTS,
    ids=[f"{m}-{p.split('/api/v1/')[1]}" for m, p, _ in PAPER_ENDPOINTS + EXAM_PROJECT_ENDPOINTS],
)
def test_paper_chain_endpoints_reject_invalid_token(env, method, path, body):
    _auth, _factory = env
    forged = TestClient(app, headers={"Authorization": "Bearer not-a-real-token"})
    assert _call(forged, method, path, body).status_code == 401


def test_exam_projects_endpoints_accept_token(env):
    auth, _factory = env
    course = auth.post(
        "/api/v1/courses", json={"name": "数据结构", "slug": "ds", "description": "x"}
    )
    assert course.status_code == 201, course.text
    cid = course.json()["id"]

    listed = auth.get(f"/api/v1/courses/{cid}/exam-projects")
    assert listed.status_code == 200
    assert listed.json() == []

    created = auth.post(f"/api/v1/courses/{cid}/exam-projects", json={"name": "期末卷"})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]

    detail = auth.get(f"/api/v1/courses/{cid}/exam-projects/{pid}")
    assert detail.status_code == 200
    assert detail.json()["id"] == pid


def test_paper_version_and_exports_accept_token(env):
    auth, factory = env
    course = auth.post("/api/v1/courses", json={"name": "数据结构2", "slug": "ds2"})
    assert course.status_code == 201, course.text
    cid = course.json()["id"]
    proj = auth.post(f"/api/v1/courses/{cid}/exam-projects", json={"name": "期末卷"})
    assert proj.status_code == 201, proj.text
    pid = proj.json()["id"]

    # 生成链路太重：直接落一条 paper_version（0 题目），鉴权测试只关心 HTTP 层
    with factory() as session:
        session.execute(
            paper_versions.insert().values(
                id="pv1",
                course_id=cid,
                exam_project_id=pid,
                version_no=1,
                status="candidate",
            )
        )
        session.commit()

    current = auth.get(f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-versions/current")
    assert current.status_code == 200, current.text
    assert current.json()["id"] == "pv1"

    needs = auth.get(f"/api/v1/courses/{cid}/paper-versions/pv1/needs-review")
    assert needs.status_code == 200
    assert needs.json() == []

    js = auth.get(
        f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-versions/pv1/export/json"
    )
    assert js.status_code == 200, js.text
    assert js.headers["content-disposition"] == 'attachment; filename="answer_detail_v1.json"'

    for suffix in ("student", "answer-key", "answer-card"):
        html = auth.get(
            f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-versions/pv1/export/{suffix}"
        )
        assert html.status_code == 200, f"{suffix}: {html.text[:200]}"
        assert "html" in html.headers["content-type"]
