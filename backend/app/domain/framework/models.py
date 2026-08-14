"""Assessment-led examination framework models."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    final_exam_rules: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_anchor_keys(self):
        if not self.anchors:
            raise ValueError("at least one assessment anchor is required")
        keys = [anchor.key for anchor in self.anchors]
        if len(keys) != len(set(keys)):
            raise ValueError("assessment anchor keys must be unique")
        return self


class FrameworkConflict(BaseModel):
    key: str
    kind: Literal["missing_teaching_coverage", "weight_total", "scope_conflict"]
    message: str
    status: Literal["open", "resolved"] = "open"


class FrameworkCandidate(BaseModel):
    anchors: list[AssessmentAnchor]
    teaching_topics: list[TeachingTopic]
    conflicts: list[FrameworkConflict]
    final_exam_rules: dict = Field(default_factory=dict)


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
        if any(not resolution.strip() for resolution in self.conflict_resolutions.values()):
            raise ValueError("conflict resolution must not be blank")
        return self


class SyllabusExtractor(Protocol):
    def extract_teaching(self, blocks: list[str]) -> list[TeachingTopic]: ...

    def extract_assessment(self, blocks: list[str]) -> AssessmentOutline: ...


class FrameworkRepository(Protocol):
    def persist_candidate(self, state: dict, candidate: FrameworkCandidate) -> str: ...

    def publish(self, state: dict, confirmation: FrameworkConfirmation) -> str: ...
