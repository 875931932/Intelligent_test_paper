"""Contracts and deterministic admission for exam-point evidence relevance."""

from __future__ import annotations

import re
import unicodedata
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
    embedding: list[float] | None = None


# 情境绑定语：把内容锚定到特定实验运行（案例讲解的叙述背景）而非可迁移
# 知识。知识卡是 RAG 检索库的源头，案例叙述（"失衡问题出现在上一轮训练中"
# "本次实验使用 Qwen3-0.6B"）必须在入库前剥离情境或丢弃——只保留其承载
# 的通用结论（"混合数据集用于解决两类数据失衡"）。
# 指示词与运行词之间允许 ≤12 个非标点字符（如"本次 QLoRA 微调实验"），
# 避免误伤"上一轮迭代采样"类通用机制描述。
SITUATIONAL_BINDING_LANGUAGE = re.compile(
    r"(?:上一轮|本轮|该轮|这轮|本次|这次|我们的)[^，。；;]{0,12}?(?:实验|训练|微调|运行|实践)",
    re.IGNORECASE,
)


def is_transferable_fact(value: str) -> bool:
    """事实是否为可迁移知识（未绑定特定实验运行的情境）。"""

    return not SITUATIONAL_BINDING_LANGUAGE.search(str(value or "").strip())


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


class EvidenceDecision(BaseModel):
    exam_point_code: str
    evidence_chunk_id: str
    relevance_class: RelevanceClass
    support_claim: str
    evidence_role: str | None = None
    content_kind: ContentKind
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
    def classify_file(
        self,
        *,
        exam_points: list[ExamPoint],
        material_version_id: str,
        chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> list[ExamPointFileDecision]: ...


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
    normalized_role = (decision.evidence_role or "").strip().casefold()
    if normalized_role not in DIRECT_EVIDENCE_ROLES:
        raise ValueError("direct evidence requires a fact or rubric evidence role")


_SEMANTIC_OPERATOR_ALIASES = {
    "<=>": "<=>",
    "⇔": "<=>",
    "↔": "<=>",
    "⇒": "->",
    "→": "->",
    "=>": "->",
    "≥": ">=",
    "≤": "<=",
    "≠": "!=",
    "∧": "&&",
    "∨": "||",
    "¬": "!",
    "×": "*",
    "÷": "/",
}
_SEMANTIC_OPERATOR_PATTERN = re.compile(
    "|".join(
        re.escape(operator)
        for operator in sorted(_SEMANTIC_OPERATOR_ALIASES, key=len, reverse=True)
    )
)
_SEMANTIC_DELIMITERS = frozenset("()[]{}")
_RAG_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])RAG(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _is_semantic_word_character(character: str) -> bool:
    category = unicodedata.category(character)
    return character == "_" or category[0] in {"L", "M", "N"}


def _semantic_tokens(value: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    cursor = 0
    while cursor < len(value):
        character = value[cursor]
        if character.isspace():
            end = cursor + 1
            while end < len(value) and value[end].isspace():
                end += 1
            tokens.append(("whitespace", " "))
        elif _is_semantic_word_character(character):
            end = cursor + 1
            while end < len(value) and _is_semantic_word_character(value[end]):
                end += 1
            tokens.append(("word", value[cursor:end]))
        elif character in _SEMANTIC_DELIMITERS:
            end = cursor + 1
            tokens.append(("delimiter", character))
        else:
            end = cursor + 1
            while (
                end < len(value)
                and not value[end].isspace()
                and not _is_semantic_word_character(value[end])
                and value[end] not in _SEMANTIC_DELIMITERS
            ):
                end += 1
            tokens.append(("operator", value[cursor:end]))
        cursor = end
    return tokens


def _normalize_semantic_width(value: str) -> str:
    """Normalize common fullwidth ASCII without compatibility-folding notation."""

    normalized = unicodedata.normalize("NFC", value)
    return "".join(
        " "
        if character == "\u3000"
        else chr(ord(character) - 0xFEE0)
        if "\uff01" <= character <= "\uff5e"
        else character
        for character in normalized
    )


def semantic_text_key(value: str) -> str:
    """Return a fail-closed key without erasing semantic text structure."""

    normalized = _normalize_semantic_width(value)
    normalized = normalized.replace("检索增强生成", "RAG")
    normalized = _RAG_TERM_PATTERN.sub("RAG", normalized)
    normalized = _SEMANTIC_OPERATOR_PATTERN.sub(
        lambda match: _SEMANTIC_OPERATOR_ALIASES[match.group(0)],
        normalized,
    )
    key_parts: list[str] = []
    previous_kind: str | None = None
    pending_whitespace = False
    for kind, token in _semantic_tokens(normalized):
        if kind == "whitespace":
            pending_whitespace = True
            continue
        if (
            pending_whitespace
            and key_parts
            and (previous_kind, kind) in {("word", "word"), ("operator", "operator")}
        ):
            key_parts.append(" ")
        key_parts.append(token)
        previous_kind = kind
        pending_whitespace = False
    return "".join(key_parts)


def assessable_fact_keys(values: list[str]) -> frozenset[str]:
    """Split safe fact boundaries and preserve comparison/math semantics."""

    return frozenset(
        key
        for value in values
        for atom in re.split(r"[;；\r\n]+", value)
        if (key := semantic_text_key(atom.strip()))
    )


# 归属限定语的最短证据 key：更短的 key 作子串会产生误命中（如"用于控制"），
# 只有足够长的证据事实才允许作为被包裹的核心参与包含判定。
_MIN_WRAPPED_EVIDENCE_KEY_LENGTH = 6

# 语义兜底的最小实词长度：更短的连续汉字/词条（如"中""的""用"）作比对噪声
# 会稀释覆盖率，造成误判。低于该长度的词条不参与语义覆盖率判定。
_MIN_SEMANTIC_TOKEN_LEN = 2
# 卡片事实语义词条被证据覆盖的最小占比：低于该比例即视为未被支撑（编造）。
# 0.66 在容忍措辞变体（措辞不同但实词重叠）与拒绝编造（新增实词过多）间折中。
_SEMANTIC_COVERAGE_MIN = 0.66


_CJK_TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]{2,}")
_ASCII_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]{2,}")
# 2字子串覆盖率阈值：容忍归并阶段插入的字（"步骤可将""参数"）与措辞变体，
# 又足以区分凭空编造（编造句与证据仅有"用于"这类高频泛动词重叠）。
_SEMANTIC_BIGRAM_COVERAGE_MIN = 0.5


