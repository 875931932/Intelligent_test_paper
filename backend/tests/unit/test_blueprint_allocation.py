from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.blueprint.models import BlueprintRequest, PlanItem, UnitCoverage
from app.services.blueprint_service import (
    BlueprintValidationError,
    _largest_remainder,
    allocate_plan_items,
)


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
    assert all(item.exam_point_id == item.unit_id for item in plan.items)


def test_blueprint_mode_distribution_is_not_driven_by_material_card_count():
    request = BlueprintRequest(
        total_score=20,
        type_rules={
            "single_choice": {
                "count": 10,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 40,
                    "conceptual": 50,
                    "application": 10,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[
            UnitCoverage(
                unit_id="theory",
                exam_point_id="ep-theory",
                anchor_key="chapter",
                card_ids=["theory-card"],
                allowed_assessment_modes=["theory_recall", "conceptual", "application"],
                operational_detail_policy="supporting_only",
            ),
            UnitCoverage(
                unit_id="practical",
                exam_point_id="ep-practical",
                anchor_key="chapter",
                card_ids=[f"operation-{index}" for index in range(20)],
                allowed_assessment_modes=["practical_operation"],
                operational_detail_policy="directly_assessable",
            ),
        ],
    )

    plan = allocate_plan_items(request)

    assert plan.assessment_mode_counts["single_choice"] == {
        "theory_recall": 4,
        "conceptual": 5,
        "application": 1,
        "problem_solving": 0,
        "practical_operation": 0,
    }
    assert all(item.assessment_mode != "practical_operation" for item in plan.items)
    assert all(item.unit_id == "theory" for item in plan.items)


def test_blueprint_rejects_invalid_mode_distribution_and_unsupported_practical_mode():
    request = _request()
    request.type_rules["single_choice"]["assessment_mode_distribution"] = {
        "theory_recall": 70,
        "conceptual": 20,
    }
    with pytest.raises(BlueprintValidationError, match="assessment mode distribution"):
        allocate_plan_items(request)

    request = BlueprintRequest(
        total_score=2,
        type_rules={
            "single_choice": {
                "count": 1,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 0,
                    "conceptual": 0,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 100,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[
            UnitCoverage(
                unit_id="unit",
                anchor_key="chapter",
                card_ids=["card"],
                allowed_assessment_modes=["practical_operation"],
                operational_detail_policy="supporting_only",
            )
        ],
    )
    with pytest.raises(BlueprintValidationError, match="practical_operation"):
        allocate_plan_items(request)


def test_blueprint_orders_each_question_type_by_difficulty_then_assessment_mode():
    request = BlueprintRequest(
        total_score=4,
        type_rules={
            "single_choice": {
                "count": 2,
                "score": 2,
                "difficulty_distribution": {"low": 100, "medium": 0, "high": 0},
                "assessment_mode_distribution": {
                    "theory_recall": 50,
                    "conceptual": 50,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[
            UnitCoverage(
                unit_id="unit",
                anchor_key="chapter",
                card_ids=["card"],
            )
        ],
    )

    plan = allocate_plan_items(request)

    assert [(item.difficulty, item.assessment_mode) for item in plan.items] == [
        ("low", "theory_recall"),
        ("low", "conceptual"),
    ]


@pytest.mark.parametrize(
    "chapter_weights",
    [
        {"concept-only": 50, "recall-only": 50},
        {"recall-only": 50, "concept-only": 50},
    ],
)
def test_blueprint_jointly_matches_chapter_quota_and_mode_eligibility(chapter_weights):
    request = BlueprintRequest(
        total_score=4,
        type_rules={
            "single_choice": {
                "count": 2,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 50,
                    "conceptual": 50,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights=chapter_weights,
        units=[
            UnitCoverage(
                unit_id="concept-unit",
                anchor_key="concept-only",
                card_ids=["concept-card"],
                allowed_assessment_modes=["conceptual"],
            ),
            UnitCoverage(
                unit_id="recall-unit",
                anchor_key="recall-only",
                card_ids=["recall-card"],
                allowed_assessment_modes=["theory_recall"],
            ),
        ],
    )

    plan = allocate_plan_items(request)

    assert {(item.anchor_key, item.assessment_mode) for item in plan.items} == {
        ("recall-only", "theory_recall"),
        ("concept-only", "conceptual"),
    }


def test_largest_remainder_normalizes_tolerated_weight_drift_to_exact_total():
    counts = _largest_remainder(
        20_000,
        {
            "theory_recall": 100.009,
            "conceptual": 0,
            "application": 0,
            "problem_solving": 0,
            "practical_operation": 0,
        },
    )

    assert sum(counts.values()) == 20_000
    assert counts["theory_recall"] == 20_000


def test_blueprint_rejects_non_finite_distribution_values():
    request = _request()
    request.type_rules["single_choice"]["assessment_mode_distribution"] = {
        "theory_recall": float("nan"),
        "conceptual": 100,
    }

    with pytest.raises(BlueprintValidationError, match="finite"):
        allocate_plan_items(request)


def test_blueprint_models_reject_unknown_modes_and_operational_policy():
    with pytest.raises(ValidationError):
        UnitCoverage(
            unit_id="unit",
            anchor_key="chapter",
            card_ids=["card"],
            allowed_assessment_modes=["typo"],
        )
    with pytest.raises(ValidationError):
        UnitCoverage(
            unit_id="unit",
            anchor_key="chapter",
            card_ids=["card"],
            operational_detail_policy="anything",
        )
    with pytest.raises(ValidationError):
        PlanItem(
            item_index=1,
            question_type="single_choice",
            score=2,
            anchor_key="chapter",
            unit_id="unit",
            card_id="card",
            assessment_mode="typo",
        )


@pytest.mark.parametrize("reverse_units", [False, True])
def test_blueprint_rotates_exam_points_before_cards(reverse_units):
    units = [
        UnitCoverage(
            unit_id="small",
            exam_point_id="ep-small",
            anchor_key="chapter",
            card_ids=["small-card"],
        ),
        UnitCoverage(
            unit_id="large",
            exam_point_id="ep-large",
            anchor_key="chapter",
            card_ids=[f"large-{index}" for index in range(20)],
        ),
    ]
    if reverse_units:
        units.reverse()
    request = BlueprintRequest(
        total_score=20,
        type_rules={"single_choice": {"count": 10, "score": 2}},
        chapter_weights={"chapter": 100},
        units=units,
    )

    plan = allocate_plan_items(request)
    counts = {
        point: sum(item.exam_point_id == point for item in plan.items)
        for point in ("ep-small", "ep-large")
    }

    assert counts == {"ep-small": 5, "ep-large": 5}


def test_blueprint_allocates_practical_mode_only_to_directly_assessable_unit():
    request = BlueprintRequest(
        total_score=2,
        type_rules={
            "short_answer": {
                "count": 1,
                "score": 2,
                "assessment_mode_distribution": {
                    "theory_recall": 0,
                    "conceptual": 0,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 100,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[
            UnitCoverage(
                unit_id="practical",
                exam_point_id="ep-practical",
                anchor_key="chapter",
                card_ids=["practical-card"],
                allowed_assessment_modes=["practical_operation"],
                operational_detail_policy="directly_assessable",
            )
        ],
    )

    plan = allocate_plan_items(request)

    assert plan.items[0].assessment_mode == "practical_operation"
    assert plan.items[0].exam_point_id == "ep-practical"


def test_blueprint_allocates_large_valid_plan_without_recursion_limit():
    request = BlueprintRequest(
        total_score=500,
        type_rules={
            "single_choice": {
                "count": 1000,
                "score": 0.5,
                "assessment_mode_distribution": {
                    "theory_recall": 100,
                    "conceptual": 0,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
        },
        chapter_weights={"chapter": 100},
        units=[
            UnitCoverage(
                unit_id="unit",
                anchor_key="chapter",
                card_ids=["card"],
                allowed_assessment_modes=["theory_recall"],
            )
        ],
    )

    plan = allocate_plan_items(request)

    assert len(plan.items) == 1000


def test_blueprint_collapses_uniform_eligibility_states_for_mixed_scores():
    request = BlueprintRequest(
        total_score=75,
        type_rules={
            f"type-{index}": {
                "count": 10,
                "score": score,
                "assessment_mode_distribution": {
                    "theory_recall": 100,
                    "conceptual": 0,
                    "application": 0,
                    "problem_solving": 0,
                    "practical_operation": 0,
                },
            }
            for index, score in enumerate((0.5, 1, 1.5, 2, 2.5))
        },
        chapter_weights={f"chapter-{index}": 20 for index in range(5)},
        units=[
            UnitCoverage(
                unit_id=f"unit-{index}",
                anchor_key=f"chapter-{index}",
                card_ids=["card"],
                allowed_assessment_modes=["theory_recall"],
            )
            for index in range(5)
        ],
    )

    plan = allocate_plan_items(request)

    assert plan.anchor_counts == {f"chapter-{index}": 10 for index in range(5)}
    assert len(plan.items) == 50
