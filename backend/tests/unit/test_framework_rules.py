from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, User, exam_points, framework_build_runs, framework_conflicts, framework_versions
from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy, WeightSource
from app.domain.framework.models import (
    AssessmentAnchor,
    AssessmentOutline,
    FrameworkCandidate,
    FrameworkConfirmation,
    FrameworkConflict,
    TeachingTopic,
)
from app.services.framework_service import DatabaseFrameworkRepository, FrameworkInputError


def _exam_point(
    *,
    code="core-concept",
    anchor_key="unit-1",
    weight_value=100,
    weight_source=WeightSource.ASSESSMENT_SYLLABUS,
    teaching_anchor_keys=None,
):
    return ExamPoint(
        code=code,
        anchor_key=anchor_key,
        title="核心概念",
        assessment_requirement="理解并应用核心概念",
        weight_value=weight_value,
        weight_source=weight_source,
        weight_group_id=anchor_key,
        cognitive_targets=["apply"],
        allowed_question_types=["single_choice"],
        operational_detail_policy=OperationalDetailPolicy.SUPPORTING_ONLY,
        retrieval_intent="检索核心概念的定义、原理与应用依据",
        assessment_anchor_keys=[anchor_key],
        teaching_anchor_keys=teaching_anchor_keys or ["topic-1"],
    )


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
                "exam_points": [_exam_point().model_dump(mode="json")],
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
                "exam_points": [_exam_point(weight_value=60).model_dump(mode="json")],
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
                "exam_points": [_exam_point().model_dump(mode="json")],
                "conflict_resolutions": {"coverage:unit-1": "   "},
                "teacher_exclusions": [],
            }
        )


def test_assessment_outline_requires_at_least_one_exam_anchor():
    with pytest.raises(ValidationError, match="at least one assessment anchor"):
        AssessmentOutline(anchors=[], exam_points=[_exam_point()])


def test_assessment_outline_requires_at_least_one_exam_point():
    with pytest.raises(ValidationError, match="at least 1 item"):
        AssessmentOutline(
            anchors=[AssessmentAnchor(key="unit-1", title="核心概念", exam_weight=100)],
            exam_points=[],
        )


def test_assessment_outline_rejects_duplicate_anchor_keys():
    with pytest.raises(ValidationError, match="assessment anchor keys must be unique"):
        AssessmentOutline(
            anchors=[
                AssessmentAnchor(key="same", title="范围一", exam_weight=50),
                AssessmentAnchor(key="same", title="范围二", exam_weight=50),
            ],
            exam_points=[_exam_point(anchor_key="same", weight_value=50)],
        )


def test_confirmation_rejects_duplicate_anchor_keys():
    with pytest.raises(ValidationError, match="confirmed anchor keys must be unique"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [
                    {"key": "same", "title": "范围一", "exam_weight": 50},
                    {"key": "same", "title": "范围二", "exam_weight": 50},
                ],
                "exam_points": [_exam_point(anchor_key="same", weight_value=50).model_dump(mode="json")],
                "conflict_resolutions": {},
                "teacher_exclusions": [],
            }
        )


def test_assessment_outline_rejects_duplicate_exam_point_codes():
    with pytest.raises(ValidationError, match="exam point codes must be unique"):
        AssessmentOutline(
            anchors=[AssessmentAnchor(key="unit-1", title="核心概念", exam_weight=100)],
            exam_points=[_exam_point(), _exam_point()],
        )


def test_assessment_outline_rejects_explicit_exam_point_total_above_parent_weight():
    with pytest.raises(ValidationError, match="explicit exam point weight must not exceed parent anchor weight"):
        AssessmentOutline(
            anchors=[AssessmentAnchor(key="unit-1", title="核心概念", exam_weight=100)],
            exam_points=[
                _exam_point(code="concept-a", weight_value=60),
                _exam_point(code="concept-b", weight_value=60),
            ],
        )


def test_framework_candidate_requires_at_least_one_exam_point():
    with pytest.raises(ValidationError, match="at least 1 item"):
        FrameworkCandidate(
            anchors=[AssessmentAnchor(key="unit-1", title="核心概念", exam_weight=100)],
            exam_points=[],
            teaching_topics=[],
            conflicts=[],
        )


def test_framework_confirmation_requires_at_least_one_exam_point():
    with pytest.raises(ValidationError, match="at least 1 item"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [{"key": "unit-1", "title": "核心概念", "exam_weight": 100}],
                "exam_points": [],
                "conflict_resolutions": {},
                "teacher_exclusions": [],
            }
        )


def test_confirmation_rejects_explicit_exam_point_weight_total_below_parent_weight():
    with pytest.raises(ValidationError, match="must equal parent anchor weight"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [{"key": "unit-1", "title": "核心概念", "exam_weight": 100}],
                "exam_points": [
                    _exam_point(code="concept-a", weight_value=30).model_dump(mode="json"),
                    _exam_point(code="concept-b", weight_value=30).model_dump(mode="json"),
                ],
                "conflict_resolutions": {},
                "teacher_exclusions": [],
            }
        )


