import pytest
from pydantic import ValidationError

from app.domain.generation.contract import (
    DEFAULT_CENTRALITY_THRESHOLD,
    ContractSlot,
    ForbiddenContext,
    PaperContract,
    boundaries_overlap,
    boundaries_share_proposition,
    compute_atom_centrality,
    trim_sibling_proposition_overlap,
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


# ── 分句级命题重叠：线上重复题事故的根治判据 ──────────────────────────
# 事故对：单选与判断同考点，两答案域换措辞考查"最终回复在消息列表最后一项
# content"，整串互含看不见，两题重复计分。
_INCIDENT_SINGLE = (
    "Agent调用invoke的返回值为包含完整消息历史的列表，"
    "最终回复需通过['messages'][-1].content获取；"
    "AIMessage的content字段存储模型回执文本"
)
_INCIDENT_TRUE_FALSE = (
    "Agent invoke返回完整消息历史，"
    "最终回复为消息列表最后一项的content字段；"
    "纯文本回复的AIMessage无工具调用，tool_calls为空列表"
)


def _prop_slot(item_index: int, boundary: str, point: str = "EP1") -> ContractSlot:
    return ContractSlot(
        item_index=item_index,
        question_type="single_choice",
        score=2,
        difficulty="medium",
        cognitive_level="understand",
        exam_point_id=point,
        anchor_key="A1",
        unit_id=f"U{item_index}",
        card_id=f"C{item_index}",
        coverage_atom=f"原子{item_index}",
        answer_boundary=boundary,
    )


def test_boundaries_share_proposition_catches_paraphrased_incident_pair():
    # 旧判据（整串互含）漏报，新判据必须看见；无关边界不误报
    assert not boundaries_overlap(_INCIDENT_SINGLE, _INCIDENT_TRUE_FALSE)
    assert boundaries_share_proposition(_INCIDENT_SINGLE, _INCIDENT_TRUE_FALSE)
    assert not boundaries_share_proposition("SFTTrainer需要SFTConfig", "QLoRA使用NF4量化")


def test_boundaries_share_proposition_fires_on_shared_boilerplate_clause():
    left = "成本资源控制类中间件核心目标是控成本、控配额、防无限调用，预置实现包含ModelCallLimit等"
    right = "决策增强与编排类中间件目标是提升决策质量，拆解任务，预置实现包含TodoList、LLM tool selector"
    assert boundaries_share_proposition(left, right)


def test_trim_gives_incident_proposition_to_front_slot():
    keeper_boundary = (
        "Agent调用invoke的返回值为包含完整消息历史的列表，"
        "最终回复需通过['messages'][-1].content获取"
    )
    loser_boundary = (
        "Agent invoke返回完整消息历史，最终回复为消息列表最后一项的content字段；"
        "纯文本回复的AIMessage无工具调用，tool_calls为空列表"
    )
    slots, trims, conflicts = trim_sibling_proposition_overlap([
        _prop_slot(3, keeper_boundary), _prop_slot(11, loser_boundary),
    ])
    assert conflicts == [] and len(trims) == 1
    assert slots[0].answer_boundary == keeper_boundary  # 靠前题位保留命题
    assert trims[0].item_index == 11 and trims[0].kept_with_item_index == 3
    # 后位题让出背景分句与命题本体（重复通道关闭），非共享分句保留
    assert "完整消息历史" not in slots[1].answer_boundary
    assert "最后一项" not in slots[1].answer_boundary
    assert "纯文本回复的AIMessage无工具调用" in slots[1].answer_boundary


def test_trim_only_removes_boilerplate_and_keeps_own_propositions():
    keeper = "成本资源控制类中间件核心目标是控成本、控配额、防无限调用，预置实现包含ModelCallLimit等"
    loser = "决策增强与编排类中间件目标是提升决策质量，拆解任务，预置实现包含TodoList、LLM tool selector"
    slots, trims, conflicts = trim_sibling_proposition_overlap([
        _prop_slot(4, keeper), _prop_slot(12, loser),
    ])
    assert conflicts == [] and len(trims) == 1
    # 只剔共享引导分句，后位题自己的命题本体原样保留
    assert "决策增强与编排类中间件目标是提升决策质量" in slots[1].answer_boundary
    assert "拆解任务" in slots[1].answer_boundary
    assert "预置实现包含" not in slots[1].answer_boundary


def test_trim_conflicts_instead_of_emptying_boundary():
    keeper = "Agent invoke返回完整消息历史的获取方式"
    loser = "Agent invoke返回完整消息历史的获取方式与调试用法"
    slots, trims, conflicts = trim_sibling_proposition_overlap([
        _prop_slot(1, keeper), _prop_slot(2, loser),
    ])
    assert trims == []  # 剔空即放弃自动剔除
    assert [c.code for c in conflicts] == ["boundary_overlap"]
    assert slots[1].answer_boundary == loser  # 不动边界，交教师处置


def test_trim_ignores_different_exam_points():
    slots, trims, conflicts = trim_sibling_proposition_overlap([
        _prop_slot(1, "完整消息历史与最终回复位置", point="EP1"),
        _prop_slot(2, "完整消息历史与最终回复位置的调试用途", point="EP2"),
    ])
    assert trims == [] and conflicts == []
    assert slots[1].answer_boundary == "完整消息历史与最终回复位置的调试用途"


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
