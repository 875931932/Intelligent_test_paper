"""Assessment-led exam point contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class WeightSource(StrEnum):
    ASSESSMENT_SYLLABUS = "assessment_syllabus"
    INHERITED_GROUP = "inherited_group"
    TEACHER_CONFIRMED = "teacher_confirmed"


class OperationalDetailPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    SUPPORTING_ONLY = "supporting_only"
    DIRECTLY_ASSESSABLE = "directly_assessable"


class ExamPoint(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    anchor_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=255)
    assessment_requirement: str
    weight_value: float = Field(ge=0, le=100)
    weight_source: WeightSource
    weight_group_id: str = Field(min_length=1, max_length=160)
    priority: str = "normal"
    cognitive_targets: list[str] = Field(default_factory=list)
    assessment_orientations: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)
    operational_detail_policy: OperationalDetailPolicy = OperationalDetailPolicy.SUPPORTING_ONLY
    scope_boundary: dict[str, Any] = Field(default_factory=dict)
    required_evidence_roles: list[str] = Field(default_factory=list)
    retrieval_intent: str
    assessment_anchor_keys: list[str] = Field(default_factory=list)
    teaching_anchor_keys: list[str] = Field(default_factory=list)
    status: str = "candidate"

    @field_validator("assessment_requirement", "retrieval_intent")
    @classmethod
    def validate_required_narrative(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value
