from app.domain.blueprint.models import PlanItem
from app.domain.generation.contract import PoolAtom, atom_bigram_features, assign_atoms_to_items


def _atom(text: str, boundary: str, centrality: float = 0.8) -> PoolAtom:
    return PoolAtom(card_id="C1", unit_id="U1", exam_point_id="EP1", atom_text=text,
                    boundary=boundary, centrality=centrality, features=atom_bigram_features(text))


def _item(index: int) -> PlanItem:
    return PlanItem(item_index=index, question_type="single_choice", score=2,
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
    clusters = [
        [_atom("原子甲的完整表述", "边界甲"), _atom("原子乙的完整表述", "边界乙")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    assert len(assignments) == 2
    assert assignments[0][1].atom_text != assignments[1][1].atom_text


def test_rotation_spreads_three_items_over_two_clusters():
    # 3 题 2 簇：轮转 簇0→簇1→簇0（簇0 第二个原子）
    clusters = [
        [_atom("甲簇第一原子文本样本", "甲边界一"), _atom("甲簇第二原子文本示例", "甲边界二")],
        [_atom("乙簇唯一原子文本样例", "乙边界一")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2), _item(3)], clusters)
    assert not conflicts
    texts = [a[1].atom_text for a in assignments]
    assert texts == ["甲簇第一原子文本样本", "乙簇唯一原子文本样例", "甲簇第二原子文本示例"]
