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
    semantic_text_key,
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
        tool: dict[str, Any] | None = None,
        max_tokens: int | None = None,
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


_COMPACT_CLASS_ARRAY_KEYS = {
    "background_chunk_ids": "background",
    "backgroundChunkIds": "background",
    "out_of_scope_chunk_ids": "out_of_scope",
    "outOfScopeChunkIds": "out_of_scope",
}


def _normalize_classification_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider-neutral aliases without admitting incomplete direct evidence.

    允许模型用紧凑数组汇报 background/out_of_scope 判定（每条只需 evidence_chunk_id），
    在此展开为完整 decision；direct/supporting 仍必须逐条输出完整字段。
    """

    normalized = dict(raw)
    decisions = normalized.get("decisions")
    if not isinstance(decisions, list):
        # 只有紧凑数组、没有 decisions 键时仍需展开；两者皆缺则原样返回，交由校验失败闭环。
        if not any(
            isinstance(normalized.get(key), list) for key in _COMPACT_CLASS_ARRAY_KEYS
        ):
            return normalized
        decisions = []
    point_code = normalized.get("exam_point_code")
    normalized_decisions: list[Any] = []
    for raw_decision in decisions:
        if not isinstance(raw_decision, dict):
            normalized_decisions.append(raw_decision)
            continue
        decision = dict(raw_decision)
        if "exam_point_code" not in decision and isinstance(point_code, str):
            decision["exam_point_code"] = point_code
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
    for array_key, relevance_class in _COMPACT_CLASS_ARRAY_KEYS.items():
        chunk_ids = normalized.pop(array_key, None)
        if not isinstance(chunk_ids, list):
            continue
        for chunk_id in chunk_ids:
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                continue
            normalized_decisions.append(
                {
                    "exam_point_code": point_code,
                    "evidence_chunk_id": chunk_id.strip(),
                    "relevance_class": relevance_class,
                    "support_claim": "（未提供说明）",
                    "content_kind": "background",
                    "confidence": 100,
                }
            )
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


_CARD_FIELDS = {
    "name",
    "performance_statement",
    "assessable_content",
    "scope_boundary",
    "cognitive_targets",
    "allowed_question_types",
    "importance",
    "evidence_chunk_ids",
    "prompt_material",
    "status",
}


# 归并分批：每批最多 4 条 direct 证据、总证据 ≤6，控制单次输出规模，
# 规避 MiMo 大 JSON 输出被截断导致的格式失败。
# 归并输出上限：给足余量避免截断，但不必过大（每批仅 1~3 张卡）。
_BATCH_DIRECT_MAX = 4
_BATCH_TOTAL_MAX = 6
_CONSOLIDATION_MAX_TOKENS = 4096


def _normalize_consolidation_response(
    raw: dict[str, Any], exam_point: ExamPoint, admitted: list[EvidenceDecision]
) -> dict[str, Any]:
    """Accept the provider's flat-card variant without weakening evidence checks.

    归并只要求模型输出扁平知识卡（顶层 cards 数组）；单元层面的 code/title/
    exam_point_code/performance_statement 由代码确定性组装，避免模型产出嵌套
    assessment_units 时因缺单元必填字段触发 schema 校验失败。同时保留对旧
    assessment_units 输出的解析，防止重构期间回归。
    """

    def card_with_defaults(card: dict[str, Any]) -> dict[str, Any]:
        out = {key: value for key, value in card.items() if key in _CARD_FIELDS}
        out["assessable_content"] = _split_multi_clause_atoms(
            out.get(
                "assessable_content",
                out.get("content", out.get("facts", out.get("knowledge_points", []))),
            )
        )
        out["evidence_chunk_ids"] = out.get("evidence_chunk_ids", out.get("evidence_ids", []))
        if not isinstance(out.get("name"), str) or not out.get("name").strip():
            facts = [
                item for item in out.get("assessable_content", [])
                if isinstance(item, str) and item.strip()
            ]
            out["name"] = facts[0][:40] if facts else exam_point.title
        if (
            not isinstance(out.get("performance_statement"), str)
            or not out.get("performance_statement").strip()
        ):
            out["performance_statement"] = exam_point.assessment_requirement
        return out

    normalized = dict(raw)
    normalized.setdefault("exam_point_code", exam_point.code)
    raw_locations = normalized.get("source_locations")
    normalized["source_locations"] = (
        [location for location in raw_locations if isinstance(location, dict)]
        if isinstance(raw_locations, list)
        else []
    )

    raw_cards = normalized.get("cards")
    if isinstance(raw_cards, list):
        normalized["cards"] = [
            card_with_defaults(card) for card in raw_cards if isinstance(card, dict)
        ]
        return normalized

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
                unit["cards"] = [
                    card_with_defaults(card) for card in cards if isinstance(card, dict)
                ]
            converted.append(unit)
            continue
        if "name" not in raw_unit or "assessable_content" not in raw_unit:
            converted.append(raw_unit)
            continue
        card = card_with_defaults(dict(raw_unit))
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
    model_config = ConfigDict(extra="ignore")

    name: _Text
    performance_statement: _Text
    assessable_content: _TextList = Field(default_factory=list)
    scope_boundary: dict[str, Any] = Field(default_factory=dict)
    cognitive_targets: _TextList = Field(default_factory=list)
    allowed_question_types: _TextList = Field(default_factory=list)
    importance: int = Field(default=1, ge=1, le=5)
    evidence_chunk_ids: _TextList = Field(default_factory=list)
    prompt_material: _TextList = Field(default_factory=list)
    status: Literal["active", "excluded", "material_only", "needs_teacher_review"] = "active"

    @model_validator(mode="after")
    def require_active_content(self):
        if self.status == "active" and not self.assessable_content:
            raise ValueError("active knowledge card requires assessable_content")
        return self


class _AssessmentUnitResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    model_config = ConfigDict(extra="ignore")
    exam_point_code: _Text
    cards: list[_KnowledgeCardResponse] = Field(default_factory=list)
    assessment_units: list[_AssessmentUnitResponse] = Field(default_factory=list)
    source_locations: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_output_form(self):
        if not self.cards and not self.assessment_units:
            raise ValueError("consolidation must output cards or assessment_units")
        return self


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

        def compact_point_payload(point: ExamPoint) -> dict[str, Any]:
            # 分类只需要判断证据关系所需字段。完整 model_dump 会把
            # scope_boundary / required_evidence_roles / teaching_anchor_keys 等
            # 长字段一并传给模型：每考点约 300-600 token，且与分类决策无关，
            # 批次内多考点时被重复计费，是分类输入膨胀的主要来源之一。
            return {
                "code": point.code,
                "title": point.title,
                "assessment_requirement": point.assessment_requirement,
                "cognitive_targets": point.cognitive_targets,
                "assessment_orientations": point.assessment_orientations,
                "allowed_question_types": point.allowed_question_types,
                "operational_detail_policy": point.operational_detail_policy.value,
                "retrieval_intent": point.retrieval_intent,
            }

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
            if not (set(seen) <= expected_pairs):
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    "classification output references an unknown chunk",
                )
            if len(seen) != len(set(seen)):
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    "classification output contains duplicate evidence decisions",
                )

        self.client.request_json(
            system_prompt=(
                "你判断一份教学资料文件与多个考试考点的证据关系。输入包含 exam_points 数组与该文件全部召回的 chunks。"
                "必须返回 JSON 对象，顶层字段 file_decisions 为数组；每个元素对应一个考点，"
                "包含 exam_point_code、material_version_id、decisions、background_chunk_ids、out_of_scope_chunk_ids。"
                "尽量覆盖全部 (考点, chunk) 组合；未明确判定的组合会被系统默认为 out_of_scope。"
                "direct/supporting 判定写入 decisions 数组，每条包含 evidence_chunk_id、relevance_class、"
                "support_claim、content_kind、confidence（无需重复 exam_point_code）；"
                "background 判定只需把 evidence_chunk_id 列入 background_chunk_ids，"
                "out_of_scope 判定只需把 evidence_chunk_id 列入 out_of_scope_chunk_ids，两者均不得输出其他字段。"
                "relevance_class 仅允许 direct、supporting、background、out_of_scope；"
                "direct 表示该 chunk 直接提供了能支撑该考点考核内容的事实或依据，"
                "support_claim 用一句话概括该 chunk 提供的具体事实即可，无需补全归属或写成自包含命题；"
                "归属补全与命题化由后续归并环节完成。"
                "仅当 chunk 确实直接支撑该考点时判 direct，否则降级为 supporting 或 background。"
                "supporting 只用于设问语境；background/out_of_scope 不产出知识事实。"
                "遵守各考点 operational_detail_policy，不使用任何课程专属黑名单。"
                "来源页码和标题仅用于教师追溯，不得写入 support_claim 的正文。返回严格 JSON。"
            ),
            payload={
                "exam_points": [compact_point_payload(point) for point in exam_points],
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
        # 兜底补全：模型漏判的 (考点, chunk) 对默认 out_of_scope。
        # 分类阶段不再要求模型输出完整候选卡，故豁免规则的遗漏应由确定性补全承担，
        # 保证每个输入 pair 都有且仅有一条决定，下游仍按完全覆盖校验。
        for point in exam_points:
            existing = collected.get(point.code)
            existing_ids = (
                {item.evidence_chunk_id for item in existing.decisions}
                if existing is not None
                else set()
            )
            missing = [
                EvidenceDecision(
                    exam_point_code=point.code,
                    evidence_chunk_id=chunk.id,
                    relevance_class=RelevanceClass.OUT_OF_SCOPE,
                    support_claim="（未提供说明）",
                    content_kind=ContentKind.BACKGROUND,
                    confidence=100,
                )
                for chunk in chunks
                if chunk.id not in existing_ids
            ]
            if not missing:
                continue
            if existing is None:
                collected[point.code] = ExamPointFileDecision(
                    exam_point_code=point.code,
                    material_version_id=material_version_id,
                    decisions=missing,
                )
            else:
                collected[point.code] = existing.model_copy(
                    update={"decisions": [*existing.decisions, *missing]}
                )
        return list(collected.values())


class DeepSeekExamPointKnowledgeConsolidator:
    def __init__(self, client: JsonRequester) -> None:
        self.client = client

    def recheck_direct(
        self,
        *,
        exam_point: ExamPoint,
        candidate_chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> set[str]:
        """对候选 chunks 逐个判定是否直接支撑考点，返回应提升为 direct 的 id 集合。

        分类在大批量判定里判 direct 偏保守，导致部分考点 0 direct；复核只针对
        单个考点，对召回但被判为 supporting/background 的 chunks 逐个布尔判定，
        把内容本身就是考点考核知识的 chunk 提回 direct。
        """
        grounding = sorted(candidate_chunks, key=lambda item: item.id)
        if not grounding:
            return set()

        def validate_response(result: dict) -> None:
            flags = result.get("is_direct")
            if not isinstance(flags, list) or any(
                not isinstance(item, bool) for item in flags
            ):
                raise DeepSeekModelError(
                    "model_schema_validation_failed",
                    "direct recheck output must contain is_direct boolean array",
                )
            if len(flags) != len(grounding):
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    "direct recheck flag count does not match chunk count",
                )

        result = self.client.request_json(
            system_prompt=(
                "你复核一个考试考点的证据判定。以下 chunks 是从教学资料中召回的相关片段，"
                "逐个判断：若 chunk 的内容本身就是该考点考核知识的直接事实或依据"
                "（概念、定义、原理、机制、规则、公式、关系、比较、约束等知识本身，"
                "或案例承载的通用结论），判 true；仅当 chunk 是设问/练习语境、"
                "背景铺垫或与考点无关时才判 false。"
                "is_direct 数组与 chunks 顺序一一对应，只输出 JSON 对象"
                " {\"is_direct\": [true, false, ...]}。"
            ),
            payload={
                "exam_point": exam_point.model_dump(mode="json"),
                "chunks": [
                    {
                        "evidence_chunk_id": chunk.id,
                        "material_version_id": chunk.material_version_id,
                        "content": chunk.content,
                        "locator": chunk.locator,
                    }
                    for chunk in grounding
                ],
            },
            temperature=0.0,
            call_context=call_context,
            response_validator=validate_response,
            max_tokens=512,
        )
        flags = result.get("is_direct") or []
        return {chunk.id for chunk, flag in zip(grounding, flags) if flag}

    def consolidate(
        self,
        *,
        exam_point: ExamPoint,
        admitted_decisions: list[EvidenceDecision],
        chunks_by_id: dict[str, StagingChunk],
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
        admitted_chunk_ids = {decision.evidence_chunk_id for decision in admitted}
        grounding_chunks = sorted(
            (chunk for chunk in chunks_by_id.values() if chunk.id in admitted_chunk_ids),
            key=lambda item: item.id,
        )
        # 分批归并：一次只让模型产出一个小批，避免单次输出过长触发 MiMo 截断
        # 或格式漂移。direct 证据按 support_claim 语义键排序后每批 ≤4 条；
        # supporting 只用于语境，按批补足。各批结果合并后统一校验与去重。
        direct = [
            decision
            for decision in admitted
            if decision.relevance_class is RelevanceClass.DIRECT
        ]
        supporting = [
            decision
            for decision in admitted
            if decision.relevance_class is RelevanceClass.SUPPORTING
        ]
        direct.sort(key=lambda decision: semantic_text_key(decision.support_claim))
        batches: list[list[EvidenceDecision]] = [
            direct[index : index + _BATCH_DIRECT_MAX]
            for index in range(0, len(direct), _BATCH_DIRECT_MAX)
        ]
        if not batches:
            batches.append([])
        for index, decision in enumerate(supporting):
            batch = batches[index % len(batches)]
            if len(batch) < _BATCH_TOTAL_MAX:
                batch.append(decision)
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
            if response.cards:
                units = [
                    AssessmentUnitDraft.model_validate(
                        {
                            "code": f"{exam_point.code}-U1",
                            "title": exam_point.title,
                            "performance_statement": exam_point.assessment_requirement,
                            "exam_point_code": exam_point.code,
                            "scope_boundary": {},
                            "cards": [
                                card.model_dump(mode="json") for card in response.cards
                            ],
                        }
                    )
                ]
            else:
                units = [
                    AssessmentUnitDraft.model_validate(unit.model_dump(mode="json"))
                    for unit in response.assessment_units
                ]
            _validate_consolidated_units(exam_point, admitted, units, chunks_by_id=chunks_by_id)
            parsed.append(units)

        for batch in batches:
            batch_chunk_ids = {decision.evidence_chunk_id for decision in batch}
            batch_grounding = sorted(
                (chunk for chunk in grounding_chunks if chunk.id in batch_chunk_ids),
                key=lambda item: item.id,
            )
            self.client.request_json(
                system_prompt=(
                "你只归并一个考试考点已经准入的 direct 和 supporting 证据，产出该考点的可评分知识卡。"
                "按可评分表现合并同义事实、保留不同答案边界；不得按文件名、章节、页码或来源数量拆分卡片。"
                "每条 assessable_content 都必须被 direct 证据逐条支撑；supporting 内容只能进入 prompt_material。"
                "evidence_chunk_ids 只能从 payload 的 citable_chunk_ids（direct 证据的 chunk id）中选择，"
                "不得为空；其余 chunks 均为 supporting，仅供理解语境与补全归属，禁止引用。"
                "每条 assessable_content 必须是可迁移的通用知识：案例讲解只抽取其承载的通用结论，"
                "剥离绑定特定实验运行的叙述背景——不得出现'上一轮训练''本次实验''我们的实验'等情境表述"
                "（'失衡问题出现在上一轮训练中'不是知识点，'混合数据集用于解决思考与非思考数据失衡'才是）。"
                "每条事实必须自包含：脱离卡片名即可独立理解；缺主语时补全归属限定，说明参数、命令、概念"
                "属于哪个框架、工具、模型或流程（如'eval_batch_size参数用于控制评测批大小'归并为"
                "'ms-swift框架中，eval_batch_size参数用于控制评测批大小'）；归属只能来自证据或考点语境，"
                "禁止编造，禁止输出无主语的参数或命令罗列。"
                "严格输出 JSON 对象，只含两个顶层字段 exam_point_code 和 cards。cards 是扁平数组，"
                "每项字段仅为：name（卡片名）、performance_statement（一句话可评分表现说明）、"
                "assessable_content（字符串数组，每条一个可评分事实）、prompt_material（字符串数组，可空）、"
                "evidence_chunk_ids（字符串数组，从 citable_chunk_ids 中选择，不得为空）。"
                "不要输出 assessment_units、code、title、source_locations、importance 等其它字段。"
                "示例：{\"exam_point_code\":\"EP-01\",\"cards\":[{\"name\":\"混合数据集与数据失衡\","
                "\"performance_statement\":\"能说明混合数据集如何解决思考与非思考数据失衡\","
                "\"assessable_content\":[\"混合数据集用于解决思考与非思考数据失衡\"],"
                "\"prompt_material\":[],\"evidence_chunk_ids\":[\"chunk-12\"]}]}。"
                "输入还包含 chunks（准入证据的原文切片），仅用于理解语境与补全归属限定；"
                "事实命题仍须逐条被 direct 决策的 support_claim 支撑，不得引入 support_claim 之外的新事实。"
            ),
            payload={
                "exam_point": exam_point.model_dump(mode="json"),
                "admitted_decisions": [
                    item.model_dump(mode="json") for item in batch
                ],
                "citable_chunk_ids": sorted(
                    decision.evidence_chunk_id
                    for decision in batch
                    if decision.relevance_class is RelevanceClass.DIRECT
                ),
                "chunks": [
                    {
                        "evidence_chunk_id": chunk.id,
                        "material_version_id": chunk.material_version_id,
                        "content": chunk.content,
                        "locator": chunk.locator,
                        "role": (
                            "direct"
                            if any(
                                decision.evidence_chunk_id == chunk.id
                                and decision.relevance_class is RelevanceClass.DIRECT
                                for decision in batch
                            )
                            else "supporting"
                        ),
                    }
                    for chunk in batch_grounding
                ],
            },
            temperature=0.0,
            call_context=call_context,
            response_validator=validate_response,
            max_tokens=_CONSOLIDATION_MAX_TOKENS,
        )

        # 合并各批卡片并去重（跨批语义键相同的卡片只保留一张）。
        merged_cards: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for units in parsed:
            for unit in units:
                for card in unit.cards:
                    name_key = semantic_text_key(card.name)
                    if name_key in seen_names:
                        continue
                    seen_names.add(name_key)
                    merged_cards.append(card.model_dump(mode="json"))
        if not merged_cards:
            return []
        units = [
            AssessmentUnitDraft.model_validate(
                {
                    "code": f"{exam_point.code}-U1",
                    "title": exam_point.title,
                    "performance_statement": exam_point.assessment_requirement,
                    "exam_point_code": exam_point.code,
                    "scope_boundary": {},
                    "cards": merged_cards,
                }
            )
        ]
        _validate_consolidated_units(exam_point, admitted, units, chunks_by_id=chunks_by_id)
        return units


def _validate_consolidated_units(
    exam_point: ExamPoint,
    admitted: list[EvidenceDecision],
    units: list[AssessmentUnitDraft],
    *,
    chunks_by_id: dict[str, "StagingChunk"],
) -> None:
    direct_by_id = {
        decision.evidence_chunk_id: decision
        for decision in admitted
        if decision.relevance_class is RelevanceClass.DIRECT
    }
    # 支撑池取该考点全部 direct 证据：既含分类阶段 support_claim 概括，也含
    # 其对应 teaching chunk 的原文（经 assessable_fact_keys 按可评分事实边界
    # 切分为规范化 key）。模型归并时虽被要求逐字落在 support_claim，但 MiMo
    # 实际会用自己的措辞重述事实或把某条 chunk 原句浓缩成更短可评分句；把
    # 原文一并纳入支撑池，配合 fact_key_supported 的双向子串判定，可放行
    # 措辞变体而仍拒绝凭空编造。误引的 supporting id 在下方确定性剔除。
    point_evidence_keys = assessable_fact_keys(
        [
            *(
                decision.support_claim
                for decision in direct_by_id.values()
            ),
            *(
                chunks_by_id[evidence_id].content
                for evidence_id in direct_by_id
                if evidence_id in chunks_by_id
            ),
        ]
    )
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
            evidence_ids = [
                evidence_id
                for evidence_id in card.evidence_chunk_ids
                if evidence_id in direct_by_id
            ]
            if not evidence_ids:
                raise DeepSeekModelError(
                    "model_output_evidence_gap",
                    "knowledge card references no admitted direct evidence",
                )
            card.evidence_chunk_ids = evidence_ids
            if not all_facts_supported(
                assessable_fact_keys(card.assessable_content), point_evidence_keys
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
