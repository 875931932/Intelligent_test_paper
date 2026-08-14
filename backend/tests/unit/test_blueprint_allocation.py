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
    assert plan.anchor_counts["rag"] == 15
    assert plan.anchor_counts["agent"] == 10
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
