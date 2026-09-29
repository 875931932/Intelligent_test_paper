"""资料库「试卷」文件夹：试卷快照归档（存 / 列 / 详 / 删）。

归档是**独立副本**——存的是题目解析后的完整快照（teacher_override 已合并），
与 ``paper_versions`` 没有外键牵连。这正是它存在的理由：「每个项目只留最近
3 份」的保留策略会把最旧的卷连题面一起物理删掉，而教师存进文件夹的卷必须
在源卷消失后照样可看、可编辑、可下载。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.schema import exam_projects, generated_questions, paper_archives, paper_items, paper_versions
from app.services.paper_version_service import (
    PaperVersionError,
    # 教师题最小校验（题干/答案必填、选择题答案落在选项上）：归档编辑与试卷页
    # 同口径，直接复用不复制第二份。
    _validate_teacher_item,
    get_paper_version,
    prune_paper_version_history,
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


# ─── 快照编辑（归档在资料库里直接改，不动原试卷） ─────────────────────────


def _snapshot_questions(session: Session, *, course_id: str, archive_id: str) -> tuple[dict, list[dict]]:
    detail = get_paper_archive(session, course_id=course_id, archive_id=archive_id)
    snapshot = dict(detail.get("snapshot") or {})
    questions = [q for q in (snapshot.get("questions") or []) if isinstance(q, dict)]
    return snapshot, questions


def _snapshot_total(questions: list[dict]) -> float:
    return float(sum(float(q.get("score") or 0) for q in questions))


def _save_snapshot(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
    snapshot: dict,
    questions: list[dict],
) -> dict:
    """落快照并同步题数/总分两个档案列——列表页只读档案列，必须与快照同口径。"""
    total = _snapshot_total(questions)
    snapshot = dict(snapshot or {})
    snapshot["questions"] = questions
    snapshot["total_score"] = total
    try:
        session.execute(
            update(paper_archives)
            .where(
                paper_archives.c.id == archive_id,
                paper_archives.c.course_id == course_id,
            )
            .values(
                snapshot=snapshot,
                item_count=len(questions),
                total_score=total,
                updated_at=func.now(),
            )
        )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperArchiveError(f"数据库错误: {exc}") from exc
    return get_paper_archive(session, course_id=course_id, archive_id=archive_id)


def rename_paper_archive(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
    name: str,
) -> dict:
    """改归档名（留空则拒绝——归档名是文件夹里的唯一辨识）。"""
    title = (name or "").strip()
    if not title:
        raise PaperArchiveError("归档名称不能为空")
    try:
        result = session.execute(
            update(paper_archives)
            .where(
                paper_archives.c.id == archive_id,
                paper_archives.c.course_id == course_id,
            )
            .values(name=title[:255], updated_at=func.now())
        )
        if result.rowcount == 0:
            raise PaperArchiveError(f"试卷归档不存在: {archive_id}")
        session.commit()
    except PaperArchiveError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperArchiveError(f"数据库错误: {exc}") from exc
    return get_paper_archive(session, course_id=course_id, archive_id=archive_id)


def update_archive_question(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
    item_index: int,
    patch: dict,
) -> dict:
    """单题补丁：键域与试卷页 teacher_override 相同（stem/options/answer/score/
    question_type/difficulty/explanation/rubric/subquestions…），浅合并进快照题。

    校验与试卷页同口径（_validate_teacher_item）：题干、答案必填，多选答案须落在
    选项上——归档也是要进卷面的题，放行空答案等于把空白答卷导出去。
    """
    if not isinstance(patch, dict) or not patch:
        raise PaperArchiveError("补丁不能为空")
    patch = dict(patch)
    # 与试卷页同款的「已处理完毕」勾选：清掉快照里的待审核标记，
    # 否则教师改完题左栏的黄点永远消不掉（快照没有 quality_checks 可回查）
    clear_review = bool(patch.pop("clear_needs_review", False))
    snapshot, questions = _snapshot_questions(session, course_id=course_id, archive_id=archive_id)
    target = next((q for q in questions if int(q.get("item_index") or 0) == item_index), None)
    if target is None:
        raise PaperArchiveError(f"item_index={item_index} 不在该归档中")

    merged = {**target, **patch}
    try:
        _validate_teacher_item(
            stem=merged.get("stem", ""),
            question_type=str(merged.get("question_type") or ""),
            options=merged.get("options") or [],
            answer=merged.get("answer"),
        )
    except PaperVersionError as exc:
        raise PaperArchiveError(str(exc)) from exc

    if patch:
        target.update(patch)
        # 快照题目是解析后的成品，改一题即等价于试卷页的 teacher_override
        target["has_override"] = True
    if clear_review:
        target["needs_review"] = False
        target["needs_review_reason"] = None
    return _save_snapshot(
        session, course_id=course_id, archive_id=archive_id, snapshot=snapshot, questions=questions
    )


def delete_archive_question(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
    item_index: int,
) -> dict:
    """删题并把其后题号前移 1（与试卷页 delete_paper_item 同语义）。"""
    snapshot, questions = _snapshot_questions(session, course_id=course_id, archive_id=archive_id)
    kept = [q for q in questions if int(q.get("item_index") or 0) != item_index]
    if len(kept) == len(questions):
        raise PaperArchiveError(f"item_index={item_index} 不在该归档中")
    for i, q in enumerate(kept, start=1):
        q["item_index"] = i
    return _save_snapshot(
        session, course_id=course_id, archive_id=archive_id, snapshot=snapshot, questions=kept
    )


def reorder_archive_questions(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
    ordered_indices: list[int],
) -> dict:
    """按给定题号顺序重排（ordered_indices 必须恰好覆盖当前全部题号）。"""
    snapshot, questions = _snapshot_questions(session, course_id=course_id, archive_id=archive_id)
    by_index = {int(q.get("item_index") or 0): q for q in questions}
    if len(ordered_indices) != len(questions) or set(ordered_indices) != set(by_index):
        raise PaperArchiveError("ordered_indices 必须恰好包含当前全部题号且不重复")
    reordered = [by_index[i] for i in ordered_indices]
    for i, q in enumerate(reordered, start=1):
        q["item_index"] = i
    return _save_snapshot(
        session, course_id=course_id, archive_id=archive_id, snapshot=snapshot, questions=reordered
    )


# ─── 存回试卷区 ──────────────────────────────────────────────────────


def restore_archive_to_paper(
    session: Session,
    *,
    course_id: str,
    archive_id: str,
    created_by: str | None = None,
) -> dict:
    """把归档快照存回试卷区：新建一版并设为项目当前卷，返回新卷完整试卷。

    与生成建卷同一语义：version_no 追加、状态 candidate、项目置 review 并切
    active 指针、走「每个项目只留最近 3 份」保留策略（更早版本连题目一并物理
    删除）。归档本身不动——它是独立副本，源卷被修剪也照样留在资料库里。

    每题落一条 generated_questions：generation_run_id / plan_item_id 置空（存回
    的题没有生成 run、也没有蓝图槽位），题面整体进 payload，paper_items 照常
    引用它——读写、导出、待审核、AI 改题等既有链路无需特判。
    """
    try:
        detail = get_paper_archive(session, course_id=course_id, archive_id=archive_id)
        snapshot = dict(detail.get("snapshot") or {})
        questions = [q for q in (snapshot.get("questions") or []) if isinstance(q, dict)]
        if not questions:
            raise PaperArchiveError("归档没有题目，无法存回试卷区")

        project_id = detail["exam_project_id"]
        proj = session.execute(
            select(exam_projects.c.id).where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
        ).one_or_none()
        if proj is None:
            raise PaperArchiveError("归档所属试卷项目已不存在，无法存回试卷区")

        version_no = int(
            session.execute(
                select(func.max(paper_versions.c.version_no)).where(
                    paper_versions.c.exam_project_id == project_id,
                    paper_versions.c.course_id == course_id,
                )
            ).scalar_one_or_none()
            or 0
        ) + 1

        pv_id = _nid()
        gq_rows: list[dict] = []
        item_rows: list[dict] = []
        display_order = 1
        for q in questions:
            gq_id = _nid()
            payload = dict(q)
            # 题号以 paper_items.display_order 为准，payload 里的旧号会随删题/重排失真
            payload.pop("item_index", None)
            gq_rows.append(
                {
                    "id": gq_id,
                    "course_id": course_id,
                    "generation_run_id": None,
                    "plan_item_id": None,
                    "knowledge_card_id": q.get("knowledge_card_id"),
                    "revision_no": 1,
                    "status": "candidate",
                    "payload": payload,
                }
            )
            item_rows.append(
                {
                    "id": _nid(),
                    "course_id": course_id,
                    "paper_version_id": pv_id,
                    "generated_question_id": gq_id,
                    "display_order": display_order,
                    "teacher_override": {},
                    "finalized_text": None,
                    "needs_review": bool(q.get("needs_review")),
                    "needs_review_reason": (str(q.get("needs_review_reason") or "")[:200] or None),
                    "quality_audit": q.get("quality_audit") or {},
                }
            )
            display_order += 1

        session.execute(
            paper_versions.insert().values(
                id=pv_id,
                course_id=course_id,
                exam_project_id=project_id,
                generation_run_id=None,
                version_no=version_no,
                status="candidate",
                created_by=created_by,
                metadata={
                    "created_from": "archive_restore",
                    "archive_id": archive_id,
                    "archive_name": detail.get("name"),
                    "source_version_no": detail.get("source_version_no"),
                },
            )
        )
        session.execute(generated_questions.insert(), gq_rows)
        session.execute(paper_items.insert(), item_rows)

        # 与生成建卷同口径：新卷成为当前卷，项目回到待审核
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
            .values(status="review", active_paper_version_id=pv_id)
        )

        # 历史保留策略与生成同事务：删失败则整次存回回滚
        prune_paper_version_history(session, course_id=course_id, project_id=project_id)
        session.commit()
    except PaperArchiveError:
        session.rollback()
        raise
    except PaperVersionError as exc:
        session.rollback()
        raise PaperArchiveError(str(exc)) from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperArchiveError(f"数据库错误: {exc}") from exc

    return get_paper_version(session, pv_id, course_id=course_id)


# ─── 导出（与 get_paper_version 同构的 pv dict，喂给既有渲染器） ──────────


def load_archive_export_paper(session: Session, *, course_id: str, archive_id: str) -> dict:
    """归档 → 可直接交给 export_* 渲染器的 pv dict（同构、不落库）。

    version_no/total_score/status 来自快照，project_name 来自项目行（试卷页导出
    的 _paper_meta 同字段），questions 即快照题目。
    """
    detail = get_paper_archive(session, course_id=course_id, archive_id=archive_id)
    snapshot = detail.get("snapshot") or {}
    total = snapshot.get("total_score")
    if total is None:
        total = detail.get("total_score") or 0.0
    return {
        "id": detail["id"],
        "exam_project_id": detail["exam_project_id"],
        "project_name": detail.get("project_name") or "",
        "version_no": int(snapshot.get("version_no") or detail.get("source_version_no") or 1),
        "status": str(snapshot.get("status") or ""),
        "total_score": float(total),
        "questions": detail.get("questions") or [],
    }
