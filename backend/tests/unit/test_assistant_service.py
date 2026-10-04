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
    assessment_units,
    blueprint_versions,
    content_domains,
    exam_points,
    exam_projects,
    framework_versions,
    generated_questions,
    knowledge_cards,
    knowledge_catalog_versions,
    materials,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.domain.knowledge.relevance import StagingChunk
from app.services import assistant_service
from app.services.staging_retrieval_service import RankedChunk
from app.services.assistant_service import (
    AssistantError,
    MemoryTurnEventSink,
    REFUSED_REPLY,
    build_intent_prompt,
    build_proposal_payload,
    enqueue_turn,
    execute_read_tool,
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
                "stream": kwargs.get("stream", False),
                "tool": kwargs.get("tool"),
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


class ThinkingStubClient(StubClient):
    """思考模型桩：意图解析与正文流式都先出推理，再出正文。"""

    def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
        result = super().request_json(
            system_prompt=system_prompt,
            payload=payload,
            temperature=temperature,
            call_context=call_context,
            **kwargs,
        )
        on_think = kwargs.get("on_think")
        if on_think is not None:
            on_think("【意图推理】先看课程有没有试卷项目。")
        return result

    def stream_text(self, *, system_prompt, payload, temperature, on_delta, call_context, **kwargs):
        on_think = kwargs.get("on_think")
        if on_think is not None:
            on_think("【正文推理】组织一段简洁回答。")
        return super().stream_text(
            system_prompt=system_prompt,
            payload=payload,
            temperature=temperature,
            on_delta=on_delta,
            call_context=call_context,
            **kwargs,
        )


def test_turn_thinking_streamed_and_persisted_separate_from_content(session):
    """思考模型：推理走独立 think 事件 + thinking 列，正文通道与落库正文不含思考。"""
    client = ThinkingStubClient([_intent("这条不需要工具。")])
    task_id = _new_turn(session, "t-think")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "你是谁"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    events = [(e["event"], e["data"]) for e in sink.registry[task_id]]
    think_texts = "".join(d["text"] for ev, d in events if ev == "think")
    delta_texts = "".join(d["text"] for ev, d in events if ev == "delta")
    # 意图阶段（流式、经 think 缓冲聚批）与段2（流式聚批）的推理都进 think 通道
    assert "【意图推理】" in think_texts
    assert "【正文推理】" in think_texts
    assert client.calls[0]["stream"] is True  # 意图解析启用流式，等待期实时推思考
    # 正文通道绝不混入思考
    assert "【" not in delta_texts
    first_think = next(i for i, (ev, _) in enumerate(events) if ev == "think")
    first_delta = next(i for i, (ev, _) in enumerate(events) if ev == "delta")
    assert first_think < first_delta  # 意图推理先于正文增量
    assert events[-1][0] == "done"

    row = session.execute(
        select(assistant_messages.c.content, assistant_messages.c.thinking)
        .where(assistant_messages.c.task_run_id == task_id)
    ).one()
    assert row.content == "你好，这是流式回答。"  # 正式回复 = 纯正文
    assert "【意图推理】" in row.thinking
    assert "【正文推理】" in row.thinking

    # 视图带出 thinking，前端据此渲染独立思考区
    view = assistant_service.list_messages(session, course_id="c1")[-1]
    assert view["thinking"] == row.thinking
    assert view["content"] == row.content


def test_read_tool_returns_result_card_without_stream(session):
    client = StubClient([
        _intent("资料清单见下表：", {"tool": "course_overview", "args": {"section": "materials"}})
    ])
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
    # 执行契约 + 注册表下发的卡片元数据（label/impact/auto）：建课程是确认类
    assert row["payload"] == {
        "body": {"name": "新课程X"},
        "label": "新建课程",
        "impact": "将在课程空间新增一门课程；不影响当前课程的数据。",
        "auto": False,
    }
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


def test_empty_intent_retried_with_feedback(session):
    """模型输出退化 JSON（既无 reply 也无 action）→ 带反馈重试，不再静默降级成无卡聊天。

    线上根因：step-3.7-flash 两轮 reasoning 正确、content 通道却输出 `{}`/破损串，
    旧逻辑把空对象当合法纯问答放行 → 教师只收到口头承诺、没有任何提案卡。
    """
    client = StubClient([{}, _intent("好的，这就为你创建试卷项目。")])
    task_id = _new_turn(session, "t-empty")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "新建试卷项目"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    assert len(client.calls) == 2  # 第一次被拦下并带反馈重试
    assert "reply" in (client.calls[1]["previous_error"] or "")
    row = session.execute(
        select(assistant_messages.c.content).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()
    assert "流式回答" in row  # 纠错后的正文来自段2 流式，不是降级文案


def test_broken_json_twice_degrades_to_deterministic_text(session):
    """两次都退化 → 确定性失败文案兜底：可见的失败优于无声承诺。"""
    client = StubClient([{}, {"foo": "bar"}])
    task_id = _new_turn(session, "t-broken")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "继续"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    assert len(client.calls) == 2  # 只重试一次
    assert len(client.stream_calls) == 0  # 确定性文案不再进模型
    row = session.execute(
        select(assistant_messages.c.content).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()
    assert row.startswith("我没能处理这个请求")


def test_normalize_intent_rejects_degenerate_output():
    """_normalize_intent 层的空/破损输出守卫（run_turn 据此触发带反馈重试）。"""
    with pytest.raises(AssistantError, match="reply"):
        assistant_service._normalize_intent({})
    with pytest.raises(AssistantError, match="reply"):
        assistant_service._normalize_intent({"foo": "bar"})
    with pytest.raises(AssistantError, match="JSON 对象"):
        assistant_service._normalize_intent("not-a-dict")
    # 合法纯问答（有正文、无动作）仍然放行
    assert assistant_service._normalize_intent({"reply": "你好"}) == {
        "reply": "你好",
        "action": None,
    }


def test_normalize_intent_accepts_content_alias():
    """模型漏调工具、退回 content 通道时按 history 形状用 content 当正文（线上 21:53 实测）。"""
    assert assistant_service._normalize_intent({"content": "好的"}) == {
        "reply": "好的",
        "action": None,
    }


def test_intent_sent_through_function_calling_tool(session):
    """段1 用 function calling 收口：下发 submit_reply 工具，其动作枚举与白名单同源。"""
    client = StubClient([
        _intent("这是你的试卷项目列表：", {"tool": "course_overview", "args": {"section": "projects"}})
    ])
    task_id = _new_turn(session, "t-tool")
    run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "我有哪些试卷项目"},
        client=client,
        sink=MemoryTurnEventSink(task_id),
    )

    call = client.calls[0]
    assert call["tool"] is assistant_service._INTENT_TOOL
    assert call["stream"] is True  # 工具通道仍走流式，思考增量照常实时推
    params = call["tool"]["parameters"]
    assert params["required"] == ["reply"]
    enum = params["properties"]["action"]["properties"]["tool"]["enum"]
    assert set(enum) == (
        set(assistant_service.READ_TOOLS)
        | set(assistant_service.PROPOSAL_TOOLS)
        | {assistant_service.RAG_TOOL}
    )
    # 拒绝清单不给出题口：模型不可能"发明"被禁工具名
    assert not set(assistant_service.REFUSED_TOOLS) & set(enum)


def test_normalize_intent_accepts_unwrapped_action():
    """模型漏掉 {reply, action} 外层包装、直接吐 action 形状 → 确定性归一（线上 21:26/21:27 实测）。"""
    assert assistant_service._normalize_intent(
        {"tool": "create_exam_project", "args": {"name": "新卷"}}
    ) == {"reply": "", "action": {"tool": "create_exam_project", "args": {"name": "新卷"}}}
    # 归一后仍走同一套校验：tool 空 / args 非法照旧报错
    with pytest.raises(AssistantError, match="action.tool"):
        assistant_service._normalize_intent({"tool": "", "args": {}})
    with pytest.raises(AssistantError, match="action.args"):
        assistant_service._normalize_intent({"tool": "start_parse", "args": "bad"})


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


def test_create_course_accepts_valid_category_and_rejects_unknown():
    ctx = _ctx()
    ok = build_proposal_payload(
        "create_course", {"name": "分布式系统", "category": "computer"}, context=ctx
    )
    assert ok["body"]["category"] == "computer"
    # 未知类别走带反馈重试（不静默落默认，让模型改用 payload.course_categories 的 key）
    with pytest.raises(AssistantError, match="未知课程类别"):
        build_proposal_payload("create_course", {"name": "x", "category": "bogus"}, context=ctx)


def test_update_question_type_format_payload_contract():
    ctx = _ctx(framework={"exam_rules": {"type_formats": {"fill_blank": "旧格式"}}})
    payload = build_proposal_payload(
        "update_question_type_format",
        {"question_type": "填空题", "template": "新的填空格式"},
        context=ctx,
    )
    # body 即执行体：前端确认后原样 PATCH /rules/type-formats
    assert payload["body"] == {"question_type": "fill_blank", "template": "新的填空格式"}
    assert payload["current"] == "旧格式"  # 卡片对比展示现值
    # 恢复默认 = 显式空串；现值取自上下文（未设置过 = 空）
    restore = build_proposal_payload(
        "update_question_type_format", {"question_type": "essay", "template": ""},
        context=_ctx(),
    )
    assert restore["body"] == {"question_type": "essay", "template": ""}
    assert restore["current"] == ""
    # 综合题原型 key 同表可覆盖（键=原型名，与标准题型同一 PATCH 执行体）
    comp = build_proposal_payload(
        "update_question_type_format",
        {"question_type": "code_completion_scenario", "template": "新的代码补全任务卡"},
        context=ctx,
    )
    assert comp["body"] == {
        "question_type": "code_completion_scenario",
        "template": "新的代码补全任务卡",
    }


def test_update_question_type_format_rejects_bad_args():
    ctx = _ctx()
    # 裸 comprehensive 不是任务卡键（可覆盖的是 8 个原型 key，见上一用例）
    with pytest.raises(AssistantError, match="未知题型"):
        build_proposal_payload(
            "update_question_type_format",
            {"question_type": "comprehensive", "template": "x"},
            context=ctx,
        )
    with pytest.raises(AssistantError, match="未知题型"):
        build_proposal_payload(
            "update_question_type_format",
            {"question_type": "变态题", "template": "x"},
            context=ctx,
        )
    # template 整键缺失 ≠ 恢复默认（必须显式空串）
    with pytest.raises(AssistantError, match="template"):
        build_proposal_payload(
            "update_question_type_format", {"question_type": "essay"}, context=ctx
        )
    with pytest.raises(AssistantError, match="超长"):
        build_proposal_payload(
            "update_question_type_format",
            {"question_type": "essay", "template": "x" * 2001},
            context=ctx,
        )


def test_route_update_question_type_format_as_proposal():
    routed = route_intent(
        _intent("", {"tool": "update_question_type_format",
                     "args": {"question_type": "fill_blank", "template": "改后的格式"}}),
        session=None, context=_ctx(),
    )
    assert routed["kind"] == "proposal"
    # 出卷主线上的提案自动执行（status=auto）；不需教师点确认
    assert routed["action"]["status"] == "auto"
    assert routed["payload"]["auto"] is True
    assert routed["payload"]["label"] == "修改题型格式"
    assert routed["payload"]["body"]["question_type"] == "fill_blank"
    assert "提案" in routed["reply"]
    assert "update_question_type_format" in assistant_service.PROPOSAL_TOOLS


# ---------------------------------------------------------------------------
# 出卷主线提案（创建项目/考核规则/蓝图/蓝图确认/发起生成）
# ---------------------------------------------------------------------------


def test_paper_pipeline_tools_whitelisted_and_unrefused():
    for tool in (
        "create_exam_project",
        "update_exam_rules",
        "create_blueprint",
        "confirm_blueprint",
        "start_generation",
        "enqueue_paper_review",
    ):
        assert tool in assistant_service.PROPOSAL_TOOLS
        assert tool not in assistant_service.REFUSED_TOOLS
    # 定稿/导出仍是里程碑拒绝项
    assert "finalize_paper" in assistant_service.REFUSED_TOOLS
    assert "export_paper" in assistant_service.REFUSED_TOOLS


def _paper_ctx(**overrides) -> dict:
    """有框架+已发布目录+单项目（蓝图草稿/合同未确认）的上下文。"""
    framework = {
        "version_no": 1,
        "status": "confirmed",
        "exam_rules": {
            "exam_form": "闭卷",
            "duration_minutes": 120,
            "total_score": 100,
            "question_type_ratios": [
                {"question_type": "single_choice", "ratio": 40},
                {"question_type": "comprehensive", "ratio": 20},
            ],
            "chapter_weights": [{"anchor_key": "ch1", "weight": 100}],
            "assessment_focus": [{"assessment_mode": "conceptual", "weight": 60}],
            "type_formats": {},
        },
    }
    base = _ctx(
        framework=framework,
        catalog={"version_no": 1, "status": "published"},
        projects=[
            {
                "id": "p1",
                "name": "期末卷",
                "blueprint": {"status": "draft", "confirmed": False},
                "contract": {"exists": True, "confirmed": False},
                "paper": {"exists": False},
            }
        ],
    )
    base.update(overrides)
    return base


def test_create_exam_project_payload_and_guards():
    payload = build_proposal_payload(
        "create_exam_project", {"name": "期末考试卷"}, context=_ctx()
    )
    assert payload == {"body": {"name": "期末考试卷"}}
    with pytest.raises(AssistantError, match="name"):
        build_proposal_payload("create_exam_project", {"name": "   "}, context=_ctx())


def test_update_exam_rules_merges_untouched_current_values():
    focus = [
        {"assessment_mode": "theory_recall", "weight": 50},
        {"assessment_mode": "conceptual", "weight": 30},
        {"assessment_mode": "application", "weight": 20},
    ]
    payload = build_proposal_payload(
        "update_exam_rules", {"assessment_focus": focus}, context=_paper_ctx()
    )
    body = payload["body"]
    # 只改 assessment_focus：题型比例/章节权重按快照现值合并（整份替换不抹现值）
    assert body["question_type_ratios"] == [
        {"question_type": "single_choice", "ratio": 40},
        {"question_type": "comprehensive", "ratio": 20},
    ]
    assert body["chapter_weights"] == [{"anchor_key": "ch1", "weight": 100}]
    assert body["assessment_focus"][0] == {"assessment_mode": "theory_recall", "weight": 50.0}
    # before 供提案卡做「现值 → 新值」对比
    assert payload["before"]["assessment_focus"] == [
        {"assessment_mode": "conceptual", "weight": 60}
    ]


def test_update_exam_rules_rejects_bad_args():
    with pytest.raises(AssistantError, match="命题框架"):
        build_proposal_payload(
            "update_exam_rules",
            {"assessment_focus": [{"assessment_mode": "theory_recall", "weight": 1}]},
            context=_ctx(),  # framework=None
        )
    with pytest.raises(AssistantError, match="未知考查方式"):
        build_proposal_payload(
            "update_exam_rules",
            {"assessment_focus": [{"assessment_mode": "brain_memory", "weight": 1}]},
            context=_paper_ctx(),
        )
    with pytest.raises(AssistantError, match="未知题型"):
        build_proposal_payload(
            "update_exam_rules",
            {"question_type_ratios": [{"question_type": "变态题", "ratio": 10}]},
            context=_paper_ctx(),
        )
    with pytest.raises(AssistantError, match="至少给一个"):
        build_proposal_payload("update_exam_rules", {}, context=_paper_ctx())


def test_create_blueprint_guards_and_archetypes_pool():
    with pytest.raises(AssistantError, match="知识目录"):
        build_proposal_payload(
            "create_blueprint", {"project_id": "p1"}, context=_paper_ctx(catalog=None)
        )
    with pytest.raises(AssistantError, match="命题框架"):
        build_proposal_payload(
            "create_blueprint", {"project_id": "p1"}, context=_paper_ctx(framework=None)
        )
    payload = build_proposal_payload(
        "create_blueprint",
        {
            "project_id": "p1",
            "comprehensive_archetypes": [
                "case_analysis",
                "solution_design",
                "case_analysis",  # 重复保留：重复=数量（教师要 N 道同类原型）
            ],
        },
        context=_paper_ctx(),
    )
    assert payload["project_id"] == "p1"
    assert payload["body"] == {
        "comprehensive_archetypes": ["case_analysis", "solution_design", "case_analysis"]
    }
    # basis：生成依据（考核规则现值）——提案卡展示「蓝图将按什么确定性生成」
    assert payload["basis"]["question_type_ratios"] == [
        {"question_type": "single_choice", "ratio": 40},
        {"question_type": "comprehensive", "ratio": 20},
    ]
    assert payload["basis"]["assessment_focus"] == [
        {"assessment_mode": "conceptual", "weight": 60}
    ]
    assert payload["basis"]["chapter_weights"] == [{"anchor_key": "ch1", "weight": 100}]
    # 不带原型池 = 走默认轮换池，body 为空，basis 照带
    plain = build_proposal_payload(
        "create_blueprint", {"project_id": "p1"}, context=_paper_ctx()
    )
    assert plain["body"] == {}
    assert plain["basis"] == payload["basis"]
    with pytest.raises(AssistantError, match="未知综合题原型"):
        build_proposal_payload(
            "create_blueprint",
            {"project_id": "p1", "comprehensive_archetypes": ["code_xxx"]},
            context=_paper_ctx(),
        )
    # 比例已声明却没综合题：蓝图不会出综合题 → 先改比例，不创建后报错
    ctx = _paper_ctx()
    ctx["framework"]["exam_rules"]["question_type_ratios"] = [
        {"question_type": "single_choice", "ratio": 100}
    ]
    with pytest.raises(AssistantError, match="未包含综合题"):
        build_proposal_payload(
            "create_blueprint",
            {"project_id": "p1", "comprehensive_archetypes": ["case_analysis"]},
            context=ctx,
        )


def test_confirm_blueprint_guards():
    ctx = _paper_ctx()
    ctx["projects"][0]["blueprint"] = None
    with pytest.raises(AssistantError, match="还没有蓝图"):
        build_proposal_payload("confirm_blueprint", {"project_id": "p1"}, context=ctx)

    ctx = _paper_ctx()
    ctx["projects"][0]["blueprint"] = {"status": "confirmed", "confirmed": True}
    with pytest.raises(AssistantError, match="已确认"):
        build_proposal_payload("confirm_blueprint", {"project_id": "p1"}, context=ctx)

    payload = build_proposal_payload(
        "confirm_blueprint", {"project_id": "p1"}, context=_paper_ctx()
    )
    assert payload["project_name"] == "期末卷"


def test_start_generation_requires_confirmed_contract():
    with pytest.raises(AssistantError, match="合同尚未确认"):
        build_proposal_payload(
            "start_generation", {"project_id": "p1"}, context=_paper_ctx()
        )
    ctx = _paper_ctx()
    ctx["projects"][0]["contract"]["confirmed"] = True
    payload = build_proposal_payload(
        "start_generation", {"project_id": "p1"}, context=ctx
    )
    assert payload["project_id"] == "p1"
    assert payload["project_name"] == "期末卷"


def _seed_blueprint_chain(session) -> None:
    """种子：框架/目录/考点/单元/知识卡 + 项目 p1 + 蓝图 v1 + 2 个题位。"""
    session.execute(framework_versions.insert().values(
        id="fv1", course_id="c1", version_no=1, status="published",
        payload={"anchors": [{"key": "A1", "title": "第1章 绪论"}]},
    ))
    session.execute(knowledge_catalog_versions.insert().values(
        id="cv1", course_id="c1", framework_version_id="fv1",
        version_no=1, status="published",
    ))
    session.execute(exam_points.insert().values(
        id="ep1", course_id="c1", framework_version_id="fv1", anchor_key="A1",
        code="EP1", title="考点1", assessment_requirement="掌握A", weight_value=30.0,
        weight_source="teacher_confirmed", weight_group_id="A1", priority="normal",
        cognitive_targets=[], assessment_orientations=[],
        allowed_question_types=["single_choice"],
        operational_detail_policy="supporting_only", scope_boundary={},
        required_evidence_roles=[], retrieval_intent="围绕A检索",
        teaching_anchor_keys=[], status="active",
    ))
    session.execute(content_domains.insert().values(
        id="cd1", course_id="c1", catalog_version_id="cv1", parent_domain_id=None,
        level=1, framework_anchor_key="A1", code="A1", name="章1", status="active",
    ))
    session.execute(assessment_units.insert().values(
        id="au1", course_id="c1", catalog_version_id="cv1", content_domain_id="cd1",
        exam_point_id="ep1", code="U1", title="单元1", performance_statement="ps1",
        weight=30, status="active",
    ))
    session.execute(knowledge_cards.insert().values(
        id="kc1", course_id="c1", catalog_version_id="cv1", assessment_unit_id="au1",
        name="卡A1", performance_statement="掌握A1",
        assessable_content=["A1-原子1定义"], content_hash="hca1",
        status="active", concept_cluster="A", answer_proposition="A1-边界",
    ))
    # 先建项目再建蓝图：active_blueprint_version_id 是同课程复合 FK
    session.execute(exam_projects.insert().values(
        id="p1", course_id="c1", name="期末卷", status="draft",
    ))
    session.execute(blueprint_versions.insert().values(
        id="bv1", course_id="c1", exam_project_id="p1",
        framework_version_id="fv1", catalog_version_id="cv1", version_no=1,
        status="draft", type_rules={}, chapter_weights={},
    ))
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == "p1")
        .values(active_blueprint_version_id="bv1")
    )
    for idx, qtype, score, difficulty in (
        (1, "single_choice", 6.0, "low"),
        (2, "short_answer", 4.0, "medium"),
    ):
        session.execute(plan_items.insert().values(
            id=f"pi{idx}", course_id="c1", blueprint_version_id="bv1",
            assessment_unit_id="au1", question_type=qtype, item_index=idx,
            score=score, difficulty=difficulty, cognitive_level="understand",
            assessment_mode="conceptual", exam_point_id="ep1",
            knowledge_card_id="kc1",
        ))
    session.commit()


