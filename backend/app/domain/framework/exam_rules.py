"""考核大纲里的结构化考试规则：题型比例、章节命题权重与考试侧重点。

考纲通常写明"选择题占 20%/判断题占 20%…"以及"第1章 5%/第2章 25%…"的命题
权重表。这些是命题的硬约束，必须原样进入框架并被蓝图消费，否则出卷比例与
考纲声明脱节。本模块做三件确定性的事：

1. ``normalize_exam_rules``：把模型/教师给的自由形态规则归一成内部约定
   （题型名统一英文枚举、剔除未知项、比例归一到 100；考试侧重点与难度
   比例按同样口径归一）；
2. ``type_rules_from_ratios``：按题型比例推导蓝图的 type_rules
   （题数取整后定点修正，保证总分精确闭合）；
3. 考试侧重点 ``assessment_focus`` 与难度比例 ``difficulty_distribution``
   只声明权重偏好，由蓝图创建时确定性地折算成各题型的考查方式/槽位难度
   ——权重本身不进任何 prompt。
"""
from __future__ import annotations

import re

from app.domain.blueprint.models import ASSESSMENT_MODES
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.question_formats import QUESTION_TEMPLATES

# 模型与教师都可能用中文题型名，统一映射到内部英文枚举
QUESTION_TYPE_ALIASES: dict[str, str] = {
    "单选题": "single_choice",
    "单选": "single_choice",
    "single_choice": "single_choice",
    "singlechoice": "single_choice",
    "选择题": "single_choice",
    "多选题": "multiple_choice",
    "多选": "multiple_choice",
    "multiple_choice": "multiple_choice",
    "multiplechoice": "multiple_choice",
    "判断题": "true_false",
    "判断": "true_false",
    "true_false": "true_false",
    "truefalse": "true_false",
    "填空题": "fill_blank",
    "填空": "fill_blank",
    "fill_blank": "fill_blank",
    "fillblank": "fill_blank",
    "简答题": "short_answer",
    "简答": "short_answer",
    "short_answer": "short_answer",
    "shortanswer": "short_answer",
    "综合题": "comprehensive",
    "综合": "comprehensive",
    "comprehensive": "comprehensive",
    "论述题": "essay",
    "论述": "essay",
    "essay": "essay",
    "计算题": "calculation",
    "计算": "calculation",
    "calculation": "calculation",
}

# 蓝图默认题型分布：单题分值沿用校内常规（客观题 1-2 分、主观题 5-10 分）
DEFAULT_TYPE_RULES: dict[str, dict[str, float]] = {
    "single_choice": {"count": 15, "score": 2},
    "true_false": {"count": 10, "score": 1},
    "fill_blank": {"count": 10, "score": 2},
    "short_answer": {"count": 4, "score": 5},
    "comprehensive": {"count": 2, "score": 10},
}

# 难度比例只认三个规范档（顺序即归一后输出顺序），与蓝图引擎 _DIFFICULTY_ORDER 同源口径
_DIFFICULTY_KEYS = ("low", "medium", "high")


