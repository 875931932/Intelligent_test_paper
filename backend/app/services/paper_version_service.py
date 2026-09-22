"""试卷版本服务：从生成结果创建 candidate → 评审覆写 → 确认/回滚。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.schema import (
    exam_projects,
    generated_questions,
    generation_runs,
    paper_items,
    paper_versions,
    plan_items,
)


class PaperVersionError(Exception):
    """试卷版本操作的一般性错误。"""


class PendingNeedsReview(PaperVersionError):
    """确认试卷时仍有 needs_review=True 的项目未处理。"""

    def __init__(self, message: str, item_indices: list[int] | None = None):
        super().__init__(message)
        self.item_indices = item_indices or []


class Conflict(PaperVersionError):
    """对已 finalized 的试卷进行变更（HTTP 语义 409）。"""


def _nid() -> str:
    return uuid.uuid4().hex[:16]


def _row_to_dict(row) -> dict[str, Any]:
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row._asdict()) if hasattr(row, "_asdict") else dict(row)


def pick_current_paper_version_id(
    active_paper_version_id: str | None,
    versions: list[dict],
) -> str | None:
    """从项目的试卷版本中挑选“当前正在处理”的版本 id。

    判定规则（按优先级）：

    1. 最新一个**未定稿**（status != 'finalized'）的版本 —— 生成刚完成、
       待教师审核/修订的 candidate；
    2. ``exam_projects.active_paper_version_id`` —— 已定稿导出的版本，项目
       没有待审 candidate 时（导出、定稿回看）用的就是它；
    3. 版本号最大的版本 —— 遗留数据兜底。

    三条规则共用同一语义：审核流需要的是“项目当前这一版试卷”。
    ``active_paper_version_id`` 只在确认定稿时回写、回滚时清空，若把它当作
    审核门禁，生成成功后前端永远拿不到候选版本（报“请先在生成阶段完成生成”），
    确认后重新生成也会拿到陈旧旧卷。
    """
    pending = [v for v in versions if v.get("status") != "finalized"]
    if pending:
        return str(max(pending, key=lambda v: v["version_no"])["id"])
    if active_paper_version_id:
        return str(active_paper_version_id)
    if not versions:
        return None
    return str(max(versions, key=lambda v: v["version_no"])["id"])


def resolve_current_paper_version_id(
    session: Session,
    *,
    course_id: str,
    project_id: str,
) -> str:
    """解析项目当前试卷版本 id，规则见 ``pick_current_paper_version_id``。

    项目不存在或尚无任何试卷版本时抛 ``PaperVersionError``（API 层映射 404）。
    """
    proj = session.execute(
        select(exam_projects.c.active_paper_version_id).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).one_or_none()
    if proj is None:
        raise PaperVersionError("exam project not found")
    version_rows = session.execute(
        select(
            paper_versions.c.id,
            paper_versions.c.version_no,
            paper_versions.c.status,
        )
        .where(
            paper_versions.c.exam_project_id == project_id,
            paper_versions.c.course_id == course_id,
        )
    ).mappings().all()
    resolved = pick_current_paper_version_id(
        proj._mapping["active_paper_version_id"],
        [dict(r) for r in version_rows],
    )
    if resolved is None:
        raise PaperVersionError("no paper version exists for project")
    return resolved


def summarize_paper_versions_for_projects(
    session: Session,
    *,
    course_id: str,
    project_ids: list[str],
) -> dict[str, dict]:
    """批量汇总各项目当前试卷版本的关键指标。

    返回 ``{project_id: {paper_version_id, version_no, status,
    total_score, item_count}}``；尚无任何试卷版本的项目不出现在结果里。
    无版本项目的键由调用方补 None，保证接口字段稳定。
    """
    if not project_ids:
        return {}
    active_rows = session.execute(
        select(exam_projects.c.id, exam_projects.c.active_paper_version_id).where(
            exam_projects.c.course_id == course_id,
            exam_projects.c.id.in_(project_ids),
        )
    ).mappings().all()
    active_by_project = {r["id"]: r["active_paper_version_id"] for r in active_rows}

    version_rows = session.execute(
        select(
            paper_versions.c.exam_project_id,
            paper_versions.c.id,
            paper_versions.c.version_no,
            paper_versions.c.status,
        )
        .where(
            paper_versions.c.course_id == course_id,
            paper_versions.c.exam_project_id.in_(project_ids),
        )
    ).mappings().all()
    by_project: dict[str, list[dict]] = {}
    for row in version_rows:
        by_project.setdefault(row["exam_project_id"], []).append(dict(row))

    chosen: dict[str, str] = {}
    for pid, versions in by_project.items():
        picked = pick_current_paper_version_id(active_by_project.get(pid), versions)
        if picked is not None:
            chosen[pid] = picked
    if not chosen:
        return {}

    aggregate_rows = session.execute(
        select(
            paper_items.c.paper_version_id,
            func.count(paper_items.c.id).label("item_count"),
            func.coalesce(func.sum(plan_items.c.score), 0.0).label("total_score"),
        )
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .join(plan_items, plan_items.c.id == generated_questions.c.plan_item_id, isouter=True)
        .where(paper_items.c.paper_version_id.in_(list(chosen.values())))
        .group_by(paper_items.c.paper_version_id)
    ).all()
    stats = {
        r._mapping["paper_version_id"]: (r._mapping["item_count"], r._mapping["total_score"])
        for r in aggregate_rows
    }

    summaries: dict[str, dict] = {}
    for pid, pv_id in chosen.items():
        version = next(v for v in by_project[pid] if str(v["id"]) == pv_id)
        item_count, total_score = stats.get(pv_id, (0, 0.0))
        summaries[pid] = {
            "paper_version_id": pv_id,
            "version_no": version["version_no"],
            "status": version["status"],
            "total_score": float(total_score or 0.0),
            "item_count": int(item_count or 0),
        }
    return summaries


def create_paper_version_from_generation(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    generation_run_id: str,
    questions_list: list[dict],
) -> str:
    """从 generation 结果创建 candidate 试卷版本。

    questions_list 每项：{'plan_item_id': str, 'quality': {...}, ...}
    """
    try:
        proj = session.execute(
            select(exam_projects).where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
        ).one_or_none()
        if proj is None:
            raise PaperVersionError(f"项目不存在: {project_id}")

        # 计算 version_no
        current_max = session.execute(
            select(func.max(paper_versions.c.version_no))
            .where(
                paper_versions.c.exam_project_id == project_id,
                paper_versions.c.course_id == course_id,
            )
        ).scalar_one_or_none() or 0
        version_no = int(current_max) + 1

        # 插入 paper_version
        pv_id = _nid()
        session.execute(
            paper_versions.insert().values(
                id=pv_id,
                course_id=course_id,
                exam_project_id=project_id,
                generation_run_id=generation_run_id,
                version_no=version_no,
                status="candidate",
                metadata={
                    "generation_run_id": generation_run_id,
                    "created_from": "generation_service",
                },
            )
        )

        # 按 plan_item.item_index 对 questions_list 排序
        plan_item_ids = [q.get("plan_item_id") for q in questions_list]
        # 查每个 plan_item 的 item_index 用于排序
        order_map: dict[str, int] = {}
        if plan_item_ids:
            rows = session.execute(
                select(plan_items.c.id, plan_items.c.item_index)
                .where(
                    plan_items.c.id.in_([pid for pid in plan_item_ids if pid]),
                    plan_items.c.course_id == course_id,
                )
            ).all()
            for r in rows:
                order_map[r._mapping["id"]] = r._mapping["item_index"]

        # 为没有关联 plan_item 的题目给大的 item_index 兜底
        indexed = sorted(
            enumerate(questions_list),
            key=lambda pair: (
                order_map.get(pair[1].get("plan_item_id") or "", 10**9),
                pair[0],
            ),
        )

        display_order = 1
        pi_items_rows = []
        # 建立 gq 关联：先根据 plan_item_id 在 generated_questions 表查
        # 注意：调用方应在本函数之前插入 generated_questions 并 flush，
        # 这里按 plan_item_id + generation_run_id 查询
        gq_rows = session.execute(
            select(
                generated_questions.c.id,
                generated_questions.c.plan_item_id,
                generated_questions.c.payload,
            )
            .where(
                generated_questions.c.generation_run_id == generation_run_id,
                generated_questions.c.course_id == course_id,
                generated_questions.c.revision_no == 1,
            )
        ).all()
        gq_by_plan_item: dict[str, list[dict]] = {}
        for r in gq_rows:
            d = dict(r._mapping)
            gq_by_plan_item.setdefault(d["plan_item_id"], []).append(d)

        for original_idx, q in indexed:
            pi_id = q.get("plan_item_id") or ""
            quality = q.get("quality") or {}
            # 判断 needs_review
            needs_review = False
            review_reasons: list[str] = []
            if quality.get("needs_review"):
                needs_review = True
                review_reasons.append(str(quality.get("message") or "质量检查标记"))
            for check in quality.get("quality_checks") or []:
                if check.get("status") != "pass":
                    needs_review = True
                    review_reasons.append(
                        f"{check.get('check_type', '?')}:{check.get('status')}"
                    )
                    if check.get("details") and check["details"].get("message"):
                        review_reasons.append(str(check["details"]["message"])[:80])

            gq_list = gq_by_plan_item.get(pi_id, [])
            if not gq_list:
                # 找不到对应 gq：跳过（应至少有匹配）
                continue
            gq = gq_list.pop(0)
            gq_id = gq["id"]

            pi_items_rows.append({
                "id": _nid(),
                "course_id": course_id,
                "paper_version_id": pv_id,
                "generated_question_id": gq_id,
                "display_order": display_order,
                "teacher_override": {},
                "finalized_text": None,
                "needs_review": needs_review,
                "needs_review_reason": "；".join(review_reasons)[:200] if needs_review else None,
                "quality_audit": {
                    "needs_review": needs_review,
                    "quality": quality,
                    "quality_checks": list(quality.get("quality_checks") or []),
                    "generation_run_id": generation_run_id,
                },
            })
            display_order += 1

        if pi_items_rows:
            session.execute(paper_items.insert(), pi_items_rows)

        # 更新项目状态为 review
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
            .values(status="review")
        )

        session.commit()
        return pv_id

    except PaperVersionError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


def get_paper_version(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
) -> dict:
    """读取某试卷版本，附带排序后的题目列表与覆写解析。"""
    stmt = (
        select(
            paper_versions,
            generation_runs.c.id.label("gen_run_id"),
            exam_projects.c.id.label("proj_id"),
            exam_projects.c.status.label("project_status"),
        )
        .select_from(paper_versions)
        .join(generation_runs, generation_runs.c.id == paper_versions.c.generation_run_id, isouter=True)
        .join(exam_projects, exam_projects.c.id == paper_versions.c.exam_project_id)
        .where(
            paper_versions.c.id == paper_version_id,
            paper_versions.c.course_id == course_id,
            exam_projects.c.course_id == course_id,
        )
    )
    pv_row = session.execute(stmt).one_or_none()
    if pv_row is None:
        raise PaperVersionError(f"试卷版本不存在或不属于该课程: {paper_version_id}")
    pv = dict(pv_row._mapping)

    item_rows = session.execute(
        select(
            paper_items.c.display_order,
            paper_items.c.teacher_override,
            paper_items.c.needs_review,
            paper_items.c.needs_review_reason,
            paper_items.c.quality_audit,
            paper_items.c.finalized_text,
            generated_questions.c.payload.label("gq_payload"),
            generated_questions.c.plan_item_id,
            generated_questions.c.knowledge_card_id,
            plan_items.c.score.label("plan_score"),
            plan_items.c.exam_point_id.label("plan_exam_point_id"),
        )
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .join(plan_items, plan_items.c.id == generated_questions.c.plan_item_id, isouter=True)
        .where(
            paper_items.c.paper_version_id == paper_version_id,
            paper_items.c.course_id == course_id,
        )
        .order_by(paper_items.c.display_order)
    ).all()

    questions_out = []
    for r in item_rows:
        d = dict(r._mapping)
        payload = d.get("gq_payload") or {}
        override = d.get("teacher_override") or {}
        # 派生 stem/options/answer：override 优先
        stem = override.get("stem", payload.get("stem", ""))
        options = override.get("options", payload.get("options", []))
        answer = override.get("answer", payload.get("answer", ""))
        # 分值/考点以 plan_items 为准（合同同口径）：payload 的盖章值是生成时
        # 快照，plan_items 是蓝图槽位的权威来源，两者在正常链路一致；对未关联
        # plan_item 的历史数据退化为 payload，再退化为 0。
        plan_score = d.get("plan_score")
        if plan_score is None:
            plan_score = payload.get("score", 0.0)
        questions_out.append({
            "item_index": d["display_order"],
            "plan_item_id": d.get("plan_item_id"),
            "knowledge_card_id": d.get("knowledge_card_id"),
            "exam_point_id": payload.get("exam_point_id") or d.get("plan_exam_point_id"),
            "stem": stem,
            "options": options,
            "answer": answer,
            "explanation": override.get("explanation", payload.get("explanation")),
            "score": override.get("score", plan_score),
            "question_type": override.get("question_type") or payload.get("question_type"),
            "difficulty": override.get("difficulty") or payload.get("difficulty"),
            "cognitive_level": override.get("cognitive_level") or payload.get("cognitive_level"),
            "needs_review": d.get("needs_review", False),
            "needs_review_reason": d.get("needs_review_reason"),
            "teacher_override": override,
            "has_override": bool(override),
            "finalized_text": d.get("finalized_text"),
            "quality_audit": d.get("quality_audit") or {},
        })
    # 汇总总分：按每题解析后的分值求和（teacher_override.score 优先于
    # plan_items 原值），确保教师改分后总分实时反映，供试卷中心“总分 N 分”展示。
    pv["total_score"] = float(sum(q.get("score") or 0 for q in questions_out))

    pv["questions"] = questions_out
    return pv


def list_needs_review(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
    filters: dict | None = None,
) -> list[dict]:
    """列出 needs_review=True 的 paper_items，按 item_index 升序。"""
    stmt = (
        select(
            paper_items.c.display_order.label("item_index"),
            paper_items.c.needs_review,
            paper_items.c.needs_review_reason,
            paper_items.c.quality_audit,
            generated_questions.c.payload.label("gq_payload"),
            plan_items.c.exam_point_id,
            plan_items.c.knowledge_card_id.label("card_id"),
            plan_items.c.question_type,
        )
        .select_from(paper_items)
        .join(paper_versions, paper_versions.c.id == paper_items.c.paper_version_id)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .join(plan_items, plan_items.c.id == generated_questions.c.plan_item_id)
        .where(
            paper_items.c.paper_version_id == paper_version_id,
            paper_versions.c.course_id == course_id,
            paper_items.c.needs_review.is_(True),
        )
        .order_by(paper_items.c.display_order)
    )
    rows = [dict(r._mapping) for r in session.execute(stmt).all()]

    if filters:
        lo = filters.get("item_index_min")
        hi = filters.get("item_index_max")
        qtype = filters.get("question_type")
        if lo is not None:
            rows = [r for r in rows if r["item_index"] >= lo]
        if hi is not None:
            rows = [r for r in rows if r["item_index"] <= hi]
        if qtype:
            rows = [r for r in rows if r.get("question_type") == qtype]

    out = []
    for r in rows:
        q = r.get("gq_payload") or {}
        qa = r.get("quality_audit") or {}
        out.append({
            "item_index": r["item_index"],
            "question_type": r.get("question_type") or q.get("question_type"),
            "needs_review_reason": r.get("needs_review_reason") or "",
            "quality_message": (qa.get("quality") or {}).get("message", ""),
            "exam_point_id": r.get("exam_point_id"),
            "card_id": r.get("card_id"),
        })
    return out


def update_paper_item(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    item_index: int,
    teacher_override_patch: dict,
    clear_needs_review: bool = False,
) -> dict:
    """覆写单题 teacher_override 字段；可同时清除 needs_review 标记。"""
    try:
        # 校验 paper_version 归属且未 finalized
        pv_row = session.execute(
            select(
                paper_versions.c.id,
                paper_versions.c.status,
                paper_versions.c.course_id,
                exam_projects.c.course_id.label("proj_course_id"),
            )
            .select_from(paper_versions)
            .join(exam_projects, exam_projects.c.id == paper_versions.c.exam_project_id)
            .where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
                exam_projects.c.course_id == course_id,
            )
        ).one_or_none()
        if pv_row is None:
            raise PaperVersionError("试卷版本不存在或不属于课程")
        if pv_row._mapping["status"] == "finalized":
            raise Conflict("paper version finalized")

        # 找到 paper_item
        pi = session.execute(
            select(paper_items, generated_questions.c.payload.label("gq_payload"))
            .select_from(paper_items)
            .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
            .where(
                paper_items.c.paper_version_id == paper_version_id,
                paper_items.c.display_order == item_index,
                paper_items.c.course_id == course_id,
            )
        ).one_or_none()
        if pi is None:
            raise PaperVersionError(f"item_index={item_index} 不在该试卷版本中")
        pi_data = dict(pi._mapping)

        # 合并 teacher_override（浅合并）
        existing_override = pi_data.get("teacher_override") or {}
        new_override = dict(existing_override)
        new_override.update(teacher_override_patch or {})

        # 计算 finalized_text
        payload = pi_data.get("gq_payload") or {}
        finalized = {
            "stem": new_override.get("stem", payload.get("stem", "")),
            "options": new_override.get("options", payload.get("options", [])),
            "answer": new_override.get("answer", payload.get("answer", "")),
        }
        # 若覆写里有更多字段也带入
        for extra_key in ("question_type", "difficulty", "cognitive_level"):
            if extra_key in new_override:
                finalized[extra_key] = new_override[extra_key]
            elif extra_key in payload:
                finalized[extra_key] = payload[extra_key]

        values: dict[str, Any] = {
            "teacher_override": new_override,
            "finalized_text": finalized,
        }
        if clear_needs_review:
            values["needs_review"] = False
            values["needs_review_reason"] = None

        session.execute(
            paper_items.update()
            .where(
                paper_items.c.id == pi_data["id"],
                paper_items.c.course_id == course_id,
            )
            .values(**values)
        )
        session.commit()

        refreshed = session.execute(
            select(paper_items).where(paper_items.c.id == pi_data["id"])
        ).one()
        return _row_to_dict(refreshed)

    except (PaperVersionError, Conflict):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


def reorder_paper_items(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    ordered_indices: list[int],
) -> dict:
    """按新顺序重排试卷题目。

    ``ordered_indices`` 给出新顺序：其第 i 个元素是"当前第几个题的旧题号"。
    例如 [3, 1, 2] 表示把原第 3 题放到第 1 位、原第 1 题第 2 位、原第 2 题第 3 位。
    display_order 会写回为 1..N。
    """
    try:
        pv = session.execute(
            select(paper_versions.c.status).where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
        ).one_or_none()
        if pv is None:
            raise PaperVersionError("试卷版本不存在或不属于课程")
        if pv._mapping["status"] == "finalized":
            raise Conflict("paper version finalized")

        rows = session.execute(
            select(paper_items.c.id, paper_items.c.display_order).where(
                paper_items.c.course_id == course_id,
                paper_items.c.paper_version_id == paper_version_id,
            )
        ).all()
        by_order = {r._mapping["display_order"]: r._mapping["id"] for r in rows}
        if len(ordered_indices) != len(rows) or set(ordered_indices) != set(by_order):
            raise PaperVersionError("ordered_indices 必须恰好包含当前全部题号且不重复")

        # 分两步（先临时偏移再写回 1..N），避开 display_order 唯一约束冲突
        for new_pos, old in enumerate(ordered_indices, start=1):
            session.execute(
                update(paper_items)
                .where(paper_items.c.id == by_order[old], paper_items.c.course_id == course_id)
                .values(display_order=-(new_pos + 10_000))
            )
        for new_pos, old in enumerate(ordered_indices, start=1):
            session.execute(
                update(paper_items)
                .where(paper_items.c.id == by_order[old], paper_items.c.course_id == course_id)
                .values(display_order=new_pos)
            )
        session.commit()
        return {"status": "ok", "item_count": len(rows)}
    except (PaperVersionError, Conflict):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


def create_paper_item(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    stem: str,
    question_type: str = "short_answer",
    options: list | None = None,
    answer: str = "",
    explanation: str = "",
    score: float = 0.0,
    difficulty: str = "medium",
) -> dict:
    """在试卷末尾新增一道教师自拟题目。

    生成一条 plan_item 槽位（接在蓝图既有槽位之后）＋ generated_question ＋
    paper_item，返回刷新后的完整试卷。新增题标记 payload.teacher_added=true。
    """
    try:
        pv = session.execute(
            select(
                paper_versions.c.status,
                paper_versions.c.generation_run_id,
                generation_runs.c.blueprint_version_id,
            )
            .select_from(paper_versions)
            .join(generation_runs, generation_runs.c.id == paper_versions.c.generation_run_id)
            .where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
        ).one_or_none()
        if pv is None:
            raise PaperVersionError("试卷版本不存在或不属于课程")
        p = dict(pv._mapping)
        if p["status"] == "finalized":
            raise Conflict("paper version finalized")
        bp_id = p.get("blueprint_version_id")
        run_id = p.get("generation_run_id")
        if not bp_id or not run_id:
            raise PaperVersionError("该试卷版本缺少生成 run / 蓝图信息，无法新增题目")

        # 复用同蓝图下任意一个 assessment_unit_id（新增题不绑定具体考核单元）
        assessment_unit = session.execute(
            select(plan_items.c.assessment_unit_id)
            .where(plan_items.c.blueprint_version_id == bp_id, plan_items.c.course_id == course_id)
            .limit(1)
        ).scalar_one_or_none()
        if assessment_unit is None:
            raise PaperVersionError("蓝图无考核单元可关联，无法新增题目")

        max_idx = session.execute(
            select(func.max(plan_items.c.item_index))
            .where(plan_items.c.blueprint_version_id == bp_id, plan_items.c.course_id == course_id)
        ).scalar() or 0

        new_plan_id = _nid()
        session.execute(
            plan_items.insert().values(
                id=new_plan_id,
                course_id=course_id,
                blueprint_version_id=bp_id,
                assessment_unit_id=assessment_unit,
                question_type=question_type,
                item_index=max_idx + 1,
                score=score or 0.0,
                difficulty=difficulty,
                cognitive_level="understand",
                assessment_mode="conceptual",
                exam_point_id=None,
                knowledge_card_id=None,
            )
        )

        new_gq_id = _nid()
        session.execute(
            generated_questions.insert().values(
                id=new_gq_id,
                course_id=course_id,
                generation_run_id=run_id,
                plan_item_id=new_plan_id,
                revision_no=1,
                status="candidate",
                payload={
                    "stem": stem,
                    "options": options or [],
                    "answer": answer,
                    "explanation": explanation,
                    "score": score or 0.0,
                    "question_type": question_type,
                    "difficulty": difficulty,
                    "cognitive_level": "understand",
                    "teacher_added": True,
                },
            )
        )

        max_order = session.execute(
            select(func.max(paper_items.c.display_order))
            .where(paper_items.c.paper_version_id == paper_version_id, paper_items.c.course_id == course_id)
        ).scalar() or 0
        session.execute(
            paper_items.insert().values(
                id=_nid(),
                course_id=course_id,
                paper_version_id=paper_version_id,
                generated_question_id=new_gq_id,
                display_order=max_order + 1,
                teacher_override={},
                needs_review=False,
                quality_audit={},
                finalized_text=None,
            )
        )
        session.commit()
        return get_paper_version(session, paper_version_id, course_id=course_id)

    except (PaperVersionError, Conflict):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


def delete_paper_item(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    item_index: int,
) -> dict:
    """删除指定题目，并把其后所有题 display_order 前移 1。"""
    try:
        pv = session.execute(
            select(paper_versions.c.status).where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
        ).one_or_none()
        if pv is None:
            raise PaperVersionError("试卷版本不存在或不属于课程")
        if pv._mapping["status"] == "finalized":
            raise Conflict("paper version finalized")

        pi_id = session.execute(
            select(paper_items.c.id).where(
                paper_items.c.course_id == course_id,
                paper_items.c.paper_version_id == paper_version_id,
                paper_items.c.display_order == item_index,
            )
        ).scalar_one_or_none()
        if pi_id is None:
            raise PaperVersionError(f"item_index={item_index} 不在该试卷版本中")

        session.execute(
            delete(paper_items).where(paper_items.c.id == pi_id, paper_items.c.course_id == course_id)
        )
        session.execute(
            update(paper_items)
            .where(
                paper_items.c.course_id == course_id,
                paper_items.c.paper_version_id == paper_version_id,
                paper_items.c.display_order > item_index,
            )
            .values(display_order=paper_items.c.display_order - 1)
        )
        session.commit()
        return get_paper_version(session, paper_version_id, course_id=course_id)

    except (PaperVersionError, Conflict):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


def confirm_paper_version(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    force_ignore_needs_review: bool = False,
) -> dict:
    """确认试卷为 finalized 状态，更新项目 active_paper_version_id。"""
    try:
        pv = session.execute(
            select(paper_versions).where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
        ).one_or_none()
        if pv is None:
            raise PaperVersionError(f"试卷版本不存在: {paper_version_id}")
        pv_data = dict(pv._mapping)
        if pv_data["status"] == "finalized":
            raise Conflict("paper version already finalized")

        # 统计 needs_review=true 的 item
        pending_rows = session.execute(
            select(paper_items.c.display_order)
            .where(
                paper_items.c.paper_version_id == paper_version_id,
                paper_items.c.course_id == course_id,
                paper_items.c.needs_review.is_(True),
            )
            .order_by(paper_items.c.display_order)
        ).all()
        pending_count = len(pending_rows)
        pending_indices = [r._mapping["display_order"] for r in pending_rows]
        if pending_count > 0 and not force_ignore_needs_review:
            raise PendingNeedsReview(
                f"{pending_count} items pending needs_review",
                item_indices=pending_indices,
            )

        # 更新 paper_version
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        confirmed_at = pv_data.get("confirmed_at") or now
        session.execute(
            paper_versions.update()
            .where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
            .values(
                status="finalized",
                finalized_at=now,
                confirmed_at=confirmed_at,
            )
        )
        # 更新 exam_projects
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == pv_data["exam_project_id"],
                exam_projects.c.course_id == course_id,
            )
            .values(
                active_paper_version_id=paper_version_id,
                status="exported",
            )
        )

        session.commit()
        return {
            "status": "finalized",
            "finalized_at": now,
            "unresolved": pending_count if force_ignore_needs_review else 0,
        }

    except (PaperVersionError, Conflict, PendingNeedsReview):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


def revert_to_candidate(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
) -> dict:
    """撤销 finalized：candidate 回退 + 项目状态回 review 并清空引用。"""
    try:
        pv = session.execute(
            select(paper_versions).where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
        ).one_or_none()
        if pv is None:
            raise PaperVersionError(f"试卷版本不存在: {paper_version_id}")
        pv_data = dict(pv._mapping)
        if pv_data["status"] != "finalized":
            raise Conflict("only finalized paper versions can be reverted")

        session.execute(
            paper_versions.update()
            .where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
            .values(
                status="candidate",
                finalized_at=None,
            )
        )
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.active_paper_version_id == paper_version_id,
                exam_projects.c.course_id == course_id,
            )
            .values(
                active_paper_version_id=None,
                status="review",
            )
        )
        session.commit()
        return {"status": "candidate", "paper_version_id": paper_version_id}
    except (PaperVersionError, Conflict):
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise PaperVersionError(f"数据库错误: {exc}") from exc


# ─── 导出 ───────────────────────────────────────────────────

def _q_type_label(qt: str | None) -> str:
    labels = {
        "single_choice": "单选题",
        "multiple_choice": "多选题",
        "true_false": "判断题",
        "short_answer": "简答题",
        "essay": "论述题",
        "fill_blank": "填空题",
        "calculation": "计算题",
    }
    return labels.get(qt or "", qt or "题")


def _difficulty_label(d: Any) -> str:
    labels = {1: "容易", 2: "较易", 3: "中等", 4: "较难", 5: "困难"}
    if isinstance(d, (int, float)):
        return labels.get(int(d), str(d))
    return str(d) if d else "未知"


def export_answer_detail_json(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
) -> dict:
    """答案细则 JSON：每题含题干/选项/答案/难度/认知层级/质量审计。"""
    pv = get_paper_version(session, paper_version_id, course_id=course_id)
    questions = pv.get("questions", [])
    return {
        "answer_detail_schema_version": "1.0.0",
        "paper_version_id": paper_version_id,
        "version_no": pv.get("version_no"),
        "exam_project_id": pv.get("exam_project_id"),
        "total_questions": len(questions),
        "questions": [
            {
                "item_index": q["item_index"],
                "question_type": q.get("question_type"),
                "question_type_label": _q_type_label(q.get("question_type")),
                "stem": q.get("stem", ""),
                "options": q.get("options", []),
                "answer": q.get("answer", ""),
                "difficulty": q.get("difficulty"),
                "difficulty_label": _difficulty_label(q.get("difficulty")),
                "cognitive_level": q.get("cognitive_level"),
                "knowledge_card_id": q.get("knowledge_card_id"),
                "plan_item_id": q.get("plan_item_id"),
                "needs_review": q.get("needs_review", False),
                "needs_review_reason": q.get("needs_review_reason"),
                "quality_audit": q.get("quality_audit") or {},
            }
            for q in questions
        ],
    }


def _render_question_html(q: dict, *, with_answer: bool) -> str:
    """渲染单题 HTML。with_answer=True 时显示答案。"""
    idx = q.get("item_index", 0)
    stem = q.get("stem", "")
    qt_label = _q_type_label(q.get("question_type"))
    options = q.get("options", [])
    answer = q.get("answer", "")

    parts = [f'<div class="question">', f'<div class="q-stem">{idx}. {stem}</div>']

    if options:
        opts_html = []
        for i, opt in enumerate(options):
            label = chr(65 + i)  # A, B, C, D
            if isinstance(opt, dict):
                text = opt.get("text", str(opt))
            else:
                text = str(opt)
            opts_html.append(f'<div class="q-option">{label}. {text}</div>')
        parts.append(f'<div class="q-options">{"".join(opts_html)}</div>')

    if with_answer:
        diff = _difficulty_label(q.get("difficulty"))
        cog = q.get("cognitive_level", "")
        parts.append(
            f'<div class="q-answer"><span class="ans-label">【答案】</span>{answer}</div>'
            f'<div class="q-meta">难度: {diff}'
            + (f' ｜ 认知层级: {cog}' if cog else '')
            + '</div>'
        )

    parts.append('</div>')
    return "".join(parts)


def export_student_paper_html(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
) -> str:
    """学生卷 HTML（无答案，可打印为 PDF）。"""
    pv = get_paper_version(session, paper_version_id, course_id=course_id)
    questions = pv.get("questions", [])
    version_no = pv.get("version_no", 1)
    project_id = pv.get("exam_project_id", "")
    body = "".join(_render_question_html(q, with_answer=False) for q in questions)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>试卷 - 版本{version_no}</title>
<style>
  body {{ font-family: 'SimSun','宋体',serif; max-width: 780px; margin: 0 auto; padding: 40px 20px; color: #1d1d1f; }}
  .header {{ text-align: center; border-bottom: 2px solid #1d1d1f; padding-bottom: 16px; margin-bottom: 24px; }}
  .header h1 {{ font-size: 22px; margin: 0 0 8px; }}
  .header .meta {{ font-size: 13px; color: #6e6e73; }}
  .question {{ margin-bottom: 20px; page-break-inside: avoid; }}
  .q-stem {{ font-size: 15px; line-height: 1.8; margin-bottom: 8px; }}
  .q-options {{ padding-left: 24px; }}
  .q-option {{ font-size: 14px; line-height: 1.8; }}
  .q-type-tag {{ display: inline-block; font-size: 12px; color: #6e6e73; margin-left: 8px; }}
  .footer {{ margin-top: 40px; text-align: center; font-size: 12px; color: #86868b; border-top: 1px solid #e5e5e7; padding-top: 12px; }}
  @media print {{ body {{ padding: 20px; }} .no-print {{ display: none; }} }}
</style></head>
<body>
  <div class="header">
    <h1>试卷</h1>
    <div class="meta">版本 {version_no} ｜ 共 {len(questions)} 题</div>
  </div>
  {body}
  <div class="footer">试卷版本号: {version_no} | 项目: {project_id[:8] if project_id else 'N/A'}</div>
</body></html>"""