def test_confirmation_allows_explicit_and_inherited_group_weights_without_equal_split():
    explicit = _exam_point(code="concept-explicit", weight_value=20)
    inherited = _exam_point(
        code="concept-inherited",
        weight_value=0,
        weight_source=WeightSource.INHERITED_GROUP,
    )
    confirmation = FrameworkConfirmation.model_validate(
        {
            "anchors": [{"key": "unit-1", "title": "核心概念", "exam_weight": 100}],
            "exam_points": [explicit.model_dump(mode="json"), inherited.model_dump(mode="json")],
            "conflict_resolutions": {},
            "teacher_exclusions": [],
        }
    )
    assert [point.weight_value for point in confirmation.exam_points] == [20, 0]


def test_confirmation_accepts_explicit_weights_with_small_float_rounding_error():
    confirmation = FrameworkConfirmation.model_validate(
        {
            "anchors": [{"key": "unit-1", "title": "核心概念", "exam_weight": 100}],
            "exam_points": [
                _exam_point(code="concept-a", weight_value=33.33).model_dump(mode="json"),
                _exam_point(code="concept-b", weight_value=33.33).model_dump(mode="json"),
                _exam_point(code="concept-c", weight_value=33.33).model_dump(mode="json"),
            ],
            "conflict_resolutions": {},
            "teacher_exclusions": [],
        }
    )
    assert len(confirmation.exam_points) == 3


def test_confirmation_rejects_exam_point_outside_confirmed_anchor_scope():
    with pytest.raises(ValidationError, match="exam point anchor_key must belong to confirmed anchors"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [{"key": "unit-1", "title": "核心概念", "exam_weight": 100}],
                "exam_points": [_exam_point(anchor_key="removed-unit").model_dump(mode="json")],
                "conflict_resolutions": {},
                "teacher_exclusions": [],
            }
        )


def test_confirmation_rejects_explicit_exam_point_weight_above_parent_anchor():
    with pytest.raises(ValidationError, match="explicit exam point weight must not exceed parent anchor weight"):
        FrameworkConfirmation.model_validate(
            {
                "anchors": [
                    {"key": "unit-1", "title": "核心概念", "exam_weight": 40},
                    {"key": "unit-2", "title": "其他范围", "exam_weight": 60},
                ],
                "exam_points": [_exam_point(weight_value=60).model_dump(mode="json")],
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
        exam_points=[_exam_point()],
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
            "exam_points": [_exam_point().model_dump(mode="json")],
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


def test_repository_persists_and_confirms_exam_points(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        candidate_id = repository.persist_candidate(
            {"course_id": "course", "run_id": "run-1"},
            _candidate(conflict=False),
        )

        candidate_row = session.execute(
            select(exam_points).where(
                exam_points.c.course_id == "course",
                exam_points.c.framework_version_id == candidate_id,
            )
        ).mappings().one()
        assert candidate_row["status"] == "candidate"
        assert candidate_row["code"] == "core-concept"

        repository.publish(
            {"course_id": "course", "candidate_id": candidate_id},
            _valid_confirmation(),
        )

        confirmed_row = session.execute(
            select(exam_points).where(
                exam_points.c.course_id == "course",
                exam_points.c.framework_version_id == candidate_id,
            )
        ).mappings().one()
        assert confirmed_row["status"] == "confirmed"
        assert confirmed_row["operational_detail_policy"] == "supporting_only"
    finally:
        session.close()
        engine.dispose()


def test_repository_rejects_confirmed_exam_point_not_in_candidate_version(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        candidate_id = repository.persist_candidate(
            {"course_id": "course", "run_id": "run-1"},
            _candidate(conflict=False),
        )
        confirmation = _valid_confirmation().model_copy(
            update={"exam_points": [_exam_point(code="invented")]}
        )

        with pytest.raises(FrameworkInputError, match="not part of the current candidate"):
            repository.publish(
                {"course_id": "course", "candidate_id": candidate_id},
                confirmation,
            )
    finally:
        session.close()
        engine.dispose()


def test_repository_refuses_empty_exam_point_confirmation_without_deleting_candidate_rows(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        candidate_id = repository.persist_candidate(
            {"course_id": "course", "run_id": "run-1"},
            _candidate(conflict=False),
        )
        empty_confirmation = _valid_confirmation().model_copy(update={"exam_points": []})

        with pytest.raises(FrameworkInputError, match="at least one exam point"):
            repository.publish(
                {"course_id": "course", "candidate_id": candidate_id},
                empty_confirmation,
            )

        rows = session.execute(
            select(exam_points).where(
                exam_points.c.course_id == "course",
                exam_points.c.framework_version_id == candidate_id,
            )
        ).mappings().all()
        assert len(rows) == 1
        assert rows[0]["status"] == "candidate"
        assert session.scalar(select(framework_versions.c.status).where(framework_versions.c.id == candidate_id)) == "candidate"
    finally:
        session.close()
        engine.dispose()


def test_repository_rejects_bypassed_underweight_confirmation_before_publishing(tmp_path):
    engine, session = _framework_session(tmp_path)
    try:
        repository = DatabaseFrameworkRepository(session)
        candidate_id = repository.persist_candidate(
            {"course_id": "course", "run_id": "run-1"},
            _candidate(conflict=False),
        )
        bypassed_confirmation = _valid_confirmation().model_copy(
            update={"exam_points": [_exam_point(weight_value=30)]}
        )

        with pytest.raises(FrameworkInputError, match="must equal parent anchor weight"):
            repository.publish(
                {"course_id": "course", "candidate_id": candidate_id},
                bypassed_confirmation,
            )

        assert session.scalar(select(framework_versions.c.status).where(framework_versions.c.id == candidate_id)) == "candidate"
    finally:
        session.close()
        engine.dispose()
