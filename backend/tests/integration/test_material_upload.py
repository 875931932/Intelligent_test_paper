import hashlib
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base
from app.db.session import get_session
from app.adapters.model.embedding_gateway import OpenAICompatibleEmbeddingGateway
from app.config import settings
from app.main import app
from app.api.v1 import materials as materials_api


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
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_course_create_and_list_are_scoped_to_development_owner(client):

    created = client.post(
        "/api/v1/courses",
        json={"name": "数据结构", "slug": "data-structures", "description": "核心课程"},
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["owner_id"] == "owner-dev"
    assert payload["slug"] == "data-structures"
    listed = client.get("/api/v1/courses")
    assert listed.status_code == 200
    assert [course["id"] for course in listed.json()] == [payload["id"]]


@pytest.mark.parametrize(
    ("missing_attribute", "expected_detail"),
    [
        ("organization_embedder", "embedding client is not configured"),
        ("exam_point_evidence_classifier", "semantic classifier is not configured"),
        ("exam_point_knowledge_consolidator", "knowledge consolidator is not configured"),
    ],
)
def test_organization_run_requires_all_semantic_dependencies(client, missing_attribute, expected_detail):
    attributes = {
        "organization_embedder": object(),
        "exam_point_evidence_classifier": object(),
        "exam_point_knowledge_consolidator": object(),
    }
    for name, value in attributes.items():
        setattr(client.app.state, name, value)
    delattr(client.app.state, missing_attribute)
    try:
        response = client.post(
            "/api/v1/courses/not-needed/organization-runs",
            json={"material_version_ids": ["material-v1"]},
        )
    finally:
        for name in attributes:
            if hasattr(client.app.state, name):
                delattr(client.app.state, name)

    assert response.status_code == 503
    assert response.json()["detail"] == expected_detail


def test_organization_dependency_lazily_builds_embedding_client(client, monkeypatch):
    monkeypatch.setattr(settings, "embedding_api_key", "embedding-test-key")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.invalid/v1")
    monkeypatch.setattr(settings, "embedding_model", "embedding-model")
    app.state.exam_point_evidence_classifier = object()
    app.state.exam_point_knowledge_consolidator = object()
    if hasattr(app.state, "organization_embedder"):
        del app.state.organization_embedder
    try:
        response = client.post(
            "/api/v1/courses/not-needed/organization-runs",
            json={"material_version_ids": ["material-v1"]},
        )
        assert response.status_code == 404
        assert isinstance(app.state.organization_embedder, OpenAICompatibleEmbeddingGateway)
    finally:
        for name in ("organization_embedder", "exam_point_evidence_classifier", "exam_point_knowledge_consolidator"):
            if hasattr(app.state, name):
                delattr(app.state, name)


@pytest.mark.parametrize("missing_setting", ["deepseek_api_key", "deepseek_base_url", "deepseek_model"])
@pytest.mark.parametrize(
    ("injected_attribute", "missing_attribute", "expected_detail"),
    [
        ("exam_point_knowledge_consolidator", "exam_point_evidence_classifier", "semantic classifier is not configured"),
        ("exam_point_evidence_classifier", "exam_point_knowledge_consolidator", "knowledge consolidator is not configured"),
    ],
)
def test_organization_semantic_dependencies_require_complete_deepseek_config(
    client,
    monkeypatch,
    missing_setting,
    injected_attribute,
    missing_attribute,
    expected_detail,
):
    monkeypatch.setattr(settings, "deepseek_api_key", "configured-test-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://deepseek.invalid/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, missing_setting, "")
    app.state.organization_embedder = object()
    setattr(app.state, injected_attribute, object())
    if hasattr(app.state, missing_attribute):
        delattr(app.state, missing_attribute)
    try:
        response = client.post(
            "/api/v1/courses/not-needed/organization-runs",
            json={"material_version_ids": ["material-v1"]},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == expected_detail
    finally:
        for name in (
            "organization_embedder",
            "exam_point_evidence_classifier",
            "exam_point_knowledge_consolidator",
            "semantic_json_client",
        ):
            if hasattr(app.state, name):
                delattr(app.state, name)


def test_course_crud_hides_course_owned_by_another_teacher(client):
    with client.app.state.test_engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, display_name, role) VALUES ('owner-other', 'Other', 'teacher')"))
        connection.execute(text("INSERT INTO courses (id, owner_id, name, slug) VALUES ('course-other', 'owner-other', 'Hidden', 'hidden')"))

    assert client.get("/api/v1/courses/course-other").status_code == 404
    assert client.patch("/api/v1/courses/course-other", json={"name": "Changed"}).status_code == 404
    assert client.get("/api/v1/courses").json() == []


def test_course_patch_can_change_name_slug_and_clear_description(client):
    course = client.post("/api/v1/courses", json={"name": "Old", "slug": "old", "description": "remove me"}).json()

    response = client.patch(f"/api/v1/courses/{course['id']}", json={"name": "New", "slug": "new", "description": None})

    assert response.status_code == 200
    assert response.json() == {**course, "name": "New", "slug": "new", "description": None}


def test_duplicate_course_slug_returns_conflict(client):
    assert client.post("/api/v1/courses", json={"name": "First", "slug": "same"}).status_code == 201

    response = client.post("/api/v1/courses", json={"name": "Second", "slug": "same"})

    assert response.status_code == 409


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.presign_calls = []
        self.head_calls = []
        self.stream_calls = []
        self.finalize_calls = []
        self.presign_error = None

    def presign_put(self, *, object_key, content_type, sha256, expires_in):
        if self.presign_error is not None:
            raise self.presign_error
        self.presign_calls.append((object_key, content_type, sha256, expires_in))
        return f"https://storage.invalid/{object_key}?signature=fake"

    def head_object(self, object_key):
        self.head_calls.append(object_key)
        value = self.objects.get(object_key)
        if value is None:
            return None
        return {**value, "etag": value.get("etag", '"etag-1"')}

    def stream_object(self, object_key):
        self.stream_calls.append(object_key)
        yield self.objects[object_key]["body"]

    def finalize_object(self, source_key, destination_key, source_etag):
        self.finalize_calls.append((source_key, destination_key, source_etag))
        source = self.objects[source_key]
        if source.get("etag", '"etag-1"') != source_etag:
            raise RuntimeError("copy source precondition failed")
        self.objects[destination_key] = {**source, "etag": '"final-etag"'}


def _course(client, slug="course-a"):
    response = client.post("/api/v1/courses", json={"name": slug, "slug": slug})
    assert response.status_code == 201
    return response.json()


def _upload_request(filename="lesson.pdf", mime_type="application/pdf", size_bytes=3, sha256="a" * 64):
    return {"filename": filename, "material_type": "teaching_syllabus", "size_bytes": size_bytes, "sha256": sha256, "mime_type": mime_type}


def test_upload_session_presigns_course_and_session_scoped_key_without_secrets(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=_upload_request())

    assert response.status_code == 201
    payload = response.json()
    assert course["id"] in payload["object_key"]
    assert payload["session_id"] in payload["object_key"]
    assert payload["headers"] == {"Content-Type": "application/pdf", "x-amz-meta-sha256": "a" * 64}
    assert "minio-dev" not in response.text
    assert "secret" not in response.text.lower()
    assert storage.presign_calls


@pytest.mark.parametrize(
    "payload",
    [
        _upload_request(filename="../escape.pdf"),
        _upload_request(filename="notes.html", mime_type="text/html"),
        _upload_request(filename="notes.pdf", mime_type="text/plain"),
        _upload_request(size_bytes=0),
        _upload_request(sha256="bad"),
    ],
)
def test_invalid_upload_metadata_is_rejected_before_presign(client, payload):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client, slug=f"course-{len(payload['filename'])}-{payload['size_bytes']}")

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=payload)

    assert response.status_code == 422
    assert storage.presign_calls == []


