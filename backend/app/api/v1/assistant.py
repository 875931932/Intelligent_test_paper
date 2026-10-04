"""AI 助手对话端点（课程作用域）：turns 入队 / SSE 事件流 / 消息恢复与回写。

路由只做协议转换：意图解析、白名单校验与只读查询都在 assistant_service；
LLM 调用只发生在 Celery worker（enqueue 走 transactional outbox），SSE 端点
只转发 Redis Stream 事件、不触碰模型。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.v1.auth import get_current_user
from app.config import settings
from app.db.schema import assistant_messages, task_runs
from app.db.session import get_session
from app.services import assistant_service

# 级鉴权：新增端点默认带登录（与 exam_projects 同款）
router = APIRouter(
    prefix="/api/v1/courses/{course_id}/assistant",
    tags=["assistant"],
    dependencies=[Depends(get_current_user)],
)


class AssistantTurnRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str
    # v3 多会话：缺省时后端用课程最近会话（无则新建）——旧客户端兜底
    session_id: str | None = None


class AssistantMessagePatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_status: str
    receipt: str = ""


class AssistantSessionCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""


class AssistantSessionRename(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str


class AssistantGenerationReportRequest(BaseModel):
    """生成结束通报：由后端落一条助手消息（不产生用户消息）。"""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    project_id: str


class AssistantRelayRequest(BaseModel):
    """确认/自动执行成功后的内部接力：推进下一张卡，但不写用户消息。"""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    after_message_id: str


# ---------------------------------------------------------------------------
# 会话管理（v3 多会话：新建 / 重命名 / 删除；切换由前端带 session_id 完成）
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[dict])
def list_sessions(
    course_id: str, session: Session = Depends(get_session)
) -> list[dict]:
    """按最近活跃排序的会话列表。"""
    return assistant_service.list_sessions(session, course_id=course_id)


@router.post("/sessions", response_model=dict, status_code=status.HTTP_201_CREATED)
def create_session(
    course_id: str,
    body: AssistantSessionCreate,
    session: Session = Depends(get_session),
) -> dict:
    try:
        view = assistant_service.create_session(
            session, course_id=course_id, title=body.title
        )
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    return view


@router.patch("/sessions/{session_id}", response_model=dict)
def rename_session(
    course_id: str,
    session_id: str,
    body: AssistantSessionRename,
    session: Session = Depends(get_session),
) -> dict:
    try:
        view = assistant_service.rename_session(
            session, course_id=course_id, session_id=session_id, title=body.title
        )
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)
    return view


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    course_id: str,
    session_id: str,
    session: Session = Depends(get_session),
) -> Response:
    """删除会话及全部消息（级联）；会话内有在途轮次 → 409。"""
    try:
        assistant_service.delete_session(
            session, course_id=course_id, session_id=session_id
        )
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "在途" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=422, detail=msg)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/relays", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
def create_relay(
    course_id: str,
    body: AssistantRelayRequest,
    session: Session = Depends(get_session),
) -> dict:
    """内部接力（202 + task_run_id）：推进出卷阶梯的下一张卡，**不写用户消息**。

    教师点「确认执行」或卡片自动执行完后，前端调这里让助手继续；因为教师这一
    步并没有在对话里说话，替他发一条「继续」会在时间线上留下教师没打过的字。
    幂等：同一条消息只推进一次（重复请求返回原任务，不会把流程推两步）。
    """
    if not assistant_service.llm_configured():
        raise HTTPException(status_code=503, detail="LLM model is not configured")

    try:
        result = assistant_service.enqueue_relay(
            session,
            course_id=course_id,
            session_id=body.session_id,
            after_message_id=body.after_message_id,
        )
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        msg = str(exc)
        raise HTTPException(status_code=404 if "不存在" in msg else 422, detail=msg)

    # 与 turns 同一条投递路径：outbox 未发出时事件保持 pending，不丢任务
    from app.infrastructure.tasks.celery_app import CeleryPublisher
    from app.infrastructure.tasks.outbox import dispatch_pending_events

    try:
        dispatch_pending_events(
            session,
            CeleryPublisher(),
            course_id=course_id,
            limit=5,
        )
        session.commit()
    except Exception:
        session.rollback()

    return result


@router.post("/generation-reports", response_model=dict)
def report_generation(
    course_id: str,
    body: AssistantGenerationReportRequest,
    session: Session = Depends(get_session),
) -> dict:
    """生成结束通报（零 LLM）：由后端往会话里落一条助手消息。

    出卷要跑几分钟，教师全程只是在等待；轮询到生成终态后调这里补上收尾通报，
    聊天里**不会**出现教师没发过的「继续」气泡。幂等：同一项目同一会话只播报一次。
    """
    try:
        result = assistant_service.report_generation_complete(
            session,
            course_id=course_id,
            session_id=body.session_id,
            project_id=body.project_id,
        )
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        msg = str(exc)
        raise HTTPException(status_code=404 if "不存在" in msg else 422, detail=msg)
    return result


# ---------------------------------------------------------------------------
# 一轮对话：入队（202）
# ---------------------------------------------------------------------------


@router.post("/turns", response_model=dict, status_code=status.HTTP_202_ACCEPTED)
def create_turn(
    course_id: str,
    body: AssistantTurnRequest,
    session: Session = Depends(get_session),
) -> dict:
    """写 user 消息并创建 assistant_turn 任务（202 + task_run_id）。

    LLM 调用经 transactional outbox 进 Celery，不进请求线程；提案/回复由
    worker 产出后落 assistant_messages，前端经 SSE 或 GET messages 收取。
    """
    if not assistant_service.llm_configured():
        raise HTTPException(status_code=503, detail="LLM model is not configured")

    try:
        result = assistant_service.enqueue_turn(
            session,
            course_id=course_id,
            message=body.message,
            session_id=body.session_id,
        )
        # 显式 commit：outbox 派发会用另一个事务/连接读取事件，任务行必须先落地
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=422, detail=msg)

    # 真实任务经 transactional outbox 投递给 Celery；投递暂时失败时事件保持
    # pending，任务不会丢失，后续 dispatcher 可重试。
    from app.infrastructure.tasks.celery_app import CeleryPublisher
    from app.infrastructure.tasks.outbox import dispatch_pending_events

    try:
        dispatch_pending_events(
            session,
            CeleryPublisher(),
            course_id=course_id,
            limit=5,
        )
        session.commit()
    except Exception:
        session.rollback()

    return result


@router.post("/turns/{task_run_id}/cancel", response_model=dict)
def cancel_turn(
    course_id: str,
    task_run_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """停止生成（v3）：只改任务状态，**零 LLM**；worker 协作式检查点中止流式并
    保留已流出的部分正文（`stream_status='stopped'`）。

    幂等：任务已到终态（含已取消）时原样返回现态，不报错。
    """
    from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, cancel_task

    row = session.execute(
        select(task_runs.c.status).where(
            task_runs.c.id == task_run_id,
            task_runs.c.course_id == course_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task run not found")
    if row[0] in TERMINAL_TASK_STATUSES:
        return {"task_run_id": task_run_id, "status": row[0]}
    cancel_task(session, course_id=course_id, task_id=task_run_id)
    session.commit()
    current = session.execute(
        select(task_runs.c.status).where(
            task_runs.c.id == task_run_id,
            task_runs.c.course_id == course_id,
        )
    ).scalar_one()
    return {"task_run_id": task_run_id, "status": current}


# ---------------------------------------------------------------------------
# 消息恢复与提案状态回写
# ---------------------------------------------------------------------------


@router.get("/messages", response_model=list[dict])
def list_messages(
    course_id: str,
    session_id: str | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    """按时间序恢复对话（挂载拉取；在途轮次由前端另接 SSE）。

    `session_id` 给定只返回该会话；缺省返回全课程（向后兼容）。
    """
    return assistant_service.list_messages(
        session, course_id=course_id, session_id=session_id
    )


@router.patch("/messages/{message_id}", response_model=dict)
def patch_message(
    course_id: str,
    message_id: str,
    body: AssistantMessagePatch,
    session: Session = Depends(get_session),
) -> dict:
    """提案卡状态回写（proposed → executed/dismissed）：只记账，不执行业务。

    提案的真正执行由前端确认后调既有业务 API 完成，这里只把执行结果记回
    卡片状态（刷新后仍可见）。
    """
    try:
        view = assistant_service.patch_message_action(
            session,
            course_id=course_id,
            message_id=message_id,
            action_status=body.action_status,
            receipt=body.receipt,
        )
        session.commit()
    except assistant_service.AssistantError as exc:
        session.rollback()
        msg = str(exc)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "非法的提案状态" in msg:
            raise HTTPException(status_code=422, detail=msg)
        raise HTTPException(status_code=409, detail=msg)
    return view


# ---------------------------------------------------------------------------
# SSE 事件流
# ---------------------------------------------------------------------------


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


def _sse(event: str, data: dict, entry_id: str | None = None) -> str:
    # id 行 = Redis 流条目 id：前端断线重连时带 last_id 续读，不重放已收增量
    prefix = f"id: {entry_id}\n" if entry_id else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_data(data_raw: str) -> dict:
    try:
        parsed = json.loads(data_raw)
        return parsed if isinstance(parsed, dict) else {"text": data_raw}
    except ValueError:
        return {"text": data_raw}


def _probe_turn_state(course_id: str, task_run_id: str) -> dict:
    """读任务终态与助手消息 id（自开会话，避免跨线程复用请求 session）。"""
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        row = session.execute(
            select(task_runs.c.status, task_runs.c.error_message).where(
                task_runs.c.id == task_run_id,
                task_runs.c.course_id == course_id,
            )
        ).one_or_none()
        if row is None:
            return {"status": "missing", "error_message": None, "message_id": None}
        message_id = session.execute(
            select(assistant_messages.c.id).where(
                assistant_messages.c.task_run_id == task_run_id,
                assistant_messages.c.role == "assistant",
            ).limit(1)
        ).scalar_one_or_none()
        return {
            "status": row[0],
            "error_message": row[1],
            "message_id": message_id,
        }
    finally:
        session.close()


async def _event_stream(course_id: str, task_run_id: str, *, start_id: str = "0"):
    """转发 Redis Stream 事件；事件通道不可用时按任务终态 DB 兜底收尾。

    流式是尽力而为：Redis 整体故障时前端拿不到打字机过程，但 done/error
    兜底保证轮次能正常收尾，消息以 GET messages 落库为准。
    """
    key = f"{assistant_service.STREAM_KEY_PREFIX}{task_run_id}"
    client = None
    try:
        import redis.asyncio as aioredis

        if settings.redis_url:
            client = aioredis.from_url(
                settings.redis_url, socket_timeout=5.0, socket_connect_timeout=2.0
            )
    except Exception:  # noqa: BLE001
        client = None

    last_id = start_id  # 断线重连带 last_id 时从该条目之后续读（XREAD 语义）
    empty_rounds = 0
    # block=500ms/轮 → 上限覆盖 300s 任务租约再留余量
    max_rounds = 720
    rounds = 0
    try:
        while rounds < max_rounds:
            rounds += 1
            got_entries = False
            if client is None:
                # 无事件通道（Redis 未配置/故障）：等节奏退避，避免空转烧 CPU
                await asyncio.sleep(0.5)
            else:
                resp = None
                try:
                    resp = await client.xread({key: last_id}, block=500, count=200)
                except Exception:  # noqa: BLE001
                    client = None  # 连接故障：转 DB 兜底
                if resp:
                    for _stream, entries in resp:
                        for entry_id, fields in entries:
                            got_entries = True
                            last_id = entry_id
                            event = _decode(fields.get("event", fields.get(b"event")))
                            data_raw = _decode(fields.get("data", fields.get(b"data"))) or "{}"
                            yield _sse(event, _parse_data(data_raw), entry_id=entry_id)
                            if event in ("done", "error"):
                                return
            if got_entries:
                empty_rounds = 0
                continue

            empty_rounds += 1
            if empty_rounds == 1 or empty_rounds % 8 == 0:
                state = await run_in_threadpool(_probe_turn_state, course_id, task_run_id)
                if state["status"] in ("succeeded", "cancelled"):
                    # cancelled = 教师主动停止（§10.7）：正常收口而非错误；
                    # 检查点 A 场景 message_id 可为 null，前端以 GET messages 为权威
                    yield _sse(
                        "done",
                        {"message_id": state["message_id"], "task_run_id": task_run_id},
                    )
                    return
                if state["status"] in ("failed", "missing"):
                    yield _sse(
                        "error",
                        {
                            "message": state["error_message"] or "任务未完成",
                            "task_run_id": task_run_id,
                        },
                    )
                    return
            if empty_rounds % 10 == 0:
                yield ": ping\n\n"

        yield _sse(
            "error",
            {"message": "任务等待超时，请重新发送", "task_run_id": task_run_id},
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


def _safe_stream_id(value: str) -> str:
    """XREAD 起点校验：仅接受 Redis 流 id 形态（ms-seq），其余回退 0（从头补读）。"""
    parts = (value or "").split("-")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return value
    return "0"


@router.get("/turns/{task_run_id}/stream")
async def stream_turn(
    course_id: str,
    task_run_id: str,
    last_id: str = "0",
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """SSE 事件流（delta/card/done/error）。

    端点只读转发 Redis Stream（worker 侧发布），不调用模型——LLM 只在
    Celery worker 内执行。事件通道不可用时按任务终态兜底推送。
    `last_id` 为断线重连续读起点（取上一条 SSE `id:` 行），缺省从头补读。
    """
    owned = session.execute(
        select(task_runs.c.id).where(
            task_runs.c.id == task_run_id,
            task_runs.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(status_code=404, detail="task run not found")

    return StreamingResponse(
        _event_stream(course_id, task_run_id, start_id=_safe_stream_id(last_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
