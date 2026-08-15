from __future__ import annotations

from app.domain.blueprint.models import (
    ASSESSMENT_MODES,
    BlueprintPlan,
    BlueprintRequest,
    PlanItem,
)


class BlueprintValidationError(Exception):
    pass


def _half_point(value: float) -> bool:
    return abs(value * 2 - round(value * 2)) < 0.001


def _largest_remainder(total: int, weights: dict[str, float]) -> dict[str, int]:
    raw = {key: total * value / 100 for key, value in weights.items()}
    result = {key: int(value) for key, value in raw.items()}
    for key in sorted(raw, key=lambda item: raw[item] - result[item], reverse=True)[: total - sum(result.values())]:
        result[key] += 1
    return result


_DIFFICULTY_ORDER = ("low", "medium", "high")

_DEFAULT_MODE_DISTRIBUTIONS = {
    "single_choice": {"theory_recall": 50, "conceptual": 50},
    "true_false": {"theory_recall": 50, "conceptual": 50},
    "fill_blank": {"theory_recall": 80, "conceptual": 20},
    "short_answer": {"conceptual": 40, "application": 30, "problem_solving": 30},
    "comprehensive": {"application": 30, "problem_solving": 70},
}


def _difficulty_distribution(rule: dict) -> dict[str, float]:
    raw = rule.get("difficulty_distribution")
    if raw is None:
        return {"low": 0.0, "medium": 100.0, "high": 0.0}
    if not isinstance(raw, dict) or set(raw) - set(_DIFFICULTY_ORDER):
        raise BlueprintValidationError("difficulty distribution must contain only low, medium and high")
    distribution = {key: float(raw.get(key, 0)) for key in _DIFFICULTY_ORDER}
    if any(value < 0 for value in distribution.values()) or abs(sum(distribution.values()) - 100) > 0.01:
        raise BlueprintValidationError("difficulty distribution must total 100")
    return distribution


def _assessment_mode_distribution(question_type: str, rule: dict) -> dict[str, float]:
    raw = rule.get("assessment_mode_distribution")
    if raw is None:
        raw = _DEFAULT_MODE_DISTRIBUTIONS.get(
            question_type,
            {"theory_recall": 50, "conceptual": 50},
        )
    if not isinstance(raw, dict) or set(raw) - set(ASSESSMENT_MODES):
        raise BlueprintValidationError(
            "assessment mode distribution must contain only supported modes"
        )
    try:
        distribution = {mode: float(raw.get(mode, 0)) for mode in ASSESSMENT_MODES}
    except (TypeError, ValueError) as exc:
        raise BlueprintValidationError("assessment mode distribution must be numeric") from exc
    if any(value < 0 for value in distribution.values()) or abs(sum(distribution.values()) - 100) > 0.01:
        raise BlueprintValidationError("assessment mode distribution must total 100")
    return distribution


