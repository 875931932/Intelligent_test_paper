"""运行时模型配置注册表：DB 权威（管理界面切换），.env 兜底，30s TTL。

读路径（design §5.3）::

    resolve_llm_config(purpose)
        1. 查进程内快照（llm_bindings ⋈ llm_models ⋈ llm_providers，一条 SQL 整体加载）
        2. 未绑定/已停用 → 回落 purpose='default' → 仍未命中 → 回落 .env
           （.env 按 purpose 取对应变量，与迁移前语义一致）→ 仍无 → 抛未配置
        3. 快照 TTL 30s：管理端切换后 API 进程与各 worker 最长 30s 自动生效

运维开关 :func:`set_db_enabled`（默认开）：关闭后只走 .env，测试环境用它
隔离远程数据库（tests/conftest.py 全局关闭，registry 自身的测试单独打开）。

查库失败（空库/未跑 init_db/网络不可达）一律降级 .env 并按 TTL 负缓存，
模型配置读取绝不因 DB 故障阻断出题链路。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger("model.registry")

SNAPSHOT_TTL_SECONDS = 30.0

VALID_PURPOSES = ("default", "extract", "classify", "consolidate", "generation")

# .env 兜底时各 purpose 的专属变量（缺省回落 llm_model）——与迁移前
# knowledge.py「settings.llm_extract_model or settings.llm_model」口径一致。
_ENV_MODEL_VARS = {
    "default": "llm_model",
    "extract": "llm_extract_model",
    "classify": "llm_classify_model",
    "consolidate": "llm_consolidate_model",
    "generation": "llm_model",  # 暂无专用变量，走 default（设计 §5.1 表）
}


class RegistryNotConfigured(Exception):
    """DB 与 .env 都没有可用配置（空库 + 空 .env 的裸环境）。"""


@dataclass(frozen=True)
class LlmConfig:
    """一个用途的生效配置（来源标注 db/env，供管理端展示与排查）。"""

    api_key: str
    base_url: str
    model: str
    source: str  # "db" | "env"
    purpose: str
    provider_name: str = ""
    model_display_name: str = ""
    profile: dict | None = None


_lock = threading.Lock()
_db_enabled = True
# {"at": monotonic, "bindings": dict[purpose, LlmConfig] | None}；None=最近一次加载失败
_snapshot: dict = {"at": 0.0, "bindings": None}


def set_db_enabled(enabled: bool) -> None:
    """运维开关：关闭后只用 .env 兜底（测试环境隔离远程库用）。切换即清缓存。"""
    global _db_enabled
    with _lock:
        _db_enabled = bool(enabled)
        _snapshot["at"] = 0.0
        _snapshot["bindings"] = None
    if not enabled:
        # DB 档案一并清空，避免测试间串档（resolve 回到代码档案，行为确定）
        from app.adapters.model.model_profiles import register_db_profiles

        register_db_profiles({})


def clear_cache() -> None:
    with _lock:
        _snapshot["at"] = 0.0
        _snapshot["bindings"] = None


def _load_db_bindings() -> dict[str, LlmConfig] | None:
    """整体加载活跃绑定（一条 join SQL）。失败返回 None（调用方降级并负缓存）。"""
    try:
        # 动态取工厂：测试可 monkeypatch app.db.session.get_session_factory
        import app.db.session as db_session
        from sqlalchemy import select

        from app.adapters.model.model_profiles import profile_from_dict, register_db_profiles
        from app.db.schema import LlmBinding, LlmModel, LlmProvider

        stmt = (
            select(LlmBinding.purpose, LlmModel, LlmProvider)
            .join(LlmModel, LlmBinding.model_id == LlmModel.id)
            .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
            .where(LlmModel.status == "active", LlmProvider.status == "active")
        )
        with db_session.get_session_factory()() as session:
            rows = session.execute(stmt).all()
    except Exception as exc:  # noqa: BLE001 - 任何 DB 故障都降级 .env，不阻断出题
        logger.warning("llm_registry 加载 DB 绑定失败，回落 .env：%s", exc)
        return None

    bindings: dict[str, LlmConfig] = {}
    profiles = {}
    for purpose, model, provider in rows:
        if purpose not in VALID_PURPOSES:
            continue
        api_key = provider.api_key or ""
        if provider.api_key_env:
            import os

            api_key = os.getenv(provider.api_key_env, "") or api_key
        bindings[purpose] = LlmConfig(
            api_key=api_key,
            base_url=provider.base_url.rstrip("/"),
            model=model.model_id,
            source="db",
            purpose=purpose,
            provider_name=provider.name,
            model_display_name=model.display_name,
            profile=model.profile or None,
        )
        profile = profile_from_dict(model.model_id, model.profile)
        if profile is not None:
            profiles[model.model_id] = profile
    # 档案整体替换：被停用型号的旧档案随之消失（三级解析回到代码档案）
    register_db_profiles(profiles)
    return bindings


def _snapshot_bindings() -> dict[str, LlmConfig] | None:
    """TTL 快照：未过期直接用；过期则加载（失败缓存 None 负结果）。"""
    now = time.monotonic()
    with _lock:
        if now - float(_snapshot["at"]) < SNAPSHOT_TTL_SECONDS:
            return _snapshot["bindings"]
        if not _db_enabled:
            _snapshot["at"] = now
            _snapshot["bindings"] = {}
            return _snapshot["bindings"]
        # 锁内加载：加载本身可能较慢（首查/网络），但把并发收敛成单查；
        # TTL 30s 间隔内的并发请求最多等这一轮，之后全部命中缓存。
        bindings = _load_db_bindings()
        _snapshot["at"] = now
        _snapshot["bindings"] = bindings
        return bindings


def _env_config(purpose: str) -> LlmConfig | None:
    """.env 兜底：按 purpose 取专属模型变量，三件套缺一即未配置。"""
    if not (settings.llm_api_key.strip() and settings.llm_base_url.strip()):
        return None
    model_var = _ENV_MODEL_VARS.get(purpose, "llm_model")
    model = (getattr(settings, model_var, "") or "").strip() or settings.llm_model.strip()
    if not model:
        return None
    return LlmConfig(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url.rstrip("/"),
        model=model,
        source="env",
        purpose=purpose,
        provider_name=".env",
        model_display_name=model,
        profile=None,
    )


def resolve_llm_config(purpose: str = "default") -> LlmConfig:
    """解析用途的生效配置：DB 绑定（default 兜底链）→ .env → 抛未配置。"""
    if purpose not in VALID_PURPOSES:
        raise RegistryNotConfigured(f"unknown purpose: {purpose}")
    bindings = _snapshot_bindings()
    if bindings:
        cfg = bindings.get(purpose)
        if cfg is None and purpose != "default":
            cfg = bindings.get("default")  # 未细分用途的回落链（设计 §5.3）
        if cfg is not None:
            if not cfg.api_key.strip():
                # 绑定在但密钥为空（未填 key 的供应商）：视为不可用，走 .env
                logger.warning("purpose=%s 的 DB 绑定缺 api_key，回落 .env", purpose)
            else:
                return cfg
    env = _env_config(purpose)
    if env is not None:
        return env
    raise RegistryNotConfigured(f"purpose={purpose} 未配置（DB 绑定与 .env 均不可用）")


def llm_ready(purpose: str = "default") -> bool:
    """是否已有可用配置（供 router 的 503 门禁与入队前校验）。"""
    try:
        resolve_llm_config(purpose)
        return True
    except RegistryNotConfigured:
        return False
