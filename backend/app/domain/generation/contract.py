"""试卷合同领域模型：命题前把整卷题位算死。

全局约束（不重复、不抄袭、比例对、不冷门）在合同分配阶段构造性保证。
"""
from __future__ import annotations

import random
import re

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.blueprint.models import PlanItem, UnitCoverage
from app.domain.generation.archetypes import ComprehensiveArchetype, MaterialForm
from app.domain.generation.coverage import _normalized, _validate_comprehensive_contract
from app.domain.knowledge.relevance import semantic_text_key

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
    # 多子句复合原子降权：填空/判断题难以承载双子句语义，
    # 优先把题位留给单句原子（切分兜底之外的第二道防线）
    if "；" in atom_text or ";" in atom_text:
        score -= 0.10
    relations = card.get("relation_edges", [])
    if isinstance(relations, list):
        score += min(len(relations) * 0.03, 0.10)
    return min(max(score, 0.0), 1.0)


def atom_bigram_features(atom_text: str) -> frozenset[str]:
    cleaned = _normalized(atom_text)
    return frozenset(cleaned[i : i + 2] for i in range(len(cleaned) - 1))


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
    concept_cluster: str = ""

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
                        concept_cluster=str(card.get("concept_cluster") or ""),
                    )
                )
    for pool in pools.values():
        pool.sort(key=lambda a: -a.centrality)
    pools = {k: v for k, v in pools.items() if v}
    return pools


