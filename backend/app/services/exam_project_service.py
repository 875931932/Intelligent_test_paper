"""exam_projects 表的最小 service：list/create/get/update_status。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import blueprint_versions, exam_projects, task_runs


class ExamProjectConflictError(Exception):
    """项目名在课程内已存在。"""


class ExamProjectNotFoundError(Exception):
    """项目不存在。"""


def _backfill_active_blueprint(
    session: Session, course_id: str, project_id: str,
) -> str | None:
    """active_blueprint_version_id 为空时回填最新蓝图版本 id（含旧数据）。

    早期版本创建蓝图只把 exam_projects.status 改为 blueprint，未写
    active_blueprint_version_id，导致前端误判“尚无蓝图”。这里按版本号
    取最新一个补上，避免老项目打开后停留在创建表单。
    """
    return session.execute(
        select(blueprint_versions.c.id)
        .where(
            blueprint_versions.c.course_id == course_id,
            blueprint_versions.c.exam_project_id == project_id,
        )
        .order_by(blueprint_versions.c.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()


def _with_generation_task_status(session: Session, course_id: str, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 durable task 的真实进度附加到项目摘要，供课程首页展示。

    exam_projects.active_generation_run_id 指向 generation_runs，不是 task_runs.id；
    任务的 payload 中同时保存 project_id 和 generation_run_id，因此在服务层做一次
    归并即可避免前端把两个不同 ID 当成同一个任务轮询。
    """
    if not projects:
        return projects
    rows = session.execute(
        select(task_runs).where(
            task_runs.c.course_id == course_id,
            task_runs.c.task_type == "generation_run",
        ).order_by(task_runs.c.updated_at.desc())
    ).mappings().all()
    task_by_project: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row.get("payload") or {}
        project_id = payload.get("project_id") if isinstance(payload, dict) else None
        if project_id and project_id not in task_by_project:
            task_by_project[project_id] = dict(row)
    enriched: list[dict[str, Any]] = []
    for project in projects:
        item = dict(project)
        task = task_by_project.get(item.get("id"))
        if task:
            item.update(
                active_task_run_id=task.get("id"),
                generation_progress=task.get("progress"),
                generation_stage=task.get("stage"),
                generation_error=task.get("error_message"),
                generation_task_status=task.get("status"),
            )
        else:
            item.update(
                active_task_run_id=None,
                generation_progress=None,
                generation_stage=None,
                generation_error=None,
                generation_task_status=None,
            )
        enriched.append(item)
    return enriched


def list_projects(session: Session, course_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(exam_projects).where(exam_projects.c.course_id == course_id).order_by(exam_projects.c.id.desc())
    ).mappings().all()
    projects = [dict(r) for r in rows]
    for p in projects:
        if not p.get("active_blueprint_version_id"):
            p["active_blueprint_version_id"] = _backfill_active_blueprint(
                session, course_id, p["id"]
            )
    return _with_generation_task_status(session, course_id, projects)


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
    project = dict(row)
    if not project.get("active_blueprint_version_id"):
        project["active_blueprint_version_id"] = _backfill_active_blueprint(
            session, course_id, project_id
        )
    return _with_generation_task_status(session, course_id, [project])[0]


def update_status(session: Session, course_id: str, project_id: str, status: str) -> dict[str, Any]:
    existing = get_project(session, course_id, project_id)
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == project_id, exam_projects.c.course_id == course_id)
        .values(status=status)
    )
    session.commit()
    return {**existing, "status": status}