def test_proposal_preview_carries_blueprint_plan(session):
    """确认蓝图/合同提案附题位计划实物预览：教师在气泡里看到「确认的到底是什么」。"""
    _seed_blueprint_chain(session)
    ctx = _paper_ctx()

    routed = route_intent(
        {"reply": "", "action": {"tool": "confirm_blueprint", "args": {"project_id": "p1"}}},
        session=session,
        context=ctx,
        message="确认蓝图",
    )
    preview = routed["payload"]["preview"]
    assert preview["source"] == "blueprint"
    assert preview["version_no"] == 1
    assert preview["item_count"] == 2
    assert preview["total_score"] == 10.0
    assert preview["difficulty"] == {"low": 1, "medium": 1}
    first = preview["items"][0]
    assert first["item_index"] == 1
    assert first["question_type"] == "single_choice"
    assert first["assessment_mode"] == "conceptual"
    assert first["exam_point"] == "考点1"
    assert first["knowledge_card"] == "卡A1"

    # 合同确认前尚无快照：预览展示确定性分配将按此锁定的题位基础
    routed_contract = route_intent(
        {"reply": "", "action": {"tool": "confirm_contract", "args": {"project_id": "p1"}}},
        session=session,
        context=ctx,
        message="确认合同",
    )
    assert routed_contract["payload"]["preview"]["item_count"] == 2

    # 非确认类提案不带预览（payload 形状不受影响）
    routed_create = route_intent(
        {"reply": "", "action": {"tool": "create_exam_project", "args": {"name": "新卷"}}},
        session=session,
        context=_ctx(),
        message="新建项目",
    )
    assert "preview" not in routed_create["payload"]


