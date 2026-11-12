"""同一份课程资料经显式共享进入知识库，再被课程选用的完整链路。"""

import hashlib

import pytest
from sqlalchemy import func, select

from app.db.schema import (
    knowledge_blocks,
    knowledge_document_versions,
    knowledge_documents,
    knowledge_jobs,
    knowledge_parse_runs,
    material_versions,
)
from app.db.session import get_session
from app.main import app
from tests.integration.test_knowledge_lifecycle import env, _login, API


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    monkeypatch.setattr("app.infrastructure.tasks.knowledge_tasks.publish_job_event", lambda *_: False)


def course_material(client, storage, *, course_id=None, existing=None, kind="teaching_material", content=b"# Stack\n\nA stack is last in first out."):
    if course_id is None:
        response = client.post("/api/v1/courses", json={"name": "Bridge course"})
        assert response.status_code == 201, response.text
        course_id = response.json()["id"]
    upload = client.post(f"/api/v1/courses/{course_id}/upload-sessions", json={
        "filename": "lecture.md", "material_type": kind, "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(), "mime_type": "text/markdown",
        "existing_material_id": existing,
    })
    assert upload.status_code == 201, upload.text
    body = upload.json()
    storage.put_bytes(body["object_key"], content, "text/markdown")
    done = client.post(f"/api/v1/courses/{course_id}/upload-sessions/{body['session_id']}/complete")
    assert done.status_code == 200, done.text
    return course_id, done.json()


def share(client, space, course_id, version, **kwargs):
    return client.post(f"{API}/spaces/{space['id']}/import-from-course", json={
        "course_id": course_id, "material_version_id": version["id"],
        "expected_policy_version": space["policy_version"], "confirm_shareable": True,
        **kwargs,
    })


def parse_publish(client, space, copied):
    response = client.post(f"{API}/spaces/{space['id']}/jobs/{copied['job_id']}/run")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded", response.text
    published = client.post(f"{API}/spaces/{space['id']}/documents/{copied['document_id']}/publish", json={})
    assert published.status_code == 200, published.text
    return published.json()


def test_existing_material_visible_without_migration_and_private(env):
    a, b = _login("teacher-a"), _login("teacher-b")
    course, version = course_material(a, env)
    items = a.get(f"{API}/course-materials").json()
    assert len(items) == 1 and items[0]["version_id"] == version["id"]
    assert items[0]["can_share"] and items[0]["shared_versions"] == []
    assert b.get(f"{API}/course-materials").json() == []
    assert b.get(f"{API}/course-materials", params={"course_id": course}).status_code == 404


def test_share_parse_publish_and_import_back_without_reupload(env):
    a, b = _login("teacher-a"), _login("teacher-b")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Shared", "allow_course_import": True}).json()
    response = share(a, space, course, version)
    assert response.status_code == 200, response.text
    copied = response.json()
    assert copied["state"] == "processing" and not copied["idempotent"]
    repeated = share(a, space, course, version).json()
    assert repeated["idempotent"] and repeated["version_id"] == copied["version_id"]
    published = parse_publish(a, space, copied)
    origin = published["versions"][0]["course_origin"]
    assert origin["course_id"] == course and origin["version_no"] == 1
    assert not origin["update_available"]
    a.put(f"{API}/spaces/{space['id']}/members/teacher-b", json={"role": "reader"})
    b_detail = b.get(f"{API}/spaces/{space['id']}/documents/{copied['document_id']}").json()
    assert "course_id" not in b_detail["versions"][0]["course_origin"]
    assert b.get(f"/api/v1/courses/{course}/materials").status_code == 404
    target = b.post("/api/v1/courses", json={"name": "Reader course"}).json()["id"]
    imported = b.post(f"{API}/spaces/{space['id']}/documents/{copied['document_id']}/versions/{copied['version_id']}/import-to-course", json={"course_id": target})
    assert imported.status_code == 200, imported.text
    material = b.get(f"/api/v1/courses/{target}/materials").json()[0]
    assert material["latest_version"]["sha256"] == version["sha256"]
    assert material["knowledge_origin"]["space_id"] == space["id"]
    # 二次转发不能把原受限共享资料扩大到另一空间。
    other = b.post(f"{API}/spaces", json={"name": "Other"}).json()
    assert share(b, other, target, {"id": imported.json()["material_version_id"]}).status_code == 403


