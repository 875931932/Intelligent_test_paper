"""合同必须按蓝图产出满分：考点题位超配时回补到同章兄弟考点，而不是静默丢题。

对应线上事故：蓝图把 4 个题位压给只有 1 张知识卡的考点（答案域是卡级标量，
一张卡全卷只能出 1 题），合同静默丢掉 6 个题位，总分 83/100。
"""
from __future__ import annotations

from app.domain.blueprint.models import (
    BlueprintPlan,
    BlueprintRequest,
    PlanItem,
    UnitCoverage,
)
from app.domain.generation.contract import boundaries_overlap
from app.services.contract_service import ContractRequest, allocate_paper_contract


def _units():
    """A1 章：ep-weak 只有 1 张卡（1 个答案域），ep-rich 有 3 张卡（3 个答案域）。"""
    return [
        UnitCoverage(unit_id="u-weak", exam_point_id="ep-weak", anchor_key="A1", card_ids=["c-weak"]),
        UnitCoverage(unit_id="u-rich-1", exam_point_id="ep-rich", anchor_key="A1", card_ids=["c-rich-1"]),
        UnitCoverage(unit_id="u-rich-2", exam_point_id="ep-rich", anchor_key="A1", card_ids=["c-rich-2"]),
        UnitCoverage(unit_id="u-rich-3", exam_point_id="ep-rich", anchor_key="A1", card_ids=["c-rich-3"]),
    ]


