"""exam_projects 表的最小 service：list/create/get/update_status。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import exam_projects


class ExamProjectConflictError(Exception):
    """项目名在课程内已存在。"""


class ExamProjectNotFoundError(Exception):
    """项目不存在。"""


def list_projects(session: Session, course_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(exam_projects).where(exam_projects.c.course_id == course_id).order_by(exam_projects.c.created_at.desc())
    ).mappings().all()
    return [dict(r) for r in rows]


def create_project(session: Session, course_id: str, name: str) -> dict[str, Any]:
    from app.db.schema import exam_projects as tbl
    import uuid
    project_id = str(uuid.uuid4())
    try:
        session.execute(
            tbl.insert().values(id=project_id, course_id=course_id, name=name, status="draft")
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ExamProjectConflictError(str(exc))
    return {"id": project_id, "course_id": course_id, "name": name, "status": "draft"}


def get_project(session: Session, course_id: str, project_id: str) -> dict[str, Any]:
    row = session.execute(
        select(exam_projects).where(
            exam_projects.c.course_id == course_id,
            exam_projects.c.id == project_id,
        )
    ).mappings().first()
    if not row:
        raise ExamProjectNotFoundError(project_id)
    return dict(row)


def update_status(session: Session, course_id: str, project_id: str, status: str) -> dict[str, Any]:
    existing = get_project(session, course_id, project_id)
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == project_id, exam_projects.c.course_id == course_id)
        .values(status=status)
    )
    session.commit()
    return {**existing, "status": status}
