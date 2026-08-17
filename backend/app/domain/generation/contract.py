"""试卷合同领域模型：命题前把整卷题位算死。

全局约束（不重复、不抄袭、比例对、不冷门）在合同分配阶段构造性保证。
"""
from __future__ import annotations

import re

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.blueprint.models import PlanItem, UnitCoverage
from app.domain.generation.archetypes import ComprehensiveArchetype, MaterialForm
from app.domain.generation.coverage import _normalized, _validate_comprehensive_contract

# 原子核心度阈值：打分基准 0.5、唯一扣分项 -0.05（含括号），
# 非核心卡且无任何核心信号（定义类关键词 / 绩效强调 / 术语偏好 / 关联边）的原子
# 得分落在 0.45~0.5，视为偏门剔除；核心卡（+0.25 → 0.75+）或含定义类关键词、
# performance_statement 强调（+0.15 → 0.65）的原子可通过。
DEFAULT_CENTRALITY_THRESHOLD = 0.6


def compute_atom_centrality(card: dict, atom_text: str) -> float:
    """计算一条知识原子的核心度（0~1），低于阈值视为偏门。"""
    score = 0.5
    atom_lower = atom_text.lower()
    if card.get("is_core") or card.get("core"):
        score += 0.25
    perf = str(card.get("performance_statement", ""))
    if any(kw in perf for kw in ["核心", "重点", "掌握", "必须"]):
        score += 0.15
    if any(kw in atom_lower for kw in ["定义", "概念", "定理", "公式", "原理"]):
        score += 0.15
    if any(term in atom_text for term in card.get("preferred_terms", [])):
        score += 0.10
    if "(" in atom_text or "（" in atom_text:
        score -= 0.05
    relations = card.get("relation_edges", [])
    if isinstance(relations, list):
        score += min(len(relations) * 0.03, 0.10)
    return min(max(score, 0.0), 1.0)


def atom_bigram_features(atom_text: str) -> frozenset[str]:
    cleaned = _normalized(atom_text)
    return frozenset(cleaned[i : i + 2] for i in range(len(cleaned) - 1))


# 同一概念簇最多供给的题数（防“同概念扎堆”：如同一术语出 6 题）
MAX_QUESTIONS_PER_CLUSTER = 2

# 术语锚停用词：高频非概念英文词不参与强制同簇
_TERM_ANCHOR_STOPWORDS = frozenset({
    "the", "and", "for", "with", "api", "use", "using", "not", "can", "llm", "ai",
})

_TERM_ANCHOR_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}")


def _term_anchors(text: str) -> set[str]:
    """提取原子文本中的英文技术术语锚（小写、滤停用词）。

    "QLoRA 使用 NF4 量化" → {"qlora", "nf4"}；中文与停用词不产生锚。
    bigram 相似度看不见“术语锚”（英文术语只占全句几个字符被稀释），
    共享术语锚的原子必为同一概念，由聚类阶段强制同簇。
    """
    return {
        token.lower()
        for token in _TERM_ANCHOR_PATTERN.findall(text)
        if token.lower() not in _TERM_ANCHOR_STOPWORDS
    }


def jaccard_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def boundaries_overlap(left: str, right: str) -> bool:
    """答案域互斥检测：归一化后相等或互为包含（≥4字符）视为重叠。"""
    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 4 and (a in b or b in a)


class ForbiddenContext(BaseModel):
    """该题生成时必须避开的同考点其他题内容。"""
    model_config = ConfigDict(extra="forbid")

    atoms: list[str] = Field(default_factory=list)
    answer_cores: list[str] = Field(default_factory=list)


