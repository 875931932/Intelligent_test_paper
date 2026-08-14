from __future__ import annotations

import pytest

from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.services.blueprint_service import BlueprintValidationError, allocate_plan_items


def _request():
    return BlueprintRequest(
        total_score=40,
        type_rules={"single_choice": {"count": 15, "score": 2}, "true_false": {"count": 10, "score": 1}},
        chapter_weights={"rag": 60, "agent": 40},
        units=[
            UnitCoverage(unit_id="u-rag-1", anchor_key="rag", card_ids=["c1", "c2"]),
            UnitCoverage(unit_id="u-agent-1", anchor_key="agent", card_ids=["c3"]),
        ],
    )


def test_blueprint_allocates_type_quota_and_chapter_weight_without_one_item_per_knowledge_point():
    plan = allocate_plan_items(_request())

    assert len(plan.items) == 25
    assert sum(item.score for item in plan.items) == 40
    assert plan.type_counts == {"single_choice": 15, "true_false": 10}
    assert sum(item.score for item in plan.items if item.anchor_key == "rag") == 24
    assert sum(item.score for item in plan.items if item.anchor_key == "agent") == 16
    assert len({item.unit_id for item in plan.items}) == 2


def test_blueprint_rejects_non_half_point_scores_and_weight_mismatch():
    request = _request()
    request.type_rules["single_choice"]["score"] = 1.1
    with pytest.raises(BlueprintValidationError, match="0.5"):
        allocate_plan_items(request)

    request = _request()
    request.chapter_weights["agent"] = 30
    with pytest.raises(BlueprintValidationError, match="100"):
        allocate_plan_items(request)


def test_blueprint_requires_every_plan_item_to_have_a_knowledge_card():
    request = _request()
    request.units[1].card_ids = []
    with pytest.raises(BlueprintValidationError, match="knowledge card"):
        allocate_plan_items(request)


def test_blueprint_matches_weighted_scores_across_mixed_question_values():
    anchors = {"ch1": 5, "ch2": 25, "ch3": 35, "ch4": 5, "ch5": 10, "ch6": 15, "ch7": 5}
    request = BlueprintRequest(
        total_score=100,
        type_rules={
            "single_choice": {"count": 10, "score": 2},
            "true_false": {"count": 10, "score": 2},
            "fill_blank": {"count": 10, "score": 1},
            "short_answer": {"count": 4, "score": 5},
            "comprehensive": {"count": 3, "score": 10},
        },
        chapter_weights=anchors,
        units=[UnitCoverage(unit_id=f"unit-{key}", anchor_key=key, card_ids=[f"card-{key}"]) for key in anchors],
    )
    plan = allocate_plan_items(request)
    scores = {anchor: sum(item.score for item in plan.items if item.anchor_key == anchor) for anchor in anchors}
    assert scores == {anchor: float(weight) for anchor, weight in anchors.items()}


def test_blueprint_selects_only_cards_allowed_for_question_type():
    request = BlueprintRequest(
        total_score=4,
        type_rules={"single_choice": {"count": 1, "score": 2}, "true_false": {"count": 1, "score": 2}},
        chapter_weights={"chapter": 100},
        units=[UnitCoverage(unit_id="unit", anchor_key="chapter", card_ids=["choice-card", "judge-card"])],
        card_question_types={"choice-card": ["single_choice"], "judge-card": ["true_false"]},
    )
    plan = allocate_plan_items(request)
    assert [item.card_id for item in plan.items] == ["choice-card", "judge-card"]


def test_blueprint_allocates_difficulty_counts_per_question_type_and_orders_easy_to_hard():
    request = BlueprintRequest(
        total_score=20,
        type_rules={
            "single_choice": {"count": 10, "score": 2, "difficulty_distribution": {"low": 50, "medium": 30, "high": 20}},
        },
        chapter_weights={"chapter": 100},
        units=[UnitCoverage(unit_id="unit", anchor_key="chapter", card_ids=["c1"])],
    )

    plan = allocate_plan_items(request)

    assert plan.difficulty_counts == {"single_choice": {"low": 5, "medium": 3, "high": 2}}
    assert [item.difficulty for item in plan.items] == ["low"] * 5 + ["medium"] * 3 + ["high"] * 2


def test_blueprint_rejects_difficulty_distribution_not_totaling_100():
    request = _request()
    request.type_rules["single_choice"]["difficulty_distribution"] = {"low": 50, "medium": 20, "high": 20}

    with pytest.raises(BlueprintValidationError, match="difficulty distribution"):
        allocate_plan_items(request)


def test_blueprint_defaults_legacy_type_rule_to_all_medium_without_global_difficulty():
    plan = allocate_plan_items(_request())

    assert all(item.difficulty == "medium" for item in plan.items)
    assert plan.difficulty_counts["single_choice"] == {"low": 0, "medium": 15, "high": 0}