def _semantic_token_set(value: str) -> set[str]:
    """抽取句子中的语义词条集合（连续汉字串 + 字母数字标识符）。

    用于证据语义覆盖率的近似判定：中文不做分词，直接取连续汉字串为
    一个实词（如"模型调用"），拉丁标识符（如"swift"、"infer"）原样保留。
    这样"swift infer命令用于进行模型调用"与"swift infer 命令用于模型调用"
    虽措辞细节不同，但共享的实词 token 高度重叠。
    """
    normalized = _normalize_semantic_width(value)
    tokens: set[str] = set()
    tokens.update(
        match.group(0) for match in _CJK_TOKEN_PATTERN.finditer(normalized)
    )
    tokens.update(
        match.group(0) for match in _ASCII_TOKEN_PATTERN.finditer(normalized)
    )
    return {token for token in tokens if len(token) >= _MIN_SEMANTIC_TOKEN_LEN}


def _semantic_bigram_set(value: str) -> set[str]:
    """抽取纯中文的 2 字子串集合。

    拉丁标识符（如 "swift"）的本质字面已由 ASCII token 层保障，中文连续汉字
    串做 2-gram 可容忍归并阶段在句中插入若干字（"步骤可将""参数"），仍能
    高比例保留原句字面；编造句则与证据仅有零星泛动词重叠。
    """
    normalized = _normalize_semantic_width(value)
    characters: list[str] = []
    buffer: list[str] = []
    for character in normalized:
        if "\u3400" <= character <= "\u9fff":
            buffer.append(character)
        else:
            if len(buffer) >= 2:
                characters.extend(buffer)
            buffer.clear()
    if len(buffer) >= 2:
        characters.extend(buffer)
    return {
        characters[index] + characters[index + 1]
        for index in range(len(characters) - 1)
    }


def fact_semantically_supported(atom_key: str, evidence_keys: frozenset[str]) -> bool:
    """语义兜底：卡片事实是否被证据集合中任一原文语义覆盖。

    两层宽松判定，任一命中即支撑：
    1. 语义词条覆盖率：卡片事实的实词 token 被任一证据原文覆盖；对拉丁
       标识符（swift/infer/LoRA）敏感。
    2. 中文 2-gram 覆盖率：卡片事实的连续字面 2 元组被任一证据覆盖；对
       归并阶段插入字/措辞变体稳健。
    两层都拒绝：卡片事实大量实词在证据中找不到，且字面连续性不匹配——
       即凭空编造。
    """
    atom_tokens = _semantic_token_set(atom_key)
    atom_bigrams = _semantic_bigram_set(atom_key)
    if not atom_tokens and not atom_bigrams:
        return False
    best_token_coverage = 0.0
    best_bigram_coverage = 0.0
    for evidence_key in evidence_keys:
        evidence_tokens = _semantic_token_set(evidence_key)
        if atom_tokens and evidence_tokens:
            overlap = atom_tokens & evidence_tokens
            best_token_coverage = max(
                best_token_coverage, len(overlap) / len(atom_tokens)
            )
        evidence_bigrams = _semantic_bigram_set(evidence_key)
        if atom_bigrams and evidence_bigrams:
            overlap = atom_bigrams & evidence_bigrams
            best_bigram_coverage = max(
                best_bigram_coverage, len(overlap) / len(atom_bigrams)
            )
    if best_token_coverage >= _SEMANTIC_COVERAGE_MIN:
        return True
    return best_bigram_coverage >= _SEMANTIC_BIGRAM_COVERAGE_MIN


