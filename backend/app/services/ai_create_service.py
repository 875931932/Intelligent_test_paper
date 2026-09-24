"""新增题目 AI 生成：整题提案生成与校验。

链路：POST .../items/ai-generate 建 task_runs → worker 调
``execute_ai_create_task`` 产出结构化提案（以该试卷现有题目清单 + 知识卡为
素材 grounding）→ ``validate_generated_question`` 确定性收口 → 前端把提案
回填「新增题目」表单 → 教师微调确认后走既有 POST .../items 落库。

本模块只产出提案，不写任何试卷数据；写入复用 paper_version_service 的
既有 create_paper_item（内部有 _validate_teacher_item）。

与 ai_revise_service 的关系：架构完全镜像（状态门禁 / prompt 纯函数 /
规整 / 校验收口 / 重试一次 / 幂等入队），形态规整直接复用其
``_align_options`` / ``_align_answer``，不复制第二份。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import (
    generated_questions,
    knowledge_cards,
    paper_items,
    paper_versions,
    task_runs,
)
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
# 复用改题侧的既有函数（模块级私有，跨服务 import 优于复制第二份）
from app.services.ai_revise_service import (
    _align_answer,
    _align_options,
    llm_configured,
)
from app.services.generation_service import validate_generated_question

TASK_TYPE = "ai_create_item"
_INPUT_VERSION = "ai_create_v1"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致

# 一期题型白名单：基础题型。综合题（分问/总分约束）与论述题不支持整题生成。
_SUPPORTED_QUESTION_TYPES = (
    "single_choice",
    "multiple_choice",
    "true_false",
    "fill_blank",
    "short_answer",
)
_CHOICE_TYPES = {"single_choice", "multiple_choice"}

# 选择题回填教师新增表单的固定形态（A-D 键对象）；模型返回 4 项列表时规整过来
_CHOICE_OPTION_SHAPE = {"A": "", "B": "", "C": "", "D": ""}

# 难度别名收敛到前端表单口径（easy/medium/hard）
_DIFFICULTY_ALIASES = {
    "low": "easy",
    "high": "hard",
    "容易": "easy",
    "易": "easy",
    "中等": "medium",
    "困难": "hard",
    "难": "hard",
}

# grounding 素材上限：现有题最多带 30 条、知识卡最多 6 张，避免 prompt 过长
_MAX_EXISTING_ITEMS = 30
_MAX_KNOWLEDGE_CARDS = 6
_STEM_PREVIEW_LEN = 40

# 模型超时 45s × 2 次尝试 × 2 轮（首提 + 带反馈纠错）最坏 ~180s，须留在租约内。
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

_SYSTEM_PROMPT = """你是高校期末试卷的学科编辑。教师给出要新出的一道题的要求，你在该试卷「已有题目清单」与「知识卡」的素材范围内新写一道完整的题。

硬性规则（违反即废稿）：
1. question_type 只能取这些值：single_choice（单选）/ multiple_choice（多选）/ true_false（判断）/ fill_blank（填空）/ short_answer（简答）。综合题、分问题、论述题一期不支持——收到此类要求时改出最接近的基础题型，不要返回这些类型。
2. 新题不得与「已有题目清单」中的题重复或换皮：考查点、题干情境、选项内容都要避开已有题。
3. 题干禁止来源话术：不得出现「根据课件/资料」「第X章/页/讲」「实验X」等表述。
4. 形态规则：
   - 选择题恰好四个互斥选项（options）；单选题正确答案唯一、多选题至少两项正确；answer 写选项字母（如 "A" 或 "AB"）。
   - 填空题题干恰好一个由连续4个以上下划线构成的空（____），answer 为简短术语（不超过20个汉字、不含标点）。
   - 判断题 answer 为布尔值 true/false，options 返回 null。
   - 简答题必须给出 explanation（解析）与 rubric（评分细则，字符串或要点数组）。
5. difficulty 只能取 easy / medium / hard；未指明时用 medium。
6. 若提供「知识卡」，考查内容应落在知识卡 assessable_content 范围内。
7. 不要返回 score——分值是教师的总分决策，由教师在表单里自己定。