def allocate_plan_items(request: BlueprintRequest) -> BlueprintPlan:
    if abs(sum(request.chapter_weights.values()) - 100) > 0.01:
        raise BlueprintValidationError("chapter weights must total 100")
    if set(request.chapter_weights) != {unit.anchor_key for unit in request.units}:
        raise BlueprintValidationError("chapter weights must cover every unit anchor")
    total_count = 0
    total_score = 0.0
    difficulty_counts: dict[str, dict[str, int]] = {}
    assessment_mode_counts: dict[str, dict[str, int]] = {}
    assessment_mode_distributions: dict[str, dict[str, float]] = {}
    for question_type, rule in request.type_rules.items():
        count = int(rule.get("count", 0))
        score = float(rule.get("score", 0))
        if count <= 0 or not _half_point(score):
            raise BlueprintValidationError("each question score must use 0.5 point increments")
        distribution = _difficulty_distribution(rule)
        mode_distribution = _assessment_mode_distribution(question_type, rule)
        difficulty_counts[question_type] = _largest_remainder(count, distribution)
        assessment_mode_distributions[question_type] = mode_distribution
        assessment_mode_counts[question_type] = _largest_remainder(count, mode_distribution)
        total_count += count
        total_score += count * score
    if abs(total_score - request.total_score) > 0.01:
        raise BlueprintValidationError("question scores must equal total score")
    by_anchor = {anchor: [unit for unit in request.units if unit.anchor_key == anchor] for anchor in request.chapter_weights}
    if any(not units or any(not unit.card_ids for unit in units) for units in by_anchor.values()):
        raise BlueprintValidationError("every plan item requires a knowledge card")
    slots = []
    for type_order, (question_type, rule) in enumerate(request.type_rules.items()):
        type_index = 0
        for difficulty in _DIFFICULTY_ORDER:
            for _ in range(difficulty_counts[question_type][difficulty]):
                slots.append({"question_type": question_type, "score": float(rule["score"]), "difficulty": difficulty, "type_order": type_order, "type_index": type_index})
                type_index += 1
    mode_index_by_type = {question_type: 0 for question_type in request.type_rules}
    mode_sequences = {
        question_type: [
            mode
            for mode in ASSESSMENT_MODES
            for _ in range(assessment_mode_counts[question_type][mode])
        ]
        for question_type in request.type_rules
    }
    for slot in slots:
        question_type = slot["question_type"]
        mode_index = mode_index_by_type[question_type]
        slot["assessment_mode"] = mode_sequences[question_type][mode_index]
        mode_index_by_type[question_type] += 1
    remaining = _largest_remainder(round(request.total_score * 2), request.chapter_weights)
    assigned = []
    for slot in sorted(slots, key=lambda item: (-round(item["score"] * 2), item["type_order"], item["type_index"])):
        slot_units = round(slot["score"] * 2)
        eligible = [anchor for anchor, units in remaining.items() if units >= slot_units]
        if not eligible:
            raise BlueprintValidationError("selected question scores cannot satisfy chapter weights in 0.5 point increments")
        anchor = max(eligible, key=lambda key: remaining[key])
        remaining[anchor] -= slot_units
        assigned.append((slot, anchor))
    if any(remaining.values()):
        raise BlueprintValidationError("selected question scores cannot satisfy chapter weights in 0.5 point increments")

    assignment_by_slot = {(slot["type_order"], slot["type_index"]): anchor for slot, anchor in assigned}
    difficulty_by_slot = {(slot["type_order"], slot["type_index"]): slot["difficulty"] for slot, _ in assigned}
    mode_by_slot = {(slot["type_order"], slot["type_index"]): slot["assessment_mode"] for slot, _ in assigned}
    items: list[PlanItem] = []
    index = 1
    anchor_cursors = {(anchor, question_type): 0 for anchor in by_anchor for question_type in request.type_rules}
    for type_order, (question_type, rule) in enumerate(request.type_rules.items()):
        for type_index in range(int(rule["count"])):
            anchor = assignment_by_slot[(type_order, type_index)]
            candidates = [
                (unit, card_id)
                for unit in by_anchor[anchor]
                for card_id in unit.card_ids
                if not request.card_question_types.get(card_id) or question_type in request.card_question_types[card_id]
                if mode_by_slot[(type_order, type_index)] in unit.allowed_assessment_modes
                if mode_by_slot[(type_order, type_index)] != "practical_operation"
                or unit.operational_detail_policy == "directly_assessable"
            ]
            if not candidates:
                mode = mode_by_slot[(type_order, type_index)]
                raise BlueprintValidationError(
                    f"question type {question_type}, assessment mode {mode}, chapter {anchor} "
                    "has no eligible knowledge card"
                )
            cursor_key = (anchor, question_type)
            cursor = anchor_cursors[cursor_key]
            unit, card_id = candidates[cursor % len(candidates)]
            items.append(
                PlanItem(
                    item_index=index,
                    question_type=question_type,
                    score=float(rule["score"]),
                    anchor_key=anchor,
                    exam_point_id=unit.exam_point_id or unit.unit_id,
                    unit_id=unit.unit_id,
                    card_id=card_id,
                    difficulty=difficulty_by_slot[(type_order, type_index)],
                    assessment_mode=mode_by_slot[(type_order, type_index)],
                )
            )
            anchor_cursors[cursor_key] += 1
            index += 1
    return BlueprintPlan(
        total_score=request.total_score,
        items=items,
        type_counts={key: int(value["count"]) for key, value in request.type_rules.items()},
        difficulty_counts=difficulty_counts,
        anchor_counts={anchor: sum(item.anchor_key == anchor for item in items) for anchor in request.chapter_weights},
        assessment_mode_counts=assessment_mode_counts,
    )
