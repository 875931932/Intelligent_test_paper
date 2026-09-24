"""合同槽位 AI 解释：只读解读与调整建议（零写路径）。

链路：POST .../contract-slots/{item_index}/explain 建 task_runs → worker 调
``execute_explain_task`` 把确定性分配的真实产出（目标槽位字段、关联知识卡、
蓝图题位、章权重、同卷其他槽位概览）交给模型「解读」→ 前端轮询展示只读解释
与调整建议。

本模块不写任何合同/蓝图/试卷数据：合同按与 revise 预览同一条
``allocate_with_fallback`` 只读重算（同蓝图同 seed 结果固定），任何调整都由
教师走既有 PATCH contracts/revise（slot_revisions）与 POST contracts/confirm
落地。prompt 只要求模型「解读给定数据」——不许重做分配、不许编造数据外
事实、不许建议绕过全局约束的手工改法（比例/难度/去重由确定性算法负责）。

结构镜像 ai_create_service（状态门禁 / prompt 纯函数 / 规整 / 校验收口 /
重试一次 / 幂等入队）。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import (
    blueprint_versions,
    content_domains,
    exam_projects,
    knowledge_cards,
    plan_items,
    task_runs,
)
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
# 复用改题侧的判定与错误基类来源（模块级 import 优于复制第二份）
from app.services.ai_revise_service import llm_configured
from app.services.contract_execution_service import (
    ContractExecutionError,
    allocate_with_fallback,
)

TASK_TYPE = "explain_contract_slot"
_INPUT_VERSION = "contract_explain_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致

# 同卷其他槽位概览的原子截断长度：供模型解释「为何是这个原子而非别的」时
# 有全局视野，又不把整卷原子全塞进 prompt。
_OTHER_SLOT_ATOM_LEN = 20

# 解释正文的最低字数：过短（含空串）视为模型没干活，带反馈纠错一次。
_MIN_EXPLANATION_LEN = 20

# 模型超时 45s × 2 次尝试 × 2 轮（首解 + 带反馈纠错）最坏 ~180s，须留在租约内。
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

_SYSTEM_PROMPT = """你是高校期末试卷的命题专家。教师在出卷流水线的合同阶段向你询问：某个题位为什么被分配到这个考查原子、如果想换考什么应该怎么办。你只负责解读系统给出的确定性分配结果，帮助教师看懂这份合同。

硬性规则（违反即废稿）：
1. 只解读「给定数据」：目标槽位、蓝图计划项、章与章权重、关联知识卡、同卷其他槽位概览。不得编造数据之外的考点、卡片、规则或比例；数据里没有的信息直接说明「未提供」。
2. 分配由确定性算法完成：不要重新做分配，不要输出「重新分配应该是什么样」的推算结果，也不要声称你能修改任何数据——你产出的只是解释与建议。
3. 解释要落在真实字段上：考查原子（coverage_atom）、答案域（answer_boundary）、禁用上下文（forbidden_context）、难度、认知层级、分值、章权重、同卷其他槽位的分布；回答「为什么是这个原子而非别的」时只能引用这些给定信息。
4. suggestions 只能引导教师走两条既有落地路径：①合同阶段的「合同修订」（先 PATCH contracts/revise 预览、再 POST contracts/confirm 落库）；②换分配方案（allocation_seed）或调整蓝图后重新运行分配算法。
5. 禁止给出绕过全局约束的手工改法：不得建议教师直接手改难度/分值/题型去凑比例，不得建议绕过「不重复、比例对、难度对」这些由算法保证的全局约束。
6. target_item_index 必须取自给定槽位的 item_index（不指向具体题位时用 null）。
7. 教师追问（instruction）非空时，instruction_response 直接回答该追问；没有追问时返回空串。explanation 面向教师、口吻清楚、2-4 段。

