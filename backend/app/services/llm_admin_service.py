"""模型管理业务逻辑：供应商/型号 CRUD、用途绑定、连通性测试（设计 §5.5）。

平台专属（org_admin 不可触达，router 级 require_platform_admin 把关）。
密钥纪律：api_key 明文只落 DB 列，任何读接口只回掩码；api_key_env 指向
环境变量名时运行时优先取环境变量（不入库方案）。

写操作后统一 :func:`app.services.llm_registry.clear_cache`——只清本进程
快照让管理端立即看到新值；各 worker 进程按各自 30s TTL 自动刷新（设计 §5.3）。
"""

from __future__ import annotations

import logging
import os
import time
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.schema import LlmBinding, LlmModel, LlmProvider
from app.services import llm_registry
from app.services.llm_registry import VALID_PURPOSES, RegistryNotConfigured, resolve_llm_config

logger = logging.getLogger("model.admin")


class LlmAdminError(Exception):
    """模型管理异常基类（router 按子类映射 404/409/400）。"""


class LlmAdminNotFound(LlmAdminError):
    """目标资源不存在。"""


class LlmAdminConflict(LlmAdminError):
    """引用冲突/状态冲突（被绑定引用的型号、重复 base_url+model 等）。"""


class LlmAdminValidationError(LlmAdminError):
    """入参不合业务规则（未启用目标、非法 purpose 等）。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def mask_key(key: str | None) -> str | None:
    """密钥掩码：只回前 3 后 4（短值整体打码）。读接口唯一出口。"""
    if not key:
        return None
    if len(key) < 12:
        return "***"
    return f"{key[:3]}***{key[-4:]}"


def _provider_key(provider: LlmProvider) -> str:
    """运行时实际使用的密钥：api_key_env 指向环境变量时优先取环境变量。"""
    if provider.api_key_env:
        return os.getenv(provider.api_key_env, "") or (provider.api_key or "")
    return provider.api_key or ""


def _provider_dict(provider: LlmProvider) -> dict:
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "api_key_masked": mask_key(_provider_key(provider)) if _provider_key(provider) else None,
        "api_key_env": provider.api_key_env,
        "status": provider.status,
        "remark": provider.remark,
    }


def _model_dict(model: LlmModel, provider_name: str | None = None) -> dict:
    return {
        "id": model.id,
        "provider_id": model.provider_id,
        "provider_name": provider_name,
        "model_id": model.model_id,
        "display_name": model.display_name,
        "profile": model.profile or {},
        "price_input_per_mtk": model.price_input_per_mtk,
        "price_output_per_mtk": model.price_output_per_mtk,
        "status": model.status,
    }


# ── 供应商 ────────────────────────────────────────────────────────────────────


def list_providers(session: Session) -> list[dict]:
    providers = session.scalars(select(LlmProvider).order_by(LlmProvider.name)).all()
    return [_provider_dict(p) for p in providers]


def create_provider(
    session: Session,
    *,
    name: str,
    base_url: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    remark: str | None = None,
) -> dict:
    if not api_key and not api_key_env:
        raise LlmAdminValidationError("必须提供 api_key 或 api_key_env（二选一）")
    provider = LlmProvider(
        id=_new_id("llmprov"),
        name=name,
        base_url=base_url.rstrip("/"),
        api_key=api_key or None,
        api_key_env=api_key_env or None,
        remark=remark or None,
    )
    session.add(provider)
    session.commit()
    logger.info("创建供应商 %s (%s)", provider.id, provider.name)
    return _provider_dict(provider)


def update_provider(
    session: Session,
    provider_id: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    status: str | None = None,
    remark: str | None = None,
) -> dict:
    provider = session.get(LlmProvider, provider_id)
    if provider is None:
        raise LlmAdminNotFound("供应商不存在")
    if name is not None:
        provider.name = name
    if base_url is not None:
        provider.base_url = base_url.rstrip("/")
    if api_key is not None:
        # 空串 = 清空密钥；非空 = 覆盖。只在显式传字段时生效。
        provider.api_key = api_key or None
    if api_key_env is not None:
        provider.api_key_env = api_key_env or None
    if status is not None:
        provider.status = status
    if remark is not None:
        provider.remark = remark or None
    session.commit()
    logger.info("更新供应商 %s（status=%s）", provider.id, provider.status)
    llm_registry.clear_cache()
    return _provider_dict(provider)


# ── 型号 ──────────────────────────────────────────────────────────────────────


def list_models(session: Session) -> list[dict]:
    models = session.scalars(select(LlmModel).order_by(LlmModel.model_id)).all()
    provider_names = {
        p.id: p.name for p in session.scalars(select(LlmProvider)).all()
    }
    return [_model_dict(m, provider_names.get(m.provider_id)) for m in models]


def create_model(
    session: Session,
    *,
    provider_id: str,
    model_id: str,
    display_name: str,
    profile: dict | None = None,
    price_input_per_mtk: float | None = None,
    price_output_per_mtk: float | None = None,
) -> dict:
    provider = session.get(LlmProvider, provider_id)
    if provider is None:
        raise LlmAdminNotFound("供应商不存在")
    dup = session.scalar(
        select(LlmModel).where(LlmModel.provider_id == provider_id, LlmModel.model_id == model_id)
    )
    if dup is not None:
        raise LlmAdminConflict("该供应商下型号已存在")
    model = LlmModel(
        id=_new_id("llmmodel"),
        provider_id=provider_id,
        model_id=model_id,
        display_name=display_name,
        profile=profile or {},
        price_input_per_mtk=price_input_per_mtk,
        price_output_per_mtk=price_output_per_mtk,
    )
    session.add(model)
    session.commit()
    logger.info("创建型号 %s (%s @ %s)", model.id, model_id, provider_id)
    llm_registry.clear_cache()
    return _model_dict(model, provider.name if provider else None)


def update_model(
    session: Session,
    model_pk: str,
    *,
    display_name: str | None = None,
    profile: dict | None = None,
    price_input_per_mtk: float | None = None,
    price_output_per_mtk: float | None = None,
    status: str | None = None,
) -> dict:
    model = session.get(LlmModel, model_pk)
    if model is None:
        raise LlmAdminNotFound("型号不存在")
    provider = session.get(LlmProvider, model.provider_id)
    if display_name is not None:
        model.display_name = display_name
    if profile is not None:
        model.profile = profile
    if price_input_per_mtk is not None:
        model.price_input_per_mtk = price_input_per_mtk
    if price_output_per_mtk is not None:
        model.price_output_per_mtk = price_output_per_mtk
    if status is not None:
        model.status = status
    session.commit()
    logger.info("更新型号 %s（status=%s）", model_pk, model.status)
    llm_registry.clear_cache()
    return _model_dict(model, provider.name if provider else None)


def delete_model(session: Session, model_pk: str) -> None:
    """型号可真删：先查未被绑定引用；被引用则 409，只能先停用或改绑。"""
    model = session.get(LlmModel, model_pk)
    if model is None:
        raise LlmAdminNotFound("型号不存在")
    binding = session.scalar(select(LlmBinding).where(LlmBinding.model_id == model_pk))
    if binding is not None:
        raise LlmAdminConflict(
            f"型号正被用途「{binding.purpose}」引用，请先改绑或停用（停用取代删除）"
        )
    session.delete(model)
    session.commit()
    logger.info("删除型号 %s (%s)", model_pk, model.model_id)
    llm_registry.clear_cache()


# ── 用途绑定 ──────────────────────────────────────────────────────────────────


def _binding_rows(session: Session) -> dict[str, dict]:
    rows = session.execute(
        select(LlmBinding, LlmModel, LlmProvider)
        .join(LlmModel, LlmBinding.model_id == LlmModel.id)
        .join(LlmProvider, LlmModel.provider_id == LlmProvider.id)
    ).all()
    return {
        binding.purpose: {
            "purpose": binding.purpose,
            "source": "db",
            "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
            "model": _model_dict(model, provider.name),
            "provider": {"id": provider.id, "name": provider.name, "base_url": provider.base_url},
        }
        for binding, model, provider in rows
    }


def _effective(session: Session, purpose: str) -> dict:
    """某用途的生效配置（与 registry 同一回落链，但从 session 现查、无 TTL 陈旧）。"""
    rows = _binding_rows(session)
    if purpose in rows:
        return rows[purpose]
    if "default" in rows and purpose != "default":
        # 未细分用途回落 db default：purpose 标成本请求的用途，via 标注回落来源
        return {**rows["default"], "purpose": purpose, "via": "default"}
    # DB 无绑定 → .env 兜底（settings 现读，无缓存）
    try:
        cfg = resolve_llm_config(purpose)
    except RegistryNotConfigured:
        return {"purpose": purpose, "source": "none", "model": None}
    if cfg.source == "env":
        return {
            "purpose": purpose,
            "source": "env",
            "model": {"model_id": cfg.model, "display_name": cfg.model},
            "provider": {"name": ".env", "base_url": cfg.base_url},
        }
    return {"purpose": purpose, "source": "db", "model": {"model_id": cfg.model}}


def list_bindings(session: Session) -> list[dict]:
    """五类用途一览：各自绑定行 + 生效配置（db / db(default) / env / none）。"""
    return [_effective(session, purpose) for purpose in VALID_PURPOSES]


def set_binding(session: Session, *, purpose: str, model_pk: str) -> dict:
    """一键切换：校验目标型号与其供应商均启用后写入（或替换）绑定行。"""
    if purpose not in VALID_PURPOSES:
        raise LlmAdminValidationError(f"未知用途：{purpose}")
    model = session.get(LlmModel, model_pk)
    if model is None:
        raise LlmAdminNotFound("型号不存在")
    if model.status != "active":
        raise LlmAdminValidationError("目标型号未启用，无法绑定")
    provider = session.get(LlmProvider, model.provider_id)
    if provider is None or provider.status != "active":
        raise LlmAdminValidationError("目标供应商未启用，无法绑定")

    binding = session.get(LlmBinding, purpose)
    previous = None
    if binding is not None:
        previous = binding.model_id
        binding.model_id = model_pk
        binding.updated_at = _utcnow()
    else:
        session.add(LlmBinding(purpose=purpose, model_id=model_pk))
    session.commit()
    llm_registry.clear_cache()
    logger.info(
        "用途切换 purpose=%s：%s → %s（%s）",
        purpose, previous or "(env兜底)", model_pk, model.model_id,
    )
    return _effective(session, purpose)


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)


# ── 连通性测试 ────────────────────────────────────────────────────────────────


def _probe(base_url: str, api_key: str, model: str) -> dict:
    """最小 chat 探测：max_tokens=1、10s 硬超时、同步单并发（管理面短探测）。"""
    started = time.monotonic()
    if not (base_url and api_key and model):
        return {"ok": False, "latency_ms": 0, "error": "缺少 base_url / api_key / model"}
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            resp = client.post(
                url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
        latency = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            return {
                "ok": False,
                "latency_ms": latency,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        return {"ok": True, "latency_ms": latency}
    except httpx.TimeoutException:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": "超时（>10s）",
        }
    except Exception as exc:  # noqa: BLE001 - 探测要把任何网络错误原样报给管理端
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc)[:200],
        }


def test_connection(
    session: Session, *, provider_id: str | None = None, model_pk: str | None = None
) -> dict:
    """body {provider_id?, model_id?}：
    - 指定型号 → 测该型号所在供应商端点；
    - 只指定供应商 → 测其首个启用型号（无型号则用 settings.llm_model）；
    - 都不给 → 自检现行 default 生效配置（DB 优先/.env 兜底）。
    """
    if model_pk:
        model = session.get(LlmModel, model_pk)
        if model is None:
            raise LlmAdminNotFound("型号不存在")
        provider = session.get(LlmProvider, model.provider_id)
        if provider is None:
            raise LlmAdminNotFound("供应商不存在")
        return _probe(provider.base_url, _provider_key(provider), model.model_id)

    if provider_id:
        provider = session.get(LlmProvider, provider_id)
        if provider is None:
            raise LlmAdminNotFound("供应商不存在")
        fallback = session.scalar(
            select(LlmModel)
            .where(LlmModel.provider_id == provider_id, LlmModel.status == "active")
            .limit(1)
        )
        model_name = fallback.model_id if fallback is not None else settings.llm_model
        return _probe(provider.base_url, _provider_key(provider), model_name)

    try:
        cfg = resolve_llm_config("default")
    except RegistryNotConfigured:
        return {"ok": False, "latency_ms": 0, "error": "未配置任何模型（DB 与 .env 均为空）"}
    return _probe(cfg.base_url, cfg.api_key, cfg.model)
