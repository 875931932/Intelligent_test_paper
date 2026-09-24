"""单题 AI 改题：提案生成与校验。

链路：POST .../items/{idx}/ai-revise 建 task_runs → worker 调
``execute_ai_revise_task`` 产出结构化提案（以合同槽位 + 知识卡为约束
grounding）→ ``validate_generated_question`` 收口 → 前端 diff 预览 →
教师确认后走既有 PATCH teacher_override_patch 落库。

本模块只产出提案，不写任何试卷数据；写入与回滚都复用
paper_version_service 的既有覆写端点（teacher_override 分层保留原题）。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import (
    generation_runs,
    generated_questions,
    knowledge_cards,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
from app.services.generation_service import validate_generated_question

TASK_TYPE = "ai_revise_item"
_INPUT_VERSION = "ai_revise_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致

# 提案只允许落在这些字段：题型/分值/难度/认知层级由合同锁定，不给改。
_PROPOSAL_KEYS = ("stem", "options", "answer", "explanation")

_TRUE_LITERALS = {"true", "t", "1", "yes", "y", "正确", "对"}
_FALSE_LITERALS = {"false", "f", "0", "no", "n", "错误", "错"}

# 模型超时 45s × 2 次尝试 × 2 轮（首提 + 带反馈纠错）最坏 ~180s，须留在租约内。
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

_SYSTEM_PROMPT = """你是高校期末试卷的学科编辑。教师给出对某一道题的修改要求，你在试卷合同（逐题锁定的约束）与知识卡的范围内改写这道题。

硬性规则（违反即废稿）：
1. 只修改教师要求涉及的内容；未要求改动的字段原样返回。
2. 题型、分值、难度、认知层级、考查原子不得改变——它们由试卷合同锁定。
3. 改写必须仍落在「考查原子」与「答案域」之内；「禁用上下文」列出的同考点其他题的原子与答案核心不得出现在本题中。
4. 改后答案必须仍然正确且与题干/选项严格对应：选择题保持四个互斥选项，单选题正确答案唯一、多选题至少两项。
5. 题干禁止来源话术：不得出现「根据课件/资料」「第X章/页/讲」「实验X」等表述。
6. 形态保持：填空题恰好一个由连续4个以上下划线构成的空，答案为简短术语（不超过20个汉字、不含标点）；判断题答案为布尔值 true/false；简答题保留或补齐解析（explanation）；综合题的分问（subquestions）不得改动。
7. options 的数据形态必须与原题完全一致（对象或数组），answer 的写法（选项字母/选项原文/布尔值）与原题一致；无需改动的字段一字不差地复述原值。

