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
            course_service.create_course(session, owner_id="owner", name="Course", slug="course", description=None)
    engine.dispose()


def test_update_course_does_not_report_unrelated_integrity_error_as_slug_conflict(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        course = course_service.create_course(session, owner_id="owner", name="Course", slug="course", description=None)

        def fail_commit():
            raise IntegrityError("UPDATE", {}, RuntimeError("unrelated constraint"))

        monkeypatch.setattr(session, "commit", fail_commit)

        with pytest.raises(IntegrityError):
            course_service.update_course(session, "owner", course.id, name="Changed")
    engine.dispose()


def test_create_course_stores_and_normalizes_category():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        styled = course_service.create_course(
            session, owner_id="owner", name="分布式", slug="dist", description=None,
            category="computer",
        )
        assert styled.category == "computer"

        # 未知类别回退默认档（读侧 category_profile 同样兜底，双保险）
        bogus = course_service.create_course(
            session, owner_id="owner", name="未知类", slug="bogus", description=None,
            category="bogus",
        )
        assert bogus.category == "general"

        plain = course_service.create_course(
            session, owner_id="owner", name="缺省类", slug="plain", description=None,
        )
        assert plain.category == "general"
    engine.dispose()