class ContractSlot(BaseModel):
    """单个题位的合同：考哪个原子、答案域、禁用上下文。"""
    model_config = ConfigDict(extra="forbid")

    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: str = "conceptual"
    exam_point_id: str
    anchor_key: str
    unit_id: str
    card_id: str
    coverage_atom: str
    answer_boundary: str
    performance_statement: str = ""
    prompt_material: list[str] = Field(default_factory=list)
    scope_boundary: dict = Field(default_factory=dict)
    preferred_terms: list[str] = Field(default_factory=list)
    forbidden_context: ForbiddenContext = Field(default_factory=ForbiddenContext)
    comprehensive_archetype: ComprehensiveArchetype | None = None
    material_form: MaterialForm | None = None
    cognitive_sequence: list[str] = Field(default_factory=list)
    subquestion_count_range: list[int] | None = None
    subquestion_actions: list[str] = Field(default_factory=list)
    answer_boundaries: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self):
        _validate_comprehensive_contract(
            question_type=self.question_type,
            assessment_mode=self.assessment_mode,
            comprehensive_archetype=self.comprehensive_archetype,
            material_form=self.material_form,
            cognitive_sequence=self.cognitive_sequence,
            subquestion_count_range=self.subquestion_count_range,
            subquestion_actions=self.subquestion_actions,
            answer_boundaries=self.answer_boundaries,
        )
        return self


class ContractConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str  # atom_pool_insufficient | cluster_exhausted | missing_exam_point
    exam_point_id: str = ""
    message: str
    detail: dict = Field(default_factory=dict)


class ExamPointProportion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exam_point_id: str
    weight: float
    question_count: int
    proportion: float


class ContractAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exam_points: list[ExamPointProportion] = Field(default_factory=list)
    type_counts: dict[str, int] = Field(default_factory=dict)
    difficulty_counts: dict[str, int] = Field(default_factory=dict)


class PaperContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_score: float
    slots: list[ContractSlot]
    conflicts: list[ContractConflict] = Field(default_factory=list)
    audit_summary: ContractAuditSummary = Field(default_factory=ContractAuditSummary)


@dataclass(frozen=True)
class PoolAtom:
    """考点原子池中的一条候选原子。"""
    card_id: str
    unit_id: str
    exam_point_id: str
    atom_text: str
    boundary: str
    centrality: float
    features: frozenset[str]

    @property
    def atom_key(self) -> str:
        return _normalized(self.atom_text)


def build_exam_point_pools(
    units: list[UnitCoverage],
    knowledge_cards: dict[str, dict],
    *,
    threshold: float = DEFAULT_CENTRALITY_THRESHOLD,
) -> dict[str, list[PoolAtom]]:
    """按考点聚合全部单元知识卡的原子，核心度门槛预过滤后按核心度降序。

    过滤后池子不足的冲突检测由上层合同分配器负责，本函数只产出池子。
    """
    pools: dict[str, list[PoolAtom]] = {}
    for unit in units:
        if not unit.exam_point_id:
            continue
        pool = pools.setdefault(unit.exam_point_id, [])
        for card_id in unit.card_ids:
            card = knowledge_cards.get(card_id)
            if not card:
                continue
            boundary = str(card.get("answer_boundary") or card.get("answer_proposition") or "")
            for raw in card.get("assessable_content", []):
                atom_text = str(raw or "").strip()
                if not atom_text:
                    continue
                centrality = compute_atom_centrality(card, atom_text)
                if centrality < threshold:
                    continue
                pool.append(
                    PoolAtom(
                        card_id=card_id,
                        unit_id=unit.unit_id,
                        exam_point_id=unit.exam_point_id,
                        atom_text=atom_text,
                        boundary=boundary,
                        centrality=centrality,
                        features=atom_bigram_features(atom_text),
                    )
                )
    for pool in pools.values():
        pool.sort(key=lambda a: -a.centrality)
    pools = {k: v for k, v in pools.items() if v}
    return pools


def cluster_pool_atoms(
    pool: list[PoolAtom], *, similarity_threshold: float = 0.5
) -> list[list[PoolAtom]]:
    """并查集聚类：bigram Jaccard > 阈值的原子归入同簇；共享术语锚的原子强制同簇。

    术语锚强制同簇堵住 bigram 的盲区：同一概念的不同表述
    （如围绕 QLoRA 的多张知识卡）字面相似度可能低于阈值，
    但共享的英文术语锚暴露了它们是同一概念。
    簇按最高核心度降序；簇内原子按核心度降序。O(n²) 对比对
    每考点 <100 原子的规模足够快。
    """
    parent = list(range(len(pool)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]  # 路径减半
            i = parent[i]
        return i

    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if jaccard_similarity(pool[i].features, pool[j].features) > similarity_threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    anchors = [_term_anchors(atom.atom_text) for atom in pool]
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if anchors[i] & anchors[j]:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, list[PoolAtom]] = {}
    for idx, atom in enumerate(pool):
        groups.setdefault(find(idx), []).append(atom)
    return sorted(
        (sorted(group, key=lambda a: -a.centrality) for group in groups.values()),
        key=lambda group: -max(a.centrality for a in group),
    )