def test_proposal_preview_start_generation_falls_back_to_blueprint(session):
    """发起生成时合同已确认（理论上有槽位快照）；本种子无 generation run → 回退蓝图明细。"""
    _seed_blueprint_chain(session)
    ctx = _paper_ctx()
    ctx["projects"][0]["contract"]["confirmed"] = True

    routed = route_intent(
        {"reply": "", "action": {"tool": "start_generation", "args": {"project_id": "p1"}}},
        session=session,
        context=ctx,
        message="开始生成",
    )
    preview = routed["payload"]["preview"]
    assert preview["source"] == "blueprint"
    assert preview["item_count"] == 2
    assert preview["items"][1]["knowledge_card"] == "卡A1"


def test_enqueue_paper_review_payload_and_guards():
    """整卷评审提案：有试卷才放行，目标卷取自 paper 快照（active 优先解析）。"""
    ctx = _paper_ctx()
    ctx["projects"][0]["paper"] = {
        "exists": True,
        "paper_version_id": "pv9",
        "version_no": 3,
        "status": "candidate",
        "needs_review_count": 2,
    }
    payload = build_proposal_payload(
        "enqueue_paper_review",
        {"project_id": "p1", "instruction": "重点看填空题答案唯一性"},
        context=ctx,
    )
    assert payload["paper_version_id"] == "pv9"
    assert payload["paper_version_no"] == 3
    assert payload["project_name"] == "期末卷"
    assert payload["body"] == {"instruction": "重点看填空题答案唯一性"}
    # instruction 可省略 = 常规评审
    assert build_proposal_payload(
        "enqueue_paper_review", {"project_id": "p1"}, context=ctx
    )["body"] == {"instruction": ""}
    # 没有试卷 → 拦下并引导先走生成
    with pytest.raises(AssistantError, match="还没有试卷"):
        build_proposal_payload(
            "enqueue_paper_review", {"project_id": "p1"}, context=_paper_ctx()
        )
    # 白名单外项目 → 拒绝
    with pytest.raises(AssistantError, match="白名单"):
        build_proposal_payload(
            "enqueue_paper_review", {"project_id": "evil"}, context=ctx
        )


def test_prompt_documents_paper_review_tool():
    """整卷 AI 评审接入助手：工具说明（注册表渲染）、质量检查落点与停点例外齐备。"""
    system_prompt, _payload = build_intent_prompt(_paper_ctx(), "帮我检查一下试卷")
    assert "enqueue_paper_review" in system_prompt
    # 卡片开场白有专属文案（非通用兜底）
    assert "整卷 AI 评审任务的发起提案" in (
        assistant_service._DEFAULT_PROPOSAL_REPLIES["enqueue_paper_review"]
    )
    # 落点在注册表里跟着工具走（不再是一段手写的「要求落点」散文）
    assert "检查试卷" in system_prompt
    assert "前提：该项目已有试卷" in system_prompt
    assert "不要重复发起" in system_prompt
    # review/exported 停点放行评审卡（停点只拦出卷主线推进）
    assert "① 教师明确要求检查/评审试卷" in system_prompt


def test_prompt_documents_paper_pipeline_ladder():
    """prompt 装配出卷主线阶梯与五个新工具；蓝图确认/发起生成移出拒绝话术。"""
    system_prompt, _payload = build_intent_prompt(_paper_ctx(), "我需要出一张试卷")
    for tool in (
        "create_exam_project",
        "update_exam_rules",
        "create_blueprint",
        "confirm_blueprint",
        "start_generation",
    ):
        assert tool in system_prompt
    assert "出卷主线推进" in system_prompt
    # 接力交互：执行成功后由系统内部指令推进（不是教师说的话，不落用户消息），
    # 助手不再要求教师手动打字
    assert "内部的「继续」指令" in system_prompt
    assert "不是教师说的话" in system_prompt
    assert "要求教师手动输入" in system_prompt
    # 发起生成后不再追问：等生成结束由系统直接发完成通报
    assert "生成结束后由系统直接发一条完成通报" in system_prompt
    assert "请教师确认卡片后回复" not in system_prompt
    # 自动化：出卷主线上的提案由前端自动执行，只有确认类卡片等教师点确认
    assert "其余提案卡片由前端自动执行并回报执" in system_prompt
    assert "建议由前端自动应用" in system_prompt
    # 每一步只推进一级：历史走过的步骤不重复发起（防建议环节打转）
    assert "每一步只推进一级" in system_prompt
    # 接力停点只剩两个：主线完成引导审核、生成中不重复发起；难度建议由前端
    # 自动应用，不再有「建议未应用」这个需要教师跳页的停点
    assert "全部应用" not in system_prompt
    assert "status=generating" in system_prompt
    # 停点优先级：先按项目 status 判定、命中即停，防主线走完后被后续停点误拦
    assert "先按项目 status 判定，命中即停、不再往下看" in system_prompt
    assert system_prompt.index("status=review 或 exported") < system_prompt.index(
        "status=generating 且 generation_task_status"
    )
    # review 引导给固定话术：不列导出格式、不摆选项让教师点单（曾回「导出
    # 试卷（Word/PDF 等格式）/发布/查看详情」三选一，格式与顺序均属臆造）
    assert "请到『试卷』页审核编辑，定稿与导出也在该页完成" in system_prompt
    assert "不列举导出格式" in system_prompt
    # 指令粒度保真：「每个题型」级限定词原样保留，防丢词改变逐题型/整卷换算口径
    assert "每个题型" in system_prompt
    # 旧拒绝文案（蓝图确认一并拒绝）不再出现；定稿/导出仍拒绝
    assert "蓝图确认、试卷定稿、导出" not in system_prompt
    assert "试卷定稿、导出" in system_prompt
    # 教师的难度比例说法要进蓝图建议指令
    assert "5简单3中等2难" in system_prompt
    # 新卷分支：已有 review 项目时「另出一份」也从 create_exam_project 起步，
    # 不被阶梯第1步的独占前提（没有试卷项目）与停点1双重封死（曾在 langchain课
    # 程上振荡后回「是否现在发起？」的口头承诺、action=null 没有下一步）
    assert "或教师点名要另出一份新卷" in system_prompt
    assert "② 教师明确要另出一份新卷" in system_prompt
    assert "不受本项目 review 状态牵连" in system_prompt
    # 卡片即执行：禁止「是否现在发起」式口头征求；带要求开新卷不塞第一张卡
    assert "先反问「是否现在发起」" in system_prompt
    assert "出卷主线上的写操作卡片由前端自动执行" in system_prompt
    assert "不要**塞进本卡 args" in system_prompt
    # 倾向型难度说法（非数字比例）如实进建议指令，不承诺确定性换算；
    # 「难度偏中等」这类倾向说法的落点写在注册表的 instruction 参数说明里
    assert "难度偏中等" in system_prompt
    # 生成阶段双读：status=generating 只是合同确认时置上的阶段标记，真在跑看
    # generation_task_status——曾在合同确认后、生成未发起的窗口被停点2 当成
    # 「任务在跑」拦死，start_generation 永远发不出（前端只见徽章「生成中」而无任务）
    assert "从未发起生成" in system_prompt
    assert "generation_task_status 为 null" in system_prompt
    assert "**不是停点**" in system_prompt
    assert "generation_task_status 为 queued/running" in system_prompt
    # 生题题型可控：综合题原型按序可重复=数量（两道代码题=写两次）——
    # 这条口径现在写在注册表的 comprehensive_archetypes 参数说明里
    assert "可重复，重复即数量" in system_prompt
    assert "code_completion_scenario 写两次" in system_prompt
    # 原型 key 可覆盖任务卡、裸 comprehensive 不收
    assert "不接受裸 comprehensive" in system_prompt
    # 红线不回退：助手不换算不承诺
    assert "比例/难度/去重" in system_prompt


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
    # 点名查询的定位规则必须写进提示（否则卡片永远是全课程范围）
    assert "结果卡只呈现该项目" in system_prompt

    _sys2, payload2 = build_intent_prompt(context, "重试", previous_error="material_id 非法")
    assert payload2["previous_validation_error"] == "material_id 非法"


