"""Strict DeepSeek adapters for syllabus-led semantic curation."""

from __future__ import annotations

import re

from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from app.adapters.model.deepseek_gateway import DeepSeekJsonClient, DeepSeekModelError
from app.domain.framework.exam_points import ExamPoint
from app.domain.generation.semantic_diversity import AnswerRelation, InstanceCarrier
from app.domain.framework.models import AssessmentAnchor, AssessmentOutline, TeachingTopic
from app.domain.knowledge.models import AssessmentUnitDraft
from app.domain.knowledge.relevance import (
    ContentKind,
    EvidenceDecision,
    ExamPointFileDecision,
    RelevanceClass,
    StagingChunk,
    all_facts_supported,
    assessable_fact_keys,
    is_transferable_fact,
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
        response_validator=None,
    ) -> dict: ...


_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _text_list(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


_TextList = Annotated[list[_Text], BeforeValidator(_text_list)]


def _normalize_assessment_outline(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize compact LLM anchors without relaxing the exam-point contract."""

    normalized = dict(raw)
    raw_points = normalized.get("exam_points")
    weights_by_anchor: dict[str, Any] = {}
    if isinstance(raw_points, list):
        for point in raw_points:
            if isinstance(point, dict) and point.get("anchor_key"):
                weights_by_anchor.setdefault(str(point["anchor_key"]), point.get("weight_value"))

    raw_anchors = normalized.get("anchors")
    if not isinstance(raw_anchors, list):
        return normalized
    allowed_anchor_fields = {
        "key",
        "title",
        "exam_weight",
        "ability_requirements",
        "allowed_question_types",
        "excluded_content",
        "alignment_keys",
    }
    anchors: list[Any] = []
    for raw_anchor in raw_anchors:
        if not isinstance(raw_anchor, dict):
            anchors.append(raw_anchor)
            continue
        anchor = {
            key: value for key, value in raw_anchor.items() if key in allowed_anchor_fields
        }
        if not anchor.get("title") and isinstance(anchor.get("description"), str):
            anchor["title"] = anchor["description"]
        if not anchor.get("title") and isinstance(raw_anchor.get("description"), str):
            anchor["title"] = raw_anchor["description"]
        if anchor.get("exam_weight") is None and anchor.get("key"):
            anchor["exam_weight"] = weights_by_anchor.get(str(anchor["key"]))
        exam_weight = anchor.get("exam_weight")
        if isinstance(exam_weight, str):
            compact_weight = exam_weight.strip().replace("％", "%")
            if compact_weight.endswith("%"):
                try:
                    anchor["exam_weight"] = float(compact_weight[:-1].strip())
                except ValueError:
                    pass
        for key in (
            "ability_requirements",
            "allowed_question_types",
            "excluded_content",
            "alignment_keys",
        ):
            anchor.setdefault(key, [])
        anchors.append(anchor)
    normalized["anchors"] = anchors
    return normalized


def _normalize_classification_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider-neutral aliases without admitting incomplete direct evidence."""

    normalized = dict(raw)
    decisions = normalized.get("decisions")
    if not isinstance(decisions, list):
        return normalized
    normalized_decisions: list[Any] = []
    for raw_decision in decisions:
        if not isinstance(raw_decision, dict):
            normalized_decisions.append(raw_decision)
            continue
        decision = dict(raw_decision)
        if "evidence_chunk_id" not in decision:
            decision["evidence_chunk_id"] = decision.pop("chunk_id", decision.pop("id", None))
        confidence = decision.get("confidence")
        if isinstance(confidence, float) and 0 <= confidence <= 1:
            decision["confidence"] = round(confidence * 100)
        elif isinstance(confidence, str):
            compact_confidence = confidence.strip().casefold().replace("％", "%")
            qualitative_confidence = {"高": 85, "high": 85, "中": 65, "medium": 65, "低": 40, "low": 40}
            if compact_confidence in qualitative_confidence:
                decision["confidence"] = qualitative_confidence[compact_confidence]
            else:
                numeric_confidence = compact_confidence.removesuffix("%").strip()
                try:
                    parsed_confidence = float(numeric_confidence)
                except ValueError:
                    pass
                else:
                    if 0 <= parsed_confidence <= 1:
                        decision["confidence"] = round(parsed_confidence * 100)
                    elif 1 < parsed_confidence <= 100:
                        decision["confidence"] = round(parsed_confidence)
        relevance = decision.get("relevance_class")
        content_kind = decision.get("content_kind")
        generic_content_kinds = {
            "text",
            "文本",
            "metadata",
            "meta",
            "header",
            "heading",
            "title",
            "admin",
            "administrative",
            "other",
            "其他",
            "unknown",
            "未知",
            "information",
            "knowledge",
            "explanation",
            "description",
            "general",
            "general_content",
        }
        normalized_kind = content_kind.strip().casefold() if isinstance(content_kind, str) else None
        known_kinds = {item.value for item in ContentKind}
        if isinstance(normalized_kind, str) and (
            normalized_kind in generic_content_kinds
            or normalized_kind not in known_kinds
        ):
            decision["content_kind"] = (
                "fact"
                if relevance
                in {RelevanceClass.DIRECT.value, RelevanceClass.SUPPORTING.value}
                else "background"
            )
        normalized_decisions.append(decision)
    normalized["decisions"] = normalized_decisions
    return normalized


def _normalize_file_classification_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the file-level wrapper without weakening per-point evidence checks."""

    normalized = dict(raw)
    file_decisions = None
    for key in ("file_decisions", "fileDecisions", "FileDecisions"):
        if isinstance(normalized.get(key), list):
            file_decisions = normalized.pop(key)
            break
    if file_decisions is None and isinstance(normalized.get("decisions"), list):
        legacy_item = {key: value for key, value in normalized.items() if key != "decisions"}
        legacy_item["decisions"] = normalized["decisions"]
        file_decisions = [legacy_item]
    if file_decisions is None:
        return normalized
    normalized["file_decisions"] = [
        _normalize_classification_response(item) if isinstance(item, dict) else item
        for item in file_decisions
    ]
    return normalized


def _split_multi_clause_atoms(values: Any) -> list[str]:
    """多子句复合事实按"；/;"切分为独立原子。

    填空/判断题无法承载双子句语义；切分后的子句是原子的子串，
    证据包含判定（fact_key_supported）不受影响。
    """

    if not isinstance(values, list):
        values = [values]
    pieces: list[str] = []
    for value in values:
        for piece in re.split(r"[；;]", str(value or "")):
            piece = piece.strip()
            if piece:
                pieces.append(piece)
    return pieces


def _normalize_consolidation_response(
    raw: dict[str, Any], exam_point: ExamPoint, admitted: list[EvidenceDecision]
) -> dict[str, Any]:
    """Accept the provider's flat-card variant without weakening evidence checks."""

    normalized = dict(raw)
    normalized.setdefault("exam_point_code", exam_point.code)
    raw_locations = normalized.get("source_locations")
    normalized["source_locations"] = (
        [location for location in raw_locations if isinstance(location, dict)]
        if isinstance(raw_locations, list)
        else []
    )
    units = normalized.get("assessment_units")
    if not isinstance(units, list):
        return normalized
    converted: list[Any] = []
    for index, raw_unit in enumerate(units, start=1):
        if not isinstance(raw_unit, dict):
            converted.append(raw_unit)
            continue
        if "cards" in raw_unit:
            unit = dict(raw_unit)
            cards = unit.get("cards")
            if isinstance(cards, list):
                card_fields = {
                    "name",
                    "performance_statement",
                    "assessable_content",
                    "scope_boundary",
                    "cognitive_targets",
                    "allowed_question_types",
                    "importance",
                    "evidence_chunk_ids",
                    "prompt_material",
                    "concept_cluster",
                    "answer_proposition",
                    "required_propositions",
                    "relation_edges",
                    "instance_carriers",
                    "status",
                }
                unit["cards"] = [
                    {
                        **{
                            key: value
                            for key, value in card.items()
                            if key in card_fields
                        },
                        "assessable_content": _split_multi_clause_atoms(card.get(
                            "assessable_content",
                            card.get("content", card.get("facts", card.get("knowledge_points", []))),
                        )),
                        "evidence_chunk_ids": card.get(
                            "evidence_chunk_ids", card.get("evidence_ids", [])
                        ),
                    }
                    for card in cards
                    if isinstance(card, dict)
                ]
            converted.append(unit)
            continue
        if "name" not in raw_unit or "assessable_content" not in raw_unit:
            converted.append(raw_unit)
            continue
        card = dict(raw_unit)
        card["assessable_content"] = _split_multi_clause_atoms(card.get(
            "assessable_content",
            card.get("content", card.get("facts", card.get("knowledge_points", []))),
        ))
        card["evidence_chunk_ids"] = card.get(
            "evidence_chunk_ids", card.get("evidence_ids", [])
        )
        card.pop("title", None)
        card.pop("code", None)
        card.pop("exam_point_code", None)
        card.pop("source_locations", None)
        converted.append(
            {
                "code": f"{exam_point.code}-U{index}",
                "title": str(raw_unit["name"]),
                "performance_statement": exam_point.assessment_requirement,
                "exam_point_code": exam_point.code,
                "cards": [card],
            }
        )
    normalized["assessment_units"] = converted
    return normalized


class _TeachingTopicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: _Text
    title: _Text
    depth: _Text
    requirements: _TextList = Field(default_factory=list)


class _AssessmentAnchorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: _Text
    title: _Text
    exam_weight: float = Field(ge=0, le=100)
    ability_requirements: _TextList = Field(default_factory=list)
    allowed_question_types: _TextList = Field(default_factory=list)
    excluded_content: _TextList = Field(default_factory=list)
    alignment_keys: _TextList = Field(default_factory=list)


class _KnowledgeCardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _Text
    performance_statement: _Text
    assessable_content: _TextList = Field(default_factory=list)
    scope_boundary: dict[str, Any] = Field(default_factory=dict)
    cognitive_targets: _TextList = Field(default_factory=list)
    allowed_question_types: _TextList = Field(default_factory=list)
    importance: int = Field(default=1, ge=1, le=5)
    evidence_chunk_ids: _TextList = Field(default_factory=list)
    prompt_material: _TextList = Field(default_factory=list)
    concept_cluster: str = ""
    answer_proposition: str = ""
    required_propositions: _TextList = Field(default_factory=list)
    relation_edges: list[AnswerRelation] = Field(default_factory=list)
    instance_carriers: list[InstanceCarrier] = Field(default_factory=list)
    status: Literal["active", "excluded", "material_only", "needs_teacher_review"] = "active"

    @model_validator(mode="after")
    def require_active_content(self):
        if self.status == "active" and not self.assessable_content:
            raise ValueError("active knowledge card requires assessable_content")
        return self


class _AssessmentUnitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: _Text
    title: _Text
    performance_statement: _Text
    exam_point_code: _Text
    scope_boundary: dict[str, Any] = Field(default_factory=dict)
    cards: list[_KnowledgeCardResponse] = Field(default_factory=list)
    status: Literal["active", "excluded", "needs_teacher_review"] = "active"


class _TeachingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teaching_topics: list[_TeachingTopicResponse]


class _AssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchors: list[_AssessmentAnchorResponse]
    exam_points: list["_AssessmentPointResponse"] = Field(min_length=1)
    final_exam_rules: dict[str, Any] = Field(default_factory=dict)


class _AssessmentPointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: _Text
    anchor_key: _Text
    title: _Text
    assessment_requirement: _Text
    weight_value: float = Field(ge=0, le=100)
    weight_source: Literal["assessment_syllabus", "inherited_group"]
    weight_group_id: _Text
    cognitive_targets: _TextList
    assessment_orientations: _TextList
    operational_detail_policy: Literal[
        "forbidden", "supporting_only", "directly_assessable"
    ]
    retrieval_intent: _Text
    teaching_anchor_keys: _TextList
    priority: _Text = "normal"
    allowed_question_types: _TextList = Field(default_factory=list)
    scope_boundary: dict[str, Any] = Field(default_factory=dict)
    required_evidence_roles: _TextList = Field(default_factory=list)
    assessment_anchor_keys: _TextList = Field(default_factory=list)


class _EvidenceDecisionResponse(EvidenceDecision):
    model_config = ConfigDict(extra="forbid")
    source_locator: dict[str, Any] = Field(default_factory=dict)


class _ClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exam_point_code: _Text
    material_version_id: _Text
    decisions: list[_EvidenceDecisionResponse]
    source_locations: list[dict[str, Any]] = Field(default_factory=list)


class _FileClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_decisions: list[_ClassificationResponse]


class _ConsolidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exam_point_code: _Text
    assessment_units: list[_AssessmentUnitResponse]
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
        parsed: list[list[TeachingTopic]] = []

        def validate_response(result: dict) -> None:
            try:
                response = _TeachingResponse.model_validate(result)
                parsed.append(
                    [TeachingTopic.model_validate(topic.model_dump(mode="json")) for topic in response.teaching_topics]
                )
            except ValidationError as exc:
                raise _schema_error(exc) from None

        self.client.request_json(
            system_prompt=(
                "你负责整理高校课程教学大纲中的教学内容与要求。只输出实际教学主题、教学深度和要求，"
                "忽略封面、日期、签字、制表说明等行政内容。必须返回严格 JSON 对象，顶层字段仅为 "
                "teaching_topics；每项包含 key、title、depth、requirements。"
            ),
            payload={"blocks": blocks},
            temperature=0.0,
            call_context=call_context,
            response_validator=validate_response,
        )
        return parsed[0]

    def extract_assessment(
        self,
        blocks: list[str],
        *,
        call_context: ModelCallContext | None = None,
    ) -> AssessmentOutline:
        parsed: list[AssessmentOutline] = []

        def validate_response(result: dict) -> None:
            try:
                response = _AssessmentResponse.model_validate(
                    _normalize_assessment_outline(result)
                )
                outline = AssessmentOutline(
                    anchors=[
                        AssessmentAnchor.model_validate(anchor.model_dump(mode="json"))
                        for anchor in response.anchors
                    ],
                    exam_points=[
                        ExamPoint.model_validate(point.model_dump(mode="json"))
                        for point in response.exam_points
                    ],
                    final_exam_rules=response.final_exam_rules,
                )
            except ValidationError as exc:
                raise _schema_error(exc) from None
            points = [
                point.model_copy(
                    update={
                        "assessment_anchor_keys": point.assessment_anchor_keys
                        or [point.anchor_key],
                    }
                )
                for point in outline.exam_points
            ]
            parsed.append(outline.model_copy(update={"exam_points": points}))

        self.client.request_json(
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
            response_validator=validate_response,
        )
        return parsed[0]


class DeepSeekExamPointEvidenceClassifier:
    def __init__(self, client: JsonRequester) -> None:
        self.client = client

    def classify_file(
        self,
        *,
        exam_points: list[ExamPoint],
        material_version_id: str,
        chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> list[ExamPointFileDecision]:
        """一个资料文件 × 全部相关考点，一次调用完成分类。"""
        if any(chunk.material_version_id != material_version_id for chunk in chunks):
            raise DeepSeekModelError(
                "model_input_scope_violation",
                "classification input contains another material version",
            )
        expected_pairs = {
            (point.code, chunk.id) for point in exam_points for chunk in chunks
        }
        collected: dict[str, ExamPointFileDecision] = {}

        def validate_response(result: dict) -> None:
            try:
                response = _FileClassificationResponse.model_validate(
                    _normalize_file_classification_response(result)
                )
            except ValidationError as exc:
                raise _schema_error(exc) from None
            seen: list[tuple[str, str]] = []
            for item in response.file_decisions:
                if item.material_version_id != material_version_id:
                    raise DeepSeekModelError(
                        "model_output_scope_violation",
                        "classification response belongs to another material version",
                    )
                if item.exam_point_code not in {point.code for point in exam_points}:
                    raise DeepSeekModelError(
                        "model_output_scope_violation",
                        "classification response references unknown exam point",
                    )
                for decision in item.decisions:
                    if decision.exam_point_code != item.exam_point_code:
                        raise DeepSeekModelError(
                            "model_output_scope_violation",
                            "classification decision belongs to another exam point",
                        )
                    seen.append((item.exam_point_code, decision.evidence_chunk_id))
                collected[item.exam_point_code] = ExamPointFileDecision(
                    exam_point_code=item.exam_point_code,
                    material_version_id=item.material_version_id,
                    decisions=[
                        EvidenceDecision.model_validate(
                            d.model_dump(exclude={"source_locator"})
                        )
                        for d in item.decisions
                    ],
                )
            if len(seen) != len(set(seen)) or set(seen) != expected_pairs:
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    "classification output must cover every (exam_point, chunk) pair exactly once",
                )

        self.client.request_json(
            system_prompt=(
                "你判断一份教学资料文件与多个考试考点的证据关系。输入包含 exam_points 数组与该文件全部召回的 chunks。"
                "必须返回 JSON 对象，顶层字段 file_decisions 为数组；每个元素对应一个考点，"
                "包含 exam_point_code、material_version_id、decisions。"
                "每个考点必须对其与该文件相关的全部输入 chunks 逐一判定，不得遗漏；"
                "每条 decisions 必须一一对应输入的 evidence_chunk_id，且必须包含 exam_point_code、"
                "evidence_chunk_id、relevance_class、support_claim、content_kind、confidence。"
                "relevance_class 仅允许 direct、supporting、background、out_of_scope；"
                "direct 必须能直接支撑可评分事实、答案或评分点，且必须提供 candidate_assessment_unit 与 "
                "candidate_card_content，否则降级为 supporting 或 background；"
                "candidate_card_content.assessable_content 的每条事实必须自包含：明确归属主体，"
                "说明该参数/命令/概念属于哪个框架、工具、模型或流程（如写'ms-swift 的 eval_batch_size 参数…'而非'eval_batch_size 参数…'），"
                "归属信息可结合 chunk 内容与 locator 的 heading_path 推断，禁止输出无主语的参数、命令或数值罗列；"
                "supporting 只用于设问语境；background/out_of_scope 不得生成知识卡。"
                "遵守各考点 operational_detail_policy，不使用任何课程专属黑名单。"
                "来源页码和标题仅用于教师追溯，不得写入 candidate_card_content 的正文。返回严格 JSON。"
            ),
            payload={
                "exam_points": [point.model_dump(mode="json") for point in exam_points],
                "material_version_id": material_version_id,
                "chunks": [
                    {
                        "evidence_chunk_id": chunk.id,
                        "material_version_id": chunk.material_version_id,
                        "content": chunk.content,
                        "locator": chunk.locator,
                    }
                    for chunk in chunks
                ],
            },
            temperature=0.0,
            call_context=call_context,
            response_validator=validate_response,
        )
        return list(collected.values())


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
        parsed: list[list[AssessmentUnitDraft]] = []

        def validate_response(result: dict) -> None:
            try:
                response = _ConsolidationResponse.model_validate(
                    _normalize_consolidation_response(result, exam_point, admitted)
                )
            except ValidationError as exc:
                raise _schema_error(exc) from None
            if response.exam_point_code != exam_point.code:
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    "consolidation output belongs to another exam point",
                )
            _validate_consolidated_units(
                exam_point,
                admitted,
                [
                    AssessmentUnitDraft.model_validate(unit.model_dump(mode="json"))
                    for unit in response.assessment_units
                ],
            )
            parsed.append(
                [
                    AssessmentUnitDraft.model_validate(unit.model_dump(mode="json"))
                    for unit in response.assessment_units
                ]
            )

        self.client.request_json(
            system_prompt=(
                "你只归并一个考试考点已经准入的 direct 和 supporting 决策。按可评分表现合并同义事实，"
                "同时保留不同答案边界；不得按文件名、章节、页码或来源数量拆分知识卡。知识卡的每条"
                "assessable_content 都必须被其 evidence_chunk_ids 引用的 direct 证据支持。supporting 内容"
                "只能进入 prompt_material。"
                "每条 assessable_content 必须是可迁移的通用知识：案例讲解只抽取其承载的通用结论，"
                "剥离绑定特定实验运行的叙述背景——不得出现'上一轮训练''本次实验''我们的实验'等"
                "情境表述（'失衡问题出现在上一轮训练中'不是知识点，"
                "'混合数据集用于解决思考与非思考数据失衡'才是）；知识卡会进入检索库，"
                "案例叙述会污染检索且消耗资源。"
                "assessable_content 的每条事实必须自包含：脱离卡片名和单元名即可独立理解；"
                "证据事实缺主语时必须补全归属限定（说明参数、命令、概念属于哪个框架、工具、模型或流程，"
                "如证据为'eval_batch_size参数用于控制评测批大小'时归并为'ms-swift框架中，eval_batch_size参数用于控制评测批大小'），"
                "归属限定只能来自证据本身、同考点其他证据或考点语境，禁止编造归属；"
                "禁止输出无主语的参数、命令或数值罗列。"
                "输出严格 JSON 对象，包含 exam_point_code、assessment_units，可另带 source_locations"
                "供教师查看，但来源信息不得进入卡片 name、performance_statement 或 assessable_content。每张 active 卡还要输出"
                "来源无关的 concept_cluster、answer_proposition、required_propositions、relation_edges 和 instance_carriers。"
                "concept_cluster 按共同考核能力聚合；relation_edges 仅描述等价、上下位、组成、对比、汇总和前置关系；"
                "instance_carriers 标记具体对象是考纲要求主体还是可替代示例，不得使用课程专属黑名单。"
            ),
            payload={
                "exam_point": exam_point.model_dump(mode="json"),
                "admitted_decisions": [item.model_dump(mode="json") for item in admitted],
            },
            temperature=0.0,
            call_context=call_context,
            response_validator=validate_response,
        )
        return parsed[0]


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
        if unit.status == "active" and not unit.cards:
            raise DeepSeekModelError(
                "model_output_evidence_gap",
                "active assessment unit requires at least one knowledge card",
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
            if not all_facts_supported(
                assessable_fact_keys(card.assessable_content), supported_facts
            ):
                raise DeepSeekModelError(
                    "model_output_evidence_gap",
                    "knowledge card contains a fact not covered by direct evidence",
                )
            # 可迁移性：知识卡是 RAG 检索库的源头，案例叙述背景
            # （"失衡问题出现在上一轮训练中"）不得入库，入库前即拒绝
            non_transferable = [
                text for text in card.assessable_content
                if not is_transferable_fact(text)
            ]
            if non_transferable:
                raise DeepSeekModelError(
                    "model_output_evidence_gap",
                    "knowledge card contains case-narrative facts bound to a specific "
                    "experiment run; extract only the transferable conclusion",
                )


def _schema_error(exc: ValidationError) -> DeepSeekModelError:
    errors = exc.errors()
    fields = sorted({".".join(str(part) for part in item["loc"]) for item in errors})
    invalid_values = {
        ".".join(str(part) for part in item["loc"]): str(item.get("input", ""))[:80]
        for item in errors
        if str(item["loc"][-1]) in {"confidence", "content_kind"}
    }
    invalid_inputs = {
        ".".join(str(part) for part in item["loc"]): {
            "type": type(item.get("input")).__name__,
            "preview": str(item.get("input", ""))[:240],
        }
        for item in errors
        if len(item["loc"]) >= 3 and item["loc"][0] == "assessment_units"
    }
    validation_errors = [
        {"loc": list(item.get("loc", [])), "msg": str(item.get("msg", ""))[:160]}
        for item in errors
    ]
    return DeepSeekModelError(
        "model_schema_validation_failed",
        "model JSON does not match the required schema",
        details={
            "invalid_fields": fields[:20],
            "invalid_values": invalid_values,
            "invalid_inputs": invalid_inputs,
            "validation_errors": validation_errors,
        },
    )


__all__ = [
    "DeepSeekExamPointEvidenceClassifier",
    "DeepSeekExamPointKnowledgeConsolidator",
    "DeepSeekJsonClient",
    "DeepSeekModelError",
    "DeepSeekSyllabusExtractor",
]
