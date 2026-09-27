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
让模型自己算。调分必须成对（增减合计 0）由代码校验，不靠模型自觉。

结构镜像 paper_review_service 的既定套路（上下文装配 / prompt 纯函数 / 归一 /
校验收口 / 带反馈纠错一次 / 幂等入队 / worker 入口）。
"""

from __future__ import annotations

import hashlib
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

只返回严格 JSON 对象：
{"summary": "两三句总评", "suggestions": [{"item_index": 5, "field": "difficulty", "value": "high", "reason": "..."}]}
summary 与 reason 面向教师可读，只随任务结果返回，不进入任何落库载荷。"""


class BlueprintSuggestError(Exception):
    """蓝图题位调整建议业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def _resolve_blueprint_version(session: Session, *, course_id: str, project_id: str) -> dict:
    """定位蓝图版本并做状态门禁（镜像 router 的 active → 最新 fallback 解析）。

    草稿才可调整：已确认蓝图受冻结纪律保护，PATCH 会 409，入队时就提前拒绝
    （错误文案与 PATCH 的"不可原地修改"同词，API 层可映射同一状态码）。
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
    if bv["status"] != "draft":
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
    去重、丢弃无操作建议；summary 取原样字符串。"""
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
        out.append(
            {"item_index": idx, "field": field, "value": value, "reason": reason}
        )
    return {"summary": summary, "suggestions": out}


def validate_suggestions(result: dict, context: dict) -> dict:
    """对建议跑数据安全门禁，返回 {passed, code, message}。

    两条门禁：总评必须存在（教师要靠它决定看不看清单）；score 类建议的分值
    增减合计必须为 0——调分成对由代码校验，全卷总分不变不靠模型自觉。
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
