from __future__ import annotations

from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.config import settings
from app.domain.generation.contract import ContractSlot
from app.workflows.generation_graph import build_generation_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["generation"])
_gateway_lock = Lock()


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: list[dict] = Field(min_length=1)
    knowledge_cards: dict[str, dict] = Field(default_factory=dict)


def get_gateway(request: Request):
    gateway = getattr(request.app.state, "generation_gateway", None)
    if gateway is not None:
        return gateway
    with _gateway_lock:
        gateway = getattr(request.app.state, "generation_gateway", None)
        if gateway is not None:
            return gateway
        if not all(
            value.strip()
            for value in (
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                settings.deepseek_model,
            )
        ):
            raise HTTPException(status_code=503, detail="DeepSeek model is not configured")
        gateway = DeepSeekGateway(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )
        request.app.state.generation_gateway = gateway
        return gateway


@router.post("/generation-runs", status_code=status.HTTP_202_ACCEPTED)
def generate_paper(
    course_id: str,
    request: GenerationRequest,
    gateway=Depends(get_gateway),
) -> dict:
    # 合同在入口校验：非法 slot 直接 422，不进图
    try:
        slots = [ContractSlot.model_validate(raw) for raw in request.contract]
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"contract invalid: {exc}")
    try:
        result = build_generation_graph(gateway).invoke({
            "contract": [s.model_dump(mode="json") for s in slots],
            "knowledge_cards": request.knowledge_cards,
        })
        questions = sorted(result.get("questions", []), key=lambda q: q.get("item_index", 0))
        return {
            "status": "candidate",
            "questions": questions,
            "final_check": result.get("final_check", {}),
            "model_call_count": result.get("model_call_count", 0),
            "model": settings.deepseek_model,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"question generation failed: {exc}")
