"""Contracts and deterministic admission for exam-point evidence relevance."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class ContentKind(StrEnum):
    CONCEPT = "concept"
    DEFINITION = "definition"
    PRINCIPLE = "principle"
    MECHANISM = "mechanism"
    RULE = "rule"
    RELATIONSHIP = "relationship"
    FACT = "fact"
    CONSTRAINT = "constraint"
    FORMULA = "formula"
    DERIVATION = "derivation"
    COMPARISON = "comparison"
    CASE = "case"
    SCENARIO = "scenario"
    DIAGNOSTIC = "diagnostic"
    OPERATIONAL_DETAIL = "operational_detail"
    BACKGROUND = "background"


_CONTENT_KIND_ALIASES = {
    "conceptual": ContentKind.CONCEPT,
    "conceptual_fact": ContentKind.FACT,
    "concept_fact": ContentKind.FACT,
    "command": ContentKind.OPERATIONAL_DETAIL,
    "command_or_configuration": ContentKind.OPERATIONAL_DETAIL,
    "configuration": ContentKind.OPERATIONAL_DETAIL,
    "installation_step": ContentKind.OPERATIONAL_DETAIL,
    "installation_or_environment": ContentKind.OPERATIONAL_DETAIL,
    "environment_setup": ContentKind.OPERATIONAL_DETAIL,
    "path": ContentKind.OPERATIONAL_DETAIL,
    "file": ContentKind.OPERATIONAL_DETAIL,
    "filename": ContentKind.OPERATIONAL_DETAIL,
    "file_or_path": ContentKind.OPERATIONAL_DETAIL,
    "procedure": ContentKind.OPERATIONAL_DETAIL,
    "procedural_step": ContentKind.OPERATIONAL_DETAIL,
    "operation": ContentKind.OPERATIONAL_DETAIL,
    "概念": ContentKind.CONCEPT,
    "定义": ContentKind.DEFINITION,
    "原理": ContentKind.PRINCIPLE,
    "机制": ContentKind.MECHANISM,
    "规则": ContentKind.RULE,
    "关系": ContentKind.RELATIONSHIP,
    "事实": ContentKind.FACT,
    "约束": ContentKind.CONSTRAINT,
    "公式": ContentKind.FORMULA,
    "推导": ContentKind.DERIVATION,
    "比较": ContentKind.COMPARISON,
    "对比": ContentKind.COMPARISON,
    "案例": ContentKind.CASE,
    "场景": ContentKind.SCENARIO,
    "诊断": ContentKind.DIAGNOSTIC,
    "背景": ContentKind.BACKGROUND,
    "命令": ContentKind.OPERATIONAL_DETAIL,
    "配置": ContentKind.OPERATIONAL_DETAIL,
    "安装步骤": ContentKind.OPERATIONAL_DETAIL,
    "环境配置": ContentKind.OPERATIONAL_DETAIL,
    "路径": ContentKind.OPERATIONAL_DETAIL,
    "文件": ContentKind.OPERATIONAL_DETAIL,
    "文件名": ContentKind.OPERATIONAL_DETAIL,
    "操作": ContentKind.OPERATIONAL_DETAIL,
    "操作步骤": ContentKind.OPERATIONAL_DETAIL,
    "操作细节": ContentKind.OPERATIONAL_DETAIL,
}


class AssessmentUnitCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    title: str
    performance_statement: str
    scope_boundary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", "title", "performance_statement")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class KnowledgeCardCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    performance_statement: str
    assessable_content: list[str]
    scope_boundary: dict[str, Any] = Field(default_factory=dict)
    cognitive_targets: list[str] = Field(default_factory=list)
    allowed_question_types: list[str] = Field(default_factory=list)

    @field_validator("name", "performance_statement")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("assessable_content")
    @classmethod
    def validate_assessable_content(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if not normalized or any(not value for value in normalized):
            raise ValueError("assessable_content must contain non-blank facts")
        return normalized


class EvidenceDecision(BaseModel):
    exam_point_code: str
    evidence_chunk_id: str
    relevance_class: RelevanceClass
    support_claim: str
    evidence_role: str | None = None
    content_kind: ContentKind
    candidate_assessment_unit: AssessmentUnitCandidate | None = None
    candidate_card_content: KnowledgeCardCandidate | None = None
    prompt_material: str | None = None
    confidence: int = Field(ge=0, le=100)

    @field_validator(
        "exam_point_code",
        "evidence_chunk_id",
        "support_claim",
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

    @field_validator("content_kind", mode="before")
    @classmethod
    def normalize_content_kind(cls, value: object) -> object:
        if isinstance(value, ContentKind):
            return value
        if not isinstance(value, str):
            return value
        normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
        return _CONTENT_KIND_ALIASES.get(normalized, normalized)


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

DIRECT_EVIDENCE_ROLES = frozenset(
    {
        "fact",
        "definition",
        "fact_or_definition",
        "principle",
        "relationship",
        "causal_relationship",
        "constraint",
        "fact_or_constraint",
        "comparison_basis",
        "formula",
        "derivation",
        "formula_or_derivation",
        "diagnostic_basis",
        "worked_example",
        "answer",
        "answer_basis",
        "rubric",
        "rubric_basis",
        "answer_or_rubric_basis",
        "scoring",
        "scoring_basis",
    }
)


def validate_direct_evidence_decision(
    decision: EvidenceDecision,
    *,
    exam_point_code: str,
) -> None:
    """Validate the source-independent shape shared by admission and publish."""

    if decision.exam_point_code != exam_point_code:
        raise ValueError("exam point code does not match the requested exam point")
    if decision.relevance_class is not RelevanceClass.DIRECT:
        raise ValueError("evidence decision is not direct")
    if decision.confidence < MINIMUM_ADMISSION_CONFIDENCE:
        raise ValueError("confidence is below the evidence admission threshold")
    if not decision.evidence_chunk_id.strip():
        raise ValueError("direct evidence requires an evidence chunk id")
    if not decision.support_claim.strip():
        raise ValueError("direct evidence requires a support claim and content kind")
    if not decision.candidate_assessment_unit:
        raise ValueError("direct evidence requires an assessment unit candidate")
    if not decision.candidate_card_content:
        raise ValueError("direct evidence requires a card candidate")
    normalized_role = (decision.evidence_role or "").strip().casefold()
    if normalized_role not in DIRECT_EVIDENCE_ROLES:
        raise ValueError("direct evidence requires a fact or rubric evidence role")


_FACT_OPERATOR_TOKENS = (
    ("<=>", " operator_iff "),
    ("↔", " operator_iff "),
    ("=>", " operator_implies "),
    ("->", " operator_implies "),
    ("→", " operator_implies "),
    ("&&", " operator_and "),
    ("∧", " operator_and "),
    ("||", " operator_or "),
    ("∨", " operator_or "),
    (">=", " operator_greater_or_equal "),
    ("<=", " operator_less_or_equal "),
    ("!=", " operator_not_equal "),
    ("==", " operator_equal "),
    ("≥", " operator_greater_or_equal "),
    ("≤", " operator_less_or_equal "),
    ("≠", " operator_not_equal "),
    ("!", " operator_not "),
    ("¬", " operator_not "),
    (">", " operator_greater_than "),
    ("<", " operator_less_than "),
    ("=", " operator_equal "),
    ("+", " operator_plus "),
    ("-", " operator_minus "),
    ("*", " operator_multiply "),
    ("×", " operator_multiply "),
    ("/", " operator_divide "),
    ("÷", " operator_divide "),
)


def _fact_key(value: str) -> str:
    normalized = value.casefold()
    for operator, token in _FACT_OPERATOR_TOKENS:
        normalized = normalized.replace(operator, token)
    return re.sub(
        r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+",
        "",
        normalized,
    )


def assessable_fact_keys(values: list[str]) -> frozenset[str]:
    """Split safe fact boundaries and preserve comparison/math semantics."""

    return frozenset(
        key
        for value in values
        for atom in re.split(r"[;；\r\n]+", value)
        if (key := _fact_key(atom.strip()))
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

    if (
        decision.content_kind is ContentKind.BACKGROUND
        and decision.relevance_class is not RelevanceClass.OUT_OF_SCOPE
    ):
        return _without_products(
            decision,
            relevance_class=RelevanceClass.BACKGROUND,
            keep_prompt_material=False,
        )

    if decision.content_kind is ContentKind.OPERATIONAL_DETAIL:
        if point.operational_detail_policy is OperationalDetailPolicy.FORBIDDEN:
            return _without_products(
                decision,
                relevance_class=RelevanceClass.OUT_OF_SCOPE,
                keep_prompt_material=False,
            )
        if (
            point.operational_detail_policy
            is OperationalDetailPolicy.SUPPORTING_ONLY
            and decision.relevance_class
            not in {RelevanceClass.BACKGROUND, RelevanceClass.OUT_OF_SCOPE}
        ):
            return _without_products(
                decision,
                relevance_class=RelevanceClass.SUPPORTING,
                keep_prompt_material=True,
            )

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

    validate_direct_evidence_decision(decision, exam_point_code=point.code)

    return decision.model_copy(deep=True)
