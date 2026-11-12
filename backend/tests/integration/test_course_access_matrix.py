"""P0.1 课程授权回归矩阵：跨账号访问必须统一 404，且覆盖此前裸奔的路由组。

复现方式与 2026-11-12 隔离测试一致：SQLite 内存库 + 真实登录，两个合成教师
各有课程；A 用 B 的 course_id 请求列表/详情/任务/会话/框架/知识目录。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base, Course, User
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password


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
                User(
                    id="teacher-a",
                    username="teacher_a",
                    password_hash=hash_password("123456"),
                    display_name="教师A",
                    role="teacher",
                    status="active",
                ),
                User(
                    id="teacher-b",
                    username="teacher_b",
                    password_hash=hash_password("123456"),
                    display_name="教师B",
                    role="teacher",
                    status="active",
                ),
                User(
                    id="admin",
                    username="admin",
                    password_hash=hash_password("123456"),
                    display_name="管理员",
                    role="admin",
                    status="active",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                Course(id="course-b", owner_id="teacher-b", slug="b", name="B 的课程"),
                Course(id="course-a", owner_id="teacher-a", slug="a", name="A 的课程"),
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


def _login(username: str) -> TestClient:
    runner = TestClient(app)
    response = runner.post(
        "/api/v1/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text
    return TestClient(app, headers={"Authorization": "Bearer " + response.json()["token"]})


# 覆盖此前只校验登录或完全未鉴权的代表性读取端点。
CROSS_ACCOUNT_ENDPOINTS = [
    ("GET", "/api/v1/courses/{cid}/materials"),
    ("GET", "/api/v1/courses/{cid}/materials/m1"),
    ("GET", "/api/v1/courses/{cid}/framework-versions/current"),
    ("GET", "/api/v1/courses/{cid}/published-knowledge"),
    ("GET", "/api/v1/courses/{cid}/exam-projects"),
    ("GET", "/api/v1/courses/{cid}/exam-projects/p1"),
    ("GET", "/api/v1/courses/{cid}/paper-versions/pv1/needs-review"),
    ("GET", "/api/v1/courses/{cid}/paper-archives"),
    ("GET", "/api/v1/courses/{cid}/assistant/sessions"),
    ("GET", "/api/v1/courses/{cid}/assistant/messages"),
]


@pytest.mark.parametrize(("method", "template"), CROSS_ACCOUNT_ENDPOINTS)
def test_other_teachers_course_is_invisible(env, method, template):
    client = _login("teacher_a")
    path = template.format(cid="course-b")
    response = client.request(method, path)
    # 无权与不存在统一 404：不通过响应差异暴露他人课程是否存在。
    assert response.status_code == 404, f"{path}: {response.status_code} {response.text}"


@pytest.mark.parametrize(("method", "template"), CROSS_ACCOUNT_ENDPOINTS)
def test_anonymous_course_endpoints_require_login(env, method, template):
    # 框架与知识目录此前完全没有鉴权，裸客户端能直接读到课程数据。
    anon = TestClient(app)
    path = template.format(cid="course-b")
    assert anon.request(method, path).status_code == 401


def test_own_course_still_works(env):
    client = _login("teacher_a")
    assert client.get("/api/v1/courses/course-a/materials").status_code == 200
    assert client.get("/api/v1/courses/course-a/assistant/sessions").status_code == 200
    assert client.get("/api/v1/courses/course-a/exam-projects").status_code == 200


def test_platform_admin_does_not_auto_read_other_course(env):
    # 管理身份不自动获得其他教师课程内容（P0 纪律）。
    admin = _login("admin")
    assert admin.get("/api/v1/courses/course-b/materials").status_code == 404


def test_deactivated_user_token_is_rejected(env):
    client = _login("teacher_b")
    assert client.get("/api/v1/courses/course-b/materials").status_code == 200
    factory = env
    with factory() as session:
        session.query(User).filter(User.id == "teacher-b").update({"status": "disabled"})
        session.commit()
    # 已签发 token 的账号被停用后，必须立刻失效，而不是等 token 过期。
    assert client.get("/api/v1/courses/course-b/materials").status_code == 401
