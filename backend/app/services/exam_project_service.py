"""exam_projects 表的最小 service：list/create/get/update_status/delete。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.schema import (
    blueprint_sections,
    blueprint_versions,
    exam_projects,
    generation_attempts,
    generated_questions,
    generation_runs,
    paper_items,
    paper_versions,
    plan_items,
    quality_checks,
    task_runs,
)
from app.services.paper_version_service import summarize_paper_versions_for_projects


class ExamProjectConflictError(Exception):
    """项目名在课程内已存在。"""


class ExamProjectNotFoundError(Exception):
    """项目不存在。"""


def get_current_contract_snapshot(
    session: Session, *, course_id: str, project_id: str,
) -> dict[str, Any] | None:
    """读取项目当前已确认的合同快照。

    合同快照持久化在 ``generation_runs.contract_snapshot``，由
    ``exam_projects.active_generation_run_id`` 指向。这是“退出项目再进入”后
    恢复合同界面的权威数据源 —— 前端不应依赖 allocate 时留在内存里的响应，
    也不应依赖任务进度是否仍在进行中。

    返回 None 表示该项目尚未确认过合同（含蓝图未确认、只 allocate 未 confirm）。
    """
    run_id = session.execute(
        select(exam_projects.c.active_generation_run_id).where(
            exam_projects.c.course_id == course_id,
            exam_projects.c.id == project_id,
        )
    ).scalar_one_or_none()
    if not run_id:
        return None
    row = session.execute(
        select(generation_runs).where(generation_runs.c.id == run_id)
    ).mappings().first()
    if row is None:
        return None
    snap = dict(row.get("contract_snapshot") or {})
    if not snap:
        return None

    slots = list(snap.get("slots") or [])
    # 落库快照不含顶层 total_score（PaperContract.total_score 与槽位求和是两个
    # 独立值），这里按槽位求和补齐，与前端既有的兜底逻辑保持一致。
    total_score = snap.get("total_score")
    if total_score is None:
        total_score = sum(float(s.get("score") or 0) for s in slots)

    # conflicts 归一化：allocate 的响应放顶层，落库快照放在 conflicts_pre_vs_post
    conflicts = snap.get("conflicts")
    if conflicts is None:
        pre_post = snap.get("conflicts_pre_vs_post") or {}
        conflicts = [
            c for key in ("pre_revision", "post_revision") for c in (pre_post.get(key) or [])
        ]

    result: dict[str, Any] = {
        "generation_run_id": run_id,
        "slots": slots,
        "total_score": total_score,
        "conflicts": conflicts,
        "audit_summary": snap.get("audit_summary") or {},
    }
    # 附带诊断信息，便于前端展示阈值/修订痕迹/分配方案（均为可选）
    for key in ("slot_revisions_applied", "conflicts_history", "allocation_seed"):
        if snap.get(key) is not None:
            result[key] = snap.get(key)
    threshold = snap.get("centrality_threshold_used")
    if threshold is None:
        threshold = row.get("centrality_threshold_used")
    if threshold is not None:
        result["centrality_threshold_used"] = threshold
    return result


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


def _with_paper_summary(session: Session, course_id: str, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """附带项目当前试卷版本的分值/题数，供列表卡片与详情头部展示。

    exam_projects 表不存分值、题数（它们由蓝图 plan_items 决定），历史上这两个
    字段后端从不填充，前端只能恒显示“尚未生成试卷”。这里按与审核/导出完全
    同一套版本解析规则（paper_version_service）取当前版本并汇总，保证头部
    显示的就是审核页正在看的那一版卷子。无任何试卷版本的项目补 None，
    前端据此显示“尚未生成试卷 / 待生成”。
    """
    if not projects:
        return projects
    summaries = summarize_paper_versions_for_projects(
        session,
        course_id=course_id,
        project_ids=[p["id"] for p in projects],
    )
    for project in projects:
        summary = summaries.get(project.get("id"))
        if summary:
            project.update(
                paper_version_id=summary["paper_version_id"],
                paper_version_no=summary["version_no"],
                paper_version_status=summary["status"],
                total_score=summary["total_score"],
                item_count=summary["item_count"],
            )
        else:
            project.update(
                paper_version_id=None,
                paper_version_no=None,
                paper_version_status=None,
                total_score=None,
                item_count=None,
            )
    return projects


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
    projects = _with_paper_summary(session, course_id, projects)
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
    project = _with_paper_summary(session, course_id, [project])[0]
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


def delete_project(session: Session, course_id: str, project_id: str) -> None:
    """删除项目及其全部派生数据（蓝图/题位/生成运行/题目/试卷版本/任务记录）。

    没有配置级联删除，这里按外键依赖自底向上清理：quality_checks →
    paper_items → generated_questions → generation_attempts/generation_runs →
    paper_versions → plan_items/blueprint_sections → blueprint_versions →
    task_runs（payload 里带 project_id，无外键但同属该项目痕迹）→ exam_projects。
    整个操作在一个事务里，任一步失败整体回滚，不会留下半删的项目。
    """
    get_project(session, course_id, project_id)

    run_ids = list(session.execute(
        select(generation_runs.c.id)
        .join(blueprint_versions, blueprint_versions.c.id == generation_runs.c.blueprint_version_id)
        .where(
            generation_runs.c.course_id == course_id,
            blueprint_versions.c.exam_project_id == project_id,
        )
    ).scalars().all())
    pv_ids = list(session.execute(
        select(paper_versions.c.id).where(
            paper_versions.c.course_id == course_id,
            paper_versions.c.exam_project_id == project_id,
        )
    ).scalars().all())
    bv_ids = list(session.execute(
        select(blueprint_versions.c.id).where(
            blueprint_versions.c.course_id == course_id,
            blueprint_versions.c.exam_project_id == project_id,
        )
    ).scalars().all())

    try:
        if run_ids:
            gq_ids = list(session.execute(
                select(generated_questions.c.id)
                .where(generated_questions.c.generation_run_id.in_(run_ids))
            ).scalars().all())
            if gq_ids:
                session.execute(delete(quality_checks).where(quality_checks.c.generated_question_id.in_(gq_ids)))
                session.execute(delete(paper_items).where(paper_items.c.generated_question_id.in_(gq_ids)))
                session.execute(delete(generated_questions).where(generated_questions.c.id.in_(gq_ids)))
        if pv_ids:
            session.execute(delete(paper_items).where(paper_items.c.paper_version_id.in_(pv_ids)))
            session.execute(delete(paper_versions).where(paper_versions.c.id.in_(pv_ids)))
        if run_ids:
            session.execute(delete(generation_attempts).where(generation_attempts.c.generation_run_id.in_(run_ids)))
            session.execute(delete(generation_runs).where(generation_runs.c.id.in_(run_ids)))
        if bv_ids:
            session.execute(delete(plan_items).where(plan_items.c.blueprint_version_id.in_(bv_ids)))
            session.execute(delete(blueprint_sections).where(blueprint_sections.c.blueprint_version_id.in_(bv_ids)))
            session.execute(delete(blueprint_versions).where(blueprint_versions.c.id.in_(bv_ids)))
        # task_runs 无外键，按 payload.project_id 清理该项目留下的任务痕迹
        session.execute(
            text("DELETE FROM task_runs WHERE course_id = :cid AND payload->>'project_id' = :pid"),
            {"cid": course_id, "pid": project_id},
        )
        session.execute(
            delete(exam_projects).where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
