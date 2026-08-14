from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.blueprint.models import PlanItem


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
    question_template: str
    output_schema: dict
    teacher_revision_instruction: str = ""


def compile_question_generation_payload(plan_item: PlanItem, knowledge_card: dict) -> QuestionGenerationPayload:
    templates = {
        "single_choice": "给出一个明确问题、四个互斥选项和唯一正确答案",
        "true_false": "给出一个可判定陈述并提供正确或错误答案",
        "fill_blank": "给出一个有明确答案边界的填空题",
        "short_answer": "要求学生解释原理、比较方法或解决问题，并给出评分点",
    }
    schemas = {
        "single_choice": {"stem": "string", "options": "array[4]", "answer": "string"},
        "true_false": {"stem": "string", "answer": "boolean"},
        "fill_blank": {"stem": "string", "answer": "string"},
        "short_answer": {"stem": "string", "answer": "string", "explanation": "string", "rubric": "array"},
    }
    return QuestionGenerationPayload(
        question_type=plan_item.question_type, score=plan_item.score, difficulty=plan_item.difficulty, cognitive_level=plan_item.cognitive_level,
        performance_statement=knowledge_card["performance_statement"], scope_boundary=knowledge_card.get("scope_boundary", {}),
        assessable_content=knowledge_card["assessable_content"], question_template=templates[plan_item.question_type], output_schema=schemas[plan_item.question_type],
    )
