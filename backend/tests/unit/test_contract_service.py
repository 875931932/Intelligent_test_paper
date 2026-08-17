from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.domain.generation.contract import boundaries_overlap
from app.services.contract_service import ContractRequest, allocate_paper_contract


def _units():
    # 每考点多个单元、各持一张边界互斥的知识卡：boundary 是卡级标量，
    # 同考点多题必须来自不同卡才能构造性保证答案域互斥。
    return [
        UnitCoverage(unit_id="U1a", exam_point_id="EP1", anchor_key="A1", card_ids=["C1a"]),
        UnitCoverage(unit_id="U1b", exam_point_id="EP1", anchor_key="A1", card_ids=["C1b"]),
        UnitCoverage(unit_id="U1c", exam_point_id="EP1", anchor_key="A1", card_ids=["C1c"]),
        UnitCoverage(unit_id="U2a", exam_point_id="EP2", anchor_key="A2", card_ids=["C2a"]),
        UnitCoverage(unit_id="U2b", exam_point_id="EP2", anchor_key="A2", card_ids=["C2b"]),
        UnitCoverage(unit_id="U2c", exam_point_id="EP2", anchor_key="A2", card_ids=["C2c"]),
    ]


def _cards():
    return {
        "C1a": {
            "is_core": True, "performance_statement": "掌握提示词要素与优化",
            "assessable_content": ["有效提示词包含角色设定要素", "有效提示词包含任务说明要求"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词角色与任务要素",
            "prompt_material": ["角色设定示例"],
        },
        "C1b": {
            "is_core": True, "performance_statement": "掌握提示词要素与优化",
            "assessable_content": ["提示词可加入背景信息补充", "提示词输出格式约束条件"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词背景与格式要素",
            "prompt_material": ["格式约束示例"],
        },
        "C1c": {
            "is_core": True, "performance_statement": "掌握提示词要素与优化",
            "assessable_content": ["提示词迭代优化需要评估反馈", "提示词版本对比需要评测指标"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词评估与迭代要素",
            "prompt_material": ["评估反馈示例"],
        },
        "C2a": {
            "is_core": True, "performance_statement": "掌握SFT训练方法",
            "assessable_content": ["构建SFTTrainer需要SFTConfig配置", "QLoRA使用NF4量化格式"],
            "preferred_terms": ["SFT"], "answer_boundary": "SFT配置方法",
            "prompt_material": ["SFTConfig示例"],
        },
        "C2b": {
            "is_core": True, "performance_statement": "掌握SFT训练方法",
            "assessable_content": ["继续预训练适配领域语料", "训练数据集需格式化处理"],
            "preferred_terms": ["SFT"], "answer_boundary": "SFT数据处理",
            "prompt_material": ["数据集示例"],
        },
        "C2c": {
            "is_core": True, "performance_statement": "掌握SFT训练方法",
            "assessable_content": ["SFT训练需要学习率调度策略", "SFT训练需要梯度累积技巧"],
            "preferred_terms": ["SFT"], "answer_boundary": "SFT训练策略",
            "prompt_material": ["调度策略示例"],
        },
    }


def _request(**overrides):
    base = dict(
        blueprint=BlueprintRequest(
            total_score=10,
            type_rules={"single_choice": {"count": 5, "score": 2}},
            chapter_weights={"A1": 40, "A2": 60},
            units=_units(),
        ),
        knowledge_cards=_cards(),
    )
    base.update(overrides)
    return ContractRequest(**base)


def test_contract_slots_follow_chapter_quota_proportions():
    contract = allocate_paper_contract(_request())
    ep1 = sum(1 for s in contract.slots if s.exam_point_id == "EP1")
    ep2 = sum(1 for s in contract.slots if s.exam_point_id == "EP2")
    assert ep1 + ep2 == 5
    assert ep1 == 2 and ep2 == 3  # 40/60 配额：最大余数法必产 2/3


def test_quota_reflects_weight_direction():
    request = _request(blueprint=BlueprintRequest(
        total_score=10,
        type_rules={"single_choice": {"count": 5, "score": 2}},
        chapter_weights={"A1": 60, "A2": 40},
        units=_units(),
    ))
    contract = allocate_paper_contract(request)
    ep1 = sum(1 for s in contract.slots if s.exam_point_id == "EP1")
    ep2 = sum(1 for s in contract.slots if s.exam_point_id == "EP2")
    assert ep1 >= 3 and ep2 <= 2  # 60/40 权重方向：EP1 占多数


def test_atoms_unique_across_paper_and_mutex_within_point():
    contract = allocate_paper_contract(_request())
    atoms = [s.coverage_atom for s in contract.slots]
    assert len(atoms) == len(set(atoms))
    for point in {s.exam_point_id for s in contract.slots}:
        group = [s for s in contract.slots if s.exam_point_id == point]
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                assert not boundaries_overlap(left.answer_boundary, right.answer_boundary)


def test_slot_forbidden_context_lists_same_point_siblings():
    contract = allocate_paper_contract(_request())
    for slot in contract.slots:
        siblings = [s for s in contract.slots
                    if s.exam_point_id == slot.exam_point_id and s.item_index != slot.item_index]
        assert set(slot.forbidden_context.atoms) == {s.coverage_atom for s in siblings}
        assert set(slot.forbidden_context.answer_cores) == {s.answer_boundary for s in siblings if s.answer_boundary}


def test_pool_insufficient_produces_conflict_not_silent_gap():
    weak_cards = dict(_cards())
    for card_id in ("C2a", "C2b", "C2c"):
        weak_cards[card_id] = {
            "is_core": False, "performance_statement": "了解即可",
            "assessable_content": ["某脚注（第3页）细节"],
            "preferred_terms": [], "answer_boundary": "脚注",
        }
    contract = allocate_paper_contract(_request(knowledge_cards=weak_cards))
    assert any(c.code == "atom_pool_insufficient" for c in contract.conflicts)
    assert all(s.exam_point_id == "EP1" for s in contract.slots)


def test_comprehensive_slots_get_distinct_archetypes():
    request = ContractRequest(
        blueprint=BlueprintRequest(
            total_score=18,
            type_rules={"comprehensive": {"count": 3, "score": 6}},
            chapter_weights={"A1": 100},
            units=[u for u in _units() if u.anchor_key == "A1"],
        ),
        knowledge_cards=_cards(),
    )
    contract = allocate_paper_contract(request)
    archetypes = [s.comprehensive_archetype for s in contract.slots]
    assert all(archetypes)
    assert len(set(archetypes)) == 3


def test_slots_sorted_by_item_index_with_full_traceability():
    contract = allocate_paper_contract(_request())
    indexes = [s.item_index for s in contract.slots]
    assert indexes == sorted(indexes)
    for s in contract.slots:
        assert s.exam_point_id and s.unit_id and s.card_id and s.coverage_atom and s.answer_boundary


def test_audit_summary_reports_proportions_and_totals():
    contract = allocate_paper_contract(_request())
    summary = contract.audit_summary
    assert sum(p.question_count for p in summary.exam_points) == len(contract.slots)
    assert sum(summary.type_counts.values()) == len(contract.slots)
    assert sum(summary.difficulty_counts.values()) == len(contract.slots)
    assert contract.total_score == sum(s.score for s in contract.slots)


def test_allocate_is_deterministic_for_identical_requests():
    first = allocate_paper_contract(_request())
    second = allocate_paper_contract(_request())
    assert first.model_dump() == second.model_dump()
