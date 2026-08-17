from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from app.domain.generation.semantic_diversity import AnswerRelation, CardSemanticProfile


ASSESSMENT_MODES = (
    "theory_recall",
    "conceptual",
    "application",
    "problem_solving",
    "practical_operation",
)

AssessmentMode: TypeAlias = Literal[
    "theory_recall",
    "conceptual",
    "application",
    "problem_solving",
    "practical_operation",
]
OperationalDetailPolicy: TypeAlias = Literal[
    "forbidden",
    "supporting_only",
    "directly_assessable",
]


class UnitCoverage(BaseModel):
    unit_id: str
    exam_point_id: str = ""
    anchor_key: str
    card_ids: list[str] = Field(min_length=1)
    allowed_assessment_modes: list[AssessmentMode] = Field(
        default_factory=lambda: [
            "theory_recall",
            "conceptual",
            "application",
            "problem_solving",
        ]
    )
    operational_detail_policy: OperationalDetailPolicy = "supporting_only"
    # 核心概念单元（源自考纲考核要求）在选题时优先于材料证据单元，
    # 避免整卷被实验操作细节占满。
    core: bool = False


class BlueprintRequest(BaseModel):
    total_score: float = Field(gt=0)
    type_rules: dict[str, dict[str, Any]]
    chapter_weights: dict[str, float]
    units: list[UnitCoverage] = Field(min_length=1)
    card_question_types: dict[str, list[str]] = Field(default_factory=dict)
    card_semantic_profiles: dict[str, CardSemanticProfile] = Field(default_factory=dict)


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
    assessment_mode: AssessmentMode = "conceptual"
    concept_cluster: str = ""
    answer_proposition: str = ""
    required_propositions: list[str] = Field(default_factory=list)
    relation_edges: list[AnswerRelation] = Field(default_factory=list)
    instance_carriers: list[str] = Field(default_factory=list)


class BlueprintPlan(BaseModel):
    total_score: float
    items: list[PlanItem]
    type_counts: dict[str, int]
    difficulty_counts: dict[str, dict[str, int]]
    anchor_counts: dict[str, int]
    assessment_mode_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    cognitive_level_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
