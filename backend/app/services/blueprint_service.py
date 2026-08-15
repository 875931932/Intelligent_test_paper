from __future__ import annotations

import math
from collections.abc import Iterator
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
    grouped_slots: dict[
        tuple[int, tuple[str, ...]],
        list[dict[str, Any]],
    ] = {}
    for slot in ordered:
        slot_key = (slot["type_order"], slot["type_index"])
        group_key = (
            round(slot["score"] * 2),
            eligible_by_slot[slot_key],
        )
        grouped_slots.setdefault(group_key, []).append(slot)
    groups = [
        {
            "slot_units": group_key[0],
            "anchors": group_key[1],
            "slots": grouped_slots[group_key],
        }
        for group_key in sorted(
            grouped_slots,
            key=lambda key: (len(key[1]), -key[0], key[1]),
        )
    ]

    # When every slot has the same eligible anchor set, chapter identities are
    # interchangeable during search. Solve on sorted residual capacities so
    # permutations of equal states are collapsed instead of being explored
    # independently by the general DP below.
    eligible_sets = {group["anchors"] for group in groups}
    if len(eligible_sets) == 1:
        return _assign_uniform_eligible_groups(
            groups,
            remaining=remaining,
            anchor_order=anchor_order,
        )

    anchor_indexes = {anchor: index for index, anchor in enumerate(anchor_order)}
    suffix_available = [tuple(0 for _ in anchor_order) for _ in range(len(groups) + 1)]
    for group_position in range(len(groups) - 1, -1, -1):
        available = list(suffix_available[group_position + 1])
        group = groups[group_position]
        group_units = group["slot_units"] * len(group["slots"])
        for anchor in group["anchors"]:
            available[anchor_indexes[anchor]] += group_units
        suffix_available[group_position] = tuple(available)

    initial = tuple(remaining[anchor] for anchor in anchor_order)
    states = {initial: 0}
    nodes: list[tuple[int, int, tuple[int, ...]]] = [(-1, -1, ())]
    for group_position, group in enumerate(groups):
        eligible_indexes = tuple(anchor_indexes[anchor] for anchor in group["anchors"])
        future_available = suffix_available[group_position + 1]
        next_states: dict[tuple[int, ...], int] = {}
        for capacities, node_id in sorted(states.items()):
            for allocation in _iter_group_allocations(
                item_count=len(group["slots"]),
                slot_units=group["slot_units"],
                eligible_indexes=eligible_indexes,
                capacities=capacities,
                future_available=future_available,
            ):
                revised = list(capacities)
                for anchor_index, count in zip(
                    eligible_indexes,
                    allocation,
                    strict=True,
                ):
                    revised[anchor_index] -= count * group["slot_units"]
                revised_tuple = tuple(revised)
                if any(
                    capacity > future
                    for capacity, future in zip(
                        revised_tuple,
                        future_available,
                        strict=True,
                    )
                ):
                    continue
                if revised_tuple not in next_states:
                    nodes.append((node_id, group_position, allocation))
                    next_states[revised_tuple] = len(nodes) - 1
        if not next_states:
            states = {}
            break
        states = next_states

    zero = tuple(0 for _ in anchor_order)
    if zero not in states:
        raise BlueprintValidationError(
            "selected question scores, assessment modes and chapter weights cannot be jointly satisfied"
        )

    allocations_by_group: dict[int, tuple[int, ...]] = {}
    node_id = states[zero]
    while node_id:
        previous_node_id, group_position, allocation = nodes[node_id]
        allocations_by_group[group_position] = allocation
        node_id = previous_node_id

    assignment: dict[tuple[int, int], str] = {}
    for group_position, group in enumerate(groups):
        offset = 0
        for anchor, count in zip(
            group["anchors"],
            allocations_by_group[group_position],
            strict=True,
        ):
            for slot in group["slots"][offset : offset + count]:
                assignment[(slot["type_order"], slot["type_index"])] = anchor
            offset += count
    return assignment


