"""资料解析内容预览端点集成测试。

GET .../materials/{material_id}/content —— 教师核对 AI 引用与解析质量的唯一读路径。
与助手 RAG 同语料（content_blocks + 最新 ready run）：分页浏览、block_id 定位
上下文窗口、旧引用失效、未解析空态、404 隔离与鉴权。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import (
    Base,
    User,
    content_blocks,
    document_parse_runs,
    material_versions,
    materials,
    parser_profiles,
)
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password

CONTENT = "/api/v1/courses/{cid}/materials/{mid}/content"


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(
            id="admin", username="admin", password_hash=hash_password("123456"),
            display_name="System", role="admin",
        ))
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        bare = TestClient(app)
        login = bare.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})

        c1 = auth.post("/api/v1/courses", json={"name": "资料预览课", "slug": "mat-prev"}).json()["id"]
        c2 = auth.post("/api/v1/courses", json={"name": "隔离课", "slug": "mat-iso"}).json()["id"]

        with factory() as session:
            # m1：已解析（6 块，最新 ready run）；m3：未解析；m2：属于另一门课（隔离）
            session.execute(materials.insert().values(
                id="m1", course_id=c1, logical_name="教学大纲",
                material_type="teaching_syllabus", status="staged",
            ))
            session.execute(materials.insert().values(
                id="m3", course_id=c1, logical_name="未解析讲义",
                material_type="teaching_material", status="staged",
            ))
            session.execute(materials.insert().values(
                id="m2", course_id=c2, logical_name="他课资料",
                material_type="teaching_material", status="staged",
            ))
            session.execute(material_versions.insert().values(
                id="v1", course_id=c1, material_id="m1", version_no=1, status="staged",
                object_key="courses/c1/m1.pdf", size_bytes=100, sha256="a" * 64,
                mime_type="application/pdf",
            ))
            session.execute(parser_profiles.insert().values(
                id="mineru-profile", course_id=c1, name="mineru", version="v1",
                provider="mineru", configuration={},
            ))
            session.execute(document_parse_runs.insert().values(
                id="run1", course_id=c1, material_version_id="v1",
                parser_profile_id="mineru-profile", status="ready",
                completed_at=datetime.now(timezone.utc),
            ))
            for i in range(6):
                session.execute(content_blocks.insert().values(
                    id=f"rb{i + 1}", course_id=c1, document_parse_run_id="run1",
                    material_version_id="v1", block_index=i, block_type="text",
                    text=f"第 {i + 1} 段：监督学习的分类与回归任务讲解。",
                    heading_path=["第三章"] if i % 2 == 0 else [],
                    page_index=2 + i, reading_order=i, content_hash=f"{i}" * 64,
                ))
            session.commit()

        try:
            yield auth, c1, c2, bare
        finally:
            app.dependency_overrides.clear()
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_browse_pagination_slices_in_parse_order(env):
    auth, c1, _c2, _bare = env
    resp = auth.get(CONTENT.format(cid=c1, mid="m1") + "?offset=4&limit=3")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["material_name"] == "教学大纲"
    assert data["total"] == 6
    assert data["offset"] == 4
    assert [b["id"] for b in data["blocks"]] == ["rb5", "rb6"]
    assert data["focus"] is None
    # 定位器随块给出（前端展示页码/章节）
    assert data["blocks"][0]["page_index"] == 6
    assert data["blocks"][0]["heading_path"] == ["第三章"]


def test_focus_block_returns_context_window(env):
    auth, c1, _c2, _bare = env
    resp = auth.get(CONTENT.format(cid=c1, mid="m1") + "?block_id=rb4")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 焦点 rb4（index=3）±窗口：start=max(0,1)，到 idx+4 截尾 → rb2..rb6
    assert data["focus"] == {"id": "rb4", "index": 3}
    assert data["offset"] == 1
    assert [b["id"] for b in data["blocks"]] == ["rb2", "rb3", "rb4", "rb5", "rb6"]
    assert data["total"] == 6


def test_focus_stale_block_reports_null_index(env):
    """重新解析后旧引用失效：blocks 空 + focus.index=null，前端据此提示。"""
    auth, c1, _c2, _bare = env
    resp = auth.get(CONTENT.format(cid=c1, mid="m1") + "?block_id=gone")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["blocks"] == []
    assert data["focus"] == {"id": "gone", "index": None}
    assert data["total"] == 6


def test_unparsed_material_returns_empty_not_error(env):
    auth, c1, _c2, _bare = env
    resp = auth.get(CONTENT.format(cid=c1, mid="m3"))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 0
    assert data["blocks"] == []
    assert data["material_name"] == "未解析讲义"


def test_missing_and_cross_course_material_are_404(env):
    auth, c1, c2, _bare = env
    assert auth.get(CONTENT.format(cid=c1, mid="nope")).status_code == 404
    # 课程隔离：c2 的资料在 c1 的路径下不可见
    assert auth.get(CONTENT.format(cid=c1, mid="m2")).status_code == 404
    # 反向：本课路径能拿到自己的资料详情（新路由不遮蔽旧路由）
    assert auth.get(f"/api/v1/courses/{c1}/materials/m1").status_code == 200
    assert auth.get(f"/api/v1/courses/{c2}/materials/m2").status_code == 200


def test_content_requires_auth(env):
    """materials 全族匿名 401（含本次补的预览端点——族内曾整体缺鉴权，已根治）。"""
    _auth, c1, _c2, bare = env
    assert bare.get(CONTENT.format(cid=c1, mid="m1")).status_code == 401
    assert bare.get(f"/api/v1/courses/{c1}/materials").status_code == 401
    assert bare.delete(f"/api/v1/courses/{c1}/materials/m1").status_code == 401
