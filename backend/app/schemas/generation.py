from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.blueprint.models import AssessmentMode, PlanItem
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS, ComprehensiveArchetype, MaterialForm
from app.domain.generation.batching import QuestionBatch
from app.domain.generation.coverage import CoverageDirective

_QUESTION_TEMPLATES = {
    "single_choice": (
        "给出一个明确问题和四个互斥选项。"
        "题干必须自包含——不依赖题外资料即可理解。"
        "四个选项应围绕同一维度展开（如同一概念的不同定义、同一问题的不同方案），"
        "干扰项应有 plausible 的迷惑性但不能有明显错误。"
        "正确答案必须唯一且无歧义。"
        "选项长度应大致均衡，避免正确答案因长度异常而被猜出。"
    ),
    "true_false": (
        "给出一个可判定真伪的陈述句。"
        "陈述必须明确到可以简单判断'对/错'的程度，不能含糊或有争议。"
        "避免使用'总是'、'从不'等绝对化词汇（除非确实绝对正确）。"
        "错误陈述的错误点应隐蔽但明确，不应是显而易见的常识性错误。"
    ),
    "fill_blank": (
        "给出一个只考查术语、定义、条件或核心结论的理论填空题。"
        "答案必须简短、唯一，不设计实际场景、开放分析或多步骤应用。"
        "题干中恰好包含 1 个空，用连续下划线（不少于 4 个下划线字符）表示。"
        "严禁出现 2 个及以上的空。"
        "空的位置应放在句末或句中关键位置，不应放在句首。"
    ),
    "short_answer": (
        "要求学生解释原理、比较方法或解决问题，并给出评分点。"
        "题干应明确问题的范围和期望的回答深度。"
        "答案应为2-5句话的核心要点，rubric 应列出关键的评分要素。"
        "explanation 应解释为什么正确答案是正确的，以及常见错误。"
    ),
}

_QUESTION_SCHEMAS = {
    "single_choice": {
        "stem": "string — 自包含的题干，不依赖外部资料",
        "options": "array[4] — 四个互斥选项，按同一维度排列，长度均衡",
        "answer": "string — 唯一正确答案，必须与某一选项完全一致",
    },
    "true_false": {
        "stem": "string — 可明确判定真伪的陈述句",
        "answer": "boolean — true 或 false",
    },
    "fill_blank": {
        "stem": "string — 恰好含 1 处连续下划线空（不少于4个_），空在句中或句末",
        "answer": "string — 简短唯一的术语、数值或短语",
    },
    "short_answer": {
        "stem": "string — 明确问题和期望回答深度",
        "answer": "string — 2-5句话的核心要点",
        "explanation": "string — 解释答案正确性和常见错误",
        "rubric": "array — 评分要素列表",
    },
}


def _comprehensive_template_and_schema(
    archetype: ComprehensiveArchetype | None,
    subquestion_count_range: list[int] | None,
) -> tuple[str, dict]:
    """综合题模板拼装：原型模板 + 结构要求（+ 挖空规则）与分问数量约束的 schema。"""
    if archetype is None:
        raise ValueError("comprehensive directive requires an archetype contract")
    contract = ARCHETYPE_CONTRACTS[archetype]
    question_template = contract.question_template + " 结构要求：" + "；".join(contract.structure_requirements)
    if archetype == "code_completion_scenario":
        code_blank_rule = (
            " 挖空格式要求：每处空写成 ____________(编号)__________ 的形式并按 (1)(2)(3) 顺延编号，"
            "共 4 至 6 处；代码其余部分保持完整、缩进清晰；"
            "分问（1）的 prompt 写‘请在不改变整体结构的前提下补全代码’，answer 逐空给出编号与答案值；"
            "分问（2）围绕该场景的一个真实运行或优化问题，answer 给出原因分析和改进方向。"
        )
        question_template += code_blank_rule
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


class EvidenceTracePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str
    knowledge_card_id: str
    evidence_ids: list[str]


class QuestionGenerationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: AssessmentMode = "conceptual"
    performance_statement: str
    scope_boundary: dict
    assessable_content: list[str]
    prompt_material: list[str] = Field(default_factory=list)
    coverage_atom: str = ""
    answer_boundary: str = ""
    preferred_terms: list[str] = Field(default_factory=list)
    novelty_contract: str = ""
    generation_policy: dict = Field(default_factory=dict)
    comprehensive_archetype: ComprehensiveArchetype | None = None
    material_form: MaterialForm | None = None
    cognitive_sequence: list[str] = Field(default_factory=list)
    subquestion_count_range: list[int] | None = None
    subquestion_actions: list[str] = Field(default_factory=list)
    answer_boundaries: list[str] = Field(default_factory=list)
    expression_policy: dict = Field(default_factory=lambda: {
        "prefer_direct_common_terms": True,
        "max_parenthetical_pairs": 1,
        "difficulty_guidance": {
            "low": "使用课程常用术语，直接提问，不设置场景干扰",
            "medium": "可以使用简单场景包装，但核心考查点应清晰可辨",
            "high": "可以使用复杂场景或多步骤推理，需要学生综合运用多个知识点",
        },
    })
    question_template: str
    output_schema: dict
    teacher_revision_instruction: str = ""


