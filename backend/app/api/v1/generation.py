from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.config import settings
from app.workflows.generation_graph import build_generation_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["generation"])


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_items: list[dict]
    knowledge_cards: dict[str, dict]


def get_gateway(request: Request):
    gateway = getattr(request.app.state, "generation_gateway", None)
    if gateway is not None:
        return gateway
    if not settings.deepseek_api_key.strip():
        raise HTTPException(status_code=503, detail="DeepSeek model is not configured")
    gateway = DeepSeekGateway(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, model=settings.deepseek_model)
    request.app.state.generation_gateway = gateway
    return gateway


@router.post("/generation-runs", status_code=status.HTTP_202_ACCEPTED)
def generate_paper(course_id: str, request: GenerationRequest, gateway=Depends(get_gateway)) -> dict:
    try:
        result = build_generation_graph(gateway).invoke(request.model_dump())
        return {"status": "candidate", "questions": result["questions"], "model": settings.deepseek_model}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"question generation failed: {exc}")
