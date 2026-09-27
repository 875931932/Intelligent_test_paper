"""考核规则 AI 助手：一句话要求 → 考试规则提案（只产提案，不绕教师确认流）。

链路：POST .../framework-versions/current/rules/ai-propose 建 task_runs →
worker 调 ``execute_propose_task`` 把确定性上下文（当前考核规则、章节锚点、
课程已确认考点允许的题型）与教师的一句话要求交给模型产出规则提案 →
``normalize_exam_rules`` 确定性归一（题型英文枚举、未知项剔除、比例归一 100、
侧重点归一）→ 前端轮询拿到提案后**填入考核规则卡的编辑草稿**，教师核对修改后
点「保存」走既有 PATCH rules 端点落库。

红线：本模块只写 task_runs，不写 framework_versions——命题比例/侧重点属于
教师确认流，AI 只能提案；归一化是确定性函数（domain 既有 normalize_exam_rules），
不是把"比例要凑 100"这类约束写进 prompt 让模型自觉遵守。模型未提及的字段
照抄当前规则（prompt 明说 + 合并双保险），防止提案把教师已有设置清空。

结构镜像 paper_review_service 的既定套路（上下文装配 / prompt 纯函数 / 归一 /
校验收口 / 带反馈纠错一次 / 幂等入队 / worker 入口）。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import task_runs
from app.domain.blueprint.models import ASSESSMENT_MODES
from app.domain.framework.exam_rules import normalize_exam_rules
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
# 复用既有判定与错误基类来源（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
from app.services.framework_service import (
    FrameworkNotFoundError,
    allowed_question_types,
    get_current_framework,
)

TASK_TYPE = "propose_exam_rules"
_INPUT_VERSION = "exam_rules_propose_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

# 提案字段（模型给不出就沿用当前规则的那些）——顺序即合并顺序
_PROPOSAL_FIELDS = (
    "exam_form",
    "duration_minutes",
    "total_score",
    "question_type_ratios",
    "chapter_weights",
    "assessment_focus",
)

_SYSTEM_PROMPT = """你是高校课程的考核规则设计助手。命题教师会用一句话描述他想要的期末考核安排（例如"闭卷笔试90分钟，选择题40%，第3章多考一些，侧重实操"），你据此产出一份结构化的「考试规则提案」。你的读者是命题教师：提案会被填入考核规则卡的编辑草稿，由教师核对、修改后点保存才生效——你只是提案，不代替教师做决定。

硬规则：
1. question_type_ratios 的 question_type 只能取 payload.allowed_question_types 里的英文枚举，ratio 是该题型占总分的百分比；各题合计应约 100。
2. chapter_weights 的 anchor_key 只能取 payload.anchors 里的 key，weight 是该章占命题权重的百分比；教师没提章节就照抄 payload.current_rules.chapter_weights 原样返回，不要留空。
3. assessment_focus 的 assessment_mode 只能取 payload.assessment_modes 里的英文枚举，weight 是偏好权重（合计约 100）；教师没提侧重点就照抄 payload.current_rules.assessment_focus 原样返回（当前为空就返回 []）。
4. exam_form 是考试形式（如"闭卷笔试"），duration_minutes 是时长（分钟），total_score 是总分（分）；教师没提就照抄 payload.current_rules 的对应值，数字字段必须是数字而不是带单位的字符串。
5. 比例归一到 100 由后端确定性完成，你不用为凑 100 过度纠结，但要给出合理比例。
6. 只依据教师的要求与 payload 里的既有规则，不臆造章节或题型；提案必须给出非空的 question_type_ratios。

只返回严格 JSON 对象：
{"exam_form": "...", "duration_minutes": 90, "total_score": 100,
 "question_type_ratios": [{"question_type": "single_choice", "ratio": 40}],
 "chapter_weights": [{"anchor_key": "A1", "weight": 30}],
 "assessment_focus": [{"assessment_mode": "conceptual", "weight": 60}],
 "explanation": "一两句话说明这份提案如何呼应教师的要求"}
explanation 面向教师可读，不进入落库载荷。"""


class ExamRulesAIError(Exception):
    """考核规则 AI 助手业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def load_propose_context(session: Session, *, course_id: str) -> dict:
    """装配提案上下文：当前考核规则 + 章节锚点 + 可用题型 + 考查方式枚举。

    框架不存在直接拒绝（API 层映射 404）；所有查询带 course_id 过滤。
    """
    try:
        current = get_current_framework(session, course_id=course_id)
    except FrameworkNotFoundError as exc:
        raise ExamRulesAIError("请先构建命题框架，再使用考核规则助手") from exc

    payload = current.get("payload") or {}
    anchors = [
        {"key": str(a.get("key")), "title": str(a.get("title") or a.get("key"))}
        for a in (payload.get("anchors") or [])
        if isinstance(a, dict) and a.get("key")
    ]
    version_id = str(current.get("id") or "")
    return {
        "course_id": course_id,
        "instruction": "",
        "current_rules": current.get("exam_rules") or {},
        "anchors": anchors,
        "anchor_keys": [a["key"] for a in anchors],
        "allowed_question_types": allowed_question_types(
            session, course_id=course_id, framework_version_id=version_id
        ),
        "assessment_modes": list(ASSESSMENT_MODES),
    }