def compile_question_generation_payload(plan_item: PlanItem | CoverageDirective, knowledge_card: dict | None = None) -> QuestionGenerationPayload:
    if isinstance(plan_item, CoverageDirective):
        if plan_item.question_type == "comprehensive":
            question_template, output_schema = _comprehensive_template_and_schema(
                plan_item.comprehensive_archetype, plan_item.subquestion_count_range,
            )
        else:
            question_template = _QUESTION_TEMPLATES[plan_item.question_type]
            output_schema = _QUESTION_SCHEMAS[plan_item.question_type]
        return QuestionGenerationPayload(
            question_type=plan_item.question_type,
            score=plan_item.score,
            difficulty=plan_item.difficulty,
            cognitive_level=plan_item.cognitive_level,
            assessment_mode=plan_item.assessment_mode,
            performance_statement=plan_item.performance_statement,
            scope_boundary=plan_item.scope_boundary,
            assessable_content=plan_item.assessable_content,
            prompt_material=plan_item.prompt_material,
            coverage_atom=plan_item.coverage_atom,
            answer_boundary=plan_item.answer_boundary,
            preferred_terms=plan_item.preferred_terms,
            novelty_contract=plan_item.novelty_contract,
            generation_policy=plan_item.generation_policy,
            comprehensive_archetype=plan_item.comprehensive_archetype,
            material_form=plan_item.material_form,
            cognitive_sequence=plan_item.cognitive_sequence,
            subquestion_count_range=plan_item.subquestion_count_range,
            subquestion_actions=plan_item.subquestion_actions,
            answer_boundaries=plan_item.answer_boundaries,
            question_template=question_template,
            output_schema=output_schema,
        )
    if knowledge_card is None:
        raise TypeError("knowledge_card is required for a PlanItem")
    if plan_item.question_type == "comprehensive":
        raise TypeError("comprehensive questions require a planned CoverageDirective")
    raw_prompt_material = knowledge_card.get("prompt_material", [])
    prompt_material = [raw_prompt_material] if isinstance(raw_prompt_material, str) else list(raw_prompt_material or [])
    return QuestionGenerationPayload(
        question_type=plan_item.question_type,
        score=plan_item.score,
        difficulty=plan_item.difficulty,
        cognitive_level=plan_item.cognitive_level,
        assessment_mode=plan_item.assessment_mode,
        performance_statement=knowledge_card["performance_statement"],
        scope_boundary=knowledge_card.get("scope_boundary", {}),
        assessable_content=knowledge_card["assessable_content"],
        prompt_material=prompt_material,
        preferred_terms=knowledge_card.get("preferred_terms", []),
        question_template=_QUESTION_TEMPLATES[plan_item.question_type],
        output_schema=_QUESTION_SCHEMAS[plan_item.question_type],
    )


class BatchQuestionSpec(BaseModel):
    """批内单题的生成规格。"""
    model_config = ConfigDict(extra="forbid")

    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: AssessmentMode = "conceptual"
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


def compile_batch_generation_payload(
    batch: QuestionBatch, knowledge_cards: dict[str, dict]
) -> BatchGenerationPayload:
    specs: list[BatchQuestionSpec] = []
    for slot in batch.slots:
        card = knowledge_cards.get(slot.card_id, {})
        if slot.question_type == "comprehensive":
            question_template, output_schema = _comprehensive_template_and_schema(
                slot.comprehensive_archetype, slot.subquestion_count_range,
            )
        else:
            question_template = _QUESTION_TEMPLATES[slot.question_type]
            output_schema = _QUESTION_SCHEMAS[slot.question_type]
        specs.append(BatchQuestionSpec(
            item_index=slot.item_index,
            question_type=slot.question_type,
            score=slot.score,
            difficulty=slot.difficulty,
            cognitive_level=slot.cognitive_level,
            assessment_mode=slot.assessment_mode,
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
            question_template=question_template,
            output_schema=output_schema,
        ))
    instruction = (
        f"为本批 {len(specs)} 道题目一次性命题，返回 JSON 对象（顶层字段 questions 为数组），数组每个元素必须含 item_index 字段及对应 output_schema 要求的全部字段。"
        "同批各题考查视角必须互补：题型与认知层级已指定，不得从同一角度重复考查同一内容。"
        "forbidden_atoms 与 forbidden_answer_cores 是同考点其他题目已使用的原子与答案核心，"
        "它们不得出现在本批任何题干、选项或答案文本中。"
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
