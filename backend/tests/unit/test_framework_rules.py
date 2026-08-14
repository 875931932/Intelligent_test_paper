from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User, framework_build_runs, framework_conflicts, framework_versions
from app.domain.framework.models import (
    AssessmentAnchor,
    AssessmentOutline,
    FrameworkCandidate,
    FrameworkConfirmation,
    FrameworkConflict,
    TeachingTopic,
)
from app.services.framework_service import DatabaseFrameworkRepository, FrameworkInputError


def test_framework_confirmation_rejects_boolean_only_payload():
    with pytest.raises(ValidationError):
        FrameworkConfirmation.model_validate({"confirmed": True})


def test_framework_confirmation_requires_explicit_exclusion_list():
    with pytest.raises(ValidationError):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [
                    {
                        "key": "unit-1",
                        "title": "核心概念",
                        "exam_weight": 100,
                        "ability_requirements": [],
                        "allowed_question_types": ["single_choice"],
                        "excluded_content": [],
                        "alignment_keys": ["topic-1"],
                    }
                ],
                "conflict_resolutions": {},
            }
        )


def test_framework_confirmation_requires_weights_to_total_one_hundred():
    with pytest.raises(ValidationError, match="total 100"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [
                    {
                        "key": "unit-1",
                        "title": "核心概念",
                        "exam_weight": 60,
                    }
                ],
                "conflict_resolutions": {},
                "teacher_exclusions": [],
            }
        )


def test_framework_confirmation_rejects_blank_conflict_decisions():
    with pytest.raises(ValidationError, match="conflict resolution"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [
                    {
                        "key": "unit-1",
                        "title": "核心概念",
                        "exam_weight": 100,
                    }
                ],
                "conflict_resolutions": {"coverage:unit-1": "   "},
                "teacher_exclusions": [],
            }
        )


def test_assessment_outline_requires_at_least_one_exam_anchor():
    with pytest.raises(ValidationError, match="at least one assessment anchor"):
        AssessmentOutline(anchors=[])


def test_assessment_outline_rejects_duplicate_anchor_keys():
    with pytest.raises(ValidationError, match="assessment anchor keys must be unique"):
        AssessmentOutline(
            anchors=[
                AssessmentAnchor(key="same", title="范围一", exam_weight=50),
                AssessmentAnchor(key="same", title="范围二", exam_weight=50),
            ]
        )


def test_confirmation_rejects_duplicate_anchor_keys():
    with pytest.raises(ValidationError, match="confirmed anchor keys must be unique"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [
                    {"key": "same", "title": "范围一", "exam_weight": 50},
                    {"key": "same", "title": "范围二", "exam_weight": 50},
                ],
                "conflict_resolutions": {},
                "teacher_exclusions": [],
            }
        )


def _framework_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'framework.db'}")
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="owner", display_name="Owner", role="teacher"))
    session.flush()
    session.add(Course(id="course", owner_id="owner", slug="course", name="Course"))
    session.flush()
    session.execute(
        framework_build_runs.insert().values(
            id="run-1",
            course_id="course",
            status="running",
            input_snapshot={"teaching_material_version_id": "t1", "assessment_material_version_id": "a1"},
        )
    )
    session.commit()
    return engine, session


def _candidate(*, conflict=True):
    return FrameworkCandidate(
        anchors=[AssessmentAnchor(key="unit-1", title="核心概念", exam_weight=100, alignment_keys=["topic-1"])],
        teaching_topics=[TeachingTopic(key="topic-1", title="核心概念", depth="apply")],
        conflicts=(
            [
                FrameworkConflict(
                    key="scope:unit-1",
                    kind="scope_conflict",
                    message="范围需要教师确认",
                )
            ]
            if conflict
            else []
        ),
    )


def _valid_confirmation(*, resolutions=None):
    return FrameworkConfirmation.model_validate(
        {
            "anchors": [{"key": "unit-1", "title": "核心概念", "exam_weight": 100}],
            "conflict_resolutions": resolutions or {},
            "teacher_exclusions": [],
        }
    )


def test_repository_refuses_to_publish_unresolved_candidate_conflicts(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        candidate_id = repository.persist_candidate({"course_id": "course", "run_id": "run-1"}, _candidate())

        with pytest.raises(FrameworkInputError, match="every open conflict"):
            repository.publish(
                {"course_id": "course", "candidate_id": candidate_id},
                _valid_confirmation(),
            )

        status = session.scalar(select(framework_versions.c.status).where(framework_versions.c.id == candidate_id))
        assert status == "candidate"
    finally:
        session.close()
        engine.dispose()


def test_repository_rejects_conflict_decisions_not_present_in_candidate(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        candidate_id = repository.persist_candidate({"course_id": "course", "run_id": "run-1"}, _candidate(conflict=False))

        with pytest.raises(FrameworkInputError, match="unknown conflict"):
            repository.publish(
                {"course_id": "course", "candidate_id": candidate_id},
                _valid_confirmation(resolutions={"coverage:invented": "无对应冲突"}),
            )
    finally:
        session.close()
        engine.dispose()


def test_publishing_new_framework_supersedes_previous_version_and_resolves_conflicts(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        first_id = repository.persist_candidate({"course_id": "course", "run_id": "run-1"}, _candidate())
        repository.publish(
            {"course_id": "course", "candidate_id": first_id},
            _valid_confirmation(resolutions={"scope:unit-1": "按教师修订后的范围发布"}),
        )
        session.execute(
            framework_build_runs.insert().values(
                id="run-2",
                course_id="course",
                status="running",
                input_snapshot={"teaching_material_version_id": "t2", "assessment_material_version_id": "a2"},
            )
        )
        session.commit()
        second_id = repository.persist_candidate({"course_id": "course", "run_id": "run-2"}, _candidate(conflict=False))
        repository.publish(
            {"course_id": "course", "candidate_id": second_id},
            _valid_confirmation(),
        )

        statuses = dict(session.execute(select(framework_versions.c.id, framework_versions.c.status)).all())
        assert statuses == {first_id: "superseded", second_id: "published"}
        conflict = session.execute(select(framework_conflicts).where(framework_conflicts.c.framework_version_id == first_id)).mappings().one()
        assert conflict["status"] == "resolved"
        assert conflict["details"]["teacher_resolution"] == "按教师修订后的范围发布"
    finally:
        session.close()
        engine.dispose()


def test_persist_candidate_is_idempotent_for_the_same_framework_run(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)

        first_id = repository.persist_candidate({"course_id": "course", "run_id": "run-1"}, _candidate(conflict=False))
        second_id = repository.persist_candidate({"course_id": "course", "run_id": "run-1"}, _candidate(conflict=False))

        assert second_id == first_id
        version_ids = session.scalars(
            select(framework_versions.c.id).where(framework_versions.c.framework_build_run_id == "run-1")
        ).all()
        assert version_ids == [first_id]
    finally:
        session.close()
        engine.dispose()