def _cards():
    return {
        "c-weak": {
            "is_core": True, "performance_statement": "掌握训练数据配比设计",
            "assessable_content": ["混合训练数据集需按领域配比", "配比失衡会导致能力退化"],
            "preferred_terms": ["配比"], "answer_boundary": "训练数据配比设计",
            "prompt_material": ["配比示例"],
        },
        "c-rich-1": {
            "is_core": True, "performance_statement": "掌握提示词要素",
            "assessable_content": ["有效提示词包含角色设定要素", "提示词可加入背景信息"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词角色与背景要素",
            "prompt_material": ["角色设定示例"],
        },
        "c-rich-2": {
            "is_core": True, "performance_statement": "掌握提示词要素",
            "assessable_content": ["提示词输出格式需要约束条件", "提示词迭代需要评估反馈"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词格式与评估要素",
            "prompt_material": ["格式约束示例"],
        },
        "c-rich-3": {
            "is_core": True, "performance_statement": "掌握提示词要素",
            "assessable_content": ["提示词版本对比需要评测指标", "提示词优化依赖错误分析"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词版本与指标要素",
            "prompt_material": ["评测指标示例"],
        },
    }


def _plan(item_count: int, *, exam_point_id: str, unit_id: str, card_id: str,
          anchor_key: str = "A1", score: float = 2.0,
          question_type: str = "single_choice") -> BlueprintPlan:
    items = [
        PlanItem(
            item_index=index,
            question_type=question_type,
            score=score,
            anchor_key=anchor_key,
            exam_point_id=exam_point_id,
            unit_id=unit_id,
            card_id=card_id,
        )
        for index in range(1, item_count + 1)
    ]
    return BlueprintPlan(
        total_score=item_count * score,
        items=items,
        type_counts={question_type: item_count},
        difficulty_counts={question_type: {"low": 0, "medium": item_count, "high": 0}},
        anchor_counts={anchor_key: item_count},
    )


def _request(plan: BlueprintPlan, units, cards, **overrides) -> ContractRequest:
    anchors = {item.anchor_key for item in plan.items}
    weights = {anchor: 100.0 / len(anchors) for anchor in sorted(anchors)}
    base = dict(
        blueprint=BlueprintRequest(
            total_score=plan.total_score,
            type_rules={"single_choice": {"count": len(plan.items), "score": plan.items[0].score}},
            chapter_weights=weights,
            units=units,
        ),
        knowledge_cards=cards,
        plan=plan,
    )
    base.update(overrides)
    return ContractRequest(**base)


def test_over_assigned_point_is_backfilled_from_sibling_point_for_full_score():
    """已存储蓝图把 4 题压给只有 1 张卡的考点：必须回补到同章富余考点，总分满分。"""
    plan = _plan(4, exam_point_id="ep-weak", unit_id="u-weak", card_id="c-weak")
    contract = allocate_paper_contract(_request(plan, _units(), _cards()))

    assert contract.total_score == 8.0
    assert len(contract.slots) == 4
    assert [s.item_index for s in contract.slots] == [1, 2, 3, 4]
    # 全部仍在 A1 章内回补，章节配额不被跨章挪用
    assert all(s.anchor_key == "A1" for s in contract.slots)
    # 弱考点只出 1 题（1 张卡 = 1 个答案域），其余 3 题落到兄弟考点
    assert sum(1 for s in contract.slots if s.exam_point_id == "ep-weak") == 1
    assert sum(1 for s in contract.slots if s.exam_point_id == "ep-rich") == 3
    assert not contract.conflicts


def test_backfill_keeps_other_chapters_untouched():
    """回补只在缺题的那一章内进行，其他章题位与考点保持不变。"""
    units = _units() + [
        UnitCoverage(unit_id="u2-a", exam_point_id="ep2", anchor_key="A2", card_ids=["c2-a"]),
        UnitCoverage(unit_id="u2-b", exam_point_id="ep2", anchor_key="A2", card_ids=["c2-b"]),
    ]
    cards = _cards() | {
        "c2-a": {
            "is_core": True, "performance_statement": "掌握量化格式",
            "assessable_content": ["QLoRA使用NF4量化格式压缩"],
            "preferred_terms": [], "answer_boundary": "量化格式NF4",
        },
        "c2-b": {
            "is_core": True, "performance_statement": "掌握量化格式",
            "assessable_content": ["量化格式只保留四位精度"],
            "preferred_terms": [], "answer_boundary": "量化精度取舍",
        },
    }
    items = [
        PlanItem(item_index=1, question_type="single_choice", score=2.0, anchor_key="A1",
                 exam_point_id="ep-weak", unit_id="u-weak", card_id="c-weak"),
        PlanItem(item_index=2, question_type="single_choice", score=2.0, anchor_key="A1",
                 exam_point_id="ep-weak", unit_id="u-weak", card_id="c-weak"),
        PlanItem(item_index=3, question_type="single_choice", score=2.0, anchor_key="A2",
                 exam_point_id="ep2", unit_id="u2-a", card_id="c2-a"),
        PlanItem(item_index=4, question_type="single_choice", score=2.0, anchor_key="A2",
                 exam_point_id="ep2", unit_id="u2-b", card_id="c2-b"),
    ]
    plan = BlueprintPlan(
        total_score=8.0, items=items,
        type_counts={"single_choice": 4},
        difficulty_counts={"single_choice": {"low": 0, "medium": 4, "high": 0}},
        anchor_counts={"A1": 2, "A2": 2},
    )
    contract = allocate_paper_contract(_request(plan, units, cards))

    assert contract.total_score == 8.0
    assert len(contract.slots) == 4
    assert sum(1 for s in contract.slots if s.anchor_key == "A1") == 2
    assert sum(1 for s in contract.slots if s.anchor_key == "A2") == 2
    assert {s.exam_point_id for s in contract.slots if s.anchor_key == "A2"} == {"ep2"}
    assert not contract.conflicts


def test_backfilled_paper_still_satisfies_answer_mutex():
    """回补后的合同必须仍然满足全卷答案域互斥（构造性保证不放松）。"""
    plan = _plan(4, exam_point_id="ep-weak", unit_id="u-weak", card_id="c-weak")
    contract = allocate_paper_contract(_request(plan, _units(), _cards()))

    for i, left in enumerate(contract.slots):
        for right in contract.slots[i + 1:]:
            assert not boundaries_overlap(left.answer_boundary, right.answer_boundary)


def test_no_sibling_capacity_still_reports_conflict_loudly():
    """同章没有任何富余考点时保持显式冲突，绝不静默降分。"""
    units = [
        UnitCoverage(unit_id="u-weak", exam_point_id="ep-weak", anchor_key="A1", card_ids=["c-weak"]),
    ]
    plan = _plan(3, exam_point_id="ep-weak", unit_id="u-weak", card_id="c-weak")
    contract = allocate_paper_contract(_request(plan, units, _cards()))

    assert contract.total_score == 2.0
    assert len(contract.slots) == 1
    assert contract.conflicts
    assert any(c.code == "atom_pool_insufficient" for c in contract.conflicts)
    assert any(c.code == "cluster_exhausted" for c in contract.conflicts)


def test_sibling_without_free_boundary_cannot_absorb_extra_item():
    """兄弟考点存在但答案域都已被占用时不能硬塞，仍报冲突。"""
    units = [
        UnitCoverage(unit_id="u-weak", exam_point_id="ep-weak", anchor_key="A1", card_ids=["c-weak"]),
        UnitCoverage(unit_id="u-collide", exam_point_id="ep-collide", anchor_key="A1", card_ids=["c-collide"]),
    ]
    cards = _cards() | {
        "c-collide": {
            "is_core": True, "performance_statement": "掌握训练数据配比设计",
            "assessable_content": ["配比失衡会导致能力退化"],
            "preferred_terms": ["配比"], "answer_boundary": "训练数据配比设计",
        },
    }
    plan = _plan(3, exam_point_id="ep-weak", unit_id="u-weak", card_id="c-weak")
    contract = allocate_paper_contract(_request(plan, units, cards))

    assert contract.total_score == 2.0
    assert len(contract.slots) == 1
    assert contract.conflicts
