"""合同驱动的试卷生成图。

build_batches → Send(batch_generate) 按考点批并行 → merge_and_check → END。
批内一次模型调用同批互见；跨批互斥由合同禁用上下文构造性保证；
单题失败带原因重试 ≤ max_retries；仍失败时从同考点未用原子中换原子重出
一次（仅当替换也失败才标记 needs_review），不阻塞整卷。
"""
from __future__ import annotations

import logging
import operator
import re
from typing import Annotated, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.config import settings
from app.domain.framework.exam_rules import canonical_question_type
from app.domain.generation.batching import QuestionBatch, split_contract_into_batches
from app.domain.generation.contract import ContractSlot, _normalized, boundaries_overlap
from app.schemas.generation import compile_batch_generation_payload
from app.services.generation_service import (
    answer_option_keys,
    audit_paper_against_contract,
    validate_generated_question,
)

logger = logging.getLogger("generation.graph")


def _error_digest(exc: BaseException) -> str:
    """日志用错误摘要：模型网关异常只取脱敏 error_code（其原始 message
    可能夹带模型输出），其余异常取类型名+消息。"""
    code = getattr(exc, "error_code", None)
    if code:
        return f"{type(exc).__name__}({code})"
    return f"{type(exc).__name__}: {exc}"


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
    或（选择题）与某一选项一致——选项集即答案域的操作化。

    选择题答案既可能是选项字母（'B'/'ABD'）也可能是选项原文，两种形态都要
    认：先用 answer_option_keys 把字母映射回选项原文再比对，否则模型按字母
    作答时会被误判"答案未命中答案域"，触发无谓重试甚至丢题。
    """
    if not boundary:
        return True
    options = [str(o) for o in (question.get("options") or [])]
    answer = question.get("answer")
    candidates = [str(answer if answer is not None else "")]
    if options:
        keys = answer_option_keys(answer, options)
        if keys:
            candidates.extend(sorted(keys))  # 字母本身
            candidates.extend(
                option for index, option in enumerate(options)
                if chr(65 + index) in keys
            )  # 字母对应的选项原文
    for candidate in candidates:
        left = _normalized(candidate)
        if not left:
            continue
        right = _normalized(boundary)
        if left in right or right in left:
            return True
        if _longest_common_run(left, right) >= 3:
            return True
    return any(option in candidates for option in options)


def _check_question(question: dict, slot: ContractSlot) -> dict:
    """单题确定性校验：schema + 禁用上下文泄漏 + 答案命中边界。

    禁用上下文的下发面（slot.forbidden_context，随题给模型）与校验面必须一致：
    校验范围覆盖题干、选项、答案与解析——提示词已明令这四处都不得出现禁用内容，
    只查题干+选项会让"答案/解析里照抄禁用原子"成为漏网之门。
    """
    quality = validate_generated_question(question, atom_text=slot.coverage_atom)
    reasons: list[str] = []
    if quality["status"] != "pass":
        reasons.append(quality["message"])
    surface = _compact_text([
        question.get("stem", ""),
        *(question.get("options") or []),
        question.get("answer", ""),
        question.get("explanation", ""),
    ])
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


def _card_allows_question_type(card: dict, question_type: str) -> bool:
    """知识卡声明的允许题型是否覆盖该题位。

    卡片里的 allowed_question_types 是模型标注的中文题型名（"单选题"），
    与蓝图引擎的英文字段名不对应；这里做归一化后再比较，卡片没标注时放行。
    """
    raw = card.get("allowed_question_types")
    if not isinstance(raw, list) or not raw:
        return True
    canonical = {canonical_question_type(entry) for entry in raw}
    canonical.discard(None)
    if not canonical:
        return True
    return question_type in canonical


def _pick_backfill_target(
    slot: ContractSlot,
    cards: dict,
    point_cards: dict[str, list[str]],
    point_anchor: dict[str, str],
    batch_points: set[str],
    occupied_atom_keys: set[str],
    occupied_boundaries: list[str],
    used_points: dict[str, int],
    max_per_point: int,
) -> tuple[str, str, str, str] | None:
    """同章内找富余考点回补：返回 (考点, card_id, 原子, 答案边界) 或 None。

    自愈的最后一道防线。只在**同一章（anchor）**内挑兄弟考点，且：
    - 该考点本批没有自己的题位在跑（不去抢同章其他题位的考点）；
    - 未被回补超过 max_per_point 次（不把富余考点抽干）；
    - 卡片声明的允许题型覆盖该题位题型；
    - 原子未被占用、答案域与在跑题位互斥。
    题型/分值/难度/认知层级保持不变——变的只是"考哪个考点"。
    """
    anchor = point_anchor.get(slot.exam_point_id, "")
    candidates = [
        point for point in sorted(point_cards)
        if point != slot.exam_point_id
        and point not in batch_points
        and (anchor == "" or point_anchor.get(point, "") == anchor)
        and used_points.get(point, 0) < max_per_point
        and point_cards.get(point)
    ]
    # 卡片多的考点通常原子更富余，优先试
    candidates.sort(key=lambda p: len(point_cards.get(p) or []), reverse=True)
    for point in candidates:
        picked = _pick_replacement_atom(slot, cards, point_cards[point], occupied_atom_keys, occupied_boundaries)
        if picked is None:
            continue
        card_id, atom, boundary = picked
        if not _card_allows_question_type(cards.get(card_id) or {}, slot.question_type):
            continue
        return point, card_id, atom, boundary
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
        point_anchor: dict[str, str] = {}
        for unit in payload.get("units") or []:
            if isinstance(unit, dict) and unit.get("exam_point_id"):
                point_cards.setdefault(unit["exam_point_id"], []).extend(
                    unit.get("card_ids") or []
                )
                point_anchor.setdefault(unit["exam_point_id"], str(unit.get("anchor_key") or ""))
        # 本批在跑的考点：回补时不抢这些考点（它们有自己的题位要出）
        batch_points = {s.exam_point_id for s in batch.slots}
        backfill_into: dict[str, int] = {}
        backfill_count = 0
        calls = 0
        produced: dict[int, dict] = {}
        slot_keys = {s.item_index: _normalized(s.coverage_atom) for s in batch.slots}
        slot_bounds = {s.item_index: s.answer_boundary for s in batch.slots}

        try:
            raw_questions = list(gateway.generate_batch(compile_batch_generation_payload(batch, cards)))
            calls += 1
        except Exception as exc:
            # 批调用失败绝不能静默：整批缺失会连锁触发逐题重试，日志是
            # 区分"模型服务不可用"与"模型在吐坏题"的唯一依据。
            logger.warning(
                "批次模型调用失败 batch=%s anchor=%s slots=%d error=%s",
                batch.batch_id, batch.anchor_key, len(batch.slots), _error_digest(exc),
            )
            raw_questions = []
            calls += 1

        for slot in sorted(batch.slots, key=lambda s: s.item_index):
            raw = next((q for q in raw_questions if q.get("item_index") == slot.item_index), None)
            # 批返回缺失该题也走同一条重试链：单题重试 → 换原子兜底 → 同章回补，
            # 全部失守才标 needs_review（批缺失≠不可生成）
            question: dict | None = None
            quality: dict = {"status": "blocker", "message": "批返回缺失该题"}
            # 其他题位已占用的原子与答案域：换原子/回补都要与之互斥
            occupied_keys = {
                key for index, key in slot_keys.items() if index != slot.item_index
            }
            occupied_bounds = [
                bound for index, bound in slot_bounds.items() if index != slot.item_index
            ]
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
                except Exception as exc:
                    logger.warning(
                        "单题重试模型调用失败 batch=%s item_index=%d attempt=%d error=%s",
                        batch.batch_id, slot.item_index, attempts, _error_digest(exc),
                    )
                    break
                candidate = next((q for q in retried if q.get("item_index") == slot.item_index), None)
                if candidate is None:
                    continue
                question = _stamp_question(dict(candidate), slot)
                quality = _check_question(question, slot)
            # 原子重试耗尽仍失败 → 换同考点未用原子重出一次（原子层兜底）：
            # 失败原因常在原子本身（多子句/答案域不可命中），修复措辞无解
            if quality["status"] != "pass":
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
                    except Exception as exc:
                        logger.warning(
                            "换原子模型调用失败 batch=%s item_index=%d "
                            "replacement_atom=%s error=%s",
                            batch.batch_id, slot.item_index, rep_atom,
                            _error_digest(exc),
                        )
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
            # 同考点也救不回来 → 最后一道自愈：同章其他富余考点回补一题。
            # 上限从配置读取（默认每批 1 题、每考点 1 题），只兜底不作常态。
            if (
                quality["status"] != "pass"
                and question is None
                and backfill_count < settings.generation_backfill_max_per_batch
            ):
                target = _pick_backfill_target(
                    slot, cards, point_cards, point_anchor, batch_points,
                    occupied_keys, occupied_bounds, backfill_into,
                    settings.generation_backfill_max_per_point,
                )
                if target is not None:
                    to_point, bf_card_id, bf_atom, bf_boundary = target
                    original_point = slot.exam_point_id
                    backfilled_slot = slot.model_copy(update={
                        "card_id": bf_card_id,
                        "coverage_atom": bf_atom,
                        "answer_boundary": bf_boundary,
                        # 题位实际改考到同章富余考点，溯源必须指向真实来源
                        "exam_point_id": to_point,
                    })
                    calls += 1
                    backfill_batch = QuestionBatch(
                        batch_id=batch.batch_id, anchor_key=batch.anchor_key,
                        exam_point_ids=batch.exam_point_ids, slots=[backfilled_slot],
                        forbidden_context=batch.forbidden_context,
                    )
                    try:
                        bf_raw = list(gateway.generate_batch(
                            compile_batch_generation_payload(backfill_batch, cards)
                        ))
                    except Exception as exc:
                        logger.warning(
                            "回补模型调用失败 batch=%s item_index=%d to_point=%s error=%s",
                            batch.batch_id, slot.item_index, to_point, _error_digest(exc),
                        )
                        bf_raw = []
                    candidate = next(
                        (q for q in bf_raw if q.get("item_index") == slot.item_index), None
                    )
                    if candidate is not None:
                        candidate = _stamp_question(dict(candidate), backfilled_slot)
                        bf_quality = _check_question(candidate, backfilled_slot)
                        if bf_quality["status"] == "pass":
                            question, quality, slot = candidate, bf_quality, backfilled_slot
                            slot_keys[slot.item_index] = _normalized(bf_atom)
                            slot_bounds[slot.item_index] = bf_boundary
                            question["backfilled_from"] = {
                                "from_exam_point_id": original_point,
                                "to_exam_point_id": to_point,
                                "anchor_key": batch.anchor_key,
                            }
                            backfill_into[to_point] = backfill_into.get(to_point, 0) + 1
                            backfill_count += 1
                            logger.info(
                                "题位回补成功 batch=%s item_index=%d %s → 同章考点 %s",
                                batch.batch_id, slot.item_index, original_point, to_point,
                            )
            if question is None:
                # 三道防线 + 同章回补全部失守 → 保留合同槽位标 needs_review
                produced[slot.item_index] = _missing_question(slot, quality["message"])
                continue
            question["quality"] = quality
            question["needs_review"] = quality["status"] != "pass"
            produced[slot.item_index] = question
        ordered = [produced[s.item_index] for s in sorted(batch.slots, key=lambda s: s.item_index)]
        review_count = sum(1 for q in ordered if q.get("needs_review"))
        logger.info(
            "批次完成 batch=%s anchor=%s produced=%d needs_review=%d calls=%d",
            batch.batch_id, batch.anchor_key, len(ordered), review_count, calls,
        )
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
