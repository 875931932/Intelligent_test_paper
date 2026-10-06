"""Course operations scoped to the authenticated owner."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import Base, Course, exam_projects
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

    course_id 外键是裸引用（无 ON DELETE CASCADE），必须显式清理，且顺序有两条硬约束：

    1. **先解除 FK 环**：exam_projects 的 active_blueprint_version_id /
       active_generation_run_id / active_paper_version_id 反向引用
       blueprint_versions / generation_runs / paper_versions，与它们构成复合外键环
       （Match SIMPLE：任一项置空即不再受约束）。不先置空，删这三张表时项目行
       还在引用，直接外键违约（与 exam_project_service.delete_project 同一处置）。
    2. **子表先于父表**：按依赖逆序删除。metadata.sorted_tables 是「建表序」
       （父表在前），正向遍历删除会先删 exam_projects 再删 blueprint_versions，
       触发 ForeignKeyViolation（线上首页删课程 500 的根因）。

    不带 course_id 的表不可能引用课程域（已用元数据自省验证：40 表中 38 个带
    course_id，其余无一指向课程域），因此逐表按 course_id 清理即覆盖全部课程数据。
    """
    course = get_owned_course(session, owner_id, course_id)
    # 1) 解除环：三个反向引用列整体置空（课程内项目本就要全删，置空无副作用）
    session.execute(
        update(exam_projects)
        .where(exam_projects.c.course_id == course_id)
        .values(
            active_blueprint_version_id=None,
            active_generation_run_id=None,
            active_paper_version_id=None,
        )
    )
    # 2) 逆依赖序清理：子表先删，父表后删（未来新增课程域表自动纳入）
    for table in reversed(Base.metadata.sorted_tables):
        if "course_id" in table.c:
            session.execute(table.delete().where(table.c.course_id == course_id))
    session.delete(course)
    session.commit()