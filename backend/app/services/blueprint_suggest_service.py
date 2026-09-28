"""蓝图题位 AI 调整建议：逐题提案 → 教师确认 → 既有 PATCH plan-items 落库。

链路：POST .../{project_id}/blueprints/current/ai-suggest 建 task_runs →
worker 调 ``execute_suggest_task`` 把确定性上下文（题位全量、题型分值差异
对照、难度/认知/章节分布、当前考核规则期望分值）交给模型产出逐题调整建议 →
``normalize``/``validate`` 确定性收口（题号在册、字段在词表、值域合法、调分
成对且全卷总分不变、丢弃无操作建议）→ 前端展示建议清单，教师逐条/批量点
「应用」走既有 PATCH plan-items 端点（0.5 步进、draft 冻结、course 隔离等
服务端校验原样生效）。

红线：本模块只写 task_runs——建议永远不直接改题位；题型分值差异/难度分布
对照由后端确定性算好作为「事实数据」给模型解读，不把比例约束检查塞进 prompt
让模型自己算。调分必须成对（增减合计 0）由代码校验，不靠模型自觉；教师指令
里的难度比例（如「按5简单3中等2难」）也由本模块确定性换算成目标分布与缺口，
「整卷清单必须给全、应用后恰好达标」同样由代码门禁判定，不靠模型自觉。

结构镜像 paper_review_service 的既定套路（上下文装配 / prompt 纯函数 / 归一 /
校验收口 / 带反馈纠错一次 / 幂等入队 / worker 入口）。
"""

from __future__ import annotations

import hashlib
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import blueprint_versions, exam_projects, task_runs
from app.domain.framework.exam_rules import canonical_question_type
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
# 复用既有判定与错误基类来源（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
from app.services.blueprint_persistence_service import list_plan_items
from app.services.framework_service import (
    FrameworkNotFoundError,
    allowed_question_types,
    get_current_framework,
)

TASK_TYPE = "suggest_blueprint_adjustments"
_INPUT_VERSION = "blueprint_suggest_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

# 建议可动的字段 = 既有 PATCH plan-items 端点的 allowed（assessment_mode 不在
# 其列：分配时定死，生成后不可原地改）
SUGGESTED_FIELDS = (
    "question_type",
    "difficulty",
    "cognitive_level",
    "score",
    "exam_point_id",
    "card_id",
)

# 蓝图题位难度词表（blueprint_service 值域 low/medium/high；模型惯用 easy/hard
# 别名，归一时映射过来——与前端 PLAN_DIFFICULTY_OPTIONS 同值域）
_DIFFICULTY_VOCAB = ("low", "medium", "high")
_DIFFICULTY_ALIASES = {
    "easy": "low", "易": "low", "偏低": "low",
    "hard": "high", "难": "high", "偏难": "high",
    "中": "medium", "中等": "medium",
}

# 教师指令里的难度比例写法：「按5简单3中等2难」「50%简单30%中等20%难」与
# 「5:3:2」（冒号式要求比例紧邻难度语境，避免把章节比例误当难度）
_RATIO_WORDS = re.compile(
    r"(\d+)[\s%％]*(?:简单|容易|易)"
    r"[\s\S]{0,6}?"
    r"(\d+)[\s%％]*(?:中等|中难度|中)"
    r"[\s\S]{0,6}?"
    r"(\d+)[\s%％]*(?:困难|高难度|难)"
)
_RATIO_COLON = re.compile(
    r"(\d+)[\s%％]*[::][\s%％]*(\d+)[\s%％]*[::][\s%％]*(\d+)"
)
_RATIO_HINT = re.compile(r"难度|难题|简单|中等|容易")

# 「每个题型/各题型/按题型」= 比例要**逐题型**分别达标（仅在难度比例已解析出时
# 生效，_difficulty_target 据此切换粒度）——否则维持整卷换算。教师写「每个题型」
# 却被整卷凑平糊弄（全卷恰好 5:3:2、各题型全错）是实测反馈过的缺口。
_TYPE_SCOPE = re.compile(
    r"每个题型|各题型|每种题型|每类题型|每个类型|各类型|按题型|逐题型|分题型|题型分别"
)