def fact_key_supported(atom_key: str, evidence_keys: frozenset[str] | set[str]) -> bool:
    """判定一条归并原子是否被直接证据落地。

    落地方式（命中任一即放行）：
    1. 与证据事实的规范化 key 完全相等（严格口径）；
    2. 证据事实 key 是原子 key 的子串——归并阶段为碎片事实补全归属限定语
       （"ms-swift框架中，eval_batch_size参数用于控制评测批大小" 落在证据
       "eval_batch_size参数用于控制评测批大小"）；核心事实仍须逐字出现。
    3. 原子 key 是证据（chunk 原文长句）的子串——模型把教材原句浓缩成
       更短可评分事实（"swift infer 命令用于模型调用" 落在 chunk 原文句
       "ms-swift 中 swift infer 命令用于进行模型调用和多模态输入验证"）。
       浓缩不改变事实归属，仍被证据原文支撑；凭空编造不受任何长句包含。
    4. 语义兜底：任一证据原文能覆盖卡片事实 >=66% 的语义词条。措辞变体
       至此放行，但新增实词过多的编造依旧被拒。
    """

    if atom_key in evidence_keys:
        return True
    if any(
        (
            len(evidence_key) >= _MIN_WRAPPED_EVIDENCE_KEY_LENGTH
            and evidence_key in atom_key
        )
        or (
            len(atom_key) >= _MIN_WRAPPED_EVIDENCE_KEY_LENGTH
            and atom_key in evidence_key
        )
        for evidence_key in evidence_keys
    ):
        return True
    return fact_semantically_supported(atom_key, frozenset(evidence_keys))


def all_facts_supported(
    atom_keys: frozenset[str], evidence_keys: frozenset[str] | set[str]
) -> bool:
    """整卡原子是否全部被证据落地（逐条独立判定）。"""

    return bool(atom_keys) and all(
        fact_key_supported(atom_key, evidence_keys) for atom_key in atom_keys
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
            "prompt_material": (
                decision.prompt_material if keep_prompt_material else None
            ),
        }
    )


# 分类阶段不再要求模型输出 evidence_role；direct 决策的 evidence_role 由 content_kind
# 确定性推导。凡被准入为 direct 的证据，其 support_claim 本身就是该考点的答案事实依据，
# 统一落为 answer_basis，使覆盖判定（_ANSWER_BASIS_ROLES / missing_answer_or_rubric_basis）
# 不会因缺 answer/rubric 类角色而误判"覆盖不足"。
_CONTENT_KIND_TO_EVIDENCE_ROLE: dict[ContentKind, str] = {
    ContentKind.CONCEPT: "answer_basis",
    ContentKind.DEFINITION: "answer_basis",
    ContentKind.PRINCIPLE: "answer_basis",
    ContentKind.MECHANISM: "answer_basis",
    ContentKind.RULE: "answer_basis",
    ContentKind.RELATIONSHIP: "answer_basis",
    ContentKind.FACT: "answer_basis",
    ContentKind.CONSTRAINT: "answer_basis",
    ContentKind.FORMULA: "answer_basis",
    ContentKind.DERIVATION: "answer_basis",
    ContentKind.COMPARISON: "answer_basis",
    ContentKind.CASE: "answer_basis",
    ContentKind.SCENARIO: "answer_basis",
    ContentKind.DIAGNOSTIC: "answer_basis",
}


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

    # 分类阶段不再要求模型输出 evidence_role；从 content_kind 确定性推导答案角色，
    # 避免 direct 决策因缺失 evidence_role 被准入校验误拒。
    if not decision.evidence_role:
        decision = decision.model_copy(
            update={
                "evidence_role": _CONTENT_KIND_TO_EVIDENCE_ROLE.get(
                    decision.content_kind, "answer_basis"
                )
            }
        )

    validate_direct_evidence_decision(decision, exam_point_code=point.code)

    return decision.model_copy(deep=True)
