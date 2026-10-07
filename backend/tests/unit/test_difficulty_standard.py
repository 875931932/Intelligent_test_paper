"""难度标准 v1 的单元测试。

覆盖三个出口：
1. ``canonical_difficulty`` 词表归一（前端 easy/hard、中文词 → low/medium/high）；
2. ``difficulty_spec`` 三档特征规格 + 与蓝图认知配对表的对齐守护；
3. ``check_difficulty_fit`` 经 ``validate_generated_question`` 的终检规则。
"""
from app.domain.blueprint.models import BlueprintRequest  # noqa: F401  (确保蓝图模块可导入)
from app.domain.generation.batching import split_contract_into_batches
from app.domain.generation.contract import ContractSlot
from app.domain.generation.difficulty_standard import (
    COGNITIVE_LEVELS,
    DIFFICULTY_COGNITIVE_ALLOWED,
    canonical_difficulty,
    check_difficulty_fit,
    difficulty_spec,
)
from app.schemas.generation import compile_batch_generation_payload
from app.services.blueprint_service import _DIFFICULTY_COGNITIVE_WEIGHTS
from app.services.generation_service import validate_generated_question

# ---------------------------------------------------------------------------
# 词表归一
# ---------------------------------------------------------------------------


def test_canonical_difficulty_aliases_converge_to_backend_tiers():
    assert canonical_difficulty("easy") == "low"
    assert canonical_difficulty("hard") == "high"
    assert canonical_difficulty("容易") == "low"
    assert canonical_difficulty("困难") == "high"
    assert canonical_difficulty("中等") == "medium"
    assert canonical_difficulty("low") == "low"
    assert canonical_difficulty("medium") == "medium"
    assert canonical_difficulty("high") == "high"


def test_canonical_difficulty_defaults_to_medium():
    # 无法识别/缺失时按 medium——与既有终检默认行为一致，不引入新的拦截面
    assert canonical_difficulty(None) == "medium"
    assert canonical_difficulty("") == "medium"
    assert canonical_difficulty("未知词") == "medium"


# ---------------------------------------------------------------------------
# 特征规格（随任务卡下发的结构化字段）
# ---------------------------------------------------------------------------


def test_difficulty_spec_shape_per_tier():
    for tier, label in (("low", "容易"), ("medium", "中等"), ("high", "困难")):
        spec = difficulty_spec(tier)
        assert spec["tier"] == tier
        assert spec["label"] == label
        features = spec["features"]
        # D1 认知 + D2 知识跨度 + D3 推理步骤 + D4 情境 + D5 信息方式
        assert features["cognitive"] == sorted(DIFFICULTY_COGNITIVE_ALLOWED[tier])
        for key in ("knowledge_span", "reasoning_steps", "context", "information"):
            assert features.get(key)


def test_difficulty_spec_distractor_only_for_choice_types():
    assert "distractor" in difficulty_spec("low", "single_choice")["features"]
    assert "distractor" not in difficulty_spec("low", "fill_blank")["features"]


def test_difficulty_spec_accepts_frontend_vocabulary():
    # 前端词表 easy/hard 进来也要给出 low/high 的规格
    assert difficulty_spec("easy")["tier"] == "low"
    assert difficulty_spec("hard")["tier"] == "high"


def test_allowed_cognitive_levels_align_with_blueprint():
    """镜像守护：本模块的允许集必须与蓝图分配用的权重表同键集。

    蓝图按权重表分配认知层次，终检按允许集拦截——两处一旦漂移，
    校验会误伤合法槽位（或放过非法组合）。
    """
    assert set(_DIFFICULTY_COGNITIVE_WEIGHTS) == set(DIFFICULTY_COGNITIVE_ALLOWED)
    for tier, weights in _DIFFICULTY_COGNITIVE_WEIGHTS.items():
        assert set(weights) == set(DIFFICULTY_COGNITIVE_ALLOWED[tier])
        assert set(weights) <= set(COGNITIVE_LEVELS)


