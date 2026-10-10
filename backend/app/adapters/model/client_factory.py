"""统一 LLM 客户端工厂：收敛散落的 LLMJsonClient 构造点（design §5.4）。

用法::

    client = build_llm_client("classify", timeout=..., recorder=...)

- 配置解析走 :mod:`app.services.llm_registry`（DB 权威，.env 兜底，30s TTL）；
- 思考开关 / 超时 / 重试 / 思考档 / 观测 recorder 全部由调用方按既有语义
  作为 overrides 传入——工厂只负责"用哪个供应商/型号"，不改变任何调用参数；
- **动态引用** ``llm_gateway.LLMJsonClient``：既有测试以
  ``monkeypatch.setattr("app.adapters.model.llm_gateway.LLMJsonClient", ...)``
  打桩，模块级 from-import 会让桩失效。
"""

from __future__ import annotations

from typing import Any

from app.services.llm_registry import LlmConfig, resolve_llm_config


def build_llm_client(
    purpose: str = "default",
    *,
    recorder: Any = None,
    **overrides: Any,
) -> Any:
    """按用途构造 LLMJsonClient；参数语义与各调用点原构造完全一致。"""
    from app.adapters.model import llm_gateway

    cfg = resolve_llm_config(purpose)
    kwargs: dict[str, Any] = {
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "model": cfg.model,
    }
    kwargs.update(overrides)
    return llm_gateway.LLMJsonClient(recorder=recorder, **kwargs)


def resolve_llm(purpose: str = "default") -> LlmConfig:
    """需要读配置本身（而非构造客户端）的调用方用它，如 LLMGateway 日志参数。"""
    return resolve_llm_config(purpose)
