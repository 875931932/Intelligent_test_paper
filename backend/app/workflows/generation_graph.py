"""合同驱动的试卷生成图。

build_batches → Send(batch_generate) 按考点批并行 → merge_and_check → END。
批内一次模型调用同批互见；跨批互斥由合同禁用上下文构造性保证；
单题失败带原因重试 ≤ max_retries；仍失败时从同考点未用原子中换原子重出
一次（仅当替换也失败才标记 needs_review），不阻塞整卷。
"""
from __future__ import annotations

import operator
import re
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.domain.generation.batching import QuestionBatch, split_contract_into_batches
from app.domain.generation.contract import ContractSlot, _normalized, boundaries_overlap
from app.schemas.generation import compile_batch_generation_payload
from app.services.generation_service import audit_paper_against_contract, validate_generated_question


class BatchGateway(Protocol):
    def generate_batch(self, payload) -> list[dict]: ...


class GenerationState(TypedDict, total=False):
    contract: list[dict]
    knowledge_cards: dict[str, dict]
    units: list[dict]
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
    quality = validate_generated_question(question, atom_text=slot.coverage_atom)
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
    # 判断题答案是对原子的真伪判断，不以答案域文本承载：
    # 跳过答案边界命中检查，其质量由布尔校验 + 禁用上下文泄漏检查保障
    if slot.question_type != "true_false" and not _answer_hits_boundary(question, slot.answer_boundary):
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


def _pick_replacement_atom(
    slot: ContractSlot,
    cards: dict,
    point_card_ids: list[str],
    occupied_atom_keys: set[str],
    occupied_boundaries: list[str],
) -> tuple[str, str, str] | None:
    """为失败题位找同考点替换原子：未被占用且答案域与其他题位互斥。

    候选限定同考点卡片（units 缺失时退化为原卡）；返回 (card_id, 原子,
    答案边界) 或 None。occupied_* 只含其他题位的占用（本题位原原子
    已随失败废弃，不阻塞替换）。
    """

    for card_id in point_card_ids:
        card = cards.get(card_id)
        if not card:
            continue
        boundary = str(card.get("answer_boundary") or card.get("answer_proposition") or "")
        for raw in card.get("assessable_content", []):
            text = str(raw or "").strip()
            key = _normalized(text)
            if not text or not key or key in occupied_atom_keys:
                continue
            if key == _normalized(slot.coverage_atom):
                continue  # 原子刚失败，换同一个无意义
            if any(b and boundaries_overlap(boundary, b) for b in occupied_boundaries):
                continue
            return card_id, text, boundary
    return None


def build_generation_graph(gateway: BatchGateway, *, max_retries: int = 2):
    def build_batches(state: GenerationState) -> dict:
        slots = [ContractSlot.model_validate(raw) for raw in state["contract"]]
        batches = split_contract_into_batches(slots)
        return {"batches": [batch.model_dump(mode="json") for batch in batches]}

    def route_batches(state: GenerationState) -> list[Send]:
        sends = [Send("batch_generate", {
            "batch": batch,
            "knowledge_cards": state.get("knowledge_cards", {}),
            "units": state.get("units", []),
        }) for batch in state.get("batches", [])]
        return sends or [Send("batch_generate", {"batch": None, "knowledge_cards": {}})]

    def batch_generate(payload: dict) -> dict:
        if payload.get("batch") is None:
            return {"questions": [], "model_call_count": 0}
        batch = QuestionBatch.model_validate(payload["batch"])
        cards = payload.get("knowledge_cards", {})
        point_cards: dict[str, list[str]] = {}
        for unit in payload.get("units") or []:
            if isinstance(unit, dict) and unit.get("exam_point_id"):
                point_cards.setdefault(unit["exam_point_id"], []).extend(
                    unit.get("card_ids") or []
                )
        calls = 0
        produced: dict[int, dict] = {}
        slot_keys = {s.item_index: _normalized(s.coverage_atom) for s in batch.slots}
        slot_bounds = {s.item_index: s.answer_boundary for s in batch.slots}

        try:
            raw_questions = list(gateway.generate_batch(compile_batch_generation_payload(batch, cards)))
            calls += 1
        except Exception:
            raw_questions = []
            calls += 1

        for slot in sorted(batch.slots, key=lambda s: s.item_index):
            raw = next((q for q in raw_questions if q.get("item_index") == slot.item_index), None)
            # 批返回缺失该题也走同一条重试链：单题重试 → 换原子兜底，
            # 三道防线全部失守才标 needs_review（批缺失≠不可生成）
            question: dict | None = None
            quality: dict = {"status": "blocker", "message": "批返回缺失该题"}
            if raw is not None:
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
            # 原子重试耗尽仍失败 → 换同考点未用原子重出一次（原子层兜底）：
            # 失败原因常在原子本身（多子句/答案域不可命中），修复措辞无解
            if quality["status"] != "pass":
                occupied_keys = {
                    key for index, key in slot_keys.items() if index != slot.item_index
                }
                occupied_bounds = [
                    bound for index, bound in slot_bounds.items() if index != slot.item_index
                ]
                replacement_found = _pick_replacement_atom(
                    slot, cards,
                    point_cards.get(slot.exam_point_id) or [slot.card_id],
                    occupied_keys, occupied_bounds,
                )
                if replacement_found:
                    rep_card_id, rep_atom, rep_boundary = replacement_found
                    replacement = slot.model_copy(update={
                        "card_id": rep_card_id,
                        "coverage_atom": rep_atom,
                        "answer_boundary": rep_boundary,
                    })
                    calls += 1
                    swap_batch = QuestionBatch(
                        batch_id=batch.batch_id, anchor_key=batch.anchor_key,
                        exam_point_ids=batch.exam_point_ids, slots=[replacement],
                        forbidden_context=batch.forbidden_context,
                    )
                    try:
                        swapped = list(gateway.generate_batch(
                            compile_batch_generation_payload(swap_batch, cards)
                        ))
                    except Exception:
                        swapped = []
                    candidate = next(
                        (q for q in swapped if q.get("item_index") == slot.item_index), None
                    )
                    if candidate is not None:
                        swapped_question = _stamp_question(dict(candidate), replacement)
                        swapped_quality = _check_question(swapped_question, replacement)
                        if swapped_quality["status"] == "pass":
                            question, quality, slot = (
                                swapped_question, swapped_quality, replacement
                            )
                            slot_keys[slot.item_index] = _normalized(rep_atom)
                            slot_bounds[slot.item_index] = rep_boundary
            if question is None:
                # 批缺失且重试/换原子均未取回 → 保留合同槽位标 needs_review
                produced[slot.item_index] = _missing_question(slot, quality["message"])
                continue
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
