"""Strict DeepSeek adapters for syllabus-led semantic curation."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.adapters.model.deepseek_gateway import DeepSeekJsonClient, DeepSeekModelError
from app.domain.framework.exam_points import ExamPoint
from app.domain.framework.models import AssessmentAnchor, AssessmentOutline, TeachingTopic
from app.domain.knowledge.models import AssessmentUnitDraft
from app.domain.knowledge.relevance import (
    EvidenceDecision,
    ExamPointFileDecision,
    RelevanceClass,
    StagingChunk,
    assessable_fact_keys,
)
from app.domain.model_calls import ModelCallContext


class JsonRequester(Protocol):
    def request_json(
        self,
        *,
        system_prompt: str,
        payload: Any,
        temperature: float,
        call_context: ModelCallContext | None = None,
    ) -> dict: ...


class _TeachingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teaching_topics: list[TeachingTopic]


class _AssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchors: list[AssessmentAnchor]
    exam_points: list[ExamPoint] = Field(min_length=1)
    final_exam_rules: dict[str, Any] = Field(default_factory=dict)


class _EvidenceDecisionResponse(EvidenceDecision):
    model_config = ConfigDict(extra="forbid")
    source_locator: dict[str, Any] = Field(default_factory=dict)


class _ClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_point_code: str
    material_version_id: str
    decisions: list[_EvidenceDecisionResponse]
    source_locations: list[dict[str, Any]] = Field(default_factory=list)


class _ConsolidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_point_code: str
    assessment_units: list[AssessmentUnitDraft]
    source_locations: list[dict[str, Any]] = Field(default_factory=list)


class DeepSeekSyllabusExtractor:
    def __init__(self, client: JsonRequester) -> None:
        self.client = client

    def extract_teaching(
        self,
        blocks: list[str],
        *,
        call_context: ModelCallContext | None = None,
    ) -> list[TeachingTopic]:
        result = self.client.request_json(
            system_prompt=(
                "你负责整理高校课程教学大纲中的教学内容与要求。只输出实际教学主题、教学深度和要求，"
                "忽略封面、日期、签字、制表说明等行政内容。必须返回严格 JSON 对象，顶层字段仅为 "
                "teaching_topics；每项包含 key、title、depth、requirements。"
            ),
            payload={"blocks": blocks},
            temperature=0.0,
            call_context=call_context,
        )
        try:
            return _TeachingResponse.model_validate(result).teaching_topics
        except ValidationError as exc:
            raise _schema_error(exc) from None

    def extract_assessment(
        self,
        blocks: list[str],
        *,
        call_context: ModelCallContext | None = None,
    ) -> AssessmentOutline:
        result = self.client.request_json(
            system_prompt=(
                "你负责从高校课程考核大纲中提取期末考试框架。只读取期末考试、终结性考试或同义栏目；"
                "平时成绩、实验过程、考勤、课程封面和行政信息不得转成期末考点。输出严格 JSON 对象，"
                "包含 anchors、exam_points、final_exam_rules。每个 exam_point 必须包含 code、anchor_key、"
                "title、assessment_requirement、weight_value、weight_source、weight_group_id、"
                "cognitive_targets、assessment_orientations、operational_detail_policy、retrieval_intent、"
                "teaching_anchor_keys。操作命令、安装和环境配置默认标记 supporting_only；只有考核大纲明确"
                "要求实践配置或操作考核时才可标记 directly_assessable。weight_source 仅允许 "
                "assessment_syllabus 或 inherited_group。"
            ),
            payload={"blocks": blocks},
            temperature=0.0,
            call_context=call_context,
        )
        try:
            response = _AssessmentResponse.model_validate(result)
            outline = AssessmentOutline(
                anchors=response.anchors,
                exam_points=response.exam_points,
                final_exam_rules=response.final_exam_rules,
            )
        except ValidationError as exc:
            raise _schema_error(exc) from None
        points = [
            point.model_copy(
                update={
                    "assessment_anchor_keys": point.assessment_anchor_keys or [point.anchor_key],
                }
            )
            for point in outline.exam_points
        ]
        return outline.model_copy(update={"exam_points": points})


class DeepSeekExamPointEvidenceClassifier:
    def __init__(self, client: JsonRequester) -> None:
        self.client = client

    def classify(
        self,
        *,
        exam_point: ExamPoint,
        material_version_id: str,
        chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> ExamPointFileDecision:
        if any(chunk.material_version_id != material_version_id for chunk in chunks):
            raise DeepSeekModelError(
                "model_input_scope_violation",
                "classification input contains another material version",
            )
        result = self.client.request_json(
            system_prompt=(
                "你只判断一个考试考点与一个教学资料文件中的证据关系。必须逐条返回输入 chunks 的判定，"
                "relevance_class 仅允许 direct、supporting、background、out_of_scope。direct 必须能直接支撑"
                "可评分事实、答案或评分点；supporting 只用于设问语境；background 和 out_of_scope 不得生成知识卡。"
                "遵守 exam_point.operational_detail_policy，不使用任何课程专属黑名单。来源页码和标题仅用于教师追溯，"
                "不得写入 candidate_card_content 的正文。返回 exam_point_code、material_version_id、decisions。"
            ),
            payload={
                "exam_point": exam_point.model_dump(mode="json"),
                "material_version_id": material_version_id,
                "chunks": [
                    {
                        "id": chunk.id,
                        "material_version_id": chunk.material_version_id,
                        "content": chunk.content,
                        "locator": chunk.locator,
                    }
                    for chunk in chunks
                ],
            },
            temperature=0.0,
            call_context=call_context,
        )
        try:
            response = _ClassificationResponse.model_validate(result)
            decision = ExamPointFileDecision(
                exam_point_code=response.exam_point_code,
                material_version_id=response.material_version_id,
                decisions=[
                    EvidenceDecision.model_validate(item.model_dump(exclude={"source_locator"}))
                    for item in response.decisions
                ],
            )
        except ValidationError as exc:
            raise _schema_error(exc) from None
        expected_ids = {chunk.id for chunk in chunks}
        actual_ids = [item.evidence_chunk_id for item in decision.decisions]
        if (
            decision.exam_point_code != exam_point.code
            or decision.material_version_id != material_version_id
            or len(actual_ids) != len(set(actual_ids))
            or set(actual_ids) != expected_ids
            or any(item.exam_point_code != exam_point.code for item in decision.decisions)
        ):
            raise DeepSeekModelError(
                "model_output_scope_violation",
                "classification output does not match the requested exam-point file pair",
            )
        return decision


class DeepSeekExamPointKnowledgeConsolidator:
    def __init__(self, client: JsonRequester) -> None:
        self.client = client

    def consolidate(
        self,
        *,
        exam_point: ExamPoint,
        admitted_decisions: list[EvidenceDecision],
        call_context: ModelCallContext | None = None,
    ) -> list[AssessmentUnitDraft]:
        admitted = [
            decision
            for decision in admitted_decisions
            if decision.relevance_class in {RelevanceClass.DIRECT, RelevanceClass.SUPPORTING}
        ]
        if any(decision.exam_point_code != exam_point.code for decision in admitted):
            raise DeepSeekModelError(
                "model_input_scope_violation",
                "consolidation input contains another exam point",
            )
        result = self.client.request_json(
            system_prompt=(
                "你只归并一个考试考点已经准入的 direct 和 supporting 决策。按可评分表现合并同义事实，"
                "同时保留不同答案边界；不得按文件名、章节、页码或来源数量拆分知识卡。知识卡的每条"
                "assessable_content 都必须被其 evidence_chunk_ids 引用的 direct 证据支持。supporting 内容"
                "只能进入 prompt_material。输出 exam_point_code、assessment_units，可另带 source_locations"
                "供教师查看，但来源信息不得进入卡片 name、performance_statement 或 assessable_content。"
            ),
            payload={
                "exam_point": exam_point.model_dump(mode="json"),
                "admitted_decisions": [item.model_dump(mode="json") for item in admitted],
            },
            temperature=0.0,
            call_context=call_context,
        )
        try:
            response = _ConsolidationResponse.model_validate(result)
        except ValidationError as exc:
            raise _schema_error(exc) from None
        if response.exam_point_code != exam_point.code:
            raise DeepSeekModelError(
                "model_output_scope_violation",
                "consolidation output belongs to another exam point",
            )
        _validate_consolidated_units(exam_point, admitted, response.assessment_units)
        return response.assessment_units


def _validate_consolidated_units(
    exam_point: ExamPoint,
    admitted: list[EvidenceDecision],
    units: list[AssessmentUnitDraft],
) -> None:
    direct_by_id = {
        decision.evidence_chunk_id: decision
        for decision in admitted
        if decision.relevance_class is RelevanceClass.DIRECT
    }
    for unit in units:
        if unit.exam_point_code != exam_point.code:
            raise DeepSeekModelError(
                "model_output_scope_violation",
                "assessment unit belongs to another exam point",
            )
        for card in unit.cards:
            evidence_ids = card.evidence_chunk_ids
            if not evidence_ids or set(evidence_ids) - set(direct_by_id):
                raise DeepSeekModelError(
                    "model_output_evidence_gap",
                    "knowledge card references evidence outside admitted direct decisions",
                )
            supported_facts = set()
            for evidence_id in evidence_ids:
                decision = direct_by_id[evidence_id]
                if decision.candidate_card_content is not None:
                    supported_facts.update(
                        assessable_fact_keys(decision.candidate_card_content.assessable_content)
                    )
                supported_facts.update(assessable_fact_keys([decision.support_claim]))
            if not assessable_fact_keys(card.assessable_content).issubset(supported_facts):
                raise DeepSeekModelError(
                    "model_output_evidence_gap",
                    "knowledge card contains a fact not covered by direct evidence",
                )


def _schema_error(exc: ValidationError) -> DeepSeekModelError:
    fields = sorted({".".join(str(part) for part in item["loc"]) for item in exc.errors()})
    return DeepSeekModelError(
        "model_schema_validation_failed",
        "model JSON does not match the required schema",
        details={"invalid_fields": fields[:20]},
    )


__all__ = [
    "DeepSeekExamPointEvidenceClassifier",
    "DeepSeekExamPointKnowledgeConsolidator",
    "DeepSeekJsonClient",
    "DeepSeekModelError",
    "DeepSeekSyllabusExtractor",
]
