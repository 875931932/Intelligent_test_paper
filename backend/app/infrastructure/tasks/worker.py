"""Generic Celery worker bridge for durable task runs.

Business workflows register handlers by ``task_type`` in later tasks.  This
module owns only lease acquisition, handler dispatch, and durable completion;
Celery remains a transport and never becomes the source of task state.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import task_runs
from app.db.session import get_session_factory
from app.infrastructure.tasks.models import (
    # 各类型租约时长的唯一权威表（与 lease 收尾逻辑同居 models）；
    # 保留原私有名，各 service 的「与 worker.py 保持一致」注释继续成立。
    LEASE_SECONDS_BY_TYPE as _LEASE_SECONDS_BY_TYPE,
    claim_task,
    complete_task,
    fail_task,
    refresh_lease,
    update_task_progress,
    wait_for_external,
)


@dataclass(frozen=True)
class TaskContext:
    session: Session
    course_id: str
    task_id: str
    worker_id: str
    payload: dict

    def heartbeat(self, *, lease_seconds: int = 60) -> bool:
        return refresh_lease(
            self.session,
            course_id=self.course_id,
            task_id=self.task_id,
            worker_id=self.worker_id,
            lease_seconds=lease_seconds,
        )

    def report_progress(self, *, stage: str, progress: int) -> bool:
        return update_task_progress(
            self.session,
            course_id=self.course_id,
            task_id=self.task_id,
            worker_id=self.worker_id,
            stage=stage,
            progress=progress,
        )

    def pause_until(self, next_poll_at: datetime) -> bool:
        return wait_for_external(
            self.session,
            course_id=self.course_id,
            task_id=self.task_id,
            worker_id=self.worker_id,
            next_poll_at=next_poll_at,
        )


TaskHandler = Callable[[TaskContext], dict]
_HANDLERS: dict[str, TaskHandler] = {}

# 各任务类型租约时长见 models.LEASE_SECONDS_BY_TYPE（claim 时按表取值，
# 长任务跑批期间由进度写入心跳续租，收尾只看 owner 不看过期）。


def register_task_handler(task_type: str, handler: TaskHandler) -> None:
    if not task_type.strip():
        raise ValueError("task_type is required")
    _HANDLERS[task_type] = handler


def _persisted_run_error(payload: dict) -> str | None:
    """读取业务处理器已持久化的真实错误（如 generation_runs.error_message）。"""

    run_id = (payload or {}).get("generation_run_id")
    if not run_id:
        return None
    try:
        from app.db.schema import generation_runs

        probe = get_session_factory()()
        try:
            message = probe.execute(
                select(generation_runs.c.error_message).where(generation_runs.c.id == run_id)
            ).scalar_one_or_none()
        finally:
            probe.close()
    except Exception:
        return None
    text = (message or "").strip()
    return text or None


def _handler_error_message(payload: dict, exc: BaseException) -> str:
    """组装失败原因：保留原前缀，同时带出真实异常，避免只暴露 "task handler failed"。"""

    detail = _persisted_run_error(payload) or str(exc).strip() or exc.__class__.__name__
    return f"task handler failed: {detail}"[:2000]


def execute_task(task_id: str, *, worker_id: str | None = None) -> bool:
    """Claim and execute one durable task, returning whether it was handled."""

    worker_id = worker_id or f"{os.getpid()}:{uuid4().hex}"
    session = get_session_factory()()
    try:
        row = session.execute(select(task_runs).where(task_runs.c.id == task_id)).mappings().one_or_none()
        if row is None:
            return False
        course_id = row["course_id"]
        lease_seconds = _LEASE_SECONDS_BY_TYPE.get(row["task_type"], 60)
        if not claim_task(
            session,
            course_id=course_id,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        ):
            session.rollback()
            return False
        # 将租约作为独立事务提交。业务处理器可能因模型/数据库错误回滚，
        # 不能因此把已领取的任务恢复成 queued，导致重复执行或无法失败收敛。
        session.commit()
        handler = _HANDLERS.get(row["task_type"])
        if handler is None:
            fail_task(
                session,
                course_id=course_id,
                task_id=task_id,
                worker_id=worker_id,
                error_code="handler_not_registered",
                error_message="no worker handler is registered for this task type",
            )
            session.commit()
            return False
        try:
            context = TaskContext(
                session=session,
                course_id=course_id,
                task_id=task_id,
                worker_id=worker_id,
                payload=dict(row["payload"] or {}),
            )
            result = handler(context)
        except Exception as exc:
            fail_task(
                session,
                course_id=course_id,
                task_id=task_id,
                worker_id=worker_id,
                error_code="handler_error",
                error_message=_handler_error_message(row["payload"], exc),
            )
            session.commit()
            return False
        completed = complete_task(
            session,
            course_id=course_id,
            task_id=task_id,
            worker_id=worker_id,
            result=result,
        )
        if completed:
            session.commit()
            return True
        status = session.execute(select(task_runs.c.status).where(task_runs.c.id == task_id)).scalar_one()
        session.commit()
        return status == "waiting_external"
    finally:
        session.close()


def _handle_generation_run(context: TaskContext) -> dict:
    """Execute the durable paper-generation task with the worker's lease."""
    from app.services.generation_runner_service import execute_generation_task_handler

    context.report_progress(stage="generating", progress=5)
    row = context.session.execute(
        select(task_runs).where(
            task_runs.c.id == context.task_id,
            task_runs.c.course_id == context.course_id,
        )
    ).one()
    return execute_generation_task_handler(
        context.session,
        row,
        manage_task_run=False,
    )


