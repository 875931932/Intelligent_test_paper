from __future__ import annotations

from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.config import settings
from app.db.session import get_session
from app.domain.generation.structure_signature import load_recent_structure_signatures
from app.workflows.generation_graph import build_generation_graph

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["generation"])
_gateway_lock = Lock()


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_items: list[dict]
    knowledge_cards: dict[str, dict]


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
    session: Session = Depends(get_session),
) -> dict:
    try:
        recent_signatures = load_recent_structure_signatures(session, course_id, paper_limit=5)
        initial_state = request.model_dump()
        initial_state["recent_structure_signatures"] = [signature.model_dump() for signature in recent_signatures]
        result = build_generation_graph(gateway).invoke(initial_state)
        return {"status": "candidate", "questions": result["questions"], "model": settings.deepseek_model}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"question generation failed: {exc}")
