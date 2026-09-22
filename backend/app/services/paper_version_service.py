"""试卷版本服务：从生成结果创建 candidate → 评审覆写 → 确认/回滚。"""
from __future__ import annotations

import html
import re
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
from app.services.generation_service import answer_option_keys


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


def _validate_teacher_item(
    *,
    stem: str,
    question_type: str,
    options: list | None,
    answer: Any,
) -> None:
    """教师手动新增题目的最小约束：题干与答案必填，选择题答案须落在选项上。

    生成链路有 validate_generated_question 把关，但手动新增此前完全不校验，
    导致卷面出现无答案题目——导出答卷/答案细则时就是空白，属于不应出现的数据。
    """
    if not str(stem or "").strip():
        raise PaperVersionError("题干不能为空")
    text = _answer_text(answer)
    if not text:
        raise PaperVersionError("答案不能为空：每道题都必须有答案，否则答卷与答案细则会出现空答案")
    if question_type == "multiple_choice":
        opts = options or []
        if len(opts) < 2:
            raise PaperVersionError("多选题至少需要两个选项")
        picked = answer_option_keys(answer, opts)
        if len(picked) < 2:
            raise PaperVersionError("多选题答案必须对应两个及以上选项（写选项字母如 AB，或选项原文）")


def create_paper_item(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    stem: str,
    question_type: str = "short_answer",
    options: list | None = None,
    answer: Any = "",
    explanation: str = "",
    score: float = 0.0,
    difficulty: str = "medium",
) -> dict:
    """在试卷末尾新增一道教师自拟题目。

    生成一条 plan_item 槽位（接在蓝图既有槽位之后）＋ generated_question ＋
    paper_item，返回刷新后的完整试卷。新增题标记 payload.teacher_added=true。
    """
    try:
        _validate_teacher_item(
            stem=stem,
            question_type=question_type,
            options=options,
            answer=answer,
        )
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

_Q_TYPE_LABELS = {
    "single_choice": "单选题",
    "multiple_choice": "多选题",
    "true_false": "判断题",
    "short_answer": "简答题",
    "comprehensive": "综合题",
    "essay": "论述题",
    "fill_blank": "填空题",
    "calculation": "计算题",
}

# 卷面分节顺序：客观题在前，主观题在后
_SECTION_ORDER = [
    "single_choice",
    "multiple_choice",
    "true_false",
    "fill_blank",
    "short_answer",
    "comprehensive",
    "essay",
    "calculation",
]

_CN_NUMERALS = "一二三四五六七八九十"

# 题干自带的层级编号（"1." / "1.1" / "1.1."）与分值前缀（"（10分）"）。
# 剥离后再加导出题号，否则会叠成用户看到的「1.1.」「2.2.」。
# 单独编号里的点不能紧跟数字（否则 "3.14是圆周率" 会被误剥成 "14是…"）；
# 层级编号后必须紧跟分隔符或空白，避免误伤正文里的版本号一类数字。
_LEADING_INDEX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)+\s*(?:[\.、．:：]|\s)|\d+\s*(?:[、．:：]|\.(?!\d)))\s*")
_LEADING_SCORE_RE = re.compile(r"^\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*")


def _q_type_label(qt: str | None) -> str:
    return _Q_TYPE_LABELS.get(qt or "", qt or "题")


def _difficulty_label(d: Any) -> str:
    # 生成链路用 easy/medium/hard 字符串，部分历史数据用 1-5 整数，两边都要能读
    str_labels = {"easy": "容易", "low": "容易", "medium": "中等", "hard": "困难", "high": "困难"}
    int_labels = {1: "容易", 2: "较易", 3: "中等", 4: "较难", 5: "困难"}
    if isinstance(d, bool):
        return "未知"
    if isinstance(d, (int, float)):
        return int_labels.get(int(d), str(d))
    text = str(d).strip() if d else ""
    if not text:
        return "未知"
    return str_labels.get(text.lower(), text)


def _strip_stem_noise(stem: str) -> str:
    """剥掉题干自带的编号与分值前缀，避免与导出题号重复。"""
    text = stem or ""
    previous = None
    while previous != text:
        previous = text
        text = _LEADING_SCORE_RE.sub("", text)
        text = _LEADING_INDEX_RE.sub("", text)
    return text.strip()


def _answer_text(answer: Any) -> str:
    """答案统一成可读文本。判断题在后端是布尔值，没有选项字段。"""
    if isinstance(answer, bool):
        return "正确" if answer else "错误"
    if answer is None:
        return ""
    if isinstance(answer, (list, tuple)):
        return "、".join(str(a).strip() for a in answer if str(a).strip())
    return str(answer).strip()


