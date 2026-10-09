"""题目难度标准 v2（特征化标准，不是 prompt 穿透）。

设计文档：docs/superpowers/specs/2026-10-07-difficulty-feature-standard-design.md（§3.2 按 v2 修订）
改进方案：docs/superpowers/plans/2026-10-08-difficulty-standard-v2-improvement-plan.md

v2 相对 v1 的变化（2026-10-10 落地 = 改进方案第一轮 + 第二轮报告结构）：
1. 三档定义统一"单主原子"口径——难度靠任务要求与情境变化提升，不强制增加考查原子
   （v1 的"双原子/跨原子"与合同 coverage_atom 单主原子矛盾，文义与执行对齐）；
2. 规格按题型给出 ``reasoning_step_range`` 并声明"什么算一步"，不再全题型统一"困难≥4步"；
3. 干扰项三档统一底线（同质、错误项合理、无答案线索），难度靠所需知识与辨析任务体现；
4. 枚举严格校验：难度/认知字段"存在但非法" → blocker；字段缺失保持兼容缺省
   （正式生成路径由合同槽位盖章保证字段恒在且合法，缺失只发生在 AI 单题创建/改题等兼容入口）；
5. 关键词升级为**任务表达识别**（"请分析""比较…异同""…并说明"等对学生的要求）→ blocker；
   术语豁免限定到"具体术语及其出现位置"（局部窗口与原子共现），不再整题全局豁免；
   不能可靠判定 → ``risks`` 风险提示，不冒充确定性断言；
6. 返回结构扩展 ``checks`` / ``risks`` / ``unverified``（向后兼容：status/code/message 不变）。
   情境陌生度与推理复杂度无法自动判定 → 明确列 ``unverified`` 交教师复核；
   报告经 ``question["quality"]`` 随题目持久化，并由 ``audit_paper_against_contract``
   汇总进 ``final_check``（AI 整卷评审与教师端可读"检查了什么、还有什么没验证"）。
"""

from __future__ import annotations

import re

# 本模块口径的版本号，随 difficulty_spec 进任务卡 payload（生成运行的 prompt 记录可追溯）。
# 合同级冻结（standard_version 落合同）属改进方案开放问题 4，待定后升级。
STANDARD_VERSION = "difficulty-standard-v2.0"

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

# 高阶认知动词表（只做**任务表达识别**的锚点，不再做"任意位置包含即拦截"）。
HIGH_ORDER_HINTS = (
    "分析", "评价", "评估", "设计", "创造", "比较", "对比",
    "综合", "判断并说明", "论证", "批判", "优化",
)

# 三档操作定义（改进方案 1.1：围绕主原子，难度靠任务要求与情境变化提升）
_TIER_OPERATION = {
    "low": "围绕主原子，直接识别、解释或执行熟悉的基础操作",
    "medium": "围绕主原子，在常规变式中进行信息转换、应用或多步求解",
    "high": "围绕主原子，在陌生情境中进行条件整合、方案选择或论证",
}

# D2~D6 的三档语义。v2 修正：中/高档不再写"双原子/跨原子"（与合同单主原子矛盾）；
# 干扰项三档统一底线（同质、错误项合理、无答案线索），难度不靠措辞刁钻制造。
_FEATURE_TEXT: dict[str, dict[str, str]] = {
    "low": {
        "knowledge_span": "单主原子，不引入其他考查知识",
        "reasoning_steps": "单步直取：直接回忆或一步识别",
        "context": "无情境或教材原情境",
        "information": "条件直接给出，无隐含前提",
        "distractor": "同质表述、错误项合理、不泄露答案线索；正确项可由主原子直接辨识",
    },
    "medium": {
        "knowledge_span": "围绕主原子；相邻知识仅作背景，不参与判分",
        "reasoning_steps": "2~3 步常规推理或简单迁移",
        "context": "常规新情境（教材情境的变式）",
        "information": "条件需加工转换后使用",
        "distractor": "同质近似、围绕同一维度、不泄露答案线索",
    },
    "high": {
        "knowledge_span": "围绕主原子，不强制跨原子/跨章节；难度由情境与任务承载",
        "reasoning_steps": "多步链式推理或开放探究",
        "context": "陌生情境或探究性任务",
        "information": "存在隐含条件，需自行挖掘",
        "distractor": "同质、错误项合理且具辨析度、不泄露答案线索；不靠含糊或刁钻措辞制造难度",
    },
}