def test_intent_prompt_carries_categories_and_format_tool():
    """create_course 的 category 取值来源 + 题型格式工具的文档必须进段1提示。"""
    system_prompt, payload = build_intent_prompt(_ctx(), "把填空题格式改成两空")
    keys = {item["key"] for item in payload["course_categories"]}
    assert {"general", "computer", "humanities"} <= keys
    assert "update_question_type_format" in system_prompt
    assert "category" in system_prompt
    # 已设置的题型格式进快照（模型据此对比「现格式 → 新格式」）
    _sys, snapshot_payload = build_intent_prompt(
        _ctx(framework={"exam_rules": {"type_formats": {"fill_blank": "旧格式"}}}), "现在什么格式"
    )
    assert snapshot_payload["snapshot"]["framework"]["exam_rules"]["type_formats"] == {"fill_blank": "旧格式"}


def test_intent_prompt_documents_capability_map_and_usage_guide():
    """能力地图/出卷主线/助手边界必须进段1；询问用法的路由规则指向 usage_guide。"""
    system_prompt, _payload = build_intent_prompt(_ctx(), "这个网站怎么用")

    assert "usage_guide" in system_prompt
    assert "产品能力地图" in system_prompt
    assert "出卷主线" in system_prompt
    # 边界：在线考试与阅卷明确排除
    assert "在线考试与阅卷不在本系统范围内" in system_prompt
    # 询问「下一步做什么」→ 引导卡，而非 action 置 null；回复依据是 snapshot（单次调用时工具尚未执行）
    assert "下一步做什么" in system_prompt
    assert "payload.snapshot" in system_prompt


def test_usage_guide_route_returns_steps_and_navigation():
    """usage_guide 走只读路由 → 引导卡：六步主线 + 页面导航 + 当前步骤定位。"""
    routed = route_intent(
        _intent("", {"tool": "usage_guide", "args": {}}), session=None, context=_ctx()
    )

    assert routed["kind"] == "result"
    assert routed["action"]["kind"] == "result"
    assert routed["action"]["tool"] == "usage_guide"
    steps = routed["payload"]["steps"]
    assert [s["key"] for s in steps] == [
        "materials",
        "framework",
        "knowledge",
        "blueprint",
        "contract_generate",
        "review_export",
    ]
    # 空进度上下文 → 从第一步开始
    assert routed["payload"]["current_step"] == "materials"
    assert steps[0]["status"] == "current"
    assert all(s["status"] == "todo" for s in steps[1:])
    # 每步都带可跳转的页面锚点；页面导航覆盖五个模块
    assert {s["nav"] for s in steps} <= {"materials", "framework", "knowledge", "paper"}
    assert {"课程概览", "资料库", "命题框架", "知识目录", "试卷"} <= {
        p["label"] for p in routed["payload"]["pages"]
    }
    # 模型缺省回复也已登记
    assert "出卷全流程见下卡" in routed["reply"]


def test_usage_guide_status_derives_current_step():
    """完成信号按前缀推导：就绪到蓝图 → 当前步=确认合同并生成；全完成 → current_step None。"""
    ready_materials = [
        {
            "id": "m1",
            "name": "教学大纲",
            "type": "teaching_syllabus",
            "status": "staged",
            "parse_status": "ready",
        },
    ]
    confirmed_project = {
        "id": "p1",
        "name": "期末卷",
        "status": "contract",
        "blueprint": {
            "blueprint_version_id": "bp1",
            "version_no": 1,
            "status": "confirmed",
            "confirmed": True,
            "item_count": 5,
            "by_type": {},
        },
        "contract": {"exists": True, "confirmed": True, "slot_count": 5},
        "paper": {"exists": False},
    }
    mid = execute_read_tool(
        None,
        context=_ctx(
            materials=ready_materials,
            framework={"version_no": 1, "status": "published"},
            catalog={"version_no": 1, "status": "published"},
            projects=[confirmed_project],
        ),
        tool="usage_guide",
    )
    assert {s["key"]: s["status"] for s in mid["steps"]} == {
        "materials": "done",
        "framework": "done",
        "knowledge": "done",
        "blueprint": "done",
        "contract_generate": "current",
        "review_export": "todo",
    }
    assert mid["current_step"] == "contract_generate"

    done = execute_read_tool(
        None,
        context=_ctx(
            materials=ready_materials,
            framework={"version_no": 1, "status": "published"},
            catalog={"version_no": 1, "status": "published"},
            projects=[
                {
                    **confirmed_project,
                    "status": "exported",
                    "paper": {
                        "exists": True,
                        "paper_version_id": "pv1",
                        "version_no": 1,
                        "status": "finalized",
                        "needs_review_count": 0,
                    },
                }
            ],
        ),
        tool="usage_guide",
    )
    assert done["current_step"] is None
    assert all(s["status"] == "done" for s in done["steps"])


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
# 只读工具的点名定位（项目级过滤，卡片不再罗列教师没问的项目）
# ---------------------------------------------------------------------------


def _project_ctx() -> dict:
    """含两个试卷项目的手工上下文（与 _ctx 同构，仅 projects/白名单不同）。"""
    return _ctx(
        projects=[
            {
                "id": "p1",
                "name": "学期2",
                "status": "active",
                "blueprint": {
                    "blueprint_version_id": "bp1", "version_no": 1, "status": "candidate",
                    "confirmed": False, "item_count": 3, "by_type": {},
                },
                "contract": {"exists": False, "confirmed": False},
                "paper": {"exists": True, "paper_version_id": "pv1", "version_no": 1,
                          "status": "candidate", "needs_review_count": 0},
            },
            {
                "id": "p2",
                "name": "测试卷",
                "status": "active",
                "blueprint": None,
                "contract": {"exists": False, "confirmed": False},
                "paper": {"exists": False},
            },
        ],
        allowed_ids={"material_ids": ["m1", "m2"], "project_ids": ["p1", "p2"]},
    )


def test_read_tool_targets_named_project():
    """教师点名项目：结果卡只含该项目，不再罗列其它项目。"""
    routed = route_intent(
        _intent(
            "学期2的试卷情况见下表：",
            {"tool": "course_overview", "args": {"section": "paper", "project_id": "p1"}},
        ),
        session=None,
        context=_project_ctx(),
    )
    assert routed["kind"] == "result"
    assert routed["payload"]["section"] == "paper"  # 结果卡据此选视图
    assert [p["id"] for p in routed["payload"]["projects"]] == ["p1"]


def test_read_tool_without_target_lists_all_projects():
    routed = route_intent(
        _intent("", {"tool": "course_overview", "args": {"section": "projects"}}),
        session=None,
        context=_project_ctx(),
    )
    assert [p["id"] for p in routed["payload"]["projects"]] == ["p1", "p2"]


def test_read_tool_rejects_unknown_section():
    """合并后的查询工具按 section 选视图：非法 section 走带反馈重试。"""
    with pytest.raises(AssistantError, match="未知 section"):
        route_intent(
            _intent("", {"tool": "course_overview", "args": {"section": "evil"}}),
            session=None,
            context=_project_ctx(),
        )


def test_read_tool_course_overview_targets_project():
    routed = route_intent(
        _intent("", {"tool": "course_overview", "args": {"project_id": "p2"}}),
        session=None,
        context=_project_ctx(),
    )
    assert [p["id"] for p in routed["payload"]["projects"]] == ["p2"]
    assert routed["payload"]["course_name"] == "CS101"  # 课程级字段不受项目过滤影响


def test_read_tool_rejects_foreign_project_id():
    """点名参数与提案同一套 id 白名单：非法 id → AssistantError（上层带反馈重试一次）。"""
    with pytest.raises(AssistantError, match="白名单"):
        route_intent(
            _intent("", {"tool": "course_overview", "args": {"project_id": "p-evil"}}),
            session=None,
            context=_project_ctx(),
        )


# ---------------------------------------------------------------------------
# 资料内容问答（RAG）：意图路由、白名单/解析状态校验、检索模式与来源卡 payload
# ---------------------------------------------------------------------------


def _rag_chunks(*, with_embeddings: bool = False) -> list:
    from app.domain.knowledge.relevance import StagingChunk

    chunks = [
        StagingChunk(
            id="blk-1",
            material_version_id="v1",
            content="监督学习的分类与回归是两种基本任务。",
            locator={
                "material_id": "m1",
                "material_name": "教学大纲",
                "page_index": 2,
                "heading_path": ["第3章", "3.1 监督学习"],
            },
            embedding=[1.0, 0.0] if with_embeddings else None,
        ),
        StagingChunk(
            id="blk-2",
            material_version_id="v1",
            content="天气晴朗。",
            locator={
                "material_id": "m1",
                "material_name": "教学大纲",
                "page_index": 9,
                "heading_path": [],
            },
            embedding=[0.0, 1.0] if with_embeddings else None,
        ),
    ]
    return chunks


def _rag_ctx(**overrides) -> dict:
    materials = [
        {"id": "m1", "name": "教学大纲", "type": "teaching_syllabus", "status": "staged", "parse_status": "ready"},
        {"id": "m2", "name": "第3章讲义", "type": "teaching_material", "status": "staged", "parse_status": None},
    ]
    return _ctx(materials=materials, **overrides)


