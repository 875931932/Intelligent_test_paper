from __future__ import annotations

from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.blueprint.models import PlanItem
from app.schemas.generation import compile_question_generation_payload
from app.services.generation_service import validate_generated_question


class QuestionGateway(Protocol):
    def generate(self, payload): ...


class GenerationState(TypedDict, total=False):
    plan_items: list[dict]
    knowledge_cards: dict[str, dict]
    payloads: list[dict]
    questions: list[dict]


def build_generation_graph(gateway: QuestionGateway, *, max_repair_attempts: int = 2):
    def compile_payloads(state):
        payloads = []
        for raw_item in state["plan_items"]:
            item = PlanItem.model_validate(raw_item)
            payloads.append(compile_question_generation_payload(item, state["knowledge_cards"][item.card_id]))
        return {"payloads": payloads}

    def generate_questions(state):
        questions = []
        for raw_item, payload in zip(state["plan_items"], state["payloads"]):
            question = dict(gateway.generate(payload))
            question["question_type"] = raw_item["question_type"]
            question["score"] = raw_item["score"]
            quality = validate_generated_question(question)
            attempts = 0
            while quality["status"] != "pass" and attempts < max_repair_attempts:
                attempts += 1
                question = dict(gateway.generate(payload))
                question["question_type"] = raw_item["question_type"]
                question["score"] = raw_item["score"]
                quality = validate_generated_question(question)
            question["quality"] = quality
            questions.append(question)
        return {"questions": questions}

    graph = StateGraph(GenerationState)
    graph.add_node("compile_source_free_payloads", compile_payloads)
    graph.add_node("generate_and_quality_check", generate_questions)
    graph.add_edge(START, "compile_source_free_payloads")
    graph.add_edge("compile_source_free_payloads", "generate_and_quality_check")
    graph.add_edge("generate_and_quality_check", END)
    return graph.compile()
