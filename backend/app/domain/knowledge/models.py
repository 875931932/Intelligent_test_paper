"""Source-separated L1-L4 knowledge-tree models."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeCardDraft(BaseModel):
    name: str
    performance_statement: str
    assessable_content: list[str]
    scope_boundary: dict = Field(default_factory=dict)
    cognitive_targets: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)
    importance: int = Field(default=1, ge=1, le=5)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    status: Literal["active", "excluded", "material_only", "needs_teacher_review"] = "active"


class AssessmentUnitDraft(BaseModel):
    code: str
    title: str
    performance_statement: str
    scope_boundary: dict = Field(default_factory=dict)
    cards: list[KnowledgeCardDraft] = Field(default_factory=list)
    status: Literal["active", "excluded", "needs_teacher_review"] = "active"


class KnowledgeTopicDraft(BaseModel):
    code: str
    name: str
    framework_anchor_key: str
    units: list[AssessmentUnitDraft] = Field(default_factory=list)
    status: Literal["active", "excluded", "needs_teacher_review"] = "active"


class UnmatchedCandidate(BaseModel):
    material_version_id: str
    label: str
    reason: str


class FileKnowledgeCandidate(BaseModel):
    material_version_id: str
    topics: list[KnowledgeTopicDraft] = Field(default_factory=list)
    unmatched: list[UnmatchedCandidate] = Field(default_factory=list)


class KnowledgeTreeCandidate(BaseModel):
    framework_version_id: str
    topics: list[KnowledgeTopicDraft]
    unmatched: list[UnmatchedCandidate] = Field(default_factory=list)


class TreeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["rename_topic", "exclude_topic", "exclude_unit", "move_topic"]
    target_code: str
    value: str | None = None


class KnowledgeTreeConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[TreeOperation]
    reviewed_topic_codes: list[str]
    teacher_exclusions: list[str]


class KnowledgeFileExtractor(Protocol):
    def extract_file(
        self,
        *,
        material_version_id: str,
        blocks: list[str],
        framework_summary: dict,
    ) -> FileKnowledgeCandidate: ...


class KnowledgeRepository(Protocol):
    def persist_candidate(self, state: dict, tree: KnowledgeTreeCandidate) -> str: ...

    def publish(self, state: dict, tree: KnowledgeTreeCandidate, confirmation: KnowledgeTreeConfirmation) -> dict: ...