def _patch_rag(monkeypatch, chunks=None, *, configured: bool = False):
    """隔离 DB 与嵌入配置：记录 ensure/load 调用并返回预置语料。"""
    calls = {"ensure": [], "load": []}

    def fake_ensure(session, *, course_id, material_ids=None, run_ids=None):
        calls["ensure"].append((course_id, material_ids))
        return 0

    def fake_load(session, *, course_id, material_ids=None, include_embedding=True):
        calls["load"].append((course_id, material_ids, include_embedding))
        return _rag_chunks() if chunks is None else chunks

    monkeypatch.setattr(assistant_service, "ensure_embedded", fake_ensure)
    monkeypatch.setattr(assistant_service, "load_content_chunks", fake_load)
    monkeypatch.setattr(assistant_service, "embedding_configured", lambda: configured)
    # 夹具无真实库：默认不走 PG 下推（下推专项用例再覆盖为 True）
    monkeypatch.setattr(assistant_service, "supports_semantic_pushdown", lambda session: False)
    return calls


def test_rag_prompt_documents_tool_and_grounding_rules():
    """段1 prompt 有工具说明与解析状态约束；拒绝表第 6 条不再禁问答；红线不入 prompt。"""
    system_prompt, _payload = build_intent_prompt(_rag_ctx(), "总结这份资料")

    assert "answer_material_content" in system_prompt
    assert 'parse_status=="ready"' in system_prompt
    assert "material_id" in system_prompt
    # 全文照抄仍拒绝（第 6 条改写）
    assert "原样输出/朗读整份资料全文" in system_prompt
    assert "暂不支持" not in system_prompt
    # 既有红线不回退
    assert "比例/难度/去重" in system_prompt

    # 双保险集合：问答工具移出 REFUSED，全文照抄仍被拒
    assert assistant_service.RAG_TOOL not in assistant_service.REFUSED_TOOLS
    assert "read_material_content" in assistant_service.REFUSED_TOOLS

    # 段2 grounding 规则
    rag_prompt = assistant_service._RAG_ANSWER_SYSTEM_PROMPT
    assert "只依据下方「资料片段」作答" in rag_prompt
    assert "没有找到" in rag_prompt
    assert "比例/难度/去重" in rag_prompt


def test_rag_query_variants_strip_frames_and_leading_verbs():
    """确定性查询改写：原问题恒为首变体；削问句框架与句首泛化动词得主题核心。"""
    assert assistant_service._rag_query_variants("总结教学大纲讲了什么？") == [
        "总结教学大纲讲了什么？",
        "教学大纲",
    ]
    assert assistant_service._rag_query_variants("请介绍混淆矩阵") == [
        "请介绍混淆矩阵",
        "混淆矩阵",
    ]
    assert assistant_service._rag_query_variants("SK3020是什么") == ["SK3020是什么", "SK3020"]
    # 无问句框架可削 → 单变体（多查询自动退化为既有单查询行为）
    assert assistant_service._rag_query_variants("数据归一化的方法") == ["数据归一化的方法"]
    # 削空后不追加空串
    assert assistant_service._rag_query_variants("总结一下") == ["总结一下"]


def _rag_chunk(cid: str, page: int | None, content: str, *, material: str = "m1") -> StagingChunk:
    return StagingChunk(
        id=cid,
        material_version_id="v1",
        content=content,
        locator={
            "material_id": material,
            "material_name": "资料",
            "page_index": page,
            "heading_path": [],
        },
    )


def _rag_hit(chunk: StagingChunk) -> RankedChunk:
    return RankedChunk(chunk=chunk, score=0.45, lexical_score=0.05, semantic_score=0.67)


def test_expand_rag_neighborhood_attaches_body_and_respects_rules():
    """邻域扩展：同/邻页正文进上下文；同页优先、跨资料与短块排除、分数继承锚点。"""
    hit = _rag_hit(_rag_chunk("h1", 2, "（一）考核成绩构成"))
    body_same = _rag_chunk("b1", 2, "同" * 300)       # 同页正文
    short_same = _rag_chunk("b2", 2, "短" * 20)       # 同页但 <60 → 又一个标题，排除
    body_adj = _rag_chunk("b3", 3, "邻" * 100)        # 邻页正文
    other = _rag_chunk("x1", 2, "外" * 200, material="m2")  # 跨资料，排除

    out = assistant_service._expand_rag_neighborhood(
        [hit], [hit.chunk, body_same, short_same, body_adj, other]
    )

    assert [item.chunk.id for item in out] == ["h1", "b1", "b3"]
    assert out[1].score == 0.45 and out[1].semantic_score == 0.67  # 继承锚点分数


def test_expand_rag_neighborhood_dedup_cap_and_degenerate():
    """已命中长块去重、总扩封顶、无页码锚点跳过、空排名直通。"""
    # 命中 h5 自身是长正文（与 h4 同页且是其最优邻域候选）→ 已命中必须去重
    hits = [
        _rag_hit(
            _rag_chunk(f"h{i}", 4 if i == 5 else i, ("长" * 200) if i == 5 else f"标题{i}")
        )
        for i in range(6)
    ]
    no_page_hit = RankedChunk(
        chunk=_rag_chunk("hN", None, "无页码标题"),
        score=0.4,
        lexical_score=0.0,
        semantic_score=0.6,
    )
    bodies = [_rag_chunk(f"b{i}", i, "文" * 80) for i in range(7)]
    ranked = [no_page_hit, *hits]

    out = assistant_service._expand_rag_neighborhood(
        ranked, [item.chunk for item in ranked] + bodies
    )

    # 无页码锚点跳过；6 个有页码锚点各带邻域，总数被 _RAG_EXPAND_MAX_TOTAL 封顶
    assert [item.chunk.id for item in out[:7]] == ["hN", "h0", "h1", "h2", "h3", "h4", "h5"]
    assert len(out) == len(ranked) + assistant_service._RAG_EXPAND_MAX_TOTAL
    assert "h5" not in [item.chunk.id for item in out[len(ranked):]]  # 已命中不重复带
    # 空排名直通
    assert assistant_service._expand_rag_neighborhood([], bodies) == []


def test_rag_route_targets_named_material_lexical_mode(monkeypatch):
    """点名资料：白名单通过 + ready → kind=rag，词面模式组装来源卡 payload。"""
    calls = _patch_rag(monkeypatch)

    routed = route_intent(
        _intent("已检索到相关资料，回答如下：", {"tool": assistant_service.RAG_TOOL, "args": {"material_id": "m1"}}),
        session=None,
        context=_rag_ctx(),
        message="监督学习的分类与回归是什么",
    )

    assert routed["kind"] == "rag"
    assert routed["stream"] is True
    assert routed["retrieval"]["mode"] == "lexical"
    assert calls["ensure"] == [("c1", ["m1"])]  # 自愈索引按点名范围
    assert calls["load"] == [("c1", ["m1"], True)]

    payload = routed["payload"]
    assert payload["question"] == "监督学习的分类与回归是什么"  # 教师原话，不经模型转述
    assert payload["material_id"] == "m1"
    assert payload["material_name"] == "教学大纲"
    assert payload["mode"] == "lexical"
    # 命中相关块、过滤无关块
    assert [s["block_id"] for s in payload["sources"]] == ["blk-1"]
    source = payload["sources"][0]
    assert source["material_name"] == "教学大纲"
    assert source["page_index"] == 2
    assert source["heading_path"] == ["第3章", "3.1 监督学习"]
    assert len(source["snippet"]) <= assistant_service._RAG_SNIPPET_CHARS
    assert routed["action"] == {
        "kind": "sources",
        "tool": assistant_service.RAG_TOOL,
        "args": {"material_id": "m1"},
        "status": "completed",
    }


def test_rag_route_course_wide_without_material_id(monkeypatch):
    calls = _patch_rag(monkeypatch)

    routed = route_intent(
        _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
        session=None,
        context=_rag_ctx(),
        message="这些资料都讲了什么",
    )

    assert routed["kind"] == "rag"
    assert calls["ensure"] == [("c1", None)]  # 全课程语料
    assert routed["payload"]["material_id"] is None
    assert routed["payload"]["material_name"] is None


def test_rag_route_hybrid_mode_when_embeddings_present(monkeypatch):
    """语料向量齐备 + 嵌入已配置 → 多查询混合检索（原问题+主题核心双变体批量嵌入）。"""

    class QueryEmbedder:
        def embed(self, texts):
            # 双变体契约：原问题恒为首变体，去问句框架的主题串为第二变体
            assert texts == ["监督学习的分类与回归是什么", "监督学习的分类与回归"]
            return [[1.0, 0.0], [1.0, 0.0]]

    calls = _patch_rag(monkeypatch, _rag_chunks(with_embeddings=True), configured=True)
    monkeypatch.setattr(assistant_service, "build_embedder", lambda: QueryEmbedder())

    routed = route_intent(
        _intent("", {"tool": assistant_service.RAG_TOOL, "args": {"material_id": "m1"}}),
        session=None,
        context=_rag_ctx(),
        message="监督学习的分类与回归是什么",
    )

    assert routed["retrieval"]["mode"] == "hybrid"
    assert routed["payload"]["mode"] == "hybrid"
    # 语义命中 blk-1（cos=1），blk-2 语义为 0 被过滤；两组同文合并后仍只有一条
    assert [s["block_id"] for s in routed["payload"]["sources"]] == ["blk-1"]
    assert calls["ensure"]  # 自愈仍先执行


def test_rag_route_embedder_failure_degrades_to_lexical(monkeypatch):
    """混合检索嵌入失败 → 词面降级，不断轮。"""

    class BrokenEmbedder:
        def embed(self, texts):
            raise RuntimeError("embedding down")

    _patch_rag(monkeypatch, _rag_chunks(with_embeddings=True), configured=True)
    monkeypatch.setattr(assistant_service, "build_embedder", lambda: BrokenEmbedder())

    routed = route_intent(
        _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
        session=None,
        context=_rag_ctx(),
        message="监督学习的分类与回归是什么",
    )
    assert routed["retrieval"]["mode"] == "lexical"
    assert routed["payload"]["sources"]