只返回严格 JSON 对象：
{"stem": "...", "options": ..., "answer": ..., "explanation": "...", "change_summary": "一两句话说明改了什么、为什么"}
change_summary 面向教师可读，不进入卷面。"""


class AiReviseError(Exception):
    """AI 改题业务错误（消息可直接面向 API 层映射）。"""


class AiReviseConflict(AiReviseError):
    """状态冲突（如试卷已定稿）。"""


def llm_configured() -> bool:
    """判定收口在 ``settings.llm_configured``；本薄包装保留给 router 与测试打桩。"""
    from app.config import settings

    return settings.llm_configured()


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def _contract_slot(session: Session, *, course_id: str, generation_run_id: str | None, plan_item_index: int | None) -> dict | None:
    """从 generation_run 的合同快照取该题位的槽位（按 plan_item 的 item_index 对齐）。"""
    if not generation_run_id or plan_item_index is None:
        return None
    # 局部导入：contract_execution_service 是快照读写属主，避免引入导入环
    from app.services.contract_execution_service import coerce_contract_snapshot

    snap = coerce_contract_snapshot(
        session.execute(
            select(generation_runs.c.contract_snapshot).where(
                generation_runs.c.id == generation_run_id,
                generation_runs.c.course_id == course_id,
            )
        ).scalar_one_or_none()
    )
    if snap is None:
        return None
    slots = snap.get("slots")
    if not isinstance(slots, list):
        return None
    for raw in slots:
        if isinstance(raw, dict) and raw.get("item_index") == plan_item_index:
            return raw
    return None


def load_revise_context(session: Session, *, course_id: str, paper_version_id: str, item_index: int) -> dict:
    """装配单题改题上下文：生效题面（override 优先）+ 合同槽位 + 知识卡。"""
    pv = session.execute(
        select(paper_versions.c.status, paper_versions.c.generation_run_id).where(
            paper_versions.c.id == paper_version_id,
            paper_versions.c.course_id == course_id,
        )
    ).one_or_none()
    if pv is None:
        raise AiReviseError("试卷版本不存在或不属于课程")
    if pv._mapping["status"] == "finalized":
        raise AiReviseConflict("试卷已定稿，撤销定稿后才能 AI 改题")

    row = session.execute(
        select(
            paper_items.c.teacher_override,
            generated_questions.c.payload.label("gq_payload"),
            generated_questions.c.knowledge_card_id,
            plan_items.c.item_index.label("plan_item_index"),
            plan_items.c.knowledge_card_id.label("plan_card_id"),
        )
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .join(plan_items, plan_items.c.id == generated_questions.c.plan_item_id, isouter=True)
        .where(
            paper_items.c.paper_version_id == paper_version_id,
            paper_items.c.course_id == course_id,
            paper_items.c.display_order == item_index,
        )
    ).one_or_none()
    if row is None:
        raise AiReviseError(f"item_index={item_index} 不在该试卷版本中")

    payload = row._mapping["gq_payload"] or {}
    override = row._mapping["teacher_override"] or {}

    def pick(key: str):
        # 与 get_paper_version 的派生口径一致：override 有该键就用 override
        return override.get(key, payload.get(key))

    current = {
        "question_type": pick("question_type"),
        "stem": pick("stem") or "",
        "options": pick("options"),
        "answer": pick("answer"),
        "explanation": pick("explanation"),
        "difficulty": pick("difficulty"),
        "cognitive_level": pick("cognitive_level"),
        "score": pick("score") if pick("score") is not None else payload.get("score"),
        "rubric": pick("rubric"),
        "subquestions": pick("subquestions") or [],
        "comprehensive_archetype": pick("comprehensive_archetype"),
    }

    slot = _contract_slot(
        session,
        course_id=course_id,
        generation_run_id=pv._mapping["generation_run_id"],
        plan_item_index=row._mapping["plan_item_index"],
    )

    card = None
    card_id = (
        row._mapping["knowledge_card_id"]
        or (slot or {}).get("card_id")
        or row._mapping["plan_card_id"]
    )
    if card_id:
        crow = session.execute(
            select(
                knowledge_cards.c.name,
                knowledge_cards.c.performance_statement,
                knowledge_cards.c.assessable_content,
                knowledge_cards.c.scope_boundary,
                knowledge_cards.c.answer_proposition,
                knowledge_cards.c.prompt_material,
            ).where(
                knowledge_cards.c.id == card_id,
                knowledge_cards.c.course_id == course_id,
            )
        ).one_or_none()
        if crow is not None:
            card = dict(crow._mapping)

    return {"item_index": item_index, "current": current, "slot": slot, "card": card}


def build_revise_prompt(context: dict, instruction: str, *, previous_error: str = "") -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言合同字段进了 prompt。"""
    slot = context.get("slot")
    contract = None
    if isinstance(slot, dict) and slot:
        contract = {
            "question_type": slot.get("question_type"),
            "difficulty": slot.get("difficulty"),
            "cognitive_level": slot.get("cognitive_level"),
            "score": slot.get("score"),
            "coverage_atom": slot.get("coverage_atom"),
            "answer_boundary": slot.get("answer_boundary"),
            "forbidden_context": slot.get("forbidden_context") or {},
            "performance_statement": slot.get("performance_statement"),
            "preferred_terms": slot.get("preferred_terms") or [],
        }
    card = context.get("card")
    card_section = None
    if isinstance(card, dict) and card:
        card_section = {
            "name": card.get("name"),
            "performance_statement": card.get("performance_statement"),
            "assessable_content": card.get("assessable_content") or [],
            "scope_boundary": card.get("scope_boundary") or {},
            "answer_proposition": card.get("answer_proposition"),
            "prompt_material": card.get("prompt_material") or [],
        }
    payload: dict = {
        "instruction": instruction,
        "question": context.get("current") or {},
        "contract": contract,
        "knowledge_card": card_section,
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 提案规整与校验（确定性收口）
# ---------------------------------------------------------------------------


def _align_options(raw_options, current_options):
    """把模型返回的选项规整回原题形态（dict↔list 互转）；数量问题交给校验。"""
    if raw_options is None or current_options is None:
        return raw_options
    if isinstance(current_options, dict) and isinstance(raw_options, list):
        keys = list(current_options.keys())
        if len(keys) == len(raw_options):
            return dict(zip(keys, raw_options))
    if isinstance(current_options, list) and isinstance(raw_options, dict):
        return [raw_options[k] for k in sorted(raw_options)]
    return raw_options


def _align_answer(raw_answer, *, current_answer, question_type: str):
    """判断题答案规整回布尔值；其余题型原样交给校验。"""
    if isinstance(current_answer, bool):
        if isinstance(raw_answer, bool):
            return raw_answer
        text = str(raw_answer or "").strip().lower()
        if text in _TRUE_LITERALS:
            return True
        if text in _FALSE_LITERALS:
            return False
        return raw_answer
    return raw_answer


def normalize_proposal(raw, *, current: dict) -> dict:
    """把模型返回收敛为四字段完整提案；缺失字段回填当前值，形态对齐原题。"""
    if not isinstance(raw, dict):
        raise AiReviseError("模型未返回 JSON 对象")
    proposal = {
        "stem": raw.get("stem") if raw.get("stem") is not None else current.get("stem") or "",
        "options": raw.get("options") if raw.get("options") is not None else current.get("options"),
        "answer": raw.get("answer") if raw.get("answer") is not None else current.get("answer"),
        "explanation": raw.get("explanation") if raw.get("explanation") is not None else current.get("explanation"),
    }
    if proposal["options"] is not None:
        proposal["options"] = _align_options(proposal["options"], current.get("options"))
    proposal["answer"] = _align_answer(
        proposal["answer"],
        current_answer=current.get("answer"),
        question_type=str(current.get("question_type") or ""),
    )
    if not str(proposal["stem"] or "").strip():
        proposal["stem"] = str(current.get("stem") or "")
    return proposal


def validate_proposal(proposal: dict, *, current: dict, atom_text: str = "") -> dict:
    """对「提案合并后的生效题」跑单题质量门禁，返回 {passed, code, message}。"""
    question = {
        "question_type": current.get("question_type"),
        "difficulty": current.get("difficulty") or "medium",
        "score": current.get("score") if current.get("score") is not None else 0,
        "stem": proposal.get("stem") or "",
        "options": proposal.get("options"),
        "answer": proposal.get("answer"),
        "explanation": proposal.get("explanation") or current.get("explanation"),
    }
    # 主观题 rubric / 综合题分问与题型约束来自原题，提案不改它们但校验会查
    if current.get("rubric"):
        question["rubric"] = current["rubric"]
    if current.get("subquestions"):
        question["subquestions"] = current["subquestions"]
    if current.get("comprehensive_archetype"):
        question["comprehensive_archetype"] = current["comprehensive_archetype"]

    result = validate_generated_question(question, atom_text=atom_text or "")
    if result.get("status") == "blocker":
        return {"passed": False, "code": result.get("code", ""), "message": result.get("message", "")}
    return {"passed": True, "code": "ok", "message": "通过基础质量检查"}


# ---------------------------------------------------------------------------
# 提案执行
# ---------------------------------------------------------------------------


def run_ai_revise(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    item_index: int,
    instruction: str,
    client,
) -> dict:
    """产出一次改题提案；首提未过校验时带反馈纠错一次，仍未过则如实上报。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。
    """
    context = load_revise_context(
        session, course_id=course_id, paper_version_id=paper_version_id, item_index=item_index
    )
    current = context["current"]
    atom_text = str((context.get("slot") or {}).get("coverage_atom") or "")
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_revise_prompt(context, instruction)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    proposal = normalize_proposal(raw, current=current)
    validation = validate_proposal(proposal, current=current, atom_text=atom_text)
    attempts = 1

    if not validation["passed"]:
        system_prompt, retry_payload = build_revise_prompt(
            context, instruction, previous_error=validation["message"]
        )
        raw = client.request_json(
            system_prompt=system_prompt,
            payload=retry_payload,
            temperature=0.4,
            call_context=call_context,
        )
        proposal = normalize_proposal(raw, current=current)
        validation = validate_proposal(proposal, current=current, atom_text=atom_text)
        attempts = 2

    return {
        "item_index": item_index,
        "instruction": instruction,
        "current": current,
        "proposal": proposal,
        "change_summary": str(raw.get("change_summary") or ""),
        "validation": validation,
        "attempts": attempts,
    }


def execute_ai_revise_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行改题提案（不写试卷数据）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise AiReviseError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_ai_revise(
        session,
        course_id=str(payload["course_id"]),
        paper_version_id=str(payload["paper_version_id"]),
        item_index=int(payload["item_index"]),
        instruction=str(payload["instruction"]),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(paper_version_id: str, item_index: int, instruction: str) -> str:
    return hashlib.sha256(
        f"ai-revise:{paper_version_id}:{item_index}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_ai_revise(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    item_index: int,
    instruction: str,
) -> str:
    """校验状态并创建改题任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_generation）：同题同要求的**在途**任务复用，
    避免双击重复烧模型；已到终态的任务换一把新键，教师重发同一要求
    才能真正拿到新提案，而不是旧任务的完成态。
    """
    instruction = str(instruction or "").strip()
    if not instruction:
        raise AiReviseError("修改要求不能为空")

    # 状态校验与 update_paper_item 同口径（试卷存在 / 未定稿 / 题目存在）；
    # 装配结果不入队，worker 执行时再取最新（教师可能刚改过题）。
    load_revise_context(
        session, course_id=course_id, paper_version_id=paper_version_id, item_index=item_index
    )

    base_key = _task_key(paper_version_id, item_index, instruction)
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
            "item_index": item_index,
            "instruction": instruction,
        },
    )