def _answer_missing(q: dict) -> bool:
    return not _answer_text(q.get("answer"))


def _cn_ordinal(index: int) -> str:
    return _CN_NUMERALS[index] if 0 <= index < len(_CN_NUMERALS) else str(index + 1)


def _section_groups(questions: list[dict]) -> list[dict]:
    """按题型分节，节内保持卷面顺序；计算节总分与（Uniform 时的）每题分值。"""
    position: dict[str, int] = {}
    groups: list[dict] = []
    for q in questions:
        qt = q.get("question_type") or "short_answer"
        if qt not in position:
            position[qt] = len(groups)
            groups.append({"type": qt, "items": []})
        groups[position[qt]]["items"].append(q)

    def sort_key(group: dict) -> int:
        t = group["type"]
        return _SECTION_ORDER.index(t) if t in _SECTION_ORDER else len(_SECTION_ORDER)

    groups.sort(key=sort_key)
    for i, group in enumerate(groups):
        items = group["items"]
        group["ordinal"] = _cn_ordinal(i)
        group["total"] = sum(float(q.get("score") or 0) for q in items)
        scores = {round(float(q.get("score") or 0), 2) for q in items}
        group["per_score"] = scores.pop() if len(scores) == 1 else None
    return groups


def _section_caption(group: dict) -> str:
    """「一、选择题（共10题，每题2分，共20分）」；分值不统一时省略"每题X分"。"""
    count = len(group["items"])
    per = group["per_score"]
    total = _trim_number(group["total"])
    head = f"{group['ordinal']}、{_q_type_label(group['type'])}（共{count}题，"
    body = f"每题{_trim_number(per)}分，共{total}分）" if per is not None else f"共{total}分）"
    return head + body


