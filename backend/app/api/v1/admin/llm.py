"""模型管理端点（/api/v1/admin/llm）：供应商/型号 CRUD + 用途绑定 + 连通性测试。

仅平台管理员（router 级 require_platform_admin）；org_admin 一律 403。
router 只做协议转换：入参校验 → service 调用 → 异常映射 HTTP 状态码。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.admin.deps import require_platform_admin
from app.db.session import get_session
from app.services import llm_admin_service as svc

router = APIRouter(
    prefix="/llm",
    tags=["admin-llm"],
    dependencies=[Depends(require_platform_admin)],
)


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=300)
    api_key: str | None = Field(default=None, max_length=300)
    api_key_env: str | None = Field(default=None, max_length=80)
    remark: str | None = Field(default=None, max_length=300)


class ProviderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=300)
    api_key: str | None = Field(default=None, max_length=300)
    api_key_env: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    remark: str | None = Field(default=None, max_length=300)


class ModelCreate(BaseModel):
    provider_id: str
    model_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    profile: dict = Field(default_factory=dict)
    price_input_per_mtk: float | None = Field(default=None, ge=0)
    price_output_per_mtk: float | None = Field(default=None, ge=0)


class ModelPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    profile: dict | None = None
    price_input_per_mtk: float | None = Field(default=None, ge=0)
    price_output_per_mtk: float | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class BindingPut(BaseModel):
    model_id: str


class TestRequest(BaseModel):
    provider_id: str | None = None
    model_id: str | None = None


def _map_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, svc.LlmAdminNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, svc.LlmAdminConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, svc.LlmAdminValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    raise exc


# ── 供应商 ────────────────────────────────────────────────────────────────────


@router.get("/providers")
def list_providers(session: Session = Depends(get_session)) -> list[dict]:
    return svc.list_providers(session)


@router.post("/providers", status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return svc.create_provider(
            session,
            name=payload.name.strip(),
            base_url=payload.base_url.strip(),
            api_key=payload.api_key.strip() if payload.api_key else None,
            api_key_env=payload.api_key_env.strip() if payload.api_key_env else None,
            remark=payload.remark,
        )
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)


@router.patch("/providers/{provider_id}")
def update_provider(
    provider_id: str, payload: ProviderPatch, session: Session = Depends(get_session)
) -> dict:
    try:
        return svc.update_provider(
            session,
            provider_id,
            name=payload.name,
            base_url=payload.base_url,
            api_key=payload.api_key,
            api_key_env=payload.api_key_env,
            status=payload.status,
            remark=payload.remark,
        )
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)


# ── 型号 ──────────────────────────────────────────────────────────────────────


@router.get("/models")
def list_models(session: Session = Depends(get_session)) -> list[dict]:
    return svc.list_models(session)


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(payload: ModelCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return svc.create_model(
            session,
            provider_id=payload.provider_id,
            model_id=payload.model_id.strip(),
            display_name=payload.display_name.strip(),
            profile=payload.profile,
            price_input_per_mtk=payload.price_input_per_mtk,
            price_output_per_mtk=payload.price_output_per_mtk,
        )
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)


@router.patch("/models/{model_id}")
def update_model(
    model_id: str, payload: ModelPatch, session: Session = Depends(get_session)
) -> dict:
    try:
        return svc.update_model(
            session,
            model_id,
            display_name=payload.display_name,
            profile=payload.profile,
            price_input_per_mtk=payload.price_input_per_mtk,
            price_output_per_mtk=payload.price_output_per_mtk,
            status=payload.status,
        )
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(model_id: str, session: Session = Depends(get_session)) -> None:
    try:
        svc.delete_model(session, model_id)
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)


# ── 用途绑定（一键切换） ──────────────────────────────────────────────────────


@router.get("/bindings")
def list_bindings(session: Session = Depends(get_session)) -> list[dict]:
    return svc.list_bindings(session)


@router.put("/bindings/{purpose}")
def set_binding(
    purpose: str, payload: BindingPut, session: Session = Depends(get_session)
) -> dict:
    try:
        return svc.set_binding(session, purpose=purpose, model_pk=payload.model_id)
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)


# ── 连通性测试 ────────────────────────────────────────────────────────────────


@router.post("/test")
def test_connection(payload: TestRequest, session: Session = Depends(get_session)) -> dict:
    try:
        return svc.test_connection(
            session, provider_id=payload.provider_id, model_pk=payload.model_id
        )
    except svc.LlmAdminError as exc:
        raise _map_errors(exc)
