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
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.schema import (
    assessment_units,
    content_domains,
    exam_points,
    generation_runs,
    generated_questions,
    knowledge_cards,
    knowledge_catalog_versions,
    paper_items,
    paper_versions,
    plan_items,
    task_runs,
)
from app.domain.blueprint.models import UnitCoverage
from app.domain.generation.contract import (
    ContractSlot,
    boundaries_overlap,
    boundaries_share_proposition,
    build_exam_point_pools,
)
from app.domain.generation.coverage import normalized_atom_key
from app.domain.generation.difficulty_standard import (
    COGNITIVE_LEVELS,
    DIFFICULTY_COGNITIVE_ALLOWED,
    canonical_difficulty,
    resolve_difficulty_tier,
)
from app.domain.model_calls import ModelCallContext
from app.infrastructure.tasks.models import TERMINAL_TASK_STATUSES, create_task_run
from app.services.generation_service import validate_generated_question

TASK_TYPE = "ai_revise_item"
# v2：请求载荷新增可选 targets（难度/认知/考点目标属性），旧任务不受影响
_INPUT_VERSION = "ai_revise_v2"
_TASK_LEASE_SECONDS = 300  # 与 worker.py 的 _LEASE_SECONDS_BY_TYPE 保持一致

# 内容提案字段：题型/分值不给改；难度/认知/考点只经教师显式 targets 调整，
# 由系统确定性写进提案属性（模型永远只写题面内容）。
_PROPOSAL_KEYS = ("stem", "options", "answer", "explanation")
# 提案携带的目标属性键（与 current 同名同口径，前端 diff 按同键比较）
_PROPOSAL_ATTR_KEYS = (
    "difficulty", "cognitive_level",
    "exam_point_id", "exam_point_title", "exam_point_code",
    "knowledge_card_id", "coverage_atom", "answer_boundary", "unit_id", "backfilled_from",
)

_TRUE_LITERALS = {"true", "t", "1", "yes", "y", "正确", "对"}
_FALSE_LITERALS = {"false", "f", "0", "no", "n", "错误", "错"}

# 模型超时 45s × 2 次尝试 × 2 轮（首提 + 带反馈纠错）最坏 ~180s，须留在租约内。
_CLIENT_TIMEOUT_SECONDS = 45.0
_CLIENT_MAX_ATTEMPTS = 2

