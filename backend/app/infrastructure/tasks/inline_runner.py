"""进程内轻量任务执行器：用于测试或开发环境，无需 Celery worker。

轮询下一个 queued task → 原子租约 → 调 handler → 标记完成/失败。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.schema import task_runs


def _now() -> datetime:
    return datetime.now(UTC)


def run_once(
    session_factory: sessionmaker[Session],
    *,
    handlers: dict[str, Callable] | None = None,
    worker_id: str | None = None,
    lease_seconds: int = 300,
) -> bool:
    """尝试执行一个 queued 任务。成功执行返回 True，无任务返回 False。

    handlers 默认：{'generation_run': execute_generation_task_handler}
    每个 handler 签名：handler(session, task_run_row) -> None。
    """
    if handlers is None:
        from app.services.generation_runner_service import (
            execute_generation_task_handler,
        )
        handlers = {"generation_run": execute_generation_task_handler}
    worker_id = worker_id or f"inline-{uuid.uuid4().hex[:8]}"

    task: dict | None = None
    course_id: str | None = None
    with session_factory() as probe_session:
        # 查找一条 queued 任务：按 created_at 升序（最老优先）
        # 注意 SQLite 没有 SKIP LOCKED；这里用乐观租约：查 → update where status=queued
        row = probe_session.execute(
            select(task_runs)
            .where(task_runs.c.status == "queued")
            .order_by(task_runs.c.created_at)
            .limit(1)
        ).one_or_none()
        if row is None:
            return False
        task_id = row._mapping["id"]
        course_id = row._mapping["course_id"]
        now = _now()
        # 原子性抢租：WHERE 子句包含 status='queued' 保证同一时刻只一个 worker 成功
        res = probe_session.execute(
            task_runs.update()
            .where(
                task_runs.c.id == task_id,
                task_runs.c.course_id == course_id,
                task_runs.c.status == "queued",
            )
            .values(
                status="running",
                stage="leased",
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt=task_runs.c.attempt + 1,
                updated_at=now,
            )
        )
        probe_session.commit()
        if res.rowcount != 1:
            return False
        # 重新加载完整 task_run 行
        reloaded = probe_session.execute(
            select(task_runs).where(
                task_runs.c.id == task_id,
                task_runs.c.course_id == course_id,
            )
        ).one()
        task = dict(reloaded._mapping)

    assert task is not None and course_id is not None

    task_type = task["task_type"]
    handler = handlers.get(task_type)
    if handler is None:
        # 无对应 handler：回滚为 queued，attempt 保持
        with session_factory() as sess:
            sess.execute(
                task_runs.update()
                .where(
                    task_runs.c.id == task["id"],
                    task_runs.c.course_id == course_id,
                )
                .values(
                    status="queued",
                    stage="handler_missing",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=_now(),
                )
            )
            sess.commit()
        return False

    try:
        with session_factory() as exec_session:
            handler(exec_session, task)
        return True
    except Exception as exc:
        # handler 内部已做状态标记，这里只是兜底
        with session_factory() as fallback:
            now = _now()
            err_msg = str(exc)[:2000]
            fallback.execute(
                task_runs.update()
                .where(
                    task_runs.c.id == task["id"],
                    task_runs.c.course_id == course_id,
                )
                .values(
                    status="failed",
                    error_message=err_msg,
                    updated_at=now,
                )
            )
            fallback.commit()
        return True
