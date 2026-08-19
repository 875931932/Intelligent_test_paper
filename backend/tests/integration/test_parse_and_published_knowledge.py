"""教师控制台新端点测试：资料解析驱动（parse/poll）与已发布知识读取。"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.document.protocol import (
    DocumentProviderError,
    ParseArtifact,
    ParseProgress,
    ParseRequest,
    ParseState,
    ParseSubmission,
)
from app.config import settings
from app.db.schema import Base
from app.db.session import get_session
from app.main import app
from app.services import parse_service


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        test_client = TestClient(app)
        test_client.app.state.test_engine = engine
        yield test_client
    finally:
        app.dependency_overrides.clear()
        if hasattr(app.state, "storage"):
            delattr(app.state, "storage")
        Base.metadata.drop_all(engine)
        engine.dispose()


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def presign_put(self, *, object_key, content_type, sha256, expires_in):
        return f"https://storage.invalid/{object_key}"

    def head_object(self, object_key):
        value = self.objects.get(object_key)
        return {**value, "etag": '"etag-1"'} if value else None

    def stream_object(self, object_key) -> Iterator[bytes]:
        yield self.objects[object_key]["body"]

    def finalize_object(self, source_key, destination_key, source_etag):
        self.objects[destination_key] = dict(self.objects[source_key])

    def put_bytes(self, object_key: str, content: bytes, content_type: str) -> None:
        self.objects[object_key] = {"body": content, "etag": '"put-etag"'}


class FakeMineruParser:
    """脚本化 MinerU 假解析器：submit 记录请求，poll 按 states 轮转，fetch 出 ZIP。"""

    def __init__(self, states: list[ParseState]):
        self.states = list(states)
        self.submitted: list[ParseRequest] = []
        self.poll_count = 0

    async def submit(self, request: ParseRequest) -> ParseSubmission:
        self.submitted.append(request)
        return ParseSubmission(provider_batch_id="batch-1")

    async def poll(self, provider_batch_id: str) -> ParseProgress:
        index = min(self.poll_count, len(self.states) - 1)
        state = self.states[index]
        self.poll_count += 1
        return ParseProgress(
            state=state,
            result_url="https://artifact.invalid/full.zip" if state == ParseState.DONE else None,
            trace_id="trace-1",
        )

    async def fetch(self, provider_batch_id: str) -> ParseArtifact:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            content = [
                {"type": "title", "text": "第一章 绪论", "text_level": 1, "page_idx": 0},
                {"type": "text", "text": "检索增强生成把检索与生成结合。", "page_idx": 0},
            ]
            archive.writestr("full.md", "# 第一章 绪论")
            archive.writestr("content_list.json", json.dumps(content, ensure_ascii=False))
        return ParseArtifact(provider_batch_id=provider_batch_id, content=buffer.getvalue())


def _course(client, slug="parse-course"):
    response = client.post("/api/v1/courses", json={"name": slug, "slug": slug})
    assert response.status_code == 201
    return response.json()


def _upload_material(client, course_id, *, material_type="teaching_syllabus", filename="lesson.pdf", body=b"pdf-bytes"):
    import hashlib

    storage = client.app.state.storage
    created = client.post(
        f"/api/v1/courses/{course_id}/upload-sessions",
        json={
            "filename": filename,
            "material_type": material_type,
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "mime_type": "application/pdf",
        },
    )
    assert created.status_code == 201
    session_payload = created.json()
    storage.objects[session_payload["object_key"]] = {
        "size": len(body),
        "content_type": "application/pdf",
        "metadata": {},
        "body": body,
        "etag": '"etag-1"',
    }
    completed = client.post(
        f"/api/v1/courses/{course_id}/upload-sessions/{session_payload['session_id']}/complete"
    )
    assert completed.status_code == 200
    return completed.json()


def test_parse_submit_and_poll_until_ready_stores_blocks(client, monkeypatch):
    monkeypatch.setattr(settings, "mineru_api_token", "mineru-test-token")
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    version = _upload_material(client, course["id"])
    material_id = version["material_id"]

    parser = FakeMineruParser([ParseState.RUNNING, ParseState.DONE])
    monkeypatch.setattr(parse_service, "build_mineru_client", lambda: parser)

    started = client.post(f"/api/v1/courses/{course['id']}/materials/{material_id}/parse")
    assert started.status_code == 202
    assert started.json()["status"] == "submitted"
    assert started.json()["reused"] is False
    assert len(parser.submitted) == 1

    first = client.post(f"/api/v1/courses/{course['id']}/materials/{material_id}/parse/poll")
    assert first.status_code == 200
    assert first.json()["status"] == "running"

    # MinerU DONE 是内部瞬态：同一次推进即拉取产物、落块并置 ready
    second = client.post(f"/api/v1/courses/{course['id']}/materials/{material_id}/parse/poll")
    assert second.json()["status"] == "ready"
    # ready 后幂等空转
    again = client.post(f"/api/v1/courses/{course['id']}/materials/{material_id}/parse/poll")
    assert again.json()["status"] == "ready"

    listed = client.get(f"/api/v1/courses/{course['id']}/materials")
    item = next(m for m in listed.json() if m["id"] == material_id)
    assert item["parse_status"]["status"] == "ready"

    with client.app.state.test_engine.begin() as connection:
        blocks = connection.execute(text("SELECT COUNT(*) FROM content_blocks")).scalar_one()
    assert blocks >= 2


def test_parse_reuses_ready_result_for_same_hash(client, monkeypatch):
    monkeypatch.setattr(settings, "mineru_api_token", "mineru-test-token")
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    first = _upload_material(client, course["id"], filename="one.pdf")
    second = _upload_material(client, course["id"], filename="two.pdf")

    parser = FakeMineruParser([ParseState.DONE])
    monkeypatch.setattr(parse_service, "build_mineru_client", lambda: parser)

    started = client.post(f"/api/v1/courses/{course['id']}/materials/{first['material_id']}/parse")
    assert started.json()["reused"] is False
    polled = client.post(f"/api/v1/courses/{course['id']}/materials/{first['material_id']}/parse/poll")
    assert polled.json()["status"] == "ready"

    reused = client.post(f"/api/v1/courses/{course['id']}/materials/{second['material_id']}/parse")
    assert reused.status_code == 202
    assert reused.json()["reused"] is True
    assert reused.json()["status"] == "ready"
    # 复用不产生第二次 MinerU 提交
    assert len(parser.submitted) == 1


def test_parse_poll_without_run_returns_404(client, monkeypatch):
    monkeypatch.setattr(settings, "mineru_api_token", "mineru-test-token")
    client.app.state.storage = FakeStorage()
    course = _course(client)
    version = _upload_material(client, course["id"])

    response = client.post(f"/api/v1/courses/{course['id']}/materials/{version['material_id']}/parse/poll")

    assert response.status_code == 404


def test_published_knowledge_requires_published_catalog(client):
    course = _course(client)

    response = client.get(f"/api/v1/courses/{course['id']}/published-knowledge")

    assert response.status_code == 404


def test_published_knowledge_returns_cards_units_and_exam_points(client):
    """最小已发布目录（SQL 直插）→ 端点聚合出命题输入视图。"""
    course = _course(client, slug="knowledge-course")
    engine = client.app.state.test_engine
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO framework_versions (id, course_id, version_no, status, payload) VALUES ('fw-1', :course, 1, 'published', '{}')"), {"course": course["id"]})
        connection.execute(
            text(
                "INSERT INTO exam_points (id, course_id, framework_version_id, anchor_key, code, title,"
                " assessment_requirement, weight_value, weight_source, weight_group_id, priority,"
                " cognitive_targets, assessment_orientations, allowed_question_types,"
                " operational_detail_policy, scope_boundary, required_evidence_roles, retrieval_intent,"
                " teaching_anchor_keys, status)"
                " VALUES ('ep-1', :course, 'fw-1', 'chapter-1', 'EP1', '检索增强', '掌握RAG', 25,"
                " 'assessment_syllabus', 'g1', 'normal', '[\"understand\"]', '[]', '[]',"
                " 'supporting_only', '{}', '[]', 'intent', '[]', 'confirmed')"
            ),
            {"course": course["id"]},
        )
        connection.execute(text("INSERT INTO organization_runs (id, course_id, status, input_snapshot) VALUES ('run-1', :course, 'published', '{}')"), {"course": course["id"]})
        connection.execute(text("INSERT INTO knowledge_catalog_versions (id, course_id, organization_run_id, framework_version_id, version_no, status, payload) VALUES ('cat-1', :course, 'run-1', 'fw-1', 1, 'published', '{}')"), {"course": course["id"]})
        connection.execute(text("INSERT INTO content_domains (id, course_id, catalog_version_id, level, framework_anchor_key, code, name, status) VALUES ('dom-1', :course, 'cat-1', 1, 'chapter-1', 'd1', '内容域', 'active')"), {"course": course["id"]})
        connection.execute(text("INSERT INTO assessment_units (id, course_id, catalog_version_id, content_domain_id, exam_point_id, code, title, performance_statement, scope_boundary, status) VALUES ('unit-1', :course, 'cat-1', 'dom-1', 'ep-1', 'U1', 'RAG流程', '说明RAG', '{}', 'active')"), {"course": course["id"]})
        connection.execute(
            text(
                "INSERT INTO knowledge_cards (id, course_id, catalog_version_id, assessment_unit_id, name,"
                " performance_statement, assessable_content, scope_boundary, cognitive_targets,"
                " allowed_question_types, importance, concept_cluster, answer_proposition, prompt_material,"
                " content_hash, status, version)"
                " VALUES ('card-1', :course, 'cat-1', 'unit-1', 'RAG基本流程', '说明RAG流程',"
                " '[\"检索、上下文构造和生成\"]', '{}', '[\"understand\"]', '[\"single_choice\"]', 2,"
                " 'RAG流程组成', '检索、上下文构造和生成', '[]', 'hash-1', 'active', 1)"
            ),
            {"course": course["id"]},
        )

    response = client.get(f"/api/v1/courses/{course['id']}/published-knowledge")

    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog_version_id"] == "cat-1"
    assert [point["code"] for point in payload["exam_points"]] == ["EP1"]
    assert payload["exam_points"][0]["weight_value"] == 25
    unit = payload["units"][0]
    assert unit["unit_id"] == "unit-1"
    assert unit["exam_point_id"] == "ep-1"
    assert unit["anchor_key"] == "chapter-1"
    assert unit["card_ids"] == ["card-1"]
    card = payload["knowledge_cards"]["card-1"]
    assert card["assessable_content"] == ["检索、上下文构造和生成"]
    assert card["concept_cluster"] == "RAG流程组成"
    assert card["answer_boundary"] == "检索、上下文构造和生成"
