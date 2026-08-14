import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import Base
from app.services import course_service


def test_create_course_does_not_report_unrelated_integrity_error_as_slug_conflict(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        def fail_commit():
            raise IntegrityError("INSERT", {}, RuntimeError("unrelated constraint"))

        monkeypatch.setattr(session, "commit", fail_commit)

        with pytest.raises(IntegrityError):
            course_service.create_course(session, name="Course", slug="course", description=None)
    engine.dispose()


def test_update_course_does_not_report_unrelated_integrity_error_as_slug_conflict(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        course = course_service.create_course(session, name="Course", slug="course", description=None)

        def fail_commit():
            raise IntegrityError("UPDATE", {}, RuntimeError("unrelated constraint"))

        monkeypatch.setattr(session, "commit", fail_commit)

        with pytest.raises(IntegrityError):
            course_service.update_course(session, course.id, name="Changed")
    engine.dispose()