def test_share_reuses_ready_course_parse_without_reparse(env):
    """课程阶段已解析（md 本地解析）的资料共享后直接可用，不再重复解析。"""

    a = _login("teacher-a")
    course, version = course_material(a, env)
    parsed = a.post(f"/api/v1/courses/{course}/materials/{version['material_id']}/parse")
    assert parsed.status_code == 202, parsed.text
    assert parsed.json()["status"] == "ready"
    space = a.post(f"{API}/spaces", json={"name": "Reuse", "allow_course_import": True}).json()

    copied = share(a, space, course, version).json()
    assert copied["state"] == "ready" and copied["job_id"] is None

    with next(app.dependency_overrides[get_session]()) as session:
        assert session.scalar(
            select(func.count()).select_from(knowledge_jobs).where(
                knowledge_jobs.c.space_id == space["id"]
            )
        ) == 0
        assert session.scalar(
            select(knowledge_parse_runs.c.state).where(
                knowledge_parse_runs.c.id == copied["parse_run_id"]
            )
        ) == "ready"
        assert session.scalar(
            select(func.count()).select_from(knowledge_blocks).where(
                knowledge_blocks.c.parse_run_id == copied["parse_run_id"]
            )
        ) > 0

    published = a.post(
        f"{API}/spaces/{space['id']}/documents/{copied['document_id']}/publish", json={}
    )
    assert published.status_code == 200, published.text
    content = a.get(
        f"{API}/spaces/{space['id']}/documents/{copied['document_id']}"
        f"/versions/{copied['version_id']}/content"
    )
    assert content.status_code == 200 and content.json()["blocks"]


def test_repeat_share_heals_copy_stuck_in_parse(env):
    """先共享后解析的旧副本：再次共享时用课程 ready 结果原地补齐。"""

    a = _login("teacher-a")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Heal", "allow_course_import": True}).json()
    first = share(a, space, course, version).json()
    assert first["state"] == "processing" and first["job_id"]

    parsed = a.post(f"/api/v1/courses/{course}/materials/{version['material_id']}/parse")
    assert parsed.status_code == 202 and parsed.json()["status"] == "ready"

    healed = share(a, space, course, version).json()
    assert healed["idempotent"] and healed["state"] == "ready" and healed["job_id"] is None

    with next(app.dependency_overrides[get_session]()) as session:
        job_state = session.scalar(
            select(knowledge_jobs.c.state).where(knowledge_jobs.c.id == first["job_id"])
        )
        assert job_state == "cancelled"
        assert session.scalar(
            select(knowledge_parse_runs.c.state).where(
                knowledge_parse_runs.c.id == first["parse_run_id"]
            )
        ) == "ready"

    published = a.post(
        f"{API}/spaces/{space['id']}/documents/{first['document_id']}/publish", json={}
    )
    assert published.status_code == 200, published.text


def test_new_source_version_appends_and_old_publication_remains(env):
    a = _login("teacher-a")
    course, v1 = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Versions"}).json()
    first = share(a, space, course, v1).json()
    parse_publish(a, space, first)
    _, v2 = course_material(a, env, course_id=course, existing=v1["material_id"], content=b"# Queue\n\nA queue is first in first out.")
    detail = a.get(f"{API}/spaces/{space['id']}/documents/{first['document_id']}").json()
    assert detail["versions"][0]["course_origin"]["update_available"]
    assert detail["versions"][0]["course_origin"]["latest_version_id"] == v2["id"]
    second = share(a, space, course, v2).json()
    assert second["document_id"] == first["document_id"] and second["version_no"] == 2
    detail = a.get(f"{API}/spaces/{space['id']}/documents/{first['document_id']}").json()
    assert detail["status"] == "published" and detail["published_version_id"] == first["version_id"]
    assert detail["versions"][0]["state"] == "processing"
    updated = parse_publish(a, space, second)
    assert updated["published_version_id"] == second["version_id"]
    assert a.get(f"/api/v1/courses/{course}/materials").json()[0]["latest_version"]["id"] == v2["id"]
    assert len(a.get(f"{API}/course-materials").json()[0]["shared_versions"]) == 1


