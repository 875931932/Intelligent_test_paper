import pytest
from pydantic import ValidationError

from app.domain.generation.contract import (
    DEFAULT_CENTRALITY_THRESHOLD,
    ContractSlot,
    ForbiddenContext,
    PaperContract,
    boundaries_overlap,
    compute_atom_centrality,
)


def _card(**overrides):
    base = {
        "is_core": True,
        "performance_statement": "掌握SFTTrainer构建方法",
        "assessable_content": ["构建SFTTrainer需要传入SFTConfig"],
        "preferred_terms": ["SFTTrainer"],
        "relation_edges": [],
    }
    base.update(overrides)
    return base


def test_centrality_core_card_scores_above_threshold():
    score = compute_atom_centrality(_card(), "构建SFTTrainer需要传入SFTConfig")
    assert score >= DEFAULT_CENTRALITY_THRESHOLD


def test_centrality_obscure_atom_scores_lower():
    core = compute_atom_centrality(_card(), "构建SFTTrainer需要传入SFTConfig")
    obscure = compute_atom_centrality(_card(is_core=False), "某实验附注（第3页脚注）")
    assert obscure < core


def test_obscure_atom_falls_below_threshold():
    """非核心卡且无任何核心信号的原子（0.45~0.5）应低于阈值，被过滤。"""
    score = compute_atom_centrality(
        _card(is_core=False, performance_statement="了解即可"),
        "某实验附注（第3页脚注）",
    )
    assert score < DEFAULT_CENTRALITY_THRESHOLD


def test_definition_keyword_atom_passes_threshold():
    """非核心卡但原子含定义类关键词（+0.15 → 0.65）应通过阈值。"""
    score = compute_atom_centrality(
        _card(is_core=False, performance_statement="了解即可"),
        "提示词的定义与构成要素",
    )
    assert score >= DEFAULT_CENTRALITY_THRESHOLD


def test_boundaries_overlap_detects_containment_and_equality():
    assert boundaries_overlap("SFTTrainer需要SFTConfig", "SFTTrainer需要SFTConfig")
    assert boundaries_overlap("SFTConfig", "构建SFTTrainer需要SFTConfig")
    assert not boundaries_overlap("SFTConfig", "QLoRA使用NF4量化")


def test_contract_slot_forbids_comprehensive_fields_on_plain_type():
    with pytest.raises(ValidationError):
        ContractSlot(
            item_index=1,
            question_type="single_choice",
            score=2,
            difficulty="medium",
            cognitive_level="understand",
            exam_point_id="EP1",
            anchor_key="A1",
            unit_id="U1",
            card_id="C1",
            coverage_atom="原子",
            answer_boundary="边界",
            comprehensive_archetype="case_analysis",
        )


def test_paper_contract_round_trips_forbidden_context():
    slot = ContractSlot(
        item_index=1, question_type="true_false", score=1, difficulty="low",
        cognitive_level="remember", exam_point_id="EP1", anchor_key="A1",
        unit_id="U1", card_id="C1", coverage_atom="原子A", answer_boundary="核心A",
        forbidden_context=ForbiddenContext(atoms=["原子B"], answer_cores=["核心B"]),
    )
    contract = PaperContract(total_score=1, slots=[slot])
    restored = PaperContract.model_validate(contract.model_dump(mode="json"))
    assert restored.slots[0].forbidden_context.atoms == ["原子B"]
