"""试卷合同分配器：配额→门槛→聚类→簇轮转→互斥→结构轮换，纯确定性零模型。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.blueprint.models import BlueprintRequest, PlanItem, UnitCoverage
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.contract import (
    DEFAULT_CENTRALITY_THRESHOLD,
    ContractAuditSummary,
    ContractConflict,
    ContractSlot,
    ExamPointProportion,
    ForbiddenContext,
    PaperContract,
    _normalized,
    assign_atoms_to_items,
    boundaries_overlap,
    build_exam_point_pools,
    cluster_pool_atoms,
)
from app.services.blueprint_service import allocate_plan_items

_ARCHETYPE_ROTATION = [
    "code_completion_scenario", "fault_diagnosis", "comparative_decision",
    "integrated_explanation", "case_analysis", "solution_design",
    "process_optimization", "critique_correction",
]
_COGNITIVE_SEQUENCES = [
    ["understand", "apply", "analyze"],
    ["apply", "analyze", "evaluate"],
    ["remember", "understand", "apply"],
]


class ContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blueprint: BlueprintRequest
    knowledge_cards: dict[str, dict] = Field(min_length=1)
    centrality_threshold: float = DEFAULT_CENTRALITY_THRESHOLD


def _comprehensive_fields(nth: int) -> dict:
    """第 nth 道综合题的结构轮换字段。"""
    archetype = _ARCHETYPE_ROTATION[nth % len(_ARCHETYPE_ROTATION)]
    contract = ARCHETYPE_CONTRACTS[archetype]
    material_forms = sorted(contract.material_forms)
    material_form = material_forms[nth % len(material_forms)]
    sequence = _COGNITIVE_SEQUENCES[nth % len(_COGNITIVE_SEQUENCES)]
    if archetype == "code_completion_scenario":
        # 该原型固定两个分问：补全代码与问题分析
        subquestion_count = 2
        count_range = [2, 2]
    else:
        subquestion_count = len(sequence)
        count_range = [max(2, subquestion_count - 1), min(4, subquestion_count + 1)]
    return {
        "comprehensive_archetype": archetype,
        "material_form": material_form,
        "cognitive_sequence": sequence,
        "subquestion_count_range": count_range,
        "subquestion_actions": [f"分问{i + 1}" for i in range(subquestion_count)],
        "answer_boundaries": [f"分问{i + 1}答案域" for i in range(subquestion_count)],
    }


def allocate_paper_contract(request: ContractRequest) -> PaperContract:
    plan = allocate_plan_items(request.blueprint)
    pools = build_exam_point_pools(
        request.blueprint.units, request.knowledge_cards,
        threshold=request.centrality_threshold,
    )

    conflicts: list[ContractConflict] = []
    # 配额不足冲突（显式报告，不静默降级）
    quota: dict[str, int] = {}
    for item in plan.items:
        if not item.exam_point_id:
            conflicts.append(ContractConflict(
                code="missing_exam_point",
                message=f"题位 {item.item_index} 未关联考点",
                detail={"item_index": item.item_index},
            ))
        else:
            quota[item.exam_point_id] = quota.get(item.exam_point_id, 0) + 1
    for point, count in quota.items():
        available = len(pools.get(point, []))
        if available < count:
            conflicts.append(ContractConflict(
                code="atom_pool_insufficient", exam_point_id=point,
                message=f"考点 {point} 可用原子不足：需 {count} 个，门槛过滤后仅 {available} 个",
                detail={"required": count, "available": available},
            ))

    items_by_point: dict[str, list[PlanItem]] = {}
    for item in plan.items:
        if item.exam_point_id:
            items_by_point.setdefault(item.exam_point_id, []).append(item)
    for group in items_by_point.values():
        group.sort(key=lambda i: i.item_index)  # assign_atoms_to_items 依赖顺序

    slots: list[ContractSlot] = []
    comp_counter = 0
    # 全卷共享互斥状态：跨考点原子唯一 + 答案边界互斥（终检为全卷两两比较）
    used_keys: set[str] = set()
    used_boundaries: list[str] = []
    for point in sorted(items_by_point):
        clusters = cluster_pool_atoms(pools.get(point, []))
        assignments, point_conflicts = assign_atoms_to_items(
            items_by_point[point], clusters,
            shared_used_keys=used_keys, shared_used_boundaries=used_boundaries,
        )
        conflicts.extend(point_conflicts)
        for item, atom in assignments:
            card = request.knowledge_cards.get(atom.card_id, {})
            raw = card.get("prompt_material", [])
            prompt_material = [raw] if isinstance(raw, str) else list(raw or [])
            extra: dict = {}
            assessment_mode = item.assessment_mode
            if item.question_type == "comprehensive":
                fields = _comprehensive_fields(comp_counter)
                # 校验兼容性，不兼容则顺延原型
                for _ in range(len(_ARCHETYPE_ROTATION)):
                    contract_def = ARCHETYPE_CONTRACTS[fields["comprehensive_archetype"]]
                    if assessment_mode in contract_def.allowed_modes:
                        break
                    comp_counter += 1
                    fields = _comprehensive_fields(comp_counter)
                contract_def = ARCHETYPE_CONTRACTS[fields["comprehensive_archetype"]]
                if assessment_mode not in contract_def.allowed_modes:
                    assessment_mode = sorted(contract_def.allowed_modes)[0]
                extra = fields
                comp_counter += 1
            slots.append(ContractSlot(
                item_index=item.item_index,
                question_type=item.question_type,
                score=item.score,
                difficulty=item.difficulty,
                cognitive_level=item.cognitive_level,
                assessment_mode=assessment_mode,
                exam_point_id=point,
                anchor_key=item.anchor_key,
                unit_id=atom.unit_id,
                card_id=atom.card_id,
                coverage_atom=atom.atom_text,
                answer_boundary=atom.boundary,
                performance_statement=str(card.get("performance_statement", "")),
                prompt_material=prompt_material,
                scope_boundary=card.get("scope_boundary", {}) or {},
                preferred_terms=list(card.get("preferred_terms", []) or []),
                **extra,
            ))

    # 禁用上下文：同考点兄弟题位
    final_slots: list[ContractSlot] = []
    for slot in slots:
        siblings = [s for s in slots
                    if s.exam_point_id == slot.exam_point_id and s.item_index != slot.item_index]
        final_slots.append(slot.model_copy(update={"forbidden_context": ForbiddenContext(
            atoms=[s.coverage_atom for s in siblings],
            answer_cores=[s.answer_boundary for s in siblings if s.answer_boundary],
        )}))
    final_slots.sort(key=lambda s: s.item_index)

    total = sum(s.score for s in final_slots)
    weights: dict[str, float] = {}
    for unit in request.blueprint.units:
        if unit.exam_point_id:
            weights[unit.exam_point_id] = request.blueprint.chapter_weights.get(unit.anchor_key, 0.0)
    summary = ContractAuditSummary(
        exam_points=[
            ExamPointProportion(
                exam_point_id=point,
                weight=weights.get(point, 0.0),
                question_count=sum(1 for s in final_slots if s.exam_point_id == point),
                proportion=(sum(1 for s in final_slots if s.exam_point_id == point) / len(final_slots)) if final_slots else 0.0,
            )
            for point in sorted(quota)
        ],
        type_counts={
            qtype: sum(1 for s in final_slots if s.question_type == qtype)
            for qtype in sorted({s.question_type for s in final_slots})
        },
        difficulty_counts={
            level: sum(1 for s in final_slots if s.difficulty == level)
            for level in sorted({s.difficulty for s in final_slots})
        },
    )
    return PaperContract(total_score=total, slots=final_slots, conflicts=conflicts, audit_summary=summary)


class ContractRevisionError(ValueError):
    """教师修订违反合同构造性保证时抛出。"""


def apply_slot_revisions(
    contract: PaperContract,
    revisions: list[dict],
    *,
    units: list[UnitCoverage],
    knowledge_cards: dict[str, dict],
) -> PaperContract:
    """教师换单题原子：只接受同考点池内原子，且修订后全卷互斥仍成立。"""
    pools = build_exam_point_pools(units, knowledge_cards)
    updated = {s.item_index: s.model_copy(deep=True) for s in contract.slots}
    for revision in revisions:
        item_index = int(revision["item_index"])
        atom_text = str(revision["coverage_atom"]).strip()
        slot = updated.get(item_index)
        if slot is None:
            raise ContractRevisionError(f"题位 {item_index} 不存在于合同")
        pool = pools.get(slot.exam_point_id, [])
        match = next((a for a in pool if a.atom_text == atom_text), None)
        if match is None:
            raise ContractRevisionError(
                f"原子不在考点 {slot.exam_point_id} 的可用池中：{atom_text}"
            )
        slot.coverage_atom = match.atom_text
        slot.card_id = match.card_id
        slot.unit_id = match.unit_id
        slot.answer_boundary = match.boundary

    slots = list(updated.values())
    # 修订后全卷原子唯一性（空 answer_boundary 可绕过边界互斥，需显式去重）
    seen_atoms: dict[str, int] = {}
    for slot in slots:
        atom_key = _normalized(slot.coverage_atom)
        if atom_key in seen_atoms:
            raise ContractRevisionError(
                f"修订后题位 {seen_atoms[atom_key]} 与 {slot.item_index} 考查同一原子"
            )
        seen_atoms[atom_key] = slot.item_index
    # 修订后全卷重验互斥（全卷两两，与终检口径一致）
    for i, left in enumerate(slots):
        for right in slots[i + 1:]:
            if boundaries_overlap(left.answer_boundary, right.answer_boundary):
                raise ContractRevisionError(
                    f"修订后题位 {left.item_index} 与 {right.item_index} 答案域重叠"
                )

    final = []
    for slot in slots:
        siblings = [s for s in slots
                    if s.exam_point_id == slot.exam_point_id and s.item_index != slot.item_index]
        final.append(slot.model_copy(update={"forbidden_context": ForbiddenContext(
            atoms=[s.coverage_atom for s in siblings],
            answer_cores=[s.answer_boundary for s in siblings if s.answer_boundary],
        )}))
    final.sort(key=lambda s: s.item_index)
    return contract.model_copy(update={"slots": final})