# 按题型的预期求解步骤范围（改进方案 1.2：不搞全题型统一"困难≥4步"）。
_STEP_DEFINITION = "步 = 一次独立的提取、识别、判断、代入或同维比较；题干显式分问按问计"
_STEP_RANGES: dict[str, dict[str, str]] = {
    "objective": {"low": "1 步", "medium": "1~2 步", "high": "2~3 步"},
    "short_answer": {"low": "1~2 步", "medium": "2~3 步", "high": "3~4 步"},
    "comprehensive": {"low": "2~3 步", "medium": "3~4 步", "high": "4~5 步"},
}
_OBJECTIVE_TYPES = frozenset({"single_choice", "multiple_choice", "true_false", "fill_blank"})


def _step_family(question_type: str) -> str | None:
    if question_type in _OBJECTIVE_TYPES:
        return "objective"
    if question_type in {"short_answer", "essay"}:
        return "short_answer"
    if question_type == "comprehensive":
        return "comprehensive"
    return None


# 任务表达模式（改进方案 1.5）：高精度断言，只认"对学生提出认知要求"的形态；
# 模糊形态（名词用法等）一律走风险提示。新增词表被明确禁止——方向是识别表达而非堆词。
_DEMAND_PATTERNS: tuple[str, ...] = (
    # 请/试/需要/要求/如何/怎样 + 认知动词
    r"(?:请|试|需要|要求|如何|怎样)\s*[，,、]?\s*(?:系统|深入|分别|结合)?\s*"
    r"(?:分析|评价|评估|论证|批判|设计|创造|优化|比较|对比)",
    # 认知动词 … 并(说明|指出|阐述|论述|给出|提出|判断|解释)
    r"(?:分析|评价|评估|设计|创造|优化|论证|批判|比较|对比)[^。？!]{0,24}?"
    r"(?:并|并分别|并简要)\s*(?:说明|指出|阐述|论述|给出|提出|判断|解释)",
    # 比较/对比 … 异同/差异/优劣/区别
    r"(?:比较|对比)[^。？!]{0,40}?(?:异同|差异|优劣|区别)",
    # 认知动词紧跟"下列/以下"（分析下列…）
    r"(?:分析|评价|评估|比较|对比)\s*(?:下列|以下)",
    # 综合性要求
    r"综合(?:考虑|运用|分析)",
    # 句首认知动词
    r"^(?:分析|评价|评估|设计|创造|优化|论证|批判)",
)

# 无法自动判定的维度（改进方案 2.2 第四行）：明确交教师复核，不冒充通过含义。
UNVERIFIABLE_CODES = ("context_novelty", "reasoning_depth")

_WORD_CHAR = re.compile(r"[\w\u4e00-\u9fff]")


def canonical_difficulty(raw) -> str:
    """归一到 low/medium/high；无法识别时按 medium（兼容入口口径，规格下发用）。

    严格校验在 ``check_difficulty_fit`` 内单独做：字段**存在但非法**判 blocker，
    只有字段缺失/空白才回落本函数的缺省。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return "medium"
    if text in _DIFFICULTY_ALIASES:
        return _DIFFICULTY_ALIASES[text]
    return text if text in TIER_LABELS else "medium"


def _resolve_difficulty(raw) -> str | None:
    """严格解析：合法档位（含别名）→ tier；空/缺失 → None（兼容缺省）；非法 → 非法哨兵。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None  # 字段缺失，调用方按兼容缺省处理
    if text in _DIFFICULTY_ALIASES:
        return _DIFFICULTY_ALIASES[text]
    return text if text in TIER_LABELS else "__invalid__"