def test_rag_route_pushdown_scores_without_loading_embeddings(monkeypatch):
    """PG 方言语义下推：向量列不装载（59MB → ~4MB 文本），SQL 预计算分直进混合检索。"""

    class QueryEmbedder:
        def embed(self, texts):
            # 双变体一次批量嵌入 → 只为 SQL 下推取查询向量（打分不再调嵌入）
            assert texts == ["监督学习的分类与回归是什么", "监督学习的分类与回归"]
            return [[1.0, 0.0], [1.0, 0.0]]

    calls = _patch_rag(monkeypatch, _rag_chunks(), configured=True)  # 无向量语料
    monkeypatch.setattr(assistant_service, "supports_semantic_pushdown", lambda session: True)
    monkeypatch.setattr(assistant_service, "build_embedder", lambda: QueryEmbedder())
    monkeypatch.setattr(
        assistant_service,
        "load_semantic_scores",
        lambda *a, **kw: [{"blk-1": 1.0, "blk-2": 0.0}, {"blk-1": 1.0, "blk-2": 0.0}],
    )

    routed = route_intent(
        _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
        session=None,
        context=_rag_ctx(),
        message="监督学习的分类与回归是什么",
    )

    assert routed["retrieval"]["mode"] == "hybrid"
    # 语义分与 Python cosine 同款过滤：blk-1 命中、blk-2 语义 0 被滤
    assert [s["block_id"] for s in routed["payload"]["sources"]] == ["blk-1"]
    # 向量列不选出（include_embedding=False）
    assert calls["load"] and calls["load"][0][2] is False


def test_rag_route_pushdown_incomplete_coverage_degrades_to_lexical(monkeypatch):
    """下推分缺块（旧模型向量被过滤等）→ 覆盖不全，降级词面不断轮。"""

    class StubEmbedder:
        def embed(self, texts):
            return [[1.0, 0.0], [1.0, 0.0]]

    _patch_rag(monkeypatch, _rag_chunks(), configured=True)
    monkeypatch.setattr(assistant_service, "supports_semantic_pushdown", lambda session: True)
    monkeypatch.setattr(assistant_service, "build_embedder", lambda: StubEmbedder())
    monkeypatch.setattr(
        assistant_service,
        "load_semantic_scores",
        lambda *a, **kw: [{"blk-1": 1.0}, {"blk-1": 1.0}],  # 变体齐但缺 blk-2
    )

    routed = route_intent(
        _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
        session=None,
        context=_rag_ctx(),
        message="监督学习的分类与回归是什么",
    )
    assert routed["retrieval"]["mode"] == "lexical"


def test_rag_route_pushdown_failure_degrades_to_lexical(monkeypatch):
    """SQL 下推任一环失败（嵌入/查询）→ 降级词面，不上抛断轮。"""

    class BrokenEmbedder:
        def embed(self, texts):
            raise RuntimeError("embedding down")

    _patch_rag(monkeypatch, _rag_chunks(), configured=True)
    monkeypatch.setattr(assistant_service, "supports_semantic_pushdown", lambda session: True)
    monkeypatch.setattr(assistant_service, "build_embedder", lambda: BrokenEmbedder())

    routed = route_intent(
        _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
        session=None,
        context=_rag_ctx(),
        message="监督学习的分类与回归是什么",
    )
    assert routed["retrieval"]["mode"] == "lexical"


def test_rag_route_rejects_foreign_material_id(monkeypatch):
    _patch_rag(monkeypatch)
    with pytest.raises(AssistantError, match="白名单"):
        route_intent(
            _intent("", {"tool": assistant_service.RAG_TOOL, "args": {"material_id": "m-evil"}}),
            session=None,
            context=_rag_ctx(),
            message="问题",
        )


def test_rag_route_rejects_unparsed_material(monkeypatch):
    """点名未解析资料 → AssistantError（带反馈重试一次后落确定性文案）。"""
    _patch_rag(monkeypatch)
    with pytest.raises(AssistantError, match="尚未解析完成"):
        route_intent(
            _intent("", {"tool": assistant_service.RAG_TOOL, "args": {"material_id": "m2"}}),
            session=None,
            context=_rag_ctx(),
            message="问题",
        )


def test_rag_route_requires_parsed_material_course_wide(monkeypatch):
    """全课程无已解析资料 → 引导先解析（不让模型空转检索）。"""
    _patch_rag(monkeypatch)
    context = _ctx(materials=[
        {"id": "m1", "name": "教学大纲", "type": "teaching_syllabus", "status": "staged", "parse_status": None},
    ])
    with pytest.raises(AssistantError, match="已解析"):
        route_intent(
            _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
            session=None,
            context=context,
            message="问题",
        )


def test_rag_route_empty_question_rejected(monkeypatch):
    _patch_rag(monkeypatch)
    with pytest.raises(AssistantError, match="问题不能为空"):
        route_intent(
            _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
            session=None,
            context=_rag_ctx(),
            message="   ",
        )


def test_rag_route_no_retrievable_content_raises(monkeypatch):
    _patch_rag(monkeypatch, chunks=[])
    with pytest.raises(AssistantError, match="没有可检索"):
        route_intent(
            _intent("", {"tool": assistant_service.RAG_TOOL, "args": {}}),
            session=None,
            context=_rag_ctx(),
            message="问题",
        )


def test_run_turn_rag_streams_answer_and_persists_sources_card(session, monkeypatch):
    """整轮：段1 意图 → 检索 → 段2 流式 → action(kind=sources) 落库 + card 事件。"""
    _patch_rag(monkeypatch)
    # run_turn 从 DB 装配上下文；本测试不建解析数据，用手工上下文（含 ready 状态）
    monkeypatch.setattr(assistant_service, "load_turn_context", lambda session, *, course_id, session_id=None: _rag_ctx())
    client = StubClient(
        [_intent("已检索到相关资料，回答如下：", {"tool": assistant_service.RAG_TOOL, "args": {"material_id": "m1"}})],
        stream_text="监督学习分为分类与回归两类任务。",
    )
    task_id = _new_turn(session, "t-rag")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "监督学习的分类与回归是什么"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "rag"
    # 段2 用 RAG 专用 grounding prompt，且携带检索片段
    stream_call = client.stream_calls[0]
    assert stream_call["system_prompt"].startswith("你是高校课程「CS101」")
    assert "只依据下方「资料片段」作答" in stream_call["system_prompt"]
    assert stream_call["payload"]["question"] == "监督学习的分类与回归是什么"
    assert stream_call["payload"]["sources"][0]["material_name"] == "教学大纲"
    assert len(stream_call["payload"]["sources"][0]["text"]) <= assistant_service._RAG_BLOCK_PROMPT_CHARS

    # 落库：action kind=sources + payload，SSE 收到 card 事件
    row = session.execute(
        select(assistant_messages.c.action, assistant_messages.c.content, assistant_messages.c.stream_status)
        .where(assistant_messages.c.task_run_id == task_id, assistant_messages.c.role == "assistant")
    ).one()
    action = row.action
    assert row.stream_status == "complete"
    assert action["kind"] == "sources"
    assert action["tool"] == assistant_service.RAG_TOOL
    assert action["status"] == "completed"
    assert [s["block_id"] for s in action["payload"]["sources"]] == ["blk-1"]
    assert action["payload"]["mode"] == "lexical"
    assert row.content == "监督学习分为分类与回归两类任务。"
    cards = [e for e in sink.registry[task_id] if e["event"] == "card"]
    assert cards and cards[0]["data"]["kind"] == "sources"


def test_run_turn_rag_without_hits_persists_plain_chat(session, monkeypatch):
    """无命中：正文说明没找到，不落 sources 卡（action 回落普通问答形态）。"""
    from app.domain.knowledge.relevance import StagingChunk

    _patch_rag(
        monkeypatch,
        [StagingChunk(id="far", material_version_id="v1", content="完全无关内容", locator={})],
    )
    monkeypatch.setattr(assistant_service, "load_turn_context", lambda session, *, course_id, session_id=None: _rag_ctx())
    client = StubClient(
        [_intent("", {"tool": assistant_service.RAG_TOOL, "args": {}})],
        stream_text="资料里没有找到相关内容。",
    )
    task_id = _new_turn(session, "t-rag-empty")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "量子引力如何统一"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "rag"
    row = session.execute(
        select(assistant_messages.c.action, assistant_messages.c.stream_status).where(
            assistant_messages.c.task_run_id == task_id, assistant_messages.c.role == "assistant"
        )
    ).one()
    assert row.stream_status == "complete"
    assert row.action == {}  # 无卡：普通问答
    assert not [e for e in sink.registry[task_id] if e["event"] == "card"]


# ---------------------------------------------------------------------------
# 会话管理（v3 多会话）：CRUD / 入队归属 / 记忆边界
# ---------------------------------------------------------------------------


def test_session_crud_lifecycle(session):
    created = assistant_service.create_session(session, course_id="c1", title="  期末复习  ")
    session.commit()
    assert created["title"] == "期末复习"
    sid = created["id"]

    # 列表按最近活跃倒序（新建的在最前）
    assert [s["id"] for s in assistant_service.list_sessions(session, course_id="c1")] == [sid]

    renamed = assistant_service.rename_session(
        session, course_id="c1", session_id=sid, title="复习范围"
    )
    session.commit()
    assert renamed["title"] == "复习范围"

    # 空标题拒绝
    with pytest.raises(AssistantError, match="不能为空"):
        assistant_service.rename_session(session, course_id="c1", session_id=sid, title="   ")
    # 跨课程视角（c2）= 不存在——隔离红线：查询恒带 course_id
    with pytest.raises(AssistantError, match="不存在"):
        assistant_service.rename_session(session, course_id="c2", session_id=sid, title="x")
    with pytest.raises(AssistantError, match="不存在"):
        assistant_service.delete_session(session, course_id="c2", session_id=sid)

    # 删除（无在途）→ 清会话；再删 404
    assistant_service.delete_session(session, course_id="c1", session_id=sid)
    session.commit()
    assert assistant_service.list_sessions(session, course_id="c1") == []
    with pytest.raises(AssistantError, match="不存在"):
        assistant_service.delete_session(session, course_id="c1", session_id=sid)


