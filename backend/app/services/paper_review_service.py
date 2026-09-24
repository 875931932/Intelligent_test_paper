"""整卷 AI 质量评审：只读报告（零写路径）。

链路：POST .../paper-versions/{pv_id}/ai-review 建 task_runs → worker 调
``execute_review_task`` 把确定性数据（题目全量、needs_review 清单、合同终检
``audit_paper_against_contract`` 真实输出、蓝图/合同难度与题型配额）交给模型
「解读」→ 前端轮询展示只读质量评审报告。

本模块不写任何业务表（只 create_task_run）：发现问题时只引导教师走试卷页
既有功能（手动编辑 / AI 改题 / AI 生成新题 / 重新生成 / 确定稿）。报告只针对
试卷稿本身——禁止学生答卷评分（在线阅卷是范围外需求）。合同终检调用
generation_service 的既有函数拿 checks，不把它的逻辑抄进 prompt 让模型自己算；
快照缺失时如实降级为「终检不可用」，不伪造结果。

结构镜像 contract_explain_service（状态门禁 / prompt 纯函数 / 规整 / 校验收口 /
重试一次 / 幂等入队）。定稿（finalized）试卷照样可评审——报告是只读的，
这正是它的价值。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import (
    generated_questions,
    generation_runs,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
# 复用改题侧的判定与错误基类来源（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
from app.services.generation_service import audit_paper_against_contract
from app.services.paper_version_service import (
    PaperVersionError,
    get_paper_version,
    list_needs_review,
)

TASK_TYPE = "review_paper_version"
_INPUT_VERSION = "paper_review_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致

# 报告维度枚举（固定 5 类）：模型可只给其中若干类，越界维度在规整时丢弃
_SECTION_DIMENSIONS = (
    "难度分布",
    "题面表述",
    "答案与解析一致性",
    "覆盖与配额",
    "风险题",
)
_VERDICTS = {"pass", "attention"}
_SEVERITIES = {"info", "warn"}

# 总评最低字数：过短（含空串）视为模型没干活，带反馈纠错一次。
_MIN_SUMMARY_LEN = 10

# 模型超时 45s × 2 次尝试 × 2 轮（首解 + 带反馈纠错）最坏 ~180s，须留在租约内。
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

_SYSTEM_PROMPT = """你是高校期末试卷的质量评审专家。命题教师把一份试卷稿交给你做一次性的整卷质量评审；你的读者是命题教师，报告只针对试卷稿本身。

硬性规则（违反即废稿）：
1. 本报告只评审试卷稿：禁止学生答卷评分——不得输出学生分数、评分建议、给答卷打分、阅卷标准或任何针对学生作答内容的评价。在线阅卷不在本系统范围内。
2. 只解读「给定数据」：题目全量清单（items）、待审核清单（needs_review）、合同终检结果（final_check）、蓝图/合同难度与题型配额（quotas）、试卷基本信息（paper）。不得编造数据之外的事实；数据里没有的信息直接说明「未提供」。final_check 为 null 表示合同终检不可用，涉及终检的结论要如实标注「合同终检不可用」，不得声称检查通过，也不要自己重算检查结果。
3. 所有涉及具体题目的结论必须带真实 item_index：finding 里引用题号、sections 的 item_indexes 只能取自给定题目清单中出现的 item_index，禁止编造或想当然题号；不指向具体题时 item_indexes 给空数组。
4. 分布与配额是确定性数据：难度分布、题型与覆盖配额都以给定计数为准，不要重新计算或推测别的比例；你的工作是解读这些计数说明了什么。
5. 只读建议：发现问题时只引导教师用试卷页既有功能处理——手动编辑题目、单题 AI 改题、AI 生成新题、重新生成试卷、确认定稿；不得输出绕过确定性约束（题型比例、难度配额、合同去重、答案互斥）的改法，不得声称你能修改任何数据。
6. sections 的 dimension 只能取这 5 类：难度分布 / 题面表述 / 答案与解析一致性 / 覆盖与配额 / 风险题；只输出给定数据支撑得住的维度（可只给其中若干类）。severity：需要教师处理的问题用 warn，仅提示用 info。
7. verdict：存在需要教师处理的问题给 "attention"，否则给 "pass"。summary 面向教师 2-4 句；finding/suggestion 口吻清楚、可执行。

