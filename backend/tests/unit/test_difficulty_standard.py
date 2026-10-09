"""难度标准 v2 的单元测试。

覆盖五个出口：
1. ``canonical_difficulty`` 词表归一（前端 easy/hard、中文词 → low/medium/high）；
2. ``difficulty_spec`` 三档特征规格（单主原子口径 + 按题型步骤范围 + 版本号）
   与蓝图认知配对表的对齐守护；
3. ``check_difficulty_fit`` 经 ``validate_generated_question`` 的终检规则
   （枚举严格校验 + 任务表达识别 + 位置级术语豁免 + 风险/未验证分层）；
4. 报告结构 checks/risks/unverified 的透传（pass 与 blocker 都带）；
5. ``audit_paper_against_contract`` 把难度风险/未验证项汇总进 final_check。
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
from app.services.generation_service import (
    audit_paper_against_contract,
    validate_generated_question,
)

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
    # 无法识别/缺失时按 medium——规格下发的兼容口径（严格拦截在 check_difficulty_fit 单独做）
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
        # 版本号随 payload 进生成运行记录（可追溯本题用的哪版标准）
        assert spec["standard_version"] == "difficulty-standard-v2.0"
        assert spec["source"] == "难度标准v2"
        features = spec["features"]
        # 档级任务操作定义（v2：围绕主原子，难度靠任务要求与情境变化承载）
        assert features["operation"]
        assert features["cognitive"] == sorted(DIFFICULTY_COGNITIVE_ALLOWED[tier])
        for key in ("knowledge_span", "reasoning_steps", "context", "information"):
            assert features.get(key)


def test_spec_no_longer_promise_multi_atom_spans():
    # v1 中/高档的"双原子/跨原子"与合同 coverage_atom 单主原子矛盾（改进方案 1.1）
    assert "双原子" not in difficulty_spec("medium")["features"]["knowledge_span"]
    assert "双原子" not in difficulty_spec("high")["features"]["knowledge_span"]


def test_distractor_baseline_unified_across_tiers():
    # 改进方案 1.3：三档统一底线（同质、错误项合理、不泄露线索），
    # 难度靠知识与辨析任务体现，不靠含糊或刁钻措辞
    for tier in ("low", "medium", "high"):
        text = difficulty_spec(tier, "single_choice")["features"]["distractor"]
        assert "同质" in text and "答案线索" in text
    assert "错误项合理" in difficulty_spec("low", "single_choice")["features"]["distractor"]
    assert "刁钻" in difficulty_spec("high", "single_choice")["features"]["distractor"]


def test_step_range_is_per_question_type_with_definition():
    # 改进方案 1.2：按题型给步骤范围，不再全题型统一"困难≥4步"
    objective = difficulty_spec("low", "single_choice")["features"]
    assert objective["reasoning_step_range"] == "1 步"
    assert objective["step_definition"].startswith("步 = ")
    assert difficulty_spec("medium", "fill_blank")["features"]["reasoning_step_range"] == "1~2 步"
    assert difficulty_spec("high", "short_answer")["features"]["reasoning_step_range"] == "3~4 步"
    assert difficulty_spec("high", "comprehensive")["features"]["reasoning_step_range"] == "4~5 步"
    # 无题型上下文（兼容入口）：只给语义，不硬塞范围
    assert "reasoning_step_range" not in difficulty_spec("low")["features"]


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
# 经 validate_generated_question 的终检规则
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


# ---- 枚举严格校验（存在但非法 → 阻断；缺失 → 兼容缺省）----


def test_illegal_difficulty_value_is_blocker():
    # v1 会静默回落 medium 放行；v2 明确阻断——不把未知当通过
    result = validate_generated_question(_low_question(difficulty="极端"))
    assert result["status"] == "blocker"
    assert result["code"] == "difficulty_enum_invalid"


def test_illegal_cognitive_value_is_blocker():
    result = validate_generated_question(
        _low_question(difficulty="medium", cognitive_level="analysis")
    )
    assert result["status"] == "blocker"
    assert result["code"] == "cognitive_enum_invalid"


def test_missing_enum_fields_stay_compatible():
    # 字段缺失只发生在 AI 单题创建等兼容入口 → 缺省放行，不把未知当非法
    question = {k: v for k, v in _low_question().items() if k != "cognitive_level"}
    assert validate_generated_question(question)["status"] == "pass"
    bare = {k: v for k, v in question.items() if k != "difficulty"}
    assert validate_generated_question(bare)["status"] == "pass"


# ---- 任务表达识别（高精度断言）----


def test_low_with_task_expression_is_blocker():
    for stem in (
        "请分析梯度同步的开销来源。",        # 请 + 认知动词
        "试比较两种同步策略的异同。",          # 试 + 动词 / 比较…异同
        "分析梯度同步的影响并说明原因。",      # 动词 … 并说明
        "优化梯度同步的通信开销。",            # 句首认知动词
    ):
        result = validate_generated_question(_low_question(stem=stem))
        assert result["status"] == "blocker", stem
        assert result["code"] == "difficulty_mismatch", stem
        assert result["risks"] == [], stem


def test_low_bare_keyword_is_risk_not_blocker():
    # 名词用法（"…的分析正确的是"）不是任务表达 → 风险提示，不断言难度不符
    # （改进方案 1.5：不能可靠判定的情况输出风险，交教师复核）
    result = validate_generated_question(_low_question(stem="下列关于梯度同步的分析正确的是？"))
    assert result["status"] == "pass"
    assert result["code"] == "ok"
    assert result["risks"] and any("分析" in risk for risk in result["risks"])


def test_term_exemption_is_position_scoped():
    atom = "指标比较可通过对比不同模型版本完成。"
    stem = "关于指标比较，下列说法正确的是？"
    # 术语的局部窗口与原子共现 → 位置级豁免，干净通过（无风险项）
    clean = validate_generated_question(_low_question(stem=stem), atom_text=atom)
    assert clean["status"] == "pass"
    assert clean["risks"] == []
    # 同一题干、原子不含该术语 → 未确认为术语 → 风险提示（v1 断言拦截，v2 降级）
    risky = validate_generated_question(
        _low_question(stem=stem), atom_text="模型评估的基本流程"
    )
    assert risky["status"] == "pass"
    assert risky["risks"]


def test_frontend_easy_vocabulary_now_enforced():
    # 历史缺口：终检只认 "low"，AI 单题创建/教师表单的 "easy" 完全绕过难度检查。
    # 词表归一后 easy 即 low，同一规则生效。
    result = validate_generated_question(_low_question(difficulty="easy", stem="请分析梯度裁剪的作用。"))
    assert result["status"] == "blocker"
    assert result["code"] == "difficulty_mismatch"


# ---- 低档认知层次一致性 ----


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


# ---- 报告结构（checks / risks / unverified 透传）----


def test_pass_result_carries_report_layers():
    result = validate_generated_question(_low_question(cognitive_level="understand"))
    assert result["status"] == "pass"
    codes = [c["code"] for c in result["checks"]]
    assert "difficulty_enum" in codes and "cognitive_enum" in codes and "task_expression" in codes
    assert result["risks"] == []
    # 情境陌生度/推理复杂度无法自动判定 → 恒列 unverified 交教师复核
    assert tuple(result["unverified"]) == ("context_novelty", "reasoning_depth")
    # 中档也带报告——"通过"的含义是"检查了这些、这些没验证"，不是"没报错"
    mid = validate_generated_question(_low_question(difficulty="medium"))
    assert mid["status"] == "pass" and mid["unverified"]


def test_blocker_result_carries_report_layers():
    result = validate_generated_question(_low_question(stem="请分析梯度同步的开销来源。"))
    assert result["status"] == "blocker"
    assert [c["code"] for c in result["checks"]][-1] == "task_expression"
    assert tuple(result["unverified"]) == ("context_novelty", "reasoning_depth")


# ---- 合同终检汇总：风险进 final_check（AI 整卷评审可读）----


def test_audit_final_check_surfaces_difficulty_risks():
    slot = ContractSlot(
        item_index=1, question_type="single_choice", score=2, difficulty="low",
        cognitive_level="remember", exam_point_id="EP1", anchor_key="A1",
        unit_id="U1", card_id="C1", coverage_atom="原子1", answer_boundary="边界1",
    )
    question = {
        "item_index": 1, "exam_point_id": "EP1", "unit_id": "U1", "card_id": "C1",
        "coverage_atom": "原子1", "answer_boundary": "边界1",
        "question_type": "single_choice", "difficulty": "low",
        "cognitive_level": "remember",
        # 裸高阶词（名词用法、原子不含该术语）→ 单题门禁放行但记为风险
        "stem": "关于指标比较，下列说法正确的是？",
        "answer": "甲", "needs_review": False,
    }
    report = audit_paper_against_contract([slot], [question])
    check = next(c for c in report["checks"] if c["code"] == "difficulty_consistency")
    assert check["passed"] is True
    assert check["detail"]["items_checked"] == 1
    assert check["detail"]["risks"] and check["detail"]["risks"][0]["item_index"] == 1
    assert {"context_novelty", "reasoning_depth"} <= set(check["detail"]["unverified"])


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
    assert spec["standard_version"] == "difficulty-standard-v2.0"
    assert "reasoning_steps" in spec["features"]
    assert spec["features"]["reasoning_step_range"] == "1 步"
    # 批指令里有对 difficulty_spec 的约束说明（不是无定义的裸标签）
    assert "difficulty_spec" in payload.batch_instruction
