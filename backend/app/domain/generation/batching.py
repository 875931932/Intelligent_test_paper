"""按考点分批：同考点同批（批内互见防重复），跨批互斥由合同禁用上下文保证。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.generation.contract import ContractSlot, ForbiddenContext

BATCH_MAX_SIZE = 6
BATCH_MIN_SIZE = 3


class QuestionBatch(BaseModel):
    """一次模型调用要生成的题位组。"""
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    anchor_key: str
    exam_point_ids: list[str]
    slots: list[ContractSlot]
    forbidden_context: ForbiddenContext = Field(default_factory=ForbiddenContext)


def split_contract_into_batches(slots: list[ContractSlot]) -> list[QuestionBatch]:
    """分批规则（纯确定性）：
    1. 按考点分组，同考点必须在同一批（重复只发生在同考点内部）
    2. 大考点（>6 题）拆子批，子批间通过 forbidden_context 互见
    3. 小考点（<3 题）与同 anchor 的其他小考点合并成批
    4. 每批携带同考点其他批次的原子与答案核心作为禁用上下文
    """
    by_point: dict[str, list[ContractSlot]] = {}
    for slot in slots:
        by_point.setdefault(slot.exam_point_id, []).append(slot)
    for group in by_point.values():
        group.sort(key=lambda s: s.item_index)

    fragments: list[list[ContractSlot]] = []
    pending_small: dict[str, list[ContractSlot]] = {}
    for point in sorted(by_point):
        group = by_point[point]
        if len(group) < BATCH_MIN_SIZE:
            pending_small.setdefault(group[0].anchor_key, []).extend(group)
        else:
            fragments.extend(
                group[i : i + BATCH_MAX_SIZE]
                for i in range(0, len(group), BATCH_MAX_SIZE)
            )
    for anchor in sorted(pending_small):
        pile = sorted(pending_small[anchor], key=lambda s: s.item_index)
        fragments.extend(
            pile[i : i + BATCH_MAX_SIZE]
            for i in range(0, len(pile), BATCH_MAX_SIZE)
        )

    batches: list[QuestionBatch] = []
    for i, group in enumerate(fragments):
        points = sorted({s.exam_point_id for s in group})
        other_atoms: list[str] = []
        other_cores: list[str] = []
        for j, other in enumerate(fragments):
            if i == j:
                continue
            # 只带同考点的其他批次（跨考点内容与本题无关，不必禁用）
            if any(s.exam_point_id in points for s in other):
                other_atoms.extend(s.coverage_atom for s in other)
                other_cores.extend(s.answer_boundary for s in other if s.answer_boundary)
        batches.append(QuestionBatch(
            batch_id=f"B{i + 1:02d}",
            anchor_key=group[0].anchor_key,
            exam_point_ids=points,
            slots=group,
            forbidden_context=ForbiddenContext(
                atoms=list(dict.fromkeys(other_atoms)),
                answer_cores=list(dict.fromkeys(other_cores)),
            ),
        ))
    return batches
