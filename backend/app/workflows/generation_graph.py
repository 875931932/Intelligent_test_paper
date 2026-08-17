"""合同驱动的试卷生成图。

build_batches → Send(batch_generate) 按考点批并行 → merge_and_check → END。
批内一次模型调用同批互见；跨批互斥由合同禁用上下文构造性保证；
单题失败带原因重试 ≤ max_retries，仍失败标记 needs_review，不阻塞整卷。
"""
from __future__ import annotations

import operator
import re
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.domain.generation.batching import QuestionBatch, split_contract_into_batches
from app.domain.generation.contract import ContractSlot, _normalized
from app.schemas.generation import compile_batch_generation_payload
from app.services.generation_service import audit_paper_against_contract, validate_generated_question


class BatchGateway(Protocol):
    def generate_batch(self, payload) -> list[dict]: ...


class GenerationState(TypedDict, total=False):
    contract: list[dict]
    knowledge_cards: dict[str, dict]
    batches: list[dict]
    questions: Annotated[list[dict], operator.add]
    model_call_count: Annotated[int, operator.add]
    final_check: dict


def _compact_text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def _longest_common_run(left: str, right: str) -> int:
    best = 0
    prev = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        cur = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _answer_hits_boundary(question: dict, boundary: str) -> bool:
    """答案与答案域兼容：与合同边界互含、共享 ≥3 连续字符片段，
    或（选择题）与某一选项一致——选项集即答案域的操作化。"""
    if not boundary:
        return True
    left = _normalized(str(question.get("answer") or ""))
    if not left:
        return False
    right = _normalized(boundary)
    if left in right or right in left:
        return True
    if _longest_common_run(left, right) >= 3:
        return True
    answer = str(question.get("answer"))
    return any(str(option) == answer for option in (question.get("options") or []))


def _check_question(question: dict, slot: ContractSlot) -> dict:
    """单题确定性校验：schema + 禁用上下文泄漏 + 答案命中边界。"""
    quality = validate_generated_question(question)
    reasons: list[str] = []
    if quality["status"] != "pass":
        reasons.append(quality["message"])
    surface = _compact_text([question.get("stem", ""), *(question.get("options") or [])])
    for atom in slot.forbidden_context.atoms:
        core = _normalized(atom)
        if len(core) >= 6 and core in surface:
            reasons.append(f"题干泄漏同考点其他题原子：{atom[:24]}…")
    for core_raw in slot.forbidden_context.answer_cores:
        core = _normalized(core_raw)
        if len(core) >= 6 and core in surface:
            reasons.append(f"题干泄漏同考点其他题答案核心：{core_raw[:24]}…")
    if not _answer_hits_boundary(question, slot.answer_boundary):
        reasons.append("答案未命中答案域")
    if not reasons:
        return {"status": "pass", "message": "通过合同校验"}
    return {"status": "blocker", "message": "；".join(reasons)}


def _stamp_question(question: dict, slot: ContractSlot) -> dict:
    """把合同溯源字段盖到题目上。"""
    question.update({
        "item_index": slot.item_index,
        "question_type": slot.question_type,
        "score": slot.score,
        "difficulty": slot.difficulty,
        "cognitive_level": slot.cognitive_level,
        "coverage_atom": slot.coverage_atom,
        "answer_boundary": slot.answer_boundary,
        "exam_point_id": slot.exam_point_id,
        "unit_id": slot.unit_id,
        "card_id": slot.card_id,
    })
    for field in ("comprehensive_archetype", "material_form", "cognitive_sequence"):
        if slot.question_type == "comprehensive":
            question[field] = getattr(slot, field)
        else:
            question.pop(field, None)
    return question


def _missing_question(slot: ContractSlot, message: str) -> dict:
    return {
        "item_index": slot.item_index, "question_type": slot.question_type,
        "score": slot.score, "difficulty": slot.difficulty,
        "cognitive_level": slot.cognitive_level,
        "coverage_atom": slot.coverage_atom, "answer_boundary": slot.answer_boundary,
        "exam_point_id": slot.exam_point_id, "unit_id": slot.unit_id,
        "card_id": slot.card_id,
        "quality": {"status": "blocker", "message": message},
        "needs_review": True,
    }


def build_generation_graph(gateway: BatchGateway, *, max_retries: int = 2):
    def build_batches(state: GenerationState) -> dict:
        slots = [ContractSlot.model_validate(raw) for raw in state["contract"]]
        batches = split_contract_into_batches(slots)
        return {"batches": [batch.model_dump(mode="json") for batch in batches]}

    def route_batches(state: GenerationState) -> list[Send]:
        sends = [Send("batch_generate", {
            "batch": batch, "knowledge_cards": state.get("knowledge_cards", {}),
        }) for batch in state.get("batches", [])]
        return sends or [Send("batch_generate", {"batch": None, "knowledge_cards": {}})]

    def batch_generate(payload: dict) -> dict:
        if payload.get("batch") is None:
            return {"questions": [], "model_call_count": 0}
        batch = QuestionBatch.model_validate(payload["batch"])
        cards = payload.get("knowledge_cards", {})
        calls = 0
        produced: dict[int, dict] = {}

        try:
            raw_questions = list(gateway.generate_batch(compile_batch_generation_payload(batch, cards)))
            calls += 1
        except Exception:
            raw_questions = []
            calls += 1

        for slot in sorted(batch.slots, key=lambda s: s.item_index):
            raw = next((q for q in raw_questions if q.get("item_index") == slot.item_index), None)
            if raw is None:
                produced[slot.item_index] = _missing_question(slot, "批返回缺失该题")
                continue
            question = _stamp_question(dict(raw), slot)
            quality = _check_question(question, slot)
            attempts = 0
            while quality["status"] != "pass" and attempts < max_retries:
                attempts += 1
                calls += 1
                retry_batch = QuestionBatch(
                    batch_id=batch.batch_id, anchor_key=batch.anchor_key,
                    exam_point_ids=batch.exam_point_ids, slots=[slot],
                    forbidden_context=batch.forbidden_context,
                )
                retry_payload = compile_batch_generation_payload(retry_batch, cards).model_copy(
                    update={"teacher_revision_instruction":
                            f"题位 {slot.item_index} 未通过校验：{quality['message']}。请只修复该题并返回。"}
                )
                try:
                    retried = list(gateway.generate_batch(retry_payload))
                except Exception:
                    break
                candidate = next((q for q in retried if q.get("item_index") == slot.item_index), None)
                if candidate is None:
                    continue
                question = _stamp_question(dict(candidate), slot)
                quality = _check_question(question, slot)
            question["quality"] = quality
            question["needs_review"] = quality["status"] != "pass"
            produced[slot.item_index] = question

        ordered = [produced[s.item_index] for s in sorted(batch.slots, key=lambda s: s.item_index)]
        return {"questions": ordered, "model_call_count": calls}

    def merge_and_check(state: GenerationState) -> dict:
        questions = sorted(state.get("questions", []), key=lambda q: q.get("item_index", 0))
        slots = [ContractSlot.model_validate(raw) for raw in state["contract"]]
        report = audit_paper_against_contract(slots, questions)
        return {"final_check": report}

    graph = StateGraph(GenerationState)
    graph.add_node("build_batches", build_batches)
    graph.add_node("batch_generate", batch_generate)
    graph.add_node("merge_and_check", merge_and_check)
    graph.add_edge(START, "build_batches")
    graph.add_conditional_edges("build_batches", route_batches, ["batch_generate"])
    graph.add_edge("batch_generate", "merge_and_check")
    graph.add_edge("merge_and_check", END)
    return graph.compile()
