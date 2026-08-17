import pytest

from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.domain.generation.contract import PaperContract
from app.services.contract_service import (
    ContractRequest,
    ContractRevisionError,
    allocate_paper_contract,
    apply_slot_revisions,
)


@pytest.fixture
def units():
    return [
        UnitCoverage(unit_id="U1a", exam_point_id="EP1", anchor_key="A1", card_ids=["C1a"]),
        UnitCoverage(unit_id="U1b", exam_point_id="EP1", anchor_key="A1", card_ids=["C1b"]),
        UnitCoverage(unit_id="U1c", exam_point_id="EP1", anchor_key="A1", card_ids=["C1c"]),
    ]


@pytest.fixture
def cards():
    return {
        "C1a": {
            "is_core": True, "performance_statement": "掌握提示词角色与任务要素",
            "assessable_content": ["有效提示词包含角色设定要素", "备用原子角色相关表述甲"],
            "preferred_terms": ["提示词"], "answer_boundary": "角色与任务要素",
        },
        "C1b": {
            "is_core": True, "performance_statement": "掌握提示词背景与格式要素",
            "assessable_content": ["有效提示词包含背景信息补充"],
            "preferred_terms": [], "answer_boundary": "背景与格式要素",
        },
        "C1c": {
            "is_core": True, "performance_statement": "掌握提示词评估与迭代要素",
            "assessable_content": ["提示词输出格式约束条件"],
            "preferred_terms": [], "answer_boundary": "评估与迭代要素",
        },
    }


@pytest.fixture
def contract(units, cards):
    return allocate_paper_contract(ContractRequest(
        blueprint=BlueprintRequest(
            total_score=6, type_rules={"single_choice": {"count": 3, "score": 2}},
            chapter_weights={"A1": 100}, units=units,
        ),
        knowledge_cards=cards,
    ))


def test_valid_revision_replaces_atom_and_rebuilds_forbidden_context(contract, units, cards):
    revised = apply_slot_revisions(
        contract,
        revisions=[{"item_index": contract.slots[0].item_index,
                    "coverage_atom": "备用原子角色相关表述甲"}],
        units=units, knowledge_cards=cards,
    )
    slot1 = revised.slots[0]
    siblings_atoms = [s.coverage_atom for s in revised.slots[1:]]
    assert slot1.coverage_atom == "备用原子角色相关表述甲"
    assert all(a in slot1.forbidden_context.atoms for a in siblings_atoms)
    # 兄弟题的禁用上下文也同步更新
    for s in revised.slots[1:]:
        assert "备用原子角色相关表述甲" in s.forbidden_context.atoms


def test_revision_rejects_atom_from_other_point_pool(contract, units, cards):
    other_cards = dict(cards)
    other_cards["C2"] = {
        "is_core": True, "performance_statement": "掌握SFT训练配置",
        "assessable_content": ["构建SFTTrainer需要SFTConfig配置"],
        "preferred_terms": [], "answer_boundary": "SFT训练配置",
    }
    other_units = units + [UnitCoverage(unit_id="U2", exam_point_id="EP2", anchor_key="A2", card_ids=["C2"])]
    with pytest.raises(ContractRevisionError):
        apply_slot_revisions(
            contract,
            revisions=[{"item_index": contract.slots[0].item_index,
                        "coverage_atom": "构建SFTTrainer需要SFTConfig配置"}],
            units=other_units, knowledge_cards=other_cards,
        )


