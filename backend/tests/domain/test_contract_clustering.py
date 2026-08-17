from app.domain.generation.contract import PoolAtom, atom_bigram_features, cluster_pool_atoms


def _atom(text: str, centrality: float = 0.8) -> PoolAtom:
    return PoolAtom(card_id="C", unit_id="U", exam_point_id="EP",
                    atom_text=text, boundary="", centrality=centrality,
                    features=atom_bigram_features(text))


def test_semantically_similar_atoms_land_in_same_cluster():
    pool = [
        _atom("构建SFTTrainer需要传入SFTConfig训练参数"),
        _atom("构建SFTTrainer需要传入训练数据集"),
        _atom("QLoRA使用NF4格式进行模型量化"),
    ]
    clusters = cluster_pool_atoms(pool)
    assert len(clusters) == 2
    flattened = [sorted(a.atom_text for a in c) for c in clusters]
    assert ["构建SFTTrainer需要传入SFTConfig训练参数", "构建SFTTrainer需要传入训练数据集"] in flattened


def test_disjoint_atoms_each_own_cluster():
    pool = [_atom("提示词包含角色设定"), _atom("QLoRA使用NF4量化"), _atom("模型评估衡量泛化能力")]
    assert len(cluster_pool_atoms(pool)) == 3


def test_clusters_sorted_by_max_centrality_desc():
    pool = [_atom("低分主题文本", centrality=0.4), _atom("高分主题甲文本", centrality=0.9)]
    clusters = cluster_pool_atoms(pool)
    assert clusters[0][0].centrality >= clusters[-1][0].centrality


def test_atoms_inside_cluster_sorted_by_centrality_desc():
    pool = [
        _atom("构建SFTTrainer需要传入SFTConfig训练参数", centrality=0.7),
        _atom("构建SFTTrainer需要传入训练数据集", centrality=0.95),  # 同簇但分更高
    ]
    clusters = cluster_pool_atoms(pool)
    assert len(clusters) == 1
    assert clusters[0][0].atom_text == "构建SFTTrainer需要传入训练数据集"


def test_transitive_chain_merges_into_one_cluster():
    # A~B 相似(0.61)、B~C 相似(0.72)、A~C 不相似(0.44) → 传递合并为一簇
    a = _atom("掌握提示词的角色设定要素")
    b = _atom("掌握提示词的角色设定要素与任务说明要求")
    c = _atom("掌握提示词的角色设定要素与任务说明要求及输出格式规范")
    clusters = cluster_pool_atoms([a, b, c])
    assert len(clusters) == 1


def test_empty_pool_returns_empty_list():
    assert cluster_pool_atoms([]) == []


def test_single_atom_returns_single_cluster():
    clusters = cluster_pool_atoms([_atom("孤立原子")])
    assert len(clusters) == 1 and clusters[0][0].atom_text == "孤立原子"
