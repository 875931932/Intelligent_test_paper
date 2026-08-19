from app.domain.blueprint.models import PlanItem
from app.domain.generation.contract import PoolAtom, atom_bigram_features, assign_atoms_to_items


def _atom(text: str, boundary: str, centrality: float = 0.8) -> PoolAtom:
    return PoolAtom(card_id="C1", unit_id="U1", exam_point_id="EP1", atom_text=text,
                    boundary=boundary, centrality=centrality, features=atom_bigram_features(text))


def _item(index: int, question_type: str = "single_choice") -> PlanItem:
    return PlanItem(item_index=index, question_type=question_type, score=2,
                    anchor_key="A1", exam_point_id="EP1", unit_id="U1", card_id="C1")


def test_consecutive_items_rotate_across_clusters():
    clusters = [
        [_atom("SFTTrainer需要SFTConfig配置参数", "SFTConfig参数")],
        [_atom("QLoRA使用NF4量化格式压缩", "NF4量化格式")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    assert assignments[0][1].atom_text != assignments[1][1].atom_text  # 跨簇


def test_answer_boundary_mutex_skips_conflicting_candidate():
    # 簇2 第一个原子的边界与题位1已选边界重叠 → 必须跳过取簇2第二个原子
    clusters = [
        [_atom("SFTTrainer需要SFTConfig配置参数", "量化格式NF4")],
        [_atom("QLoRA使用NF4量化压缩技术", "量化格式NF4"), _atom("模型评估衡量泛化能力表现", "泛化能力")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    boundaries = [a[1].boundary for a in assignments]
    assert boundaries == ["量化格式NF4", "泛化能力"]


def test_atom_key_dedup_skips_repeated_atom_across_units():
    # 两簇含相同 atom_key 的原子 → 第二次出现必须跳过
    clusters = [
        [_atom("SFTTrainer需要SFTConfig配置参数", "SFTConfig参数")],
        [_atom("SFTTrainer需要SFTConfig配置参数", "另一个边界XYZ"), _atom("训练数据集格式化要求", "数据集格式")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    assert assignments[1][1].atom_text == "训练数据集格式化要求"


def test_cluster_exhausted_reports_conflict():
    clusters = [[_atom("唯一可用原子文本", "唯一边界")]]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert len(assignments) == 1
    assert conflicts and conflicts[0].code == "cluster_exhausted"
    assert conflicts[0].detail["item_index"] == 2


def test_no_clusters_reports_conflict_for_every_item():
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], [])
    assert assignments == []
    assert len(conflicts) == 2


def test_same_seed_reproduces_same_atom_selection():
    # 富余池（4 簇各 1 原子，2 题位）：并列打破靠种子
    clusters = [
        [_atom(f"原子文本甲{ch}", f"边界甲{ch}")] for ch in "ABCD"
    ]
    items = [_item(1), _item(2)]
    run_a = assign_atoms_to_items(items, clusters, seed=42)
    run_b = assign_atoms_to_items(items, clusters, seed=42)
    assert [a[1].atom_text for a in run_a[0]] == [a[1].atom_text for a in run_b[0]]


def test_different_seeds_yield_different_atom_combinations():
    # 富余池上不同种子应至少有一次选出不同组合（并列被不同打破）
    clusters = [
        [_atom(f"原子文本甲{ch}", f"边界甲{ch}")] for ch in "ABCD"
    ]
    items = [_item(1), _item(2)]
    picks = {
        tuple(a[1].atom_text for a in assign_atoms_to_items(items, clusters, seed=seed)[0])
        for seed in range(12)
    }
    assert len(picks) > 1


def test_seed_never_violates_hard_constraints():
    # 任何种子下：原子唯一、答案域互斥不破
    clusters = [
        [_atom("SFTTrainer需要SFTConfig配置参数", "SFTConfig参数")],
        [_atom("QLoRA使用NF4量化格式压缩", "NF4量化格式")],
        [_atom("模型评估衡量泛化能力表现", "泛化能力指标")],
    ]
    for seed in range(8):
        assignments, conflicts = assign_atoms_to_items(
            [_item(i) for i in (1, 2, 3)], clusters, seed=seed,
        )
        assert not conflicts
        texts = [a[1].atom_text for a in assignments]
        bounds = [a[1].boundary for a in assignments]
        assert len(set(texts)) == 3
        assert len(set(bounds)) == 3


def test_same_cluster_reuse_when_clusters_fewer_than_items():
    # 单簇两个原子：池紧张时贪心自动退化同簇连供（同题型也不再被硬拒绝）
    clusters = [
        [_atom("原子甲的完整表述", "边界甲"), _atom("原子乙的完整表述", "边界乙")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    assert len(assignments) == 2
    assert assignments[0][1].atom_text != assignments[1][1].atom_text


def test_rotation_spreads_three_items_over_two_clusters():
    # 3 题 2 簇：贪心 簇0→簇1→簇0（簇0 第二个原子）
    clusters = [
        [_atom("甲簇第一原子文本样本", "甲边界一"), _atom("甲簇第二原子文本示例", "甲边界二")],
        [_atom("乙簇唯一原子文本样例", "乙边界一")],
    ]
    assignments, conflicts = assign_atoms_to_items(
        [_item(1), _item(2), _item(3, "true_false")], clusters
    )
    assert not conflicts
    texts = [a[1].atom_text for a in assignments]
    assert texts == ["甲簇第一原子文本样本", "乙簇唯一原子文本样例", "甲簇第二原子文本示例"]


def test_shared_state_enforces_cross_caller_mutex():
    # 模拟两个考点（两次调用共享同一 used 集）：第二考点必须跳过
    # 与第一考点已选边界重叠的原子，实现全卷边界互斥
    shared_keys: set[str] = set()
    shared_boundaries: list[str] = []
    ep1_clusters = [[_atom("SFTTrainer需要SFTConfig配置参数", "量化格式NF4")]]
    ep2_clusters = [
        [_atom("QLoRA使用NF4量化压缩技术", "量化格式NF4")],
        [_atom("模型评估衡量泛化能力表现", "泛化能力")],
    ]
    first, first_conflicts = assign_atoms_to_items(
        [_item(1)], ep1_clusters,
        shared_used_keys=shared_keys, shared_used_boundaries=shared_boundaries,
    )
    assert not first_conflicts
    second, conflicts = assign_atoms_to_items(
        [_item(2)], ep2_clusters,
        shared_used_keys=shared_keys, shared_used_boundaries=shared_boundaries,
    )
    assert not conflicts
    assert second[0][1].boundary == "泛化能力"  # 跳过与考点1重叠的“量化格式NF4”
    assert shared_keys == {first[0][1].atom_key, second[0][1].atom_key}
    assert shared_boundaries == ["量化格式NF4", "泛化能力"]


def test_greedy_prefers_unused_clusters():
    # 池充足（3 簇各 3+ 原子 ≥ 6 题位）时贪心自然跨簇分散：每簇恰供 2 题，
    # 大簇不扎堆，全部题位无冲突满足
    qlora_cluster = [
        _atom(f"QLoRA降低显存占用量化方案{t}", f"甲边界{t}") for t in "一二三四五六"
    ]
    other_a = [
        _atom("提示词包含角色设定要素", "乙边界一"),
        _atom("模型评估衡量泛化能力表现", "乙边界二"),
        _atom("训练数据集需要格式化处理", "乙边界三"),
    ]
    other_b = [
        _atom("领域语料继续预训练适配", "丙边界一"),
        _atom("梯度累积技巧缓解显存压力", "丙边界二"),
        _atom("学习率调度策略影响收敛", "丙边界三"),
    ]
    types = ["single_choice", "true_false", "single_choice",
             "true_false", "true_false", "single_choice"]
    items = [_item(i + 1, t) for i, t in enumerate(types)]
    assignments, conflicts = assign_atoms_to_items(items, [qlora_cluster, other_a, other_b])
    assert not conflicts
    cluster_texts = [
        {a.atom_text for a in cluster}
        for cluster in (qlora_cluster, other_a, other_b)
    ]
    for idx, texts in enumerate(cluster_texts):
        supplied = sum(1 for _, atom in assignments if atom.atom_text in texts)
        assert supplied == 2, f"cluster#{idx} 供给 {supplied} 题（期望每簇 2 题）"


def test_type_spread_within_cluster():
    # 大簇 7 原子、小簇各 1 原子：小簇先各供 1 题，大簇吸纳剩余 4 题
    # （判断×2、选择×1、填空×1），其两道判断题在判断题位序列里互相隔开
    # （中间隔着其他簇的判断题）
    qlora_cluster = [
        _atom(f"QLoRA降低显存占用量化方案{t}", f"甲边界{t}") for t in "一二三四五六七"
    ]
    other_a = [_atom("模型评估衡量泛化能力表现", "乙边界一")]
    other_b = [_atom("领域语料继续预训练适配", "丙边界一")]
    types = ["true_false", "single_choice", "true_false",
             "single_choice", "true_false", "fill_blank"]
    items = [_item(i + 1, t) for i, t in enumerate(types)]
    assignments, conflicts = assign_atoms_to_items(items, [qlora_cluster, other_a, other_b])
    assert not conflicts
    assert len(assignments) == 6
    qlora_texts = {a.atom_text for a in qlora_cluster}
    qlora_items = [(item, atom) for item, atom in assignments if atom.atom_text in qlora_texts]
    assert len(qlora_items) == 4  # 小簇耗尽后大簇吸纳剩余题位，供给 4 题
    # 小簇各供 1 题（判断题位序列中间的隔板）
    assert {a.atom_text for _, a in assignments} >= {"模型评估衡量泛化能力表现", "领域语料继续预训练适配"}
    qlora_tf = [item.item_index for item, atom in qlora_items
                if item.question_type == "true_false"]
    assert len(qlora_tf) == 2
    assert qlora_tf[1] - qlora_tf[0] >= 2  # 两道判断题位不相邻（隔着小簇的判断题）


def test_pool_exhaustion_still_reports_conflict():
    # 原子数 < 题位数：真正的池耗尽如实报 cluster_exhausted，不静默降级
    # （同簇同题型连供 2 题不再被拒，第 3 题起才因池耗尽报冲突）
    qlora_cluster = [
        _atom("QLoRA量化降低显存占用", "甲边界一"),
        _atom("QLoRA部署消费级显卡方案", "甲边界二"),
    ]
    items = [_item(i + 1, "true_false") for i in range(4)]
    assignments, conflicts = assign_atoms_to_items(items, [qlora_cluster])
    assert len(assignments) == 2
    assert all(c.code == "cluster_exhausted" for c in conflicts)
    assert [c.detail["item_index"] for c in conflicts] == [3, 4]


def test_same_cluster_same_type_questions_spread():
    # 2 簇（大 3 原子、小 1 原子）4 道判断题：同簇同题型不再被硬拒绝，
    # 贪心先让小簇隔开——小簇供 1 题、大簇供 3 题，大簇前两道判断题位
    # 不相邻（题位 2 由小簇供给），小簇耗尽后大簇才连供
    big_cluster = [
        _atom(f"QLoRA量化降低显存占用方案{t}", f"甲边界{t}") for t in "一二三"
    ]
    small_cluster = [_atom("模型评估衡量泛化能力表现", "乙边界一")]
    items = [_item(i + 1, "true_false") for i in range(4)]
    assignments, conflicts = assign_atoms_to_items(items, [big_cluster, small_cluster])
    assert not conflicts
    assert len(assignments) == 4
    big_texts = {a.atom_text for a in big_cluster}
    item_cluster = [
        "big" if atom.atom_text in big_texts else "small" for _, atom in assignments
    ]
    assert item_cluster == ["big", "small", "big", "big"]  # 小簇在中间隔开
    big_indexes = [item.item_index for item, atom in assignments
                   if atom.atom_text in big_texts]
    assert big_indexes[1] - big_indexes[0] >= 2  # 大簇前两道判断题位不相邻


def test_same_cluster_same_type_allowed_without_conflict():
    # 池里只有该簇且两题位同题型：同簇同题型连供合法，不再报冲突
    qlora_cluster = [
        _atom("QLoRA量化降低显存占用", "甲边界一"),
        _atom("QLoRA部署消费级显卡方案", "甲边界二"),
    ]
    assignments, conflicts = assign_atoms_to_items(
        [_item(1, "true_false"), _item(2, "true_false")], [qlora_cluster]
    )
    assert not conflicts
    assert len(assignments) == 2
    assert assignments[0][1].atom_text != assignments[1][1].atom_text
