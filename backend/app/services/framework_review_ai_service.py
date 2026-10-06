"""框架候选 AI 评审：只读评审报告 → 教师照常确认/拒绝（AI 不改框架）。

链路：POST .../framework-versions/current/ai-review 建 task_runs →
worker 调 ``execute_review_task`` 把确定性上下文（锚点、考点、考试规则、
待裁决冲突、后端算好的覆盖/权重/认知分层统计）交给模型产出只读评审报告
（verdict 参考意见 + summary 总评 + findings 问题清单）→ ``normalize``/
``validate`` 确定性收口（verdict 词表、severity/area 词表、去重、限条数、
总评长度）→ 前端轮询展示报告，教师照常走既有确认/拒绝流程。

红线：本模块只写 task_runs，不写 framework_versions / framework_conflicts——
报告纯只读，冲突裁决与发布仍是教师确认流；覆盖/权重/认知统计由后端确定性
算好作为「事实数据」给模型解读，不把统计口径塞进 prompt 让模型自己算；
verdict 是参考意见，不触发任何自动动作。

结构镜像 exam_rules_ai_service 的既定套路（上下文装配 / prompt 纯函数 / 归一 /
校验收口 / 带反馈纠错一次 / 幂等入队 / worker 入口）。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import framework_conflicts, framework_versions, task_runs
from app.domain.framework.exam_rules import normalize_exam_rules
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
# 复用既有判定与错误基类来源（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
from app.services.framework_service import allowed_question_types

TASK_TYPE = "review_framework_candidate"
_INPUT_VERSION = "framework_review_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

# 评审报告词表——报告只展示给教师看，词表保证前端能稳定着色/分组渲染
_VERDICTS = ("ready", "revise_first")
_VERDICT_ALIASES = {
    "ok": "ready", "pass": "ready", "confirm": "ready", "可确认": "ready", "可发布": "ready",
    "revise": "revise_first", "adjust": "revise_first", "hold": "revise_first",
    "not_ready": "revise_first", "需修改": "revise_first", "先修改": "revise_first",
}
_SEVERITIES = ("info", "warning", "critical")
_SEVERITY_ALIASES = {
    "note": "info", "low": "info", "低": "info", "提示": "info",
    "warn": "warning", "medium": "warning", "中": "warning", "警告": "warning",
    "error": "critical", "high": "critical", "严重": "critical", "高": "critical",
}
_AREAS = ("coverage", "weight", "question_type", "cognitive", "rules", "conflicts", "other")

# 认知层级词表（与蓝图引擎同值域）——统计口径在代码里定，不靠模型复述
_COGNITIVE_LEVELS = ("remember", "understand", "apply", "analyze", "evaluate", "create")

_MAX_FINDINGS = 12
_MIN_SUMMARY_LEN = 10
_MIN_MESSAGE_LEN = 5

_SYSTEM_PROMPT = """你是高校命题框架评审助手。命题教师刚由「教学大纲 + 考核大纲」生成了命题框架候选（考核范围锚点、考点、考试规则、待裁决冲突），正在决定是否确认发布。你产出一份**只读**评审报告：指出覆盖、权重、题型可行性、认知分层、规则一致性、冲突处置等方面的问题与亮点。报告只展示给教师参考——你不修改框架、不裁决冲突、不能代替教师点确认。

硬规则：
1. verdict 只能是 "ready"（可确认发布）或 "revise_first"（建议先修改）——这是参考意见，最终由教师决定。
2. findings 每条：severity ∈ payload.vocab.severity（info/warning/critical）；area ∈ payload.vocab.area；message 一两句说清问题与依据；suggestion 给教师可操作的下一步（没有就留空字符串 ""）。
3. 引用考点/章节时用 payload 里的 code / anchor_key，不臆造不存在的考点。
4. payload.stats、payload.open_conflicts 是后端算好的事实（覆盖、权重、认知分层、待裁决冲突），直接引用，不要自己重算比例。
5. findings 至多 12 条，按重要性排序；没有问题就返回空数组，在 summary 里说明亮点。
6. summary 是两三句面向教师的总评。

