"""思考模型推理内容的透出（on_think）：与正文严格分通道。

- stream_text：SSE chunk 的 reasoning_content / reasoning 增量回调 on_think，
  不混入正文 parts（返回全文仍是纯回答）。
- request_json：非流式响应 message 上的整段推理在校验成功后一次性回调；
  校验失败走重试路径时不推半截思考。
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
    """httpx.Client 替身：非流式 post 与流式 stream 都按预置返回。"""

    def __init__(self, *, response: _FakeResponse | None = None, lines: list[str] | None = None) -> None:
        self._response = response
        self._lines = lines or []
        self.post_calls: list[str] = []
        self.stream_calls: list[str] = []

    def post(self, url: str, **_kwargs) -> _FakeResponse:
        self.post_calls.append(url)
        assert self._response is not None
        return self._response

    def stream(self, _method: str, url: str, **_kwargs) -> _FakeStreamCM:
        self.stream_calls.append(url)
        return _FakeStreamCM(_FakeStreamResponse(self._lines))


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
    """内容为空 → 解析失败重试 → 最终失败：一次思考都不推（防半截内容）。"""
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