只返回严格 JSON 对象：
{"explanation": "面向教师的解释，2-4 段", "suggestions": [{"concern": "教师可能的疑虑", "suggestion": "建议怎么做（走合同修订/换方案重跑）", "target_item_index": null}], "instruction_response": "对教师追问的直接回答，无追问则为空串"}"""


class ContractExplainError(Exception):
    """合同槽位解释业务错误（消息可直接面向 API 层映射）。"""


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def load_explain_context(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    item_index: int,
    allocation_seed: int | None,
    blueprint_version_id: str,
) -> dict:
    """装配合同槽位解释上下文：项目校验 + 合同只读重算 + 知识卡/蓝图/同卷概览。

    合同走 ``allocate_with_fallback`` 只读重算（与 revise 预览同一条路径，
    同蓝图同 seed 结果固定），不落库；所有查询带 course_id 过滤。
    """
    proj = session.execute(
        select(exam_projects.c.id).where(
            exam_projects.c.id == project_id,
            exam_projects.c.course_id == course_id,
        )
    ).one_or_none()
    if proj is None:
        raise ContractExplainError("项目不存在或不属于课程")

    bv_row = session.execute(
        select(
            blueprint_versions.c.catalog_version_id,
            blueprint_versions.c.chapter_weights,
        ).where(
            blueprint_versions.c.id == blueprint_version_id,
            blueprint_versions.c.course_id == course_id,
        )
    ).one_or_none()
    if bv_row is None:
        raise ContractExplainError("蓝图版本不存在或不属于课程")
    chapter_weights = dict(bv_row._mapping["chapter_weights"] or {})

    # 合同分配只读重算：确定性算法的真实产出，供模型解读，不落库
    try:
        contract, used_threshold, _history = allocate_with_fallback(
            session,
            blueprint_version_id=blueprint_version_id,
            course_id=course_id,
            allocation_seed=allocation_seed,
        )
    except ContractExecutionError as exc:
        raise ContractExplainError(str(exc)) from exc

    snap = contract.model_dump(mode="json")
    slots = snap.get("slots") or []
    slot = next(
        (s for s in slots if isinstance(s, dict) and s.get("item_index") == item_index),
        None,
    )
    if slot is None:
        raise ContractExplainError(f"item_index={item_index} 不在该合同槽位中")

    # 章名映射（anchor_key → content_domains.name），同卷概览与目标章都要用
    anchor_rows = session.execute(
        select(
            content_domains.c.framework_anchor_key,
            content_domains.c.name,
        ).where(
            content_domains.c.course_id == course_id,
            content_domains.c.catalog_version_id == bv_row._mapping["catalog_version_id"],
        )
    ).all()
    anchor_names = {
        r._mapping["framework_anchor_key"]: r._mapping["name"]
        for r in anchor_rows
        if r._mapping["framework_anchor_key"]
    }

    # 蓝图该题位的计划信息（计划分值/难度/题型；分配后可能因同章回补与槽位不同）
    plan_row = session.execute(
        select(
            plan_items.c.question_type,
            plan_items.c.assessment_mode,
            plan_items.c.score,
            plan_items.c.difficulty,
            plan_items.c.cognitive_level,
            plan_items.c.exam_point_id,
            plan_items.c.knowledge_card_id,
            plan_items.c.assessment_unit_id,
        ).where(
            plan_items.c.blueprint_version_id == blueprint_version_id,
            plan_items.c.course_id == course_id,
            plan_items.c.item_index == item_index,
        )
    ).one_or_none()
    plan_item = dict(plan_row._mapping) if plan_row is not None else None

    # 关联知识卡 grounding（槽位 card_id → knowledge_cards，带 course_id）
    card = None
    card_id = slot.get("card_id")
    if card_id:
        crow = session.execute(
            select(
                knowledge_cards.c.name,
                knowledge_cards.c.performance_statement,
                knowledge_cards.c.assessable_content,
                knowledge_cards.c.answer_proposition,
            ).where(
                knowledge_cards.c.id == card_id,
                knowledge_cards.c.course_id == course_id,
            )
        ).one_or_none()
        if crow is not None:
            card = dict(crow._mapping)

    # 目标章信息（章名与权重拿得到才带，拿不到不硬凑）
    anchor_key = str(slot.get("anchor_key") or "")
    chapter: dict = {"anchor_key": anchor_key}
    chapter_name = anchor_names.get(anchor_key)
    if chapter_name:
        chapter["name"] = chapter_name
    if anchor_key in chapter_weights:
        chapter["weight"] = chapter_weights[anchor_key]

    # 同卷其他槽位紧凑概览：模型解释「为何是这个原子而非别的」时的全局视野
    other_slots = [
        {
            "item_index": s.get("item_index"),
            "question_type": s.get("question_type"),
            "chapter": anchor_names.get(s.get("anchor_key")) or s.get("anchor_key"),
            "coverage_atom": str(s.get("coverage_atom") or "")[:_OTHER_SLOT_ATOM_LEN],
        }
        for s in slots
        if s.get("item_index") != item_index
    ]

    return {
        "project_id": project_id,
        "blueprint_version_id": blueprint_version_id,
        "allocation": {
            "allocation_seed": allocation_seed,
            "centrality_threshold_used": used_threshold,
        },
        "item_index": item_index,
        "slot": slot,
        "plan_item": plan_item,
        "chapter": chapter,
        "chapter_weights": chapter_weights,
        "card": card,
        "other_slots": other_slots,
    }


def build_explain_prompt(
    context: dict, instruction: str, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言槽位真实字段进了 prompt。"""
    card = context.get("card")
    card_section = None
    if isinstance(card, dict) and card:
        card_section = {
            "name": card.get("name"),
            "performance_statement": card.get("performance_statement"),
            "assessable_content": card.get("assessable_content") or [],
            "answer_proposition": card.get("answer_proposition"),
        }
    payload: dict = {
        "instruction": instruction,
        "slot": context.get("slot") or {},
        "plan_item": context.get("plan_item"),
        "chapter": context.get("chapter"),
        "chapter_weights": context.get("chapter_weights") or {},
        "knowledge_card": card_section,
        "other_slots": context.get("other_slots") or [],
        "allocation": context.get("allocation") or {},
    }
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 结果规整与校验（确定性收口）
# ---------------------------------------------------------------------------


