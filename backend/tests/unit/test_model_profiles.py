"""型号档案与网关参数下发的单测——把 StepFun 官方文档口径锁定成可执行断言。

覆盖：档案解析（已收录/未收录/供应商回退）、档位收敛（2603 两档）、
json_schema strict 结构约束的下发与回退、tool_choice 门控。
"""
from __future__ import annotations

import json

import httpx

from app.adapters.model.llm_gateway import LLMJsonClient
from app.adapters.model.model_profiles import (
    MODEL_PROFILES,
    normalize_effort,
    resolve_model_profile,
)

_STEPFUN_BASE = "https://api.stepfun.com/step_plan/v1"
_GENERIC_BASE = "https://llm.example.com/v1"


def test_step_models_resolve_to_stepfun_profile():
    profile = resolve_model_profile("step-3.7-flash", base_url=_STEPFUN_BASE)
    assert profile.thinking_style == "reasoning_effort"
    assert profile.supported_efforts == ("low", "medium", "high")
    assert profile.default_effort == "low"
    assert profile.supports_json_schema is True
    # StepFun 未文档化 tool_choice，下发会 400（既有经验）
    assert profile.supports_tool_choice is False


def test_two_tier_model_profile_registered():
    profile = resolve_model_profile("step-3.5-flash-2603", base_url=_STEPFUN_BASE)
    # 官方文档：2603 只收 low/high，medium 必须收敛否则 400
    assert profile.supported_efforts == ("low", "high")
    assert profile.default_effort == "low"


def test_unknown_stepfun_model_falls_back_to_endpoint_style():
    profile = resolve_model_profile("step-9.9-new", base_url=_STEPFUN_BASE)
    assert profile.thinking_style == "reasoning_effort"
    assert profile.supports_tool_choice is False
    assert profile.supports_json_schema is True


def test_unknown_generic_model_uses_openai_compatible_fallback():
    profile = resolve_model_profile("some-vendor-model", base_url=_GENERIC_BASE)
    assert profile.thinking_style == "thinking_disabled"
    # 通用端点不发供应商特性参数
    assert profile.supports_json_schema is False
    assert profile.supported_efforts == ()
    assert profile.supports_tool_choice is True


def test_registry_covers_documented_step_models():
    # 官方 Quickstart 模型清单：换已收录型号只改 .env，档案自动跟随
    assert {"step-5-preview", "step-3.7-flash", "step-3.5-flash"} <= set(MODEL_PROFILES)


def test_normalize_effort_passthrough_and_clamp():
    profile = MODEL_PROFILES["step-3.5-flash-2603"]
    assert normalize_effort("high", profile) == "high"
    # medium 不被 2603 支持 → 收敛到档案缺省档 low，避免 400
    assert normalize_effort("medium", profile) == "low"
    assert normalize_effort(None, profile) is None


def _client_with_capture(
    model: str, base_url: str, **kwargs
) -> tuple[LLMJsonClient, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"ok": True})}}]},
        )

    client = LLMJsonClient(
        api_key="test-key",
        base_url=base_url,
        model=model,
        max_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )
    return client, requests


def _call(client: LLMJsonClient, **overrides):
    kwargs = {
        "system_prompt": "system",
        "payload": {"blocks": ["材料"]},
        "temperature": 0.0,
    }
    kwargs.update(overrides)
    return client.request_json(**kwargs)


def test_gateway_sends_strict_json_schema_for_supported_model():
    client, requests = _client_with_capture("step-3.7-flash", _STEPFUN_BASE)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    _call(client, response_schema=schema)
    body = requests[0]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == schema
    # disable_thinking=True → 三档型号默认下发 low（官方口径：信息抽取用 low）
    assert body["reasoning_effort"] == "low"
    assert "thinking" not in body


def test_gateway_falls_back_to_json_object_when_profile_unsupported():
    client, requests = _client_with_capture("some-vendor-model", _GENERIC_BASE)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    _call(client, response_schema=schema)
    body = requests[0]
    # 未收录型号不支持 json_schema → 回退 json_object，绝不发供应商特性参数
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_gateway_without_schema_keeps_json_object():
    client, requests = _client_with_capture("step-3.7-flash", _STEPFUN_BASE)
    _call(client)
    assert requests[0]["response_format"] == {"type": "json_object"}


def test_gateway_clamps_effort_to_profile_support():
    client, requests = _client_with_capture("step-3.5-flash-2603", _STEPFUN_BASE)
    _call(client, reasoning_effort="medium")
    assert requests[0]["reasoning_effort"] == "low"


def test_gateway_tool_choice_gated_by_profile():
    tool = {"name": "f", "parameters": {"type": "object", "properties": {}}}
    step_client, step_requests = _client_with_capture(
        "step-3.7-flash", _STEPFUN_BASE
    )
    _call(step_client, tool=tool)
    assert "tool_choice" not in step_requests[0]

    gen_client, gen_requests = _client_with_capture("generic-model", _GENERIC_BASE)
    _call(gen_client, tool=tool)
    assert gen_requests[0]["tool_choice"] == "required"