def test_private_source_deleted_after_share_keeps_published_copy(env):
    a = _login("teacher-a")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Snapshot"}).json()
    copied = share(a, space, course, version).json()
    parse_publish(a, space, copied)
    assert a.delete(f"/api/v1/courses/{course}/materials/{version['material_id']}").status_code == 204
    detail = a.get(f"{API}/spaces/{space['id']}/documents/{copied['document_id']}").json()
    assert detail["status"] == "published"
    assert not detail["versions"][0]["course_origin"]["source_available"]
    content = a.get(f"{API}/spaces/{space['id']}/documents/{copied['document_id']}/versions/{copied['version_id']}/content")
    assert content.status_code == 200 and content.json()["blocks"]


def test_both_permissions_confirmation_and_policy_version_checked(env):
    a, b = _login("teacher-a"), _login("teacher-b")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Permissions"}).json()
    own_b = b.post(f"{API}/spaces", json={"name": "Private"}).json()
    assert share(b, own_b, course, version).status_code == 404
    assert share(b, space, course, version).status_code == 404
    assert share(a, space, course, version, confirm_shareable=False).status_code == 422
    assert share(a, space, course, version, expected_policy_version=999).status_code == 409
    assert share(a, space, course, version, classification="confidential_exam").status_code == 422
    assert a.get(f"{API}/spaces/{space['id']}/documents").json() == []
    a.put(f"{API}/spaces/{space['id']}/members/teacher-b", json={"role": "reader"})
    b_course, b_version = course_material(b, env)
    assert share(b, space, b_course, b_version).status_code == 403


def test_exam_materials_are_not_shareable(env):
    a = _login("teacher-a")
    course, version = course_material(a, env, kind="assessment_syllabus")
    space = a.post(f"{API}/spaces", json={"name": "No exams"}).json()
    assert not a.get(f"{API}/course-materials").json()[0]["can_share"]
    assert share(a, space, course, version).status_code == 422


def test_limits_and_file_tampering_leave_no_versions(env, monkeypatch):
    a = _login("teacher-a")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Limits"}).json()
    monkeypatch.setattr("app.infrastructure.tasks.knowledge_tasks.MAX_ACTIVE_JOBS_PER_ACTOR", 0)
    response = share(a, space, course, version)
    assert response.status_code == 429, response.text
    with next(app.dependency_overrides[get_session]()) as session:
        for table in (knowledge_documents, knowledge_document_versions, knowledge_jobs):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    monkeypatch.setattr("app.infrastructure.tasks.knowledge_tasks.MAX_ACTIVE_JOBS_PER_ACTOR", 5)
    for key in list(env.objects):
        if "/materials/" in key:
            env.put_bytes(key, b"changed", "text/markdown")
    assert share(a, space, course, version).status_code == 409
    assert a.get(f"{API}/spaces/{space['id']}/documents").json() == []


@pytest.mark.parametrize("local_edit", [False, True])
def test_shared_update_appends_same_material_or_reports_local_conflict(env, local_edit):
    a, b = _login("teacher-a"), _login("teacher-b")
    course, v1 = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Updates", "allow_course_import": True}).json()
    first = share(a, space, course, v1).json()
    parse_publish(a, space, first)
    a.put(f"{API}/spaces/{space['id']}/members/teacher-b", json={"role": "reader"})
    target = b.post("/api/v1/courses", json={"name": "Target"}).json()["id"]
    space = a.get(f"{API}/spaces/{space['id']}").json()
    def use(version):
        return b.post(f"{API}/spaces/{space['id']}/documents/{first['document_id']}/versions/{version}/import-to-course", json={"course_id": target})
    imported = use(first["version_id"]).json()
    if local_edit:
        _, local = course_material(b, env, course_id=target, existing=imported["material_id"], content=b"# Local change")
        # A local edit does not erase the imported lineage or permit redistribution.
        other = b.post(f"{API}/spaces", json={"name": "Other"}).json()
        assert not b.get(f"{API}/course-materials").json()[0]["can_share"]
        assert share(b, other, target, local).status_code == 403
        origin = b.get(f"/api/v1/courses/{target}/materials").json()[0]["knowledge_origin"]
        assert origin["locally_modified"] and origin["version_no"] == 1
    _, v2 = course_material(a, env, course_id=course, existing=v1["material_id"], content=b"# Updated source")
    second = share(a, space, course, v2).json()
    parse_publish(a, space, second)
    response = use(second["version_id"])
    if local_edit:
        assert response.status_code == 409, response.text
    else:
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["material_id"] == imported["material_id"] and result["appended_version"]
        assert use(second["version_id"]).json()["idempotent"]
        with next(app.dependency_overrides[get_session]()) as session:
            old = session.execute(select(material_versions).where(material_versions.c.id == imported["material_version_id"])).mappings().one()
            assert old.sha256 == v1["sha256"] and old.status == "staged"
        items = b.get(f"/api/v1/courses/{target}/materials").json()
        assert len(items) == 1 and items[0]["latest_version"]["version_no"] == 2
        assert items[0]["knowledge_origin"]["version_no"] == 2


