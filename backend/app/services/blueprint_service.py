from __future__ import annotations

import math
from typing import Any

from app.domain.blueprint.models import (
    ASSESSMENT_MODES,
    BlueprintPlan,
    BlueprintRequest,
    PlanItem,
    UnitCoverage,
)


class BlueprintValidationError(Exception):
    pass


def _half_point(value: float) -> bool:
    return math.isfinite(value) and abs(value * 2 - round(value * 2)) < 0.001


def _largest_remainder(total: int, weights: dict[str, float]) -> dict[str, int]:
    if total < 0 or not weights:
        raise BlueprintValidationError("allocation total and weights must be valid")
    values = {key: float(value) for key, value in weights.items()}
    if any(not math.isfinite(value) or value < 0 for value in values.values()):
        raise BlueprintValidationError("allocation weights must be finite and non-negative")
    weight_total = sum(values.values())
    if weight_total <= 0:
        raise BlueprintValidationError("allocation weights must have a positive total")
    raw = {key: total * value / weight_total for key, value in values.items()}
    result = {key: math.floor(value) for key, value in raw.items()}
    residual = total - sum(result.values())
    order = sorted(raw, key=lambda key: (-(raw[key] - result[key]), str(key)))
    for key in order[:residual]:
        result[key] += 1
    if sum(result.values()) != total:
        raise BlueprintValidationError("largest-remainder allocation did not close")
    return result


_DIFFICULTY_ORDER = ("low", "medium", "high")

_DEFAULT_MODE_DISTRIBUTIONS = {
    "single_choice": {"theory_recall": 50, "conceptual": 50},
    "true_false": {"theory_recall": 50, "conceptual": 50},
    "fill_blank": {"theory_recall": 80, "conceptual": 20},
    "short_answer": {
        "conceptual": 40,
        "application": 30,
        "problem_solving": 30,
    },
    "comprehensive": {"application": 30, "problem_solving": 70},
}


def _validated_distribution(
    raw: object,
    *,
    keys: tuple[str, ...],
    label: str,
) -> dict[str, float]:
    if not isinstance(raw, dict) or set(raw) - set(keys):
        raise BlueprintValidationError(f"{label} must contain only supported values")
    try:
        distribution = {key: float(raw.get(key, 0)) for key in keys}
    except (TypeError, ValueError) as exc:
        raise BlueprintValidationError(f"{label} must be numeric") from exc
    if any(not math.isfinite(value) for value in distribution.values()):
        raise BlueprintValidationError(f"{label} values must be finite")
    if any(value < 0 for value in distribution.values()) or abs(sum(distribution.values()) - 100) > 0.01:
        raise BlueprintValidationError(f"{label} must total 100")
    return distribution


def _difficulty_distribution(rule: dict) -> dict[str, float]:
    raw = rule.get("difficulty_distribution")
    if raw is None:
        return {"low": 0.0, "medium": 100.0, "high": 0.0}
    return _validated_distribution(
        raw,
        keys=_DIFFICULTY_ORDER,
        label="difficulty distribution",
    )


def _assessment_mode_distribution(
    question_type: str,
    rule: dict,
) -> dict[str, float]:
    raw = rule.get("assessment_mode_distribution")
    if raw is None:
        raw = _DEFAULT_MODE_DISTRIBUTIONS.get(
            question_type,
            {"theory_recall": 50, "conceptual": 50},
        )
    return _validated_distribution(
        raw,
        keys=ASSESSMENT_MODES,
        label="assessment mode distribution",
    )


def _eligible_cards(
    request: BlueprintRequest,
    unit: UnitCoverage,
    question_type: str,
) -> list[str]:
    return sorted(
        card_id
        for card_id in unit.card_ids
        if not request.card_question_types.get(card_id)
        or question_type in request.card_question_types[card_id]
    )


def _eligible_units(
    request: BlueprintRequest,
    by_anchor: dict[str, list[UnitCoverage]],
    *,
    anchor: str,
    question_type: str,
    assessment_mode: str,
) -> list[UnitCoverage]:
    return [
        unit
        for unit in by_anchor[anchor]
        if assessment_mode in unit.allowed_assessment_modes
        and (
            assessment_mode != "practical_operation"
            or unit.operational_detail_policy == "directly_assessable"
        )
        and _eligible_cards(request, unit, question_type)
    ]


