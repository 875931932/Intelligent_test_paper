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


def test_same_cluster_reuse_when_clusters_fewer_than_items():
    # 单簇两个原子：题位2 轮转回到同簇，取下一个可用原子
    # （同簇第 2 题须换题型，故题位2 为 true_false）
    clusters = [
        [_atom("原子甲的完整表述", "边界甲"), _atom("原子乙的完整表述", "边界乙")],
    ]
    assignments, conflicts = assign_atoms_to_items(
        [_item(1), _item(2, "true_false")], clusters
    )
    assert not conflicts
    assert len(assignments) == 2
    assert assignments[0][1].atom_text != assignments[1][1].atom_text


def test_rotation_spreads_three_items_over_two_clusters():
    # 3 题 2 簇：轮转 簇0→簇1→簇0（簇0 第二个原子）
    # （同簇第 2 题须换题型，故题位3 为 true_false）
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


def test_cluster_quota_caps_at_two():
    # 6 题位，某簇（术语锚合并后）有 6 个原子、其他簇原子充足：
    # 该簇最多供 2 题；两簇配额耗尽后报 cluster_exhausted，不静默降级
    qlora_cluster = [
        _atom(f"QLoRA降低显存占用量化方案{t}", f"甲边界{t}") for t in "一二三四五六"
    ]
    other_cluster = [
        _atom("提示词包含角色设定要素", "乙边界一"),
        _atom("模型评估衡量泛化能力表现", "乙边界二"),
        _atom("训练数据集需要格式化处理", "乙边界三"),
        _atom("梯度累积技巧缓解显存压力", "乙边界四"),
        _atom("学习率调度策略影响收敛", "乙边界五"),
        _atom("领域语料继续预训练适配", "乙边界六"),
    ]
    types = ["single_choice", "true_false", "single_choice",
             "true_false", "true_false", "single_choice"]
    items = [_item(i + 1, t) for i, t in enumerate(types)]
    assignments, conflicts = assign_atoms_to_items(items, [qlora_cluster, other_cluster])
    qlora_count = sum(1 for _, atom in assignments if "QLoRA" in atom.atom_text)
    assert qlora_count == 2  # 6 原子的大簇被配额封顶在 2 题
    assert all(c.code == "cluster_exhausted" for c in conflicts)
    assert [c.detail["item_index"] for c in conflicts] == [5, 6]


def test_same_cluster_two_questions_require_different_types():
    # 轮回同簇时若题型与该簇第 1 题相同 → 跳过该簇，从其他簇取
    qlora_cluster = [
        _atom("QLoRA量化降低显存占用", "甲边界一"),
        _atom("QLoRA部署消费级显卡方案", "甲边界二"),
    ]
    other_cluster = [
        _atom("模型评估衡量泛化能力", "乙边界一"),
        _atom("训练数据集格式化处理", "乙边界二"),
    ]
    items = [_item(1, "single_choice"), _item(2, "true_false"), _item(3, "single_choice")]
    assignments, conflicts = assign_atoms_to_items(items, [qlora_cluster, other_cluster])
    assert not conflicts
    qlora_count = sum(1 for _, atom in assignments if "QLoRA" in atom.atom_text)
    assert qlora_count == 1  # 同簇第 2 题（同为 single_choice）被题型互斥挡下
    assert assignments[2][1].atom_text == "训练数据集格式化处理"  # 改从其他簇取


def test_same_cluster_same_type_only_pool_reports_exhausted():
    # 池里只有该簇且两题位同题型 → 第二题报 cluster_exhausted，不静默降级
    qlora_cluster = [
        _atom("QLoRA量化降低显存占用", "甲边界一"),
        _atom("QLoRA部署消费级显卡方案", "甲边界二"),
    ]
    assignments, conflicts = assign_atoms_to_items(
        [_item(1, "true_false"), _item(2, "true_false")], [qlora_cluster]
    )
    assert len(assignments) == 1
    assert conflicts and conflicts[0].code == "cluster_exhausted"
    assert conflicts[0].detail["item_index"] == 2
