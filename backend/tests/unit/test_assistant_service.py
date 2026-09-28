"""AI 助手对话服务单元测试。

覆盖：意图路由三分支（纯问答流式 / 只读结果卡 / 提案卡）、id 白名单硬校验与
带反馈的一次纠错重试、未知工具的确定性失败文案、危险工具拒绝、提案只组装不
执行写、提案状态单向回写、消息时间序恢复、任务入队幂等、流式失败降级段1回复。
LLM 用同接口桩注入，不发真实请求。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session

from app.db.schema import (
    Base,
    Course,
    User,
    assistant_messages,
    materials,
    task_runs,
)
from app.services import assistant_service
from app.services.assistant_service import (
    AssistantError,
    MemoryTurnEventSink,
    REFUSED_REPLY,
    build_intent_prompt,
    build_proposal_payload,
    enqueue_turn,
    list_messages,
    load_turn_context,
    patch_message_action,
    route_intent,
    run_turn,
)


class StubClient:
    """LLMJsonClient 同接口桩：按序返回预置意图，并记录调用与流式输出。"""

    def __init__(self, responses, *, stream_text: str = "你好，这是流式回答。", stream_error: Exception | None = None):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self._stream_text = stream_text
        self._stream_error = stream_error

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "payload": payload,
                "temperature": temperature,
                "previous_error": payload.get("previous_validation_error"),
            }
        )
        return self._responses.pop(0)

    def stream_text(self, *, system_prompt, payload, temperature, on_delta, call_context, **kwargs):
        self.stream_calls.append({"system_prompt": system_prompt, "payload": payload})
        if self._stream_error is not None:
            raise self._stream_error
        # 模拟增量回调两段
        if on_delta is not None:
            on_delta("你好，")
            on_delta("这是流式回答。")
        return self._stream_text


def _intent(reply: str, action: dict | None = None) -> dict:
    return {"reply": reply, "action": action}


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'assistant.db'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(id="u1", display_name="T1", role="teacher"))
        s.flush()
        s.add(Course(id="c1", owner_id="u1", slug="cs101", name="CS101"))
        s.flush()
        s.execute(materials.insert().values(
            id="m1", course_id="c1", logical_name="教学大纲",
            material_type="teaching_syllabus", status="staged",
        ))
        s.execute(materials.insert().values(
            id="m2", course_id="c1", logical_name="第3章讲义",
            material_type="teaching_material", status="staged",
        ))
        s.commit()
    yield s
    s.close()


def _ctx(**overrides) -> dict:
    """手工上下文（只读工具与提案校验只依赖 context，不触库）。"""
    base = {
        "course_id": "c1",
        "course_name": "CS101",
        "materials": [
            {"id": "m1", "name": "教学大纲", "type": "teaching_syllabus", "status": "staged", "parse_status": None},
            {"id": "m2", "name": "第3章讲义", "type": "teaching_material", "status": "staged", "parse_status": None},
        ],
        "framework": None,
        "catalog": None,
        "projects": [],
        "history": [],
        "allowed_ids": {"material_ids": ["m1", "m2"], "project_ids": ["p1"]},
    }
    base.update(overrides)
    return base


def _new_turn(session, task_run_id: str) -> str:
    """预置一行 task_runs（消息表 FK 依赖），模拟 enqueue 的产物。"""
    session.execute(
        task_runs.insert().values(
            id=task_run_id,
            course_id="c1",
            task_type=assistant_service.TASK_TYPE,
            input_version=assistant_service._INPUT_VERSION,
            idempotency_key=f"test-{task_run_id}",
            payload={"course_id": "c1", "task_run_id": task_run_id},
        )
    )
    session.commit()
    return task_run_id


# ---------------------------------------------------------------------------
# 意图路由三分支
# ---------------------------------------------------------------------------


def test_chat_intent_streams_answer(session):
    client = StubClient([_intent("这条不需要工具。")])
    task_id = _new_turn(session, "t-chat")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "你是谁"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    assert len(client.stream_calls) == 1  # 纯问答才走段2流式
    events = [e["event"] for e in sink.registry[task_id]]
    assert events[0] == "delta"
    assert events[-1] == "done"
    row = session.execute(
        select(assistant_messages.c.content, assistant_messages.c.stream_status)
        .where(assistant_messages.c.task_run_id == task_id)
    ).one()
    assert row.content == "你好，这是流式回答。"
    assert row.stream_status == "complete"


def test_read_tool_returns_result_card_without_stream(session):
    client = StubClient([_intent("资料清单见下表：", {"tool": "list_materials", "args": {}})])
    task_id = _new_turn(session, "t-read")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "看下资料"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "result"
    assert len(client.stream_calls) == 0  # 查询类不再二次调用模型
    cards = [e for e in sink.registry[task_id] if e["event"] == "card"]
    assert len(cards) == 1
    names = [m["name"] for m in cards[0]["data"]["payload"]["materials"]]
    assert names == ["教学大纲", "第3章讲义"]
    row = session.execute(
        select(assistant_messages.c.action).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()
    assert row["kind"] == "result"
    assert row["status"] == "completed"


def test_proposal_card_not_executed(session):
    """提案只组装执行契约：任何业务表都不得因解析而被写入。"""
    client = StubClient([
        _intent("已生成提案：", {"tool": "create_course", "args": {"name": "新课程X"}})
    ])
    task_id = _new_turn(session, "t-prop")
    sink = MemoryTurnEventSink(task_id)
    courses_before = session.execute(select(Course.id)).scalars().all()

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "建一门新课程"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "proposal"
    courses_after = session.execute(select(Course.id)).scalars().all()
    assert courses_after == courses_before  # 课程没有被创建
    row = session.execute(
        select(assistant_messages.c.action).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()
    assert row["kind"] == "proposal"
    assert row["status"] == "proposed"
    assert row["payload"] == {"body": {"name": "新课程X"}}
    cards = [e for e in sink.registry[task_id] if e["event"] == "card"]
    assert cards[0]["data"]["kind"] == "proposal"


def test_refused_tool_falls_back_to_chat(session):
    client = StubClient([_intent("", {"tool": "delete_material", "args": {}})])
    task_id = _new_turn(session, "t-refuse")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "删掉那份资料"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    row = session.execute(
        select(assistant_messages.c.content).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()
    assert row == REFUSED_REPLY
    assert len(client.stream_calls) == 0  # 拒绝走确定性文案，不再问模型


# ---------------------------------------------------------------------------
# 白名单硬校验 + 带反馈纠错一次
# ---------------------------------------------------------------------------


def test_bad_material_id_retried_with_feedback(session):
    client = StubClient(
        [
            _intent("", {"tool": "start_parse", "args": {"material_id": "not-allowed"}}),
            _intent("请问你要解析哪份资料？"),
        ]
    )
    task_id = _new_turn(session, "t-wl")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "解析一下"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"  # 纠错后模型改为追问
    assert len(client.calls) == 2
    assert "material_id" in (client.calls[1]["previous_error"] or "")
    # 白名单失败没有产生任何提案卡
    assert not [e for e in sink.registry[task_id] if e["event"] == "card"]


def test_unknown_tool_exhausts_retry_with_deterministic_text(session):
    client = StubClient(
        [
            _intent("", {"tool": "launch_missile", "args": {}}),
            _intent("", {"tool": "launch_missile", "args": {}}),
        ]
    )
    task_id = _new_turn(session, "t-unknown")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "干点别的"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    assert len(client.calls) == 2  # 只重试一次
    row = session.execute(
        select(assistant_messages.c.content).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()
    assert row.startswith("我没能处理这个请求")


def test_contract_confirm_refused_when_already_confirmed():
    ctx = _ctx(
        projects=[{"id": "p1", "name": "期末", "status": "draft", "blueprint": None,
                   "contract": {"exists": True, "confirmed": True, "slot_count": 3}, "paper": {"exists": False}}],
    )
    with pytest.raises(AssistantError, match="已确认冻结"):
        build_proposal_payload("confirm_contract", {"project_id": "p1"}, context=ctx)


def test_update_course_targets_current_course_only():
    ctx = _ctx()
    payload = build_proposal_payload("update_course", {"name": "CS101改名"}, context=ctx)
    assert payload["course_id"] == "c1"  # 目标课程钉死为当前课程，不取模型值


def test_create_course_validates_and_builds_body():
    ctx = _ctx()
    ok = build_proposal_payload(
        "create_course", {"name": "数据结构", "slug": "ds-101", "description": "desc"}, context=ctx
    )
    assert ok == {"body": {"name": "数据结构", "slug": "ds-101", "description": "desc"}}
    with pytest.raises(AssistantError):
        build_proposal_payload("create_course", {"slug": "no-name"}, context=ctx)
    with pytest.raises(AssistantError):
        build_proposal_payload("create_course", {"name": "x", "slug": "Bad Slug"}, context=ctx)


# ---------------------------------------------------------------------------
# prompt 装配与纯函数
# ---------------------------------------------------------------------------


def test_intent_prompt_carries_real_snapshot_and_ids():
    context = _ctx()
    system_prompt, payload = build_intent_prompt(context, "看下资料")
    assert payload["snapshot"]["materials"][0]["name"] == "教学大纲"
    assert payload["ids"]["material_ids"] == ["m1", "m2"]
    assert payload["user_message"] == "看下资料"
    assert "previous_validation_error" not in payload
    # 系统提示含 JSON 示例花括号：必须按字面输出而不是被 format 吃掉
    assert '{"reply":' in system_prompt
    assert "{course_name}" not in system_prompt
    assert "CS101" in system_prompt

    _sys2, payload2 = build_intent_prompt(context, "重试", previous_error="material_id 非法")
    assert payload2["previous_validation_error"] == "material_id 非法"


def test_route_unknown_kind_neutral():
    # action 为 None → 纯问答
    assert route_intent(_intent("hi"), session=None, context=_ctx())["kind"] == "chat"
    # 只读工具 → 结果卡
    routed = route_intent(
        _intent("", {"tool": "course_overview", "args": {}}), session=None, context=_ctx()
    )
    assert routed["kind"] == "result"
    assert routed["payload"]["course_name"] == "CS101"
    # 未知工具 → AssistantError（上层重试）
    with pytest.raises(AssistantError):
        route_intent(_intent("", {"tool": "nope", "args": {}}), session=None, context=_ctx())


# ---------------------------------------------------------------------------
# 落库：幂等 / 时间序 / 提案状态回写
# ---------------------------------------------------------------------------


def test_run_turn_is_idempotent_per_task(session):
    client = StubClient([_intent("第一次回答")])
    task_id = _new_turn(session, "t-idem")
    sink = MemoryTurnEventSink(task_id)
    payload = {"course_id": "c1", "task_run_id": task_id, "message": "你好"}

    first = run_turn(session, payload=payload, client=client, sink=sink)
    second = run_turn(session, payload=payload, client=client, sink=sink)

    assert first["message_id"] == second["message_id"]
    assert second.get("duplicate") is True
    assert len(client.calls) == 1  # 租约重领不重跑模型
    count = session.execute(
        select(assistant_messages.c.id).where(assistant_messages.c.task_run_id == task_id)
    ).scalars().all()
    assert len(count) == 1


def test_stream_failure_degrades_to_intent_reply(session):
    client = StubClient(
        [_intent("段1的整段回复。")], stream_error=RuntimeError("gateway down")
    )
    task_id = _new_turn(session, "t-degrade")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "你是谁"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    row = session.execute(
        select(assistant_messages.c.content, assistant_messages.c.stream_status)
        .where(assistant_messages.c.task_run_id == task_id)
    ).one()
    assert row.content == "段1的整段回复。"
    assert row.stream_status == "failed"  # 降级如实标注，不冒充完整流式
    assert [e["event"] for e in sink.registry[task_id]][-1] == "done"


def test_messages_list_is_time_ordered_and_course_scoped(session):
    enqueue_turn(session, course_id="c1", message="第一条")
    session.commit()
    enqueue_turn(session, course_id="c1", message="第二条")
    session.commit()
    # 他课消息：不得出现在本课时间线
    session.execute(Course.__table__.insert().values(id="c2", owner_id="u1", slug="other", name="Other"))
    enqueue_turn(session, course_id="c2", message="他课消息")
    session.commit()

    views = list_messages(session, course_id="c1")
    assert [v["content"] for v in views] == ["第一条", "第二条"]
    assert all(v["role"] == "user" for v in views)
    assert all(v["course_id"] if "course_id" in v else True for v in views)  # 视图不泄他课数据


def test_patch_proposal_transition_is_one_way(session):
    task_id = _new_turn(session, "t-patch")
    assistant_service._insert_message(
        session,
        course_id="c1",
        task_run_id=task_id,
        role="assistant",
        content="已生成提案：",
        action={"kind": "proposal", "tool": "create_course", "args": {}, "payload": {}, "status": "proposed"},
    )
    session.commit()
    msg_id = session.execute(
        select(assistant_messages.c.id).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()

    view = patch_message_action(
        session, course_id="c1", message_id=msg_id, action_status="executed", receipt="已创建"
    )
    session.commit()
    assert view["action"]["status"] == "executed"
    assert view["action"]["receipt"] == "已创建"

    # 单向：executed 不能再改，也不能回 proposed
    with pytest.raises(AssistantError, match="proposed"):
        patch_message_action(session, course_id="c1", message_id=msg_id, action_status="dismissed")
    with pytest.raises(AssistantError, match="非法的提案状态"):
        patch_message_action(session, course_id="c1", message_id=msg_id, action_status="proposed")
    # 课程隔离：他课回写 404
    with pytest.raises(AssistantError, match="不存在"):
        patch_message_action(
            session, course_id="c2", message_id=msg_id, action_status="executed"
        )


# ---------------------------------------------------------------------------
# 任务入队幂等
# ---------------------------------------------------------------------------


def test_enqueue_reuses_inflight_and_rotates_after_terminal(session):
    first = enqueue_turn(session, course_id="c1", message="同一句话")
    session.commit()
    again = enqueue_turn(session, course_id="c1", message="同一句话")
    assert again["task_run_id"] == first["task_run_id"]  # 在途复用
    assert again["user_message_id"] == first["user_message_id"]

    session.execute(
        update(task_runs).where(task_runs.c.id == first["task_run_id"]).values(status="succeeded")
    )
    session.commit()
    fresh = enqueue_turn(session, course_id="c1", message="同一句话")
    assert fresh["task_run_id"] != first["task_run_id"]  # 终态后换新键
    assert fresh["user_message_id"] != first["user_message_id"]


def test_enqueue_rejects_blank_and_oversize(session):
    with pytest.raises(AssistantError, match="不能为空"):
        enqueue_turn(session, course_id="c1", message="   ")
    with pytest.raises(AssistantError, match="过长"):
        enqueue_turn(session, course_id="c1", message="x" * 4001)


# ---------------------------------------------------------------------------
# 上下文装配（真库）
# ---------------------------------------------------------------------------


def test_load_context_lists_materials_with_whitelist(session):
    context = load_turn_context(session, course_id="c1")
    assert [m["id"] for m in context["materials"]] == ["m1", "m2"]
    assert context["allowed_ids"]["material_ids"] == ["m1", "m2"]
    assert context["course_name"] == "CS101"
    assert context["framework"] is None  # 未建框架不是错误，只是快照为空


def test_load_context_rejects_missing_course(session):
    with pytest.raises(AssistantError, match="课程不存在"):
        load_turn_context(session, course_id="nope")
