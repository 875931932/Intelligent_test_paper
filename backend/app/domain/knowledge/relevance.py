"""Contracts and deterministic admission for exam-point evidence relevance."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from app.domain.framework.exam_points import ExamPoint, OperationalDetailPolicy
from app.domain.model_calls import ModelCallContext


class StagingChunk(BaseModel):
    """A parsed material chunk that has not yet been published to the knowledge base."""

    id: str
    material_version_id: str
    content: str = Field(min_length=1)
    locator: dict[str, Any] = Field(default_factory=dict)


class RelevanceClass(StrEnum):
    DIRECT = "direct"
    SUPPORTING = "supporting"
    BACKGROUND = "background"
    OUT_OF_SCOPE = "out_of_scope"


class EvidenceDecision(BaseModel):
    exam_point_code: str
    evidence_chunk_id: str
    relevance_class: RelevanceClass
    support_claim: str
    evidence_role: str | None = None
    content_kind: str
    candidate_assessment_unit: dict[str, Any] | None = None
    candidate_card_content: dict[str, Any] | None = None
    prompt_material: str | None = None
    confidence: int = Field(ge=0, le=100)

    @field_validator(
        "exam_point_code",
        "evidence_chunk_id",
        "support_claim",
        "content_kind",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("evidence_role", "prompt_material")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ExamPointFileDecision(BaseModel):
    exam_point_code: str = Field(min_length=1)
    material_version_id: str = Field(min_length=1)
    decisions: list[EvidenceDecision]


class ExamPointCoverage(BaseModel):
    exam_point_code: str = Field(min_length=1)
    direct_count: int = Field(ge=0)
    supporting_count: int = Field(ge=0)
    background_count: int = Field(ge=0)
    out_of_scope_count: int = Field(ge=0)
    status: Literal["sufficient", "insufficient", "conflicting"]
    reasons: list[str] = Field(default_factory=list)


class ExamPointEvidenceClassifier(Protocol):
    def classify(
        self,
        *,
        exam_point: ExamPoint,
        material_version_id: str,
        chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> ExamPointFileDecision: ...


MINIMUM_ADMISSION_CONFIDENCE = 50

_OPERATIONAL_CONTENT_KINDS = frozenset(
    {
        "operational_detail",
        "procedural_step",
        "command_or_configuration",
        "installation_or_environment",
        "file_or_path",
    }
)

_DIRECT_EVIDENCE_ROLE_MARKERS = (
    "fact",
    "definition",
    "constraint",
    "principle",
    "answer",
    "rubric",
    "scoring",
    "worked_example",
    "事实",
    "定义",
    "约束",
    "原理",
    "答案",
    "评分",
)


def _is_direct_evidence_role(
    role: str | None,
    *,
    required_roles: list[str],
) -> bool:
    if role is None:
        return False
    normalized = role.strip().casefold()
    normalized_required = {item.strip().casefold() for item in required_roles}
    return bool(normalized) and (
        normalized in normalized_required
        or any(marker in normalized for marker in _DIRECT_EVIDENCE_ROLE_MARKERS)
    )


def _without_products(
    decision: EvidenceDecision,
    *,
    relevance_class: RelevanceClass,
    keep_prompt_material: bool,
) -> EvidenceDecision:
    return decision.model_copy(
        update={
            "relevance_class": relevance_class,
            "candidate_assessment_unit": None,
            "candidate_card_content": None,
            "prompt_material": (
                decision.prompt_material if keep_prompt_material else None
            ),
        }
    )


def admit_evidence_decision(
    point: ExamPoint,
    decision: EvidenceDecision,
) -> EvidenceDecision:
    """Fail closed on malformed decisions and normalize policy-limited products."""

    if decision.exam_point_code != point.code:
        raise ValueError("exam point code does not match the requested exam point")
    if decision.confidence < MINIMUM_ADMISSION_CONFIDENCE:
        raise ValueError("confidence is below the evidence admission threshold")

    if decision.relevance_class in {
        RelevanceClass.BACKGROUND,
        RelevanceClass.OUT_OF_SCOPE,
    }:
        return _without_products(
            decision,
            relevance_class=decision.relevance_class,
            keep_prompt_material=False,
        )

    if decision.relevance_class is RelevanceClass.SUPPORTING:
        return _without_products(
            decision,
            relevance_class=RelevanceClass.SUPPORTING,
            keep_prompt_material=True,
        )

    if decision.content_kind.casefold() in _OPERATIONAL_CONTENT_KINDS:
        if point.operational_detail_policy is OperationalDetailPolicy.FORBIDDEN:
            return _without_products(
                decision,
                relevance_class=RelevanceClass.OUT_OF_SCOPE,
                keep_prompt_material=False,
            )
        if (
            point.operational_detail_policy
            is OperationalDetailPolicy.SUPPORTING_ONLY
        ):
            return _without_products(
                decision,
                relevance_class=RelevanceClass.SUPPORTING,
                keep_prompt_material=True,
            )

    if not decision.candidate_assessment_unit:
        raise ValueError("direct evidence requires an assessment unit candidate")
    if not decision.candidate_card_content:
        raise ValueError("direct evidence requires a card candidate")
    if not _is_direct_evidence_role(
        decision.evidence_role,
        required_roles=point.required_evidence_roles,
    ):
        raise ValueError("direct evidence requires a fact or rubric evidence role")

    return decision.model_copy(deep=True)
