from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


ASSESSMENT_MODES = (
    "theory_recall",
    "conceptual",
    "application",
    "problem_solving",
    "practical_operation",
)


class UnitCoverage(BaseModel):
    unit_id: str
    exam_point_id: str = ""
    anchor_key: str
    card_ids: list[str] = Field(min_length=1)
    allowed_assessment_modes: list[str] = Field(
        default_factory=lambda: [
            "theory_recall",
            "conceptual",
            "application",
            "problem_solving",
        ]
    )
    operational_detail_policy: str = "supporting_only"


class BlueprintRequest(BaseModel):
    total_score: float = Field(gt=0)
    type_rules: dict[str, dict[str, Any]]
    chapter_weights: dict[str, float]
    units: list[UnitCoverage] = Field(min_length=1)
    card_question_types: dict[str, list[str]] = Field(default_factory=dict)


class PlanItem(BaseModel):
    item_index: int
    question_type: str
    score: float
    anchor_key: str
    exam_point_id: str = ""
    unit_id: str
    card_id: str
    difficulty: str = "medium"
    cognitive_level: str = "understand"
    assessment_mode: str = "conceptual"


class BlueprintPlan(BaseModel):
    total_score: float
    items: list[PlanItem]
    type_counts: dict[str, int]
    difficulty_counts: dict[str, dict[str, int]]
    anchor_counts: dict[str, int]
    assessment_mode_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