只返回严格 JSON 对象：
{"verdict": "pass|attention", "summary": "总评 2-4 句", "sections": [{"dimension": "难度分布", "severity": "info|warn", "finding": "结论（引用 item_index 或确定性数据）", "suggestion": "教师下一步怎么做（用既有功能）", "item_indexes": []}]}"""


class PaperReviewError(Exception):
    """整卷质量评审业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 上下文装配（全部确定性数据；无状态禁令，finalized 卷照样可评审）
# ---------------------------------------------------------------------------


def _slim_items(pv_data: dict) -> list[dict]:
    """按 get_paper_version 的生效题面口径（override 优先）裁出评审需要的字段。

    不带 teacher_override/finalized_text/quality_audit 等实现细节——报告只
    针对卷面本身。
    """
    items: list[dict] = []
    for q in pv_data.get("questions") or []:
        item = {
            "item_index": q.get("item_index"),
            "question_type": q.get("question_type"),
            "difficulty": q.get("difficulty"),
            "score": q.get("score"),
            "stem": q.get("stem"),
            "options": q.get("options"),
            "answer": q.get("answer"),
            "explanation": q.get("explanation"),
            "needs_review": bool(q.get("needs_review")),
            "needs_review_reason": q.get("needs_review_reason") or "",
        }
        subquestions = q.get("subquestions") or []
        if subquestions:
            item["subquestions"] = subquestions
        items.append(item)
    return items


def _actual_counts(items: list[dict]) -> dict:
    """卷面实际难度/题型计数（确定性统计，不是让模型自己算）。"""
    difficulty = Counter(str(q.get("difficulty") or "unknown") for q in items)
    question_type = Counter(str(q.get("question_type") or "unknown") for q in items)
    return {
        "difficulty": dict(difficulty),
        "question_type": dict(question_type),
    }