_SYSTEM_PROMPT = """你是高校期末试卷的学科编辑。教师给出对某一道题的修改要求，你在试卷合同（逐题锁定的约束）与知识卡的范围内改写这道题。

硬性规则（违反即废稿）：
1. 只修改教师要求涉及的内容；未要求改动的字段原样返回。
2. 题型、分值不得改变——它们由试卷合同锁定。难度、认知层级、考查原子默认同样锁定；仅当 payload 出现 teacher_target 块时，按其中的目标重写本题——目标属性由系统确定性落库，你的职责是让题面、选项、答案、解析与目标难度档、认知要求和考查原子一致（例如目标更高档时补足推理步骤，换考点时严格围绕新的考查原子与答案域）。
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
    """判定收口在 llm_registry（DB 绑定优先，.env 兜底）；薄包装保留给 router 与测试打桩。"""
    from app.services.llm_registry import llm_ready

    return llm_ready("default")


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

    def pick_any(key: str):
        # 追踪字段（考点/原子/边界）：override 显式 null 视同未覆盖，回落 payload
        return override.get(key) or payload.get(key)

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
        # 追踪字段：换考点提案的 diff 基线与撤销回填都依赖它们（payload 是生成时快照）
        "exam_point_id": pick_any("exam_point_id"),
        "exam_point_title": pick_any("exam_point_title"),
        "exam_point_code": pick_any("exam_point_code"),
        "knowledge_card_id": pick_any("knowledge_card_id") or row._mapping["knowledge_card_id"],
        "coverage_atom": pick_any("coverage_atom"),
        "answer_boundary": pick_any("answer_boundary"),
        "unit_id": pick_any("unit_id"),
        "backfilled_from": pick_any("backfilled_from"),
    }

    slot = _contract_slot(
        session,
        course_id=course_id,
        generation_run_id=pv._mapping["generation_run_id"],
        plan_item_index=row._mapping["plan_item_index"],
    )
    # 遗留数据 payload 无考点快照时回落合同槽位（与读端派生同序：override→payload→slot）
    if not current["exam_point_id"] and isinstance(slot, dict):
        current["exam_point_id"] = slot.get("exam_point_id")

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

    return {
        "item_index": item_index,
        "plan_item_index": row._mapping["plan_item_index"],
        "current": current,
        "slot": slot,
        "card": card,
    }


def build_revise_prompt(
    context: dict, instruction: str, *, previous_error: str = "", target: dict | None = None
) -> tuple[str, dict]:
    """组装 (system_prompt, payload)。纯函数，便于断言合同字段进了 prompt。

    ``target`` 为教师显式调整的目标属性（见 ``resolve_revise_targets``）：
    难度/认知/原子/答案域按目标覆写进 contract，知识卡换成目标卡，
    并附 teacher_target 块告知模型哪些锁定被解禁。
    """
    slot = context.get("slot")
    target = target or {}
    contract = None
    if isinstance(slot, dict) and slot or target:
        base = slot if isinstance(slot, dict) else {}
        contract = {
            "question_type": base.get("question_type"),
            "difficulty": target.get("difficulty") or base.get("difficulty"),
            "cognitive_level": target.get("cognitive_level") or base.get("cognitive_level"),
            "score": base.get("score"),
            "coverage_atom": target.get("coverage_atom") or base.get("coverage_atom"),
            "answer_boundary": target.get("answer_boundary") or base.get("answer_boundary"),
            "forbidden_context": target.get("forbidden_context") or base.get("forbidden_context") or {},
            "performance_statement": base.get("performance_statement"),
            "preferred_terms": base.get("preferred_terms") or [],
        }
    card = target.get("card_section") or context.get("card")
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
    if target:
        teacher_target: dict = {}
        if target.get("difficulty"):
            teacher_target["difficulty"] = target["difficulty"]
        if target.get("cognitive_level"):
            teacher_target["cognitive_level"] = target["cognitive_level"]
        if target.get("exam_point_id"):
            teacher_target["exam_point"] = {
                "title": target.get("exam_point_title"),
                "coverage_atom": target.get("coverage_atom"),
                "answer_boundary": target.get("answer_boundary"),
            }
        teacher_target["note"] = (
            "教师显式调整的目标属性：难度/认知层级/考查原子以本块与 contract 中的对应值为准，"
            "原题的这些属性作废；题型与分值仍锁定。"
        )
        payload["teacher_target"] = teacher_target
    if previous_error:
        payload["previous_validation_error"] = previous_error
    return _SYSTEM_PROMPT, payload


# ---------------------------------------------------------------------------
# 目标属性解析（教师显式调整：难度 / 认知层级 / 考点）
# ---------------------------------------------------------------------------


def _all_contract_slots(session, *, course_id: str, generation_run_id: str | None) -> list[dict]:
    """合同快照的全部槽位（原始 dict），供互斥预检与禁用上下文重算。"""
    if not generation_run_id:
        return []
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
    slots = (snap or {}).get("slots")
    if not isinstance(slots, list):
        return []
    return [s for s in slots if isinstance(s, dict)]


def _catalog_version_id(session, *, course_id: str, generation_run_id: str | None) -> str | None:
    """试卷 → 生成运行 → 目录（本卷命题输入的冻结版本，不追新目录）。"""
    if not generation_run_id:
        return None
    return session.execute(
        select(generation_runs.c.catalog_version_id).where(
            generation_runs.c.id == generation_run_id,
            generation_runs.c.course_id == course_id,
        )
    ).scalar_one_or_none()


def _load_catalog_context(
    session, *, course_id: str, catalog_version_id: str
) -> tuple[dict, dict, dict, dict]:
    """按蓝图目录装配原子池（与合同分配同源数据：unit 全量 + 卡全量）。

    返回 (pools, units_map, cards_dict, ep_meta)。pools 走 build_exam_point_pools
    且 threshold=0——教师显式指定的目标卡不再受核心度门槛过滤；核心度门槛是
    分配器的自动选卡策略，不是教师的选卡资格线。
    """
    fw_id = session.execute(
        select(knowledge_catalog_versions.c.framework_version_id).where(
            knowledge_catalog_versions.c.id == catalog_version_id,
            knowledge_catalog_versions.c.course_id == course_id,
        )
    ).scalar_one_or_none()

    ep_meta: dict[str, dict] = {}
    if fw_id:
        for r in session.execute(
            select(exam_points.c.id, exam_points.c.code, exam_points.c.title)
            .where(
                exam_points.c.course_id == course_id,
                exam_points.c.framework_version_id == fw_id,
                exam_points.c.status == "confirmed",
            )
            .order_by(exam_points.c.code)
        ).all():
            ep_meta[str(r[0])] = {"code": r[1], "title": r[2]}

    unit_rows = session.execute(
        select(
            assessment_units.c.id.label("au_id"),
            assessment_units.c.exam_point_id,
            content_domains.c.framework_anchor_key,
        )
        .select_from(assessment_units)
        .join(
            content_domains,
            content_domains.c.id == assessment_units.c.content_domain_id,
            isouter=True,
        )
        .where(
            assessment_units.c.catalog_version_id == catalog_version_id,
            assessment_units.c.course_id == course_id,
            assessment_units.c.status == "active",
        )
        .order_by(assessment_units.c.code)
    ).all()
    units_map: dict[str, dict] = {}
    for r in unit_rows:
        au_id = r._mapping["au_id"]
        units_map[str(au_id)] = {
            "unit_id": str(au_id),
            "exam_point_id": str(r._mapping["exam_point_id"] or ""),
            "anchor_key": str(r._mapping["framework_anchor_key"] or au_id),
        }

    card_rows = session.execute(
        select(
            knowledge_cards.c.id,
            knowledge_cards.c.name,
            knowledge_cards.c.performance_statement,
            knowledge_cards.c.assessable_content,
            knowledge_cards.c.scope_boundary,
            knowledge_cards.c.cognitive_targets,
            knowledge_cards.c.allowed_question_types,
            knowledge_cards.c.importance,
            knowledge_cards.c.concept_cluster,
            knowledge_cards.c.answer_proposition,
            knowledge_cards.c.prompt_material,
            knowledge_cards.c.relation_edges,
            knowledge_cards.c.assessment_unit_id,
        ).where(
            knowledge_cards.c.catalog_version_id == catalog_version_id,
            knowledge_cards.c.course_id == course_id,
        )
    ).all()
    cards_dict: dict[str, dict] = {}
    card_ids_by_unit: dict[str, list[str]] = {}
    for r in card_rows:
        c = r._mapping
        cid = str(c["id"])
        cards_dict[cid] = {
            "name": c.get("name") or "",
            "performance_statement": c.get("performance_statement") or "",
            "assessable_content": list(c.get("assessable_content") or []),
            "scope_boundary": c.get("scope_boundary") or {},
            "cognitive_targets": list(c.get("cognitive_targets") or []),
            "allowed_question_types": list(c.get("allowed_question_types") or []),
            "importance": c.get("importance") or 1,
            "concept_cluster": c.get("concept_cluster") or "",
            "answer_proposition": c.get("answer_proposition") or "",
            # 卡无独立答案域列：与合同装配同口径，answer_proposition 即答案域
            "answer_boundary": c.get("answer_proposition") or "",
            "prompt_material": list(c.get("prompt_material") or []),
            "relation_edges": list(c.get("relation_edges") or []),
        }
        cards_dict[cid].setdefault("is_core", True)
        au = c.get("assessment_unit_id")
        if au:
            card_ids_by_unit.setdefault(str(au), []).append(cid)

    units = [
        UnitCoverage(
            unit_id=uid,
            exam_point_id=u["exam_point_id"],
            anchor_key=u["anchor_key"],
            # 与合同装配同款占位兜底：UnitCoverage 不允许空卡列表；
            # 占位 id 查不到真实卡，build_exam_point_pools 自然跳过，不产出原子
            card_ids=card_ids_by_unit.get(uid) or [f"__placeholder_{uid}"],
        )
        for uid, u in units_map.items()
    ]
    pools = build_exam_point_pools(units, cards_dict, threshold=0.0)
    return pools, units_map, cards_dict, ep_meta


def _other_question_traces(
    session,
    *,
    course_id: str,
    paper_version_id: str,
    self_item_index: int,
    self_plan_index: int | None,
    slots: list[dict],
) -> tuple[set[str], list[tuple[str, str]]]:
    """其他题位的 (原子键集合, [(标签, 答案域)...])：合同槽位 ∪ 生效题面取并集。

    口径与 audit_paper_against_contract 的 atom_uniqueness / answer_mutex 同源
    （槽位是合同权威，题面含教师改题后的覆盖值）。预检与终检必须同源——
    否则出现"预检放过、终检拦下"的漂移。
    """
    used_atoms: set[str] = set()
    others: list[tuple[str, str]] = []
    self_plan = self_plan_index if self_plan_index is not None else -1
    for s in slots:
        idx = s.get("item_index")
        if idx is not None and int(idx) == self_plan:
            continue
        atom = str(s.get("coverage_atom") or "")
        if atom:
            used_atoms.add(normalized_atom_key(atom))
        boundary = str(s.get("answer_boundary") or "")
        if boundary:
            others.append((f"合同题位 {idx}", boundary))

    rows = session.execute(
        select(
            paper_items.c.display_order,
            paper_items.c.teacher_override,
            generated_questions.c.payload,
        )
        .select_from(paper_items)
        .join(
            generated_questions,
            generated_questions.c.id == paper_items.c.generated_question_id,
        )
        .where(
            paper_items.c.paper_version_id == paper_version_id,
            paper_items.c.course_id == course_id,
        )
    ).all()
    for r in rows:
        idx = int(r._mapping["display_order"])
        if idx == self_item_index:
            continue
        override = r._mapping["teacher_override"] or {}
        payload = r._mapping["payload"] or {}
        atom = str(override.get("coverage_atom") or payload.get("coverage_atom") or "")
        if atom:
            used_atoms.add(normalized_atom_key(atom))
        boundary = str(override.get("answer_boundary") or payload.get("answer_boundary") or "")
        if boundary:
            others.append((f"第 {idx} 题", boundary))
    return used_atoms, others


def _boundary_conflict(boundary: str, others: list[tuple[str, str]]) -> str | None:
    """目标答案域与任一他题答案域重叠或共享命题 → 返回冲突方标签。"""
    for label, other in others:
        if not other:
            continue
        if boundaries_overlap(boundary, other) or boundaries_share_proposition(boundary, other):
            return label
    return None


def resolve_revise_targets(
    session,
    *,
    course_id: str,
    paper_version_id: str,
    context: dict,
    targets: dict | None,
) -> dict | None:
    """教师目标属性 → 确定性解析结果；无目标返回 None（纯内容改题）。

    预检全部发生在模型调用之前，违反即拒绝（AiReviseError → 422）：
    难度↔认知允许集、目标原子全卷唯一、目标答案域与全卷其他题不重叠不
    共享命题、目标考点池内有可用卡。模型永远只写题面内容，属性由本函数
    确定性给出（系统盖章落库，不进 prompt 让模型自觉）。
    """
    if not targets:
        return None
    if not isinstance(targets, dict):
        raise AiReviseError("targets 必须是对象")
    targets = {k: v for k, v in targets.items() if v not in (None, "")}
    if not targets:
        return None
    unknown = set(targets) - {"difficulty", "cognitive_level", "exam_point_id", "card_id"}
    if unknown:
        raise AiReviseError(f"不支持的目标字段：{', '.join(sorted(unknown))}")

    current = context.get("current") or {}
    self_slot = context.get("slot") if isinstance(context.get("slot"), dict) else {}
    out: dict = {}

    # ---- 难度 / 认知：词表归一 + 允许集，先于模型调用确定性拒绝 ----
    raw_diff = targets.get("difficulty")
    raw_cog = targets.get("cognitive_level")
    eff_diff = canonical_difficulty(current.get("difficulty"))
    eff_cog = str(current.get("cognitive_level") or "")
    if raw_diff is not None:
        tier = resolve_difficulty_tier(raw_diff)
        if tier in (None, "__invalid__"):
            raise AiReviseError(
                f"难度目标 {raw_diff!r} 非法（low/medium/high，或 easy/hard 等别名）"
            )
        out["difficulty"] = tier
        eff_diff = tier
    if raw_cog is not None:
        cog = str(raw_cog).strip()
        if cog not in COGNITIVE_LEVELS:
            raise AiReviseError(
                f"认知层级目标 {raw_cog!r} 不在布鲁姆六级词表（{'/'.join(COGNITIVE_LEVELS)}）"
            )
        out["cognitive_level"] = cog
        eff_cog = cog
    if (raw_diff is not None or raw_cog is not None) and eff_cog:
        allowed = DIFFICULTY_COGNITIVE_ALLOWED.get(eff_diff)
        if allowed is not None and eff_cog not in allowed:
            raise AiReviseError(
                f"难度「{eff_diff}」不允许认知层级「{eff_cog}」"
                f"（允许：{'/'.join(sorted(allowed))}）；请同时调整认知层级或难度目标"
            )

    # ---- 换考点 / 换知识卡 ----
    ep_raw = targets.get("exam_point_id")
    card_raw = targets.get("card_id")
    if ep_raw is None and card_raw is not None:
        raise AiReviseError("指定知识卡必须同时给出目标考点 exam_point_id")
    if ep_raw is not None:
        ep_id = str(ep_raw).strip()
        run_id = session.execute(
            select(paper_versions.c.generation_run_id).where(
                paper_versions.c.id == paper_version_id,
                paper_versions.c.course_id == course_id,
            )
        ).scalar_one_or_none()
        catalog_id = _catalog_version_id(
            session, course_id=course_id, generation_run_id=run_id
        )
        if not catalog_id:
            raise AiReviseError("该试卷没有蓝图目录，无法换考点")
        pools, units_map, cards_dict, ep_meta = _load_catalog_context(
            session, course_id=course_id, catalog_version_id=catalog_id
        )
        pool = pools.get(ep_id)
        if not pool:
            title = (ep_meta.get(ep_id) or {}).get("title") or ep_id
            raise AiReviseError(f"考点「{title}」下没有可用知识原子（无单元/无卡/原子为空）")

        slots_all = _all_contract_slots(
            session, course_id=course_id, generation_run_id=run_id
        )
        used_atoms, others = _other_question_traces(
            session,
            course_id=course_id,
            paper_version_id=paper_version_id,
            self_item_index=int(context.get("item_index") or -1),
            self_plan_index=context.get("plan_item_index"),
            slots=slots_all,
        )
        current_atom_key = normalized_atom_key(str(current.get("coverage_atom") or ""))
        picked = None
        last_conflict = ""
        boundary_cache: dict[str, str | None] = {}
        for atom in pool:
            if card_raw and atom.card_id != str(card_raw):
                continue
            # 本题当前原子不参与复用判定：换卡/换考点时它天然不被重选
            if atom.atom_key and atom.atom_key == current_atom_key:
                continue
            if atom.atom_key in used_atoms:
                continue
            if atom.card_id not in boundary_cache:
                boundary_cache[atom.card_id] = _boundary_conflict(atom.boundary, others)
            if boundary_cache[atom.card_id]:
                last_conflict = (
                    f"知识卡「{(cards_dict.get(atom.card_id) or {}).get('name') or atom.card_id}」"
                    f"答案域与{boundary_cache[atom.card_id]}重叠或共享命题"
                )
                continue
            picked = atom
            break
        ep_title = (ep_meta.get(ep_id) or {}).get("title") or ep_id
        if picked is None:
            if card_raw:
                raise AiReviseError(
                    f"目标知识卡在考点「{ep_title}」下没有不冲突的可用原子"
                    + (f"：{last_conflict}" if last_conflict else "")
                )
            raise AiReviseError(
                (f"考点「{ep_title}」内没有不与他题冲突的可用原子（{last_conflict}）")
                if last_conflict
                else f"考点「{ep_title}」内的原子均已被其他题占用"
            )

        unit = units_map.get(picked.unit_id) or {}
        card = cards_dict.get(picked.card_id) or {}
        meta = ep_meta.get(ep_id) or {}
        out.update({
            "exam_point_id": ep_id,
            "exam_point_title": meta.get("title"),
            "exam_point_code": meta.get("code"),
            "knowledge_card_id": picked.card_id,
            "coverage_atom": picked.atom_text,
            "answer_boundary": picked.boundary,
            "unit_id": picked.unit_id,
            "anchor_key": unit.get("anchor_key") or picked.unit_id,
            "card_name": card.get("name") or picked.card_id,
            "card_section": card or None,
        })
        # 换考点后同考点兄弟集合变化：禁用上下文按目标考点重算（供 prompt 约束）
        self_plan = context.get("plan_item_index")
        siblings = [
            s for s in slots_all
            if str(s.get("exam_point_id") or "") == ep_id
            and (s.get("item_index") is None or int(s["item_index"]) != (self_plan if self_plan is not None else -1))
        ]
        out["forbidden_context"] = {
            "atoms": [str(s.get("coverage_atom") or "") for s in siblings],
            "answer_cores": [
                str(s.get("answer_boundary") or "") for s in siblings if s.get("answer_boundary")
            ],
        }
        # 跨考点改考打回补标记（与生成期自愈同机制）：合同审计按原考点计数，
        # 回补明细单列供教师与评审可见，不静默改写合同配比
        cur_ep = str(current.get("exam_point_id") or self_slot.get("exam_point_id") or "")
        if ep_id != cur_ep:
            out["backfilled_from"] = {
                "from_exam_point_id": cur_ep,
                "to_exam_point_id": ep_id,
                "anchor_key": out["anchor_key"],
            }
        else:
            out["backfilled_from"] = current.get("backfilled_from")

    return out


def list_revise_targets(
    session, *, course_id: str, paper_version_id: str, item_index: int
) -> dict:
    """换考点目标菜单：按本卷蓝图目录确定性列出各考点下可用的知识卡与原子。

    可用判据与 resolve_revise_targets 完全同源（原子未被占用、答案域不与他题
    冲突、排除本题当前原子）——菜单里能选的，提交后预检必过；不可用的给出原因。
    """
    context = load_revise_context(
        session, course_id=course_id, paper_version_id=paper_version_id, item_index=item_index
    )
    run_id = session.execute(
        select(paper_versions.c.generation_run_id).where(
            paper_versions.c.id == paper_version_id,
            paper_versions.c.course_id == course_id,
        )
    ).scalar_one_or_none()
    catalog_id = _catalog_version_id(session, course_id=course_id, generation_run_id=run_id)
    if not catalog_id:
        return {"exam_points": [], "unavailable_exam_points": [], "note": "该试卷没有蓝图目录"}

    pools, _units, cards_dict, ep_meta = _load_catalog_context(
        session, course_id=course_id, catalog_version_id=catalog_id
    )
    slots_all = _all_contract_slots(session, course_id=course_id, generation_run_id=run_id)
    current = context.get("current") or {}
    used_atoms, others = _other_question_traces(
        session,
        course_id=course_id,
        paper_version_id=paper_version_id,
        self_item_index=int(context.get("item_index") or -1),
        self_plan_index=context.get("plan_item_index"),
        slots=slots_all,
    )
    current_atom_key = normalized_atom_key(str(current.get("coverage_atom") or ""))
    current_ep = str(current.get("exam_point_id") or "")

    def _meta(ep_id: str) -> dict:
        return ep_meta.get(ep_id) or {}

    # 排序：有 code 按 code；无 code（目录外但池子里存在的遗留考点）排前，纯展示语义
    out_points: list[dict] = []
    unavailable: list[dict] = []
    all_eps = sorted(
        set(ep_meta) | set(pools),
        key=lambda e: (str(_meta(e).get("code") or ""), e),
    )
    for ep_id in all_eps:
        meta = _meta(ep_id)
        pool = pools.get(ep_id) or []
        cards_out: dict[str, dict] = {}
        blocked = ""
        seen: set[str] = set()
        boundary_cache: dict[str, str | None] = {}
        for atom in pool:
            if atom.card_id in seen:
                continue
            if atom.atom_key and atom.atom_key == current_atom_key:
                continue
            if atom.atom_key in used_atoms:
                continue
            if atom.card_id not in boundary_cache:
                boundary_cache[atom.card_id] = _boundary_conflict(atom.boundary, others)
            if boundary_cache[atom.card_id]:
                seen.add(atom.card_id)
                blocked = blocked or str(boundary_cache[atom.card_id])
                continue
            seen.add(atom.card_id)
            cards_out[atom.card_id] = {
                "card_id": atom.card_id,
                "card_name": (cards_dict.get(atom.card_id) or {}).get("name") or atom.card_id,
                "atom": atom.atom_text,
            }
        entry = {
            "id": ep_id,
            "code": meta.get("code"),
            "title": meta.get("title"),
            "current": ep_id == current_ep,
            "cards": list(cards_out.values()),
        }
        if entry["cards"]:
            out_points.append(entry)
        else:
            unavailable.append({
                "id": ep_id,
                "code": meta.get("code"),
                "title": meta.get("title"),
                "current": ep_id == current_ep,
                "reason": (
                    f"无可用原子：与{blocked}重叠或共享命题" if blocked
                    else ("原子均已被其他题占用" if pool else "该考点下没有知识卡")
                ),
            })
    return {"exam_points": out_points, "unavailable_exam_points": unavailable}


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


def normalize_proposal(raw, *, current: dict, target: dict | None = None) -> dict:
    """把模型返回收敛为四字段内容提案 + 系统盖章的目标属性。

    模型永远只写题面内容；难度/认知/考点等属性由 resolve_revise_targets
    确定性给出（target），无目标时与原题一致（前端 diff 为空）。
    """
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
    # 属性盖章（确定性，不信任模型）：目标覆盖原题，缺省沿用原题
    target = target or {}
    for key in _PROPOSAL_ATTR_KEYS:
        proposal[key] = target.get(key, current.get(key))
    return proposal


def validate_proposal(proposal: dict, *, current: dict, atom_text: str = "") -> dict:
    """对「提案合并后的生效题」跑单题质量门禁，返回 {passed, code, message}。"""
    question = {
        "question_type": current.get("question_type"),
        # 难度/认知取提案盖章后的生效值（含教师目标调整），让终检按目标档位把关
        "difficulty": proposal.get("difficulty") or current.get("difficulty") or "medium",
        "cognitive_level": proposal.get("cognitive_level") or current.get("cognitive_level") or "",
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
    targets: dict | None = None,
) -> dict:
    """产出一次改题提案；首提未过校验时带反馈纠错一次，仍未过则如实上报。

    ``client`` 为 ``LLMJsonClient``（或测试注入的同接口桩）。
    ``targets`` 为教师显式的目标属性（难度/认知/考点），确定性解析与预检
    在模型调用之前完成，违反即拒绝（AiReviseError → 422）。
    """
    context = load_revise_context(
        session, course_id=course_id, paper_version_id=paper_version_id, item_index=item_index
    )
    target = resolve_revise_targets(
        session,
        course_id=course_id,
        paper_version_id=paper_version_id,
        context=context,
        targets=targets,
    )
    current = context["current"]
    atom_text = str(
        (target or {}).get("coverage_atom")
        or (context.get("slot") or {}).get("coverage_atom")
        or current.get("coverage_atom")
        or ""
    )
    call_context = ModelCallContext(course_id=course_id, stage=TASK_TYPE)

    system_prompt, payload = build_revise_prompt(context, instruction, target=target)
    raw = client.request_json(
        system_prompt=system_prompt,
        payload=payload,
        temperature=0.4,
        call_context=call_context,
    )
    proposal = normalize_proposal(raw, current=current, target=target)
    validation = validate_proposal(proposal, current=current, atom_text=atom_text)
    attempts = 1

    if not validation["passed"]:
        system_prompt, retry_payload = build_revise_prompt(
            context, instruction, previous_error=validation["message"], target=target
        )
        raw = client.request_json(
            system_prompt=system_prompt,
            payload=retry_payload,
            temperature=0.4,
            call_context=call_context,
        )
        proposal = normalize_proposal(raw, current=current, target=target)
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
        "target": target,
    }


def execute_ai_revise_task(session: Session, *, payload: dict) -> dict:
    """worker 入口：构造真实 LLM 客户端并执行改题提案（不写试卷数据）。"""
    from app.adapters.model.client_factory import build_llm_client
    from app.config import settings
    from app.db.session import get_session_factory
    from app.services.model_call_service import DatabaseModelCallRecorder

    if not llm_configured():
        raise AiReviseError("LLM model is not configured")

    client = build_llm_client(
        "default",
        disable_thinking=settings.llm_disable_thinking,
        # 思考档钉死 low（优先于 disable_thinking 的档案缺省档，见 config 注释）
        reasoning_effort=settings.ai_tool_reasoning_effort,
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
        targets=payload.get("targets") or None,
    )


# ---------------------------------------------------------------------------
# 任务入队
# ---------------------------------------------------------------------------


def _task_key(paper_version_id: str, item_index: int, instruction: str, targets: dict | None = None) -> str:
    # targets 参与幂等键：同题同要求但目标属性不同是两件事，不能复用同一在途任务
    target_part = json.dumps(targets or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(
        f"ai-revise:{paper_version_id}:{item_index}:{instruction}:{target_part}".encode()
    ).hexdigest()[:24]


def enqueue_ai_revise(
    session: Session,
    *,
    course_id: str,
    paper_version_id: str,
    item_index: int,
    instruction: str,
    targets: dict | None = None,
) -> str:
    """校验状态并创建改题任务；调用方负责 commit 与 outbox 派发。

    幂等语义（对齐 enqueue_generation）：同题同要求同目标的**在途**任务复用，
    避免双击重复烧模型；已到终态的任务换一把新键，教师重发同一要求
    才能真正拿到新提案，而不是旧任务的完成态。
    """
    instruction = str(instruction or "").strip()
    if not instruction:
        raise AiReviseError("修改要求不能为空")

    # 状态校验与 update_paper_item 同口径（试卷存在 / 未定稿 / 题目存在）；
    # 装配结果不入队，worker 执行时再取最新（教师可能刚改过题）。
    context = load_revise_context(
        session, course_id=course_id, paper_version_id=paper_version_id, item_index=item_index
    )
    # 目标属性快检：难度认知配对、考点原子可用性等预检在入队时就跑一遍，
    # 让教师立即拿到 422，而不是等 worker 异步失败（执行期会再取最新重检一次）
    resolve_revise_targets(
        session,
        course_id=course_id,
        paper_version_id=paper_version_id,
        context=context,
        targets=targets,
    )

    base_key = _task_key(paper_version_id, item_index, instruction, targets)
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
            "targets": targets or {},
        },
    )
