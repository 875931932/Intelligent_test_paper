from fastapi import APIRouter, HTTPException

from app.domain.blueprint.models import BlueprintRequest
from app.services.blueprint_service import BlueprintValidationError, allocate_plan_items

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["blueprints"])


@router.post("/blueprints/allocate")
def allocate_blueprint(course_id: str, request: BlueprintRequest) -> dict:
    try:
        return allocate_plan_items(request).model_dump(mode="json")
    except BlueprintValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