def _plan_quotas(session: Session, *, course_id: str, generation_run_id: str | None) -> dict | None:
    """蓝图（该卷 generation_run 关联的 blueprint）难度与题型配额；读不到返回 None。"""
    if not generation_run_id:
        return None
    blueprint_version_id = session.execute(
        select(generation_runs.c.blueprint_version_id).where(
            generation_runs.c.id == generation_run_id,
            generation_runs.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    if not blueprint_version_id:
        return None
    rows = session.execute(
        select(plan_items.c.difficulty, plan_items.c.question_type).where(
            plan_items.c.blueprint_version_id == blueprint_version_id,
            plan_items.c.course_id == course_id,
        )
    ).all()
    if not rows:
        return None
    difficulty = Counter(str(r._mapping["difficulty"] or "unknown") for r in rows)
    question_type = Counter(str(r._mapping["question_type"] or "unknown") for r in rows)
    return {"difficulty": dict(difficulty), "question_type": dict(question_type)}


def _load_final_check(
    session: Session, *, course_id: str, generation_run_id: str | None, paper_version_id: str
) -> dict | None:
    """调用既有「合同终检」函数拿 checks；拿不到合同快照时如实降级返回 None。

    checks 的逻辑（配额一致/原子唯一/答案互斥/溯源完整/needs_review 清零）
    只属于 generation_service.audit_paper_against_contract——本模块只取它的
    真实输出喂给模型解读，不抄逻辑进 prompt 让模型自己算。
    """
    from app.domain.generation.contract import ContractSlot
    from app.services.contract_execution_service import coerce_contract_snapshot

    if not generation_run_id:
        return None
    raw = session.execute(
        select(generation_runs.c.contract_snapshot).where(
            generation_runs.c.id == generation_run_id,
            generation_runs.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    snap = coerce_contract_snapshot(raw)
    slots_raw = (snap or {}).get("slots")
    if not isinstance(slots_raw, list) or not slots_raw:
        return None

    try:
        slots = [ContractSlot.model_validate(item) for item in slots_raw]
    except Exception:
        # 快照残缺：与其给出半真半假的终检，不如如实标不可用
        return None

    # 终检的 questions 入参要的是盖章后的溯源字段（generation_runner 持久化在
    # generated_questions.payload 里）；item_index 用当前 display_order（报告与
    # 教师看到的题号同源，重排/删除后 payload 里的旧号已失真）。
    rows = session.execute(
        select(
            paper_items.c.display_order,
            paper_items.c.needs_review,
            generated_questions.c.payload,
            generated_questions.c.knowledge_card_id,
        )
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .where(
            paper_items.c.paper_version_id == paper_version_id,
            paper_items.c.course_id == course_id,
        )
        .order_by(paper_items.c.display_order)
    ).all()
    questions = []
    for r in rows:
        payload = r._mapping["payload"] or {}
        questions.append(
            {
                "item_index": r._mapping["display_order"],
                "coverage_atom": payload.get("coverage_atom"),
                "answer_boundary": payload.get("answer_boundary"),
                "exam_point_id": payload.get("exam_point_id"),
                "unit_id": payload.get("unit_id"),
                "card_id": payload.get("card_id") or r._mapping["knowledge_card_id"],
                "needs_review": bool(r._mapping["needs_review"]),
                "backfilled_from": payload.get("backfilled_from"),
            }
        )

    try:
        return audit_paper_against_contract(slots, questions)
    except Exception:
        # 终检是报告的加分项而非门禁：异常时降级为不可用，不伪造结果
        return None


def load_review_context(
    session: Session, *, course_id: str, paper_version_id: str, instruction: str
) -> dict:
    """装配整卷评审上下文：题目全量 + needs_review 清单 + 合同终检 + 配额。

    所有查询带 course_id 过滤；试卷不存在/无题目直接拒绝（API 层映射 404/422）。
    不做状态禁令——只读报告对 finalized 试卷同样可用。
    """
    try:
        pv_data = get_paper_version(session, paper_version_id, course_id=course_id)
    except PaperVersionError as exc:
        raise PaperReviewError(str(exc)) from exc

    items = _slim_items(pv_data)
    if not items:
        raise PaperReviewError("试卷没有题目，无法发起质量评审")

    generation_run_id = pv_data.get("generation_run_id")
    final_check = _load_final_check(
        session,
        course_id=course_id,
        generation_run_id=generation_run_id,
        paper_version_id=paper_version_id,
    )
    plan_quotas = _plan_quotas(session, course_id=course_id, generation_run_id=generation_run_id)

    return {
        "paper_version_id": paper_version_id,
        "instruction": str(instruction or "").strip(),
        "paper": {
            "paper_version_id": paper_version_id,
            "version_no": pv_data.get("version_no"),
            "status": pv_data.get("status"),
            "total_score": pv_data.get("total_score"),
            "item_count": len(items),
        },
        "items": items,
        "needs_review": list_needs_review(session, paper_version_id, course_id=course_id),
        "final_check": final_check,
        "quotas": {
            "actual": _actual_counts(items),
            "plan": plan_quotas,  # 读不到就 None（如实缺失，不硬凑）
        },
    }


def build_review_prompt(
    context: dict, instruction: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言真实数据进了 prompt。"""
    payload: dict = {
        "instruction": instruction,
        "paper": context.get("paper") or {},
        "items": context.get("items") or [],
        "needs_review": context.get("needs_review") or [],
        "final_check": context.get("final_check"),
        "quotas": context.get("quotas") or {},
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 结果规整与校验（确定性收口）
# ---------------------------------------------------------------------------


def _known_item_indices(context: dict) -> set[int]:
    """给定题目清单里的全部题号：sections.item_indexes 只允许落在其中。"""
    indices: set[int] = set()
    for item in context.get("items") or []:
        idx = item.get("item_index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            indices.add(idx)
    return indices


def normalize_result(raw, context: dict) -> dict:
    """把模型返回收敛为 {verdict, summary, sections}。

    - sections 非 list → 空列表；非对象项丢弃；
    - dimension 不在固定 5 类枚举 → 丢弃该节；severity 非法 → warn；
    - item_indexes 越界（不在给定题目清单）→ 剔除，全被剔则置 []；
    - verdict 非法 → "attention"。
    """
    if not isinstance(raw, dict):
        raise PaperReviewError("模型未返回 JSON 对象")

    verdict = str(raw.get("verdict") or "").strip()
    if verdict not in _VERDICTS:
        verdict = "attention"

    known = _known_item_indices(context)
    sections: list[dict] = []
    raw_sections = raw.get("sections")
    if isinstance(raw_sections, list):
        for entry in raw_sections:
            if not isinstance(entry, dict):
                continue
            dimension = str(entry.get("dimension") or "").strip()
            if dimension not in _SECTION_DIMENSIONS:
                continue
            severity = str(entry.get("severity") or "").strip()
            if severity not in _SEVERITIES:
                severity = "warn"
            indexes: list[int] = []
            raw_indexes = entry.get("item_indexes")
            if isinstance(raw_indexes, list):
                for value in raw_indexes:
                    if isinstance(value, int) and not isinstance(value, bool) and value in known:
                        if value not in indexes:
                            indexes.append(value)
            sections.append(
                {
                    "dimension": dimension,
                    "severity": severity,
                    "finding": str(entry.get("finding") or "").strip(),
                    "suggestion": str(entry.get("suggestion") or "").strip(),
                    "item_indexes": indexes,
                }
            )

    return {
        "verdict": verdict,
        "summary": str(raw.get("summary") or "").strip(),
        "sections": sections,
    }


def validate_result(result: dict) -> dict:
    """对评审结果跑结构门禁，返回 {passed, code, message}。"""
    summary = str(result.get("summary") or "").strip()
    if len(summary) < _MIN_SUMMARY_LEN:
        return {
            "passed": False,
            "code": "summary_too_short",
            "message": f"总评为空或不足 {_MIN_SUMMARY_LEN} 字",
        }
    if result.get("verdict") not in _VERDICTS:
        return {
            "passed": False,
            "code": "verdict_invalid",
            "message": "verdict 只能是 pass 或 attention",
        }
    sections = result.get("sections")
    if not isinstance(sections, list) or not sections:
        return {
            "passed": False,
            "code": "sections_empty",
            "message": "sections 为空——至少要给出一个维度的评审结论",
        }
    for idx, section in enumerate(sections, start=1):
        if not str(section.get("finding") or "").strip():
            return {
                "passed": False,
                "code": "finding_empty",
                "message": f"第 {idx} 个维度缺少 finding 结论",
            }
    return {"passed": True, "code": "ok", "message": "通过评审结构校验"}


# ---------------------------------------------------------------------------
# 评审执行
# ---------------------------------------------------------------------------


def run_review(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    instruction: str,
    client,
) -> dict:
    """产出一次整卷质量评审报告；首提未过校验时带反馈纠错一次，仍未过则如实上报。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。本函数只读数据、
    只调模型，不写任何业务表。
    """
    context = load_review_context(
        session,
        course_id=course_id,
        paper_version_id=paper_version_id,
        instruction=instruction,
    )
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_review_prompt(context, instruction)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    result = normalize_result(raw, context)
    validation = validate_result(result)

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
        result = normalize_result(raw, context)
        validation = validate_result(result)

    return {
        "paper_version_id": paper_version_id,
        "instruction": instruction,
        "verdict": result["verdict"],
        "summary": result["summary"],
        "sections": result["sections"],
        "deterministic": {
            "needs_review_count": len(context.get("needs_review") or []),
            "final_check_available": context.get("final_check") is not None,
        },
        "validated": bool(validation["passed"]),
    }


def execute_review_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行整卷评审（不写任何业务表）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise PaperReviewError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_review(
        session,
        course_id=str(payload["course_id"]),
        paper_version_id=str(payload["paper_version_id"]),
        instruction=str(payload.get("instruction") or ""),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(paper_version_id: str, instruction: str) -> str:
    return hashlib.sha256(
        f"review:{paper_version_id}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_review(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    instruction: str,
) -> str:
    """校验试卷并创建评审任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_ai_revise / enqueue_explain）：同卷同关注点的**在途**
    任务复用，避免双击重复烧模型；已到终态的任务换一把新键，教师重新发起
    才能拿到新报告。instruction 可空 = 标准评审。
    """
    instruction = str(instruction or "").strip()

    # 状态校验与端点同口径（试卷存在 / 有题目可评）；上下文不入队，worker
    # 执行时再取最新（教师可能刚改过题）。不做 finalized 门禁——只读报告定稿后照样可用。
    load_review_context(
        session,
        course_id=course_id,
        paper_version_id=paper_version_id,
        instruction=instruction,
    )

    base_key = _task_key(paper_version_id, instruction)
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
            "paper_version_id": paper_version_id,
            "instruction": instruction,
        },
    )