def difficulty_spec(difficulty: str, question_type: str = "") -> dict:
    """三档难度的特征规格（D1~D6 + 档级任务操作），确定性给出。

    随 BatchQuestionSpec 以结构化字段下发（payload 整体 JSON 进 prompt），
    任务卡里模型看到的是字段而不是"请你出简单点"这类散文。
    """
    tier = canonical_difficulty(difficulty)
    features: dict[str, object] = {
        "operation": _TIER_OPERATION[tier],
        "cognitive": sorted(DIFFICULTY_COGNITIVE_ALLOWED[tier]),
        **_FEATURE_TEXT[tier],
    }
    family = _step_family(question_type)
    if family is not None:
        features["reasoning_step_range"] = _STEP_RANGES[family][tier]
        features["step_definition"] = _STEP_DEFINITION
    if question_type not in {"single_choice", "multiple_choice"}:
        # D6 只对选择题有意义；主观题规格里不带干扰项字段。
        features.pop("distractor", None)
    return {
        "tier": tier,
        "label": TIER_LABELS[tier],
        "standard_version": STANDARD_VERSION,
        "features": features,
        "source": "难度标准v2",
    }


def _is_word_char(ch: str) -> bool:
    return bool(ch) and bool(_WORD_CHAR.match(ch))


def _occurrence_exempt(stem: str, start: int, end: int, atom_text: str) -> bool:
    """该词出现位置是否为**具体术语**（局部窗口与合同原子共现）——位置级豁免。

    只看词的紧邻窗口（左 1~2 字 / 右 1~2 字，邻居须为中英文或数字，标点不构成术语），
    而不是"原子里出现过这个词就豁免整题"——v1 的整题全局豁免已按改进方案 1.5 收紧。
    """
    if not atom_text:
        return False
    windows: list[str] = []
    if start >= 1 and _is_word_char(stem[start - 1]):
        windows.append(stem[start - 1:end])
        if start >= 2 and _is_word_char(stem[start - 2]):
            windows.append(stem[start - 2:end])
    if end < len(stem) and _is_word_char(stem[end]):
        windows.append(stem[start:end + 1])
        if end + 1 < len(stem) and _is_word_char(stem[end + 1]):
            windows.append(stem[start:end + 2])
    return any(window in atom_text for window in windows)