def _assign_slots_to_anchors(
    request: BlueprintRequest,
    slots: list[dict[str, Any]],
    by_anchor: dict[str, list[UnitCoverage]],
    remaining: dict[str, int],
) -> dict[tuple[int, int], str]:
    anchor_order = tuple(sorted(remaining))
    eligible_by_slot: dict[tuple[int, int], tuple[str, ...]] = {}
    for slot in slots:
        key = (slot["type_order"], slot["type_index"])
        eligible_by_slot[key] = tuple(
            anchor
            for anchor in anchor_order
            if _eligible_units(
                request,
                by_anchor,
                anchor=anchor,
                question_type=slot["question_type"],
                assessment_mode=slot["assessment_mode"],
            )
        )
        if not eligible_by_slot[key]:
            raise BlueprintValidationError(
                f"question type {slot['question_type']}, assessment mode "
                f"{slot['assessment_mode']} has no eligible chapter"
            )

    ordered = sorted(
        slots,
        key=lambda slot: (
            len(eligible_by_slot[(slot["type_order"], slot["type_index"])]),
            -round(slot["score"] * 2),
            slot["type_order"],
            slot["type_index"],
        ),
    )
    initial = tuple(remaining[anchor] for anchor in anchor_order)
    failed_states: set[tuple[int, tuple[int, ...]]] = set()

    def search(
        position: int,
        capacities: tuple[int, ...],
        assignment: dict[tuple[int, int], str],
    ) -> dict[tuple[int, int], str] | None:
        state_key = (position, capacities)
        if state_key in failed_states:
            return None
        if position == len(ordered):
            return dict(assignment) if not any(capacities) else None
        slot = ordered[position]
        slot_key = (slot["type_order"], slot["type_index"])
        slot_units = round(slot["score"] * 2)
        candidates = [
            anchor
            for anchor in eligible_by_slot[slot_key]
            if capacities[anchor_order.index(anchor)] >= slot_units
        ]
        candidates.sort(
            key=lambda anchor: (
                -capacities[anchor_order.index(anchor)],
                anchor,
            )
        )
        for anchor in candidates:
            anchor_index = anchor_order.index(anchor)
            revised = list(capacities)
            revised[anchor_index] -= slot_units
            revised_tuple = tuple(revised)
            if not _future_capacity_is_possible(
                ordered,
                position + 1,
                revised_tuple,
                anchor_order,
                eligible_by_slot,
            ):
                continue
            assignment[slot_key] = anchor
            resolved = search(position + 1, revised_tuple, assignment)
            if resolved is not None:
                return resolved
            assignment.pop(slot_key, None)
        failed_states.add(state_key)
        return None

    resolved = search(0, initial, {})
    if resolved is None:
        raise BlueprintValidationError(
            "selected question scores, assessment modes and chapter weights cannot be jointly satisfied"
        )
    return resolved


def _future_capacity_is_possible(
    ordered_slots: list[dict[str, Any]],
    start: int,
    capacities: tuple[int, ...],
    anchor_order: tuple[str, ...],
    eligible_by_slot: dict[tuple[int, int], tuple[str, ...]],
) -> bool:
    for anchor_index, anchor in enumerate(anchor_order):
        available = sum(
            round(slot["score"] * 2)
            for slot in ordered_slots[start:]
            if anchor
            in eligible_by_slot[(slot["type_order"], slot["type_index"])]
        )
        if capacities[anchor_index] > available:
            return False
    return True


