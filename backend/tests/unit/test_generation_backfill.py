"""生成自愈：同章回补的目标选择必须受框架约束。"""
from app.domain.generation.contract import ContractSlot
from app.workflows.generation_graph import (
    _card_allows_question_type,
    _pick_backfill_target,
)


def _slot(**over):
    base = {
        "item_index": 7,
        "question_type": "fill_blank",
        "score": 2.0,
        "difficulty": "medium",
        "cognitive_level": "understand",
        "assessment_mode": "conceptual",
        "exam_point_id": "ep_a",
        "anchor_key": "ch1",
        "unit_id": "u1",
        "card_id": "card_a1",
        "coverage_atom": "失败的原原子",
        "answer_boundary": "原答案域",
        "forbidden_context": {"atoms": [], "answer_cores": []},
    }
    base.update(over)
    return ContractSlot(**base)


def _card(card_id, atom, boundary="边界", allowed=None):
    card = {
        "name": card_id,
        "assessable_content": [atom],
        "answer_boundary": boundary,
    }
    if allowed is not None:
        card["allowed_question_types"] = allowed
    return card


def test_backfill_prefers_sibling_in_same_chapter():
    slot = _slot()
    cards = {
        "card_a1": _card("card_a1", "同考点其他原子"),
        "card_b1": _card("card_b1", "同章富余考点原子"),
        "card_c1": _card("card_c1", "另一章考点原子"),
    }
    point_cards = {"ep_a": ["card_a1"], "ep_b": ["card_b1"], "ep_c": ["card_c1"]}
    point_anchor = {"ep_a": "ch1", "ep_b": "ch1", "ep_c": "ch2"}

    picked = _pick_backfill_target(
        slot, cards, point_cards, point_anchor,
        batch_points={"ep_a"}, occupied_atom_keys=set(), occupied_boundaries=[],
        used_points={}, max_per_point=1,
    )
    assert picked is not None
    to_point, card_id, atom, _boundary = picked
    assert to_point == "ep_b"          # 只回补到同章
    assert card_id == "card_b1"
    assert atom == "同章富余考点原子"


def test_backfill_skips_points_running_their_own_slots():
    """同章其他考点本批有自己的题位在跑，不去抢。"""
    slot = _slot()
    cards = {"card_b1": _card("card_b1", "原子乙")}
    picked = _pick_backfill_target(
        slot, cards, {"ep_a": ["card_a1"], "ep_b": ["card_b1"]},
        {"ep_a": "ch1", "ep_b": "ch1"},
        batch_points={"ep_a", "ep_b"},   # ep_b 正在跑自己的题位
        occupied_atom_keys=set(), occupied_boundaries=[],
        used_points={}, max_per_point=1,
    )
    assert picked is None


def test_backfill_respects_per_point_cap():
    slot = _slot()
    cards = {"card_b1": _card("card_b1", "原子乙")}
    args = dict(
        cards=cards, point_cards={"ep_a": ["card_a1"], "ep_b": ["card_b1"]},
        point_anchor={"ep_a": "ch1", "ep_b": "ch1"},
        batch_points={"ep_a"}, occupied_atom_keys=set(), occupied_boundaries=[],
        max_per_point=1,
    )
    assert _pick_backfill_target(slot, used_points={}, **args) is not None
    # 已被回补过一次的考点不再抽第二次
    assert _pick_backfill_target(slot, used_points={"ep_b": 1}, **args) is None


def test_backfill_respects_card_allowed_question_types():
    """目标考点卡片声明的允许题型不覆盖该题位时不用它。"""
    slot = _slot(question_type="single_choice")
    cards = {
        "card_b1": _card("card_b1", "原子乙", allowed=["判断题"]),
        "card_d1": _card("card_d1", "原子丁", allowed=["单选题"]),
    }
    picked = _pick_backfill_target(
        slot, cards, {"ep_a": ["card_a1"], "ep_b": ["card_b1"], "ep_d": ["card_d1"]},
        {"ep_a": "ch1", "ep_b": "ch1", "ep_d": "ch1"},
        batch_points={"ep_a"}, occupied_atom_keys=set(), occupied_boundaries=[],
        used_points={}, max_per_point=1,
    )
    assert picked is not None and picked[0] == "ep_d"


def test_backfill_avoids_occupied_atoms_and_boundaries():
    slot = _slot()
    cards = {"card_b1": _card("card_b1", "已被占用", boundary="撞了")}
    assert _pick_backfill_target(
        slot, cards, {"ep_a": ["card_a1"], "ep_b": ["card_b1"]},
        {"ep_a": "ch1", "ep_b": "ch1"},
        batch_points={"ep_a"}, occupied_atom_keys={"已被占用"}, occupied_boundaries=[],
        used_points={}, max_per_point=1,
    ) is None
    assert _pick_backfill_target(
        slot, cards, {"ep_a": ["card_a1"], "ep_b": ["card_b1"]},
        {"ep_a": "ch1", "ep_b": "ch1"},
        batch_points={"ep_a"}, occupied_atom_keys=set(), occupied_boundaries=["撞了"],
        used_points={}, max_per_point=1,
    ) is None


def test_card_allows_question_type_accepts_chinese_names_and_empty():
    assert _card_allows_question_type({"allowed_question_types": ["单选题"]}, "single_choice") is True
    assert _card_allows_question_type({"allowed_question_types": ["单选题"]}, "comprehensive") is False
    assert _card_allows_question_type({"allowed_question_types": ["变态题型"]}, "comprehensive") is True
    assert _card_allows_question_type({}, "comprehensive") is True
    assert _card_allows_question_type({"allowed_question_types": []}, "comprehensive") is True


def test_backfill_finds_nothing_without_siblings():
    slot = _slot()
    assert _pick_backfill_target(
        slot, {}, {"ep_a": ["card_a1"]}, {"ep_a": "ch1"},
        batch_points={"ep_a"}, occupied_atom_keys=set(), occupied_boundaries=[],
        used_points={}, max_per_point=1,
    ) is None