def canonical_question_type(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    key = text.lower().replace(" ", "").replace("-", "_")
    if key in QUESTION_TYPE_ALIASES:
        return QUESTION_TYPE_ALIASES[key]
    return QUESTION_TYPE_ALIASES.get(text.replace(" ", "").lower())


def _ratio_list(raw: object, *, anchor_keys: set[str] | None = None) -> list[tuple[str, float]]:
    """从自由形态（列表或 dict）里取出 (key, value) 对，值为非负浮点。"""
    pairs: list[tuple[str, float]] = []
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = (
                entry.get("question_type")
                or entry.get("anchor_key")
                or entry.get("key")
                or entry.get("type")
                or entry.get("assessment_mode")
            )
            value = entry.get("ratio") if "ratio" in entry else entry.get("weight")
            if key is not None:
                items.append((key, value))
    else:
        return pairs
    for key, value in items:
        if anchor_keys is not None and key not in anchor_keys:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number < 0:
            continue
        pairs.append((str(key), number))
    return pairs


def _normalize_to_100(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """按比例归一到合计 100；合计为 0 时原样返回（调用方决定是否丢弃）。"""
    total = sum(value for _, value in pairs)
    if total <= 0:
        return pairs
    if abs(total - 100) < 0.01:
        return [(key, round(value, 4)) for key, value in pairs]
    scale = 100.0 / total
    return [(key, round(value * scale, 4)) for key, value in pairs]


def _type_formats_dict(raw: object) -> dict[str, str]:
    """题型出题格式覆盖 {question_type: template}：只认已登记键，清洗留痕。

    键统一英文枚举（中文别名可入）；综合题原型 key（ARCHETYPE_CONTRACTS 的
    8 个原型名）同表登记——覆盖的是该原型的任务卡文本，分问结构与输出
    schema 恒按原型档案。值剥空白后截断到 2000 字；空值/非字符串/未知键
    直接剔除。
    """
    if not isinstance(raw, dict):
        return {}
    formats: dict[str, str] = {}
    for raw_type, template in raw.items():
        canonical = canonical_question_type(raw_type)
        if canonical not in QUESTION_TEMPLATES:
            # 裸 comprehensive 等非任务卡键不收；原型名原样作为键
            canonical = (
                raw_type
                if isinstance(raw_type, str) and raw_type in ARCHETYPE_CONTRACTS
                else None
            )
        if canonical is None or not isinstance(template, str):
            continue
        text = template.strip()
        if not text:
            continue
        formats[canonical] = text[:2000]
    return formats


def normalize_exam_rules(raw: object, *, anchor_keys: list[str] | None = None) -> dict:
    """归一化考核大纲的考试规则。

    题型比例与章节权重都会剔除未知项并归一到 100；章节权重为空时回退为各锚点
    既有考试权重（anchor 的 exam_weight 之和已经是 100），保证蓝图永远有权重可用。
    ``type_formats``（题型出题格式覆盖）同样清洗：键归英文枚举、只留已登记题型。
    """
    rules = raw if isinstance(raw, dict) else {}
    anchors = set(anchor_keys or [])

    ratios: list[tuple[str, float]] = []
    for raw_type, ratio in _ratio_list(rules.get("question_type_ratios")):
        canonical = canonical_question_type(raw_type)
        if canonical:
            ratios.append((canonical, ratio))
    ratios = _normalize_to_100(ratios)
    ratios = [(key, value) for key, value in ratios if value > 0]

    chapters: list[tuple[str, float]] = []
    for anchor_key, weight in _ratio_list(rules.get("chapter_weights"), anchor_keys=anchors or None):
        chapters.append((anchor_key, weight))
    declared = dict(chapters)
    if anchors and sum(declared.values()) > 0:
        # 考纲只声明了部分章节：未声明的锚点补 0。蓝图的章权重必须覆盖全部
        # 考核单元，缺一个锚点就会报 "chapter weights must cover every unit anchor"。
        chapters = [(key, declared.get(key, 0.0)) for key in sorted(anchors)]
    elif anchors:
        # 考纲完全没有命题权重表：返回空列表，由消费方回退到考点权重推导，
        # 好过在这里凭空均分。
        chapters = []
    chapters = _normalize_to_100(chapters)

    # 考试侧重点：教师声明的各考查方式权重偏好（归一到 100，未知方式与
    # 负值剔除，全零视为未声明）。为空时蓝图按题型默认分布出卷。
    focus: list[tuple[str, float]] = []
    for mode, weight in _ratio_list(rules.get("assessment_focus")):
        if mode in ASSESSMENT_MODES:
            focus.append((mode, weight))
    focus = _normalize_to_100(focus)
    focus = [(key, value) for key, value in focus if value > 0]

    # 难度比例：考纲/教师声明的简单/中等/困难占比（归一到 100，只认三个规范
    # 档，全零/非法视为未声明）。蓝图创建时逐题型确定性折算成槽位难度；
    # 未声明时省略键（与 type_formats 同约定），蓝图保持既有缺省——全 medium。
    difficulty: list[tuple[str, float]] = [
        (key, value)
        for key, value in _ratio_list(rules.get("difficulty_distribution"))
        if key in _DIFFICULTY_KEYS
    ]
    difficulty = _normalize_to_100(difficulty)
    difficulty = [(key, value) for key, value in difficulty if value > 0]

    duration = rules.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        duration = None
    total_score = rules.get("total_score")
    if isinstance(total_score, bool) or not isinstance(total_score, (int, float)) or total_score <= 0:
        total_score = None

    type_formats = _type_formats_dict(rules.get("type_formats"))

    result = {
        "exam_form": str(rules.get("exam_form") or "").strip(),
        "duration_minutes": int(duration) if duration is not None else None,
        "total_score": float(total_score) if total_score is not None else None,
        "question_type_ratios": [{"question_type": key, "ratio": value} for key, value in ratios],
        "chapter_weights": [{"anchor_key": key, "weight": value} for key, value in chapters],
        "assessment_focus": [
            {"assessment_mode": key, "weight": value} for key, value in focus
        ],
    }
    # 题型格式覆盖只在有值时出现（消费方一律 `or {}`），避免给全是覆盖空表的
    # 旧消费方（表单、精确断言）凭空加键。
    if type_formats:
        result["type_formats"] = type_formats
    if difficulty:
        result["difficulty_distribution"] = {
            key: value
            for key, value in sorted(difficulty, key=lambda pair: _DIFFICULTY_KEYS.index(pair[0]))
        }
    return result


def type_rules_from_ratios(
    ratios: list[dict],
    *,
    total_score: float = 100,
    default_rules: dict[str, dict[str, float]] | None = None,
    capacity: int | None = None,
) -> dict[str, dict[str, float]] | None:
    """按题型比例推导蓝图 type_rules。

    每题型先按"该题型应得分数 ÷ 默认单题分值"折算题数，再定点增删题目使总分
    精确等于 total_score（蓝图引擎强约束）。无法闭合时返回 None，由调用方回退
    到默认分布——宁可按默认出卷，也不要产出总分不对的蓝图。
    ``capacity``（知识卡池实际可出的题位数）小于推导题数时按题型分值份额等比
    缩容：总分与各题型分值占比不变、单题分值上调；为 None 时行为与不传一致。
    """
    table = default_rules or DEFAULT_TYPE_RULES
    wanted: dict[str, float] = {}
    for entry in ratios or []:
        if not isinstance(entry, dict):
            continue
        canonical = canonical_question_type(entry.get("question_type"))
        ratio = entry.get("ratio")
        if not canonical or canonical not in table or isinstance(ratio, bool):
            continue
        if not isinstance(ratio, (int, float)) or ratio <= 0:
            continue
        wanted[canonical] = float(ratio)
    if not wanted:
        return None

    target = float(total_score)
    counts: dict[str, int] = {}
    for qt, ratio in wanted.items():
        per = float(table[qt]["score"])
        counts[qt] = max(1, int(round(ratio / 100 * target / per)))

    def points(current: dict[str, int]) -> float:
        return sum(current[qt] * float(table[qt]["score"]) for qt in current)

    for _ in range(500):
        diff = round(target - points(counts), 6)
        if diff == 0:
            break
        if diff > 0:
            # 补一道题：首选亏欠最多且分值不超缺口的题型
            affordable = [qt for qt in counts if float(table[qt]["score"]) <= diff]
            if affordable:
                pick = max(affordable, key=lambda qt: wanted[qt] / 100 * target - counts[qt] * float(table[qt]["score"]))
            else:
                pick = min(counts, key=lambda qt: float(table[qt]["score"]))
            counts[pick] += 1
        else:
            # 减一道题：优先减超配最多、且减完仍有余量的题型
            removable = [qt for qt in counts if counts[qt] > 1]
            if not removable:
                return None
            pick = max(removable, key=lambda qt: counts[qt] * float(table[qt]["score"]) - wanted[qt] / 100 * target)
            counts[pick] -= 1
    else:  # pragma: no cover - 理论上不会走到
        return None

    if abs(points(counts) - target) > 0.01:
        return None
    if capacity is not None and capacity > 0 and sum(counts.values()) > capacity:
        return _shrink_counts_to_capacity(counts, table, capacity)
    return {
        qt: {"count": float(counts[qt]), "score": float(table[qt]["score"])}
        for qt in counts
    }


def _shrink_counts_to_capacity(
    counts: dict[str, int],
    table: dict[str, dict[str, float]],
    capacity: int,
) -> dict[str, dict[str, float]]:
    """题位数超容量时按题型分值份额等比缩容：分值上调、总分不变。

    每题型的分数预算 S = 题数 × 单题分值 固定不动——总分与各题型分值占比
    因此完全保持；题数按 capacity/原题数 的比例缩减，并吸附到"能整除 2·S"
    的最近合法题数（单题分值 S/c 必是 0.5 的倍数、c ≤ 原题数即分值只升不
    降）。知识卡池小于蓝图题位数时，合同分配只能给部分题位领到原子、其余
    题位被静默丢弃（排在各章末尾的综合题先全灭：2026-10 实测 42 题位 →
    29 题、综合 0 道），缩容让卷面题型比例不失真。同输入同输出、无随机。
    """
    total = sum(counts.values())
    factor = capacity / total if total else 0.0
    budget: dict[str, float] = {
        qt: n * float(table[qt]["score"]) for qt, n in counts.items()
    }
    shrunk: dict[str, int] = {}
    for qt, n in counts.items():
        # 2·S 是整数（默认单题分值都是 0.5 的倍数），整除即保证 S/c 是 0.5 倍数
        double = max(1, int(round(budget[qt] * 2)))
        ideal = max(1, min(n, int(round(n * factor))))
        shrunk[qt] = min(
            (c for c in range(1, n + 1) if double % c == 0),
            key=lambda c: (abs(c - ideal), -c),  # 最近合法值；并列时多留题数
        )
    # 逐题型独立取最近值，合计可能仍超容量（进位误差）：每次把某题型降到
    # 次大合法题数，选"新合计离容量最近、降得最少"的走法，确定性。
    for _ in range(500):
        if sum(shrunk.values()) <= capacity:
            break
        best_key: tuple[int, int, str] | None = None
        best_move: tuple[str, int] | None = None
        for qt, c in shrunk.items():
            if c <= 1:
                continue
            double = max(1, int(round(budget[qt] * 2)))
            smaller = next((k for k in range(c - 1, 0, -1) if double % k == 0), None)
            if smaller is None:
                continue
            new_total = sum(shrunk.values()) - c + smaller
            key = (abs(new_total - capacity), -new_total, qt)
            if best_key is None or key < best_key:
                best_key, best_move = key, (qt, smaller)
        if best_move is None:
            break  # 已无可再减的题数：交回既有合同冲突上报兜底
        shrunk[best_move[0]] = best_move[1]
    return {
        qt: {"count": float(shrunk[qt]), "score": round(budget[qt] / shrunk[qt], 4)}
        for qt in counts
    }


def rules_have_type_ratios(rules: dict) -> bool:
    """规则里是否有可用的题型比例。"""
    return bool(isinstance(rules, dict) and rules.get("question_type_ratios"))
