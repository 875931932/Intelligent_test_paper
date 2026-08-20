"""Course operations scoped to the fixed development teacher."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import Course, User

DEV_OWNER_ID = "owner-dev"


class CourseNotFoundError(Exception):
    pass


class CourseConflictError(Exception):
    pass


def _slug_is_taken(session: Session, slug: str, *, excluding_course_id: str | None = None) -> bool:
    statement = select(Course.id).where(Course.owner_id == DEV_OWNER_ID, Course.slug == slug)
    if excluding_course_id is not None:
        statement = statement.where(Course.id != excluding_course_id)
    return session.scalar(statement) is not None


def _ensure_dev_owner(session: Session) -> None:
    if session.get(User, DEV_OWNER_ID) is None:
        session.add(User(id=DEV_OWNER_ID, display_name="Development Owner", role="teacher"))
        session.flush()


def create_course(session: Session, *, name: str, slug: str, description: str | None) -> Course:
    _ensure_dev_owner(session)
    if not slug:
        slug = f"course-{uuid4().hex[:8]}"
    course = Course(id=str(uuid4()), owner_id=DEV_OWNER_ID, name=name, slug=slug, description=description)
    session.add(course)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _slug_is_taken(session, slug):
            raise CourseConflictError from exc
        raise
    session.refresh(course)
    return course


def list_courses(session: Session) -> list[Course]:
    return list(session.scalars(select(Course).where(Course.owner_id == DEV_OWNER_ID).order_by(Course.name, Course.id)))


def get_course(session: Session, course_id: str) -> Course:
    course = session.scalar(select(Course).where(Course.id == course_id, Course.owner_id == DEV_OWNER_ID))
    if course is None:
        raise CourseNotFoundError
    return course


def update_course(session: Session, course_id: str, **changes: object) -> Course:
    course = get_course(session, course_id)
    requested_slug = changes.get("slug", course.slug)
    for field, value in changes.items():
        setattr(course, field, value)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _slug_is_taken(session, requested_slug, excluding_course_id=course_id):
            raise CourseConflictError from exc
        raise
    session.refresh(course)
    return course
