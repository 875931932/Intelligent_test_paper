from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UnitCoverage(BaseModel):
    unit_id: str
    anchor_key: str
    card_ids: list[str] = Field(min_length=1)


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
    unit_id: str
    card_id: str
    difficulty: str = "medium"
    cognitive_level: str = "understand"


class BlueprintPlan(BaseModel):
    total_score: float
    items: list[PlanItem]
    type_counts: dict[str, int]
    difficulty_counts: dict[str, dict[str, int]]
    anchor_counts: dict[str, int]