def test_complete_stages_one_material_version_without_background_records_and_is_idempotent(client):
    import hashlib

    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    session_response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request)
    session = session_response.json()
    storage.objects[session["object_key"]] = {"size": len(body), "content_type": "application/pdf", "metadata": {"sha256": request["sha256"]}, "body": body}

    first = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{session['session_id']}/complete")
    second = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{session['session_id']}/complete")

    assert first.status_code == 200
    assert first.json()["status"] == "staged"
    assert second.json()["id"] == first.json()["id"]
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM materials")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM material_versions")).scalar_one() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM document_parse_runs")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM task_runs")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM outbox_events")).scalar_one() == 0


def test_concurrent_complete_requests_create_one_staged_version_and_return_the_same_result(tmp_path):
    """Independent API requests race only after both validated the same object."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-complete.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_session():
        with factory() as session:
            yield session

    class BarrierStorage(FakeStorage):
        def __init__(self):
            super().__init__()
            self.validated = Barrier(2)

        def head_object(self, object_key):
            self.validated.wait(timeout=5)
            return super().head_object(object_key)

    storage = BarrierStorage()
    app.dependency_overrides[get_session] = override_session
    app.state.storage = storage
    try:
        with TestClient(app) as setup_client:
            course = _course(setup_client)
            body = b"pdf"
            request = _upload_request(size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            upload = setup_client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
        storage.objects[upload["object_key"]] = {
            "size": len(body), "content_type": "application/pdf", "metadata": {"sha256": request["sha256"]}, "body": body,
        }

        def complete_from_independent_client():
            with TestClient(app, raise_server_exceptions=False) as request_client:
                response = request_client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")
                return response.status_code, response.json() if response.status_code == 200 else response.text

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: complete_from_independent_client(), range(2)))

        assert [status_code for status_code, _ in results] == [200, 200]
        assert results[0][1]["id"] == results[1][1]["id"]
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM materials")).scalar_one() == 1
            assert connection.execute(text("SELECT COUNT(*) FROM material_versions")).scalar_one() == 1
    finally:
        app.dependency_overrides.clear()
        del app.state.storage
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize(
    "object_data",
    [
        None,
        {"size": 4, "content_type": "application/pdf", "metadata": {"sha256": "a" * 64}, "body": b"nope"},
        {"size": 3, "content_type": "text/plain", "metadata": {"sha256": "a" * 64}, "body": b"pdf"},
        {"size": 3, "content_type": "application/pdf", "metadata": {"sha256": "b" * 64}, "body": b"pdf"},
    ],
)
def test_complete_rejects_missing_or_mismatched_object_without_material_side_effect(client, object_data):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    session = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=_upload_request()).json()
    if object_data is not None:
        storage.objects[session["object_key"]] = object_data

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{session['session_id']}/complete")

    assert response.status_code == 409
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM materials")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM material_versions")).scalar_one() == 0


def test_complete_hashes_stream_when_object_metadata_is_absent(client):
    import hashlib

    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[upload["object_key"]] = {"size": 3, "content_type": "application/pdf", "metadata": {}, "body": body}

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")

    assert response.status_code == 200
    assert response.json()["sha256"] == request["sha256"]


def test_complete_rejects_false_sha_metadata_when_object_body_does_not_match(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    declared = hashlib.sha256(b"expected").hexdigest()
    upload = client.post(
        f"/api/v1/courses/{course['id']}/upload-sessions",
        json=_upload_request(size_bytes=5, sha256=declared),
    ).json()
    storage.objects[upload["object_key"]] = {
        "size": 5,
        "content_type": "application/pdf",
        "metadata": {"sha256": declared},
        "body": b"wrong",
    }

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")

    assert response.status_code == 409
    assert storage.stream_calls == [upload["object_key"]]
    assert storage.finalize_calls == []
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM materials")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM material_versions")).scalar_one() == 0


def test_complete_finalizes_to_unpresigned_immutable_key_and_hides_storage_layout(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {"sha256": request["sha256"]}, "body": body,
    }

    completed = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")

    assert completed.status_code == 200
    assert "object_key" not in completed.json()
    source_key, final_key, etag = storage.finalize_calls[0]
    assert source_key == upload["object_key"]
    assert final_key != source_key
    assert "/materials/" in final_key and "/versions/" in final_key
    assert final_key not in completed.text
    storage.objects[source_key]["body"] = b"overwritten"
    assert storage.objects[final_key]["body"] == body
    material = client.get(f"/api/v1/courses/{course['id']}/materials/{completed.json()['material_id']}")
    assert "object_key" not in material.text


def test_complete_rejects_when_temp_object_etag_changes_before_finalize(client):
    class ChangingStorage(FakeStorage):
        def stream_object(self, object_key):
            yield from super().stream_object(object_key)
            self.objects[object_key]["etag"] = '"etag-2"'

    storage = ChangingStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body, "etag": '"etag-1"',
    }

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")

    assert response.status_code in {409, 503}
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM materials")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM material_versions")).scalar_one() == 0
        assert connection.execute(
            text("SELECT status FROM upload_sessions WHERE id=:id"), {"id": upload["session_id"]}
        ).scalar_one() == "pending"


def test_expired_pending_session_rejects_before_any_storage_io(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=_upload_request()).json()
    with client.app.state.test_engine.begin() as connection:
        connection.execute(
            text("UPDATE upload_sessions SET expires_at=:expired WHERE id=:id"),
            {"expired": datetime.now(UTC) - timedelta(minutes=1), "id": upload["session_id"]},
        )

    response = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")

    assert response.status_code == 410
    assert storage.head_calls == []
    assert storage.stream_calls == []
    assert storage.finalize_calls == []
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM materials")).scalar_one() == 0


def test_completed_session_remains_idempotent_after_expiry_without_storage_io(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    first = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete").json()
    with client.app.state.test_engine.begin() as connection:
        connection.execute(
            text("UPDATE upload_sessions SET expires_at=:expired WHERE id=:id"),
            {"expired": datetime.now(UTC) - timedelta(minutes=1), "id": upload["session_id"]},
        )
        assert connection.execute(
            text("SELECT completed_at IS NOT NULL FROM upload_sessions WHERE id=:id"), {"id": upload["session_id"]}
        ).scalar_one() == 1
    storage.head_calls.clear()
    storage.stream_calls.clear()
    storage.finalize_calls.clear()

    repeated = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete")

    assert repeated.status_code == 200
    assert repeated.json()["id"] == first["id"]
    assert storage.head_calls == []
    assert storage.stream_calls == []
    assert storage.finalize_calls == []


def test_presign_failure_is_sanitized_and_does_not_persist_upload_session(client):
    storage = FakeStorage()
    storage.presign_error = RuntimeError("secret-key=do-not-leak")
    client.app.state.storage = storage
    course = _course(client)

    with TestClient(app, raise_server_exceptions=False) as request_client:
        response = request_client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=_upload_request())

    assert response.status_code == 503
    assert "do-not-leak" not in response.text
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM upload_sessions")).scalar_one() == 0


def test_storage_initialization_failure_is_sanitized_as_service_unavailable(client, monkeypatch):
    if hasattr(app.state, "storage"):
        del app.state.storage
    course = _course(client)

    def fail_storage(**_kwargs):
        raise RuntimeError("secret-key=do-not-leak")

    monkeypatch.setattr(materials_api, "MinioStorage", fail_storage)
    with TestClient(app, raise_server_exceptions=False) as request_client:
        response = request_client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=_upload_request())

    assert response.status_code == 503
    assert "do-not-leak" not in response.text


def test_active_same_name_requires_explicit_version_target(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    first_upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[first_upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    assert client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{first_upload['session_id']}/complete").status_code == 200

    conflict = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request)

    assert conflict.status_code == 409
    assert len(storage.presign_calls) == 1


def test_deleted_same_name_is_restored_as_next_version(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    first_upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[first_upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    first = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{first_upload['session_id']}/complete").json()
    assert client.delete(f"/api/v1/courses/{course['id']}/materials/{first['material_id']}").status_code == 204

    second_upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[second_upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    second = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{second_upload['session_id']}/complete")

    assert second.status_code == 200
    assert second.json()["material_id"] == first["material_id"]
    assert second.json()["version_no"] == 2
    material = client.get(f"/api/v1/courses/{course['id']}/materials/{first['material_id']}").json()
    assert material["status"] == "staged"


def test_existing_material_id_creates_next_version_in_same_course(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    first_upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[first_upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    first = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{first_upload['session_id']}/complete").json()
    request["existing_material_id"] = first["material_id"]

    second_upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[second_upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    second = client.post(f"/api/v1/courses/{course['id']}/upload-sessions/{second_upload['session_id']}/complete")

    assert second.status_code == 200
    assert second.json()["material_id"] == first["material_id"]
    assert second.json()["version_no"] == 2


def test_existing_material_id_from_another_course_is_hidden_before_presign(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course_a = _course(client, "course-a")
    course_b = _course(client, "course-b")
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    upload = client.post(f"/api/v1/courses/{course_a['id']}/upload-sessions", json=request).json()
    storage.objects[upload["object_key"]] = {
        "size": 3, "content_type": "application/pdf", "metadata": {}, "body": body,
    }
    material_id = client.post(f"/api/v1/courses/{course_a['id']}/upload-sessions/{upload['session_id']}/complete").json()["material_id"]
    presign_count = len(storage.presign_calls)
    request["existing_material_id"] = material_id

    response = client.post(f"/api/v1/courses/{course_b['id']}/upload-sessions", json=request)

    assert response.status_code == 404
    assert len(storage.presign_calls) == presign_count


@pytest.mark.parametrize("payload", [{"name": None}, {"slug": None}])
def test_course_patch_rejects_null_required_fields(client, payload):
    course = _course(client)

    response = client.patch(f"/api/v1/courses/{course['id']}", json=payload)

    assert response.status_code == 422


def test_filename_is_normalized_to_nfc_and_rejects_format_characters(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client)
    decomposed = unicodedata.normalize("NFD", "课件.pdf")

    accepted = client.post(
        f"/api/v1/courses/{course['id']}/upload-sessions",
        json=_upload_request(filename=decomposed),
    )
    rejected = client.post(
        f"/api/v1/courses/{course['id']}/upload-sessions",
        json=_upload_request(filename="safe\u202efile.pdf"),
    )

    assert accepted.status_code == 201
    assert unicodedata.normalize("NFC", decomposed) in accepted.json()["object_key"]
    assert rejected.status_code == 422


def test_material_reads_and_completion_are_course_scoped_and_delete_is_retained(client):
    import hashlib

    storage = FakeStorage()
    client.app.state.storage = storage
    course_a = _course(client, "course-a")
    course_b = _course(client, "course-b")
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    session = client.post(f"/api/v1/courses/{course_a['id']}/upload-sessions", json=request).json()
    storage.objects[session["object_key"]] = {"size": 3, "content_type": "application/pdf", "metadata": {"sha256": request["sha256"]}, "body": body}
    material = client.post(f"/api/v1/courses/{course_a['id']}/upload-sessions/{session['session_id']}/complete").json()

    assert client.post(f"/api/v1/courses/{course_b['id']}/upload-sessions/{session['session_id']}/complete").status_code == 404
    assert client.get(f"/api/v1/courses/{course_b['id']}/materials/{material['material_id']}").status_code == 404
    assert client.delete(f"/api/v1/courses/{course_a['id']}/materials/{material['material_id']}").status_code == 204
    assert client.get(f"/api/v1/courses/{course_a['id']}/materials").json() == []
    retained = client.get(f"/api/v1/courses/{course_a['id']}/materials?include_deleted=true")
    assert retained.status_code == 200
    assert retained.json()[0]["status"] == "deleted"


def test_deleting_material_invalidates_direct_evidence_and_current_index_membership(client):
    storage = FakeStorage()
    client.app.state.storage = storage
    course = _course(client, "delete-evidence")
    body = b"pdf"
    request = _upload_request(size_bytes=3, sha256=hashlib.sha256(body).hexdigest())
    upload = client.post(f"/api/v1/courses/{course['id']}/upload-sessions", json=request).json()
    storage.objects[upload["object_key"]] = {
        "size": 3,
        "content_type": "application/pdf",
        "metadata": {},
        "body": body,
    }
    version = client.post(
        f"/api/v1/courses/{course['id']}/upload-sessions/{upload['session_id']}/complete"
    ).json()
    values = {
        "course": course["id"],
        "material": version["material_id"],
        "version": version["id"],
    }
    with client.app.state.test_engine.begin() as connection:
        connection.execute(text("INSERT INTO framework_build_runs (id, course_id, status, input_snapshot) VALUES ('fr', :course, 'published', '{}')"), values)
        connection.execute(text("INSERT INTO framework_versions (id, course_id, framework_build_run_id, version_no, status, payload) VALUES ('fv', :course, 'fr', 1, 'published', '{\"anchors\":[{\"key\":\"core\"}]}')"), values)
        connection.execute(text("""
            INSERT INTO exam_points (
                id, course_id, framework_version_id, anchor_key, code, title,
                assessment_requirement, weight_value, weight_source, weight_group_id,
                priority, cognitive_targets, assessment_orientations, allowed_question_types,
                operational_detail_policy, scope_boundary, required_evidence_roles,
                retrieval_intent, teaching_anchor_keys, status
            ) VALUES (
                'ep', :course, 'fv', 'core', 'EP-1', '核心', '理解核心', 100,
                'assessment_syllabus', 'core', 'normal', '[]', '[]', '[]',
                'supporting_only', '{}', '[\"answer_or_rubric_basis\"]', '检索核心', '[]', 'confirmed'
            )
        """), values)
        connection.execute(text("INSERT INTO organization_runs (id, course_id, framework_version_id, status, input_snapshot) VALUES ('org', :course, 'fv', 'published', '{}')"), values)
        connection.execute(text("INSERT INTO evidence_chunks (id, course_id, organization_run_id, material_version_id, chunk_index, content, content_hash) VALUES ('ev', :course, 'org', :version, 0, '事实', :hash)"), {**values, "hash": "e" * 64})
        connection.execute(text("INSERT INTO exam_point_evidence_links (id, course_id, organization_run_id, exam_point_id, evidence_chunk_id, relevance_class, support_claim, evidence_role, confidence, status) VALUES ('epl', :course, 'org', 'ep', 'ev', 'direct', '事实', 'answer_or_rubric_basis', 95, 'published')"), values)
        connection.execute(
            text("INSERT INTO knowledge_catalog_versions (id, course_id, organization_run_id, framework_version_id, version_no, status, payload) VALUES ('cat', :course, 'org', 'fv', 1, 'published', :payload)"),
            {**values, "payload": '{"historical":true}'},
        )
        connection.execute(text("INSERT INTO content_domains (id, course_id, catalog_version_id, level, framework_anchor_key, code, name, status) VALUES ('domain', :course, 'cat', 1, 'core', 'core', '核心', 'active')"), values)
        connection.execute(text("INSERT INTO assessment_units (id, course_id, catalog_version_id, content_domain_id, exam_point_id, code, title, performance_statement, scope_boundary, status) VALUES ('unit', :course, 'cat', 'domain', 'ep', 'U1', '核心', '理解核心', '{}', 'active')"), values)
        connection.execute(text("INSERT INTO knowledge_cards (id, course_id, catalog_version_id, assessment_unit_id, name, performance_statement, assessable_content, scope_boundary, cognitive_targets, allowed_question_types, importance, content_hash, status, version) VALUES ('card', :course, 'cat', 'unit', '核心事实', '说明事实', '[\"事实\"]', '{}', '[]', '[]', 1, :hash, 'active', 1)"), {**values, "hash": "f" * 64})
        connection.execute(text("INSERT INTO knowledge_evidence_links (id, course_id, knowledge_card_id, evidence_chunk_id, evidence_role, confidence, teacher_confirmed, lifecycle_status) VALUES ('kel', :course, 'card', 'ev', 'answer_or_rubric_basis', 95, 1, 'active')"), values)
        connection.execute(text("INSERT INTO index_versions (id, course_id, catalog_version_id, version_no, status) VALUES ('idx', :course, 'cat', 1, 'published')"), values)
        connection.execute(text("INSERT INTO index_memberships (id, course_id, index_version_id, knowledge_card_id) VALUES ('member', :course, 'idx', 'card')"), values)

    response = client.delete(f"/api/v1/courses/{course['id']}/materials/{version['material_id']}")

    assert response.status_code == 204
    with client.app.state.test_engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM exam_point_evidence_links WHERE id='epl'")).scalar_one() == "source_deleted"
        assert connection.execute(text("SELECT lifecycle_status FROM knowledge_evidence_links WHERE id='kel'")).scalar_one() == "source_deleted"
        assert connection.execute(text("SELECT status FROM knowledge_cards WHERE id='card'")).scalar_one() == "affected_by_source_deletion"
        assert connection.execute(text("SELECT COUNT(*) FROM index_memberships WHERE knowledge_card_id='card'")).scalar_one() == 0
        assert connection.execute(text("SELECT payload FROM knowledge_catalog_versions WHERE id='cat'")).scalar_one() is not None