def _pick_atom(
    clusters: list[list[PoolAtom]],
    cursor: int,
    used_keys: set[str],
    used_boundaries: list[str],
    cluster_supply: dict[int, int],
    cluster_first_type: dict[int, str],
    item_type: str,
) -> tuple[PoolAtom | None, int, int]:
    """从 cursor 所在簇开始轮转，找第一个未用且边界互斥的原子。

    簇配额：每簇最多供给 MAX_QUESTIONS_PER_CLUSTER 题，满员跳过；
    同簇第 2 题必须与该簇第 1 题题型不同，题型撞车跳过该簇。
    返回 (原子, 下一个 cursor, 命中簇下标)；无候选返回 (None, cursor, -1)。
    """
    n = len(clusters)
    for step in range(n):
        idx = (cursor + step) % n
        supplied = cluster_supply.get(idx, 0)
        if supplied >= MAX_QUESTIONS_PER_CLUSTER:
            continue  # 该簇配额已满
        if supplied == 1 and cluster_first_type.get(idx) == item_type:
            continue  # 同簇第 2 题题型互斥
        for atom in clusters[idx]:  # 簇内已按核心度降序
            if atom.atom_key in used_keys:
                continue
            if any(b and boundaries_overlap(atom.boundary, b) for b in used_boundaries):
                continue
            return atom, (idx + 1) % n, idx
    return None, cursor, -1


def assign_atoms_to_items(
    items: list[PlanItem],
    clusters: list[list[PoolAtom]],
    *,
    shared_used_keys: set[str] | None = None,
    shared_used_boundaries: list[str] | None = None,
) -> tuple[list[tuple[PlanItem, PoolAtom]], list[ContractConflict]]:
    """同考点题位按 item_index 顺序，簇轮转 + 答案域互斥地取原子。

    构造性保证：跨簇优先（子主题不重复）、atom_key 唯一、
    答案边界互斥（任何两题答案不可互相包含）、
    同簇最多 MAX_QUESTIONS_PER_CLUSTER 题且两题题型不同（同概念不扎堆）。
    传入 shared_used_keys/shared_used_boundaries 时直接读写共享集，
    使多个考点调用间互斥状态全卷贯通（与终检全卷两两比较口径一致）；
    不传则每次调用独立维护局部集。簇数不足题数时轮转绕回同簇取下一个
    可用原子（受簇配额与题型互斥约束）；耗尽则报冲突，不静默降级。
    """
    used_keys: set[str] = shared_used_keys if shared_used_keys is not None else set()
    used_boundaries: list[str] = (
        shared_used_boundaries if shared_used_boundaries is not None else []
    )
    assignments: list[tuple[PlanItem, PoolAtom]] = []
    conflicts: list[ContractConflict] = []
    cluster_supply: dict[int, int] = {}
    cluster_first_type: dict[int, str] = {}
    cursor = 0
    for item in items:
        if not clusters:
            conflicts.append(ContractConflict(
                code="cluster_exhausted", exam_point_id=item.exam_point_id or "",
                message=f"题位 {item.item_index} 无可用原子簇",
                detail={"item_index": item.item_index},
            ))
            continue
        atom, cursor, cluster_idx = _pick_atom(
            clusters, cursor, used_keys, used_boundaries,
            cluster_supply, cluster_first_type, item.question_type,
        )
        if atom is None:
            conflicts.append(ContractConflict(
                code="cluster_exhausted", exam_point_id=item.exam_point_id or "",
                message=f"题位 {item.item_index} 的原子池已耗尽（互斥、去重或簇配额后无候选）",
                detail={"item_index": item.item_index},
            ))
            continue
        used_keys.add(atom.atom_key)
        used_boundaries.append(atom.boundary)
        cluster_supply[cluster_idx] = cluster_supply.get(cluster_idx, 0) + 1
        if cluster_supply[cluster_idx] == 1:
            cluster_first_type[cluster_idx] = item.question_type
        assignments.append((item, atom))
    return assignments, conflicts