# 认知层级词表（与蓝图引擎同值域）
_COGNITIVE_LEVELS = ("remember", "understand", "apply", "analyze", "evaluate", "create")

# 建议字段 → 当前题位上的字段名（card_id 落库映射 knowledge_card_id）
_CURRENT_FIELD = {
    "card_id": "knowledge_card_id",
}

_MIN_SUMMARY_LEN = 10

_SYSTEM_PROMPT = """你是高校命题教师的试卷蓝图题位调整助手。教师已经生成了一份蓝图（题位清单 + 后端算好的确定性统计），你逐题给出调整建议。建议会展示给教师逐条确认，教师点「应用」后由既有接口改题位——你只提案，不直接改动，也无权改动。

硬规则：
1. suggestions 的 item_index 只能取 payload.items 里出现过的题号（整数）；field 只能取 payload.suggested_fields 里的六个字段；同题位同字段只给一条。
2. value 值域：difficulty ∈ payload.vocab.difficulty（low/medium/high）；cognitive_level ∈ payload.vocab.cognitive_level；question_type ∈ payload.allowed_question_types（英文枚举）；score 是 0.5 步进的正数；exam_point_id/card_id 只能取 payload.known_exam_point_ids / payload.known_card_ids。
3. 所有 score 类建议的分值增减合计必须为 0（全卷总分不变）；没有成对把握就不要建议调分。
4. 值与题位当前值相同的建议不要给（无操作建议会被丢弃）；cognitive_level 要与题型命题常识相符（客观题一般不建议 analyze/evaluate/create）。
5. payload.type_diff / difficulty_dist / cognitive_dist / chapter_dist 是后端算好的事实，直接引用，不要自己重算比例。
6. 每条建议给 reason（一两句，说明依据哪项统计、为什么改）；summary 是两三句面向教师的总评。蓝图无需调整时返回空 suggestions 并在 summary 里说明。
7. payload.difficulty_target 非空时：那是后端按教师指令换算好的难度目标——target_counts 是各档应有的题数，gap 是还差的题数（正数=还要调入该档几道，负数=还要调出几道）。scope=="question_type" 表示教师要求「每个题型」分别达标：by_type 给出每个题型自己的 count/target_counts/current_counts/gap，必须让**每个题型各自**恰好达标（顶层 target_counts 是各题型之和，全卷同时自然满足），此时只准调 difficulty、不准改 question_type。无论哪种 scope，都必须给出**完整清单**：按缺口把每一道需要动的题位全部列出，直到你的全部 difficulty 建议套用后目标恰好达成；只给几条示范、或回复「无需调整」，都会因未达标被打回重试。
8. 教师 instruction 是针对整卷的整体性要求（比例/分布/全卷统一标准）时，suggestions 必须覆盖该要求涉及的全部需调整题位，禁止只给代表性样本。

只返回严格 JSON 对象：
{"summary": "两三句总评", "suggestions": [{"item_index": 5, "field": "difficulty", "value": "high", "reason": "..."}]}
summary 与 reason 面向教师可读，只随任务结果返回，不进入任何落库载荷。"""


