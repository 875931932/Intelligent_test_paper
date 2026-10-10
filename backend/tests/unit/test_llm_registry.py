"""llm_registry 解析链测试：.env 分 purpose 兜底 / DB 绑定优先 / TTL / 档案注册。

全局 conftest 已把 db 开关关到 env 模式；本文件用 db_registry fixture
（本地 SQLite + 注入会话工厂）单独打开 DB 路径，验证完即恢复。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.model.model_profiles import resolve_model_profile
from app.config import settings
from app.db.schema import Base, LlmBinding, LlmModel, LlmProvider
from app.services import llm_registry
from app.services.llm_registry import RegistryNotConfigured, resolve_llm_config


@pytest.fixture
def env_settings(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "env-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://env.example/v1/")
    monkeypatch.setattr(settings, "llm_model", "env-default")
    monkeypatch.setattr(settings, "llm_extract_model", "env-extract")
    monkeypatch.setattr(settings, "llm_classify_model", "")
    monkeypatch.setattr(settings, "llm_consolidate_model", "")
    llm_registry.clear_cache()
    yield
    llm_registry.clear_cache()


@pytest.fixture
def db_registry(monkeypatch):
    """打开 DB 开关：本地 SQLite 快照 + 注入 app.db.session.get_session_factory。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: factory)
    llm_registry.clear_cache()
    llm_registry.set_db_enabled(True)
    yield factory
    llm_registry.set_db_enabled(False)
    llm_registry.clear_cache()
    engine.dispose()


def _seed_db(
    factory,
    *,
    provider_status: str = "active",
    model_id: str = "m1",
    wire_model: str = "db-model",
    binding_purpose: str = "default",
    profile: dict | None = None,
):
    with factory() as session:
        session.add(
            LlmProvider(
                id="p1",
                name="DB供应商",
                base_url="https://db.example/v1",
                api_key="db-key",
                status=provider_status,
            )
        )
        session.add(
            LlmModel(
                id=model_id,
                provider_id="p1",
                model_id=wire_model,
                display_name="DB模型",
                profile=profile or {},
            )
        )
        session.add(LlmBinding(purpose=binding_purpose, model_id=model_id))
        session.commit()


# ── .env 兜底（db 关闭，与既有测试打桩口径一致） ──────────────────────────────


def test_env_fallback_uses_per_purpose_model_variables(env_settings):
    llm_registry.set_db_enabled(False)
    cfg = resolve_llm_config("extract")
    assert (cfg.source, cfg.model) == ("env", "env-extract")
    # 未设置分阶段变量的用途回落 LLM_MODEL
    assert resolve_llm_config("classify").model == "env-default"
    assert resolve_llm_config("generation").model == "env-default"
    assert resolve_llm_config("default").base_url == "https://env.example/v1"  # 尾斜杠已收敛
    assert llm_registry.llm_ready("default")


def test_unconfigured_purpose_raises_and_not_ready(env_settings, monkeypatch):
    llm_registry.set_db_enabled(False)
    monkeypatch.setattr(settings, "llm_api_key", "")
    with pytest.raises(RegistryNotConfigured):
        resolve_llm_config("default")
    assert not llm_registry.llm_ready("default")


def test_unknown_purpose_raises(env_settings):
    llm_registry.set_db_enabled(False)
    with pytest.raises(RegistryNotConfigured):
        resolve_llm_config("bogus")


# ── DB 绑定优先 ──────────────────────────────────────────────────────────────


def test_db_binding_wins_over_env_and_default_fallback(db_registry, env_settings):
    _seed_db(db_registry)
    cfg = resolve_llm_config("default")
    assert (cfg.source, cfg.model, cfg.base_url) == (
        "db",
        "db-model",
        "https://db.example/v1",
    )
    # 未细分用途 → 回落 db default（不再被 .env 的分阶段变量抢走）
    assert resolve_llm_config("classify").source == "db"
    assert resolve_llm_config("classify").model == "db-model"


def test_db_profile_registered_with_priority(db_registry, env_settings):
    _seed_db(
        db_registry,
        profile={
            "thinking_style": "reasoning_effort",
            "supported_efforts": ["low", "high"],
            "default_effort": "low",
            "supports_tool_choice": False,
            "supports_json_schema": True,
        },
    )
    resolve_llm_config("default")  # 触发快照加载 → 注册 DB 档案
    profile = resolve_model_profile("db-model", base_url="https://other.example/v1")
    assert profile.supports_json_schema is True
    assert profile.supports_tool_choice is False
    assert profile.default_effort == "low"


def test_disabled_provider_falls_back_to_env_and_clears_profiles(db_registry, env_settings):
    _seed_db(db_registry, provider_status="disabled")
    cfg = resolve_llm_config("default")
    assert cfg.source == "env"
    assert cfg.model == "env-default"
    # 停用后 DB 档案整体清空 → 三级解析回到通用档
    profile = resolve_model_profile("db-model", base_url="https://db.example/v1")
    assert profile.supports_json_schema is False


def test_set_db_enabled_false_clears_registered_profiles(db_registry, env_settings):
    _seed_db(
        db_registry,
        profile={"thinking_style": "reasoning_effort", "supports_json_schema": True},
    )
    resolve_llm_config("default")
    assert resolve_model_profile("db-model", base_url="x").supports_json_schema is True
    llm_registry.set_db_enabled(False)  # conftest 同款关闸：档案一并清空
    assert resolve_model_profile("db-model", base_url="x").supports_json_schema is False


# ── TTL 与降级 ───────────────────────────────────────────────────────────────


def test_snapshot_ttl_cache_until_cleared(db_registry, env_settings):
    _seed_db(db_registry, wire_model="model-a")
    assert resolve_llm_config("default").model == "model-a"

    # TTL 内改库不生效（命中缓存）
    with db_registry() as session:
        session.add(
            LlmModel(id="m2", provider_id="p1", model_id="model-b", display_name="B")
        )
        session.get(LlmBinding, "default").model_id = "m2"
        session.commit()
    assert resolve_llm_config("default").model == "model-a"

    # 管理端写操作调用 clear_cache 后立即可见
    llm_registry.clear_cache()
    assert resolve_llm_config("default").model == "model-b"


def test_db_failure_degrades_to_env_not_raise(db_registry, env_settings, monkeypatch):
    import app.db.session as db_session

    def _boom():
        raise RuntimeError("db unreachable")

    _seed_db(db_registry)  # 先成功加载一次，再把工厂打断
    monkeypatch.setattr(db_session, "get_session_factory", _boom)
    llm_registry.clear_cache()  # at 归零 → 下次 resolve 必然重载
    cfg = resolve_llm_config("default")
    assert cfg.source == "env"  # 任何 DB 故障都降级 .env，绝不阻断出题链路