只返回严格 JSON 对象：
{"question_type": "...", "stem": "...", "options": ..., "answer": ..., "explanation": "...", "rubric": ..., "difficulty": "...", "change_summary": "一两句话说明出了什么题、考什么"}
change_summary 面向教师可读，不进入卷面。"""


class AiCreateError(Exception):
    """AI 生成题目业务错误（消息可直接面向 API 层映射）。"""


class AiCreateConflict(AiCreateError):
    """状态冲突（如试卷已定稿）。"""


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def load_create_context(session: Session, *, course_id: str, paper_version_id: str) -> dict:
    """装配新增题上下文：试卷状态门禁 + 现有题目清单 + 知识卡 grounding。

    现有题清单是**素材约束**（防重复体验兜底），不是把合同去重算法挪进
    prompt——合同内去重仍由确定性算法负责。知识卡拿不到时返回空列表，
    纯指令生成，不报错。
    """
    pv = session.execute(
        select(paper_versions.c.status, paper_versions.c.generation_run_id).where(
            paper_versions.c.id == paper_version_id,
            paper_versions.c.course_id == course_id,
        )
    ).one_or_none()
    if pv is None:
        raise AiCreateError("试卷版本不存在或不属于课程")
    if pv._mapping["status"] == "finalized":
        raise AiCreateConflict("试卷已定稿，撤销定稿后才能 AI 生成题目")

    # 现有题目清单（生效题面：override 优先），题干截断前 40 字控 prompt 体量
    rows = session.execute(
        select(
            paper_items.c.display_order,
            paper_items.c.teacher_override,
            generated_questions.c.payload,
        )
        .select_from(paper_items)
        .join(generated_questions, generated_questions.c.id == paper_items.c.generated_question_id)
        .where(
            paper_items.c.paper_version_id == paper_version_id,
            paper_items.c.course_id == course_id,
        )
        .order_by(paper_items.c.display_order)
        .limit(_MAX_EXISTING_ITEMS)
    ).all()
    items: list[dict] = []
    for r in rows:
        override = r._mapping["teacher_override"] or {}
        payload = r._mapping["payload"] or {}
        stem = str(override.get("stem") or payload.get("stem") or "")
        items.append(
            {
                "item_index": r._mapping["display_order"],
                "question_type": override.get("question_type")
                or payload.get("question_type")
                or "",
                "stem": stem[:_STEM_PREVIEW_LEN],
            }
        )

    # 知识卡 grounding：取该试卷 generation_run 关联过的若干张卡（所有查询带 course_id）
    cards: list[dict] = []
    run_id = pv._mapping["generation_run_id"]
    if run_id:
        card_ids = session.execute(
            select(generated_questions.c.knowledge_card_id)
            .where(
                generated_questions.c.generation_run_id == run_id,
                generated_questions.c.course_id == course_id,
                generated_questions.c.knowledge_card_id.is_not(None),
            )
            .distinct()
            .limit(_MAX_KNOWLEDGE_CARDS)
        ).scalars().all()
        if card_ids:
            crows = session.execute(
                select(
                    knowledge_cards.c.name,
                    knowledge_cards.c.assessable_content,
                    knowledge_cards.c.prompt_material,
                ).where(
                    knowledge_cards.c.id.in_(card_ids),
                    knowledge_cards.c.course_id == course_id,
                )
            ).all()
            cards = [dict(c._mapping) for c in crows]

    return {"items": items, "cards": cards}


def build_create_prompt(
    instruction: str, context: dict, *, previous_error: str = ""
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言现有题清单进了 prompt。"""
    payload: dict = {
        "instruction": instruction,
        "existing_items": context.get("items") or [],
    }
    cards = context.get("cards")
    if cards:
        payload["knowledge_cards"] = [
            {
                "name": c.get("name"),
                "assessable_content": c.get("assessable_content") or [],
                "prompt_material": c.get("prompt_material") or [],
            }
            for c in cards
        ]
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 提案规整与校验（确定性收口）
# ---------------------------------------------------------------------------


def normalize_proposal(raw) -> dict:
    """把模型返回收敛为完整提案；形态对齐教师新增表单口径。"""
    if not isinstance(raw, dict):
        raise AiCreateError("模型未返回 JSON 对象")
    question_type = str(raw.get("question_type") or "").strip()
    difficulty = str(raw.get("difficulty") or "").strip().lower() or "medium"
    difficulty = _DIFFICULTY_ALIASES.get(difficulty, difficulty)
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"

    options = raw.get("options")
    if question_type in _CHOICE_TYPES:
        if options is not None:
            # 模型常返回 4 项列表，规整回教师表单的 A-D 对象形态；数量问题交给校验
            options = _align_options(options, _CHOICE_OPTION_SHAPE)
    else:
        options = None

    answer = raw.get("answer")
    if question_type == "true_false":
        # 判断题答案必须布尔化：复用改题侧的字面量规整。新增题没有原题可参照，
        # current_answer=True 只是「需要布尔化」的信号，非法字面量原样留给校验拦。
        answer = _align_answer(answer, current_answer=True, question_type=question_type)

    return {
        "question_type": question_type,
        "stem": str(raw.get("stem") or ""),
        "options": options,
        "answer": answer,
        "explanation": str(raw.get("explanation") or ""),
        "difficulty": difficulty,
        "rubric": raw.get("rubric"),
    }


