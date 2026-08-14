import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.schema import Base
from app.db.session import get_session
from app.main import app


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


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.presign_calls = []

    def presign_put(self, *, object_key, content_type, sha256, expires_in):
        self.presign_calls.append((object_key, content_type, sha256, expires_in))
        return f"https://storage.invalid/{object_key}?signature=fake"

    def head_object(self, object_key):
        return self.objects.get(object_key)

    def stream_object(self, object_key):
        yield self.objects[object_key]["body"]


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
