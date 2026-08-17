from app.domain.generation.batching import BATCH_MAX_SIZE, BATCH_MIN_SIZE, QuestionBatch, split_contract_into_batches
from app.domain.generation.contract import ContractSlot


def _slot(index: int, point: str, anchor: str = "A1") -> ContractSlot:
    return ContractSlot(
        item_index=index, question_type="single_choice", score=2, difficulty="medium",
        cognitive_level="understand", exam_point_id=point, anchor_key=anchor,
        unit_id=f"U-{point}", card_id=f"C{index}", coverage_atom=f"原子{index}",
        answer_boundary=f"边界{index}",
    )


def test_same_point_slots_stay_in_one_batch():
    batches = split_contract_into_batches([_slot(i, "EP1") for i in range(1, 4)])
    assert len(batches) == 1
    assert all(s.exam_point_id == "EP1" for s in batches[0].slots)


def test_different_points_form_separate_batches():
    slots = [_slot(1, "EP1"), _slot(2, "EP1"), _slot(3, "EP1"), _slot(4, "EP2"), _slot(5, "EP2"), _slot(6, "EP2")]
    batches = split_contract_into_batches(slots)
    assert len(batches) == 2
    assert {frozenset(b.exam_point_ids) for b in batches} == {frozenset({"EP1"}), frozenset({"EP2"})}


def test_large_point_splits_into_subbatches_with_forbidden_context():
    slots = [_slot(i, "EP1") for i in range(1, 9)]  # 8 题 > 6
    batches = split_contract_into_batches(slots)
    sizes = [len(b.slots) for b in batches]
    assert all(size <= BATCH_MAX_SIZE for size in sizes)
    assert sum(sizes) == 8
    assert len(batches) == 2  # 6 + 2
    # 子批禁用上下文：批1 能看到批2 的原子与答案核心
    by_first_item = sorted(batches, key=lambda b: b.slots[0].item_index)
    first, second = by_first_item
    assert set(second.coverage_atom_texts()) <= set(first.forbidden_context.atoms) if hasattr(first, "coverage_atom_texts") else True
    assert {s.coverage_atom for s in second.slots} <= set(first.forbidden_context.atoms)
    assert {s.answer_boundary for s in second.slots} <= set(first.forbidden_context.answer_cores)
    # 反向同样
    assert {s.coverage_atom for s in first.slots} <= set(second.forbidden_context.atoms)


def test_small_points_merge_by_same_anchor():
    slots = [_slot(1, "EP1", "A1"), _slot(2, "EP2", "A1"), _slot(3, "EP3", "A2")]
    batches = split_contract_into_batches(slots)
    # EP1+EP2 都 <3 题 → 合并为一批；EP3 单独（A2 无同伴但也是小考点，单考点一批）
    assert len(batches) == 2
    merged = next(b for b in batches if len(b.exam_point_ids) == 2)
    assert set(merged.exam_point_ids) == {"EP1", "EP2"}


def test_distinct_anchor_points_do_not_merge():
    slots = [_slot(1, "EP1", "A1"), _slot(2, "EP2", "A2"), _slot(3, "EP3", "A3")]
    batches = split_contract_into_batches(slots)
    # 全部 <3 且 anchor 互不相同 → 各自一批（3 批）
    assert len(batches) == 3


def test_exact_min_size_is_not_merged():
    slots = [_slot(i, "EP1") for i in range(1, BATCH_MIN_SIZE + 1)]  # 恰好 3 题
    batches = split_contract_into_batches(slots)
    assert len(batches) == 1
    assert batches[0].exam_point_ids == ["EP1"]


def test_batch_ids_unique_and_ordered():
    slots = [_slot(i, "EP1") for i in range(1, 9)] + [_slot(9, "EP2"), _slot(10, "EP2")]
    batches = split_contract_into_batches(slots)
    ids = [b.batch_id for b in batches]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)


def test_slots_sorted_within_batch():
    slots = [_slot(3, "EP1"), _slot(1, "EP1"), _slot(2, "EP1")]
    batches = split_contract_into_batches(slots)
    assert [s.item_index for s in batches[0].slots] == [1, 2, 3]


def test_empty_contract_returns_empty():
    assert split_contract_into_batches([]) == []