只返回严格 JSON 对象：
{"verdict": "ready", "summary": "两三句总评", "findings": [{"severity": "warning", "area": "weight", "message": "...", "suggestion": "..."}]}
summary/message/suggestion 面向教师可读，只随任务结果返回，不进入任何落库载荷。"""


class FrameworkReviewError(Exception):
    """框架候选 AI 评审业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def _resolve_target_version(session: Session, *, course_id: str) -> dict:
    """定位被评审版本：候选优先（教师正决定发不发布），否则最新已发布版。

    与 get_current_framework 的 published 优先相反——评审发生在确认动作之前，
    此时 pending 的 candidate 才是教师要看的那份。
    """
    row = session.execute(
        select(framework_versions)
        .where(
            framework_versions.c.course_id == course_id,
            framework_versions.c.status == "candidate",
        )
        .order_by(framework_versions.c.created_at.desc())
        .limit(1)
    ).mappings().one_or_none()
    if row is None:
        row = session.execute(
            select(framework_versions)
            .where(
                framework_versions.c.course_id == course_id,
                framework_versions.c.status == "published",
            )
            .order_by(framework_versions.c.version_no.desc())
            .limit(1)
        ).mappings().one_or_none()
    if row is None:
        raise FrameworkReviewError("命题框架不存在：请先构建命题框架再发起评审")
    return dict(row)


def _slim_anchor(raw: dict) -> dict:
    return {
        "key": str(raw.get("key")),
        "title": str(raw.get("title") or raw.get("key")),
        "exam_weight": raw.get("exam_weight"),
        "ability_requirements": raw.get("ability_requirements") or [],
        "allowed_question_types": raw.get("allowed_question_types") or [],
        "excluded_content": raw.get("excluded_content") or [],
    }


def _slim_point(raw: dict) -> dict:
    return {
        "code": str(raw.get("code") or ""),
        "title": str(raw.get("title") or ""),
        "anchor_key": str(raw.get("anchor_key") or ""),
        "assessment_requirement": str(raw.get("assessment_requirement") or ""),
        "weight_value": raw.get("weight_value"),
        "priority": str(raw.get("priority") or ""),
        "cognitive_targets": raw.get("cognitive_targets") or [],
        "assessment_orientations": raw.get("assessment_orientations") or [],
        "allowed_question_types": raw.get("allowed_question_types") or [],
        "status": str(raw.get("status") or ""),
    }


def _open_conflicts(session: Session, *, course_id: str, version_id: str) -> list[dict]:
    """待裁决冲突（读表的当前状态，而非 payload 快照——已发布版的冲突在发布时已被处置）。"""
    rows = session.execute(
        select(framework_conflicts.c.status, framework_conflicts.c.details).where(
            framework_conflicts.c.course_id == course_id,
            framework_conflicts.c.framework_version_id == version_id,
            framework_conflicts.c.status == "open",
        )
    ).all()
    out: list[dict] = []
    for row in rows:
        details = row._mapping["details"] or {}
        out.append(
            {
                "key": str(details.get("key") or ""),
                "kind": str(details.get("kind") or ""),
                "severity": str(details.get("severity") or "blocking"),
                "message": str(details.get("message") or ""),
            }
        )
    return out


