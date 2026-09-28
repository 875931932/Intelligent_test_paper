"""资料库「试卷」文件夹：试卷快照归档（存 / 列 / 详 / 删）。

归档是**独立副本**——存的是题目解析后的完整快照（teacher_override 已合并），
与 ``paper_versions`` 没有外键牵连。这正是它存在的理由：「每个项目只留最近
3 份」的保留策略会把最旧的卷连题面一起物理删掉，而教师存进文件夹的卷必须
在源卷消失后照样可看、可编辑、可下载。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.schema import exam_projects, paper_archives
from app.services.paper_version_service import (
    PaperVersionError,
    get_paper_version,
    resolve_current_paper_version_id,
)


class PaperArchiveError(Exception):
    """试卷归档操作的一般性错误（API 层按消息映射 404/422）。"""


def _nid() -> str:
    return uuid.uuid4().hex[:16]


def _summary(row) -> dict[str, Any]:
    """列表/详情共用的档案字段（不含 snapshot，避免列表页拉爆载荷）。"""
    d = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return {
        "id": d["id"],
        "exam_project_id": d["exam_project_id"],
        "project_name": d.get("project_name"),
        "source_paper_version_id": d.get("source_paper_version_id"),
        "source_version_no": d.get("source_version_no"),
        "name": d["name"],
        "item_count": int(d.get("item_count") or 0),
        "total_score": float(d.get("total_score") or 0.0),
        "created_by": d.get("created_by"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }


def archive_paper_version(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    source_paper_version_id: str | None = None,
    name: str | None = None,
    created_by: str | None = None,
) -> dict:
    """把一份试卷存进资料库文件夹，返回归档档案。

    不指定源卷时取项目当前卷。归档存的是解析后的快照，源卷此后被保留策略
    删除也与它无关。
    """
    try:
        proj = session.execute(
            select(exam_projects.c.id, exam_projects.c.name).where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
        ).one_or_none()
        if proj is None:
            raise PaperArchiveError("exam project not found")
        project_name = proj._mapping["name"]

        if not source_paper_version_id:
            source_paper_version_id = resolve_current_paper_version_id(
                session, course_id=course_id, project_id=project_id
            )

        try:
            pv = get_paper_version(session, source_paper_version_id, course_id=course_id)
        except PaperVersionError as exc:
            raise PaperArchiveError(str(exc)) from exc
        if pv.get("exam_project_id") != project_id:
            raise PaperArchiveError("试卷版本不属于该项目")

        archive_id = _nid()
        title = (name or "").strip() or f"{project_name} v{pv['version_no']}"
        snapshot = {
            "version_no": pv["version_no"],
            "status": pv["status"],
            "total_score": float(pv.get("total_score") or 0.0),
            "questions": pv.get("questions") or [],
        }
        session.execute(
            paper_archives.insert().values(
                id=archive_id,
                course_id=course_id,
                exam_project_id=project_id,
                source_paper_version_id=str(source_paper_version_id),
                source_version_no=pv["version_no"],
                name=title[:255],
                item_count=len(snapshot["questions"]),
                total_score=snapshot["total_score"],
                snapshot=snapshot,
                created_by=created_by,
            )
        )
        session.commit()
    except PaperArchiveError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperArchiveError(f"数据库错误: {exc}") from exc

    return get_paper_archive(session, course_id=course_id, archive_id=archive_id)


def list_paper_archives(session: Session, *, course_id: str) -> list[dict]:
    """课程下全部归档（新 → 旧），只带档案字段不带题目内容。"""
    try:
        rows = session.execute(
            select(
                paper_archives,
                exam_projects.c.name.label("project_name"),
            )
            .select_from(paper_archives)
            .join(exam_projects, exam_projects.c.id == paper_archives.c.exam_project_id)
            .where(paper_archives.c.course_id == course_id)
            .order_by(paper_archives.c.created_at.desc())
        ).all()
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperArchiveError(f"数据库错误: {exc}") from exc
    return [_summary(r) for r in rows]


def get_paper_archive(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
) -> dict:
    """归档详情：档案字段 + 快照内全部题目（供预览/编辑/导出）。"""
    row = session.execute(
        select(
            paper_archives,
            exam_projects.c.name.label("project_name"),
        )
        .select_from(paper_archives)
        .join(exam_projects, exam_projects.c.id == paper_archives.c.exam_project_id)
        .where(
            paper_archives.c.id == archive_id,
            paper_archives.c.course_id == course_id,
        )
    ).one_or_none()
    if row is None:
        raise PaperArchiveError(f"试卷归档不存在: {archive_id}")
    snapshot = dict(row._mapping).get("snapshot") or {}
    return {
        **_summary(row),
        "snapshot": snapshot,
        "questions": snapshot.get("questions") or [],
    }


def delete_paper_archive(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
) -> None:
    """真正从库里删除一份归档（试卷文件夹里的「删除」不做软删）。"""
    try:
        target = session.execute(
            select(paper_archives.c.id).where(
                paper_archives.c.id == archive_id,
                paper_archives.c.course_id == course_id,
            )
        ).one_or_none()
        if target is None:
            raise PaperArchiveError(f"试卷归档不存在: {archive_id}")
        session.execute(
            delete(paper_archives).where(
                paper_archives.c.id == archive_id,
                paper_archives.c.course_id == course_id,
            )
        )
        session.commit()
    except PaperArchiveError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperArchiveError(f"数据库错误: {exc}") from exc