def validate_proposal(proposal: dict) -> dict:
    """对新题提案跑单题质量门禁，返回 {passed, code, message}。

    新增题没有合同槽位，atom_text 为空；分值不进提案（教师在表单里定），
    校验时 score 给 0——validate_generated_question 只在综合题分问求和时用到分值。
    rubric 随提案回填表单、经 POST items 落库（简答/综合的硬校验仍在提案期把关）。
    """
    question_type = str(proposal.get("question_type") or "")
    if question_type not in _SUPPORTED_QUESTION_TYPES:
        return {
            "passed": False,
            "code": "question_type_unsupported",
            "message": "暂不支持题型 "
            f"{question_type or '(空)'}，一期仅支持 {'/'.join(_SUPPORTED_QUESTION_TYPES)}",
        }
    question = {
        "question_type": question_type,
        "difficulty": proposal.get("difficulty") or "medium",
        "score": 0,
        "stem": proposal.get("stem") or "",
        "options": proposal.get("options"),
        "answer": proposal.get("answer"),
        "explanation": proposal.get("explanation") or "",
    }
    if proposal.get("rubric"):
        question["rubric"] = proposal["rubric"]

    result = validate_generated_question(question, atom_text="")
    if result.get("status") == "blocker":
        return {"passed": False, "code": result.get("code", ""), "message": result.get("message", "")}
    return {"passed": True, "code": "ok", "message": "通过基础质量检查"}


# ---------------------------------------------------------------------------
# 提案执行
# ---------------------------------------------------------------------------


def run_ai_create(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    instruction: str,
    client,
) -> dict:
    """产出一次整题提案；首提未过校验时带反馈纠错一次，仍未过则如实上报。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。
    """
    context = load_create_context(
        session, course_id=course_id, paper_version_id=paper_version_id
    )
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_create_prompt(instruction, context)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    proposal = normalize_proposal(raw)
    validation = validate_proposal(proposal)
    attempts = 1

    if not validation["passed"]:
        system_prompt, retry_payload = build_create_prompt(
            instruction, context, previous_error=validation["message"]
        )
        raw = client.request_json(
            system_prompt=system_prompt,
            payload=retry_payload,
            temperature=0.4,
            call_context=call_context,
        )
        proposal = normalize_proposal(raw)
        validation = validate_proposal(proposal)
        attempts = 2

    return {
        "instruction": instruction,
        "proposal": proposal,
        "validation": validation,
        "change_summary": str(raw.get("change_summary") or ""),
        "attempts": attempts,
    }


def execute_ai_create_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行整题提案（不写试卷数据）。"""
    from app.adapters.model.llm_gateway import LLMJsonClient
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise AiCreateError("LLM model is not configured")

    client = LLMJsonClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        disable_thinking=settings.llm_disable_thinking,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        max_attempts=_CLIENT_MAX_ATTEMPTS,
        recorder=DatabaseModelCallRecorder(get_session_factory()),
    )
    return run_ai_create(
        session,
        course_id=str(payload["course_id"]),
        paper_version_id=str(payload["paper_version_id"]),
        instruction=str(payload["instruction"]),
        client=client,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(paper_version_id: str, instruction: str) -> str:
    return hashlib.sha256(
        f"ai-create:{paper_version_id}:{instruction}".encode()
    ).hexdigest()[:24]


def enqueue_ai_create(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    instruction: str,
) -> str:
    """校验状态并创建生成任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_ai_revise）：同卷同要求的**在途**任务复用，
    避免双击重复烧模型；已到终态的任务换一把新键，教师重发同一要求
    才能真正拿到新提案，而不是旧任务的完成态。
    """
    instruction = str(instruction or "").strip()
    if not instruction:
        raise AiCreateError("生成要求不能为空")

    # 状态校验与 create_paper_item 同口径（试卷存在 / 未定稿）；
    # 上下文不入队，worker 执行时再取最新（教师可能刚改过题）。
    load_create_context(
        session, course_id=course_id, paper_version_id=paper_version_id
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