def _known_item_indices(context: dict) -> set[int]:
    """给定数据里出现过的全部题位号：target_item_index 只允许落在其中。"""
    indices: set[int] = set()
    slot = context.get("slot")
    if isinstance(slot, dict) and isinstance(slot.get("item_index"), int):
        indices.add(slot["item_index"])
    for other in context.get("other_slots") or []:
        if isinstance(other, dict) and isinstance(other.get("item_index"), int):
            indices.add(other["item_index"])
    return indices


def normalize_result(raw, context: dict) -> dict:
    """把模型返回收敛为 {explanation, suggestions, instruction_response}。

    suggestions 非 list 时置空；非对象项、缺 suggestion 文本的项丢弃；
    target_item_index 不在给定槽位题位号里的一律归 null。
    """
    if not isinstance(raw, dict):
        raise ContractExplainError("模型未返回 JSON 对象")

    suggestions: list[dict] = []
    raw_suggestions = raw.get("suggestions")
    if isinstance(raw_suggestions, list):
        known = _known_item_indices(context)
        for item in raw_suggestions:
            if not isinstance(item, dict):
                continue
            suggestion = str(item.get("suggestion") or "").strip()
            if not suggestion:
                continue
            target = item.get("target_item_index")
            if not isinstance(target, int) or isinstance(target, bool) or target not in known:
                target = None
            suggestions.append(
                {
                    "concern": str(item.get("concern") or "").strip(),
                    "suggestion": suggestion,
                    "target_item_index": target,
                }
            )

    return {
        "explanation": str(raw.get("explanation") or "").strip(),
        "suggestions": suggestions,
        "instruction_response": str(raw.get("instruction_response") or "").strip(),
    }


def validate_result(result: dict) -> dict:
    """对解释结果跑结构门禁，返回 {passed, code, message}。"""
    explanation = str(result.get("explanation") or "").strip()
    if len(explanation) < _MIN_EXPLANATION_LEN:
        return {
            "passed": False,
            "code": "explanation_too_short",
            "message": f"解释正文为空或不足 {_MIN_EXPLANATION_LEN} 字",
        }
    for idx, item in enumerate(result.get("suggestions") or [], start=1):
        if not str(item.get("suggestion") or "").strip():
            return {
                "passed": False,
                "code": "suggestion_missing",
                "message": f"第 {idx} 条建议缺少 suggestion 文本",
            }
    return {"passed": True, "code": "ok", "message": "通过解释结构校验"}


# ---------------------------------------------------------------------------
# 解释执行
# ---------------------------------------------------------------------------


def run_explain(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    item_index: int,
    allocation_seed: int | None,
    blueprint_version_id: str,
    instruction: str,
    client,
) -> dict:
    """产出一次槽位解释；首提未过校验时带反馈纠错一次，仍未过则如实上报。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。
    """
    context = load_explain_context(
        session,
        course_id=course_id,
        project_id=project_id,
        item_index=item_index,
        allocation_seed=allocation_seed,
        blueprint_version_id=blueprint_version_id,
    )
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_explain_prompt(context, instruction)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    result = normalize_result(raw, context)
    validation = validate_result(result)

    if not validation["passed"]:
        system_prompt, retry_payload = build_explain_prompt(
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
        "project_id": project_id,
        "item_index": item_index,
        "instruction": instruction,
        "explanation": result["explanation"],
        "suggestions": result["suggestions"],
        "instruction_response": result["instruction_response"],
        "validated": bool(validation["passed"]),
    }


def execute_explain_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行槽位解释（不写任何业务表）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise ContractExplainError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_explain(
        session,
        course_id=str(payload["course_id"]),
        project_id=str(payload["project_id"]),
        item_index=int(payload["item_index"]),
        allocation_seed=(
            int(payload["allocation_seed"])
            if payload.get("allocation_seed") is not None
            else None
        ),
        blueprint_version_id=str(payload["blueprint_version_id"]),
        instruction=str(payload["instruction"]),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(
    project_id: str, item_index: int, allocation_seed: int | None, instruction: str
) -> str:
    return hashlib.sha256(
        f"explain:{project_id}:{item_index}:{allocation_seed}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_explain(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    item_index: int,
    allocation_seed: int | None,
    blueprint_version_id: str,
    instruction: str,
) -> str:
    """校验项目/蓝图/槽位并创建解释任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_ai_revise）：同槽位同方案同追问的**在途**任务复用，
    避免双击重复烧模型；已到终态的任务换一把新键，教师重新发起才能拿到新解释。
    """
    instruction = str(instruction or "").strip()

    # 状态校验与端点同口径（项目存在 / 蓝图可分配 / item_index 在槽位中）；
    # 装配结果不入队，worker 执行时再取最新（方案/蓝图可能刚被教师调整）。
    load_explain_context(
        session,
        course_id=course_id,
        project_id=project_id,
        item_index=item_index,
        allocation_seed=allocation_seed,
        blueprint_version_id=blueprint_version_id,
    )

    base_key = _task_key(project_id, item_index, allocation_seed, instruction)
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
            "item_index": item_index,
            "allocation_seed": allocation_seed,
            "blueprint_version_id": blueprint_version_id,
            "instruction": instruction,
        },
    )
