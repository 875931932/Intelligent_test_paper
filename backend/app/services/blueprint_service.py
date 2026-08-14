from __future__ import annotations

from app.domain.blueprint.models import BlueprintPlan, BlueprintRequest, PlanItem


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


def allocate_plan_items(request: BlueprintRequest) -> BlueprintPlan:
    if abs(sum(request.chapter_weights.values()) - 100) > 0.01:
        raise BlueprintValidationError("chapter weights must total 100")
    if set(request.chapter_weights) != {unit.anchor_key for unit in request.units}:
        raise BlueprintValidationError("chapter weights must cover every unit anchor")
    total_count = 0
    total_score = 0.0
    for question_type, rule in request.type_rules.items():
        count = int(rule.get("count", 0))
        score = float(rule.get("score", 0))
        if count <= 0 or not _half_point(score):
            raise BlueprintValidationError("each question score must use 0.5 point increments")
        total_count += count
        total_score += count * score
    if abs(total_score - request.total_score) > 0.01:
        raise BlueprintValidationError("question scores must equal total score")
    by_anchor = {anchor: [unit for unit in request.units if unit.anchor_key == anchor] for anchor in request.chapter_weights}
    if any(not units or any(not unit.card_ids for unit in units) for units in by_anchor.values()):
        raise BlueprintValidationError("every plan item requires a knowledge card")
    items: list[PlanItem] = []
    index = 1
    anchor_cursors = {anchor: 0 for anchor in by_anchor}
    for question_type, rule in request.type_rules.items():
        allocations = _largest_remainder(int(rule["count"]), request.chapter_weights)
        for anchor, amount in allocations.items():
            units = by_anchor[anchor]
            for offset in range(amount):
                unit = units[(anchor_cursors[anchor] + offset) % len(units)]
                card_id = unit.card_ids[(anchor_cursors[anchor] + offset) % len(unit.card_ids)]
                items.append(PlanItem(item_index=index, question_type=question_type, score=float(rule["score"]), anchor_key=anchor, unit_id=unit.unit_id, card_id=card_id))
                index += 1
            anchor_cursors[anchor] += amount
    return BlueprintPlan(total_score=request.total_score, items=items, type_counts={key: int(value["count"]) for key, value in request.type_rules.items()}, anchor_counts={anchor: sum(item.anchor_key == anchor for item in items) for anchor in request.chapter_weights})
