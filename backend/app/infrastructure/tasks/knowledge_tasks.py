"""知识域持久任务：租约、fencing token、重试与 outbox 派发。

与课程任务表分开：``knowledge_jobs`` 作用域是 ``school_id + space_id`` 或
``school_id + session_id``，不伪造 course_id。Celery 只是传输层，最终状态仍
以数据库为准；worker 在开始和收尾前都重新读取当前授权。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.adapters.storage.minio_storage import StoragePort
from app.config import settings
from app.db.schema import (
    User,
    knowledge_document_versions,
    knowledge_jobs,
    knowledge_outbox_events,
    knowledge_parse_runs,
    knowledge_spaces,
)

logger = logging.getLogger(__name__)

JOB_EVENT_TYPE = "knowledge.job"
_ACTIVE_STATES = ("queued", "running")

# 并发预算：单账号同时进行的知识任务与单库排队上限，避免批量上传挤占解析。
MAX_ACTIVE_JOBS_PER_ACTOR = 5
MAX_QUEUED_JOBS_PER_SPACE = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_storage() -> StoragePort:
    """与课程资料共用同一套存储选择逻辑（未配置 S3 时回退本地磁盘）。"""

    if not (settings.s3_endpoint and settings.s3_access_key and settings.s3_secret_key):
        from app.adapters.storage.local_storage import LocalStorage

        return LocalStorage()
    from app.adapters.storage.minio_storage import MinioStorage

    return MinioStorage(
        endpoint=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        internal_endpoint=settings.s3_internal_endpoint or None,
    )


# ── 入队 ────────────────────────────────────────────────────────────────────


def enqueue_parse_job(
    session: Session,
    *,
    school_id: str,
    space_id: str,
    actor_id: str,
    version_id: str,
    parse_run_id: str,
) -> str:
    """任务与 outbox 同事务写入；同一版本重复入队返回既有任务。"""

    idempotency_key = f"parse:{version_id}"
    existing = session.execute(
        select(knowledge_jobs.c.id).where(
            knowledge_jobs.c.school_id == school_id,
            knowledge_jobs.c.actor_id == actor_id,
            knowledge_jobs.c.kind == "parse_document",
            knowledge_jobs.c.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    from app.services.knowledge_space_service import KnowledgeError

    # 锁定账号与空间行，串行化并发入队时的限额计数。
    session.execute(select(User.id).where(User.id == actor_id).with_for_update())
    session.execute(
        select(knowledge_spaces.c.id)
        .where(
            knowledge_spaces.c.id == space_id,
            knowledge_spaces.c.school_id == school_id,
        )
        .with_for_update()
    )
    active_for_actor = session.scalar(
        select(func.count()).where(
            knowledge_jobs.c.actor_id == actor_id,
            knowledge_jobs.c.school_id == school_id,
            knowledge_jobs.c.state.in_(_ACTIVE_STATES),
        )
    )
    if int(active_for_actor or 0) >= MAX_ACTIVE_JOBS_PER_ACTOR:
        raise KnowledgeError(429, "RATE_LIMITED", "同时进行的解析任务过多，请等待完成后再上传")
    queued_in_space = session.scalar(
        select(func.count()).where(
            knowledge_jobs.c.space_id == space_id,
            knowledge_jobs.c.school_id == school_id,
            knowledge_jobs.c.state == "queued",
        )
    )
    if int(queued_in_space or 0) >= MAX_QUEUED_JOBS_PER_SPACE:
        raise KnowledgeError(429, "RATE_LIMITED", "该知识库排队任务已达上限")

    job_id = uuid4().hex
    now = _now()
    session.execute(
        knowledge_jobs.insert().values(
            id=job_id,
            school_id=school_id,
            space_id=space_id,
            session_id=None,
            actor_id=actor_id,
            kind="parse_document",
            payload={"version_id": version_id, "parse_run_id": parse_run_id},
            payload_hash=None,
            state="queued",
            progress=0,
            attempts=0,
            fencing_token=0,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
    )
    session.execute(
        knowledge_outbox_events.insert().values(
            id=uuid4().hex,
            school_id=school_id,
            job_id=job_id,
            event_type=JOB_EVENT_TYPE,
            state="pending",
            attempts=0,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    return job_id


# ── 租约与 fencing ──────────────────────────────────────────────────────────


def claim_knowledge_job(
    session: Session, *, job_id: str, worker_id: str, lease_seconds: int = 300
) -> dict | None:
    """原子领取：queued 或租约已过期的 running 都可被接管，fencing token 递增。"""

    now = _now()
    result = session.execute(
        update(knowledge_jobs)
        .where(
            knowledge_jobs.c.id == job_id,
            or_(
                knowledge_jobs.c.state == "queued",
                (knowledge_jobs.c.state == "running")
                & or_(
                    knowledge_jobs.c.lease_until.is_(None),
                    knowledge_jobs.c.lease_until <= now,
                ),
            ),
        )
        .values(
            state="running",
            lease_owner=worker_id,
            lease_until=now + timedelta(seconds=lease_seconds),
            fencing_token=knowledge_jobs.c.fencing_token + 1,
            attempts=knowledge_jobs.c.attempts + 1,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return dict(
        session.execute(
            select(knowledge_jobs).where(knowledge_jobs.c.id == job_id)
        ).mappings().one()
    )


def heartbeat_knowledge_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    fencing_token: int,
    lease_seconds: int = 300,
) -> bool:
    now = _now()
    result = session.execute(
        update(knowledge_jobs)
        .where(
            knowledge_jobs.c.id == job_id,
            knowledge_jobs.c.state == "running",
            knowledge_jobs.c.lease_owner == worker_id,
            knowledge_jobs.c.fencing_token == fencing_token,
        )
        .values(lease_until=now + timedelta(seconds=lease_seconds), updated_at=now)
    )
    session.commit()
    return result.rowcount == 1


def finish_knowledge_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    fencing_token: int,
    state: str,
    result: dict | None = None,
    error_code: str | None = None,
) -> bool:
    """只有仍持有同一 fencing token 的 worker 能写终态，防止迟到结果覆盖接管者。"""

    now = _now()
    updated = session.execute(
        update(knowledge_jobs)
        .where(
            knowledge_jobs.c.id == job_id,
            knowledge_jobs.c.state == "running",
            knowledge_jobs.c.lease_owner == worker_id,
            knowledge_jobs.c.fencing_token == fencing_token,
        )
        .values(
            state=state,
            progress=100 if state == "succeeded" else knowledge_jobs.c.progress,
            result=result,
            error_code=error_code,
            lease_owner=None,
            lease_until=None,
            updated_at=now,
            completed_at=now,
        )
    ).rowcount
    session.commit()
    return updated == 1


def job_view(session: Session, job_id: str) -> dict | None:
    row = session.execute(
        select(knowledge_jobs).where(knowledge_jobs.c.id == job_id)
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "kind": row.kind,
        "state": row.state,
        "progress": row.progress,
        "attempts": row.attempts,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ── 执行 ────────────────────────────────────────────────────────────────────


def _actor_can_edit(session: Session, actor_id: str, *, school_id: str, space_id: str) -> bool:
    """收尾授权复核：账号有效、同校、仍是可编辑成员。"""

    user = session.get(User, actor_id)
    if user is None or user.status != "active":
        return False
    space = session.execute(
        select(knowledge_spaces).where(
            knowledge_spaces.c.id == space_id,
            knowledge_spaces.c.school_id == school_id,
        )
    ).mappings().one_or_none()
    if space is None or space.status != "active":
        return False
    from app.services.knowledge_space_service import load_actor, require_space_access
    from app.services.knowledge_space_service import KnowledgeError as SpaceError

    actor = load_actor(session, user)
    try:
        require_space_access(session, actor, space_id=space_id, action="edit")
    except SpaceError:
        return False
    return True


def execute_knowledge_job(
    job_id: str,
    *,
    worker_id: str | None = None,
    storage: StoragePort | None = None,
    session: Session | None = None,
) -> bool:
    """领取并执行一个知识任务；无任务/租约竞争失败返回 False。

    ``session`` 由 API 进程内执行（开发/测试）传入，用于复用调用方的数据库
    连接与会话；Celery worker 不传，使用进程级 session factory。
    """

    from app.db.session import get_session_factory

    owns_session = session is None
    worker_id = worker_id or f"{os.getpid()}:{uuid4().hex}"
    if session is None:
        session = get_session_factory()()
    try:
        job = claim_knowledge_job(session, job_id=job_id, worker_id=worker_id)
        if job is None:
            return False
        fencing_token = int(job["fencing_token"])
        school_id = job["school_id"]
        space_id = job["space_id"]
        payload = dict(job["payload"] or {})

        if job["kind"] != "parse_document":
            finish_knowledge_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                state="failed",
                error_code="handler_not_registered",
            )
            return False
        version_id = str(payload.get("version_id") or "")
        parse_run_id = str(payload.get("parse_run_id") or "")
        if not space_id or not version_id or not parse_run_id:
            finish_knowledge_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                state="failed",
                error_code="invalid_payload",
            )
            return False

        if not _actor_can_edit(session, job["actor_id"], school_id=school_id, space_id=space_id):
            _mark_version_failed(
                session,
                school_id=school_id,
                space_id=space_id,
                version_id=version_id,
                parse_run_id=parse_run_id,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            finish_knowledge_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                state="failed",
                error_code="access_revoked",
            )
            return False

        heartbeat_knowledge_job(
            session, job_id=job_id, worker_id=worker_id, fencing_token=fencing_token
        )
        from app.services import knowledge_document_service

        try:
            knowledge_document_service.run_parse(
                session,
                storage or default_storage(),
                school_id=school_id,
                space_id=space_id,
                version_id=version_id,
                parse_run_id=parse_run_id,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                authorize=lambda: _actor_can_edit(
                    session, job["actor_id"], school_id=school_id, space_id=space_id
                ),
            )
        except Exception as exc:
            if getattr(exc, "code", None) == "LEASE_LOST":
                # 已被接管：不得覆盖新持有者的正文或终态。
                session.rollback()
                logger.warning("knowledge job lease lost (job=%s)", job_id)
                return False
            if getattr(exc, "code", None) == "access_revoked":
                # 提交前复核撤权：版本与正文均未落库，只记录任务失败。
                session.rollback()
                _mark_version_failed(
                    session,
                    school_id=school_id,
                    space_id=space_id,
                    version_id=version_id,
                    parse_run_id=parse_run_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                )
                finish_knowledge_job(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    state="failed",
                    error_code="access_revoked",
                )
                return False
            logger.warning("knowledge parse job failed (job=%s): %s", job_id, exc)
            finish_knowledge_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                state="failed",
                error_code="parse_failed",
            )
            return False

        # 授权复核已与正文/版本提交在同一事务内完成（run_parse 的 authorize）。
        finish_knowledge_job(
            session,
            job_id=job_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            state="succeeded",
            result={"version_id": version_id, "parse_run_id": parse_run_id},
        )
        return True
    finally:
        if owns_session:
            session.close()


def _mark_version_failed(
    session: Session,
    *,
    school_id: str,
    space_id: str,
    version_id: str,
    parse_run_id: str,
    job_id: str | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
) -> None:
    from app.services.knowledge_document_service import _assert_job_fence

    try:
        _assert_job_fence(
            session, job_id=job_id, worker_id=worker_id, fencing_token=fencing_token
        )
    except Exception:
        # 租约已易主：旧 worker 不再改写版本状态。
        session.rollback()
        return
    now = _now()
    session.execute(
        update(knowledge_parse_runs)
        .where(knowledge_parse_runs.c.id == parse_run_id)
        .values(state="failed", error_code="access_revoked", updated_at=now)
    )
    session.execute(
        update(knowledge_document_versions)
        .where(knowledge_document_versions.c.id == version_id)
        .values(state="failed")
    )
    session.commit()


# ── outbox 派发 ─────────────────────────────────────────────────────────────


def publish_job_event(session: Session, job_id: str) -> bool:
    """尽力派发到 Celery；失败时事件保持 pending，后台可安全重试。"""

    now = _now()
    event = session.execute(
        select(knowledge_outbox_events)
        .where(
            knowledge_outbox_events.c.job_id == job_id,
            knowledge_outbox_events.c.state == "pending",
            knowledge_outbox_events.c.available_at <= now,
        )
        .order_by(knowledge_outbox_events.c.created_at)
        .limit(1)
    ).mappings().one_or_none()
    if event is None:
        return False
    from app.infrastructure.tasks.celery_app import CeleryPublisher

    try:
        CeleryPublisher().publish(event.event_type, {"job_id": job_id})
    except Exception as exc:
        session.rollback()
        _record_publish_failure(session, event_id=event.id)
        return False
    session.execute(
        update(knowledge_outbox_events)
        .where(knowledge_outbox_events.c.id == event.id)
        .values(state="published", updated_at=now)
    )
    session.commit()
    return True


def _record_publish_failure(session: Session, *, event_id: str) -> None:
    """单独事务记录失败次数；broker 不可用不应回滚已提交的业务数据。"""

    now = _now()
    try:
        session.execute(
            update(knowledge_outbox_events)
            .where(knowledge_outbox_events.c.id == event_id, knowledge_outbox_events.c.state == "pending")
            .values(attempts=knowledge_outbox_events.c.attempts + 1, updated_at=now)
        )
        session.commit()
    except Exception:
        session.rollback()


def dispatch_pending_knowledge_events(
    session: Session, *, limit: int = 20, claim_seconds: int = 60
) -> int:
    """把 pending 事件派发到 ``knowledge`` 队列；失败保持 pending 可重试。

    同时回收租约过期的 ``claimed`` 事件——派发进程中断后不能永久占用。
    """

    now = _now()
    recovered = session.execute(
        update(knowledge_outbox_events)
        .where(
            knowledge_outbox_events.c.state == "claimed",
            knowledge_outbox_events.c.claim_until.is_not(None),
            knowledge_outbox_events.c.claim_until < now,
        )
        .values(
            state="pending",
            claim_owner=None,
            claim_until=None,
            attempts=knowledge_outbox_events.c.attempts + 1,
            updated_at=now,
        )
    ).rowcount
    if recovered:
        session.commit()
    events = session.execute(
        select(knowledge_outbox_events)
        .where(
            knowledge_outbox_events.c.state == "pending",
            knowledge_outbox_events.c.available_at <= now,
        )
        .order_by(knowledge_outbox_events.c.created_at)
        .limit(limit)
    ).mappings().all()
    if not events:
        return 0
    from app.infrastructure.tasks.celery_app import CeleryPublisher

    publisher = CeleryPublisher()
    published = 0
    for event in events:
        owner = uuid4().hex
        claimed = session.execute(
            update(knowledge_outbox_events)
            .where(
                knowledge_outbox_events.c.id == event.id,
                knowledge_outbox_events.c.state == "pending",
            )
            .values(
                state="claimed",
                claim_owner=owner,
                claim_until=now + timedelta(seconds=claim_seconds),
                updated_at=now,
            )
        ).rowcount
        if claimed != 1:
            continue
        session.commit()
        try:
            publisher.publish(event.event_type, {"job_id": event.job_id})
        except Exception as exc:
            logger.warning("knowledge outbox publish failed (event=%s): %s", event.id, exc)
            session.rollback()
            session.execute(
                update(knowledge_outbox_events)
                .where(
                    knowledge_outbox_events.c.id == event.id,
                    knowledge_outbox_events.c.state == "claimed",
                    knowledge_outbox_events.c.claim_owner == owner,
                )
                .values(
                    state="pending",
                    attempts=knowledge_outbox_events.c.attempts + 1,
                    claim_owner=None,
                    claim_until=None,
                    updated_at=_now(),
                )
            )
            session.commit()
            continue
        finalized = session.execute(
            update(knowledge_outbox_events)
            .where(
                knowledge_outbox_events.c.id == event.id,
                knowledge_outbox_events.c.state == "claimed",
                knowledge_outbox_events.c.claim_owner == owner,
            )
            .values(state="published", claim_owner=None, claim_until=None, updated_at=_now())
        ).rowcount
        session.commit()
        if finalized == 1:
            published += 1
    return published