def _stats(points: list[dict], anchors: list[dict], allowed: list[str]) -> dict:
    """后端算好的事实统计——模型只负责解读，不负责重算。"""
    per_anchor: dict[str, int] = {}
    for point in points:
        key = point["anchor_key"] or "(未归类)"
        per_anchor[key] = per_anchor.get(key, 0) + 1

    cognitive: dict[str, int] = {}
    for point in points:
        for level in point["cognitive_targets"]:
            key = str(level)
            cognitive[key] = cognitive.get(key, 0) + 1
    cognitive_list = [
        {"level": level, "count": cognitive[level]}
        for level in _COGNITIVE_LEVELS
        if level in cognitive
    ] + [
        {"level": level, "count": cognitive[level]}
        for level in sorted(cognitive)
        if level not in _COGNITIVE_LEVELS
    ]

    weight_sum = round(
        sum(float(a.get("exam_weight") or 0) for a in anchors), 2
    )
    return {
        "anchor_count": len(anchors),
        "point_count": len(points),
        "points_per_anchor": [
            {"anchor_key": key, "count": count}
            for key, count in sorted(per_anchor.items())
        ],
        "anchor_weight_sum": weight_sum,
        "cognitive_coverage": cognitive_list,
        "allowed_question_types": allowed,
    }


def load_review_context(session: Session, *, course_id: str) -> dict:
    """装配评审上下文：锚点/考点瘦身 + 考试规则 + 待裁决冲突 + 确定性统计。

    框架不存在直接拒绝（API 层映射 404）；所有查询带 course_id 过滤。
    """
    version = _resolve_target_version(session, course_id=course_id)
    version_id = str(version["id"])
    payload = version.get("payload") or {}

    anchors = [
        _slim_anchor(a)
        for a in (payload.get("anchors") or [])
        if isinstance(a, dict) and a.get("key")
    ]
    points = [
        _slim_point(p)
        for p in (payload.get("exam_points") or [])
        if isinstance(p, dict) and p.get("code")
    ]
    anchor_keys = [a["key"] for a in anchors]
    rules = normalize_exam_rules(payload.get("final_exam_rules"), anchor_keys=anchor_keys)
    allowed = allowed_question_types(
        session, course_id=course_id, framework_version_id=version_id
    )
    conflicts = _open_conflicts(session, course_id=course_id, version_id=version_id)

    return {
        "course_id": course_id,
        "instruction": "",
        "framework_version_id": version_id,
        "framework_status": str(version["status"]),
        "version_no": int(version["version_no"]),
        "anchors": anchors,
        "exam_points": points,
        "exam_rules": rules,
        "open_conflicts": conflicts,
        "stats": _stats(points, anchors, allowed),
    }


