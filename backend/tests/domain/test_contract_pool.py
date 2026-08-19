from app.domain.blueprint.models import UnitCoverage
from app.domain.generation.contract import DEFAULT_CENTRALITY_THRESHOLD, PoolAtom, build_exam_point_pools


def _units():
    return [
        UnitCoverage(unit_id="U1", exam_point_id="EP1", anchor_key="A1", card_ids=["C1", "C2"]),
        UnitCoverage(unit_id="U2", exam_point_id="EP2", anchor_key="A1", card_ids=["C3"]),
    ]


def _cards():
    return {
        "C1": {
            "is_core": True,
            "performance_statement": "掌握提示词要素",
            "assessable_content": ["有效提示词包含角色设定", "有效提示词包含任务说明"],
            "preferred_terms": ["提示词"],
            "answer_boundary": "提示词要素",
            "concept_cluster": "提示词要素组织与限制条件设计",
        },
        "C2": {
            "is_core": True,
            "performance_statement": "掌握提示词优化方法",
            "assessable_content": ["提示词可加入背景信息"],
            "preferred_terms": [],
            "answer_boundary": "背景信息",
            "concept_cluster": "提示词要素组织与限制条件设计",
        },
        "C3": {
            "is_core": False,
            "performance_statement": "了解即可",
            "assessable_content": ["某脚注（第3页）细节说明"],
            "preferred_terms": [],
            "answer_boundary": "脚注细节",
        },
    }


def test_pool_contains_all_atoms_of_ep1_cards():
    pools = build_exam_point_pools(_units(), _cards())
    assert len(pools["EP1"]) == 3


def test_low_centrality_atoms_are_filtered_out():
    pools = build_exam_point_pools(_units(), _cards())
    assert pools.get("EP2") is None or pools["EP2"] == []  # C3 原子低于 0.6 被剔除
    for atom in pools["EP1"]:
        assert atom.centrality >= DEFAULT_CENTRALITY_THRESHOLD


def test_pool_sorted_by_centrality_desc():
    pools = build_exam_point_pools(_units(), _cards())
    centralities = [a.centrality for a in pools["EP1"]]
    assert centralities == sorted(centralities, reverse=True)


def test_pool_atom_carries_card_unit_point_and_boundary():
    pools = build_exam_point_pools(_units(), _cards())
    atom = pools["EP1"][0]
    assert isinstance(atom, PoolAtom)
    assert atom.card_id in {"C1", "C2"}
    assert atom.unit_id == "U1"
    assert atom.exam_point_id == "EP1"
    assert atom.boundary in {"提示词要素", "背景信息"}


def test_pool_atom_carries_concept_cluster_label():
    pools = build_exam_point_pools(_units(), _cards())
    for atom in pools["EP1"]:
        assert atom.concept_cluster == "提示词要素组织与限制条件设计"


def test_missing_card_is_skipped_silently():
    units = [UnitCoverage(unit_id="U1", exam_point_id="EP1", anchor_key="A1", card_ids=["C1", "GONE"])]
    pools = build_exam_point_pools(units, _cards())
    assert len(pools["EP1"]) == 2


def test_unit_without_exam_point_is_skipped():
    units = [UnitCoverage(unit_id="U9", exam_point_id="", anchor_key="A1", card_ids=["C1"])]
    pools = build_exam_point_pools(units, _cards())
    assert pools == {}