def allocate_plan_items(request: BlueprintRequest) -> BlueprintPlan:
    chapter_values = list(request.chapter_weights.values())
    if (
        any(not math.isfinite(value) or value < 0 for value in chapter_values)
        or abs(sum(chapter_values) - 100) > 0.01
    ):
        raise BlueprintValidationError("chapter weights must total 100 with finite non-negative values")
    if set(request.chapter_weights) != {unit.anchor_key for unit in request.units}:
        raise BlueprintValidationError("chapter weights must cover every unit anchor")

    total_score = 0.0
    difficulty_counts: dict[str, dict[str, int]] = {}
    assessment_mode_counts: dict[str, dict[str, int]] = {}
    for question_type, rule in request.type_rules.items():
        try:
            raw_count = float(rule.get("count", 0))
            score = float(rule.get("score", 0))
        except (TypeError, ValueError) as exc:
            raise BlueprintValidationError("question count and score must be numeric") from exc
        if (
            not math.isfinite(raw_count)
            or not raw_count.is_integer()
            or raw_count <= 0
            or not _half_point(score)
        ):
            raise BlueprintValidationError(
                "question count must be positive and each score must use 0.5 point increments"
            )
        count = int(raw_count)
        difficulty_counts[question_type] = _largest_remainder(
            count,
            _difficulty_distribution(rule),
        )
        assessment_mode_counts[question_type] = _largest_remainder(
            count,
            _assessment_mode_distribution(question_type, rule),
        )
        total_score += count * score
    if abs(total_score - request.total_score) > 0.01:
        raise BlueprintValidationError("question scores must equal total score")

    by_anchor = {
        anchor: sorted(
            (unit for unit in request.units if unit.anchor_key == anchor),
            key=lambda unit: (unit.exam_point_id or unit.unit_id, unit.unit_id),
        )
        for anchor in request.chapter_weights
    }
    if any(not units or any(not unit.card_ids for unit in units) for units in by_anchor.values()):
        raise BlueprintValidationError("every plan item requires a knowledge card")

    slots: list[dict[str, Any]] = []
    for type_order, (question_type, rule) in enumerate(request.type_rules.items()):
        count = int(float(rule["count"]))
        difficulties = [
            difficulty
            for difficulty in _DIFFICULTY_ORDER
            for _ in range(difficulty_counts[question_type][difficulty])
        ]
        modes = [
            mode
            for mode in ASSESSMENT_MODES
            for _ in range(assessment_mode_counts[question_type][mode])
        ]
        for type_index, (difficulty, assessment_mode) in enumerate(
            zip(difficulties, modes, strict=True)
        ):
            slots.append(
                {
                    "question_type": question_type,
                    "score": float(rule["score"]),
                    "difficulty": difficulty,
                    "assessment_mode": assessment_mode,
                    "type_order": type_order,
                    "type_index": type_index,
                }
            )
        if len(difficulties) != count or len(modes) != count:
            raise BlueprintValidationError("question quota allocation did not close")

    remaining = _largest_remainder(
        round(request.total_score * 2),
        request.chapter_weights,
    )
    assignment_by_slot = _assign_slots_to_anchors(
        request,
        slots,
        by_anchor,
        remaining,
    )
    slot_by_key = {
        (slot["type_order"], slot["type_index"]): slot for slot in slots
    }

    point_counts: dict[tuple[str, str, str], int] = {}
    unit_cursors: dict[tuple[str, str, str, str], int] = {}
    card_cursors: dict[tuple[str, str, str, str], int] = {}
    items: list[PlanItem] = []
    index = 1
    for type_order, (question_type, rule) in enumerate(request.type_rules.items()):
        for type_index in range(int(float(rule["count"]))):
            slot_key = (type_order, type_index)
            slot = slot_by_key[slot_key]
            anchor = assignment_by_slot[slot_key]
            mode = slot["assessment_mode"]
            units = _eligible_units(
                request,
                by_anchor,
                anchor=anchor,
                question_type=question_type,
                assessment_mode=mode,
            )
            by_point: dict[str, list[UnitCoverage]] = {}
            for unit in units:
                by_point.setdefault(unit.exam_point_id or unit.unit_id, []).append(unit)
            point_ids = sorted(by_point)
            exam_point_id = min(
                point_ids,
                key=lambda point_id: (
                    point_counts.get((anchor, question_type, point_id), 0),
                    point_id,
                ),
            )
            point_count_key = (anchor, question_type, exam_point_id)
            point_counts[point_count_key] = point_counts.get(point_count_key, 0) + 1

            point_units = sorted(by_point[exam_point_id], key=lambda unit: unit.unit_id)
            unit_cursor_key = (anchor, question_type, mode, exam_point_id)
            unit_cursor = unit_cursors.get(unit_cursor_key, 0)
            unit = point_units[unit_cursor % len(point_units)]
            unit_cursors[unit_cursor_key] = unit_cursor + 1

            cards = _eligible_cards(request, unit, question_type)
            card_cursor_key = (anchor, question_type, mode, unit.unit_id)
            card_cursor = card_cursors.get(card_cursor_key, 0)
            card_id = cards[card_cursor % len(cards)]
            card_cursors[card_cursor_key] = card_cursor + 1

            items.append(
                PlanItem(
                    item_index=index,
                    question_type=question_type,
                    score=float(rule["score"]),
                    anchor_key=anchor,
                    exam_point_id=exam_point_id,
                    unit_id=unit.unit_id,
                    card_id=card_id,
                    difficulty=slot["difficulty"],
                    assessment_mode=mode,
                )
            )
            index += 1

    return BlueprintPlan(
        total_score=request.total_score,
        items=items,
        type_counts={
            key: int(float(value["count"]))
            for key, value in request.type_rules.items()
        },
        difficulty_counts=difficulty_counts,
        anchor_counts={
            anchor: sum(item.anchor_key == anchor for item in items)
            for anchor in request.chapter_weights
        },
        assessment_mode_counts=assessment_mode_counts,
    )
