"""Course operations scoped to the authenticated owner."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import Base, Course
from app.domain.course.category_profiles import normalize_category


class CourseNotFoundError(Exception):
    pass


class CourseConflictError(Exception):
    pass


class CourseNameConflictError(Exception):
    pass


def _slug_is_taken(session: Session, owner_id: str, slug: str, *, excluding_course_id: str | None = None) -> bool:
    statement = select(Course.id).where(Course.owner_id == owner_id, Course.slug == slug)
    if excluding_course_id is not None:
        statement = statement.where(Course.id != excluding_course_id)
    return session.scalar(statement) is not None


def _name_is_taken(session: Session, owner_id: str, name: str, *, excluding_course_id: str | None = None) -> bool:
    statement = select(Course.id).where(Course.owner_id == owner_id, Course.name == name)
    if excluding_course_id is not None:
        statement = statement.where(Course.id != excluding_course_id)
    return session.scalar(statement) is not None


def create_course(
    session: Session, *, owner_id: str, name: str, slug: str,
    description: str | None, category: str = "general",
) -> Course:
    if not slug:
        slug = f"course-{uuid4().hex[:8]}"
    course = Course(
        id=str(uuid4()), owner_id=owner_id, name=name, slug=slug,
        description=description, category=normalize_category(category),
    )
    session.add(course)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _name_is_taken(session, owner_id, name):
            raise CourseNameConflictError from exc
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
    requested_name = changes.get("name", course.name)
    for field, value in changes.items():
        setattr(course, field, value)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _name_is_taken(session, owner_id, requested_name, excluding_course_id=course_id):
            raise CourseNameConflictError from exc
        if _slug_is_taken(session, owner_id, requested_slug, excluding_course_id=course_id):
            raise CourseConflictError from exc
        raise
    session.refresh(course)
    return course


def delete_course(session: Session, owner_id: str, course_id: str) -> None:
    """硬删课程及其全部课程域数据。

    course_id 外键是裸引用（无 ON DELETE CASCADE），必须显式清理：
    按 metadata.sorted_tables 拓扑序删除所有带 course_id 列的表，
    未来新增课程域表自动纳入。不带 course_id 的表不可能引用课程域
    （已用元数据自省验证：40 表中 38 个带 course_id，其余无一指向课程域）。
    """
    course = get_owned_course(session, owner_id, course_id)
    for table in Base.metadata.sorted_tables:
        if "course_id" in table.c:
            session.execute(table.delete().where(table.c.course_id == course_id))
    session.delete(course)
    session.commit()