def test_enqueue_turn_session_scoping_idempotency_autotitle(session):
    # 兜底：不带 session_id → 自动建/取默认会话
    r1 = enqueue_turn(session, course_id="c1", message="第一句问题")
    session.commit()
    sid1 = r1["session_id"]
    assert sid1

    # 自动标题 = 首条用户消息
    listed = assistant_service.list_sessions(session, course_id="c1")
    assert listed[0]["id"] == sid1
    assert listed[0]["title"] == "第一句问题"

    # 同会话同文本在途 → 复用同一任务（会话参与幂等键的前半）
    r1b = enqueue_turn(session, course_id="c1", message="第一句问题", session_id=sid1)
    assert r1b["task_run_id"] == r1["task_run_id"]
    assert r1b["session_id"] == sid1

    # 换会话同文本 → 新任务
    other = assistant_service.create_session(session, course_id="c1")
    session.commit()
    r2 = enqueue_turn(session, course_id="c1", message="第一句问题", session_id=other["id"])
    session.commit()
    assert r2["task_run_id"] != r1["task_run_id"]

    # payload 带 session_id（worker 段1 按会话取历史）
    payload = session.execute(
        select(task_runs.c.payload).where(task_runs.c.id == r2["task_run_id"])
    ).scalar_one()
    assert payload["session_id"] == other["id"]

    # 无效会话拒绝
    with pytest.raises(AssistantError, match="会话不存在"):
        enqueue_turn(session, course_id="c1", message="x", session_id="nope")

    # 消息按会话过滤
    m1 = assistant_service.list_messages(session, course_id="c1", session_id=sid1)
    assert [m["content"] for m in m1] == ["第一句问题"]


def test_delete_session_rejects_inflight_then_cascades(session):
    created = assistant_service.create_session(session, course_id="c1")
    session.commit()
    result = enqueue_turn(
        session, course_id="c1", message="在途问题", session_id=created["id"]
    )
    session.commit()

    # 在途（queued）→ 拒绝删除（防 worker 落库撞 FK）
    with pytest.raises(AssistantError, match="在途"):
        assistant_service.delete_session(session, course_id="c1", session_id=created["id"])

    # 任务到终态 → 可删，级联清空消息
    session.execute(
        update(task_runs)
        .where(task_runs.c.id == result["task_run_id"], task_runs.c.course_id == "c1")
        .values(status="succeeded")
    )
    session.commit()
    assistant_service.delete_session(session, course_id="c1", session_id=created["id"])
    session.commit()
    assert assistant_service.list_messages(session, course_id="c1") == []
    assert assistant_service.list_sessions(session, course_id="c1") == []


def test_history_is_session_scoped(session):
    s1 = assistant_service.create_session(session, course_id="c1")
    s2 = assistant_service.create_session(session, course_id="c1")
    session.commit()
    enqueue_turn(session, course_id="c1", message="甲会话的问题", session_id=s1["id"])
    enqueue_turn(session, course_id="c1", message="乙会话的问题", session_id=s2["id"])
    session.commit()

    # 会话是记忆边界：段1 历史只看本会话
    ctx1 = assistant_service.load_turn_context(session, course_id="c1", session_id=s1["id"])
    assert [h["content"] for h in ctx1["history"]] == ["甲会话的问题"]

    # 缺省不过滤（兼容旧 payload）
    ctx_all = assistant_service.load_turn_context(session, course_id="c1")
    assert [h["content"] for h in ctx_all["history"]] == ["甲会话的问题", "乙会话的问题"]


# ---------------------------------------------------------------------------
# 停止生成（v3）：三检查点 —— 开始前不产出 / 流式中留部分正文 / 落库前标记
# ---------------------------------------------------------------------------


def test_checkpoint_a_cancel_before_start_skips_model_and_persists_nothing(session):
    client = StubClient([_intent("不该被调用")])
    task_id = _new_turn(session, "t-cancel-a")
    session.execute(
        update(task_runs)
        .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
        .values(status="cancelled")
    )
    session.commit()
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "hi"},
        client=client,
        sink=sink,
    )

    assert result["cancelled"] is True
    assert client.calls == []  # 段1 都没进，零模型调用
    assert client.stream_calls == []
    rows = session.execute(
        select(assistant_messages.c.role).where(assistant_messages.c.task_run_id == task_id)
    ).all()
    assert rows == []  # 还没产出就不留痕（与「取消先于领取」一致）
    assert sink.registry.get(task_id, []) == []  # 无事件：SSE 走 DB 兜底发 done


def test_checkpoint_b_cancel_midstream_keeps_partial_and_marks_stopped(session):
    class CancelMidwayClient(StubClient):
        def stream_text(self, *, system_prompt, payload, temperature, on_delta, call_context, **kwargs):
            self.stream_calls.append({"system_prompt": system_prompt, "payload": payload})
            on_delta("前半段")
            # 模拟教师此刻点了停止（cancel 端点已提交）
            session.execute(
                update(task_runs)
                .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
                .values(status="cancelled")
            )
            session.commit()
            on_delta("后半段")  # guarded：append → 入缓冲 → 探测到 cancelled → raise
            return "前半段后半段"

    client = CancelMidwayClient([_intent("不该出现在正文里")])
    task_id = _new_turn(session, "t-cancel-b")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "讲讲第三章"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "chat"
    row = session.execute(
        select(assistant_messages.c.content, assistant_messages.c.stream_status).where(
            assistant_messages.c.task_run_id == task_id, assistant_messages.c.role == "assistant"
        )
    ).one()
    assert row.stream_status == "stopped"
    assert row.content == "前半段后半段"  # 保留已流出的部分正文
    assert "不该出现在正文里" not in row.content  # 没走 failed 降级
    assert sink.registry[task_id][-1]["event"] == "done"  # done 照常收口


def test_checkpoint_c_cancel_after_route_persists_stopped(session):
    class CancelAfterIntentClient(StubClient):
        def request_json(self, *, system_prompt, payload, temperature, call_context, **kwargs):
            response = super().request_json(
                system_prompt=system_prompt,
                payload=payload,
                temperature=temperature,
                call_context=call_context,
                **kwargs,
            )
            # 模拟意图解析返回后、路由/落库前教师停止
            session.execute(
                update(task_runs)
                .where(task_runs.c.id == task_id, task_runs.c.course_id == "c1")
                .values(status="cancelled")
            )
            session.commit()
            return response

    client = CancelAfterIntentClient(
        [_intent("课程当前各阶段状态见下表：", {"tool": "course_overview", "args": {}})]
    )
    task_id = _new_turn(session, "t-cancel-c")
    sink = MemoryTurnEventSink(task_id)

    result = run_turn(
        session,
        payload={"course_id": "c1", "task_run_id": task_id, "message": "课程概览"},
        client=client,
        sink=sink,
    )

    assert result["kind"] == "result"
    row = session.execute(
        select(assistant_messages.c.content, assistant_messages.c.action, assistant_messages.c.stream_status).where(
            assistant_messages.c.task_run_id == task_id, assistant_messages.c.role == "assistant"
        )
    ).one()
    assert row.stream_status == "stopped"
    assert row.content == "课程当前各阶段状态见下表："  # 产出保留
    assert row.action["kind"] == "result"  # 结果卡照常落
    assert sink.registry[task_id][-1]["event"] == "done"


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
    with pytest.raises(AssistantError, match="不能迁移到"):
        patch_message_action(session, course_id="c1", message_id=msg_id, action_status="dismissed")
    with pytest.raises(AssistantError, match="非法的提案状态"):
        patch_message_action(session, course_id="c1", message_id=msg_id, action_status="proposed")
    # 课程隔离：他课回写 404
    with pytest.raises(AssistantError, match="不存在"):
        patch_message_action(
            session, course_id="c2", message_id=msg_id, action_status="executed"
        )


def test_auto_proposal_claim_then_finish(session):
    """自动执行的卡片状态机：auto --claim--> executing --成功--> executed。

    executing 是「已开始、结果未知」的安全停态：中断后不再自动重跑（建项目/建
    蓝图非幂等，重放会重复建实体），卡上给教师「重试执行」。
    """
    task_id = _new_turn(session, "t-auto")
    assistant_service._insert_message(
        session,
        course_id="c1",
        task_run_id=task_id,
        role="assistant",
        content="已生成提案：",
        action={
            "kind": "proposal",
            "tool": "create_exam_project",
            "args": {},
            "payload": {"auto": True},
            "status": "auto",
        },
    )
    session.commit()
    msg_id = session.execute(
        select(assistant_messages.c.id).where(assistant_messages.c.task_run_id == task_id)
    ).scalar_one()

    # claim：必须在调业务 API 之前落库，刷新/双跑才不会重复执行
    claimed = patch_message_action(
        session, course_id="c1", message_id=msg_id, action_status="executing"
    )
    session.commit()
    assert claimed["action"]["status"] == "executing"
    # 认领是独占的：executing → executing 必须被拒（两个标签页同时看到 auto 卡时，
    # 只有先落库那次能 claim 成功，后到的那次不再执行业务写）
    with pytest.raises(AssistantError, match="不能迁移到"):
        patch_message_action(
            session, course_id="c1", message_id=msg_id, action_status="executing"
        )
    # auto 不是合法的目标状态（只能由后端在提案时写入）
    with pytest.raises(AssistantError, match="非法的提案状态"):
        patch_message_action(session, course_id="c1", message_id=msg_id, action_status="auto")

    done = patch_message_action(
        session, course_id="c1", message_id=msg_id, action_status="executed", receipt="已创建"
    )
    session.commit()
    assert done["action"]["status"] == "executed"
    assert done["action"]["receipt"] == "已创建"


# ---------------------------------------------------------------------------
# 生成结束通报（后端主动播报，不产生用户消息）
# ---------------------------------------------------------------------------


def _seed_generation_project(
    session, *, project_id: str = "p-gen", task_status: str = "succeeded",
    project_status: str = "review", progress: int = 100,
) -> None:
    """预置一个带生成任务的项目（get_project 会读论文摘要与生成态）。"""
    session.execute(exam_projects.insert().values(
        id=project_id, course_id="c1", name="E2E自动执行", status=project_status,
    ))
    session.execute(task_runs.insert().values(
        id=f"gr-{project_id}", course_id="c1", task_type="generation_run",
        input_version="v1", idempotency_key=f"gen-{project_id}",
        status=task_status, stage="generating", progress=progress,
        payload={"project_id": project_id, "generation_run_id": f"gr-{project_id}"},
        error_message="模型超时" if task_status == "failed" else None,
    ))
    session.commit()


