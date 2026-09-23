"""合同执行服务：阈值回退分配、教师修订、落库 generation_run。

只从 contract_service 导入 allocate_paper_contract 与 apply_slot_revisions，
不修改其内部逻辑。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.schema import (
    assessment_units,
    blueprint_versions,
    content_domains,
    exam_projects,
    generation_runs,
    index_versions,
    knowledge_cards,
    plan_items,
)
from app.domain.blueprint.models import (
    ASSESSMENT_MODES,
    BlueprintPlan,
    BlueprintRequest,
    CardSemanticProfile,
    PlanItem,
    UnitCoverage,
)
from app.domain.generation.contract import PaperContract
from app.services.contract_service import (
    ContractRequest,
    allocate_paper_contract,
    apply_slot_revisions,
)


class ContractExecutionError(Exception):
    """合同分配/修订阶段发生的可恢复错误。"""


def _nid() -> str:
    return uuid.uuid4().hex[:16]


def _row_to_dict(row) -> dict[str, Any]:
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row._asdict()) if hasattr(row, "_asdict") else dict(row)


# 历史避重回看窗口：只统计该课程最近 N 份合同用过的原子。窗口有界，
# 防止老试卷把整个池子标成"已用过"使避重退化成无效；更早的试卷允许
# 复用，最近几套保证不重样。
_HISTORY_RUN_LIMIT = 10


def _collect_used_atom_texts(
    session: Session,
    *,
    course_id: str,
    limit: int = _HISTORY_RUN_LIMIT,
) -> set[str]:
    """汇总该课程最近 limit 份合同已用过的原子原文（coverage_atom）。

    教师一旦确认合同，对应原子即视为占用——即便生成失败或尚未开始，
    那也是教师已经看过的一套题。因此不按 status 过滤，凡带快照的 run
    都计入。返回原子原文集合，键归一化由合同分配层按 atom_key 口径
    统一处理（_normalized），此处保持与快照一致的原文即可。
    """
    rows = session.execute(
        select(generation_runs.c.contract_snapshot)
        .where(generation_runs.c.course_id == course_id)
        .order_by(generation_runs.c.created_at.desc())
        .limit(limit)
    ).all()
    used: set[str] = set()
    for row in rows:
        snap = row._mapping.get("contract_snapshot")
        if isinstance(snap, str):  # JSON 列被存成文本时的兜底解析
            try:
                snap = json.loads(snap)
            except ValueError:
                continue
        if not isinstance(snap, dict):
            continue
        for slot in snap.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            text = slot.get("coverage_atom")
            if text:
                used.add(str(text))
    return used


def _build_contract_request_from_db(
    session: Session,
    *,
    blueprint_version_id: str,
    centrality_threshold: float,
    allocation_seed: int | None = None,
) -> ContractRequest:
    """从 DB 重建 ContractRequest（蓝图计划项 + 目录卡片）。"""
    # 加载 blueprint_version
    bv = session.execute(
        select(blueprint_versions).where(
            blueprint_versions.c.id == blueprint_version_id,
        )
    ).one_or_none()
    if bv is None:
        raise ContractExecutionError(f"蓝图版本不存在: {blueprint_version_id}")
    bv_data = bv._mapping
    course_id = bv_data["course_id"]
    catalog_version_id = bv_data["catalog_version_id"]
    framework_version_id = bv_data["framework_version_id"]

    # 聚合 units：用该 catalog 的全部 active assessment_units（而非仅 plan_items
    # 覆盖到的 unit）。合同分配会按章节权重重新切分全部槽位，若 units 缺失某些
    # 章节/单元，对应槽位无卡可配（fallback 到 0.45 仍冲突、总分不足 100）。
    # 与蓝图创建阶段一致地使用全量 unit + 全量卡池，分配结果才完整且确定。
    unit_rows = session.execute(
        select(
            assessment_units.c.id.label("au_id"),
            assessment_units.c.exam_point_id.label("au_exam_point_id"),
            assessment_units.c.code.label("au_code"),
            content_domains.c.framework_anchor_key,
        )
        .select_from(assessment_units)
        .join(content_domains, content_domains.c.id == assessment_units.c.content_domain_id, isouter=True)
        .where(
            assessment_units.c.catalog_version_id == catalog_version_id,
            assessment_units.c.course_id == course_id,
            assessment_units.c.status == "active",
        )
        .order_by(assessment_units.c.code)
    ).all()

    units_map: dict[str, dict[str, Any]] = {}
    for r in unit_rows:
        d = r._mapping
        au_id = d["au_id"]
        units_map[au_id] = {
            "unit_id": au_id,
            "exam_point_id": d["au_exam_point_id"] or "",
            "anchor_key": d["framework_anchor_key"] or au_id,
            "card_ids": [],
        }

    units_payload = list(units_map.values())
    # 确保每个 unit 至少有 1 张卡
    for u in units_payload:
        if not u["card_ids"]:
            u["card_ids"] = [f"__placeholder_{u['unit_id']}"]

    # 加载 knowledge_cards（作为 dict 传入合同分配器）
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
        )
        .where(
            knowledge_cards.c.catalog_version_id == catalog_version_id,
            knowledge_cards.c.course_id == course_id,
        )
    ).all()
    # 按 assessment_unit_id 聚合全量卡，回填 units 的 card_ids
    card_ids_by_unit: dict[str, list[str]] = {}
    for r in card_rows:
        au = r._mapping.get("assessment_unit_id")
        if au:
            card_ids_by_unit.setdefault(au, []).append(r._mapping["id"])
    for u in units_payload:
        ids = card_ids_by_unit.get(u["unit_id"])
        if ids:
            u["card_ids"] = ids
    cards_dict: dict[str, dict] = {}
    sem_profiles: dict[str, CardSemanticProfile] = {}
    card_qtypes: dict[str, list[str]] = {}
    for r in card_rows:
        c = r._mapping
        cid = c["id"]
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
            "answer_boundary": c.get("answer_boundary") or c.get("answer_proposition") or "",
            "prompt_material": list(c.get("prompt_material") or []),
            "relation_edges": list(c.get("relation_edges") or []),
        }
        # 注意：不把 knowledge_cards.allowed_question_types 填进 card_qtypes。
        # 该字段是 AI 标注的中文展示名（如“单选题”“简答题”），与蓝图引擎的
        # 英文题型键（single_choice / short_answer …）语义不对应；若用它过滤，
        # 所有卡都会被判定为“不允许任何题型”，导致合同分配报
        # “no eligible chapter”。卡的题型约束由蓝图阶段显式下发
        # （card_question_types），这里保持与蓝图创建一致的不限语义。
        # 如果 assessable_content 为空，给一个兜底原子以保证合同分配不崩溃
        if not cards_dict[cid]["assessable_content"]:
            cards_dict[cid]["assessable_content"] = [f"{cid} 默认知识原子"]
        # 如果核心度为阈值前兜底 1.0：添加一个 is_core 标志让 centrality 函数通过
        cards_dict[cid].setdefault("is_core", True)
        sem_profiles[cid] = CardSemanticProfile(
            concept_cluster=c.get("concept_cluster") or cid,
            answer_proposition=c.get("answer_proposition") or cid,
        )

    # 为 placeholder cards 生成最小条目
    for u in units_payload:
        for cid in u["card_ids"]:
            if cid.startswith("__placeholder_") and cid not in cards_dict:
                cards_dict[cid] = {
                    "name": cid,
                    "performance_statement": "占位卡片",
                    "assessable_content": [f"{cid} 默认知识原子"],
                    "scope_boundary": {},
                    "cognitive_targets": [],
                    "allowed_question_types": [],
                    "importance": 1,
                    "concept_cluster": cid,
                    "answer_proposition": cid,
                    "answer_boundary": cid,
                    "prompt_material": [],
                    "preferred_terms": [],
                    "relation_edges": [],
                    "is_core": True,
                }
                sem_profiles[cid] = CardSemanticProfile(
                    concept_cluster=cid,
                    answer_proposition=cid,
                )

    # 重建 BlueprintRequest（只用于合同分配器内部重建 plan；
    # type_rules 从 blueprint_version 存储字段中取）
    type_rules = bv_data.get("type_rules") or {}
    chapter_weights = bv_data.get("chapter_weights") or {}

    # 计算 total_score：若 type_rules 完整，直接计；否则从 plan_items 汇总
    total_from_rules = sum(
        float(r.get("count", 0)) * float(r.get("score", 0))
        for r in type_rules.values()
    )
    if total_from_rules <= 0:
        total_from_rules = sum(
            float(r._mapping["score"])
            for r in session.execute(
                select(plan_items.c.score).where(
                    plan_items.c.blueprint_version_id == blueprint_version_id,
                )
            ).all()
        )

    blueprint_req = BlueprintRequest(
        total_score=total_from_rules,
        type_rules=type_rules,
        chapter_weights=chapter_weights if chapter_weights else {
            u["anchor_key"]: 100.0 / max(1, len(units_payload))
            for u in units_payload
        },
        units=[UnitCoverage(**u) for u in units_payload],
        card_semantic_profiles=sem_profiles,
        card_question_types=card_qtypes,
    )

    # 用存储的计划项重建 BlueprintPlan：合同必须忠于教师看到的蓝图，
    # 不能重跑 allocate_plan_items（unit 顺序差异会把同题型槽位漂移到
    # 别的考点，导致合同与已确认蓝图不一致、冲突集与展示不匹配）。
    stored_rows = session.execute(
        select(
            plan_items.c.item_index,
            plan_items.c.question_type,
            plan_items.c.assessment_mode,
            plan_items.c.score,
            plan_items.c.difficulty,
            plan_items.c.cognitive_level,
            plan_items.c.exam_point_id,
            plan_items.c.knowledge_card_id,
            plan_items.c.assessment_unit_id,
            content_domains.c.framework_anchor_key,
        )
        .select_from(plan_items)
        .join(assessment_units, assessment_units.c.id == plan_items.c.assessment_unit_id, isouter=True)
        .join(content_domains, content_domains.c.id == assessment_units.c.content_domain_id, isouter=True)
        .where(plan_items.c.blueprint_version_id == blueprint_version_id)
        .order_by(plan_items.c.item_index)
    ).all()
    plan_item_objs: list[PlanItem] = []
    for r in stored_rows:
        d = r._mapping
        mode = d["assessment_mode"]
        plan_item_objs.append(PlanItem(
            item_index=d["item_index"],
            question_type=d["question_type"],
            score=float(d["score"]),
            anchor_key=d["framework_anchor_key"] or d["assessment_unit_id"],
            exam_point_id=d["exam_point_id"] or "",
            unit_id=d["assessment_unit_id"],
            card_id=d["knowledge_card_id"] or "",
            difficulty=d["difficulty"] or "medium",
            cognitive_level=d["cognitive_level"] or "understand",
            assessment_mode=mode if mode in ASSESSMENT_MODES else "conceptual",
        ))
    type_counts: dict[str, int] = {}
    difficulty_counts: dict[str, dict[str, int]] = {}
    anchor_counts: dict[str, int] = {}
    for it in plan_item_objs:
        type_counts[it.question_type] = type_counts.get(it.question_type, 0) + 1
        dmap = difficulty_counts.setdefault(it.difficulty, {})
        dmap[it.question_type] = dmap.get(it.question_type, 0) + 1
        anchor_counts[it.anchor_key] = anchor_counts.get(it.anchor_key, 0) + 1
    stored_plan = BlueprintPlan(
        total_score=sum(it.score for it in plan_item_objs),
        items=plan_item_objs,
        type_counts=type_counts,
        difficulty_counts=difficulty_counts,
        anchor_counts=anchor_counts,
    )

    # 历史避重：把该课程最近用过的原子喂进分配器，让新卷子在池有富余时
    # 主动改挑没用过的原子。这是纯软惩罚（排在本卷多样性目标之后、种子
    # 扰动之前），池耗尽时仍照常分配，绝不丢题或新增冲突。放在请求构建
    # 处而非各端点，保证预览、确认、阈值回退每一轮看到的历史一致。
    avoid_atoms = _collect_used_atom_texts(session, course_id=course_id)

    return ContractRequest(
        blueprint=blueprint_req,
        knowledge_cards=cards_dict,
        centrality_threshold=centrality_threshold,
        allocation_seed=allocation_seed,
        avoid_atoms=avoid_atoms or None,
        plan=stored_plan,
    ), units_payload, cards_dict


def allocate_with_fallback(
    session: Session,
    *,
    blueprint_version_id: str,
    allocation_seed: int | None = None,
) -> tuple[PaperContract, float, list[tuple[float, int]]]:
    """阈值回退分配：0.6 → 0.5 → 0.45，首次无冲突（或最后一轮）即接受。

    DB 侧的请求内容（蓝图计划项、全量卡池、历史避重）与阈值无关，只从
    数据库构建一次；每轮回退仅替换 centrality_threshold 重新分配——此前
    每轮都整份重建，同一事务里最多把同样的查询重复三遍。分配器对请求
    全程只读（不改 plan.items、不改 knowledge_cards），复用同一请求与
    逐轮重建结果等价。
    """
    thresholds = [0.6, 0.5, 0.45]
    history: list[tuple[float, int]] = []
    base_request, _, _ = _build_contract_request_from_db(
        session,
        blueprint_version_id=blueprint_version_id,
        centrality_threshold=thresholds[0],
        allocation_seed=allocation_seed,
    )
    current_request = base_request

    last_contract: PaperContract | None = None
    used_threshold: float = thresholds[-1]

    for idx, threshold in enumerate(thresholds):
        if idx:
            current_request = base_request.model_copy(
                update={"centrality_threshold": threshold}
            )
        contract = allocate_paper_contract(current_request)
        conflict_count = len(contract.conflicts)
        history.append((threshold, conflict_count))
        last_contract = contract
        used_threshold = threshold
        # 冲突为空 或 到最后一轮接受全部
        if conflict_count == 0 or idx == len(thresholds) - 1:
            break

    assert last_contract is not None
    return last_contract, used_threshold, history


def revise_and_confirm(
    session: Session,
    *,
    course_id: str,
    project_id: str,
    blueprint_version_id: str,
    slot_revisions: list[dict],
    allocation_seed: int | None = None,
) -> dict:
    """分配 → 应用修订 → 生成 generation_run → 推动项目到 generating 状态。"""
    try:
        # 1. 校验 blueprint 归属
        bv = session.execute(
            select(blueprint_versions).where(
                blueprint_versions.c.id == blueprint_version_id,
                blueprint_versions.c.course_id == course_id,
                blueprint_versions.c.exam_project_id == project_id,
            )
        ).one_or_none()
        if bv is None:
            raise ContractExecutionError("蓝图版本不属于该项目或课程")
        bv_data = bv._mapping

        # 2. 执行阈值回退分配
        contract, used_threshold, history = allocate_with_fallback(
            session,
            blueprint_version_id=blueprint_version_id,
            allocation_seed=allocation_seed,
        )

        # 3. 应用教师修订（若有）：无修订（重新生成的默认路径）时不必再做
        # 一次全量 DB 重建，allocate_with_fallback 已经构建过同样的数据。
        if slot_revisions:
            _, units_payload, cards_dict = _build_contract_request_from_db(
                session,
                blueprint_version_id=blueprint_version_id,
                centrality_threshold=used_threshold,
                allocation_seed=allocation_seed,
            )
            try:
                contract = apply_slot_revisions(
                    contract,
                    slot_revisions,
                    units=[UnitCoverage(**u) for u in units_payload],
                    knowledge_cards=cards_dict,
                )
            except Exception as exc:
                raise ContractExecutionError(f"应用 slot 修订失败: {exc}") from exc

        # 4. 构建 contract_snapshot
        # Pre-vs-post conflicts：记录原始冲突列表（历史中的最后一个）和修订后的冲突
        conflicts_pre = [c.model_dump(mode="json") for c in contract.conflicts]
        slots_ser = [s.model_dump(mode="json") for s in contract.slots]
        contract_snapshot: dict[str, Any] = {
            "slots": slots_ser,
            "slot_revisions_applied": slot_revisions,
            "centrality_threshold_used": used_threshold,
            # 种子落库：退出项目再进入时，前端凭此回填"分配方案"下拉，
            # 保证再次确认用的是同一版而非静默回到第 1 版。None（确定性
            # 默认）照原样写，前端按缺失处理。
            "allocation_seed": allocation_seed,
            "conflicts_history": [
                {"threshold": t, "count": c} for t, c in history
            ],
            "conflicts_pre_vs_post": {
                "pre_revision": conflicts_pre,
                "post_revision": [],  # apply_slot_revisions 抛错即中断，不冲突
            },
            "audit_summary": (
                contract.audit_summary.model_dump(mode="json")
                if contract.audit_summary else {}
            ),
        }

        # 5. 查找最新 index_version_id 为该 catalog（找不到则 NULL）
        iv = session.execute(
            select(index_versions.c.id)
            .where(
                index_versions.c.catalog_version_id == bv_data["catalog_version_id"],
                index_versions.c.course_id == course_id,
            )
            .order_by(index_versions.c.version_no.desc())
        ).first()
        index_version_id = iv._mapping["id"] if iv else None

        # 6. 插入 generation_run
        gr_id = _nid()
        session.execute(
            generation_runs.insert().values(
                id=gr_id,
                course_id=course_id,
                framework_version_id=bv_data["framework_version_id"],
                catalog_version_id=bv_data["catalog_version_id"],
                index_version_id=index_version_id,
                blueprint_version_id=blueprint_version_id,
                prompt_template_version="v1",
                run_type="paper_generation",
                status="queued",
                contract_snapshot=contract_snapshot,
                centrality_threshold_used=used_threshold,
            )
        )

        # 7. 更新 exam_projects: active_generation_run_id, status='generating'
        session.execute(
            exam_projects.update()
            .where(
                exam_projects.c.id == project_id,
                exam_projects.c.course_id == course_id,
            )
            .values(
                active_generation_run_id=gr_id,
                status="generating",
            )
        )

        session.commit()
        return {
            "generation_run_id": gr_id,
            "threshold": used_threshold,
            "slot_count": len(contract.slots),
            "conflicts_history": history,
        }

    except ContractExecutionError:
        session.rollback()
        raise
    except SQLAlchemyError as exc:
        session.rollback()
        raise ContractExecutionError(f"数据库错误: {exc}") from exc


def get_contract_conflicts(session: Session, generation_run_id: str) -> list[dict]:
    """从 generation_run.contract_snapshot 中提取冲突记录。"""
    row = session.execute(
        select(generation_runs.c.contract_snapshot, generation_runs.c.course_id)
        .where(generation_runs.c.id == generation_run_id)
    ).one_or_none()
    if row is None:
        return []
    snap = row._mapping.get("contract_snapshot") or {}
    conflicts = snap.get("conflicts_pre_vs_post") or {}
    result: list[dict] = []
    for key in ("pre_revision", "post_revision"):
        for c in conflicts.get(key) or []:
            c2 = dict(c) if isinstance(c, dict) else {"message": str(c)}
            c2.setdefault("stage", key)
            result.append(c2)
    return result
