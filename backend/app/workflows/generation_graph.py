from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.blueprint.models import PlanItem
from app.domain.generation.coverage import (
    CoverageDirective,
    CoveragePlanError,
    build_coverage_directives,
    compile_coverage_planning_payload,
)
from app.domain.generation.structure_signature import build_structure_signature
from app.schemas.generation import QuestionGenerationPayload, compile_question_generation_payload
from app.services.generation_service import audit_question_set, validate_generated_question


QUESTION_TYPES = ("single_choice", "true_false", "fill_blank", "short_answer", "comprehensive")


class QuestionGateway(Protocol):
    def plan_coverage(self, payload): ...

    def generate(self, payload): ...

    def audit_paper(self, payload): ...


class GenerationState(TypedDict, total=False):
    plan_items: list[dict]
    knowledge_cards: dict[str, dict]
    directives: list[dict]
    payloads: dict[int, QuestionGenerationPayload]
    single_choice_questions: list[dict]
    true_false_questions: list[dict]
    fill_blank_questions: list[dict]
    short_answer_questions: list[dict]
    comprehensive_questions: list[dict]
    questions: list[dict]
    conflicts: list[dict]
    repair_attempts: int
    recent_structure_signatures: list[dict]


def build_generation_graph(gateway: QuestionGateway, *, max_repair_attempts: int = 2, max_workers: int = 8, max_planning_attempts: int = 2):
    def plan_coverage(state):
        items = [PlanItem.model_validate(raw) for raw in state["plan_items"]]
        recent_signatures = state.get("recent_structure_signatures", [])
        payload = compile_coverage_planning_payload(
            items,
            state["knowledge_cards"],
            recent_structure_signatures=recent_signatures,
        )
        last_error: Exception | None = None
        for _ in range(max_planning_attempts):
            raw_plan = gateway.plan_coverage(payload)
            try:
                directives = build_coverage_directives(
                    items,
                    state["knowledge_cards"],
                    raw_plan,
                    recent_structure_signatures=recent_signatures,
                )
                return {"directives": [directive.model_dump() for directive in directives], "repair_attempts": 0}
            except CoveragePlanError as exc:
                last_error = exc
                revised_policy = dict(payload.global_policy)
                revision_instruction = str(exc)
                if exc.structure_key:
                    revision_instruction += f"；冲突 structure_key={exc.structure_key}"
                revised_policy["revision_instruction"] = revision_instruction
                payload = payload.model_copy(update={"global_policy": revised_policy})
        raise CoveragePlanError(f"全卷考查计划连续无效：{last_error}")

    def compile_payloads(state):
        directives = [CoverageDirective.model_validate(raw) for raw in state["directives"]]
        return {"payloads": {directive.item_index: compile_question_generation_payload(directive) for directive in directives}}

    def generate_one(directive: CoverageDirective, payload: QuestionGenerationPayload) -> dict:
        active_payload = payload
        attempts = 0
        while True:
            question = dict(gateway.generate(active_payload))
            question.update(
                {
                    "item_index": directive.item_index,
                    "question_type": directive.question_type,
                    "score": directive.score,
                    "cognitive_level": directive.cognitive_level,
                    "coverage_atom": directive.coverage_atom,
                    "answer_boundary": directive.answer_boundary,
                }
            )
            if directive.question_type == "comprehensive":
                raw_subquestions = question.get("subquestions")
                subquestions = raw_subquestions if isinstance(raw_subquestions, list) else []
                generated_actions = [
                    str(row.get("action") or "").strip()
                    for row in subquestions
                    if isinstance(row, dict)
                ]
                generated_boundaries = [
                    str(row.get("answer_boundary") or "").strip()
                    for row in subquestions
                    if isinstance(row, dict)
                ]
                subquestion_actions = generated_actions if generated_actions and all(generated_actions) else directive.subquestion_actions
                answer_boundaries = (
                    generated_boundaries
                    if generated_boundaries and all(generated_boundaries)
                    else directive.answer_boundaries
                )
                signature = build_structure_signature(
                    archetype=str(directive.comprehensive_archetype),
                    material_form=str(directive.material_form),
                    cognitive_sequence=directive.cognitive_sequence,
                    subquestion_actions=subquestion_actions,
                    answer_boundaries=answer_boundaries,
                )
                question.update(
                    {
                        "comprehensive_archetype": directive.comprehensive_archetype,
                        "material_form": directive.material_form,
                        "cognitive_sequence": directive.cognitive_sequence,
                        "structure_signature": signature.model_dump(),
                    }
                )
            else:
                for field in (
                    "comprehensive_archetype",
                    "material_form",
                    "cognitive_sequence",
                    "structure_signature",
                ):
                    question.pop(field, None)
            quality = validate_generated_question(question)
            if quality["status"] == "pass" or attempts >= max_repair_attempts:
                question["quality"] = quality
                return question
            attempts += 1
            active_payload = payload.model_copy(
                update={"teacher_revision_instruction": f"单题结构质检未通过：{quality['message']}。请只修复该问题并返回完整 JSON。"}
            )

    def make_type_node(question_type: str, output_field: str):
        def node(state):
            directives = [CoverageDirective.model_validate(raw) for raw in state["directives"] if raw["question_type"] == question_type]
            if not directives:
                return {output_field: []}
            with ThreadPoolExecutor(max_workers=min(max_workers, len(directives))) as pool:
                questions = list(pool.map(lambda directive: generate_one(directive, state["payloads"][directive.item_index]), directives))
            return {output_field: questions}

        return node

    def merge_and_audit(state):
        questions = []
        for question_type in QUESTION_TYPES:
            questions.extend(state.get(f"{question_type}_questions", []))
        questions.sort(key=lambda row: row["item_index"])
        return {"questions": questions, "conflicts": audit_whole_paper(questions)}

    def semantic_audit(questions: list[dict]) -> list[dict]:
        if len(questions) < 2:
            return []
        question_summaries = []
        for question in questions:
            summary = {
                    "item_index": question["item_index"],
                    "question_type": question["question_type"],
                    "stem": question.get("stem", ""),
                    "options": question.get("options", []),
                    "answer": question.get("answer"),
                    "coverage_atom": question.get("coverage_atom", ""),
                    "answer_boundary": question.get("answer_boundary", ""),
                }
            if question.get("question_type") == "comprehensive":
                signature = question.get("structure_signature")
                summary.update(
                    {
                        "comprehensive_archetype": question.get("comprehensive_archetype"),
                        "material_form": question.get("material_form"),
                        "structure_key": signature.get("structure_key") if isinstance(signature, dict) else None,
                    }
                )
            question_summaries.append(summary)
        payload = {
            "questions": question_summaries,
            "policy": {
                "same_card_reuse_requires_distinct_atoms": True,
                "cross_question_answer_leakage": "block",
                "repair_only_later_or_more_redundant_item": True,
            },
        }
        raw = gateway.audit_paper(payload)
        rows = raw.get("conflicts", []) if isinstance(raw, dict) else []
        if isinstance(rows, dict):
            rows = rows.get("items", [])
        if not isinstance(rows, list):
            return []
        valid_indexes = {question["item_index"] for question in questions}
        conflicts = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            indexes = [int(value) for value in row.get("item_indexes", []) if int(value) in valid_indexes]
            repair_index = int(row.get("repair_item_index", indexes[-1] if indexes else -1))
            if len(indexes) < 2 or repair_index not in valid_indexes:
                continue
            conflicts.append(
                {
                    "code": str(row.get("code") or "semantic_overlap"),
                    "item_indexes": indexes,
                    "repair_item_index": repair_index,
                    "message": str(row.get("message") or "全卷语义审查发现题目重复或答案泄漏"),
                }
            )
        return conflicts

    def audit_whole_paper(questions: list[dict]) -> list[dict]:
        deterministic = audit_question_set(questions)
        return deterministic if deterministic else semantic_audit(questions)

    def repair_conflicts(state):
        repair_indexes = sorted({int(conflict["repair_item_index"]) for conflict in state["conflicts"]})
        conflict_messages: dict[int, list[str]] = {index: [] for index in repair_indexes}
        for conflict in state["conflicts"]:
            conflict_messages[int(conflict["repair_item_index"])].append(str(conflict["message"]))
        directives = {raw["item_index"]: CoverageDirective.model_validate(raw) for raw in state["directives"]}
        replacements: dict[int, dict] = {}
        for item_index in repair_indexes:
            directive = directives[item_index]
            base_payload = state["payloads"][item_index]
            instruction = "全卷审查发现答案泄漏或题目冲突：" + "；".join(conflict_messages[item_index])
            has_duplicate_structure = any(
                conflict.get("code") == "duplicate_comprehensive_structure"
                for conflict in state["conflicts"]
                if int(conflict["repair_item_index"]) == item_index
            )
            if has_duplicate_structure:
                instruction += "。这是后一道综合题，请保持考查原子不变，调整分问动作或结构化答案边界以形成不同的来源无关结构；同时改写题干，避免暴露其他题答案。"
            else:
                instruction += "。保持指定考查原子和答案边界不变，改写题干及选项，避免暴露其他题答案，并使用直接常用的术语、减少括号解释。"
            repaired = generate_one(directive, base_payload.model_copy(update={"teacher_revision_instruction": instruction}))
            replacements[item_index] = repaired
        questions = [replacements.get(question["item_index"], question) for question in state["questions"]]
        questions.sort(key=lambda row: row["item_index"])
        return {"questions": questions, "repair_attempts": state.get("repair_attempts", 0) + 1}

    def audit_after_repair(state):
        return {"conflicts": audit_whole_paper(state["questions"])}

    def route_after_audit(state):
        if state.get("conflicts") and state.get("repair_attempts", 0) < max_repair_attempts:
            return "repair_conflicts"
        return "finalize"

    def finalize(state):
        conflicted = {int(conflict["repair_item_index"]) for conflict in state.get("conflicts", [])}
        questions = []
        for question in state["questions"]:
            result = dict(question)
            if result["item_index"] in conflicted or result.get("quality", {}).get("status") != "pass":
                result["quality"] = {
                    "status": "needs_review",
                    "code": "cross_question_conflict" if result["item_index"] in conflicted else result.get("quality", {}).get("code", "invalid_question"),
                    "message": "达到局部修复上限，需教师复核",
                }
            questions.append(result)
        return {"questions": questions}

    graph = StateGraph(GenerationState)
    graph.add_node("plan_whole_paper_coverage", plan_coverage)
    graph.add_node("compile_source_free_payloads", compile_payloads)
    generation_nodes = []
    for question_type in QUESTION_TYPES:
        node_name = f"generate_{question_type}"
        graph.add_node(node_name, make_type_node(question_type, f"{question_type}_questions"))
        generation_nodes.append(node_name)
    graph.add_node("merge_and_cross_question_audit", merge_and_audit)
    graph.add_node("repair_conflicts", repair_conflicts)
    graph.add_node("audit_after_repair", audit_after_repair)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "plan_whole_paper_coverage")
    graph.add_edge("plan_whole_paper_coverage", "compile_source_free_payloads")
    for node_name in generation_nodes:
        graph.add_edge("compile_source_free_payloads", node_name)
    graph.add_edge(generation_nodes, "merge_and_cross_question_audit")
    graph.add_conditional_edges("merge_and_cross_question_audit", route_after_audit, {"repair_conflicts": "repair_conflicts", "finalize": "finalize"})
    graph.add_edge("repair_conflicts", "audit_after_repair")
    graph.add_conditional_edges("audit_after_repair", route_after_audit, {"repair_conflicts": "repair_conflicts", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile()
