"""Course operations scoped to the authenticated owner."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import Course


class CourseNotFoundError(Exception):
    pass


class CourseConflictError(Exception):
    pass


def _slug_is_taken(session: Session, owner_id: str, slug: str, *, excluding_course_id: str | None = None) -> bool:
    statement = select(Course.id).where(Course.owner_id == owner_id, Course.slug == slug)
    if excluding_course_id is not None:
        statement = statement.where(Course.id != excluding_course_id)
    return session.scalar(statement) is not None


def create_course(session: Session, *, owner_id: str, name: str, slug: str, description: str | None) -> Course:
    if not slug:
        slug = f"course-{uuid4().hex[:8]}"
    course = Course(id=str(uuid4()), owner_id=owner_id, name=name, slug=slug, description=description)
    session.add(course)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _slug_is_taken(session, owner_id, slug):
            raise CourseConflictError from exc
        raise
    session.refresh(course)
    return course


def list_courses(session: Session, owner_id: str) -> list[Course]:
    return list(session.scalars(select(Course).where(Course.owner_id == owner_id).order_by(Course.name, Course.id)))


def get_course(session: Session, course_id: str) -> Course:
    """Fetch a course by id without an owner filter (downstream tenant-scoped services)."""
    course = session.scalar(select(Course).where(Course.id == course_id))
    if course is None:
        raise CourseNotFoundError
    return course


def get_owned_course(session: Session, owner_id: str, course_id: str) -> Course:
    course = session.scalar(select(Course).where(Course.id == course_id, Course.owner_id == owner_id))
    if course is None:
        raise CourseNotFoundError
    return course


def update_course(session: Session, owner_id: str, course_id: str, **changes: object) -> Course:
    course = get_owned_course(session, owner_id, course_id)
    requested_slug = changes.get("slug", course.slug)
    for field, value in changes.items():
        setattr(course, field, value)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _slug_is_taken(session, owner_id, requested_slug, excluding_course_id=course_id):
            raise CourseConflictError from exc
        raise
    session.refresh(course)
    return course