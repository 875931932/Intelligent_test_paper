"""知识域文档生命周期集成测试（P1.3/P1.4）。

覆盖：上传会话哈希校验、解析产物落库、发布后读者可见、撤回后立即不可读、
读者不能上传/发布、重复完成会话被拒绝。
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.storage.minio_storage import ObjectInfo, StoragePreconditionError
from app.db.schema import Base, Organization, User
from app.db.session import get_session
from app.main import app
from app.services.auth_service import hash_password

API = "/api/v1/knowledge"


class FakeStorage:
    """内存对象存储，语义与 StoragePort 一致。"""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def presign_put(self, *, object_key: str, content_type: str, sha256: str, expires_in: int) -> str:
        return f"/fake-storage/{object_key}"

    def head_object(self, object_key: str) -> ObjectInfo | None:
        if object_key not in self.objects:
            return None
        data, content_type = self.objects[object_key]
        return ObjectInfo(
            size=len(data),
            content_type=content_type,
            metadata={},
            etag=hashlib.md5(data).hexdigest(),
        )

    def stream_object(self, object_key: str):
        if object_key not in self.objects:
            raise FileNotFoundError(object_key)
        yield self.objects[object_key][0]

    def finalize_object(self, source_key: str, destination_key: str, source_etag: str) -> None:
        data, content_type = self.objects[source_key]
        if hashlib.md5(data).hexdigest() != source_etag:
            raise StoragePreconditionError("etag mismatch")
        self.objects[destination_key] = (data, content_type)

    def put_bytes(self, object_key: str, content: bytes, content_type: str) -> None:
        self.objects[object_key] = (content, content_type)


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
            ]
        )
        session.flush()
        session.add_all(
            [
                _user("teacher-a", "college-1"),
                _user("teacher-b", "college-1"),
                _user("teacher-c", "college-2"),
                _user("org-admin", "college-1", role="org_admin"),
                _user("platform-admin", "school-1", role="admin"),
            ]
        )
        session.commit()

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    storage = FakeStorage()
    previous_storage = getattr(app.state, "storage", None)
    app.state.storage = storage
    try:
        yield storage
    finally:
        app.dependency_overrides.clear()
        app.state.storage = previous_storage
        Base.metadata.drop_all(engine)
        engine.dispose()


def _user(user_id: str, org_id: str, *, role: str = "teacher") -> User:
    return User(
        id=user_id,
        username=user_id,
        password_hash=hash_password("123456"),
        display_name=user_id,
        role=role,
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


def _upload(a: TestClient, storage: FakeStorage, space_id: str, content: bytes, *, mime: str = "text/markdown", sha: str | None = None):
    digest = sha or hashlib.sha256(content).hexdigest()
    created = a.post(
        f"{API}/spaces/{space_id}/upload-sessions",
        json={
            "filename": "chapter.md",
            "size_bytes": len(content),
            "mime_type": mime,
            "sha256": digest,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    storage.put_bytes(body["object_key"], content, mime)
    return body


def test_upload_parse_publish_withdraw_flow(env):
    storage = env
    a = _login("teacher-a")
    b = _login("teacher-b")

    space = a.post(f"{API}/spaces", json={"name": "数据结构共享"}).json()
    space_id = space["id"]
    a.put(f"{API}/spaces/{space_id}/members/teacher-b", json={"role": "reader"})

    content = "# 栈\n\n栈是后进先出的线性表。\n\n## 队列\n\n队列是先进先出的线性表。\n".encode("utf-8")
    upload = _upload(a, storage, space_id, content)
    completed = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": "第一章 线性结构"},
    )
    assert completed.status_code == 200, completed.text
    done = completed.json()
    assert done["state"] == "processing"

    # 重复完成同一会话明确拒绝，不产生第二个版本。
    duplicate = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={},
    )
    assert duplicate.status_code == 409, duplicate.text

    # 解析任务：进程内执行（正式部署由 knowledge 队列 worker 执行）。
    ran = a.post(f"{API}/spaces/{space_id}/jobs/{done['job_id']}/run")
    assert ran.status_code == 200, ran.text
    assert ran.json()["state"] == "succeeded"

    detail = a.get(f"{API}/spaces/{space_id}/documents/{done['document_id']}").json()
    assert detail["status"] == "ready"
    assert detail["versions"][0]["state"] == "ready"
    assert detail["versions"][0]["parse"]["state"] == "ready"

    # 未发布：读者看不到文档，也不能读取正文，检索不返回草稿。
    assert b.get(f"{API}/spaces/{space_id}/documents").json() == []
    assert b.get(f"{API}/spaces/{space_id}/documents/{done['document_id']}").status_code == 404
    assert (
        b.get(
            f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{done['version_id']}/content"
        ).status_code
        == 404
    )
    assert (
        b.post(f"{API}/search", json={"query": "后进先出", "space_ids": [space_id]}).json()["hits"]
        == []
    )

    # 读者不能上传或发布。
    reader_upload = b.post(
        f"{API}/spaces/{space_id}/upload-sessions",
        json={
            "filename": "x.md",
            "size_bytes": 1,
            "mime_type": "text/markdown",
            "sha256": "a" * 64,
        },
    )
    assert reader_upload.status_code == 403, reader_upload.text
    assert b.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={}).status_code == 403

    published = a.post(
        f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish",
        json={"expected_access_version": 1},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    listed = b.get(f"{API}/spaces/{space_id}/documents").json()
    assert len(listed) == 1 and listed[0]["title"] == "第一章 线性结构"
    content_view = b.get(
        f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{done['version_id']}/content"
    )
    assert content_view.status_code == 200, content_view.text
    joined = "\n".join(block["text"] for block in content_view.json()["blocks"])
    assert "后进先出" in joined

    # 授权检索：成员可召回；同校非成员（其他学院）因 members 范围不可见。
    search = b.post(
        f"{API}/search", json={"query": "后进先出", "space_ids": [space_id]}
    ).json()
    assert search["hits"], search
    hit = search["hits"][0]
    assert hit["document_id"] == done["document_id"]
    assert hit["version_id"] == done["version_id"]
    assert hit["channel"] == "lexical"
    assert "后进先出" in hit["text"]
    c = _login("teacher-c")
    assert (
        c.post(f"{API}/search", json={"query": "后进先出", "space_ids": [space_id]}).json()["hits"]
        == []
    )

    # 读者不能预览草稿版本（该文档只有一个已发布版本，直接用不存在版本探测）。
    assert (
        b.get(f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{'f' * 32}/content").status_code
        == 404
    )

    withdrawn = a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/withdraw")
    assert withdrawn.status_code == 200, withdrawn.text
    assert b.get(f"{API}/spaces/{space_id}/documents").json() == []
    assert (
        b.get(
            f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{done['version_id']}/content"
        ).status_code
        == 404
    )
    assert (
        b.post(f"{API}/search", json={"query": "后进先出", "space_ids": [space_id]}).json()["hits"]
        == []
    )


def test_qa_session_sources_and_revocation(env):
    storage = env
    a = _login("teacher-a")
    b = _login("teacher-b")
    space = a.post(f"{API}/spaces", json={"name": "问答知识库"}).json()
    space_id = space["id"]
    a.put(f"{API}/spaces/{space_id}/members/teacher-b", json={"role": "reader"})

    content = "# 栈\n\n栈是后进先出的线性表，只能在栈顶插入和删除。\n".encode("utf-8")
    upload = _upload(a, storage, space_id, content)
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": "栈与队列"},
    ).json()
    a.post(f"{API}/spaces/{space_id}/jobs/{done['job_id']}/run")
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={})

    created = b.post(
        f"{API}/sessions", json={"selected_space_ids": [space_id], "title": "备课问答"}
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    answered = b.post(
        f"{API}/sessions/{session_id}/ask", json={"question": "栈的插入删除在哪里进行？"}
    )
    assert answered.status_code == 200, answered.text
    messages = answered.json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["sources"], assistant[0]
    source = assistant[0]["sources"][0]
    assert source["version_id"] == done["version_id"]
    assert source["document_id"] == done["document_id"]
    assert "后进先出" in assistant[0]["content"]

    # 其他教师不能使用该空间建会话，也读不到他人会话。
    c = _login("teacher-c")
    assert (
        c.post(f"{API}/sessions", json={"selected_space_ids": [space_id]}).status_code == 404
    )
    assert c.get(f"{API}/sessions/{session_id}").status_code == 404

    # 撤回后历史答案整条隐藏，而不是只删引用。
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/withdraw")
    refreshed = b.get(f"{API}/sessions/{session_id}").json()
    assert [m for m in refreshed["messages"] if m["role"] == "assistant"] == []

    # 无证据问题明确返回未找到，不编造来源。
    empty = b.post(f"{API}/sessions/{session_id}/ask", json={"question": "量子引力如何统一？"})
    assert empty.status_code == 200
    last = empty.json()["messages"][-1]
    assert last["sources"] == []
    assert last["retrieval_snapshot"]["status"] == "insufficient_evidence"


def test_import_fixed_version_to_course(env):
    storage = env
    a = _login("teacher-a")
    b = _login("teacher-b")
    space = a.post(
        f"{API}/spaces", json={"name": "导入知识库", "allow_course_import": True}
    ).json()
    space_id = space["id"]
    a.put(f"{API}/spaces/{space_id}/members/teacher-b", json={"role": "reader"})

    content = "# 图\n\n图中顶点之间的连接称为边。\n".encode("utf-8")
    upload = _upload(a, storage, space_id, content)
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": "图论基础"},
    ).json()
    a.post(f"{API}/spaces/{space_id}/jobs/{done['job_id']}/run")
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={})

    course = a.post("/api/v1/courses", json={"name": "我的数据结构", "slug": "my-ds"}).json()
    imported = a.post(
        f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{done['version_id']}/import-to-course",
        json={"course_id": course["id"]},
    )
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["state"] == "succeeded" and body["idempotent"] is False

    materials = a.get(f"/api/v1/courses/{course['id']}/materials").json()
    assert isinstance(materials, list), materials
    assert len(materials) == 1
    assert materials[0]["logical_name"] == "图论基础"
    assert materials[0]["latest_version"]["version_no"] == 1
    assert materials[0]["latest_version"]["sha256"] == body["sha256"]
    # 资料卡展示来源库与源版本，而不是只留一份无出处副本。
    assert materials[0]["knowledge_origin"] == {
        "space_id": space_id,
        "space_name": "导入知识库",
        "version_no": 1,
        "document_id": done["document_id"],
        "version_id": done["version_id"],
    }

    again = a.post(
        f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{done['version_id']}/import-to-course",
        json={"course_id": course["id"]},
    )
    assert again.status_code == 200 and again.json()["idempotent"] is True
    assert (
        len(a.get(f"/api/v1/courses/{course['id']}/materials").json()) == 1
    )

    # 读者可读源资料，但不是目标课程 owner，不能导入。
    assert (
        b.post(
            f"{API}/spaces/{space_id}/documents/{done['document_id']}/versions/{done['version_id']}/import-to-course",
            json={"course_id": course["id"]},
        ).status_code
        == 404
    )

    # 撤回源资料不回收已交付的课程副本（独立字节，无跨域外键）。
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/withdraw")
    assert (
        len(a.get(f"/api/v1/courses/{course['id']}/materials").json()) == 1
    )


def test_hash_mismatch_is_rejected(env):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "校验测试"}).json()
    space_id = space["id"]

    content = b"# title\n\ncontent\n"
    upload = _upload(a, storage, space_id, content, sha="b" * 64)
    completed = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={},
    )
    assert completed.status_code == 409, completed.text
    assert a.get(f"{API}/spaces/{space_id}/documents").json() == []


def _publish_document(a: TestClient, storage: FakeStorage, space_id: str, content: bytes, title: str) -> dict:
    upload = _upload(a, storage, space_id, content)
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": title},
    ).json()
    a.post(f"{API}/spaces/{space_id}/jobs/{done['job_id']}/run")
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={})
    return done


def test_cross_space_qa_uses_only_authorized_spaces(env):
    storage = env
    a = _login("teacher-a")
    b = _login("teacher-b")
    space_one = a.post(f"{API}/spaces", json={"name": "跨库一"}).json()
    space_two = a.post(f"{API}/spaces", json={"name": "跨库二"}).json()
    for space in (space_one, space_two):
        a.put(f"{API}/spaces/{space['id']}/members/teacher-b", json={"role": "reader"})

    _publish_document(
        a, storage, space_one["id"], "# 树\n\n二叉树每个结点最多有两个子结点。\n".encode("utf-8"), "树结构"
    )
    _publish_document(
        a, storage, space_two["id"], "# 散列\n\n哈希表通过散列函数定位元素。\n".encode("utf-8"), "散列表"
    )

    session = b.post(
        f"{API}/sessions",
        json={"selected_space_ids": [space_one["id"], space_two["id"]], "title": "跨库备课"},
    )
    assert session.status_code == 201, session.text
    assert set(session.json()["selected_space_ids"]) == {space_one["id"], space_two["id"]}

    answered = b.post(
        f"{API}/sessions/{session.json()['id']}/ask",
        json={"question": "二叉树和哈希表的区别"},
    ).json()
    assistant = [m for m in answered["messages"] if m["role"] == "assistant"][-1]
    space_hits = {source["space_id"] for source in assistant["sources"]}
    assert space_hits == {space_one["id"], space_two["id"]}, assistant["sources"]

    # 未授权空间 id 被静默排除，不创建可读会话。
    other = _login("teacher-c").post(f"{API}/spaces", json={"name": "他人库"}).json()
    filtered = b.post(
        f"{API}/sessions",
        json={"selected_space_ids": [space_one["id"], other["id"]]},
    )
    assert filtered.status_code == 201
    assert filtered.json()["selected_space_ids"] == [space_one["id"]]


def test_governance_summary_scope_and_permissions(env):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "治理测试库"}).json()
    space_id = space["id"]
    content = "# 治理\n\n治理测试正文。\n".encode("utf-8")
    upload = _upload(a, storage, space_id, content)
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={},
    ).json()
    a.post(f"{API}/spaces/{space_id}/jobs/{done['job_id']}/run")
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={})

    assert a.get(f"{API}/admin/summary").status_code == 403
    assert _login("teacher-c").get(f"{API}/admin/summary").status_code == 403

    org_admin = _login("org-admin").get(f"{API}/admin/summary")
    assert org_admin.status_code == 200, org_admin.text
    body = org_admin.json()
    assert body["spaces"]["total"] == 1
    assert body["documents"]["published"] == 1
    assert body["index"]["chunks"] >= 1
    assert body["model_cost"]["spend"] == "unknown"
    assert "messages" not in body

    admin = _login("platform-admin").get(f"{API}/admin/summary")
    assert admin.status_code == 200
    assert admin.json()["scope"]["org_id"] is None


def test_upload_session_quota(env):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "限额测试库"}).json()
    space_id = space["id"]

    for index in range(5):
        content = f"# 限额 {index}\n\n内容 {index}\n".encode("utf-8")
        created = a.post(
            f"{API}/spaces/{space_id}/upload-sessions",
            json={
                "filename": f"quota-{index}.md",
                "size_bytes": len(content),
                "mime_type": "text/markdown",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        assert created.status_code == 201, created.text

    blocked = a.post(
        f"{API}/spaces/{space_id}/upload-sessions",
        json={
            "filename": "quota-6.md",
            "size_bytes": 4,
            "mime_type": "text/markdown",
            "sha256": hashlib.sha256(b"test").hexdigest(),
        },
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"

    # 会话过期后不应继续占用限额。
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from app.db.schema import knowledge_upload_sessions

    generator = app.dependency_overrides[get_session]()
    session = next(generator)
    try:
        session.execute(
            update(knowledge_upload_sessions).values(
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
            )
        )
        session.commit()
        content = "# 过期后\n\n新内容\n".encode("utf-8")
        renewed = a.post(
            f"{API}/spaces/{space_id}/upload-sessions",
            json={
                "filename": "after-expiry.md",
                "size_bytes": len(content),
                "mime_type": "text/markdown",
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        assert renewed.status_code == 201, renewed.text
    finally:
        session.close()
        generator.close()


def test_local_storage_put_rejects_overwriting_other_upload(env):
    storage = env
    a = _login("teacher-a")
    b = _login("teacher-b")
    space = a.post(f"{API}/spaces", json={"name": "对象写入隔离"}).json()
    space_id = space["id"]
    content = "# 写入\n\n正文\n".encode("utf-8")
    upload = _upload(a, storage, space_id, content)
    key = upload["object_key"]

    denied = b.put(f"/api/v1/_local-storage/{key}", content=b"evil")
    assert denied.status_code == 404
    assert storage.objects[key][0] == content

    # 已完成会话的旧键不可再覆盖（对象已迁移到版本路径）。
    a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={},
    )
    assert a.put(f"/api/v1/_local-storage/{key}", content=b"evil").status_code == 404
    assert storage.objects[key][0] == content


def test_new_version_keeps_old_version_serving(env):
    storage = env
    a = _login("teacher-a")
    b = _login("teacher-b")
    space = a.post(f"{API}/spaces", json={"name": "连续服务"}).json()
    space_id = space["id"]
    a.put(f"{API}/spaces/{space_id}/members/teacher-b", json={"role": "reader"})

    first = "# 旧版\n\n栈是后进先出的线性表。\n".encode("utf-8")
    upload = _upload(a, storage, space_id, first)
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": "连续服务资料"},
    ).json()
    a.post(f"{API}/spaces/{space_id}/jobs/{done['job_id']}/run")
    a.post(f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={})

    def _hits() -> int:
        result = b.post(
            f"{API}/search", json={"query": "后进先出", "space_ids": [space_id]}
        ).json()
        return len(result["hits"])

    assert _hits() == 1

    # 上传新版：文档必须保持 published，旧版仍可检索。
    second = b"%PDF-1.4 broken"
    upload_two = _upload(a, storage, space_id, second, mime="application/pdf")
    done_two = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload_two['session_id']}/complete",
        json={"document_id": done["document_id"]},
    ).json()
    detail = b.get(f"{API}/spaces/{space_id}/documents/{done['document_id']}").json()
    assert detail["status"] == "published"
    assert _hits() == 1

    # 新版解析失败后仍保持旧版服务，不能退回 draft。
    a.post(f"{API}/spaces/{space_id}/jobs/{done_two['job_id']}/run")
    detail = b.get(f"{API}/spaces/{space_id}/documents/{done['document_id']}").json()
    assert detail["status"] == "published"
    assert detail["published_version_id"] == done["version_id"]
    assert _hits() == 1


def test_fencing_blocks_stale_worker_writes(env):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "租约测试"}).json()
    space_id = space["id"]
    upload = _upload(a, storage, space_id, "# 租约\n\n正文\n".encode("utf-8"))
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={},
    ).json()

    generator = app.dependency_overrides[get_session]()
    session = next(generator)
    try:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import func, select, update

        from app.db.schema import knowledge_blocks, knowledge_document_versions, knowledge_jobs
        from app.infrastructure.tasks import knowledge_tasks
        from app.services import knowledge_document_service
        from app.services.knowledge_space_service import KnowledgeError

        first = knowledge_tasks.claim_knowledge_job(
            session, job_id=done["job_id"], worker_id="worker-old"
        )
        assert first is not None
        session.execute(
            update(knowledge_jobs)
            .where(knowledge_jobs.c.id == done["job_id"])
            .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=5))
        )
        session.commit()
        second = knowledge_tasks.claim_knowledge_job(
            session, job_id=done["job_id"], worker_id="worker-new"
        )
        assert second is not None and second["fencing_token"] > first["fencing_token"]

        with pytest.raises(KnowledgeError) as excinfo:
            knowledge_document_service.run_parse(
                session,
                storage,
                school_id=space["school_id"],
                space_id=space_id,
                version_id=done["version_id"],
                parse_run_id=done["parse_run_id"],
                job_id=done["job_id"],
                worker_id="worker-old",
                fencing_token=first["fencing_token"],
            )
        assert excinfo.value.code == "LEASE_LOST"
        session.rollback()

        version = session.execute(
            select(knowledge_document_versions.c.state).where(
                knowledge_document_versions.c.id == done["version_id"]
            )
        ).scalar_one()
        blocks = session.scalar(
            select(func.count()).select_from(knowledge_blocks).where(
                knowledge_blocks.c.school_id == space["school_id"]
            )
        )
        assert version != "ready"
        assert int(blocks or 0) == 0
    finally:
        session.close()
        generator.close()


def test_revocation_before_commit_discards_blocks(env, monkeypatch):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "撤权提交"}).json()
    space_id = space["id"]
    upload = _upload(a, storage, space_id, "# 撤权\n\n正文内容\n".encode("utf-8"))
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={},
    ).json()

    generator = app.dependency_overrides[get_session]()
    session = next(generator)
    try:
        from app.db.schema import knowledge_blocks, knowledge_document_versions, knowledge_memberships
        from app.infrastructure.tasks import knowledge_tasks
        from app.services import knowledge_document_service

        original_extract = knowledge_document_service._extract_blocks

        def revoke_during_parse(*args, **kwargs):
            # 模拟解析进行中 owner 被降为 reader，随后才走到结果提交。
            session.execute(
                update(knowledge_memberships)
                .where(knowledge_memberships.c.space_id == space_id)
                .values(role="reader")
            )
            session.commit()
            return original_extract(*args, **kwargs)

        monkeypatch.setattr(
            knowledge_document_service, "_extract_blocks", revoke_during_parse
        )

        ok = knowledge_tasks.execute_knowledge_job(
            done["job_id"],
            worker_id="worker-a",
            storage=storage,
            session=session,
        )
        assert ok is False
        session.rollback()

        job = session.execute(
            select(knowledge_tasks.knowledge_jobs.c.state, knowledge_tasks.knowledge_jobs.c.error_code)
            .where(knowledge_tasks.knowledge_jobs.c.id == done["job_id"])
        ).one()
        version = session.execute(
            select(knowledge_document_versions.c.state).where(
                knowledge_document_versions.c.id == done["version_id"]
            )
        ).scalar_one()
        blocks = session.scalar(
            select(func.count()).select_from(knowledge_blocks).where(
                knowledge_blocks.c.parse_run_id == done["parse_run_id"]
            )
        )
        assert job.state == "failed" and job.error_code == "access_revoked"
        assert version != "ready"
        assert int(blocks or 0) == 0
    finally:
        session.close()
        generator.close()

    # owner 已降为 reader，也不存在可直接发布就绪版本的路径。
    published = a.post(
        f"{API}/spaces/{space_id}/documents/{done['document_id']}/publish", json={}
    )
    assert published.status_code in (403, 404)


def test_document_limit_rechecked_on_complete(env, monkeypatch):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "资料上限"}).json()
    space_id = space["id"]
    from app.services import knowledge_document_service

    monkeypatch.setattr(knowledge_document_service, "MAX_DOCUMENTS_PER_SPACE", 1)

    sessions = []
    for index in range(2):
        content = f"# 上限 {index}\n\n正文\n".encode("utf-8")
        sessions.append(_upload(a, storage, space_id, content))

    first = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{sessions[0]['session_id']}/complete",
        json={"title": "第一份"},
    )
    assert first.status_code == 200, first.text
    second = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{sessions[1]['session_id']}/complete",
        json={"title": "第二份"},
    )
    assert second.status_code == 429, second.text
    listed = a.get(f"{API}/spaces/{space_id}/documents").json()
    assert len(listed) == 1

    # 库满后仍可为已有文档申请新版上传并完成，不触发新建限额。
    revision = _upload(a, storage, space_id, "# 修订版\n\n新正文\n".encode("utf-8"))
    document_id = first.json()["document_id"]
    updated = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{revision['session_id']}/complete",
        json={"document_id": document_id},
    )
    assert updated.status_code == 200, updated.text
    assert len(a.get(f"{API}/spaces/{space_id}/documents").json()) == 1


def test_outbox_dispatch_then_worker_writes_blocks(env, monkeypatch):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "链路验证"}).json()
    space_id = space["id"]
    upload = _upload(a, storage, space_id, "# 链路\n\n端到端正文\n".encode("utf-8"))
    done = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": "链路资料"},
    ).json()

    from app.db.schema import knowledge_blocks, knowledge_document_versions, knowledge_jobs
    from app.infrastructure.tasks import celery_app as celery_module
    from app.infrastructure.tasks import knowledge_tasks

    generator = app.dependency_overrides[get_session]()
    session = next(generator)
    published: list[str] = []

    class _Publisher:
        def publish(self, event_type: str, payload: dict) -> None:
            published.append(payload["job_id"])

    monkeypatch.setattr(celery_module, "CeleryPublisher", _Publisher)
    try:
        # 无论即时派发是否已成功，都回到 pending 以验证扫描器本身。
        session.execute(
            update(knowledge_tasks.knowledge_outbox_events)
            .where(knowledge_tasks.knowledge_outbox_events.c.job_id == done["job_id"])
            .values(state="pending", claim_owner=None, claim_until=None)
        )
        session.commit()
        assert knowledge_tasks.dispatch_pending_knowledge_events(session, limit=10) >= 1
        assert done["job_id"] in published

        ok = knowledge_tasks.execute_knowledge_job(
            done["job_id"], worker_id="worker-e2e", storage=storage, session=session
        )
        assert ok is True
        job = session.execute(
            select(knowledge_jobs.c.state).where(knowledge_jobs.c.id == done["job_id"])
        ).scalar_one()
        version = session.execute(
            select(knowledge_document_versions.c.state).where(
                knowledge_document_versions.c.id == done["version_id"]
            )
        ).scalar_one()
        blocks = session.scalar(
            select(func.count()).select_from(knowledge_blocks).where(
                knowledge_blocks.c.parse_run_id == done["parse_run_id"]
            )
        )
        assert job == "succeeded"
        assert version == "ready"
        assert int(blocks or 0) >= 1
    finally:
        session.close()
        generator.close()


def test_parse_job_concurrency_quota(env):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "并发限额库"}).json()
    space_id = space["id"]

    for index in range(5):
        content = f"# 并发 {index}\n\n任务 {index}\n".encode("utf-8")
        upload = _upload(a, storage, space_id, content)
        completed = a.post(
            f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
            json={"title": f"并发资料 {index}"},
        )
        assert completed.status_code == 200, completed.text

    content = "# 并发 5\n\n任务 5\n".encode("utf-8")
    upload = _upload(a, storage, space_id, content)
    blocked = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"title": "并发资料 5"},
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"


def test_unsupported_mime_and_limited_classification(env):
    storage = env
    a = _login("teacher-a")
    space = a.post(f"{API}/spaces", json={"name": "类型限制"}).json()
    space_id = space["id"]

    rejected = a.post(
        f"{API}/spaces/{space_id}/upload-sessions",
        json={
            "filename": "answer.docx",
            "size_bytes": 10,
            "mime_type": "application/x-msdownload",
            "sha256": "c" * 64,
        },
    )
    assert rejected.status_code == 422, rejected.text

    content = b"# x\n\ny\n"
    upload = _upload(a, storage, space_id, content)
    limited = a.post(
        f"{API}/spaces/{space_id}/upload-sessions/{upload['session_id']}/complete",
        json={"classification": "limited_teaching"},
    )
    assert limited.status_code == 200, limited.text