# ---------------------------------------------------------------------------
# 经 validate_generated_question 的终检规则（低档两规则）
# ---------------------------------------------------------------------------


def _low_question(**overrides):
    question = {
        "question_type": "single_choice",
        "difficulty": "low",
        "stem": "梯度同步会引入什么开销？",
        "options": ["通信开销", "存储开销", "计算开销", "IO开销"],
        "answer": "通信开销",
    }
    question.update(overrides)
    return question


def test_low_with_high_order_keyword_is_blocked():
    result = validate_generated_question(_low_question(stem="下列关于梯度同步的分析正确的是？"))
    assert result["status"] == "blocker"
    assert result["code"] == "difficulty_mismatch"


def test_low_keyword_present_in_examined_atom_is_exempted():
    atom = "指标比较可通过对比不同模型版本完成。"
    result = validate_generated_question(
        _low_question(stem="关于指标比较，下列说法正确的是？"), atom_text=atom
    )
    assert result["status"] == "pass"


def test_frontend_easy_vocabulary_now_enforced():
    # 历史缺口：终检只认 "low"，AI 单题创建/教师表单的 "easy" 完全绕过难度检查。
    # 词表归一后 easy 即 low，同一规则生效。
    result = validate_generated_question(_low_question(difficulty="easy", stem="请分析梯度裁剪的作用。"))
    assert result["status"] == "blocker"
    assert result["code"] == "difficulty_mismatch"


def test_low_with_cognitive_level_above_understand_is_blocked():
    # 蓝图对低档只分配 remember/understand；标着低档却要求 apply 及以上 → 拦
    result = validate_generated_question(_low_question(cognitive_level="apply"))
    assert result["status"] == "blocker"
    assert result["code"] == "difficulty_cognitive_mismatch"


def test_low_with_understand_or_missing_cognitive_passes():
    assert validate_generated_question(_low_question(cognitive_level="understand"))["status"] == "pass"
    assert validate_generated_question(_low_question(cognitive_level="remember"))["status"] == "pass"
    # 未盖认知章（如 AI 单题创建路径）不触发认知规则
    assert validate_generated_question(_low_question())["status"] == "pass"


def test_medium_and_high_are_not_subject_to_low_tier_rules():
    medium = _low_question(
        difficulty="medium", cognitive_level="analyze", stem="请分析梯度同步的优化路径。"
    )
    assert validate_generated_question(medium)["status"] == "pass"
    high = _low_question(difficulty="high", cognitive_level="create", stem="请设计一个综合优化方案。")
    assert validate_generated_question(high)["status"] == "pass"
    # 前端 hard 词表同样归 high，不受低档规则约束
    assert validate_generated_question(_low_question(difficulty="hard"))["status"] == "pass"


def test_check_difficulty_fit_returns_pass_shape_for_non_low():
    result = check_difficulty_fit({"difficulty": "medium", "stem": "题干"})
    assert result["status"] == "pass"


# ---------------------------------------------------------------------------
# 任务卡注入：payload 每题带 difficulty_spec
# ---------------------------------------------------------------------------


def test_compile_payload_carries_difficulty_spec():
    slot = ContractSlot(
        item_index=1, question_type="single_choice", score=2, difficulty="low",
        cognitive_level="remember", exam_point_id="EP1", anchor_key="A1",
        unit_id="U1", card_id="C1", coverage_atom="原子1", answer_boundary="边界1",
    )
    batch = split_contract_into_batches([slot])[0]
    payload = compile_batch_generation_payload(batch, {})
    spec = payload.questions[0].difficulty_spec
    assert spec["tier"] == "low"
    assert spec["label"] == "容易"
    assert "reasoning_steps" in spec["features"]
    # 批指令里有对 difficulty_spec 的约束说明（不是无定义的裸标签）
    assert "difficulty_spec" in payload.batch_instruction
