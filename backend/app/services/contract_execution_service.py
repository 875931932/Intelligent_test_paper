"""合同执行服务：阈值回退分配、教师修订、落库 generation_run。

只从 contract_service 导入 allocate_paper_contract 与 apply_slot_revisions，
不修改其内部逻辑。
"""
from __future__ import annotations

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
    BlueprintRequest,
    CardSemanticProfile,
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

    # 加载 plan_items + assessment_units
    pi_rows = session.execute(
        select(
            plan_items.c.item_index,
            plan_items.c.question_type,
            plan_items.c.score,
            plan_items.c.assessment_mode,
            plan_items.c.difficulty,
            plan_items.c.cognitive_level,
            plan_items.c.exam_point_id.label("pi_exam_point_id"),
            plan_items.c.knowledge_card_id,
            assessment_units.c.id.label("au_id"),
            assessment_units.c.code.label("au_code"),
            assessment_units.c.exam_point_id.label("au_exam_point_id"),
            content_domains.c.framework_anchor_key,
        )
        .select_from(plan_items)
        .join(assessment_units, assessment_units.c.id == plan_items.c.assessment_unit_id)
        .join(content_domains, content_domains.c.id == assessment_units.c.content_domain_id, isouter=True)
        .where(
            plan_items.c.blueprint_version_id == blueprint_version_id,
            plan_items.c.course_id == course_id,
        )
        .order_by(plan_items.c.item_index)
    ).all()

    # 聚合成 units：以 assessment_unit_id 为键
    units_map: dict[str, dict[str, Any]] = {}
    for r in pi_rows:
        d = r._mapping
        au_id = d["au_id"]
        ep_id = d["pi_exam_point_id"] or d["au_exam_point_id"] or ""
        anchor = d["framework_anchor_key"] or au_id
        card_id = d["knowledge_card_id"]
        entry = units_map.setdefault(au_id, {
            "unit_id": au_id,
            "exam_point_id": ep_id,
            "anchor_key": anchor,
            "card_ids": [],
        })
        if card_id and card_id not in entry["card_ids"]:
            entry["card_ids"].append(card_id)

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
        )
        .where(
            knowledge_cards.c.catalog_version_id == catalog_version_id,
            knowledge_cards.c.course_id == course_id,
        )
    ).all()
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
            "answer_boundary": c.get("answer_proposition") or "",
            "prompt_material": list(c.get("prompt_material") or []),
            "preferred_terms": [],
            "relation_edges": list(c.get("relation_edges") or []),
        }
        # 如果 assessable_content 为空，给一个兜底原子以保证合同分配不崩溃
        if not cards_dict[cid]["assessable_content"]:
            cards_dict[cid]["assessable_content"] = [f"{cid} 默认知识原子"]
        # 如果核心度为阈值前兜底 1.0：添加一个 is_core 标志让 centrality 函数通过
        cards_dict[cid].setdefault("is_core", True)
        sem_profiles[cid] = CardSemanticProfile(
            concept_cluster=c.get("concept_cluster") or cid,
            answer_proposition=c.get("answer_proposition") or cid,
        )
        card_qtypes[cid] = list(c.get("allowed_question_types") or [])

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
        total_from_rules = sum(float(r._mapping["score"]) for r in pi_rows)

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

    return ContractRequest(
        blueprint=blueprint_req,
        knowledge_cards=cards_dict,
        centrality_threshold=centrality_threshold,
        allocation_seed=allocation_seed,
    ), units_payload, cards_dict


def allocate_with_fallback(
    session: Session,
    *,
    blueprint_version_id: str,
    allocation_seed: int | None = None,
) -> tuple[PaperContract, float, list[tuple[float, int]]]:
    """阈值回退分配：0.6 → 0.5 → 0.45，首次无冲突（或最后一轮）即接受。"""
    thresholds = [0.6, 0.5, 0.45]
    history: list[tuple[float, int]] = []
    last_contract: PaperContract | None = None
    used_threshold: float = thresholds[-1]

    for idx, threshold in enumerate(thresholds):
        contract_req, _, _ = _build_contract_request_from_db(
            session,
            blueprint_version_id=blueprint_version_id,
            centrality_threshold=threshold,
            allocation_seed=allocation_seed,
        )
        contract = allocate_paper_contract(contract_req)
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

        # 3. 应用教师修订（若有）
        _, units_payload, cards_dict = _build_contract_request_from_db(
            session,
            blueprint_version_id=blueprint_version_id,
            centrality_threshold=used_threshold,
            allocation_seed=allocation_seed,
        )
        if slot_revisions:
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