def build_review_prompt(
    context: dict, instruction: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言真实数据进了 prompt。"""
    payload: dict = {
        "instruction": instruction,
        "framework_status": context.get("framework_status"),
        "anchors": context.get("anchors") or [],
        "exam_points": context.get("exam_points") or [],
        "exam_rules": context.get("exam_rules") or {},
        "open_conflicts": context.get("open_conflicts") or [],
        "stats": context.get("stats") or {},
        "vocab": {
            "verdict": list(_VERDICTS),
            "severity": list(_SEVERITIES),
            "area": list(_AREAS),
        },
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 报告归一与校验（确定性收口）
# ---------------------------------------------------------------------------


def _coerce_verdict(value) -> str | None:
    key = str(value or "").strip().lower()
    if key in _VERDICTS:
        return key
    return _VERDICT_ALIASES.get(key)


def _coerce_severity(value) -> str | None:
    key = str(value or "").strip().lower()
    if key in _SEVERITIES:
        return key
    return _SEVERITY_ALIASES.get(key)


def normalize_report(raw, context: dict) -> dict:
    """把模型返回收敛为可信的只读报告：verdict 词表、severity/area 收口、
    message 非空、去重、限条数；summary 取原样字符串。"""
    if not isinstance(raw, dict):
        raise FrameworkReviewError("模型未返回 JSON 对象")

    verdict = _coerce_verdict(raw.get("verdict"))
    summary = str(raw.get("summary") or "").strip()

    entries = raw.get("findings")
    entries = entries if isinstance(entries, list) else []
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if len(findings) >= _MAX_FINDINGS:
            break
        if not isinstance(entry, dict):
            continue
        severity = _coerce_severity(entry.get("severity"))
        if severity is None:
            continue
        area = str(entry.get("area") or "").strip().lower()
        if area not in _AREAS:
            area = "other"
        message = str(entry.get("message") or "").strip()
        if len(message) < _MIN_MESSAGE_LEN:
            continue
        suggestion = str(entry.get("suggestion") or "").strip()
        if (area, message) in seen:
            continue
        seen.add((area, message))
        findings.append(
            {
                "severity": severity,
                "area": area,
                "message": message,
                "suggestion": suggestion,
            }
        )
    return {"verdict": verdict, "summary": summary, "findings": findings}


def validate_report(report: dict, context: dict) -> dict:
    """对报告跑结构门禁，返回 {passed, code, message}。

    两条门禁：verdict 必须落在词表（教师的确认/修改参考全靠它一句话结论）；
    总评必须存在（教师先读总评再决定看不看清单）。
    """
    if report.get("verdict") not in _VERDICTS:
        return {
            "passed": False,
            "code": "verdict_missing",
            "message": '缺少 verdict——报告必须给出 "ready" 或 "revise_first" 的发布参考意见',
        }
    summary = report.get("summary") or ""
    if len(summary) < _MIN_SUMMARY_LEN:
        return {
            "passed": False,
            "code": "summary_short",
            "message": f"总评为空或不足 {_MIN_SUMMARY_LEN} 字",
        }
    return {"passed": True, "code": "ok", "message": "通过报告结构校验"}


# ---------------------------------------------------------------------------
# 报告执行
# ---------------------------------------------------------------------------


def run_review(
    session: Session,
    *,
    course_id: str,
    instruction: str,
    client,
) -> dict:
    """产出一次只读评审报告；首提未过校验时带反馈纠错一次，仍未过则如实报错。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。本函数只读数据、
    只调模型，不写任何业务表。
    """
    context = load_review_context(session, course_id=course_id)
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_review_prompt(context, instruction)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    report = normalize_report(raw, context)
    validation = validate_report(report, context)

    if not validation["passed"]:
        system_prompt, retry_payload = build_review_prompt(
            context, instruction, previous_error=validation["message"]
        )
        raw = client.request_json(
            system_prompt=system_prompt,
            payload=retry_payload,
            temperature=0.4,
            call_context=call_context,
        )
        report = normalize_report(raw, context)
        validation = validate_report(report, context)

    if not validation["passed"]:
        raise FrameworkReviewError(
            f"AI 评审报告未通过结构校验：{validation['message']}"
        )

    return {
        "course_id": course_id,
        "framework_version_id": context["framework_version_id"],
        "framework_status": context["framework_status"],
        "instruction": instruction,
        "verdict": report["verdict"],
        "summary": report["summary"],
        "findings": report["findings"],
    }


def execute_review_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行评审（不写任何业务表）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise FrameworkReviewError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        # 思考档钉死 low（优先于 disable_thinking 的档案缺省档，见 config 注释）
        reasoning_effort=settings.ai_tool_reasoning_effort,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_review(
        session,
        course_id=str(payload["course_id"]),
        instruction=str(payload.get("instruction") or ""),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(course_id: str, version_id: str, instruction: str) -> str:
    # 版本进键：重建框架后即使要求相同也要拿到针对新候选的评审
    return hashlib.sha256(
        f"review:{course_id}:{version_id}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_review(
    session: Session,
    *,
    course_id: str,
    instruction: str,
) -> str:
    """校验框架上下文并创建评审任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_propose）：同要求的**在途**任务复用，避免双击重复
    烧模型；已到终态的任务换一把新键，教师重新发起才能拿到新报告。instruction
    可空（常规评审也合法），空则以空串入键。
    """
    instruction = str(instruction or "").strip()

    # 上下文不入队，worker 执行时再取最新（教师可能刚确认/重建）
    context = load_review_context(session, course_id=course_id)

    base_key = _task_key(
        course_id, str(context["framework_version_id"]), instruction
    )
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
            "instruction": instruction,
        },
    )
