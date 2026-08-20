"""试卷版本服务：从生成结果创建 candidate → 评审覆写 → 确认/回滚。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
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
        )
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
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
        questions_out.append({
            "item_index": d["display_order"],
            "plan_item_id": d.get("plan_item_id"),
            "knowledge_card_id": d.get("knowledge_card_id"),
            "stem": stem,
            "options": options,
            "answer": answer,
            "question_type": payload.get("question_type", override.get("question_type")),
            "difficulty": payload.get("difficulty"),
            "cognitive_level": payload.get("cognitive_level"),
            "needs_review": d.get("needs_review", False),
            "needs_review_reason": d.get("needs_review_reason"),
            "teacher_override": override,
            "has_override": bool(override),
            "finalized_text": d.get("finalized_text"),
            "quality_audit": d.get("quality_audit") or {},
        })
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
