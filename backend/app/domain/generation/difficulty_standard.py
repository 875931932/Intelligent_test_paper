"""题目难度标准 v1（特征化标准，不是 prompt 穿透）。

设计文档：docs/superpowers/specs/2026-10-07-difficulty-feature-standard-design.md

核心思路：难度 = 可观测特征的确定性组合，判定权在代码，不靠模型自觉。

调研来源（特征角度与三档语义）：
- 布鲁姆认知层次（D1）：蓝图 ``_DIFFICULTY_COGNITIVE_WEIGHTS`` 已给出难度↔认知
  配对，本模块镜像其允许集，由测试守护两处一致（不改蓝图分配逻辑）；
- 鲍建生五因素综合难度模型 / 宁波 9 因素绝对难度模型 / 江苏省教育考试院难度
  分析模型 → D2~D6 特征维度（知识跨度、推理步骤、情境、信息方式、干扰项）；
- 命题实践"容易题/中档题/难题"操作定义 → 三档语义（基础单步 / 综合多步 / 综合探究）；
- 实证层（CTT 难度系数 P 值）由学生/教师问卷回收校准，是本设计闭环的第 4 段，
  见作品方案问卷章节。

本模块的两个出口：
1. ``difficulty_spec`` —— 三档特征规格，作为结构化字段随任务卡下发给模型；
2. ``check_difficulty_fit`` —— 低档题的确定性终检（高阶关键词 + 认知层次一致性），
   由 ``validate_generated_question`` 调用。低档是唯一做内容级拦截的档位：
   蓝图对低档的认知配对是硬约束（remember/understand），规则可证明不误伤合法槽位；
   中/高档的内容特征（步骤数、情境）v1 只随规格下发，不做文本判定，留待实证校准。
"""

from __future__ import annotations

TIER_LABELS = {"low": "容易", "medium": "中等", "high": "困难"}

# 三档别名 → 后端规范值（蓝图 _DIFFICULTY_ORDER 同口径：low/medium/high）。
# easy/hard 是前端表单与 AI 单题创建的词表，容易/困难是中文展示词，全部收敛。
_DIFFICULTY_ALIASES = {
    "easy": "low", "low": "low", "容易": "low", "易": "low", "简单": "low", "低": "low",
    "medium": "medium", "中等": "medium", "中": "medium",
    "hard": "high", "high": "high", "困难": "high", "难": "high", "高": "high",
}

# 布鲁姆六级（与 blueprint_service / blueprint_suggest_service 同词表）
COGNITIVE_LEVELS = ("remember", "understand", "apply", "analyze", "evaluate", "create")

# 难度 ↔ 认知层次允许集。键集与 blueprint_service._DIFFICULTY_COGNITIVE_WEIGHTS
# 一致，由 tests/unit/test_difficulty_standard.py 的对齐测试护住——蓝图怎么分配，
# 校验就认什么，规则不会与合法槽位冲突。
DIFFICULTY_COGNITIVE_ALLOWED: dict[str, frozenset[str]] = {
    "low": frozenset({"remember", "understand"}),
    "medium": frozenset({"understand", "apply", "analyze"}),
    "high": frozenset({"apply", "analyze", "evaluate", "create"}),
}

# 低档禁用的高阶认知要求关键词。豁免规则：关键词同时出现在合同原子原文中时，
# 它是被考查的术语本身（如原子"指标比较可通过……完成"中的"比较"），不是对
# 学生的认知要求——与历史终检口径完全一致。
HIGH_ORDER_HINTS = (
    "分析", "评价", "评估", "设计", "创造", "比较", "对比",
    "综合", "判断并说明", "论证", "批判", "优化",
)

# D2~D6 的三档语义（命题实践操作定义 + 调研模型的因素水平）。
_FEATURE_TEXT: dict[str, dict[str, str]] = {
    "low": {
        "knowledge_span": "单知识原子，不跨原子/跨章节",
        "reasoning_steps": "单步直取：直接回忆或一步识别",
        "context": "无情境或教材原情境",
        "information": "条件直接给出，无隐含前提",
        "distractor": "干扰项与正确项明显异类",
    },
    "medium": {
        "knowledge_span": "双原子或同一概念的相邻知识",
        "reasoning_steps": "2~3 步常规推理或简单迁移",
        "context": "常规新情境（教材情境的变式）",
        "information": "条件需加工转换后使用",
        "distractor": "干扰项同质近似，围绕同一维度",
    },
    "high": {
        "knowledge_span": "跨原子/跨章节多知识综合",
        "reasoning_steps": "≥4 步链式推理或开放探究",
        "context": "陌生情境或探究性任务",
        "information": "存在隐含条件，需自行挖掘",
        "distractor": "干扰项可两两混淆，辨析度低",
    },
}


def canonical_difficulty(raw) -> str:
    """归一到 low/medium/high；无法识别时按 medium（与既有终检默认一致）。"""
    text = str(raw or "").strip().lower()
    if not text:
        return "medium"
    if text in _DIFFICULTY_ALIASES:
        return _DIFFICULTY_ALIASES[text]
    return text if text in TIER_LABELS else "medium"


def difficulty_spec(difficulty: str, question_type: str = "") -> dict:
    """三档难度的特征规格（D1~D6），确定性给出。

    随 BatchQuestionSpec 以结构化字段下发（payload 整体 JSON 进 prompt），
    任务卡里模型看到的是字段而不是"请你出简单点"这类散文。
    """
    tier = canonical_difficulty(difficulty)
    features: dict[str, object] = {
        "cognitive": sorted(DIFFICULTY_COGNITIVE_ALLOWED[tier]),
        **_FEATURE_TEXT[tier],
    }
    if question_type in {"single_choice", "multiple_choice"}:
        # D6 只对选择题有意义；主观题规格里不带干扰项字段。
        features["distractor"] = _FEATURE_TEXT[tier]["distractor"]
    else:
        features.pop("distractor", None)
    return {
        "tier": tier,
        "label": TIER_LABELS[tier],
        "features": features,
        "source": "难度标准v1",
    }


def check_difficulty_fit(question: dict, atom_text: str = "") -> dict:
    """低档题的确定性难度一致性校验。

    规则（仅低档，方向只此一条，可证明不误伤蓝图合法槽位）：
    1. 题干含高阶认知要求关键词 → blocker（原子术语豁免）；
    2. 认知层次超出低档允许集（remember/understand）→ blocker。

    返回 {"status": "pass"|"blocker", "code", "message"}。
    """
    tier = canonical_difficulty(question.get("difficulty"))
    if tier != "low":
        return {"status": "pass", "code": "ok", "message": "通过难度标准检查"}

    stem = str(question.get("stem", "") or "")
    for hint in HIGH_ORDER_HINTS:
        if hint in stem and hint not in atom_text:
            return {
                "status": "blocker",
                "code": "difficulty_mismatch",
                "message": "低难度题目题干包含高级认知要求关键词，与指定难度不匹配",
            }

    cognitive = str(question.get("cognitive_level") or "").strip()
    if cognitive in COGNITIVE_LEVELS and cognitive not in DIFFICULTY_COGNITIVE_ALLOWED["low"]:
        return {
            "status": "blocker",
            "code": "difficulty_cognitive_mismatch",
            "message": (
                f"低难度题目认知层次为 {cognitive}，"
                "超出容易档允许的认知要求（remember/understand）"
            ),
        }
    return {"status": "pass", "code": "ok", "message": "通过难度标准检查"}