def cluster_pool_atoms(
    pool: list[PoolAtom], *, similarity_threshold: float = 0.5
) -> list[list[PoolAtom]]:
    """并查集聚类：bigram Jaccard > 阈值、共享术语锚或同 concept_cluster 的原子强制同簇。

    术语锚强制同簇堵住 bigram 的盲区：同一概念的不同表述
    （如围绕 QLoRA 的多张知识卡）字面相似度可能低于阈值，
    但共享的英文术语锚暴露了它们是同一概念。
    concept_cluster 是语义画像阶段已产出的概念簇标签：纯中文的
    同簇原子（如"提示词要素"系列枚举事实）既无英文锚、bigram 又
    被枚举项稀释，唯一可靠信号就是该标签，等值即强制同簇。
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
    cluster_labels = [semantic_text_key(atom.concept_cluster) for atom in pool]
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            same_cluster_label = (
                bool(cluster_labels[i])
                and cluster_labels[i] == cluster_labels[j]
            )
            if same_cluster_label or anchors[i] & anchors[j]:
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
    used_keys: set[str],
    used_boundaries: list[str],
    cluster_supply: dict[int, int],
    cluster_type_supply: dict[int, dict[str, int]],
    previous_cluster: int | None,
    item_type: str,
    selected_atoms: list[PoolAtom],
    rng: random.Random | None = None,
) -> tuple[PoolAtom | None, int]:
    """约束过滤 + 软评分贪心：在全部可用原子中选分散度最优者。

    候选过滤只剩硬约束（原子未被用过、答案域与已选互斥）；多样性只进
    评分不进过滤：
    score = (该原子所在簇已供题数, 该簇已用当前题型次数, 与前一题位同簇,
             题型适配惩罚, 与全部已选原子的最大 bigram Jaccard, 种子扰动)，
    tuple 越小越优。
    题型适配惩罚：填空题要求简短唯一答案，多子句原子或长答案域
    （归一化 > 30 字符）的原子适配度差，仅对 fill_blank 题位计 1。
    种子扰动只在其余各项完全并列时打破平局（rng=None 时取 0 保持
    原确定性）：同种子可复现，异种子在富余池上换出不同原子组合。
    池充足时自然跨簇分散、簇内题型错开；池紧张时自动退化同簇多题但
    仍最大化分散——多样性本身永不报错，只有真正的原子耗尽才无候选。
    并列取先遍历到者（簇与簇内均按核心度降序 → 高核心度优先）。
    返回 (原子, 命中簇下标)；无候选返回 (None, -1)。
    """
    best: PoolAtom | None = None
    best_cluster = -1
    best_score: tuple[int, int, int, int, float, float] | None = None
    for cluster_idx, cluster in enumerate(clusters):
        supply = cluster_supply.get(cluster_idx, 0)
        type_supply = cluster_type_supply.get(cluster_idx, {}).get(item_type, 0)
        adjacent = 1 if cluster_idx == previous_cluster else 0
        for atom in cluster:  # 簇内已按核心度降序
            if atom.atom_key in used_keys:
                continue
            if any(b and boundaries_overlap(atom.boundary, b) for b in used_boundaries):
                continue
            type_fit = 0
            if item_type == "fill_blank" and (
                "；" in atom.atom_text
                or ";" in atom.atom_text
                or len(_normalized(atom.boundary)) > 30
            ):
                type_fit = 1
            max_jaccard = max(
                (jaccard_similarity(atom.features, other.features)
                 for other in selected_atoms),
                default=0.0,
            )
            tiebreak = rng.random() if rng is not None else 0.0
            score = (supply, type_supply, adjacent, type_fit, max_jaccard, tiebreak)
            if best_score is None or score < best_score:
                best, best_cluster, best_score = atom, cluster_idx, score
    return best, best_cluster


def assign_atoms_to_items(
    items: list[PlanItem],
    clusters: list[list[PoolAtom]],
    *,
    shared_used_keys: set[str] | None = None,
    shared_used_boundaries: list[str] | None = None,
    seed: int | None = None,
) -> tuple[list[tuple[PlanItem, PoolAtom]], list[ContractConflict]]:
    """同考点题位按 item_index 顺序，软评分贪心 + 答案域互斥地取原子。

    构造性保证（硬约束）：atom_key 唯一、答案边界互斥（任何两题答案
    不可互相包含）。多样性（跨簇分散、簇内题型错开、避免相邻题位同簇、
    文本相似错开）只是贪心评分的软目标：池充足时自然分散，池紧张时
    自动退化同簇多题但最大化分散，永不因多样性本身报错。
    seed：仅打破评分并列（rng=None 保持原确定性）。同种子复现同卷，
    异种子在富余池上选出不同原子组合——池刚够配额时无论种子如何
    都只能全选，这也是抽取目标须大于配额的原因。
    传入 shared_used_keys/shared_used_boundaries 时直接读写共享集，
    使多个考点调用间互斥状态全卷贯通（与终检全卷两两比较口径一致）；
    不传则每次调用独立维护局部集。硬过滤后无候选（真正的原子池耗尽）
    则报冲突，不静默降级。
    """
    used_keys: set[str] = shared_used_keys if shared_used_keys is not None else set()
    used_boundaries: list[str] = (
        shared_used_boundaries if shared_used_boundaries is not None else []
    )
    rng = random.Random(seed) if seed is not None else None
    assignments: list[tuple[PlanItem, PoolAtom]] = []
    conflicts: list[ContractConflict] = []
    cluster_supply: dict[int, int] = {}
    cluster_type_supply: dict[int, dict[str, int]] = {}
    previous_cluster: int | None = None
    selected_atoms: list[PoolAtom] = []
    for item in items:
        if not clusters:
            conflicts.append(ContractConflict(
                code="cluster_exhausted", exam_point_id=item.exam_point_id or "",
                message=f"题位 {item.item_index} 无可用原子簇",
                detail={"item_index": item.item_index},
            ))
            continue
        atom, cluster_idx = _pick_atom(
            clusters, used_keys, used_boundaries,
            cluster_supply, cluster_type_supply, previous_cluster,
            item.question_type, selected_atoms, rng,
        )
        if atom is None:
            conflicts.append(ContractConflict(
                code="cluster_exhausted", exam_point_id=item.exam_point_id or "",
                message=f"题位 {item.item_index} 的原子池已耗尽（互斥、去重后无候选）",
                detail={"item_index": item.item_index},
            ))
            continue
        used_keys.add(atom.atom_key)
        used_boundaries.append(atom.boundary)
        cluster_supply[cluster_idx] = cluster_supply.get(cluster_idx, 0) + 1
        types = cluster_type_supply.setdefault(cluster_idx, {})
        types[item.question_type] = types.get(item.question_type, 0) + 1
        previous_cluster = cluster_idx
        selected_atoms.append(atom)
        assignments.append((item, atom))
    return assignments, conflicts
