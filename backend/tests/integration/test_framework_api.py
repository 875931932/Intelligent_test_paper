from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    content_blocks,
    document_parse_runs,
    framework_build_runs,
    material_versions,
    materials,
    parser_profiles,
)
from app.db.session import get_session
from app.domain.framework.models import AssessmentAnchor, AssessmentOutline, TeachingTopic
from app.main import app


class FakeSyllabusExtractor:
    def extract_teaching(self, blocks):
        assert blocks == ["教学内容与要求：理解核心概念"]
        return [TeachingTopic(key="core", title="核心概念", depth="understand")]

    def extract_assessment(self, blocks):
        assert blocks == ["期末考试：核心概念占100%"]
        return AssessmentOutline(
            anchors=[
                AssessmentAnchor(
                    key="core-exam",
                    title="核心概念",
                    exam_weight=100,
                    ability_requirements=["理解"],
                    allowed_question_types=["single_choice"],
                    alignment_keys=["core"],
                )
            ],
            final_exam_rules={"question_type_weights": {"single_choice": 100}},
        )


def _insert_outline(session, *, material_id, version_id, run_id, material_type, text):
    session.execute(
        materials.insert().values(
            id=material_id,
            course_id="course",
            logical_name=f"{material_id}.pdf",
            material_type=material_type,
            status="staged",
        )
    )
    session.execute(
        material_versions.insert().values(
            id=version_id,
            course_id="course",
            material_id=material_id,
            version_no=1,
            status="staged",
            object_key=f"courses/course/{material_id}.pdf",
            size_bytes=100,
            sha256="a" * 64,
            mime_type="application/pdf",
        )
    )
    session.execute(
        document_parse_runs.insert().values(
            id=run_id,
            course_id="course",
            material_version_id=version_id,
            parser_profile_id="mineru-profile",
            status="ready",
            completed_at=datetime.now(UTC),
        )
    )
    session.execute(
        content_blocks.insert().values(
            id=f"block-{run_id}",
            course_id="course",
            document_parse_run_id=run_id,
            material_version_id=version_id,
            block_index=0,
            block_type="text",
            text=text,
            heading_path=[],
            reading_order=0,
            content_hash="b" * 64,
        )
    )


def test_framework_api_builds_candidate_and_publishes_structured_confirmation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'framework-api.db'}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        setup.add(User(id="owner-dev", display_name="Owner", role="teacher"))
        setup.flush()
        setup.add(Course(id="course", owner_id="owner-dev", slug="course", name="Course"))
        setup.flush()
        setup.execute(
            parser_profiles.insert().values(
                id="mineru-profile",
                course_id="course",
                name="mineru",
                version="1",
                provider="mineru",
                configuration={},
            )
        )
        _insert_outline(
            setup,
            material_id="teaching",
            version_id="teaching-v1",
            run_id="parse-teaching",
            material_type="teaching_syllabus",
            text="教学内容与要求：理解核心概念",
        )
        _insert_outline(
            setup,
            material_id="assessment",
            version_id="assessment-v1",
            run_id="parse-assessment",
            material_type="assessment_syllabus",
            text="期末考试：核心概念占100%",
        )
        setup.commit()

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.state.syllabus_extractor = FakeSyllabusExtractor()
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/courses/course/framework-runs",
                json={
                    "teaching_material_version_id": "teaching-v1",
                    "assessment_material_version_id": "assessment-v1",
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            assert created.json()["status"] == "awaiting_teacher_confirmation"

            candidate = client.get(f"/api/v1/courses/course/framework-runs/{run_id}/candidate")
            assert candidate.status_code == 200
            assert candidate.json()["payload"]["anchors"][0]["exam_weight"] == 100
            assert candidate.json()["payload"]["final_exam_rules"]["question_type_weights"]["single_choice"] == 100

            confirmed = client.post(
                f"/api/v1/courses/course/framework-runs/{run_id}/confirm",
                json={
                    "anchors": candidate.json()["payload"]["anchors"],
                    "conflict_resolutions": {},
                    "teacher_exclusions": [],
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["status"] == "published"

            current = client.get("/api/v1/courses/course/framework-versions/current")
            assert current.status_code == 200
            assert current.json()["id"] == confirmed.json()["id"]

        with Session(engine) as session:
            run_status = session.scalar(select(framework_build_runs.c.status).where(framework_build_runs.c.id == run_id))
            assert run_status == "published"
    finally:
        app.dependency_overrides.clear()
        del app.state.syllabus_extractor
        engine.dispose()


def test_framework_build_requires_configured_semantic_extractor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'framework-no-model.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    if hasattr(app.state, "syllabus_extractor"):
        del app.state.syllabus_extractor
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/courses/course/framework-runs",
                json={
                    "teaching_material_version_id": "teaching-v1",
                    "assessment_material_version_id": "assessment-v1",
                },
            )
            assert response.status_code == 503
            assert response.json()["detail"] == "syllabus semantic extractor is not configured"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