def _assign_uniform_eligible_groups(
    groups: list[dict[str, Any]],
    *,
    remaining: dict[str, int],
    anchor_order: tuple[str, ...],
) -> dict[tuple[int, int], str]:
    """Assign groups with one shared eligibility set using canonical states."""

    eligible = groups[0]["anchors"]
    ineligible = [anchor for anchor in anchor_order if anchor not in eligible]
    if any(remaining[anchor] for anchor in ineligible):
        raise BlueprintValidationError(
            "selected question scores and chapter weights cannot be jointly satisfied"
        )

    ordered_groups = sorted(
        groups,
        key=lambda group: (-group["slot_units"], -len(group["slots"]), group["anchors"]),
    )
    active = tuple(sorted(eligible, key=lambda anchor: (remaining[anchor], anchor)))
    initial = tuple(remaining[anchor] for anchor in active)

    if len(ordered_groups) == 1:
        group = ordered_groups[0]
        unit = group["slot_units"]
        if any(capacity % unit for capacity in initial):
            raise BlueprintValidationError(
                "selected question scores and chapter weights cannot be jointly satisfied"
            )
        counts = tuple(capacity // unit for capacity in initial)
        if sum(counts) != len(group["slots"]):
            raise BlueprintValidationError(
                "selected question scores and chapter weights cannot be jointly satisfied"
            )
        allocations = (counts,)
    else:
        suffix_units = [0] * (len(ordered_groups) + 1)
        for position in range(len(ordered_groups) - 1, -1, -1):
            group = ordered_groups[position]
            suffix_units[position] = (
                suffix_units[position + 1]
                + group["slot_units"] * len(group["slots"])
            )

        memo: dict[tuple[int, tuple[int, ...]], tuple[tuple[int, ...], ...] | None] = {}

        def solve(
            position: int,
            capacities: tuple[int, ...],
        ) -> tuple[tuple[int, ...], ...] | None:
            state = (position, capacities)
            if state in memo:
                return memo[state]
            if position == len(ordered_groups):
                result = () if not any(capacities) else None
                memo[state] = result
                return result
            if sum(capacities) != suffix_units[position]:
                memo[state] = None
                return None

            group = ordered_groups[position]
            item_count = len(group["slots"])
            unit = group["slot_units"]
            upper_bounds = tuple(
                min(item_count, capacity // unit) for capacity in capacities
            )
            if sum(upper_bounds) < item_count:
                memo[state] = None
                return None
            future_units = suffix_units[position + 1]

            # Try balanced allocations first. In normal exam blueprints this
            # finds a solution immediately; the bounded enumeration below keeps
            # the path exact for awkward half-point combinations.
            target = item_count / max(1, len(capacities))
            for allocation in _iter_uniform_allocations(
                item_count=item_count,
                unit=unit,
                capacities=capacities,
                target=target,
            ):
                revised = tuple(
                    capacity - count * unit
                    for capacity, count in zip(capacities, allocation, strict=True)
                )
                if any(capacity < 0 or capacity > future_units for capacity in revised):
                    continue
                canonical = tuple(sorted(revised))
                result = solve(position + 1, canonical)
                if result is not None:
                    memo[state] = (allocation,) + result
                    return memo[state]
            memo[state] = None
            return None

        allocations = solve(0, initial)
        if allocations is None:
            raise BlueprintValidationError(
                "selected question scores and chapter weights cannot be jointly satisfied"
            )

    assignment: dict[tuple[int, int], str] = {}
    pairs = [(anchor, remaining[anchor]) for anchor in active]
    for group, allocation in zip(ordered_groups, allocations, strict=True):
        pairs.sort(key=lambda pair: (pair[1], pair[0]))
        offset = 0
        for (anchor, capacity), count in zip(pairs, allocation, strict=True):
            revised = capacity - count * group["slot_units"]
            if revised < 0:
                raise BlueprintValidationError(
                    "selected question scores and chapter weights cannot be jointly satisfied"
                )
            for slot in group["slots"][offset : offset + count]:
                assignment[(slot["type_order"], slot["type_index"])] = anchor
            offset += count
        pairs = [
            (anchor, capacity - count * group["slot_units"])
            for (anchor, capacity), count in zip(pairs, allocation, strict=True)
        ]
    if any(capacity for _, capacity in pairs):
        raise BlueprintValidationError(
            "selected question scores and chapter weights cannot be jointly satisfied"
        )
    return assignment


def _iter_uniform_allocations(
    *,
    item_count: int,
    unit: int,
    capacities: tuple[int, ...],
    target: float,
) -> Iterator[tuple[int, ...]]:
    upper_bounds = tuple(min(item_count, capacity // unit) for capacity in capacities)
    if sum(upper_bounds) < item_count:
        return

    # Generate counts nearest the balanced target first, then cover every
    # bounded composition exactly once if the first candidate is not feasible.
    choices = [
        sorted(range(upper + 1), key=lambda count: (abs(count - target), count))
        for upper in upper_bounds
    ]
    seen: set[tuple[int, ...]] = set()
    stack: list[tuple[int, int, tuple[int, ...]]] = [(0, item_count, ())]
    while stack:
        position, left, prefix = stack.pop()
        if position == len(upper_bounds) - 1:
            if 0 <= left <= upper_bounds[position]:
                result = (*prefix, left)
                if result not in seen:
                    seen.add(result)
                    yield result
            continue
        remaining_upper = sum(upper_bounds[position + 1 :])
        for count in reversed(choices[position]):
            if count <= left and left - count <= remaining_upper:
                stack.append((position + 1, left - count, (*prefix, count)))


def _iter_group_allocations(
    *,
    item_count: int,
    slot_units: int,
    eligible_indexes: tuple[int, ...],
    capacities: tuple[int, ...],
    future_available: tuple[int, ...],
) -> Iterator[tuple[int, ...]]:
    if slot_units == 0:
        yield (item_count, *(0 for _ in eligible_indexes[1:]))
        return

    lower_bounds = []
    upper_bounds = []
    for anchor_index in eligible_indexes:
        required_units = max(0, capacities[anchor_index] - future_available[anchor_index])
        lower_bounds.append((required_units + slot_units - 1) // slot_units)
        upper_bounds.append(min(item_count, capacities[anchor_index] // slot_units))
    if sum(lower_bounds) > item_count or sum(upper_bounds) < item_count:
        return

    suffix_lower = [0] * (len(eligible_indexes) + 1)
    suffix_upper = [0] * (len(eligible_indexes) + 1)
    for position in range(len(eligible_indexes) - 1, -1, -1):
        suffix_lower[position] = suffix_lower[position + 1] + lower_bounds[position]
        suffix_upper[position] = suffix_upper[position + 1] + upper_bounds[position]

    stack: list[tuple[int, int, tuple[int, ...]]] = [(0, item_count, ())]
    while stack:
        position, remaining_items, prefix = stack.pop()
        if position == len(eligible_indexes) - 1:
            if lower_bounds[position] <= remaining_items <= upper_bounds[position]:
                yield (*prefix, remaining_items)
            continue
        minimum = max(
            lower_bounds[position],
            remaining_items - suffix_upper[position + 1],
        )
        maximum = min(
            upper_bounds[position],
            remaining_items - suffix_lower[position + 1],
        )
        for count in range(minimum, maximum + 1):
            stack.append((position + 1, remaining_items - count, (*prefix, count)))


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
