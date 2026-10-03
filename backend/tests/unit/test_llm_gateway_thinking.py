"""思考模型推理内容的透出（on_think）：与正文严格分通道。

- stream_text：SSE chunk 的 reasoning_content / reasoning 增量回调 on_think，
  不混入正文 parts（返回全文仍是纯回答）。
- request_json 非流式：响应 message 上的整段推理在校验成功后一次性回调；
  校验失败走重试路径时不推半截思考。
- request_json(stream=True)：SSE 推理增量实时回调（等待期即可见思考），
  正文 JSON 增量只在服务端拼装；请求体同时下发 stream 与 response_format。
  流式固有取舍：解析/校验失败重试时思考可能已推出甚至重复推送。
"""

from __future__ import annotations

import json

import pytest

from app.adapters.model.llm_gateway import LLMJsonClient, LLMGatewayError
from app.domain.model_calls import ModelCallContext


def _ctx() -> ModelCallContext:
    return ModelCallContext(course_id="c1", stage="assistant_turn")


class _FakeResponse:
    """request_json 用的非流式响应替身。"""

    def __init__(self, body: dict, *, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers: dict = {}
        self.content = json.dumps(body).encode()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _completion_body(message: dict) -> dict:
    return {"id": "cmpl-1", "choices": [{"message": message}], "usage": {}}


class _FakeStreamResponse:
    """stream_text 用的流式响应替身。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        return None

    def read(self) -> bytes:
        return b""

    def iter_lines(self) -> list[str]:
        return list(self._lines)


class _FakeStreamCM:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStreamResponse:
        return self._response

    def __exit__(self, *_exc) -> bool:
        return False


class _FakeHttpClient:
    """httpx.Client 替身：非流式 post 与流式 stream 都按预置返回。

    line_sets：按 stream 调用次序逐次弹出（用于重试场景，弹完复用最后一组）；
    lines：每次 stream 都返回同一组（缺省）。
    """

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        lines: list[str] | None = None,
        line_sets: list[list[str]] | None = None,
    ) -> None:
        self._response = response
        self._lines = lines or []
        self._line_sets = [list(s) for s in (line_sets or [])]
        self.post_calls: list[str] = []
        self.post_bodies: list[dict] = []
        self.stream_calls: list[str] = []
        self.stream_bodies: list[dict] = []

    def post(self, url: str, **kwargs) -> _FakeResponse:
        self.post_calls.append(url)
        self.post_bodies.append(kwargs.get("json"))
        assert self._response is not None
        return self._response

    def stream(self, _method: str, url: str, **kwargs) -> _FakeStreamCM:
        self.stream_calls.append(url)
        self.stream_bodies.append(kwargs.get("json"))
        if self._line_sets:
            lines = self._line_sets.pop(0) if len(self._line_sets) > 1 else self._line_sets[0]
        else:
            lines = self._lines
        return _FakeStreamCM(_FakeStreamResponse(lines))


def _client(http_client) -> LLMJsonClient:
    return LLMJsonClient(
        api_key="sk-test",
        base_url="https://api.stepfun.com/v1",
        model="step-3.7-flash",
        client=http_client,
    )


def test_stream_text_forwards_reasoning_to_on_think_not_body():
    """reasoning 增量走 on_think；正文回调与返回全文都不含思考内容。"""
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "先想"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": "你好"}}]}),
        # 少数档案字段名是 reasoning，同样识别
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning": "再想"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": "，世界。"}}]}),
        "data: [DONE]",
    ]
    client = _client(_FakeHttpClient(lines=lines))
    thinks: list[str] = []
    deltas: list[str] = []

    text = client.stream_text(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.6,
        on_delta=deltas.append,
        on_think=thinks.append,
        call_context=_ctx(),
    )

    assert thinks == ["先想", "再想"]
    assert deltas == ["你好", "，世界。"]
    assert text == "你好，世界。"  # 拼装全文 = 纯正文，思考绝不混入


def test_stream_text_without_on_think_keeps_working():
    """on_think 缺省（既有调用方）：思考被忽略，不报错、不进正文。"""
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "内部推理"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": "回答"}}]}),
        "data: [DONE]",
    ]
    client = _client(_FakeHttpClient(lines=lines))
    text = client.stream_text(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.6,
        call_context=_ctx(),
    )
    assert text == "回答"


def test_request_json_reports_reasoning_after_success():
    """非流式响应的整段推理在校验成功后一次性回调。"""
    body = _completion_body(
        {
            "content": '{"reply": "ok", "action": {}}',
            "reasoning_content": "教师要出卷，先确认课程有没有项目……",
        }
    )
    client = _client(_FakeHttpClient(response=_FakeResponse(body)))
    thinks: list[str] = []

    result = client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
        on_think=thinks.append,
    )

    assert result == {"reply": "ok", "action": {}}
    assert thinks == ["教师要出卷，先确认课程有没有项目……"]


def test_request_json_no_reasoning_means_no_callback():
    body = _completion_body({"content": '{"reply": "ok"}'})
    client = _client(_FakeHttpClient(response=_FakeResponse(body)))
    thinks: list[str] = []
    client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
        on_think=thinks.append,
    )
    assert thinks == []


def test_request_json_failed_validation_does_not_report_reasoning():
    """非流式：内容为空 → 解析失败重试 → 最终失败：一次思考都不推。"""
    empty_body = _completion_body({"content": "   ", "reasoning_content": "没想完"})
    client = LLMJsonClient(
        api_key="sk-test",
        base_url="https://api.stepfun.com/v1",
        model="step-3.7-flash",
        max_attempts=1,
        client=_FakeHttpClient(response=_FakeResponse(empty_body)),
    )
    thinks: list[str] = []
    with pytest.raises(LLMGatewayError):
        client.request_json(
            system_prompt="sys",
            payload={"q": "hi"},
            temperature=0.0,
            call_context=_ctx(),
            on_think=thinks.append,
        )
    assert thinks == []


# ----------------------
# 流式 JSON（意图解析）
# ----------------------


def test_request_json_stream_forwards_reasoning_live_and_parses_json():
    """stream=True：推理增量实时回调、正文 JSON 拼装后解析，请求体带
    stream + response_format（json_object 不因流式丢失）。"""
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "先想"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning": "再想"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": '{"reply": '}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": '"ok", "action": {}}'}}]}),
        'data: ' + json.dumps({"id": "cmpl-s", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
        "data: [DONE]",
    ]
    http = _FakeHttpClient(lines=lines)
    client = _client(http)
    thinks: list[str] = []

    result = client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
        on_think=thinks.append,
        stream=True,
    )

    assert result == {"reply": "ok", "action": {}}
    assert thinks == ["先想", "再想"]
    assert http.post_calls == []  # 走了流式，没退化到非流式
    body = http.stream_bodies[0]
    assert body["stream"] is True
    assert body["response_format"] == {"type": "json_object"}


def test_request_json_stream_retry_may_repush_thinking(monkeypatch):
    """流式重试语义：第一次正文为空失败、第二次成功——思考可能重复推送
    （流式固有取舍，罕见路径可接受，见 _stream_json_once 注释）。"""
    monkeypatch.setattr("app.adapters.model.llm_gateway.time.sleep", lambda _: None)
    empty_lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "第一遍思考"}}]}),
        "data: [DONE]",
    ]
    ok_lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "第一遍思考"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"content": '{"reply": "ok"}'}}]}),
        "data: [DONE]",
    ]
    http = _FakeHttpClient(line_sets=[empty_lines, ok_lines])
    client = LLMJsonClient(
        api_key="sk-test",
        base_url="https://api.stepfun.com/v1",
        model="step-3.7-flash",
        max_attempts=2,
        client=http,
    )
    thinks: list[str] = []

    result = client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
        on_think=thinks.append,
        stream=True,
    )

    assert result == {"reply": "ok"}
    assert thinks == ["第一遍思考", "第一遍思考"]


def test_request_json_stream_assembles_tool_call_arguments():
    """stream=True + tool：arguments 被拆成多个 delta 下发（name 只在首段），
    网关按 index 拼回 message.tool_calls，交 _extract_tool_arguments 统一解析。"""
    tool = {"name": "submit_reply", "parameters": {"type": "object", "properties": {}}}
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "先想"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "submit_reply", "arguments": '{"reply": '}}
        ]}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "", "type": "", "function": {"name": "", "arguments": '"好的"'}}
        ]}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "", "type": "", "function": {"name": "", "arguments": '}'}}
        ]}}]}),
        "data: [DONE]",
    ]
    http = _FakeHttpClient(lines=lines)
    client = _client(http)
    thinks: list[str] = []

    result = client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
        tool=tool,
        on_think=thinks.append,
        stream=True,
    )

    assert result == {"reply": "好的"}
    assert thinks == ["先想"]  # 工具通道下思考增量仍实时回调
    body = http.stream_bodies[0]
    assert body["tools"] == [{"type": "function", "function": tool}]
    # 工具通道不走 json_object：function calling 与 response_format 互斥
    assert "response_format" not in body
    # StepFun 档案不发 tool_choice（既有一致行为）
    assert "tool_choice" not in body


class _FakeCacheRecorder:
    """lookup_response / record 的最小替身，用于锁定"哪些调用可缓存"。"""

    def __init__(self, cached: dict | None = None) -> None:
        self.cached = cached
        self.lookups: list[str] = []
        self.records: list[dict] = []

    def lookup_response(self, *, model: str, prompt_hash: str) -> dict | None:
        self.lookups.append(prompt_hash)
        return self.cached

    def record(self, **values) -> None:
        self.records.append(values)


def test_temperature_zero_without_validator_is_never_cached():
    """无 response_validator 的 temperature=0 调用不查也不写缓存。

    根因：助手意图解析曾是唯一不带 validator 的 temp=0 调用，退化 JSON（`{}`）
    被记成成功落库后，同 prompt 重试被逐字回放，一次模型抖动放大成永久失败。
    """
    body = _completion_body({"content": "{}"})
    http = _FakeHttpClient(response=_FakeResponse(body))
    recorder = _FakeCacheRecorder(cached={"reply": "缓存的旧回答"})
    client = LLMJsonClient(
        api_key="sk-test",
        base_url="https://api.stepfun.com/v1",
        model="step-3.7-flash",
        client=http,
        recorder=recorder,
    )

    result = client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
    )

    assert result == {}  # 真实响应，不取缓存
    assert recorder.lookups == []  # 压根不查
    assert len(http.post_calls) == 1  # 真实打了模型


def test_temperature_zero_with_validator_still_reuses_cache():
    """带 validator 的 temp=0 调用照旧复用历史成功响应（知识构建类零 token 重建）。"""
    http = _FakeHttpClient(response=_FakeResponse(_completion_body({"content": '{"ok": true}'})))
    recorder = _FakeCacheRecorder(cached={"ok": True})
    client = LLMJsonClient(
        api_key="sk-test",
        base_url="https://api.stepfun.com/v1",
        model="step-3.7-flash",
        client=http,
        recorder=recorder,
    )

    result = client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
        response_validator=lambda _r: None,
    )

    assert result == {"ok": True}
    assert len(recorder.lookups) == 1
    assert http.post_calls == []  # 命中缓存，未打模型


def test_stream_text_body_keeps_stream_without_response_format():
    """stream_text（自由文本打字机）：只下发 stream，不带 response_format
    ——_build_body 解耦后不得把 json_object 锁到正文流式上。"""
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "你好"}}]}),
        "data: [DONE]",
    ]
    http = _FakeHttpClient(lines=lines)
    client = _client(http)

    text = client.stream_text(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.6,
        call_context=_ctx(),
    )

    assert text == "你好"
    body = http.stream_bodies[0]
    assert body["stream"] is True
    assert "response_format" not in body


def test_request_json_non_stream_body_keeps_json_object():
    """非流式 JSON（_post）：json_mode 显式下发后行为不变——带
    response_format json_object、不带 stream。"""
    body = _completion_body({"content": '{"reply": "ok"}'})
    http = _FakeHttpClient(response=_FakeResponse(body))
    client = _client(http)

    client.request_json(
        system_prompt="sys",
        payload={"q": "hi"},
        temperature=0.0,
        call_context=_ctx(),
    )

    sent = http.post_bodies[0]
    assert sent["response_format"] == {"type": "json_object"}
    assert "stream" not in sent
