from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.blueprint.models import AssessmentMode
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS, ComprehensiveArchetype, MaterialForm
from app.domain.generation.batching import QuestionBatch
from app.domain.generation.question_formats import (
    COURSE_TYPE_FORMATS_KEY,
    QUESTION_SCHEMAS,
    QUESTION_TEMPLATES,
)

# 题型任务卡与生成校验的对应关系。这里显式列出而不是让调用方散着判断：
# 多选题校验要求"两个及以上正确项"、单选题要求"唯一"，二者都依赖选项集合。
MULTI_ANSWER_TYPES = {"multiple_choice"}


def _comprehensive_template_and_schema(
    archetype: ComprehensiveArchetype | None,
    subquestion_count_range: list[int] | None,
) -> tuple[str, dict]:
    """综合题模板拼装：原型模板 + 结构要求（+ 原型专属格式规则）与分问数量约束的 schema。"""
    if archetype is None:
        raise ValueError("comprehensive directive requires an archetype contract")
    contract = ARCHETYPE_CONTRACTS[archetype]
    question_template = contract.question_template + " 结构要求：" + "；".join(contract.structure_requirements)
    if contract.extra_format_rule:
        question_template += " " + contract.extra_format_rule
    output_schema = {
        "type": "object",
        "required": ["stem", "subquestions", "answer", "explanation", "rubric"],
        "stem": "string",
        "subquestions": {
            "type": "array",
            "min_items": subquestion_count_range[0],
            "max_items": subquestion_count_range[1],
            "items": {
                "type": "object",
                "required": ["action", "prompt", "answer_boundary", "answer", "rubric", "score"],
                "properties": {
                    "action": "string",
                    "prompt": "string",
                    "answer_boundary": "string",
                    "answer": "string",
                    "rubric": "array",
                    "score": "number（该分问分值，各分问分值之和等于本题总分）",
                },
            },
        },
        "answer": "string",
        "explanation": "string",
        "rubric": "array",
    }
    return question_template, output_schema


class BatchQuestionSpec(BaseModel):
    """批内单题的生成规格。"""
    model_config = ConfigDict(extra="forbid")

    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: AssessmentMode = "conceptual"
    card_name: str = ""
    performance_statement: str = ""
    scope_boundary: dict = Field(default_factory=dict)
    prompt_material: list[str] = Field(default_factory=list)
    coverage_atom: str
    answer_boundary: str
    preferred_terms: list[str] = Field(default_factory=list)
    comprehensive_archetype: ComprehensiveArchetype | None = None
    material_form: MaterialForm | None = None
    cognitive_sequence: list[str] = Field(default_factory=list)
    subquestion_count_range: list[int] | None = None
    subquestion_actions: list[str] = Field(default_factory=list)
    answer_boundaries: list[str] = Field(default_factory=list)
    # 本题位的禁用清单（合同口径：同考点全部兄弟题位已用的原子与答案核心）。
    # 随题下发，让"模型看到的"与"校验时查的"是同一份清单——否则模型会因
    # 从未被告知的内容被校验判为泄漏。
    forbidden_atoms: list[str] = Field(default_factory=list)
    forbidden_answer_cores: list[str] = Field(default_factory=list)
    question_template: str
    output_schema: dict


class BatchGenerationPayload(BaseModel):
    """一次模型调用生成整批题目的载荷（同批互见）。"""
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    exam_point_ids: list[str]
    questions: list[BatchQuestionSpec]
    forbidden_atoms: list[str] = Field(default_factory=list)
    forbidden_answer_cores: list[str] = Field(default_factory=list)
    batch_instruction: str
    output_schema: dict
    teacher_revision_instruction: str = ""


def _template_and_schema_for(question_type: str):
    """取题型的任务卡与输出 schema。

    未定义题型的报清晰错误，而不是抛 KeyError——后者会在生成图的重试路径
    （compile 在 try 之外）直接把整个 batch 节点打崩，连带同批其他题位。
    """
    if question_type == "comprehensive":
        raise ValueError("comprehensive 需由调用方传入原型与分问范围")
    try:
        return QUESTION_TEMPLATES[question_type], QUESTION_SCHEMAS[question_type]
    except KeyError:
        raise ValueError(
            f"题型 {question_type!r} 没有任务卡：蓝图/合同层应在上游过滤，"
            f"当前支持 {sorted(QUESTION_TEMPLATES)}"
        ) from None