def _trim_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _paper_meta(session: Session, *, course_id: str, pv: dict) -> dict:
    """导出头部能拿到的真实元数据：课程名（其余字段留空由教师填写）。"""
    course_name = ""
    try:
        from app.db.schema import Course

        course_name = session.scalar(select(Course.name).where(Course.id == course_id)) or ""
    except SQLAlchemyError:
        course_name = ""
    return {
        "course_name": course_name,
        "project_name": pv.get("project_name") or "",
        "version_no": pv.get("version_no", 1),
        "total_score": pv.get("total_score", 0),
        "status": pv.get("status", ""),
    }


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _exam_shell(
    *,
    title: str,
    meta: dict,
    sections_table: str,
    body: str,
    binding_lines: bool,
    footer_note: str,
) -> str:
    """三份导出共用的正式卷面外壳：信息头 + 题次表 +（可选）装订线 + 页脚。"""
    info_line = (
        f"课程名称：{_esc(meta['course_name']) or '＿＿＿＿＿＿'}"
        f"&emsp;&emsp;总分：{_trim_number(meta['total_score'])}分"
        f"&emsp;&emsp;题量：{meta['question_count']}题"
    )
    blank_line = (
        "考试时间：＿＿＿＿分钟&emsp;&emsp;考试形式：＿＿＿＿"
        "&emsp;&emsp;试卷类型：＿＿＿＿&emsp;&emsp;学分：＿＿＿＿"
    )
    binding = (
        '<div class="binding binding-left">装订线</div>'
        '<div class="binding binding-center">装订线</div>'
        '<div class="binding binding-right">装订线</div>'
        if binding_lines
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'SimSun','宋体',serif; max-width: 780px; margin: 0 auto; padding: 36px 28px 60px; color: #1d1d1f; background: #fff; }}
  .doc-title {{ text-align: center; font-size: 22px; font-weight: 700; letter-spacing: 2px; margin: 0 0 14px; }}
  .doc-subtitle {{ text-align: center; font-size: 12px; color: #6e6e73; margin: -8px 0 14px; }}
  .info-table {{ width: 100%; border-collapse: collapse; margin-bottom: 14px; font-size: 13px; }}
  .info-table td {{ padding: 5px 6px; }}
  .score-table {{ width: 100%; border-collapse: collapse; margin: 0 0 22px; font-size: 13px; text-align: center; }}
  .score-table th, .score-table td {{ border: 1px solid #1d1d1f; padding: 6px 4px; }}
  .score-table th {{ background: #f5f5f7; font-weight: 600; }}
  .section {{ margin-bottom: 26px; page-break-inside: avoid; }}
  .section-title {{ font-size: 15px; font-weight: 700; margin: 0 0 12px; }}
  .question {{ margin-bottom: 16px; page-break-inside: avoid; }}
  .q-stem {{ font-size: 14.5px; line-height: 1.85; }}
  .q-stem .q-no {{ font-weight: 700; }}
  .q-options {{ padding-left: 26px; margin-top: 4px; }}
  .q-option {{ font-size: 14px; line-height: 1.9; }}
  .q-option.is-answer {{ color: #1a7e34; font-weight: 700; }}
  .q-answer-line {{ font-size: 14px; line-height: 1.85; margin-top: 6px; color: #1a7e34; }}
  .q-answer-line .ans-label {{ font-weight: 700; }}
  .q-answer-missing {{ color: #c2331d; font-weight: 700; }}
  .q-explain {{ font-size: 12.5px; line-height: 1.8; color: #6e6e73; margin-top: 4px; }}
  .q-meta {{ font-size: 12px; color: #8e8e93; margin-top: 3px; }}
  .answer-grid {{ border-collapse: collapse; margin: 4px 0 18px; font-size: 13px; text-align: center; }}
  .answer-grid th, .answer-grid td {{ border: 1px solid #b9b9bd; padding: 5px 9px; }}
  .answer-grid th {{ background: #f5f5f7; font-weight: 600; }}
  .blank-line {{ border-bottom: 1px solid #8e8e93; height: 30px; margin: 10px 0; }}
  .write-area {{ margin: 8px 0 4px; }}
  .write-area .blank-line {{ height: 26px; }}
  .score-box {{ font-size: 13px; color: #6e6e73; margin-bottom: 2px; }}
  .sign-row {{ display: flex; gap: 28px; margin-top: 30px; font-size: 13px; }}
  .binding {{ position: fixed; top: 0; bottom: 0; writing-mode: vertical-rl; text-align: center;
              font-size: 12px; color: #8e8e93; letter-spacing: 6px; }}
  .binding-left {{ left: 26px; border-left: 1px dashed #b9b9bd; }}
  .binding-center {{ left: 50%; border-left: 1px dashed #b9b9bd; }}
  .binding-right {{ right: 26px; border-right: 1px dashed #b9b9bd; }}
  .footer {{ margin-top: 34px; padding-top: 12px; border-top: 1px solid #e5e5e7;
             text-align: center; font-size: 12px; color: #86868b; }}
  @media print {{ body {{ padding: 18px 34px; }} .binding {{ display: block; }} }}
</style></head>
<body>
  {binding}
  <h1 class="doc-title">{_esc(title)}</h1>
  <div class="doc-subtitle">{_esc(meta['project_name'])}</div>
  <table class="info-table"><tr><td>{info_line}</td></tr><tr><td>{blank_line}</td></tr></table>
  {sections_table}
  {body}
  <div class="sign-row"><span>学号：＿＿＿＿＿＿＿＿＿＿</span><span>姓名：＿＿＿＿＿＿＿＿＿＿</span></div>
  <div class="footer">{_esc(footer_note)}</div>
</body></html>"""


def _sections_table_html(groups: list[dict], with_reviewer: bool = True) -> str:
    """题次表：题次 | 一 | 二 | … | 总分 | 评卷人。"""
    head_cells = "".join(f"<th>{_esc(g['ordinal'])}</th>" for g in groups)
    score_cells = "".join(f"<td>{_trim_number(g['total'])}</td>" for g in groups)
    total = sum(g["total"] for g in groups)
    reviewer = "<th>评卷人</th><td></td>" if with_reviewer else ""
    return (
        '<table class="score-table">'
        f"<tr><th>题次</th>{head_cells}<th>总分</th>{'<th>评卷人</th>' if with_reviewer else ''}</tr>"
        f"<tr><th>分数</th>{score_cells}<td>{_trim_number(total)}</td>{'<td></td>' if with_reviewer else ''}</tr>"
        f"<tr><th>评分</th>{'<td></td>' * (len(groups) + 1)}{'<td></td>' if with_reviewer else ''}</tr>"
        "</table>"
    )


def _answer_grid_html(questions: list[dict]) -> str:
    """客观题答案速查表（题号横向排列，与命题范本的评分表一致）。"""
    cells = "".join(f"<th>{q['item_index']}</th>" for q in questions)
    answers = "".join(f"<td>{_esc(_answer_display(q) or '—')}</td>" for q in questions)
    return f'<table class="answer-grid"><tr><th>题号</th>{cells}</tr><tr><th>答案</th>{answers}</tr></table>'


def _answer_keys(q: dict) -> set[str]:
    """答案对应的选项字母。兼容 'B' / 'ABD' 与选项原文两种形态（模型两者都会给）。"""
    return answer_option_keys(q.get("answer"), q.get("options") or [])


def _answer_display(q: dict) -> str:
    """给答案速查表用的紧凑答案：能解析成字母就显示字母，否则显示原文。"""
    keys = _answer_keys(q)
    if keys:
        return "".join(sorted(keys))
    return _answer_text(q.get("answer"))


def _render_question_html(q: dict, *, with_answer: bool) -> str:
    """渲染单题。with_answer=True 时给出答案（答卷）。"""
    stem = _strip_stem_noise(str(q.get("stem", "")))
    options = q.get("options") or []
    answer_text = _answer_text(q.get("answer"))
    keys = _answer_keys(q)

    parts = [f'<div class="q-stem"><span class="q-no">{q.get("item_index", 0)}.</span> {_esc(stem)}</div>']

    if options:
        option_html = []
        for i, opt in enumerate(options):
            label = chr(65 + i)
            text = opt.get("text", str(opt)) if isinstance(opt, dict) else str(opt)
            is_answer = with_answer and label in keys
            css = ' class="q-option is-answer"' if is_answer else ' class="q-option"'
            mark = " ✓" if is_answer else ""
            option_html.append(f"<div{css}>{label}. {_esc(text)}{mark}</div>")
        parts.append(f'<div class="q-options">{"".join(option_html)}</div>')

    if with_answer:
        if answer_text:
            parts.append(
                f'<div class="q-answer-line"><span class="ans-label">【答案】</span>{_esc(answer_text)}</div>'
            )
        else:
            parts.append(
                '<div class="q-answer-line q-answer-missing">【缺答案·需人工补充】</div>'
            )
        if q.get("explanation"):
            parts.append(f'<div class="q-explain">解析：{_esc(q["explanation"])}</div>')

    parts.append(
        f'<div class="q-meta">难度：{_esc(_difficulty_label(q.get("difficulty")))}'
        f' ｜ 分值：{_trim_number(q.get("score") or 0)}分</div>'
    )
    return f'<div class="question">{"".join(parts)}</div>'


def _render_sections(groups: list[dict], *, with_answer: bool) -> str:
    blocks = []
    for group in groups:
        questions = []
        for q in group["items"]:
            questions.append(_render_question_html(q, with_answer=with_answer))
        grid = ""
        if with_answer and group["type"] in {"single_choice", "multiple_choice", "true_false"}:
            grid = _answer_grid_html(group["items"])
        blocks.append(
            f'<div class="section"><h2 class="section-title">{_esc(_section_caption(group))}</h2>'
            f"{grid}{''.join(questions)}</div>"
        )
    return "".join(blocks)


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
        "missing_answer_count": sum(1 for q in questions if _answer_missing(q)),
        "total_questions": len(questions),
        "questions": [
            {
                "item_index": q["item_index"],
                "question_type": q.get("question_type"),
                "question_type_label": _q_type_label(q.get("question_type")),
                "stem": _strip_stem_noise(str(q.get("stem", ""))),
                "options": q.get("options", []),
                "answer": q.get("answer", ""),
                "answer_missing": _answer_missing(q),
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


def export_student_paper_html(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
) -> str:
    """学生卷：正式卷面（信息头 + 题次表 + 分节题面），不含答案。"""
    pv = get_paper_version(session, paper_version_id, course_id=course_id)
    groups = _section_groups(pv.get("questions", []))
    meta = _paper_meta(session, course_id=course_id, pv=pv)
    meta["question_count"] = len(pv.get("questions", []))
    body = _render_sections(groups, with_answer=False)
    return _exam_shell(
        title="考试卷",
        meta=meta,
        sections_table=_sections_table_html(groups),
        body=body,
        binding_lines=False,
        footer_note=f"学生卷 ｜ 试卷版本 v{meta['version_no']} ｜ 请将答案作答在答卷上",
    )


def export_answer_key_html(
    session: Session,
    paper_version_id: str,
    *,
    course_id: str,
) -> str:
    """答卷：正式卷面 + 答案（客观题附答案速查表），缺答案显式标注。"""
    pv = get_paper_version(session, paper_version_id, course_id=course_id)
    questions = pv.get("questions", [])
    groups = _section_groups(questions)
    meta = _paper_meta(session, course_id=course_id, pv=pv)
    meta["question_count"] = len(questions)
    missing = sum(1 for q in questions if _answer_missing(q))
    hint = (
        f"答卷（含答案）｜ 试卷版本 v{meta['version_no']} ｜ 共 {len(questions)} 题"
        + (f" ｜ 注意：{missing} 题缺答案，已标注待人工补充" if missing else "")
    )
    return _exam_shell(
        title="答卷（含答案）",
        meta=meta,
        sections_table=_sections_table_html(groups),
        body=_render_sections(groups, with_answer=True),
        binding_lines=True,
        footer_note=hint,
    )
