"""Source-separated L1-L4 knowledge-tree models."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.domain.framework.exam_points import ExamPoint
from app.domain.generation.semantic_diversity import AnswerRelation, InstanceCarrier
from app.domain.knowledge.relevance import EvidenceDecision, ExamPointCoverage
from app.domain.model_calls import ModelCallContext


LEGACY_ORGANIZATION_SCHEMA_VERSION = 1
ORGANIZATION_SCHEMA_VERSION = 2


class KnowledgeCardDraft(BaseModel):
    name: str
    performance_statement: str
    assessable_content: list[str]
    scope_boundary: dict = Field(default_factory=dict)
    cognitive_targets: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)
    importance: int = Field(default=1, ge=1, le=5)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    prompt_material: list[str] = Field(default_factory=list)
    concept_cluster: str = ""
    answer_proposition: str = ""
    required_propositions: list[str] = Field(default_factory=list)
    relation_edges: list[AnswerRelation] = Field(default_factory=list)
    instance_carriers: list[InstanceCarrier] = Field(default_factory=list)
    status: Literal["active", "excluded", "material_only", "needs_teacher_review"] = "active"


class AssessmentUnitDraft(BaseModel):
    code: str
    title: str
    performance_statement: str
    exam_point_code: str = ""
    scope_boundary: dict = Field(default_factory=dict)
    cards: list[KnowledgeCardDraft] = Field(default_factory=list)
    status: Literal["active", "excluded", "needs_teacher_review"] = "active"
    # syllabus_core: 单元直接源自考核大纲的考核要求（章节核心概念层），
    # material_evidence: 单元源自教学材料的证据事实（含实验操作细节）。
    origin: Literal["syllabus_core", "material_evidence"] = "material_evidence"


class ExamPointKnowledgeConsolidator(Protocol):
    def consolidate(
        self,
        *,
        exam_point: ExamPoint,
        admitted_decisions: list[EvidenceDecision],
        call_context: ModelCallContext | None = None,
    ) -> list[AssessmentUnitDraft]:
        raise NotImplementedError


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
    coverage: list[ExamPointCoverage] = Field(default_factory=list)
    evidence_decisions: list[EvidenceDecision] = Field(default_factory=list)


class TreeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["rename_topic", "exclude_topic", "exclude_unit", "move_topic"]
    target_code: str
    value: str | None = None


class KnowledgeTreeConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[TreeOperation]
    reviewed_topic_codes: list[str]
    reviewed_exam_point_codes: list[str] = Field(default_factory=list)
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
