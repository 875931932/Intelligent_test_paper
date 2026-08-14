from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.blueprint.models import PlanItem
from app.domain.generation.coverage import CoverageDirective


class EvidenceTracePack(BaseModel):
    course_id: str
    knowledge_card_id: str
    evidence_ids: list[str]


class QuestionGenerationPayload(BaseModel):
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    performance_statement: str
    scope_boundary: dict
    assessable_content: list[str]
    coverage_atom: str = ""
    answer_boundary: str = ""
    preferred_terms: list[str] = Field(default_factory=list)
    novelty_contract: str = ""
    generation_policy: dict = Field(default_factory=dict)
    expression_policy: dict = Field(default_factory=lambda: {"prefer_direct_common_terms": True, "max_parenthetical_pairs": 1})
    question_template: str
    output_schema: dict
    teacher_revision_instruction: str = ""


def compile_question_generation_payload(plan_item: PlanItem | CoverageDirective, knowledge_card: dict | None = None) -> QuestionGenerationPayload:
    templates = {
        "single_choice": "给出一个明确问题、四个互斥选项和唯一正确答案",
        "true_false": "给出一个可判定陈述并提供正确或错误答案",
        "fill_blank": "给出一个只考查术语、定义、条件或核心结论的理论填空题；答案必须简短、唯一，不设计实际场景、开放分析或多步骤应用",
        "short_answer": "要求学生解释原理、比较方法或解决问题，并给出评分点",
        "comprehensive": "给出有真实问题背景的综合分析或方案设计题，设置相互关联的分问，并给出完整答案、解析和逐点评分细则",
    }
    schemas = {
        "single_choice": {"stem": "string", "options": "array[4]", "answer": "string"},
        "true_false": {"stem": "string", "answer": "boolean"},
        "fill_blank": {"stem": "string", "answer": "string"},
        "short_answer": {"stem": "string", "answer": "string", "explanation": "string", "rubric": "array"},
        "comprehensive": {"stem": "string", "subquestions": "array", "answer": "string", "explanation": "string", "rubric": "array"},
    }
    if isinstance(plan_item, CoverageDirective):
        return QuestionGenerationPayload(
            question_type=plan_item.question_type,
            score=plan_item.score,
            difficulty=plan_item.difficulty,
            cognitive_level=plan_item.cognitive_level,
            performance_statement=plan_item.performance_statement,
            scope_boundary=plan_item.scope_boundary,
            assessable_content=plan_item.assessable_content,
            coverage_atom=plan_item.coverage_atom,
            answer_boundary=plan_item.answer_boundary,
            preferred_terms=plan_item.preferred_terms,
            novelty_contract=plan_item.novelty_contract,
            generation_policy=plan_item.generation_policy,
            question_template=templates[plan_item.question_type],
            output_schema=schemas[plan_item.question_type],
        )
    if knowledge_card is None:
        raise TypeError("knowledge_card is required for a PlanItem")
    return QuestionGenerationPayload(
        question_type=plan_item.question_type,
        score=plan_item.score,
        difficulty=plan_item.difficulty,
        cognitive_level=plan_item.cognitive_level,
        performance_statement=knowledge_card["performance_statement"],
        scope_boundary=knowledge_card.get("scope_boundary", {}),
        assessable_content=knowledge_card["assessable_content"],
        preferred_terms=knowledge_card.get("preferred_terms", []),
        question_template=templates[plan_item.question_type],
        output_schema=schemas[plan_item.question_type],
    )
