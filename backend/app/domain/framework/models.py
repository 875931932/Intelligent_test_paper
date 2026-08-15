"""Assessment-led examination framework models."""

from __future__ import annotations

import math
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.framework.exam_points import ExamPoint, WeightSource
from app.domain.model_calls import ModelCallContext


class TeachingTopic(BaseModel):
    key: str
    title: str
    depth: str
    requirements: list[str] = Field(default_factory=list)


class AssessmentAnchor(BaseModel):
    key: str
    title: str
    exam_weight: float = Field(ge=0, le=100)
    ability_requirements: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)
    excluded_content: list[str] = Field(default_factory=list)
    alignment_keys: list[str] = Field(default_factory=list)


class AssessmentOutline(BaseModel):
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint] = Field(min_length=1)
    final_exam_rules: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_anchor_keys(self):
        if not self.anchors:
            raise ValueError("at least one assessment anchor is required")
        keys = [anchor.key for anchor in self.anchors]
        if len(keys) != len(set(keys)):
            raise ValueError("assessment anchor keys must be unique")
        _validate_exam_points(
            self.exam_points,
            self.anchors,
            anchor_error="exam point anchor_key must belong to assessment anchors",
        )
        return self


class FrameworkConflict(BaseModel):
    key: str
    kind: Literal[
        "missing_teaching_coverage",
        "weight_total",
        "scope_conflict",
        "teaching_depth_conflict",
        "exam_point_weight_conflict",
        "exam_point_scope_conflict",
    ]
    message: str
    status: Literal["open", "resolved"] = "open"


class FrameworkCandidate(BaseModel):
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint] = Field(min_length=1)
    teaching_topics: list[TeachingTopic]
    conflicts: list[FrameworkConflict]
    final_exam_rules: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_exam_points(self):
        _validate_exam_points(
            self.exam_points,
            self.anchors,
            anchor_error="exam point anchor_key must belong to candidate anchors",
        )
        return self


class AnchorRevision(BaseModel):
    key: str
    title: str
    exam_weight: float = Field(ge=0, le=100)
    ability_requirements: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)
    excluded_content: list[str] = Field(default_factory=list)
    alignment_keys: list[str] = Field(default_factory=list)


class FrameworkConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchors: list[AnchorRevision]
    exam_points: list[ExamPoint] = Field(min_length=1)
    conflict_resolutions: dict[str, str]
    teacher_exclusions: list[str]

    @model_validator(mode="after")
    def validate_weights(self):
        if not self.anchors:
            raise ValueError("at least one assessment anchor is required")
        keys = [anchor.key for anchor in self.anchors]
        if len(keys) != len(set(keys)):
            raise ValueError("confirmed anchor keys must be unique")
        total = sum(anchor.exam_weight for anchor in self.anchors)
        if abs(total - 100) > 0.01:
            raise ValueError("confirmed assessment weights must total 100")
        _validate_exam_points(
            self.exam_points,
            self.anchors,
            anchor_error="exam point anchor_key must belong to confirmed anchors",
            require_closed_weights=True,
        )
        if any(not resolution.strip() for resolution in self.conflict_resolutions.values()):
            raise ValueError("conflict resolution must not be blank")
        return self


class SyllabusExtractor(Protocol):
    def extract_teaching(
        self,
        blocks: list[str],
        *,
        call_context: ModelCallContext | None = None,
    ) -> list[TeachingTopic]: ...

    def extract_assessment(
        self,
        blocks: list[str],
        *,
        call_context: ModelCallContext | None = None,
    ) -> AssessmentOutline: ...


class FrameworkRepository(Protocol):
    def persist_candidate(self, state: dict, candidate: FrameworkCandidate) -> str: ...

    def publish(self, state: dict, confirmation: FrameworkConfirmation) -> str: ...


def _validate_exam_points(
    exam_points: list[ExamPoint],
    anchors: list[AssessmentAnchor | AnchorRevision],
    *,
    anchor_error: str,
    require_closed_weights: bool = False,
) -> None:
    codes = [point.code for point in exam_points]
    if len(codes) != len(set(codes)):
        raise ValueError("exam point codes must be unique")
    anchors_by_key = {anchor.key: anchor for anchor in anchors}
    explicit_weight_by_anchor: dict[str, float] = {}
    for point in exam_points:
        parent = anchors_by_key.get(point.anchor_key)
        if parent is None:
            raise ValueError(anchor_error)
        if point.weight_source != WeightSource.INHERITED_GROUP:
            explicit_weight_by_anchor[point.anchor_key] = (
                explicit_weight_by_anchor.get(point.anchor_key, 0) + point.weight_value
            )
    if any(
        weight > anchors_by_key[anchor_key].exam_weight + 0.011
        for anchor_key, weight in explicit_weight_by_anchor.items()
    ):
        raise ValueError("explicit exam point weight must not exceed parent anchor weight")
    if require_closed_weights:
        points_by_anchor: dict[str, list[ExamPoint]] = {}
        for point in exam_points:
            points_by_anchor.setdefault(point.anchor_key, []).append(point)
        for anchor in anchors:
            points = points_by_anchor.get(anchor.key, [])
            if any(point.weight_source == WeightSource.INHERITED_GROUP for point in points):
                continue
            total = sum(point.weight_value for point in points)
            if not math.isclose(total, anchor.exam_weight, rel_tol=0, abs_tol=0.011):
                raise ValueError("explicit exam point weights must equal parent anchor weight")