register_task_handler("generation_run", _handle_generation_run)


def _handle_ai_revise_item(context: TaskContext) -> dict:
    """执行单题 AI 改题提案：只调模型产出提案与校验结果，不写试卷数据。

    落库由教师在前端确认后走既有 PATCH teacher_override 端点完成。
    """
    from app.services.ai_revise_service import execute_ai_revise_task

    context.report_progress(stage="revising", progress=10)
    return execute_ai_revise_task(context.session, payload=dict(context.payload))


register_task_handler("ai_revise_item", _handle_ai_revise_item)


def _handle_ai_create_item(context: TaskContext) -> dict:
    """执行整题 AI 生成提案：只调模型产出提案与校验结果，不写试卷数据。

    落库由教师在前端确认后走既有 POST .../items（create_paper_item）端点完成。
    """
    from app.services.ai_create_service import execute_ai_create_task

    context.report_progress(stage="creating", progress=10)
    return execute_ai_create_task(context.session, payload=dict(context.payload))


register_task_handler("ai_create_item", _handle_ai_create_item)


def _handle_review_paper_version(context: TaskContext) -> dict:
    """执行整卷 AI 质量评审：只读报告，不写任何业务表。

    报告只针对试卷稿本身（禁学生答卷评分）；发现问题由教师在前端走既有
    编辑/AI 改题/AI 生成/重新生成端点处理。
    """
    from app.services.paper_review_service import execute_review_task

    context.report_progress(stage="reviewing", progress=10)
    return execute_review_task(context.session, payload=dict(context.payload))


register_task_handler("review_paper_version", _handle_review_paper_version)


def _handle_propose_exam_rules(context: TaskContext) -> dict:
    """执行考核规则提案：一句话 → 结构化提案，只写 task_runs 不碰框架数据。

    提案由前端回填考核规则卡的编辑草稿，教师点「保存」才走既有 PATCH rules
    端点落库——AI 只产提案不绕教师确认流。
    """
    from app.services.exam_rules_ai_service import execute_propose_task

    context.report_progress(stage="proposing", progress=10)
    return execute_propose_task(context.session, payload=dict(context.payload))


register_task_handler("propose_exam_rules", _handle_propose_exam_rules)


def _handle_suggest_blueprint_adjustments(context: TaskContext) -> dict:
    """执行蓝图题位调整建议：逐题提案，只写 task_runs 不碰蓝图数据。

    建议由前端展示，教师逐条确认后走既有 PATCH plan-items 落库——AI 只产
    提案不直接改题位。
    """
    from app.services.blueprint_suggest_service import execute_suggest_task

    context.report_progress(stage="suggesting", progress=10)
    return execute_suggest_task(context.session, payload=dict(context.payload))


register_task_handler("suggest_blueprint_adjustments", _handle_suggest_blueprint_adjustments)


def _handle_review_framework_candidate(context: TaskContext) -> dict:
    """执行框架候选 AI 评审：只读报告，只写 task_runs 不碰框架/冲突数据。

    报告仅供教师参考，确认/拒绝仍走既有确认流；冲突裁决不被 AI 代劳。
    """
    from app.services.framework_review_ai_service import execute_review_task

    context.report_progress(stage="reviewing", progress=10)
    return execute_review_task(context.session, payload=dict(context.payload))


register_task_handler("review_framework_candidate", _handle_review_framework_candidate)


def _handle_assistant_turn(context: TaskContext) -> dict:
    """执行一轮助手对话：意图解析 → 确定性路由 → 结果/提案卡 + 流式正文。

    只写 assistant_messages（与 task_runs）；写操作的执行由前端确认后调既有
    业务 API，本 handler 不碰任何业务表——AI 只产提案不绕教师确认流。
    """
    from app.services.assistant_service import execute_turn_task

    context.report_progress(stage="thinking", progress=10)
    return execute_turn_task(context.session, payload=dict(context.payload))


register_task_handler("assistant_turn", _handle_assistant_turn)


def _handle_material_index(context: TaskContext) -> dict:
    """解析转 ready 后的语料向量索引：只写 content_blocks 向量派生列。

    未配置嵌入服务时静默跳过（检索层走词面降级）；失败只记日志返回 0，
    查询时 ensure_embedded 自愈兜底。
    """
    from app.services.content_index_service import ensure_embedded

    context.report_progress(stage="embedding", progress=10)
    payload = dict(context.payload)
    course_id = str(payload.get("course_id") or "")
    run_ids = [str(payload["run_id"])] if payload.get("run_id") else None
    if not course_id:
        raise ValueError("material_index payload requires course_id")
    embedded = ensure_embedded(context.session, course_id=course_id, run_ids=run_ids)
    return {"embedded_blocks": embedded}


register_task_handler("material_index", _handle_material_index)