def build_propose_prompt(
    context: dict, instruction: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言真实数据进了 prompt。"""
    payload: dict = {
        "instruction": instruction,
        "current_rules": context.get("current_rules") or {},
        "anchors": context.get("anchors") or [],
        "allowed_question_types": context.get("allowed_question_types") or [],
        "assessment_modes": context.get("assessment_modes") or [],
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 提案归一与校验（确定性收口）
# ---------------------------------------------------------------------------


def normalize_proposal(raw, context: dict) -> dict:
    """把模型返回收敛为与 PATCH rules 同形态的考核规则提案。

    模型未提及的字段照抄当前规则（防止清空教师已有设置），整包交给既有
    ``normalize_exam_rules`` 归一：题型英文枚举、未知项剔除、比例与侧重点
    归一到 100、章节锚点按 payload 过滤。
    """
    if not isinstance(raw, dict):
        raise ExamRulesAIError("模型未返回 JSON 对象")

    current = context.get("current_rules") or {}
    merged = {
        key: raw[key] if key in raw else current.get(key)
        for key in _PROPOSAL_FIELDS
    }
    proposal = normalize_exam_rules(merged, anchor_keys=context.get("anchor_keys") or [])
    return {key: proposal[key] for key in _PROPOSAL_FIELDS}


def validate_proposal(proposal: dict, context: dict) -> dict:
    """对提案跑数据安全门禁，返回 {passed, code, message}。

    两条门禁：提案必须给出题型比例（否则草稿无法落成试卷结构）；教师已有的
    字段不得被提案清空（模型把数字答成字符串/把章节答成无效锚点时，归一后
    会变成 None/[]，照填草稿等于丢教师数据）。
    """
    if not proposal.get("question_type_ratios"):
        return {
            "passed": False,
            "code": "ratios_empty",
            "message": "题型比例为空——提案必须给出各题型占总分的百分比",
        }
    current = context.get("current_rules") or {}
    labels = (
        ("chapter_weights", "章节命题权重"),
        ("exam_form", "考试形式"),
        ("duration_minutes", "考试时长"),
        ("total_score", "总分"),
    )
    lost = [label for key, label in labels if current.get(key) and not proposal.get(key)]
    if lost:
        return {
            "passed": False,
            "code": "fields_lost",
            "message": f"提案清空了教师已有的{'、'.join(lost)}——未提及的字段应沿用当前规则",
        }
    return {"passed": True, "code": "ok", "message": "通过提案结构校验"}


# ---------------------------------------------------------------------------
# 提案执行
# ---------------------------------------------------------------------------


def run_propose(
    session: Session,
    *,
    course_id: str,
    instruction: str,
    client,
) -> dict:
    """产出一次考试规则提案；首提未过校验时带反馈纠错一次，仍未过则如实报错。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。本函数只读数据、
    只调模型，不写任何业务表。
    """
    context = load_propose_context(session, course_id=course_id)
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_propose_prompt(context, instruction)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    proposal = normalize_proposal(raw, context)
    validation = validate_proposal(proposal, context)

    if not validation["passed"]:
        system_prompt, retry_payload = build_propose_prompt(
            context, instruction, previous_error=validation["message"]
        )
        raw = client.request_json(
            system_prompt=system_prompt,
            payload=retry_payload,
            temperature=0.4,
            call_context=call_context,
        )
        proposal = normalize_proposal(raw, context)
        validation = validate_proposal(proposal, context)

    if not validation["passed"]:
        raise ExamRulesAIError(f"AI 提案未通过结构校验：{validation['message']}")

    return {
        "course_id": course_id,
        "instruction": instruction,
        "proposal": proposal,
        "explanation": str(raw.get("explanation") or "").strip(),
    }


def execute_propose_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行提案生成（不写任何业务表）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise ExamRulesAIError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_propose(
        session,
        course_id=str(payload["course_id"]),
        instruction=str(payload.get("instruction") or ""),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(course_id: str, instruction: str) -> str:
    return hashlib.sha256(
        f"propose:{course_id}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_propose(
    session: Session,
    *,
    course_id: str,
    instruction: str,
) -> str:
    """校验框架并创建提案任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_review）：同要求的**在途**任务复用，避免双击重复
    烧模型；已到终态的任务换一把新键，教师重新发起才能拿到新提案。
    instruction 必填（一句话要求是提案的输入，空请求无意义）。
    """
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ExamRulesAIError("请先描述考核要求（一句话即可）")

    # 上下文不入队，worker 执行时再取最新（教师可能刚改过规则）
    load_propose_context(session, course_id=course_id)

    base_key = _task_key(course_id, instruction)
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