def export_answer_key_html(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
) -> str:
    """答卷 HTML（含答案，可打印为 PDF）。"""
    pv = get_paper_version(session, paper_version_id, course_id=course_id)
    questions = pv.get("questions", [])
    version_no = pv.get("version_no", 1)
    project_id = pv.get("exam_project_id", "")
    body = "".join(_render_question_html(q, with_answer=True) for q in questions)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>答卷（含答案）- 版本{version_no}</title>
<style>
  body {{ font-family: 'SimSun','宋体',serif; max-width: 780px; margin: 0 auto; padding: 40px 20px; color: #1d1d1f; }}
  .header {{ text-align: center; border-bottom: 2px solid #1d1d1f; padding-bottom: 16px; margin-bottom: 24px; }}
  .header h1 {{ font-size: 22px; margin: 0 0 8px; }}
  .header .meta {{ font-size: 13px; color: #6e6e73; }}
  .question {{ margin-bottom: 24px; page-break-inside: avoid; }}
  .q-stem {{ font-size: 15px; line-height: 1.8; margin-bottom: 8px; }}
  .q-options {{ padding-left: 24px; }}
  .q-option {{ font-size: 14px; line-height: 1.8; }}
  .q-answer {{ font-size: 14px; line-height: 1.8; margin-top: 6px; color: #1a7e34; }}
  .ans-label {{ font-weight: 700; }}
  .q-meta {{ font-size: 12px; color: #6e6e73; margin-top: 4px; }}
  .footer {{ margin-top: 40px; text-align: center; font-size: 12px; color: #86868b; border-top: 1px solid #e5e5e7; padding-top: 12px; }}
  @media print {{ body {{ padding: 20px; }} .no-print {{ display: none; }} }}
</style></head>
<body>
  <div class="header">
    <h1>答卷（含答案）</h1>
    <div class="meta">版本 {version_no} ｜ 共 {len(questions)} 题</div>
  </div>
  {body}
  <div class="footer">试卷版本号: {version_no} | 项目: {project_id[:8] if project_id else 'N/A'}</div>
</body></html>"""
