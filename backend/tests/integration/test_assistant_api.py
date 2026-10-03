"""AI 助手对话端点集成测试（HTTP 层 + 任务落库 + worker 端到端 + SSE 兜底）。

镜像 tests/integration/test_blueprint_suggest_api.py 的环境模式（SQLite 内存库 +
dependency_overrides + 真实登录）。LLM 客户端与事件通道在测试里打桩，保证不发
真实模型请求、不连 Redis；worker 端到端用 execute_task 走完整 claim→handler→
complete 链路。SSE 端点在无 Redis 时按任务终态 DB 兜底（首轮 probe 收尾）。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db.schema import (
    Base,
    Course,
    User,
    assistant_messages,
    materials,
    task_runs,
)
from app.db.session import get_session
from app.main import app
from app.services import assistant_service
from app.services.auth_service import hash_password

PATH = "/api/v1/courses/{cid}/assistant"


class StubClient:
    """LLMJsonClient 同接口桩：意图固定为 course_overview 查询 + 流式固定文本。"""

    def __init__(self, intent=None):
        self.intent = intent or {
            "reply": "资料清单见下表：",
            "action": {"tool": "course_overview", "args": {"section": "materials"}},
        }
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "payload": payload})
        return self.intent

    def stream_text(self, *, system_prompt, payload, temperature, on_delta, call_context, **kwargs):
        self.stream_calls.append({"system_prompt": system_prompt, "payload": payload})
        if on_delta is not None:
            on_delta("流式回答")
        return "流式回答"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            User(
                id="admin",
                username="admin",
                password_hash=hash_password("123456"),
                display_name="Admin",
                role="admin",
            )
        )
        session.flush()
        session.add(
            Course(id="c1", owner_id="admin", slug="cs101", name="CS101"),
        )
        session.add(
            Course(id="c2", owner_id="admin", slug="other", name="Other"),
        )
        session.flush()
        session.execute(
            materials.insert().values(
                id="m1", course_id="c1", logical_name="教学大纲",
                material_type="teaching_syllabus", status="staged",
            )
        )
        session.commit()

    def session_override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override

    # outbox 派发打桩：记录调用，避免构造/连接 Celery 与 Redis
    dispatched: list[str] = []

    def fake_dispatch(session, publisher, *, course_id=None, limit=0):  # noqa: A002
        dispatched.append(str(course_id))

    monkeypatch.setattr(
        "app.infrastructure.tasks.outbox.dispatch_pending_events", fake_dispatch
    )
    monkeypatch.setattr(
        "app.infrastructure.tasks.celery_app.CeleryPublisher", lambda: object()
    )
    monkeypatch.setattr(assistant_service, "llm_configured", lambda: True)
    # 全局兜底：任何 get_session_factory 调用（SSE probe、失败消息独立会话）
    # 都必须落在测试库上，杜绝误连真实库（该函数返回 sessionmaker）
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: factory)
    # SSE 测试不连真 Redis（无通道 → DB 兜底首轮 probe 收尾，亚秒返回）
    monkeypatch.setattr(settings, "redis_url", "")

    try:
        runner = TestClient(app)
        login = runner.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "123456"}
        )
        assert login.status_code == 200, login.text
        auth = TestClient(app, headers={"Authorization": "Bearer " + login.json()["token"]})
        yield auth, factory, dispatched
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _turn(auth, *, course="c1", message="看下资料"):
    resp = auth.post(f"{PATH.format(cid=course)}/turns", json={"message": message})
    assert resp.status_code == 202, resp.text
    return resp.json()


def test_turn_202_creates_task_and_user_message(env):
    auth, factory, dispatched = env

    result = _turn(auth, message="第一条消息")
    task_id = result["task_run_id"]
    assert result["user_message_id"]

    with factory() as session:
        row = session.execute(
            select(
                task_runs.c.task_type, task_runs.c.status, task_runs.c.payload
            ).where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
        ).one()
        user_msg = session.execute(
            select(assistant_messages.c.role, assistant_messages.c.content).where(
                assistant_messages.c.id == result["user_message_id"]
            )
        ).one()

    assert row.task_type == "assistant_turn"
    assert row.status == "queued"
    assert row.payload["message"] == "第一条消息"
    assert row.payload["task_run_id"] == task_id
    assert user_msg.role == "user"
    assert user_msg.content == "第一条消息"
    # outbox 派发必须发生且带 course_id（事务性投递，失败会保持 pending）
    assert dispatched == ["c1"]

    # 同文本的在途请求复用同一任务（双击不重复烧模型）
    again = _turn(auth, message="第一条消息")
    assert again["task_run_id"] == task_id


def test_turn_422_blank_and_404_unknown_course(env):
    auth, _factory, dispatched = env

    resp = auth.post(f"{PATH.format(cid='c1')}/turns", json={"message": "   "})
    assert resp.status_code == 422, resp.text
    resp = auth.post(f"{PATH.format(cid='c1')}/turns", json={})
    assert resp.status_code == 422, resp.text

    resp = auth.post(f"{PATH.format(cid='ghost')}/turns", json={"message": "hi"})
    assert resp.status_code == 404, resp.text
    assert dispatched == []  # 422/404 在建任务之前，不应产生任何派发


def test_turn_503_when_llm_unconfigured(env, monkeypatch):
    auth, _factory, dispatched = env
    monkeypatch.setattr(assistant_service, "llm_configured", lambda: False)
    resp = auth.post(f"{PATH.format(cid='c1')}/turns", json={"message": "hi"})
    assert resp.status_code == 503
    assert dispatched == []


def test_messages_restore_is_course_scoped(env):
    auth, factory, _dispatched = env

    _turn(auth, message="c1 的问题")
    # 他课消息：直插，不得出现在本课时间线
    with factory() as session:
        session.execute(
            task_runs.insert().values(
                id="t-other", course_id="c2", task_type="assistant_turn",
                input_version="assistant_turn_v1", idempotency_key="k-other",
                payload={"course_id": "c2"},
            )
        )
        session.execute(
            assistant_messages.insert().values(
                id="msg-other", course_id="c2", task_run_id="t-other",
                role="user", content="他课消息", action={}, stream_status="complete",
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    views = auth.get(f"{PATH.format(cid='c1')}/messages").json()
    assert [v["content"] for v in views] == ["c1 的问题"]
    assert all(v["role"] == "user" for v in views)

    other = auth.get(f"{PATH.format(cid='c2')}/messages").json()
    assert [v["content"] for v in other] == ["他课消息"]


def test_patch_proposal_status_flow(env):
    auth, factory, _dispatched = env

    with factory() as session:
        session.execute(
            task_runs.insert().values(
                id="t-prop", course_id="c1", task_type="assistant_turn",
                input_version="assistant_turn_v1", idempotency_key="k-prop",
                payload={"course_id": "c1"},
            )
        )
        session.execute(
            assistant_messages.insert().values(
                id="msg1", course_id="c1", task_run_id="t-prop",
                role="assistant", content="提案：",
                action={"kind": "proposal", "tool": "create_course", "args": {},
                        "payload": {"body": {"name": "X"}}, "status": "proposed"},
                stream_status="complete",
                created_at=datetime.now(timezone.utc),
            )
        )
        # 普通对话消息（非提案）：不可回写
        session.execute(
            assistant_messages.insert().values(
                id="msg2", course_id="c1", task_run_id="t-prop",
                role="assistant", content="普通回复", action={}, stream_status="complete",
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    # 回写执行回执（只记账，不执行业务）
    resp = auth.patch(
        f"{PATH.format(cid='c1')}/messages/msg1",
        json={"action_status": "executed", "receipt": "已创建课程X"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"]["status"] == "executed"
    assert resp.json()["action"]["receipt"] == "已创建课程X"

    # 单向迁移：executed 后不能再改 → 409
    resp = auth.patch(
        f"{PATH.format(cid='c1')}/messages/msg1", json={"action_status": "dismissed"}
    )
    assert resp.status_code == 409

    # 非法状态值 → 422
    resp = auth.patch(
        f"{PATH.format(cid='c1')}/messages/msg1", json={"action_status": "proposed"}
    )
    assert resp.status_code == 422

    # 非提案消息 → 409
    resp = auth.patch(
        f"{PATH.format(cid='c1')}/messages/msg2", json={"action_status": "executed"}
    )
    assert resp.status_code == 409

    # 课程隔离：他课回写 → 404
    resp = auth.patch(
        f"{PATH.format(cid='c2')}/messages/msg1", json={"action_status": "executed"}
    )
    assert resp.status_code == 404

    # 未知消息 → 404
    resp = auth.patch(
        f"{PATH.format(cid='c1')}/messages/ghost", json={"action_status": "executed"}
    )
    assert resp.status_code == 404


def test_worker_end_to_end_persists_assistant_reply(env, monkeypatch):
    """端到端：turn 入队 → execute_task 完整链路（claim→handler→complete）→ 恢复。"""
    auth, factory, _dispatched = env
    result = _turn(auth, message="看下资料")
    task_id = result["task_run_id"]

    stub = StubClient()
    monkeypatch.setattr(assistant_service, "build_client", lambda: stub)
    monkeypatch.setattr(
        assistant_service, "build_event_sink", assistant_service.MemoryTurnEventSink
    )
    from app.infrastructure.tasks import worker

    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)

    handled = worker.execute_task(task_id, worker_id="test-worker")
    assert handled is True

    with factory() as session:
        row = session.execute(
            select(
                task_runs.c.status, task_runs.c.result, task_runs.c.error_message
            ).where(task_runs.c.id == task_id)
        ).one()

    assert row.status == "succeeded", row.error_message
    assert stub.calls, "段1意图解析必须执行"

    views = auth.get(f"{PATH.format(cid='c1')}/messages").json()
    roles = [v["role"] for v in views]
    assert roles == ["user", "assistant"]
    assistant = views[-1]
    assert assistant["action"]["kind"] == "result"
    assert assistant["action"]["status"] == "completed"
    # 结果卡带真实资料数据（上下文装配进查询，非模型编造）
    assert [m["name"] for m in assistant["action"]["payload"]["materials"]] == ["教学大纲"]
    assert assistant["stream_status"] == "complete"
    assert row.result["message_id"] == assistant["id"]


def test_worker_end_to_end_rag_answer_with_sources(env, monkeypatch):
    """端到端 RAG：点名已解析资料 → 真实语料装载 + 词面检索 → 流式作答 + 来源卡落库。"""
    auth, factory, _dispatched = env
    from app.db.schema import (
        content_blocks,
        document_parse_runs,
        material_versions,
        parser_profiles,
    )
    from app.services import content_index_service

    # 为 m1 建 ready 解析产物（两个块：一相关一无关）
    with factory() as session:
        session.execute(
            material_versions.insert().values(
                id="v1", course_id="c1", material_id="m1", version_no=1, status="staged",
                object_key="courses/c1/m1.pdf", size_bytes=100, sha256="a" * 64,
                mime_type="application/pdf",
            )
        )
        session.execute(
            parser_profiles.insert().values(
                id="mineru-profile", course_id="c1", name="mineru", version="v1",
                provider="mineru", configuration={},
            )
        )
        session.execute(
            document_parse_runs.insert().values(
                id="run1", course_id="c1", material_version_id="v1",
                parser_profile_id="mineru-profile", status="ready",
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.execute(
            content_blocks.insert().values(
                id="rb1", course_id="c1", document_parse_run_id="run1",
                material_version_id="v1", block_index=0, block_type="text",
                text="教学大纲第三章讲解监督学习的分类与回归任务。", heading_path=["第三章"],
                page_index=2, reading_order=0, content_hash="c" * 64,
            )
        )
        session.execute(
            content_blocks.insert().values(
                id="rb2", course_id="c1", document_parse_run_id="run1",
                material_version_id="v1", block_index=1, block_type="text",
                text="实验在 GPU 上完成。", heading_path=[], page_index=7,
                reading_order=1, content_hash="d" * 64,
            )
        )
        session.commit()

    # 嵌入一律视为未配置 → 词面路径（测试不发真实嵌入请求）
    monkeypatch.setattr(content_index_service, "embedding_configured", lambda: False)
    monkeypatch.setattr(assistant_service, "embedding_configured", lambda: False)

    result = _turn(auth, message="总结教学大纲第三章的监督学习分类")
    task_id = result["task_run_id"]
    stub = StubClient(
        intent={
            "reply": "已检索到相关资料，回答如下：",
            "action": {"tool": "answer_material_content", "args": {"material_id": "m1"}},
        }
    )
    monkeypatch.setattr(assistant_service, "build_client", lambda: stub)
    monkeypatch.setattr(
        assistant_service, "build_event_sink", assistant_service.MemoryTurnEventSink
    )
    from app.infrastructure.tasks import worker

    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)

    assert worker.execute_task(task_id, worker_id="test-worker") is True

    with factory() as session:
        row = session.execute(
            select(task_runs.c.status, task_runs.c.error_message).where(task_runs.c.id == task_id)
        ).one()
    assert row.status == "succeeded", row.error_message

    views = auth.get(f"{PATH.format(cid='c1')}/messages").json()
    assistant = views[-1]
    assert assistant["action"]["kind"] == "sources"
    payload = assistant["action"]["payload"]
    assert payload["material_id"] == "m1"
    assert payload["material_name"] == "教学大纲"
    assert payload["mode"] == "lexical"
    # 词面命中相关块 rb1，无关块 rb2 被过滤
    assert [s["block_id"] for s in payload["sources"]] == ["rb1"]
    assert payload["sources"][0]["material_name"] == "教学大纲"
    assert payload["sources"][0]["page_index"] == 2
    assert payload["sources"][0]["heading_path"] == ["第三章"]
    assert assistant["stream_status"] == "complete"

    # 段2 prompt 为 RAG grounding 版本且携带检索片段
    assert stub.stream_calls
    stream_payload = stub.stream_calls[0]["payload"]
    assert stream_payload["question"] == "总结教学大纲第三章的监督学习分类"
    assert stream_payload["sources"][0]["material_name"] == "教学大纲"
    assert "监督学习的分类与回归" in stream_payload["sources"][0]["text"]
    assert "只依据下方「资料片段」作答" in stub.stream_calls[0]["system_prompt"]


def test_worker_failure_persists_failed_message(env, monkeypatch):
    """段1 模型抛错 → 任务 failed + 失败消息可见（刷新不丢）。"""
    auth, factory, _dispatched = env
    result = _turn(auth, message="你好")
    task_id = result["task_run_id"]

    class ExplodingClient:
        def request_json(self, **kwargs):
            raise RuntimeError("gateway exploded")

    monkeypatch.setattr(assistant_service, "build_client", lambda: ExplodingClient())
    monkeypatch.setattr(
        assistant_service, "build_event_sink", assistant_service.MemoryTurnEventSink
    )
    from app.infrastructure.tasks import worker

    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)

    # handler 抛错不让测试中断：worker 自己组装 failed 状态
    worker.execute_task(task_id, worker_id="test-worker")

    with factory() as session:
        row = session.execute(
            select(task_runs.c.status, task_runs.c.error_message).where(
                task_runs.c.id == task_id
            )
        ).one()
    assert row.status == "failed"
    assert "task handler failed" in (row.error_message or "")

    views = auth.get(f"{PATH.format(cid='c1')}/messages").json()
    failed = views[-1]
    assert failed["role"] == "assistant"
    assert failed["stream_status"] == "failed"
    assert "gateway exploded" in failed["content"]


def test_stream_endpoint_db_fallback_done(env, monkeypatch):
    """无 Redis 时 SSE 按任务终态 DB 兜底：succeeded → done 后收尾。"""
    auth, factory, _dispatched = env
    result = _turn(auth, message="看下资料")
    task_id = result["task_run_id"]

    with factory() as session:
        session.execute(
            update(task_runs)
            .where(task_runs.c.id == task_id)
            .values(status="succeeded")
        )
        session.commit()

    resp = auth.get(f"{PATH.format(cid='c1')}/turns/{task_id}/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in resp.text
    assert task_id in resp.text


def test_stream_endpoint_failed_falls_back_to_error(env):
    auth, factory, _dispatched = env
    result = _turn(auth, message="看下资料")
    task_id = result["task_run_id"]

    with factory() as session:
        session.execute(
            update(task_runs)
            .where(task_runs.c.id == task_id)
            .values(status="failed", error_message="LLM boom")
        )
        session.commit()

    resp = auth.get(f"{PATH.format(cid='c1')}/turns/{task_id}/stream")
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "LLM boom" in resp.text


def test_stream_endpoint_404_unknown_or_cross_course(env):
    auth, factory, _dispatched = env
    task_id = _turn(auth, message="归属测试")["task_run_id"]

    # 未知任务
    assert auth.get(f"{PATH.format(cid='c1')}/turns/ghost/stream").status_code == 404
    # 他课任务挂在本课路径下 → 404（course_id 归属校验）
    assert (
        auth.get(f"{PATH.format(cid='c2')}/turns/{task_id}/stream").status_code == 404
    )


# ---------------------------------------------------------------------------
# 会话管理（v3 多会话）
# ---------------------------------------------------------------------------


def test_sessions_crud_over_http(env):
    auth, _factory, _dispatched = env
    base = f"{PATH.format(cid='c1')}/sessions"

    assert auth.get(base).json() == []

    created = auth.post(base, json={"title": "  期末复习  "})
    assert created.status_code == 201
    sid = created.json()["id"]
    assert created.json()["title"] == "期末复习"

    assert [s["id"] for s in auth.get(base).json()] == [sid]

    renamed = auth.patch(f"{base}/{sid}", json={"title": "复习范围"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "复习范围"

    # 空标题 422；跨课程路径挂 c1 的会话 → 404
    assert auth.patch(f"{base}/{sid}", json={"title": "   "}).status_code == 422
    assert (
        auth.patch(f"{PATH.format(cid='c2')}/sessions/{sid}", json={"title": "x"}).status_code
        == 404
    )

    # 删除 204 → 列表空 → 再删 404
    assert auth.delete(f"{base}/{sid}").status_code == 204
    assert auth.get(base).json() == []
    assert auth.delete(f"{base}/{sid}").status_code == 404


def test_turn_session_scoping_idempotency_and_filter(env):
    auth, _factory, _dispatched = env
    sessions_url = f"{PATH.format(cid='c1')}/sessions"
    s1 = auth.post(sessions_url, json={}).json()
    s2 = auth.post(sessions_url, json={}).json()

    # 不带 session_id → 兜底最近会话（s2 后建 → updated_at 更新）
    auto = _turn(auth, message="第一句")
    assert auto["session_id"] == s2["id"]
    titles = {s["id"]: s["title"] for s in auth.get(sessions_url).json()}
    assert titles[s2["id"]] == "第一句"  # 首条消息自动改题

    # 幂等：同会话同文本在途复用、换会话同文本新任务
    r1 = auth.post(
        f"{PATH.format(cid='c1')}/turns",
        json={"message": "同文本", "session_id": s1["id"]},
    )
    assert r1.status_code == 202
    r1b = auth.post(
        f"{PATH.format(cid='c1')}/turns",
        json={"message": "同文本", "session_id": s1["id"]},
    )
    assert r1b.json()["task_run_id"] == r1.json()["task_run_id"]
    r2 = auth.post(
        f"{PATH.format(cid='c1')}/turns",
        json={"message": "同文本", "session_id": s2["id"]},
    )
    assert r2.json()["task_run_id"] != r1.json()["task_run_id"]

    # messages 过滤：s1 只有一条；缺省全课程 3 条
    m1 = auth.get(f"{PATH.format(cid='c1')}/messages?session_id={s1['id']}").json()
    assert [m["content"] for m in m1] == ["同文本"]
    assert len(auth.get(f"{PATH.format(cid='c1')}/messages").json()) == 3

    # 无效会话 404
    assert (
        auth.post(
            f"{PATH.format(cid='c1')}/turns", json={"message": "x", "session_id": "nope"}
        ).status_code
        == 404
    )


def test_delete_session_409_inflight_then_204_cascade(env):
    auth, factory, _dispatched = env
    sid = auth.post(f"{PATH.format(cid='c1')}/sessions", json={}).json()["id"]
    task_id = _turn(auth, message="在途问题")["task_run_id"]  # 兜底 → 唯一会话 sid

    # 在途（queued）→ 409
    assert auth.delete(f"{PATH.format(cid='c1')}/sessions/{sid}").status_code == 409

    # 终态 → 204 + 级联删消息
    with factory() as session:
        session.execute(
            update(task_runs)
            .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
            .values(status="succeeded")
        )
        session.commit()
    assert auth.delete(f"{PATH.format(cid='c1')}/sessions/{sid}").status_code == 204
    assert auth.get(f"{PATH.format(cid='c1')}/messages").json() == []
    assert auth.get(f"{PATH.format(cid='c1')}/sessions").json() == []


# ---------------------------------------------------------------------------
# 停止生成（v3）
# ---------------------------------------------------------------------------


def test_cancel_endpoint_idempotent_and_stream_done(env):
    """停止端点：纯 DB、幂等；取消后 SSE 兜底按 done 正常收口（不是 error）。"""
    auth, _factory, _dispatched = env
    task_id = _turn(auth, message="该停就停")["task_run_id"]

    resp = auth.post(f"{PATH.format(cid='c1')}/turns/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"task_run_id": task_id, "status": "cancelled"}

    # 幂等：已终态原样返回现态
    again = auth.post(f"{PATH.format(cid='c1')}/turns/{task_id}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"

    # 404：未知任务 / 跨课程
    assert auth.post(f"{PATH.format(cid='c1')}/turns/ghost/cancel").status_code == 404
    assert auth.post(f"{PATH.format(cid='c2')}/turns/{task_id}/cancel").status_code == 404

    # SSE DB 兜底：cancelled → done（教师主动停止是正常收口）
    stream = auth.get(f"{PATH.format(cid='c1')}/turns/{task_id}/stream")
    assert "event: done" in stream.text
    assert "event: error" not in stream.text


def test_cancelled_task_worker_claim_refuses(env, monkeypatch):
    """取消后 execute_task 领不到任务：不跑 handler、零模型调用、不落助手消息。"""
    auth, factory, _dispatched = env
    task_id = _turn(auth, message="该被拦下")["task_run_id"]
    auth.post(f"{PATH.format(cid='c1')}/turns/{task_id}/cancel")

    from app.infrastructure.tasks import worker

    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)
    stub = StubClient()
    monkeypatch.setattr(assistant_service, "build_client", lambda: stub)

    handled = worker.execute_task(task_id, worker_id="test-worker")
    assert handled is False
    assert stub.calls == []

    views = auth.get(f"{PATH.format(cid='c1')}/messages").json()
    assert [v["role"] for v in views] == ["user"]  # 只有提问，无回复
    with factory() as session:
        status = session.execute(
            select(task_runs.c.status).where(task_runs.c.id == task_id)
        ).scalar_one()
    assert status == "cancelled"
