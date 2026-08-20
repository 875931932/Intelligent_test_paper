"""生成运行器：入队幂等任务、执行生成任务（mock 或真实图）。

与 generation_graph 解耦：execute_generation_task 接收可注入的 graph_invoke，
测试可传入返回 37 道题的 fixture；生产调用包装 build_generation_graph。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.schema import (
    exam_projects,
    generated_questions,
    generation_runs,
    knowledge_cards,
    paper_versions,
    plan_items,
    quality_checks,
    task_runs,
)
from app.infrastructure.tasks.models import create_task_run


class GenerationRunnerError(Exception):
    """生成任务持久化 / 执行异常。"""


def _nid() -> str:
    return uuid.uuid4().hex[:16]


def _now() -> datetime:
    return datetime.now(UTC)


def _row_to_dict(row) -> dict[str, Any]:
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row._asdict()) if hasattr(row, "_asdict") else dict(row)


def enqueue_generation(
    session: Session,
    *,
    course_id: str,
    project_id: str,
) -> str:
    """为项目的 active_generation_run 入队任务；同 project:run 组合幂等。"""
    proj = session.execute(
        select(exam_projects).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).one_or_none()
    if proj is None:
        raise GenerationRunnerError(f"项目不存在: {project_id}")
    proj_data = proj._mapping
    active_run = proj_data.get("active_generation_run_id")
    if not active_run:
        raise GenerationRunnerError("项目尚未分配 active_generation_run_id")

    key_raw = f"{project_id}:{active_run}:gen".encode()
    idempotency_key = hashlib.sha256(key_raw).hexdigest()[:24]

    try:
        return create_task_run(
            session,
            course_id=course_id,
            task_type="generation_run",
            idempotency_key=idempotency_key,
            input_version="v1",
            payload={
                "project_id": project_id,
                "generation_run_id": active_run,
            },
        )
    except IntegrityError:
        existing = session.execute(
            select(task_runs.c.id).where(
                task_runs.c.course_id == course_id,
                task_runs.c.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        return existing


# ---------------------------------------------------------------------------
# graph_invoke 包装：生产默认用真实图；测试注入 fixture。
# ---------------------------------------------------------------------------

def _default_graph_invoke(
    session: Session,
    generation_run: dict,
    contract_snapshot: dict,
) -> list[dict]:
    """运行正式 LangGraph，并把图输出映射回持久化题位。

    图只接收合同槽位和纯净知识卡；文件名、证据位置与其他资料来源均不进入
    模型请求。测试仍可通过 ``graph_invoke`` 显式替换此函数。
    """
    from app.adapters.model.deepseek_gateway import DeepSeekGateway
    from app.config import settings
    from app.db.session import get_session_factory
    from app.domain.generation.contract import ContractSlot
    from app.domain.model_calls import ModelCallContext
    from app.services.model_call_service import DatabaseModelCallRecorder
    from app.workflows.generation_graph import build_generation_graph

    if not all(value.strip() for value in (
        settings.deepseek_api_key,
        settings.deepseek_base_url,
        settings.deepseek_model,
    )):
        raise GenerationRunnerError("LLM model is not configured")

    raw_slots = contract_snapshot.get("slots") if isinstance(contract_snapshot, dict) else None
    if not isinstance(raw_slots, list) or not raw_slots:
        raise GenerationRunnerError("generation contract has no slots")
    try:
        slots = [ContractSlot.model_validate(raw) for raw in raw_slots]
    except Exception as exc:
        raise GenerationRunnerError("generation contract is invalid") from exc

    course_id = str(generation_run["course_id"])
    catalog_version_id = str(generation_run["catalog_version_id"])
    context_cards, units = _load_generation_context(
        session,
        course_id=course_id,
        catalog_version_id=catalog_version_id,
        slots=slots,
    )
    plan_item_ids = _plan_item_ids_by_index(
        session,
        course_id=course_id,
        blueprint_version_id=str(generation_run["blueprint_version_id"]),
    )
    expected_indexes = {slot.item_index for slot in slots}
    if not expected_indexes.issubset(plan_item_ids):
        raise GenerationRunnerError("generation contract does not match blueprint plan items")

    gateway = DeepSeekGateway(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
        call_context=ModelCallContext(course_id=course_id, stage="paper_generation"),
    )
    result = build_generation_graph(gateway).invoke({
        "contract": [slot.model_dump(mode="json") for slot in slots],
        "knowledge_cards": context_cards,
        "units": units,
    })
    raw_questions = result.get("questions") if isinstance(result, dict) else None
    if not isinstance(raw_questions, list):
        raise GenerationRunnerError("generation graph returned no questions")

    questions: list[dict] = []
    received_indexes: set[int] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise GenerationRunnerError("generation graph returned an invalid question")
        question = dict(raw)
        item_index = question.get("item_index")
        if isinstance(item_index, bool) or not isinstance(item_index, int):
            raise GenerationRunnerError("generation graph returned a question without item_index")
        if item_index in received_indexes or item_index not in plan_item_ids:
            raise GenerationRunnerError("generation graph returned unexpected question indexes")
        received_indexes.add(item_index)
        card_id = question.get("card_id")
        if not isinstance(card_id, str) or card_id not in context_cards:
            raise GenerationRunnerError("generation graph returned a question without a valid knowledge card")
        quality = dict(question.get("quality") or {})
        quality_status = str(quality.get("status") or "blocker")
        needs_review = bool(question.get("needs_review")) or quality_status != "pass"
        quality["needs_review"] = needs_review
        quality.setdefault("message", "generation contract check")
        quality.setdefault("quality_checks", [{
            "check_type": "generation_contract",
            "status": "pass" if quality_status == "pass" else "warn",
            "details": {"message": str(quality["message"])[:200]},
        }])
        question["plan_item_id"] = plan_item_ids[item_index]
        question["knowledge_card_id"] = card_id
        question["needs_review"] = needs_review
        question["quality"] = quality
        questions.append(question)

    if received_indexes != expected_indexes:
        raise GenerationRunnerError("generation graph did not return every contract slot")
    return sorted(questions, key=lambda question: question["item_index"])


def _load_generation_context(
    session: Session,
    *,
    course_id: str,
    catalog_version_id: str,
    slots,
) -> tuple[dict[str, dict], list[dict]]:
    """从已发布目录装配图所需的纯净卡片与同考点候选卡片。"""
    unit_to_exam_point: dict[str, str] = {}
    for slot in slots:
        existing = unit_to_exam_point.setdefault(slot.unit_id, slot.exam_point_id)
        if existing != slot.exam_point_id:
            raise GenerationRunnerError("generation contract maps one unit to multiple exam points")
    unit_ids = list(unit_to_exam_point)
    rows = session.execute(
        select(
            knowledge_cards.c.id,
            knowledge_cards.c.assessment_unit_id,
            knowledge_cards.c.name,
            knowledge_cards.c.performance_statement,
            knowledge_cards.c.assessable_content,
            knowledge_cards.c.scope_boundary,
            knowledge_cards.c.cognitive_targets,
            knowledge_cards.c.allowed_question_types,
            knowledge_cards.c.importance,
            knowledge_cards.c.concept_cluster,
            knowledge_cards.c.answer_proposition,
            knowledge_cards.c.prompt_material,
            knowledge_cards.c.relation_edges,
        ).where(
            knowledge_cards.c.course_id == course_id,
            knowledge_cards.c.catalog_version_id == catalog_version_id,
            knowledge_cards.c.assessment_unit_id.in_(unit_ids),
            knowledge_cards.c.status == "active",
        )
    ).all()
    cards: dict[str, dict] = {}
    card_ids_by_unit: dict[str, list[str]] = {unit_id: [] for unit_id in unit_ids}
    for row in rows:
        card = row._mapping
        card_id = card["id"]
        cards[card_id] = {
            "name": card["name"] or "",
            "performance_statement": card["performance_statement"] or "",
            "assessable_content": list(card["assessable_content"] or []),
            "scope_boundary": card["scope_boundary"] or {},
            "cognitive_targets": list(card["cognitive_targets"] or []),
            "allowed_question_types": list(card["allowed_question_types"] or []),
            "importance": card["importance"] or 1,
            "concept_cluster": card["concept_cluster"] or "",
            "answer_proposition": card["answer_proposition"] or "",
            "answer_boundary": card["answer_proposition"] or "",
            "prompt_material": list(card["prompt_material"] or []),
            "preferred_terms": [],
            "relation_edges": list(card["relation_edges"] or []),
        }
        card_ids_by_unit[card["assessment_unit_id"]].append(card_id)
    missing_card_ids = {slot.card_id for slot in slots}.difference(cards)
    if missing_card_ids:
        raise GenerationRunnerError("generation context is missing contracted knowledge cards")
    units = [
        {
            "unit_id": unit_id,
            "exam_point_id": unit_to_exam_point[unit_id],
            "card_ids": card_ids_by_unit[unit_id],
        }
        for unit_id in unit_ids
    ]
    return cards, units


def _plan_item_ids_by_index(
    session: Session,
    *,
    course_id: str,
    blueprint_version_id: str,
) -> dict[int, str]:
    rows = session.execute(
        select(plan_items.c.item_index, plan_items.c.id).where(
            plan_items.c.course_id == course_id,
            plan_items.c.blueprint_version_id == blueprint_version_id,
        )
    ).all()
    return {row._mapping["item_index"]: row._mapping["id"] for row in rows}


def execute_generation_task(
    task_run: dict,
    *,
    graph_invoke: Callable[[Session, dict, dict], list[dict]] | None = None,
    write_paper_version: bool = True,
    manage_task_run: bool = True,
) -> dict:
    """执行单个生成任务：更新状态 → 调图 → 写题 + 质检 → 写试卷 → 标记成功。

    Parameters
    ----------
    task_run:
        task_runs 行的 dict 表示（含 id, course_id, payload 等）。由调用方
        通过 session 加载后传入，避免该函数自行再开 session。
    graph_invoke:
        ``graph_invoke(session, generation_run_dict, contract_snapshot) -> list[question_dict]``
        题目 dict 含 plan_item_id, knowledge_card_id, stem, options, answer,
        difficulty, cognitive_level, quality 等字段。
    write_paper_version:
        True 时调用 Task 5 ``create_paper_version_from_generation`` 创建候选
        试卷版本；失败用例单独测试时可设为 False。
    """
    from app.services.paper_version_service import (
        create_paper_version_from_generation,
    )

    if graph_invoke is None:
        graph_invoke = _default_graph_invoke

    course_id = task_run["course_id"]
    payload = task_run.get("payload") or {}
    project_id = payload["project_id"]
    generation_run_id = payload["generation_run_id"]
    task_run_id = task_run["id"]

    # 构造当前 session：从 task_run 取 bind。这里允许调用方注入一个闭包来
    # 获取 session；但为了简单，我们假设传入的 task_run 是 mapping，并且
    # execute_generation_task_handler（见 inline_runner）会把 session 作为
    # kwarg 传进来。为保持签名一致，这里用一个模块级私有 thread-local 不合适。
    # 所以我们引入 session_ kwarg，兼容调用端。
    # 由于无法简单地在该函数签名上增加 session，改用一个小技巧：让 task_run
    # 对象附带 ``_session`` 属性；否则要求 graph_invoke 闭包捕获 session。
    session: Session | None = task_run.get("_session") if isinstance(task_run, dict) else None
    if session is None:  # pragma: no cover - 由 handler 保证
        raise GenerationRunnerError(
            "execute_generation_task 需要 task_run['_session'] 绑定当前会话"
        )

    now = _now()
    paper_version_id: str | None = None
    try:
        # a) 任务标记 running
        if manage_task_run:
            session.execute(
                task_runs.update()
                .where(
                    task_runs.c.id == task_run_id,
                    task_runs.c.course_id == course_id,
                )
                .values(
                    status="running",
                    stage="executing",
                    updated_at=now,
                    progress=5,
                )
            )
        # b) generation_run 标记 running
        session.execute(
            generation_runs.update()
            .where(
                generation_runs.c.id == generation_run_id,
                generation_runs.c.course_id == course_id,
            )
            .values(status="running", updated_at=now)
        )
        session.flush()

        # 读取 generation_run 与 contract_snapshot
        gr = session.execute(
            select(generation_runs).where(
                generation_runs.c.id == generation_run_id,
                generation_runs.c.course_id == course_id,
            )
        ).one()
        gr_dict = _row_to_dict(gr)
        contract_snapshot = gr_dict.get("contract_snapshot") or {}

        # c) 调用图生成题目
        questions: list[dict] = graph_invoke(session, gr_dict, contract_snapshot)

        # d) 批量插入 generated_questions
        gq_rows = []
        for q in questions:
            gq_rows.append({
                "id": _nid(),
                "course_id": course_id,
                "generation_run_id": generation_run_id,
                "plan_item_id": q.get("plan_item_id"),
                "knowledge_card_id": q.get("knowledge_card_id"),
                "revision_no": 1,
                "status": "candidate",
                "payload": {k: v for k, v in q.items() if k != "quality"},
            })
        if gq_rows:
            session.execute(generated_questions.insert(), gq_rows)
        # 生成 id → gq_id 对应，用 position 映射
        # 重新查询以获得 gq_id 顺序
        inserted_gq = session.execute(
            select(generated_questions.c.id, generated_questions.c.plan_item_id)
            .where(
                generated_questions.c.generation_run_id == generation_run_id,
                generated_questions.c.course_id == course_id,
                generated_questions.c.revision_no == 1,
            )
        ).all()
        # 按 plan_item_id 建索引；若多题同 plan_item 按顺序
        gq_by_plan_item: dict[str, list[str]] = {}
        for r in inserted_gq:
            gq_by_plan_item.setdefault(r._mapping["plan_item_id"], []).append(r._mapping["id"])
        # 与输入 questions 对齐顺序：用 questions 的 plan_item_id + index
        gq_ids_ordered: list[str] = []
        for q in questions:
            pi = q.get("plan_item_id")
            ids = gq_by_plan_item.get(pi, [])
            if ids:
                gq_ids_ordered.append(ids.pop(0))
            else:
                gq_ids_ordered.append("")

        # e) 批量插入 quality_checks
        qc_rows = []
        for q, gq_id in zip(questions, gq_ids_ordered, strict=True):
            if not gq_id:
                continue
            quality = q.get("quality") or {}
            checks = quality.get("quality_checks") or []
            for check in checks:
                qc_rows.append({
                    "id": _nid(),
                    "course_id": course_id,
                    "generated_question_id": gq_id,
                    "check_type": check.get("check_type", "unknown"),
                    "status": check.get("status", "unknown"),
                    "details": {
                        k: v for k, v in check.items()
                        if k not in ("check_type", "status")
                    },
                })
        if qc_rows:
            session.execute(quality_checks.insert(), qc_rows)

        # f) 写入 paper_version（候选）+ paper_items
        if write_paper_version:
            paper_version_id = create_paper_version_from_generation(
                session,
                course_id=course_id,
                project_id=project_id,
                generation_run_id=generation_run_id,
                questions_list=questions,
            )

        # g) 标记 generation_run + task_runs 成功
        now2 = _now()
        session.execute(
            generation_runs.update()
            .where(
                generation_runs.c.id == generation_run_id,
                generation_runs.c.course_id == course_id,
            )
            .values(
                status="succeeded",
                completed_at=now2,
                updated_at=now2,
            )
        )
        result = {
            "generated_questions": len(questions),
            "paper_version_id": paper_version_id,
        }
        if manage_task_run:
            session.execute(
                task_runs.update()
                .where(
                    task_runs.c.id == task_run_id,
                    task_runs.c.course_id == course_id,
                )
                .values(
                    status="succeeded",
                    progress=100,
                    completed_at=now2,
                    updated_at=now2,
                    result=result,
                )
            )
        session.commit()
        return result

    except Exception as exc:
        # 异常处理：回滚，标记失败
        session.rollback()
        err_msg = str(exc)[:2000]
        now3 = _now()
        try:
            session.execute(
                generation_runs.update()
                .where(
                    generation_runs.c.id == generation_run_id,
                    generation_runs.c.course_id == course_id,
                )
                .values(
                    status="failed",
                    error_message=err_msg,
                    updated_at=now3,
                )
            )
            if manage_task_run:
                session.execute(
                    task_runs.update()
                    .where(
                        task_runs.c.id == task_run_id,
                        task_runs.c.course_id == course_id,
                    )
                    .values(
                        status="failed",
                        error_message=err_msg,
                        updated_at=now3,
                    )
                )
            # 如果 paper_version 被部分创建，回滚事务已经消除，无需单独清理
            session.commit()
        except SQLAlchemyError:
            session.rollback()
        raise


def execute_generation_task_handler(
    session: Session,
    task_run_row,
    *,
    graph_invoke=None,
    write_paper_version: bool = True,
    manage_task_run: bool = True,
) -> dict:
    """供 inline_runner / worker 调用的 handler：绑定 session 再调用核心函数。"""
    if hasattr(task_run_row, "_mapping"):
        tr = dict(task_run_row._mapping)
    elif isinstance(task_run_row, dict):
        tr = dict(task_run_row)
    else:
        tr = dict(task_run_row._asdict()) if hasattr(task_run_row, "_asdict") else dict(task_run_row)
    tr["_session"] = session
    return execute_generation_task(
        tr,
        graph_invoke=graph_invoke,
        write_paper_version=write_paper_version,
        manage_task_run=manage_task_run,
    )