def check_difficulty_fit(question: dict, atom_text: str = "") -> dict:
    """难度一致性校验，返回带证据分层的报告。

    返回结构（向后兼容 v1：status/code/message 语义不变）::

        {
          "status": "pass" | "blocker",
          "code": ..., "message": ...,
          "checks": [{"code": ..., "result": ...}, ...],   # 实际检查了什么
          "risks": [str, ...],                             # 疑似不匹配（不拦截，交教师复核）
          "unverified": ("context_novelty", "reasoning_depth"),  # 无法自动判定的维度
        }

    规则（按证据强度分层，改进方案 2.2）：
    1. 难度/认知枚举**存在但非法** → blocker（字段缺失保持兼容缺省，不把未知当非法）；
    2. 低档 + 任务表达（请分析/比较…异同/…并说明…）→ blocker；
    3. 低档 + 认知层次超出允许集 → blocker；
    4. 低档 + 裸高阶词（非任务表达、也未在原子中确认为术语局部）→ risk，不断言；
    5. 情境陌生度/推理复杂度 → unverified，交教师复核。
    """
    checks: list[dict] = []
    risks: list[str] = []
    unverified = UNVERIFIABLE_CODES

    # ---- 1a. 难度枚举（存在但非法 → 阻断；缺失 → 兼容缺省 medium）----
    raw_difficulty = question.get("difficulty")
    resolved = _resolve_difficulty(raw_difficulty)
    if resolved == "__invalid__":
        return {
            "status": "blocker",
            "code": "difficulty_enum_invalid",
            "message": (
                f"难度标签 {str(raw_difficulty)!r} 不是合法档位"
                "（low/medium/high，或 easy/hard 与中文别名）"
            ),
            "checks": [{"code": "difficulty_enum", "result": "invalid"}],
            "risks": [],
            "unverified": unverified,
        }
    tier = resolved or "medium"
    checks.append({"code": "difficulty_enum", "result": "ok" if resolved else "absent_default_medium"})

    # ---- 1b. 认知枚举（存在但非法 → 阻断；缺失 → 跳过，兼容 AI 单题创建路径）----
    cognitive = str(question.get("cognitive_level") or "").strip()
    if cognitive and cognitive not in COGNITIVE_LEVELS:
        return {
            "status": "blocker",
            "code": "cognitive_enum_invalid",
            "message": (
                f"认知层次 {cognitive!r} 不在布鲁姆六级词表"
                "（remember/understand/apply/analyze/evaluate/create）"
            ),
            "checks": checks + [{"code": "cognitive_enum", "result": "invalid"}],
            "risks": [],
            "unverified": unverified,
        }
    checks.append({"code": "cognitive_enum", "result": "ok" if cognitive else "absent"})

    if tier != "low":
        # 中/高档：v1 起即只随规格下发、不做内容级文本判定（误伤率高），留实证校准收紧。
        return {
            "status": "pass", "code": "ok", "message": "通过难度标准检查",
            "checks": checks, "risks": risks, "unverified": unverified,
        }

    # ---- 3. 低档认知层次一致性（蓝图配对：低档只允许 remember/understand）----
    if cognitive:
        if cognitive not in DIFFICULTY_COGNITIVE_ALLOWED["low"]:
            return {
                "status": "blocker",
                "code": "difficulty_cognitive_mismatch",
                "message": (
                    f"低难度题目认知层次为 {cognitive}，"
                    "超出容易档允许的认知要求（remember/understand）"
                ),
                "checks": checks + [{"code": "cognitive_tier_fit", "result": "violated"}],
                "risks": [],
                "unverified": unverified,
            }
        checks.append({"code": "cognitive_tier_fit", "result": "ok"})
    else:
        checks.append({"code": "cognitive_tier_fit", "result": "skipped_absent"})

    # ---- 2/4. 任务表达识别 + 位置级术语豁免 + 模糊形态风险提示 ----
    stem = str(question.get("stem", "") or "")
    free_occurrences: list[tuple[str, int]] = []  # (hint, start)，未被术语豁免的出现
    for hint in HIGH_ORDER_HINTS:
        for m in re.finditer(re.escape(hint), stem):
            if _occurrence_exempt(stem, m.start(), m.end(), atom_text):
                continue
            free_occurrences.append((hint, m.start()))

    if not free_occurrences:
        checks.append({"code": "task_expression", "result": "ok"})
        return {
            "status": "pass", "code": "ok", "message": "通过难度标准检查",
            "checks": checks, "risks": risks, "unverified": unverified,
        }

    # 命中任一任务表达模式、且覆盖某个未豁免出现点 → 确定性拦截
    for pattern in _DEMAND_PATTERNS:
        for m in re.finditer(pattern, stem):
            if any(m.start() <= start < m.end() for _, start in free_occurrences):
                return {
                    "status": "blocker",
                    "code": "difficulty_mismatch",
                    "message": (
                        f"低难度题目包含任务表达「{m.group(0)}」，"
                        "与容易档（直接识别、解释或基础操作）不匹配"
                    ),
                    "checks": checks + [{"code": "task_expression", "result": "violation"}],
                    "risks": [],
                    "unverified": unverified,
                }

    # 未豁免、但也不构成任务表达（如名词用法）→ 风险提示，不断言难度不符
    for hint, _ in free_occurrences:
        risk = (
            f"题干含高阶认知词「{hint}」：未识别为任务表达，"
            "也未在合同原子中确认为该术语的具体用法，建议复核"
        )
        if risk not in risks:
            risks.append(risk)
    checks.append({"code": "task_expression", "result": "risk"})
    return {
        "status": "pass", "code": "ok", "message": "通过难度标准检查",
        "checks": checks, "risks": risks, "unverified": unverified,
    }
