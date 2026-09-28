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
    # 点名查询的定位规则必须写进提示（否则卡片永远是全课程范围）
    assert "结果卡只呈现该项目" in system_prompt

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
        _intent("学期2的试卷情况见下表：", {"tool": "paper_status", "args": {"project_id": "p1"}}),
        session=None,
        context=_project_ctx(),
    )
    assert routed["kind"] == "result"
    assert [p["id"] for p in routed["payload"]["projects"]] == ["p1"]


def test_read_tool_without_target_lists_all_projects():
    routed = route_intent(
        _intent("", {"tool": "paper_status", "args": {}}),
        session=None,
        context=_project_ctx(),
    )
    assert [p["id"] for p in routed["payload"]["projects"]] == ["p1", "p2"]


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
            _intent("", {"tool": "paper_status", "args": {"project_id": "p-evil"}}),
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

    def fake_load(session, *, course_id, material_ids=None):
        calls["load"].append((course_id, material_ids))
        return _rag_chunks() if chunks is None else chunks

    monkeypatch.setattr(assistant_service, "ensure_embedded", fake_ensure)
    monkeypatch.setattr(assistant_service, "load_content_chunks", fake_load)
    monkeypatch.setattr(assistant_service, "embedding_configured", lambda: configured)
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
    assert calls["load"] == [("c1", ["m1"])]

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
    """语料向量齐备 + 嵌入已配置 → 混合检索（0.35 词面 + 0.65 语义）。"""

    class QueryEmbedder:
        def embed(self, texts):
            assert texts == ["监督学习的分类与回归是什么"]
            return [[1.0, 0.0]]

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
    # 语义命中 blk-1（cos=1），blk-2 语义为 0 被过滤
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