def test_revision_rejects_boundary_collision(contract, units, cards):
    # 新增一张与 C1b 同边界的卡 C1d，改题位1到 C1d 的原子 → 必须被拒
    colliding_cards = dict(cards)
    colliding_cards["C1d"] = {
        "is_core": True, "performance_statement": "掌握提示词背景信息",
        "assessable_content": ["背景信息补充的另一种表述方式"],
        "preferred_terms": [], "answer_boundary": "背景与格式要素",  # 与 C1b 相同
    }
    colliding_units = units + [UnitCoverage(unit_id="U1d", exam_point_id="EP1", anchor_key="A1", card_ids=["C1d"])]
    # 找到当前持有 C1b 原子的题位
    holder = next(s for s in contract.slots if s.card_id == "C1b")
    assert holder is not None
    with pytest.raises(ContractRevisionError):
        apply_slot_revisions(
            contract,
            revisions=[{"item_index": contract.slots[0].item_index if contract.slots[0].card_id != "C1b" else contract.slots[-1].item_index,
                        "coverage_atom": "背景信息补充的另一种表述方式"}],
            units=colliding_units, knowledge_cards=colliding_cards,
        )


def test_revision_rejects_unknown_item_index(contract, units, cards):
    with pytest.raises(ContractRevisionError):
        apply_slot_revisions(
            contract, revisions=[{"item_index": 999, "coverage_atom": "任意原子"}],
            units=units, knowledge_cards=cards,
        )


def test_empty_revisions_returns_equivalent_contract(contract, units, cards):
    revised = apply_slot_revisions(contract, revisions=[], units=units, knowledge_cards=cards)
    assert revised.model_dump() == contract.model_dump()


def test_allocate_api_returns_contract_shape(units, cards):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post("/api/v1/courses/course-1/blueprints/allocate", json={
        "blueprint": {
            "total_score": 6,
            "type_rules": {"single_choice": {"count": 3, "score": 2}},
            "chapter_weights": {"A1": 100},
            "units": [u.model_dump() for u in units],
        },
        "knowledge_cards": cards,
    })
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["slots"]) == 3
    assert all("coverage_atom" in s and "forbidden_context" in s for s in payload["slots"])


def test_revision_rejects_duplicate_atom_with_empty_boundaries(contract, units, cards):
    # C1e 边界为空，其原子与题位2相同：空边界可绕过 boundaries_overlap，但原子去重必须拒绝
    duplicate_atom = contract.slots[1].coverage_atom
    empty_boundary_cards = dict(cards)
    empty_boundary_cards["C1e"] = {
        "is_core": True, "performance_statement": "掌握空边界表述",
        "assessable_content": [duplicate_atom],
        "preferred_terms": [], "answer_boundary": "",
    }
    empty_boundary_units = units + [UnitCoverage(unit_id="U1e", exam_point_id="EP1", anchor_key="A1", card_ids=["C1e"])]
    with pytest.raises(ContractRevisionError):
        apply_slot_revisions(
            contract,
            revisions=[{"item_index": contract.slots[0].item_index,
                        "coverage_atom": duplicate_atom}],
            units=empty_boundary_units, knowledge_cards=empty_boundary_cards,
        )


def test_confirm_malformed_revision_returns_422(contract, units, cards):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for malformed in (
        [{"item_index": "abc", "coverage_atom": "x"}],  # item_index 非整数
        [{"coverage_atom": "x"}],  # 缺 item_index
    ):
        response = client.post("/api/v1/courses/course-1/blueprints/confirm", json={
            "contract": contract.model_dump(mode="json"),
            "slot_revisions": malformed,
            "units": [u.model_dump() for u in units],
            "knowledge_cards": cards,
        })
        assert response.status_code == 422


def test_confirm_endpoint_validates_and_applies(contract, units, cards):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post("/api/v1/courses/course-1/blueprints/confirm", json={
        "contract": contract.model_dump(mode="json"),
        "slot_revisions": [
            {"item_index": contract.slots[0].item_index,
             "coverage_atom": "备用原子角色相关表述甲"},
        ],
        "units": [u.model_dump() for u in units],
        "knowledge_cards": cards,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["slots"][0]["coverage_atom"] == "备用原子角色相关表述甲"

    invalid = client.post("/api/v1/courses/course-1/blueprints/confirm", json={
        "contract": contract.model_dump(mode="json"),
        "slot_revisions": [{"item_index": 999, "coverage_atom": "任意原子"}],
        "units": [u.model_dump() for u in units],
        "knowledge_cards": cards,
    })
    assert invalid.status_code == 422
