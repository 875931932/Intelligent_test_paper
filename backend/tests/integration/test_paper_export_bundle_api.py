"""一键打包导出端点：zip 结构/清单/文件名 + 版本缺失与跨课程 404（真实渲染）。"""
from __future__ import annotations

import io
import json
import zipfile

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
    """SQLite 内存库 + dependency_overrides 替换 get_session，真实登录拿 Bearer token。"""
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
        login = TestClient(app).post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})
        yield auth, factory
    finally:
        app.dependency_overrides.pop(get_session, None)


def _seed(auth, factory, *, course_slug: str = "bundle-course") -> tuple[str, str, str]:
    """课程 + 项目 + 一条 0 题目的 paper_version；返回 (course_id, project_id, pv_id)。"""
    course = auth.post(
        "/api/v1/courses", json={"name": course_slug, "slug": course_slug}
    )
    assert course.status_code == 201, course.text
    cid = course.json()["id"]
    proj = auth.post(f"/api/v1/courses/{cid}/exam-projects", json={"name": "期末卷"})
    assert proj.status_code == 201, proj.text
    pid = proj.json()["id"]

    # 生成链路太重：直接落一条 paper_version（0 题目）——打包端点关心 zip 组装与
    # 跨端点同源渲染，题目内容渲染由 test_paper_export_render 单元层覆盖
    pvid = f"pv-{course_slug}"
    with factory() as session:
        session.execute(
            paper_versions.insert().values(
                id=pvid,
                course_id=cid,
                exam_project_id=pid,
                version_no=2,
                status="candidate",
            )
        )
        session.commit()
    return cid, pid, pvid


def test_bundle_returns_valid_zip_with_all_six_entries(env):
    auth, factory = env
    cid, pid, pvid = _seed(auth, factory)

    resp = auth.get(
        f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-versions/{pvid}/export/bundle"
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == 'attachment; filename="paper-bundle-v2.zip"'

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == [
            "考试卷_v2.docx",
            "学生卷_v2.html",
            "答题卡_v2.docx",
            "答题卡_v2.html",
            "答卷_含答案_v2.html",
            "answer_detail_v2.json",
        ]
        # 两份 docx 是真实模板渲染产物（zip 魔数），HTML/JSON 同源可读
        assert zf.read("考试卷_v2.docx").startswith(b"PK")
        assert zf.read("答题卡_v2.docx").startswith(b"PK")
        assert "html" in zf.read("学生卷_v2.html").decode("utf-8").lower()
        detail = json.loads(zf.read("answer_detail_v2.json"))
        assert detail["version_no"] == 2


def test_bundle_404_for_unknown_paper_version(env):
    auth, factory = env
    cid, pid, _ = _seed(auth, factory, course_slug="bundle-course-404")

    resp = auth.get(
        f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-versions/pv-nope/export/bundle"
    )

    assert resp.status_code == 404
    assert "试卷版本不存在" in resp.json()["detail"]


def test_bundle_404_across_course_isolation(env):
    """课程隔离：拿另一门课程的 course_id 访问，必须 404 而不是打包成功。"""
    auth, factory = env
    cid_a, _, pvid = _seed(auth, factory, course_slug="bundle-course-a")
    cid_b, pid_b, _ = _seed(auth, factory, course_slug="bundle-course-b")
    assert cid_a != cid_b

    resp = auth.get(
        f"/api/v1/courses/{cid_b}/exam-projects/{pid_b}/paper-versions/{pvid}/export/bundle"
    )

    assert resp.status_code == 404