class BlueprintSuggestError(Exception):
    """蓝图题位调整建议业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def _resolve_blueprint_version(
    session: Session, *, course_id: str, project_id: str, require_draft: bool = True
) -> dict:
    """定位蓝图版本并做状态门禁（镜像 router 的 active → 最新 fallback 解析）。

    草稿才可调整：已确认蓝图受冻结纪律保护，PATCH 会 409，入队时就提前拒绝
    （错误文案与 PATCH 的"不可原地修改"同词，API 层可映射同一状态码）。
    ``require_draft=False`` 供只读路径（读回最近一次建议）复用同一套解析而不吃
    草稿门禁——读历史记录不该因蓝图已确认就报错。
    """
    proj = session.execute(
        select(exam_projects.c.active_blueprint_version_id).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).one_or_none()
    if proj is None:
        raise BlueprintSuggestError("试卷项目不存在")
    bv_id = proj._mapping["active_blueprint_version_id"]
    if not bv_id:
        latest = session.execute(
            select(blueprint_versions.c.id)
            .where(
                blueprint_versions.c.exam_project_id == project_id,
                blueprint_versions.c.course_id == course_id,
            )
            .order_by(blueprint_versions.c.version_no.desc())
            .limit(1)
        ).one_or_none()
        if latest is None:
            raise BlueprintSuggestError("尚未生成蓝图，无法发起题位调整建议")
        bv_id = latest._mapping["id"]

    bv = session.execute(
        select(blueprint_versions).where(
            blueprint_versions.c.id == bv_id,
            blueprint_versions.c.course_id == course_id,
            blueprint_versions.c.exam_project_id == project_id,
        )
    ).mappings().one_or_none()
    if bv is None:
        raise BlueprintSuggestError("蓝图版本不存在")
    if require_draft and bv["status"] != "draft":
        raise BlueprintSuggestError(
            f"蓝图版本 status={bv['status']}，不可原地修改；请创建新版蓝图后再调整"
        )
    return dict(bv)


def _type_diff(items: list[dict], ratios: list[dict], total: float) -> list[dict]:
    """题型分值：期望（按考核规则比例×卷面总分）vs 实际——后端算好的事实。"""
    actual: dict[str, float] = {}
    for item in items:
        key = str(item.get("question_type") or "")
        actual[key] = actual.get(key, 0.0) + float(item.get("score") or 0)

    types: list[str] = []
    for ratio in ratios:
        key = str(ratio.get("question_type") or "")
        if key and key not in types:
            types.append(key)
    for key in sorted(actual):
        if key not in types:
            types.append(key)

    out: list[dict] = []
    for key in types:
        ratio = next(
            (r for r in ratios if str(r.get("question_type") or "") == key), None
        )
        expected = (
            round(float(ratio.get("ratio") or 0) / 100 * total, 2) if ratio else 0.0
        )
        got = round(actual.get(key, 0.0), 2)
        out.append(
            {
                "question_type": key,
                "expected": expected,
                "actual": got,
                "delta": round(got - expected, 2),
            }
        )
    return out


def _distribution(items: list[dict], field: str, key_name: str, order: tuple[str, ...]) -> list[dict]:
    """按词表顺序统计题数与分值（词表外的值排最后，如实呈现）。"""
    counts: dict[str, int] = {}
    scores: dict[str, float] = {}
    for item in items:
        key = str(item.get(field) or "unknown")
        counts[key] = counts.get(key, 0) + 1
        scores[key] = scores.get(key, 0.0) + float(item.get("score") or 0)
    keys = [k for k in order if k in counts] + sorted(k for k in counts if k not in order)
    return [
        {
            key_name: key,
            "count": counts[key],
            "score": round(scores[key], 2),
        }
        for key in keys
    ]


def _chapter_dist(items: list[dict], declared: dict, total: float) -> list[dict]:
    """章节：考纲声明占比 vs 蓝图实际分值占比（同为百分比口径才可比）。"""
    actual: dict[str, float] = {}
    for item in items:
        key = str(item.get("anchor_key") or "")
        if not key:
            continue
        actual[key] = actual.get(key, 0.0) + float(item.get("score") or 0)
    keys = [str(k) for k in declared] + sorted(k for k in actual if k not in declared)
    out: list[dict] = []
    for key in keys:
        expected_pct = round(float(declared.get(key, 0) or 0), 2)
        got = actual.get(key, 0.0)
        actual_pct = round(got / total * 100, 2) if total > 0 else 0.0
        out.append(
            {
                "anchor_key": key,
                "expected_pct": expected_pct,
                "actual_pct": actual_pct,
                "delta_pct": round(actual_pct - expected_pct, 2),
            }
        )
    return out


def _max_remainder_counts(n: int, weights: tuple[int, int, int]) -> dict[str, int]:
    """n 道题按 weights 最大余数法配平成 low/medium/high 三档题数。"""
    exact = [n * w / sum(weights) for w in weights]
    base = [int(x) for x in exact]
    for i in sorted(
        range(3), key=lambda i: (-(exact[i] - base[i]), i)
    )[: n - sum(base)]:
        base[i] += 1
    return dict(zip(_DIFFICULTY_VOCAB, base))


def _difficulty_target(instruction: str, items: list[dict]) -> dict | None:
    """教师指令的难度比例 → 目标分布（确定性换算，解析不到返回 None）。

    支持「难度按5简单3中等2难」「50%简单30%中等20%难」「难度分布按5:3:2」等
    写法；目标题数按题位数 × 比例取整、最大余数法配平。比例换算在代码里做
    ——payload 只带算好的 target_counts/gap 作事实，模型照缺口点题位，达标与否
    由 validate_suggestions 门禁判定，不进 prompt 让模型自己算比例。

    指令带「每个题型/各题型/按题型」时粒度切到逐题型（scope=question_type）：
    by_type 给每个题型各自的 target/gap；顶层 target_counts 取**各题型之和**而非
    整卷再配平一次，保证全卷与逐题型两层目标数学一致、门禁不互相打架。
    """
    text = str(instruction or "")
    weights: tuple[int, int, int] | None = None
    match = _RATIO_COLON.search(text)
    if match and _RATIO_HINT.search(
        text[max(0, match.start() - 8) : match.end() + 8]
    ):
        weights = tuple(int(match.group(i)) for i in (1, 2, 3))
    if weights is None:
        match = _RATIO_WORDS.search(text)
        if match:
            weights = tuple(int(match.group(i)) for i in (1, 2, 3))
    if weights is None or sum(weights) <= 0:
        return None
    # 难度字段有词表外的值（历史脏数据）时不做达标门禁——宁可不判也不误杀
    if not items or any(
        str(item.get("difficulty") or "") not in _DIFFICULTY_VOCAB
        for item in items
    ):
        return None

    def _current(group: list[dict]) -> dict[str, int]:
        counts = {k: 0 for k in _DIFFICULTY_VOCAB}
        for item in group:
            counts[str(item["difficulty"])] += 1
        return counts

    if _TYPE_SCOPE.search(text):
        groups: dict[str, list[dict]] = {}
        for item in items:
            groups.setdefault(str(item.get("question_type") or ""), []).append(item)
        by_type: dict[str, dict] = {}
        for qtype, group in groups.items():
            target_counts = _max_remainder_counts(len(group), weights)
            current_counts = _current(group)
            by_type[qtype] = {
                "count": len(group),
                "target_counts": target_counts,
                "current_counts": current_counts,
                # 正数 = 该题型还需调入该档几道，负数 = 还需调出几道
                "gap": {
                    k: target_counts[k] - current_counts[k]
                    for k in _DIFFICULTY_VOCAB
                },
            }
        target_counts = {
            k: sum(entry["target_counts"][k] for entry in by_type.values())
            for k in _DIFFICULTY_VOCAB
        }
        current_counts = _current(items)
        return {
            "ratio": list(weights),
            "scope": "question_type",
            "target_counts": target_counts,
            "current_counts": current_counts,
            "gap": {
                k: target_counts[k] - current_counts[k] for k in _DIFFICULTY_VOCAB
            },
            "by_type": by_type,
        }

    target_counts = _max_remainder_counts(len(items), weights)
    current_counts = _current(items)
    return {
        "ratio": list(weights),
        "scope": "paper",
        "target_counts": target_counts,
        "current_counts": current_counts,
        # 正数 = 还需调入该档几道，负数 = 还需调出几道
        "gap": {
            k: target_counts[k] - current_counts[k] for k in _DIFFICULTY_VOCAB
        },
    }


def _slim_item(item: dict) -> dict:
    """题位瘦身入 prompt：只给可读字段，不给任务/库内部无关字段。"""
    return {
        "item_index": item.get("item_index"),
        "question_type": item.get("question_type"),
        "score": item.get("score"),
        "difficulty": item.get("difficulty"),
        "cognitive_level": item.get("cognitive_level"),
        "assessment_mode": item.get("assessment_mode"),
        "exam_point_id": item.get("exam_point_id"),
        "exam_point": item.get("exam_point_title") or item.get("exam_point_id"),
        "anchor_key": item.get("anchor_key"),
        "knowledge_card_id": item.get("knowledge_card_id"),
        "knowledge_card": item.get("knowledge_card_name"),
    }


def load_suggest_context(session: Session, *, course_id: str, project_id: str) -> dict:
    """装配建议上下文：题位全量 + 确定性统计对照 + 当前考核规则 + 词表。

    项目/蓝图不存在、蓝图非草稿直接拒绝（API 层映射 404/409/422）；所有查询
    带 course_id 过滤。题型分值差异等对照由本函数确定性算出，作为事实数据随
    payload 进 prompt——模型只负责解读与建议，不负责算比例。
    """
    bv = _resolve_blueprint_version(
        session, course_id=course_id, project_id=project_id
    )
    items = list_plan_items(
        session, blueprint_version_id=str(bv["id"]), course_id=course_id
    )
    if not items:
        raise BlueprintSuggestError("蓝图没有题位，无法给出调整建议")

    # 当前考核规则（框架被拒/缺失时如实置空——蓝图仍可建议，只是无期望对照）
    rules: dict | None
    try:
        current = get_current_framework(session, course_id=course_id)
        rules = current.get("exam_rules") or None
    except FrameworkNotFoundError:
        rules = None
    ratios = (rules or {}).get("question_type_ratios") or []

    total = round(sum(float(i.get("score") or 0) for i in items), 2)
    chapter_weights = bv.get("chapter_weights") if isinstance(bv.get("chapter_weights"), dict) else {}

    indices = sorted(int(i["item_index"]) for i in items if i.get("item_index") is not None)
    return {
        "course_id": course_id,
        "project_id": project_id,
        "instruction": "",
        "items": [_slim_item(i) for i in items],
        "current_by_index": {
            int(i["item_index"]): i for i in items if i.get("item_index") is not None
        },
        "known_indices": indices,
        "total_score": total,
        "rules": rules,
        "type_diff": _type_diff(items, ratios, total),
        "difficulty_dist": _distribution(items, "difficulty", "difficulty", _DIFFICULTY_VOCAB),
        "cognitive_dist": _distribution(items, "cognitive_level", "cognitive_level", _COGNITIVE_LEVELS),
        "chapter_dist": _chapter_dist(items, chapter_weights, total),
        "allowed_question_types": allowed_question_types(
            session,
            course_id=course_id,
            framework_version_id=str(bv.get("framework_version_id") or ""),
        ),
        "known_exam_point_ids": sorted(
            {str(i["exam_point_id"]) for i in items if i.get("exam_point_id")}
        ),
        "known_card_ids": sorted(
            {str(i["knowledge_card_id"]) for i in items if i.get("knowledge_card_id")}
        ),
    }


def build_suggest_prompt(
    context: dict, instruction: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言真实数据进了 prompt。"""
    payload: dict = {
        "instruction": instruction,
        "total_score": context.get("total_score"),
        "items": context.get("items") or [],
        "rules": context.get("rules") or {},
        "type_diff": context.get("type_diff") or [],
        "difficulty_dist": context.get("difficulty_dist") or [],
        "cognitive_dist": context.get("cognitive_dist") or [],
        "chapter_dist": context.get("chapter_dist") or [],
        "suggested_fields": list(SUGGESTED_FIELDS),
        "vocab": {
            "difficulty": list(_DIFFICULTY_VOCAB),
            "cognitive_level": list(_COGNITIVE_LEVELS),
        },
        "allowed_question_types": context.get("allowed_question_types") or [],
        "known_exam_point_ids": context.get("known_exam_point_ids") or [],
        "known_card_ids": context.get("known_card_ids") or [],
        # 教师指令的难度目标（后端换算，可空）：给模型照 gap 点题位，达标判定在代码
        "difficulty_target": context.get("difficulty_target"),
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 建议归一与校验（确定性收口）
# ---------------------------------------------------------------------------


def _coerce_value(field: str, value, context: dict):
    """把模型给的 value 收敛到字段词表；不合法返回 None（丢弃该条建议）。"""
    if field == "question_type":
        return canonical_question_type(value)
    if field == "difficulty":
        key = str(value or "").strip().lower()
        if key in _DIFFICULTY_ALIASES:
            return _DIFFICULTY_ALIASES[key]
        return key if key in _DIFFICULTY_VOCAB else None
    if field == "cognitive_level":
        key = str(value or "").strip().lower()
        return key if key in _COGNITIVE_LEVELS else None
    if field == "score":
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        if score <= 0 or abs(score * 2 - round(score * 2)) > 0.001:
            return None
        return round(score, 2)
    if field == "exam_point_id":
        key = str(value or "").strip()
        return key if key in context["known_exam_point_ids"] else None
    if field == "card_id":
        key = str(value or "").strip()
        return key if key in context["known_card_ids"] else None
    return None


def _is_noop(item: dict, field: str, value) -> bool:
    """建议值与题位当前值相同即无操作——丢弃，不给教师看空建议。"""
    current = item.get(_CURRENT_FIELD.get(field, field))
    if field == "score":
        try:
            return abs(float(current or 0) - float(value)) < 0.001
        except (TypeError, ValueError):
            return False
    return str(current or "") == str(value or "")


def normalize_suggestions(raw, context: dict) -> dict:
    """把模型返回收敛为可信的建议清单：题号在册、字段在词表、值域合法、
    去重、丢弃无操作建议；summary 取原样字符串。每条附 from_value（提案时
    原值快照）——教师应用后题位已是新值，面板仍能显示「原值 → 新值」。"""
    if not isinstance(raw, dict):
        raise BlueprintSuggestError("模型未返回 JSON 对象")

    summary = str(raw.get("summary") or "").strip()
    entries = raw.get("suggestions")
    entries = entries if isinstance(entries, list) else []

    known = context["current_by_index"]
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("item_index")
        if isinstance(idx, bool):
            continue
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if idx not in known:
            continue
        field = str(entry.get("field") or "").strip()
        if field not in SUGGESTED_FIELDS:
            continue
        value = _coerce_value(field, entry.get("value"), context)
        if value is None:
            continue
        if _is_noop(known[idx], field, value):
            continue
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            continue
        if (idx, field) in seen:
            continue
        seen.add((idx, field))
        current = known[idx]
        from_value = (
            round(float(current.get("score") or 0), 2)
            if field == "score"
            else str(current.get(_CURRENT_FIELD.get(field, field)) or "")
        )
        out.append(
            {
                "item_index": idx,
                "field": field,
                "value": value,
                "from_value": from_value,
                "reason": reason,
            }
        )
    return {"summary": summary, "suggestions": out}


def _fmt_counts(counts: dict) -> str:
    """{low, medium, high} → 「易/中/难」计数文案（门禁反馈用）。"""
    return "/".join(str(counts.get(k, 0)) for k in _DIFFICULTY_VOCAB)


def validate_suggestions(result: dict, context: dict) -> dict:
    """对建议跑数据安全门禁，返回 {passed, code, message}。

    门禁：总评必须存在（教师要靠它决定看不看清单）；score 类建议的分值增减
    合计必须为 0——调分成对由代码校验，全卷总分不变不靠模型自觉；有
    difficulty_target 时，模拟应用全部 difficulty 建议后的各档题数必须恰好
    等于目标——「清单给全」由代码判定，防止只回几条示范性调整，也防止模型
    一句「无需调整」糊弄过去（目标没达成，空清单同样打回）。scope 是
    question_type 时逐题型分别达标，且只准动 difficulty：题型占比不在该指令
    范围内，改了 question_type 会让逐题型目标失去意义，直接打回。
    """
    summary = result.get("summary") or ""
    if len(summary) < _MIN_SUMMARY_LEN:
        return {
            "passed": False,
            "code": "summary_short",
            "message": f"总评为空或不足 {_MIN_SUMMARY_LEN} 字",
        }

    delta = 0.0
    for entry in result.get("suggestions") or []:
        if entry["field"] != "score":
            continue
        current = context["current_by_index"][entry["item_index"]]
        delta += float(entry["value"]) - float(current.get("score") or 0)
    if abs(delta) > 0.001:
        return {
            "passed": False,
            "code": "score_deltas",
            "message": f"score 类建议的分值增减合计为 {round(delta, 2)} 分不为 0——调分必须成对，全卷总分不变",
        }

    target = context.get("difficulty_target")
    if target:
        # 模拟应用后的最终状态（difficulty 建议直接覆盖原值）
        final_diff = {
            idx: str(item.get("difficulty") or "")
            for idx, item in context["current_by_index"].items()
        }
        for entry in result.get("suggestions") or []:
            if entry["field"] == "difficulty":
                final_diff[entry["item_index"]] = str(entry["value"])

        if target.get("by_type"):
            moves = [
                entry["item_index"]
                for entry in result.get("suggestions") or []
                if entry["field"] == "question_type"
            ]
            if moves:
                return {
                    "passed": False,
                    "code": "difficulty_target",
                    "message": (
                        f"逐题型难度目标下只准调整 difficulty，题位 {moves} 被建议改了 "
                        "question_type——题型占比不在本次要求内，改动会让 by_type 的"
                        "题型分组失效，撤掉这些题型改动"
                    ),
                }
            # 按题型分组统计模拟应用后的分布（题型分组未被改动 = 当前分组）
            per_type = {q: {k: 0 for k in _DIFFICULTY_VOCAB} for q in target["by_type"]}
            for idx, item in context["current_by_index"].items():
                q = str(item.get("question_type") or "")
                diff = final_diff.get(idx, "")
                if q in per_type and diff in per_type[q]:
                    per_type[q][diff] += 1
            off = [
                f"{q} 现{_fmt_counts(per_type[q])} → 目标"
                f"{_fmt_counts(target['by_type'][q]['target_counts'])}"
                for q in target["by_type"]
                if per_type[q] != target["by_type"][q]["target_counts"]
            ]
            if off:
                return {
                    "passed": False,
                    "code": "difficulty_target",
                    "message": (
                        "按题型难度目标仍未达标（易/中/难计数）：" + "；".join(off)
                        + "——按 difficulty_target.by_type 里各题型的 gap 补齐缺口，"
                        "把每个题型需要动的题位全部列出，不要只给示范条目，"
                        "更不要回复「无需调整」"
                    ),
                }
        else:
            counts = {k: 0 for k in _DIFFICULTY_VOCAB}
            for diff in final_diff.values():
                if diff in counts:
                    counts[diff] += 1
            want = target["target_counts"]
            if counts != want:
                gaps = {
                    k: want[k] - counts[k]
                    for k in _DIFFICULTY_VOCAB
                    if want[k] != counts[k]
                }
                return {
                    "passed": False,
                    "code": "difficulty_target",
                    "message": (
                        f"这些建议全部应用后难度分布为 {counts}，未达到目标 {want}"
                        f"（各档仍差 {gaps}）——按 difficulty_target.gap 把缺口"
                        "对应的题位全部列出，不要只给示范条目"
                    ),
                }
    return {"passed": True, "code": "ok", "message": "通过建议结构校验"}


# ---------------------------------------------------------------------------
# 建议执行
# ---------------------------------------------------------------------------


def run_suggest(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    instruction: str,
    client,
) -> dict:
    """产出一次题位调整建议；首提未过校验时带反馈纠错一次，仍未过则如实报错。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。本函数只读数据、
    只调模型，不写任何业务表。
    """
    context = load_suggest_context(
        session, course_id=course_id, project_id=project_id
    )
    # 教师指令里的难度比例确定性换算成目标分布（解析不到为 None，不设门禁）
    context["difficulty_target"] = _difficulty_target(
        instruction, context["items"]
    )
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_suggest_prompt(context, instruction)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    result = normalize_suggestions(raw, context)
    validation = validate_suggestions(result, context)

    if not validation["passed"]:
        system_prompt, retry_payload = build_suggest_prompt(
            context, instruction, previous_error=validation["message"]
        )
        raw = client.request_json(
            system_prompt=system_prompt,
            payload=retry_payload,
            temperature=0.4,
            call_context=call_context,
        )
        result = normalize_suggestions(raw, context)
        validation = validate_suggestions(result, context)

    if not validation["passed"]:
        raise BlueprintSuggestError(
            f"AI 调整建议未通过结构校验：{validation['message']}"
        )

    return {
        "course_id": course_id,
        "project_id": project_id,
        "instruction": instruction,
        "summary": result["summary"],
        "suggestions": result["suggestions"],
        "total_score": context["total_score"],
    }


def execute_suggest_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行建议生成（不写任何业务表）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise BlueprintSuggestError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_suggest(
        session,
        course_id=str(payload["course_id"]),
        project_id=str(payload["project_id"]),
        instruction=str(payload.get("instruction") or ""),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(course_id: str, project_id: str, instruction: str) -> str:
    return hashlib.sha256(
        f"suggest:{course_id}:{project_id}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_suggest(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    instruction: str,
) -> str:
    """校验项目/蓝图上下文并创建建议任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_review）：同要求的**在途**任务复用，避免双击重复
    烧模型；已到终态的任务换一把新键，教师重新发起才能拿到新建议。instruction
    可空（常规检查也合法），空则以空串入键。
    """
    instruction = str(instruction or "").strip()

    # 上下文不入队，worker 执行时再取最新（教师可能刚改过题位）
    load_suggest_context(
        session, course_id=course_id, project_id=project_id
    )

    base_key = _task_key(course_id, project_id, instruction)
    existing = session.execute(
        select(task_runs.c.id, task_runs.c.status).where(
            task_runs.c.course_id == course_id,
            task_runs.c.idempotency_key == base_key,
        )
    ).one_or_none()
    if existing is not None:
        if existing._mapping["status"] not in TERMINAL_TASK_STATUSES:
            return str(existing._mapping["id"])
        key = hashlib.sha256(f"{base_key}:{uuid4().hex}".encode()).hexdigest()[:24]
    else:
        key = base_key

    return create_task_run(
        session,
        course_id=course_id,
        task_type=TASK_TYPE,
        idempotency_key=key,
        input_version=_INPUT_VERSION,
        payload={
            "course_id": course_id,
            "project_id": project_id,
            "instruction": instruction,
        },
    )


# ---------------------------------------------------------------------------
# 读回（刷新/切页后恢复面板）
# ---------------------------------------------------------------------------


def latest_suggest_run(
    session: Session, *, course_id: str, project_id: str
) -> dict | None:
    """读回本项目当前蓝图版本下最近一次建议任务的 task_runs 行，无则 None。

    只读，不产生任何提案或写入。刷新页面后建议清单与「已应用」状态不能只存组件
    内存——权威数据源是 task_runs，面板挂载时据此恢复（在途任务续轮询、成功任务
    恢复清单）。范围限定当前蓝图版本创建之后的任务：蓝图重建只追加新版本，旧建议
    针对旧题位快照，恢复出来会把过期提案盖到新题位上，按 created_at 直接丢弃。

    项目过滤按 payload.project_id 在 Python 侧做（与 exam_project_service 归并
    生成任务同款写法，规避 SQLite/PG 的 JSON 路径方言差异）；行按时间倒序取，
    本项目第一条即最近一次，若它已早于当前版本则更早的必然也过期，直接断定无可
    恢复项。项目/蓝图不存在按业务异常上抛（API 层映射 404），无任务返回 None。
    """
    try:
        bv = _resolve_blueprint_version(
            session, course_id=course_id, project_id=project_id, require_draft=False
        )
    except BlueprintSuggestError:
        # 尚未生成蓝图 → 不可能有建议历史，等价于"无可恢复项"
        return None
    bv_created = bv.get("created_at")
    rows = session.execute(
        select(task_runs)
        .where(
            task_runs.c.course_id == course_id,
            task_runs.c.task_type == TASK_TYPE,
        )
        .order_by(task_runs.c.created_at.desc())
    ).mappings().all()
    for row in rows:
        payload = row.get("payload") or {}
        if payload.get("project_id") != project_id:
            continue
        created = row.get("created_at")
        if bv_created and created and created < bv_created:
            break  # 倒序：本项目最近一条已早于当前蓝图版本，更早的都是旧版建议
        return dict(row)
    return None
