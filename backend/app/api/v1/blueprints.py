from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.domain.blueprint.models import UnitCoverage
from app.domain.generation.contract import PaperContract
from app.services.blueprint_service import BlueprintValidationError
from app.services.contract_service import (
    ContractRequest,
    ContractRevisionError,
    allocate_paper_contract,
    apply_slot_revisions,
)

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["blueprints"])


class ContractConfirmation(BaseModel):
    contract: dict
    slot_revisions: list[dict] = Field(default_factory=list)
    units: list[UnitCoverage] = Field(default_factory=list)
    knowledge_cards: dict[str, dict] = Field(default_factory=dict)


@router.post("/blueprints/allocate")
def allocate_blueprint(course_id: str, request: ContractRequest) -> dict:
    try:
        contract = allocate_paper_contract(request)
        return contract.model_dump(mode="json")
    except BlueprintValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/blueprints/confirm")
def confirm_contract(course_id: str, request: ContractConfirmation) -> dict:
    try:
        contract = PaperContract.model_validate(request.contract)
    except Exception as exc:  # contract 反序列化失败
        raise HTTPException(status_code=422, detail=f"contract invalid: {exc}")
    try:
        revised = apply_slot_revisions(
            contract, request.slot_revisions,
            units=request.units, knowledge_cards=request.knowledge_cards,
        )
        return revised.model_dump(mode="json")
    except ContractRevisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
