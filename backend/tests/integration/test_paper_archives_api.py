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

from app.db.schema import Base, User, exam_projects, generated_questions, paper_items, paper_versions
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
        # active_paper_version_id 反指卷表，DROP 前先断开，否则 SQLite FK 拒绝删表
        with factory() as cleanup:
            cleanup.execute(exam_projects.update().values(active_paper_version_id=None))
            cleanup.commit()
        Base.metadata.drop_all(engine)
        engine.dispose()


C = "/api/v1/courses/c1"
ARCHIVE_ENDPOINTS = [
    ("POST", f"{C}/exam-projects/p1/paper-archives", {}),
    ("GET", f"{C}/paper-archives", None),
    ("GET", f"{C}/paper-archives/a1", None),
    ("DELETE", f"{C}/paper-archives/a1", None),
    # P2c：改名 / 改题 / 删题 / 重排 / 存回 / 四件套导出
    ("PATCH", f"{C}/paper-archives/a1", {"name": "x"}),
    ("PATCH", f"{C}/paper-archives/a1/questions/1", {"patch": {"stem": "x"}}),
    ("DELETE", f"{C}/paper-archives/a1/questions/1", None),
    ("PUT", f"{C}/paper-archives/a1/questions/reorder", {"ordered_indices": [1]}),
    ("POST", f"{C}/paper-archives/a1/restore", {}),
    ("GET", f"{C}/paper-archives/a1/export/json", None),
    ("GET", f"{C}/paper-archives/a1/export/student", None),
    ("GET", f"{C}/paper-archives/a1/export/answer-key", None),
    ("GET", f"{C}/paper-archives/a1/export/answer-card", None),
    ("GET", f"{C}/paper-archives/a1/export/bundle", None),
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


def _seed_one_question_paper(factory, cid: str, pid: str) -> None:
    """落一条 1 题的当前卷：生成链路太重，接口测试只关心 HTTP 层。"""
    with factory() as session:
        session.execute(
            generated_questions.insert().values(
                id="gq1", course_id=cid, generation_run_id=None, plan_item_id=None,
                revision_no=1, status="candidate",
                payload={
                    "stem": "什么是栈？", "answer": "后进先出的线性结构",
                    "options": [], "question_type": "short_answer",
                    "difficulty": "medium", "score": 5.0,
                    "explanation": "栈只在栈顶进出元素。",
                },
            )
        )
        session.execute(
            paper_versions.insert().values(
                id="pv1", course_id=cid, exam_project_id=pid,
                version_no=1, status="candidate",
            )
        )
        session.execute(
            paper_items.insert().values(
                id="i1", course_id=cid, paper_version_id="pv1",
                generated_question_id="gq1", display_order=1,
            )
        )
        session.execute(
            exam_projects.update().where(exam_projects.c.id == pid)
            .values(active_paper_version_id="pv1")
        )
        session.commit()


def test_archive_edit_restore_and_export_endpoints(env):
    """P2c：改名 / 改题 / 重排 → 存回试卷区 → 四件套导出，全在 HTTP 层过一遍。"""
    auth, factory = env
    cid = auth.post("/api/v1/courses", json={"name": "数据结构", "slug": "ds"}).json()["id"]
    pid = auth.post(f"/api/v1/courses/{cid}/exam-projects", json={"name": "期末卷"}).json()["id"]
    _seed_one_question_paper(factory, cid, pid)
    aid = auth.post(
        f"/api/v1/courses/{cid}/exam-projects/{pid}/paper-archives", json={"name": "归档卷"}
    ).json()["id"]

    base = f"/api/v1/courses/{cid}/paper-archives/{aid}"

    # 改名：留空被拒（422），改完详情立即可见
    blank = auth.patch(base, json={"name": "   "})
    assert blank.status_code == 422, blank.text
    renamed = auth.patch(base, json={"name": "期末卷·定稿"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "期末卷·定稿"

    # 改题：只动快照；非法答案被拒
    bad = auth.patch(f"{base}/questions/1", json={"patch": {"answer": ""}})
    assert bad.status_code == 422, bad.text
    patched = auth.patch(
        f"{base}/questions/1", json={"patch": {"score": 6.0, "answer": "后进先出"}}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["total_score"] == 6.0
    assert patched.json()["questions"][0]["answer"] == "后进先出"

    # 重排：单题恒等序通过；缺号/重号被拒
    assert auth.put(
        f"{base}/questions/reorder", json={"ordered_indices": [1]}
    ).status_code == 200
    assert auth.put(
        f"{base}/questions/reorder", json={"ordered_indices": [1, 1]}
    ).status_code == 422

    # 存回：新建一版并成为当前卷
    restored = auth.post(f"{base}/restore", json={})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["version_no"] == 2
    assert body["item_count"] == 1
    assert body["exam_project_id"] == pid

    # 四件套导出（数据源是归档快照，不是 paper_versions）
    js = auth.get(f"{base}/export/json")
    assert js.status_code == 200, js.text
    assert js.headers["content-type"].startswith("application/json")
    assert "attachment" in js.headers.get("content-disposition", "")
    assert js.json()["total_questions"] == 1
    assert js.json()["questions"][0]["answer"] == "后进先出"

    student = auth.get(f"{base}/export/student")
    assert student.status_code == 200 and "什么是栈" in student.text

    key = auth.get(f"{base}/export/answer-key")
    assert key.status_code == 200 and "后进先出" in key.text

    card = auth.get(f"{base}/export/answer-card")
    # 答题卡是空卷：只有题号与作答框，不印题干
    assert card.status_code == 200 and "answer-box" in card.text
    assert "什么是栈" not in card.text

    bundle = auth.get(f"{base}/export/bundle")
    assert bundle.status_code == 200, bundle.text
    assert bundle.headers["content-type"] == "application/zip"
    assert bundle.content[:2] == b"PK"

    # 课程隔离：别的课程动不了也导不出这份归档
    other_cid = auth.post("/api/v1/courses", json={"name": "操作系统", "slug": "os"}).json()["id"]
    other = f"/api/v1/courses/{other_cid}/paper-archives/{aid}"
    assert auth.patch(other, json={"name": "x"}).status_code == 404
    assert auth.post(f"{other}/restore", json={}).status_code == 404
    assert auth.get(f"{other}/export/student").status_code == 404
    assert auth.get(f"{other}/export/bundle").status_code == 404
