"""试卷合同分配器：配额→门槛→聚类→簇轮转→互斥→结构轮换，纯确定性零模型。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.blueprint.models import BlueprintPlan, BlueprintRequest, PlanItem, UnitCoverage
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.contract import (
    DEFAULT_CENTRALITY_THRESHOLD,
    BackfilledPoint,
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


def _comprehensive_archetype_pool(request: ContractRequest) -> list[str]:
    """综合题原型池：教师经 type_rules.comprehensive.archetypes 显式控制。

    教师可传原型白名单（顺序即偏好序），适合按学科裁剪——非编程课程可
    排除 code_completion_scenario，文科可只留 case_analysis 等。未指定
    或全部非法时回退完整原型池（通用机制，不绑定具体课程）。
    """

    rule = request.blueprint.type_rules.get("comprehensive") or {}
    requested = rule.get("archetypes")
    if not isinstance(requested, list) or not requested:
        return list(_ARCHETYPE_ROTATION)
    pool = [name for name in requested if name in ARCHETYPE_CONTRACTS]
    return pool or list(_ARCHETYPE_ROTATION)


class ContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blueprint: BlueprintRequest
    knowledge_cards: dict[str, dict] = Field(min_length=1)
    centrality_threshold: float = DEFAULT_CENTRALITY_THRESHOLD
    # 分配种子：None 保持确定性分配（同池同卷）；给定整数时打破评分
    # 并列——同种子复现同卷，异种子在富余池上选出不同原子组合
    allocation_seed: int | None = None
    # 历史避重：其他试卷已用过的原子原文（coverage_atom）集合。命中者在
    # 贪心评分中被计为软惩罚（排在本卷多样性目标之后、种子扰动之前），
    # 池有富余时改挑没用过的原子，池耗尽时仍照常分配。key 在此层按
    # atom_key 口径归一化，调用方直接传快照里的原文即可。
    avoid_atoms: set[str] | None = None
    # 已存储的蓝图计划（教师看到的版本）。合同分配必须忠于该计划而非
    # 重跑 allocate_plan_items：unit 顺序差异会把同题型槽位漂移到别的
    # 考点，导致合同与已确认蓝图不一致。
    plan: BlueprintPlan | None = None


def _comprehensive_fields(nth: int, pool: list[str]) -> dict:
    """第 nth 道综合题的结构轮换字段。

    pool 是原型轮换池（教师可裁剪）；轮换起点由 allocation_seed 扰动
    （seed=None 时起点 0 保持确定性），同种子复现、异种子换原型序列，
    避免每张卷的综合题永远是固定的前几种原型。
    """
    archetype = pool[nth % len(pool)]
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


def _point_capacity(pool: list) -> int:
    """考点可供给题数：池内不同非空答案域数 + 空边界原子数。

    答案域是卡级标量且全卷互斥：同一边界整卷只能出 1 题，故一张卡
    （无论含多少原子）只贡献 1 题容量；空边界原子之间永不互斥，各自
    独立计 1 题。
    """
    boundaries = {atom.boundary for atom in pool if atom.boundary}
    empty = sum(1 for atom in pool if not atom.boundary)
    return len(boundaries) + empty


def _backfill_over_assigned_points(
    items: list[PlanItem],
    pools: dict[str, list],
    point_anchor: dict[str, str],
) -> tuple[list[PlanItem], list[BackfilledPoint]]:
    """考点题位超配时，把超额题位改派到同章（anchor）内仍有余量的兄弟考点。

    蓝图可能把 N 个题位压给只有 M（<N）个答案域的考点（线上事故：4 个
    题位对 1 张卡，合同静默丢 6 题只得 83/100）。第一轮各考点在容量内
    照单全收，超额题位第二轮只改派到同章富余考点：仅更换 exam_point_id，
    题型/分值/难度/认知层级/考核模式/章锚点全部保留，题型配额、难度分布
    与每章比例因此不变。同章确无余量时题位留在原考点，由配额检查与互斥
    分配显式报冲突，绝不静默降分。
    """
    demand: dict[str, list[PlanItem]] = {}
    passthrough: list[PlanItem] = []
    for item in items:
        if item.exam_point_id:
            demand.setdefault(item.exam_point_id, []).append(item)
        else:
            passthrough.append(item)  # 未关联考点：交由 missing_exam_point 显式报告

    capacity = {point: _point_capacity(pool) for point, pool in pools.items()}
    room = dict(capacity)
    for point, group in demand.items():
        room[point] = room.get(point, 0) - len(group)

    adjusted = list(passthrough)
    backfilled: list[BackfilledPoint] = []
    overflow: list[PlanItem] = []
    for point in sorted(demand):
        group = sorted(demand[point], key=lambda i: i.item_index)
        keep = capacity.get(point, 0)
        adjusted.extend(group[:keep])
        overflow.extend(group[keep:])
    for item in overflow:
        anchor = point_anchor.get(item.exam_point_id, item.anchor_key)
        target = next(
            (
                sibling for sibling in sorted(pools)
                if sibling != item.exam_point_id
                and point_anchor.get(sibling, "") == anchor
                and room.get(sibling, 0) > 0
            ),
            None,
        )
        if target is None:
            adjusted.append(item)  # 同章无余量：留在原考点，显式报冲突
            continue
        room[target] -= 1
        adjusted.append(item.model_copy(update={"exam_point_id": target}))
        backfilled.append(BackfilledPoint(
            item_index=item.item_index,
            from_exam_point_id=item.exam_point_id,
            to_exam_point_id=target,
            anchor_key=anchor,
        ))
    adjusted.sort(key=lambda i: i.item_index)
    return adjusted, backfilled


def allocate_paper_contract(request: ContractRequest) -> PaperContract:
    plan = request.plan or allocate_plan_items(request.blueprint)
    pools = build_exam_point_pools(
        request.blueprint.units, request.knowledge_cards,
        threshold=request.centrality_threshold,
    )
    # 同章回补必须先于配额判定：超配题位改派到同章富余兄弟考点后，
    # 配额检查与互斥分配只针对回补后仍无法安排的题位显式报冲突
    point_anchor = {
        unit.exam_point_id: unit.anchor_key
        for unit in request.blueprint.units if unit.exam_point_id
    }
    items, backfilled_points = _backfill_over_assigned_points(
        plan.items, pools, point_anchor,
    )

    conflicts: list[ContractConflict] = []
    # 配额不足冲突（显式报告，不静默降级）
    quota: dict[str, int] = {}
    for item in items:
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
    for item in items:
        if item.exam_point_id:
            items_by_point.setdefault(item.exam_point_id, []).append(item)
    for group in items_by_point.values():
        group.sort(key=lambda i: i.item_index)  # assign_atoms_to_items 依赖顺序

    slots: list[ContractSlot] = []
    # 综合题原型池（教师可裁剪）与轮换起点（种子扰动：同种子复现，
    # 异种子换原型序列；None 保持确定性起点 0）
    archetype_pool = _comprehensive_archetype_pool(request)
    comp_counter = (
        request.allocation_seed % len(archetype_pool)
        if request.allocation_seed is not None else 0
    )
    # 全卷共享互斥状态：跨考点原子唯一 + 答案边界互斥（终检为全卷两两比较）
    used_keys: set[str] = set()
    used_boundaries: list[str] = []
    # 历史避重键集：与 atom_key 同口径（atom_text 归一化）。空集与 None
    # 等价——不传时完全保持原有确定性行为。
    avoid_keys: set[str] | None = (
        {_normalized(text) for text in request.avoid_atoms}
        if request.avoid_atoms else None
    )
    for point in sorted(items_by_point):
        clusters = cluster_pool_atoms(pools.get(point, []))
        assignments, point_conflicts = assign_atoms_to_items(
            items_by_point[point], clusters,
            shared_used_keys=used_keys, shared_used_boundaries=used_boundaries,
            seed=request.allocation_seed, avoid_keys=avoid_keys,
        )
        conflicts.extend(point_conflicts)
        for item, atom in assignments:
            card = request.knowledge_cards.get(atom.card_id, {})
            raw = card.get("prompt_material", [])
            prompt_material = [raw] if isinstance(raw, str) else list(raw or [])
            extra: dict = {}
            assessment_mode = item.assessment_mode
            if item.question_type == "comprehensive":
                fields = _comprehensive_fields(comp_counter, archetype_pool)
                # 校验兼容性，不兼容则顺延原型
                for _ in range(len(archetype_pool)):
                    contract_def = ARCHETYPE_CONTRACTS[fields["comprehensive_archetype"]]
                    if assessment_mode in contract_def.allowed_modes:
                        break
                    comp_counter += 1
                    fields = _comprehensive_fields(comp_counter, archetype_pool)
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
        backfilled_points=backfilled_points,
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