def test_latest_draft_hidden_from_reader_and_full_library_accepts_update(env, monkeypatch):
    a, b = _login("teacher-a"), _login("teacher-b")
    monkeypatch.setattr("app.services.knowledge_document_service.MAX_DOCUMENTS_PER_SPACE", 1)
    course, v1 = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Full"}).json()
    first = share(a, space, course, v1).json()
    parse_publish(a, space, first)
    a.put(f"{API}/spaces/{space['id']}/members/teacher-b", json={"role": "reader"})
    space = a.get(f"{API}/spaces/{space['id']}").json()
    _, v2 = course_material(a, env, course_id=course, existing=v1["material_id"], content=b"# Second")
    assert share(a, space, course, v2).status_code == 200
    assert a.get(f"{API}/spaces/{space['id']}/documents").json()[0]["latest_version"]["version_no"] == 2
    reader = b.get(f"{API}/spaces/{space['id']}/documents").json()[0]
    assert reader["latest_version"] is None and reader["published_version_id"] == first["version_id"]
    other_course = a.post('/api/v1/courses', json={'name': 'Other source'}).json()['id']
    _, other = course_material(a, env, course_id=other_course)
    assert share(a, space, other_course, other).status_code == 429


def test_storage_unavailable_and_archived_target_do_not_stage(env, monkeypatch):
    a = _login("teacher-a")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Unavailable"}).json()
    def broken(_):
        raise OSError("storage offline")
    monkeypatch.setattr(env, "stream_object", broken)
    assert share(a, space, course, version).status_code == 503
    assert a.get(f"{API}/spaces/{space['id']}/documents").json() == []
    a.post(f"{API}/spaces/{space['id']}/archive")
    assert share(a, space, course, version).status_code in {403, 409}


def test_selecting_own_shared_material_reuses_original(env):
    a = _login("teacher-a")
    course, version = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Round trip", "allow_course_import": True}).json()
    copied = share(a, space, course, version).json()
    parse_publish(a, space, copied)
    response = a.post(f"{API}/spaces/{space['id']}/documents/{copied['document_id']}/versions/{copied['version_id']}/import-to-course", json={"course_id": course})
    assert response.status_code == 200, response.text
    assert response.json()["reused_source"]
    assert response.json()["material_version_id"] == version["id"]
    assert len(a.get(f"/api/v1/courses/{course}/materials").json()) == 1
    assert a.get(f"{API}/course-materials").json()[0]["can_share"]


def test_deleted_course_copy_is_not_silently_resurrected(env):
    a = _login("teacher-a")
    course, v1 = course_material(a, env)
    space = a.post(f"{API}/spaces", json={"name": "Deleted copy", "allow_course_import": True}).json()
    first = share(a, space, course, v1).json()
    parse_publish(a, space, first)
    target = a.post('/api/v1/courses', json={'name': 'Disposable copy'}).json()['id']
    def use(version):
        return a.post(f"{API}/spaces/{space['id']}/documents/{first['document_id']}/versions/{version}/import-to-course", json={"course_id": target})
    imported = use(first['version_id']).json()
    assert a.delete(f"/api/v1/courses/{target}/materials/{imported['material_id']}").status_code == 204
    assert use(first['version_id']).status_code == 409
    _, v2 = course_material(a, env, course_id=course, existing=v1['material_id'], content=b'# New edition')
    second = share(a, space, course, v2).json()
    parse_publish(a, space, second)
    response = use(second['version_id'])
    assert response.status_code == 200, response.text
    assert response.json()['material_id'] != imported['material_id']
    assert len(a.get(f'/api/v1/courses/{target}/materials').json()) == 1