def compile_batch_generation_payload(
    batch: QuestionBatch, knowledge_cards: dict[str, dict]
) -> BatchGenerationPayload:
    # 课程级题型格式覆盖（类别预设 + 考核规则 type_formats，runner 经约定键注入）：
    # 逐键**替换**任务卡文本；output_schema 恒用全局档案——schema 描述的是 JSON
    # 字段形状，与出题风格无关。综合题按原型 key（archetype 名）同样可覆盖文本，
    # 分问结构与 schema 仍由原型档案确定性给出。
    overrides = knowledge_cards.get(COURSE_TYPE_FORMATS_KEY)
    overrides = overrides if isinstance(overrides, dict) else {}
    specs: list[BatchQuestionSpec] = []
    for slot in batch.slots:
        card = knowledge_cards.get(slot.card_id, {})
        if slot.question_type == "comprehensive":
            question_template, output_schema = _comprehensive_template_and_schema(
                slot.comprehensive_archetype, slot.subquestion_count_range,
            )
            custom = overrides.get(slot.comprehensive_archetype or "")
            if isinstance(custom, str) and custom.strip():
                question_template = custom.strip()
        else:
            question_template, output_schema = _template_and_schema_for(slot.question_type)
            custom = overrides.get(slot.question_type)
            if isinstance(custom, str) and custom.strip():
                question_template = custom.strip()
        specs.append(BatchQuestionSpec(
            item_index=slot.item_index,
            question_type=slot.question_type,
            score=slot.score,
            difficulty=slot.difficulty,
            cognitive_level=slot.cognitive_level,
            assessment_mode=slot.assessment_mode,
            card_name=str(card.get("name", "") or ""),
            performance_statement=slot.performance_statement or (card.get("performance_statement") or ""),
            scope_boundary=slot.scope_boundary or card.get("scope_boundary", {}) or {},
            prompt_material=slot.prompt_material or list(card.get("prompt_material", []) or []),
            coverage_atom=slot.coverage_atom,
            answer_boundary=slot.answer_boundary,
            preferred_terms=slot.preferred_terms or list(card.get("preferred_terms", []) or []),
            comprehensive_archetype=slot.comprehensive_archetype,
            material_form=slot.material_form,
            cognitive_sequence=slot.cognitive_sequence,
            subquestion_count_range=slot.subquestion_count_range,
            subquestion_actions=slot.subquestion_actions,
            answer_boundaries=slot.answer_boundaries,
            forbidden_atoms=list(slot.forbidden_context.atoms),
            forbidden_answer_cores=list(slot.forbidden_context.answer_cores),
            question_template=question_template,
            output_schema=output_schema,
        ))
    instruction = (
        f"为本批 {len(specs)} 道题目一次性命题，返回 JSON 对象（顶层字段 questions 为数组），数组每个元素必须含 item_index 字段及对应 output_schema 要求的全部字段。"
        "这些字段必须平铺在元素顶层（与 item_index 同级），不得嵌套进 output_schema 键内。"
        "同批各题考查视角必须互补：题型与认知层级已指定，不得从同一角度重复考查同一内容。"
        "每题的 card_name 是该知识卡的概念语境：题干涉及参数、命令或工具特性时，"
        "必须写清其归属（哪个框架/工具/流程的参数），使题干脱离语境仍可独立理解，不得出现无主语的参数或命令。"
        "每题的 forbidden_atoms 与 forbidden_answer_cores 是**该题**不得使用的原子与答案核心，"
        "它们不得出现在这道题的题干、选项、答案或解析中。"
    )
    if batch.forbidden_context.atoms or batch.forbidden_context.answer_cores:
        instruction += "严格执行上述禁用清单，任何泄漏都视为废题。"
    return BatchGenerationPayload(
        batch_id=batch.batch_id,
        exam_point_ids=list(batch.exam_point_ids),
        questions=specs,
        forbidden_atoms=list(batch.forbidden_context.atoms),
        forbidden_answer_cores=list(batch.forbidden_context.answer_cores),
        batch_instruction=instruction,
        output_schema={"type": "object", "questions": "array — 每个元素为单题对象，须含 item_index 与该题 output_schema 字段"},
    )
