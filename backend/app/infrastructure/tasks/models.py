"""Course-scoped durable task state transitions.

Celery is deliberately not a source of task state: these rows are the business
truth and every worker transition is a conditional database update.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.schema import outbox_events, task_runs

DISPATCH_EVENT_TYPE = "task.dispatch"

# 任务终态：到达终态后，同幂等键的新入队请求应当换一把新键（重新发起），
# 而不是复用旧任务的完成态。任务状态机的唯一判定点。
TERMINAL_TASK_STATUSES = ("succeeded", "failed", "cancelled")

# 各任务类型的最长合法执行时长差异极大：生成要跑十几分钟；AI 改题/生成整题是
# 1~2 轮模型调用（45s 超时 × 2 尝试 × 2 轮，最坏 ~180s）；其余按短任务算。
# 租约必须覆盖最坏执行时长，否则任务没跑完租约先过期，任务会被当成失联
# （claim_task 可抢占 → 双跑重烧）。长任务的实际保障是两道防线：
# ① 跑批进度写入即续租（generation_runner._publish_task_progress 心跳）；
# ② 收尾/失败写终态只看 owner、不看租约是否过期（见 complete_task/fail_task）
# —— 2026-10-04 两次 41/49 分钟的生成跑完时租约(1800s)已过期，
# complete_task 被过期门静默挡下，任务永远卡在 running，前端计时器永不停。
LEASE_SECONDS_BY_TYPE = {
    "generation_run": 1800,
    "ai_revise_item": 300,
    "ai_create_item": 300,
    "review_paper_version": 300,
    "propose_exam_rules": 300,
    "suggest_blueprint_adjustments": 300,
    "review_framework_candidate": 300,
    # 助手一轮 = 意图解析 + 可选流式正文（两次模型调用），按短任务上限算
    "assistant_turn": 300,
    # 解析块索引 = 批量嵌入 API 调用（几百块 × 分批），按短任务上限算
    "material_index": 300,
}


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _insert_for(session: Session, table):
    return postgresql_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)


def create_task_run(
    session: Session,
    *,
    course_id: str,
    task_type: str,
    idempotency_key: str,
    input_version: str,
    payload: dict,
    max_attempts: int = 3,
    task_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Create task and its initial event in the caller's transaction.

    The caller owns commit/rollback; therefore a rollback can never leave one
    half of the task/outbox pair committed.
    """

    existing = session.execute(
        select(task_runs.c.id).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    current_time = _now(now)
    new_id = task_id or uuid4().hex
    inserted = session.execute(
        _insert_for(session, task_runs)
        .values(
            id=new_id,
            course_id=course_id,
            task_type=task_type,
            input_version=input_version,
            idempotency_key=idempotency_key,
            status="queued",
            stage="queued",
            progress=0,
            attempt=0,
            max_attempts=max_attempts,
            payload=payload,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_nothing(index_elements=["course_id", "idempotency_key"])
    )
    if inserted.rowcount == 0:
        return session.execute(
            select(task_runs.c.id).where(
                task_runs.c.course_id == course_id,
                task_runs.c.idempotency_key == idempotency_key,
            )
        ).scalar_one()

    session.execute(
        _insert_for(session, outbox_events)
        .values(
            id=uuid4().hex,
            course_id=course_id,
            task_run_id=new_id,
            event_type=DISPATCH_EVENT_TYPE,
            status="pending",
            payload={"task_id": new_id},
            attempts=0,
            available_at=current_time,
            created_at=current_time,
            updated_at=current_time,
        )
        .on_conflict_do_nothing(index_elements=["course_id", "task_run_id", "event_type"], index_where=outbox_events.c.status.in_(("pending", "claimed")))
    )
    return new_id


def claim_task(session: Session, *, course_id: str, task_id: str, worker_id: str, now: datetime | None = None, lease_seconds: int = 60) -> bool:
    current_time = _now(now)
    result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.attempt < task_runs.c.max_attempts,
            ((task_runs.c.status == "queued") | ((task_runs.c.status == "running") & (task_runs.c.lease_expires_at <= current_time))),
        )
        .values(
            status="running",
            stage="running",
            lease_owner=worker_id,
            lease_expires_at=current_time + timedelta(seconds=lease_seconds),
            attempt=task_runs.c.attempt + 1,
            updated_at=current_time,
        )
    )
    return result.rowcount == 1


def refresh_lease(session: Session, *, course_id: str, task_id: str, worker_id: str, now: datetime | None = None, lease_seconds: int = 60) -> bool:
    current_time = _now(now)
    result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(lease_expires_at=current_time + timedelta(seconds=lease_seconds), updated_at=current_time)
    )
    return result.rowcount == 1


def cancel_task(session: Session, *, course_id: str, task_id: str, now: datetime | None = None) -> bool:
    current_time = _now(now)
    result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status.in_(("queued", "running", "waiting_external")),
        )
        .values(status="cancelled", stage="cancelled", lease_owner=None, lease_expires_at=None, updated_at=current_time, completed_at=current_time)
    )
    return result.rowcount == 1


def complete_task(session: Session, *, course_id: str, task_id: str, worker_id: str, result: dict, now: datetime | None = None) -> bool:
    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            # 不校验租约是否过期：owner 门已覆盖抢占（claim_task 易主）与重排
            # （recovery 清 owner），过期门只会让长任务永远收不了尾——
            # 生成一跑 41~49 分钟即超出 1800s 租约，2026-10-04 两单
            # complete_task 因此静默 0 行，任务永远卡 running、前端计时器不停。
        )
        .values(status="succeeded", stage="completed", progress=100, result=result, lease_owner=None, lease_expires_at=None, updated_at=current_time, completed_at=current_time)
    )
    return update_result.rowcount == 1


def fail_task(session: Session, *, course_id: str, task_id: str, worker_id: str, error_code: str, error_message: str, now: datetime | None = None) -> bool:
    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            # 与 complete_task 同口径：只看 owner，过期不挡收尾（异常也可能是
            # 跑满租约后才抛的，过期门会把失败也吞成永远 running）。
        )
        .values(status="failed", stage="failed", error_code=error_code, error_message=error_message, lease_owner=None, lease_expires_at=None, updated_at=current_time, completed_at=current_time)
    )
    return update_result.rowcount == 1


def wait_for_external(
    session: Session,
    *,
    course_id: str,
    task_id: str,
    worker_id: str,
    next_poll_at: datetime,
    now: datetime | None = None,
) -> bool:
    """Persist a provider polling pause only while the worker still owns its lease."""

    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(status="waiting_external", stage="waiting_external", next_poll_at=next_poll_at, lease_owner=None, lease_expires_at=None, updated_at=current_time)
    )
    return update_result.rowcount == 1


def update_task_progress(
    session: Session,
    *,
    course_id: str,
    task_id: str,
    worker_id: str,
    stage: str,
    progress: int,
    now: datetime | None = None,
) -> bool:
    """Update visible worker progress without allowing a stale lease to overwrite it."""

    current_time = _now(now)
    update_result = session.execute(
        update(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.id == task_id,
            task_runs.c.status == "running",
            task_runs.c.lease_owner == worker_id,
            task_runs.c.lease_expires_at > current_time,
        )
        .values(stage=stage, progress=progress, updated_at=current_time)
    )
    return update_result.rowcount == 1