def test_generation_report_not_settled_is_skipped(session):
    """生成还在跑：不播报（前端继续轮询，到终态再调）。"""
    _seed_generation_project(session, task_status="running", project_status="generating", progress=40)
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    session.commit()
    result = assistant_service.report_generation_complete(
        session, course_id="c1", session_id=sid, project_id="p-gen",
    )
    assert result == {"reported": False, "reason": "not_settled"}
    assert session.execute(select(assistant_messages.c.id)).scalars().all() == []


def test_generation_report_writes_assistant_message_without_user_bubble(session):
    """生成完成：只落一条助手消息（教师全程在等待，不该凭空多出自己发的「继续」）。"""
    _seed_generation_project(session)
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    session.commit()

    result = assistant_service.report_generation_complete(
        session, course_id="c1", session_id=sid, project_id="p-gen",
    )
    session.commit()

    assert result["reported"] is True
    rows = session.execute(
        select(assistant_messages.c.role, assistant_messages.c.content, assistant_messages.c.action)
    ).mappings().all()
    # 没有 user 消息：聊天里不会出现教师没发过的「继续」气泡
    assert [r["role"] for r in rows] == ["assistant"]
    assert "E2E自动执行" in rows[0]["content"]
    assert "试卷已生成" in rows[0]["content"]
    assert rows[0]["action"]["kind"] == "generation_report"
    # 幂等：前端轮询可能多次触发，重复调用不重复插消息
    again = assistant_service.report_generation_complete(
        session, course_id="c1", session_id=sid, project_id="p-gen",
    )
    assert again["reason"] == "already_reported"
    assert len(session.execute(select(assistant_messages.c.id)).scalars().all()) == 1


def test_generation_report_failure_message(session):
    """生成失败：同样由助手播报原因与重新生成引导。"""
    _seed_generation_project(
        session, task_status="failed", project_status="generating", progress=30,
    )
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    session.commit()
    result = assistant_service.report_generation_complete(
        session, course_id="c1", session_id=sid, project_id="p-gen",
    )
    session.commit()
    assert result["reported"] is True
    content = session.execute(select(assistant_messages.c.content)).scalar_one()
    assert "没有成功" in content
    assert "模型超时" in content
    assert "重新生成" in content


def test_generation_report_rejects_foreign_session(session):
    """会话不属于本课程 → 拒绝（课程隔离）。"""
    _seed_generation_project(session)
    with pytest.raises(AssistantError, match="会话不存在"):
        assistant_service.report_generation_complete(
            session, course_id="c1", session_id="nope", project_id="p-gen",
        )


def _seed_paper_with_dropped_slots(session, project_id: str = "p-gen") -> None:
    """给项目挂一份带缺口的当前卷：1 道题落卷 + 2 个题位被剔除（metadata 留痕）。"""
    _seed_generation_project(session, project_id=project_id)
    session.execute(paper_versions.insert().values(
        id="pv-gap", course_id="c1", exam_project_id=project_id,
        version_no=1, status="candidate",
        metadata={"dropped_slots": [
            {"item_index": 41, "question_type": "comprehensive",
             "exam_point_id": "ep9", "reason": "missing_stem"},
            {"item_index": 42, "question_type": "comprehensive",
             "exam_point_id": "ep9", "reason": "missing_stem"},
        ]},
    ))
    session.execute(generated_questions.insert().values(
        id="gq-gap", course_id="c1",
        payload={"stem": "正常题", "answer": "A", "score": 2},
    ))
    session.execute(paper_items.insert().values(
        id="pi-gap", course_id="c1", paper_version_id="pv-gap",
        generated_question_id="gq-gap", display_order=1,
    ))
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == project_id)
        .values(active_paper_version_id="pv-gap")
    )
    session.commit()


def test_generation_report_surfaces_dropped_slot_gap(session):
    """成功但有剔除：播报必须说清缺口（蓝图计划数/缺哪些题位/原因/补齐引导）。

    缺口无声是线上实测缺陷：蓝图 42 成卷 40，收尾消息照旧「共 40 道题，
    可以进入审核了」，教师对缺两道综合题零感知。
    """
    _seed_paper_with_dropped_slots(session)
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    session.commit()

    result = assistant_service.report_generation_complete(
        session, course_id="c1", session_id=sid, project_id="p-gen",
    )
    session.commit()

    assert result["reported"] is True
    content = session.execute(
        select(assistant_messages.c.content).where(assistant_messages.c.role == "assistant")
    ).scalar_one()
    assert "试卷已生成" in content
    assert "卷面有缺口" in content
    assert "蓝图计划 3 道题位" in content  # item_count=1 + 剔除 2
    assert "成卷 1 道" in content
    assert "#41、#42" in content
    assert "综合题" in content
    assert "模型未产出可用题干" in content
    assert "重新生成" in content


# ---------------------------------------------------------------------------
# 内部接力（推进下一张卡但不写用户消息）
# ---------------------------------------------------------------------------


def test_relay_enqueues_turn_without_user_message(session):
    """接力只建任务不插 user 消息：聊天里不该出现教师没打过的「继续」气泡。"""
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    card_id = assistant_service._insert_message(
        session,
        course_id="c1",
        task_run_id=_new_turn(session, "t-relay-src"),
        role="assistant",
        content="已生成提案：",
        action={"kind": "proposal", "tool": "confirm_blueprint", "args": {},
                "payload": {}, "status": "executed"},
        session_id=sid,
    )
    session.commit()

    result = assistant_service.enqueue_relay(
        session, course_id="c1", session_id=sid, after_message_id=card_id,
    )
    session.commit()

    assert result["duplicate"] is False
    task = session.execute(
        select(task_runs.c.payload).where(task_runs.c.id == result["task_run_id"])
    ).scalar_one()
    assert task["relay_after"] == card_id
    assert task["message"] == assistant_service._RELAY_INSTRUCTION
    # 关键：没有教师消息
    roles = session.execute(select(assistant_messages.c.role)).scalars().all()
    assert roles == ["assistant"]


def test_relay_is_idempotent_per_message(session):
    """同一条消息只推进一次：双击/重试不会把流程推两步。"""
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    card_id = assistant_service._insert_message(
        session,
        course_id="c1",
        task_run_id=_new_turn(session, "t-relay-idem"),
        role="assistant",
        content="已生成提案：",
        action={"kind": "proposal", "tool": "confirm_contract", "args": {},
                "payload": {}, "status": "executed"},
        session_id=sid,
    )
    session.commit()

    first = assistant_service.enqueue_relay(
        session, course_id="c1", session_id=sid, after_message_id=card_id,
    )
    session.commit()
    again = assistant_service.enqueue_relay(
        session, course_id="c1", session_id=sid, after_message_id=card_id,
    )
    session.commit()
    assert again["task_run_id"] == first["task_run_id"]
    assert again["duplicate"] is True
    assert len(
        session.execute(
            select(task_runs.c.id).where(task_runs.c.task_type == assistant_service.TASK_TYPE)
        ).scalars().all()
    ) == 2  # 建卡的那一轮 + 接力这一轮，没有第二轮接力


def test_relay_rejects_unknown_message_or_session(session):
    sid = assistant_service.ensure_default_session(session, course_id="c1")
    session.commit()
    with pytest.raises(AssistantError, match="目标消息不存在"):
        assistant_service.enqueue_relay(
            session, course_id="c1", session_id=sid, after_message_id="nope",
        )
    with pytest.raises(AssistantError, match="会话不存在"):
        assistant_service.enqueue_relay(
            session, course_id="c1", session_id="nope", after_message_id="nope",
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


def test_paper_summary_prefers_active_version(session):
    """试卷摘要取 active_paper_version_id（教师所见版本）；无激活记录回退最新。

    历史版本被重新激活后 active ≠ 最新版本号，评审提案与引导必须与试卷页同源。
    """
    session.execute(
        exam_projects.insert().values(
            id="proj1",
            course_id="c1",
            name="评审目标",
            status="review",
        )
    )
    session.execute(
        paper_versions.insert().values(
            id="pv1", course_id="c1", exam_project_id="proj1",
            version_no=1, status="review",
        )
    )
    session.execute(
        paper_versions.insert().values(
            id="pv2", course_id="c1", exam_project_id="proj1",
            version_no=2, status="review",
        )
    )
    # 激活指针在版本行落库后回填（FK 逐句校验，前置引用会撞 FOREIGN KEY）
    session.execute(
        exam_projects.update()
        .where(exam_projects.c.id == "proj1", exam_projects.c.course_id == "c1")
        .values(active_paper_version_id="pv1")
    )
    session.commit()

    summary = assistant_service._paper_summary(
        session, course_id="c1", project={"id": "proj1", "active_paper_version_id": "pv1"}
    )
    assert summary["exists"] is True
    assert summary["paper_version_id"] == "pv1"
    assert summary["version_no"] == 1

    fallback = assistant_service._paper_summary(
        session, course_id="c1", project={"id": "proj1"}
    )
    assert fallback["paper_version_id"] == "pv2"


def test_load_context_carries_generation_task_status(session):
    """项目快照带生成任务真状态：status=generating 只是阶段标记（合同确认即置上），
    模型必须另看 generation_task_status 才能区分「从未发起（null）/ 在跑 /
    已失败」——否则会把生成未发起的项目当「任务在跑」停住，start_generation 发不出。"""
    session.execute(
        exam_projects.insert().values(
            id="proj-g1", course_id="c1", name="在跑项目", status="generating",
        )
    )
    session.execute(
        exam_projects.insert().values(
            id="proj-g2", course_id="c1", name="未发起项目", status="generating",
        )
    )
    session.execute(
        task_runs.insert().values(
            id="gen-task-1",
            course_id="c1",
            task_type="generation_run",
            input_version="v1",
            idempotency_key="test-gen-task-1",
            status="running",
            payload={"project_id": "proj-g1"},
        )
    )
    session.commit()

    context = load_turn_context(session, course_id="c1")
    by_id = {p["id"]: p for p in context["projects"]}
    assert by_id["proj-g1"]["generation_task_status"] == "running"
    assert by_id["proj-g1"]["active_task_run_id"] == "gen-task-1"
    assert by_id["proj-g2"]["generation_task_status"] is None
    assert by_id["proj-g2"]["active_task_run_id"] is None
