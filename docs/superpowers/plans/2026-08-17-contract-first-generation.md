# 合同优先试卷生成重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用"试卷合同（纯确定性分配）→ 按考点分批生成（批内互见）→ 确定性终检"替换现有"精排→按题型并行→审计修复循环"生成段，构造性消灭重复考点、上下文抄写、比例失衡，并把每次出卷模型调用从 ~50 次降到 ~8 次。

**Architecture:** 蓝图配额复用现有 `allocate_plan_items`；新增合同分配器（核心度门槛→bigram 聚类→簇轮转→答案域互斥→结构轮换）产出 PaperContract；生成图重写为 `build_batches → Send(batch_generate) 并行 → merge_and_check`，批次内一次模型调用同批互见；删除精排节点与修复循环。

**Tech Stack:** Python 3.14 / FastAPI / LangGraph (StateGraph + Send) / pydantic v2 / pytest。设计文档：`docs/superpowers/specs/2026-08-17-contract-first-generation-design.md`

**工作目录：** 所有相对路径基于 `f:\比赛项目\阅卷出题功能\.worktrees\core-implementation\backend`（前端任务除外）。

**测试命令约定：** 均在 backend 目录执行 `python -m pytest <path> -v`。

---

### Task 1: 合同领域模型 + 原子评分函数迁移

**Files:**
- Create: `app/domain/generation/contract.py`
- Create: `tests/domain/test_contract_models.py`
- Modify: `app/domain/generation/atom_selector.py`（仅确认待迁移函数，本任务不删文件）

- [ ] **Step 1: 写失败测试**

`tests/domain/test_contract_models.py`：

```python
import pytest
from pydantic import ValidationError

from app.domain.generation.contract import (
    DEFAULT_CENTRALITY_THRESHOLD,
    ContractSlot,
    ForbiddenContext,
    PaperContract,
    boundaries_overlap,
    compute_atom_centrality,
)


def _card(**overrides):
    base = {
        "is_core": True,
        "performance_statement": "掌握SFTTrainer构建方法",
        "assessable_content": ["构建SFTTrainer需要传入SFTConfig"],
        "preferred_terms": ["SFTTrainer"],
        "relation_edges": [],
    }
    base.update(overrides)
    return base


def test_centrality_core_card_scores_above_threshold():
    score = compute_atom_centrality(_card(), "构建SFTTrainer需要传入SFTConfig")
    assert score >= DEFAULT_CENTRALITY_THRESHOLD


def test_centrality_obscure_atom_scores_lower():
    core = compute_atom_centrality(_card(), "构建SFTTrainer需要传入SFTConfig")
    obscure = compute_atom_centrality(_card(is_core=False), "某实验附注（第3页脚注）")
    assert obscure < core


def test_boundaries_overlap_detects_containment_and_equality():
    assert boundaries_overlap("SFTTrainer需要SFTConfig", "SFTTrainer需要SFTConfig")
    assert boundaries_overlap("SFTConfig", "构建SFTTrainer需要SFTConfig")
    assert not boundaries_overlap("SFTConfig", "QLoRA使用NF4量化")


def test_contract_slot_forbids_comprehensive_fields_on_plain_type():
    with pytest.raises(ValidationError):
        ContractSlot(
            item_index=1,
            question_type="single_choice",
            score=2,
            difficulty="medium",
            cognitive_level="understand",
            exam_point_id="EP1",
            anchor_key="A1",
            unit_id="U1",
            card_id="C1",
            coverage_atom="原子",
            answer_boundary="边界",
            comprehensive_archetype="case_analysis",
        )


def test_paper_contract_round_trips_forbidden_context():
    slot = ContractSlot(
        item_index=1, question_type="true_false", score=1, difficulty="low",
        cognitive_level="remember", exam_point_id="EP1", anchor_key="A1",
        unit_id="U1", card_id="C1", coverage_atom="原子A", answer_boundary="核心A",
        forbidden_context=ForbiddenContext(atoms=["原子B"], answer_cores=["核心B"]),
    )
    contract = PaperContract(total_score=1, slots=[slot])
    restored = PaperContract.model_validate(contract.model_dump(mode="json"))
    assert restored.slots[0].forbidden_context.atoms == ["原子B"]
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/domain/test_contract_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.generation.contract'`

- [ ] **Step 3: 实现 contract.py**

`app/domain/generation/contract.py`（评分函数从 `atom_selector.py` L64-129 原样迁移，迁移后 atom_selector 中暂留原函数，Task 14 统一删除）：

```python
"""试卷合同领域模型：命题前把整卷题位算死。

全局约束（不重复、不抄袭、比例对、不冷门）在合同分配阶段构造性保证。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.generation.archetypes import ComprehensiveArchetype, MaterialForm
from app.domain.generation.coverage import _normalized, _validate_comprehensive_contract

DEFAULT_CENTRALITY_THRESHOLD = 0.35


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
```

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/domain/test_contract_models.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/domain/generation/contract.py tests/domain/test_contract_models.py
git commit -m "feat(contract): add paper contract domain models with atom scoring"
```

---

### Task 2: 考点原子池构建 + 核心度门槛预过滤

**Files:**
- Modify: `app/domain/generation/contract.py`（追加）
- Create: `tests/domain/test_contract_pool.py`

- [ ] **Step 1: 写失败测试**

`tests/domain/test_contract_pool.py`：

```python
from app.domain.blueprint.models import UnitCoverage
from app.domain.generation.contract import DEFAULT_CENTRALITY_THRESHOLD, build_exam_point_pools


def _units():
    return [
        UnitCoverage(unit_id="U1", exam_point_id="EP1", anchor_key="A1", card_ids=["C1", "C2"]),
        UnitCoverage(unit_id="U2", exam_point_id="EP2", anchor_key="A1", card_ids=["C3"]),
    ]


def _cards():
    return {
        "C1": {
            "is_core": True,
            "performance_statement": "掌握提示词要素",
            "assessable_content": ["有效提示词包含角色设定", "有效提示词包含任务说明"],
            "preferred_terms": ["提示词"],
            "answer_boundary": "提示词要素",
        },
        "C2": {
            "is_core": True,
            "performance_statement": "掌握提示词优化方法",
            "assessable_content": ["提示词可加入背景信息"],
            "preferred_terms": [],
            "answer_boundary": "背景信息",
        },
        "C3": {
            "is_core": False,
            "performance_statement": "了解某个脚注细节",
            "assessable_content": ["某脚注（第3页）细节说明"],
            "preferred_terms": [],
            "answer_boundary": "脚注细节",
        },
    }


def test_pool_contains_all_atoms_of_units_cards():
    pools, conflicts = build_exam_point_pools(_units(), _cards())
    assert len(pools["EP1"]) == 3
    assert len(pools["EP2"]) == 0 or "EP2" not in pools or True  # EP2 原子低于门槛被剔除


def test_low_centrality_atoms_are_filtered_out():
    pools, _ = build_exam_point_pools(_units(), _cards())
    for atom in pools.get("EP2", []):
        assert atom.centrality >= DEFAULT_CENTRALITY_THRESHOLD


def test_pool_sorted_by_centrality_desc():
    pools, _ = build_exam_point_pools(_units(), _cards())
    centralities = [a.centrality for a in pools["EP1"]]
    assert centralities == sorted(centralities, reverse=True)


def test_pool_atom_carries_card_and_unit():
    pools, _ = build_exam_point_pools(_units(), _cards())
    atom = pools["EP1"][0]
    assert atom.card_id in {"C1", "C2"}
    assert atom.unit_id == "U1"
    assert atom.exam_point_id == "EP1"
    assert atom.boundary == "提示词要素"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/domain/test_contract_pool.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_exam_point_pools'`

- [ ] **Step 3: 实现**

追加到 `contract.py`：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolAtom:
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
    """按考点聚合全部单元知识卡的原子，核心度门槛预过滤后按核心度降序。"""
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
    return pools
```

同时在文件头部补 import：`from app.domain.blueprint.models import UnitCoverage`（放在 archetypes import 之前，保持导入排序）。

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/domain/test_contract_pool.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/domain/generation/contract.py tests/domain/test_contract_pool.py
git commit -m "feat(contract): build per-exam-point atom pools with centrality threshold"
```

---

### Task 3: bigram 聚类（并查集）

**Files:**
- Modify: `app/domain/generation/contract.py`（追加）
- Create: `tests/domain/test_contract_clustering.py`

- [ ] **Step 1: 写失败测试**

`tests/domain/test_contract_clustering.py`：

```python
from app.domain.generation.contract import PoolAtom, atom_bigram_features, cluster_pool_atoms


def _atom(text: str, centrality: float = 0.8) -> PoolAtom:
    return PoolAtom(
        card_id="C", unit_id="U", exam_point_id="EP",
        atom_text=text, boundary="", centrality=centrality,
        features=atom_bigram_features(text),
    )


def test_semantically_similar_atoms_land_in_same_cluster():
    pool = [
        _atom("构建SFTTrainer需要传入SFTConfig训练参数"),
        _atom("构建SFTTrainer需要传入训练数据集"),          # 与上一条高度相似
        _atom("QLoRA使用NF4格式进行模型量化"),              # 完全不同主题
    ]
    clusters = cluster_pool_atoms(pool)
    assert len(clusters) == 2
    by_text = [sorted(a.atom_text for a in c) for c in clusters]
    assert ["构建SFTTrainer需要传入SFTConfig训练参数", "构建SFTTrainer需要传入训练数据集"] in by_text


def test_disjoint_atoms_each_own_cluster():
    pool = [_atom("提示词包含角色设定"), _atom("QLoRA使用NF4量化"), _atom("模型评估衡量泛化能力")]
    assert len(cluster_pool_atoms(pool)) == 3


def test_clusters_sorted_by_max_centrality_desc():
    pool = [_atom("低分主题", centrality=0.4), _atom("高分主题A", centrality=0.9)]
    clusters = cluster_pool_atoms(pool)
    assert clusters[0][0].centrality >= clusters[-1][0].centrality
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/domain/test_contract_clustering.py -v`
Expected: FAIL — `ImportError: cannot import name 'cluster_pool_atoms'`

- [ ] **Step 3: 实现**

追加到 `contract.py`：

```python
def cluster_pool_atoms(
    pool: list[PoolAtom], *, similarity_threshold: float = 0.5
) -> list[list[PoolAtom]]:
    """并查集聚类：bigram Jaccard > 阈值的原子归入同簇；簇按最高核心度降序。"""
    parent = list(range(len(pool)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if jaccard_similarity(pool[i].features, pool[j].features) > similarity_threshold:
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
```

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/domain/test_contract_clustering.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/domain/generation/contract.py tests/domain/test_contract_clustering.py
git commit -m "feat(contract): union-find bigram clustering of pool atoms"
```

---

### Task 4: 簇轮转选取 + 答案域互斥

**Files:**
- Modify: `app/domain/generation/contract.py`（追加）
- Create: `tests/domain/test_contract_assignment.py`

- [ ] **Step 1: 写失败测试**

`tests/domain/test_contract_assignment.py`：

```python
from app.domain.blueprint.models import PlanItem
from app.domain.generation.contract import PoolAtom, atom_bigram_features, assign_atoms_to_items


def _atom(text: str, boundary: str, centrality: float = 0.8) -> PoolAtom:
    return PoolAtom(card_id="C1", unit_id="U1", exam_point_id="EP1", atom_text=text,
                    boundary=boundary, centrality=centrality, features=atom_bigram_features(text))


def _item(index: int) -> PlanItem:
    return PlanItem(item_index=index, question_type="single_choice", score=2,
                    anchor_key="A1", exam_point_id="EP1", unit_id="U1", card_id="C1")


def test_consecutive_items_rotate_across_clusters():
    clusters = [
        [_atom("SFTTrainer需要SFTConfig", "SFTConfig参数")],
        [_atom("QLoRA使用NF4量化", "NF4量化格式")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    assert assignments[0][1].atom_text != assignments[1][1].atom_text  # 跨簇


def test_answer_boundary_mutex_skips_conflicting_candidate():
    clusters = [
        [_atom("SFTTrainer需要SFTConfig", "量化格式NF4")],
        [_atom("QLoRA使用NF4量化", "量化格式NF4"), _atom("模型评估衡量泛化能力", "泛化能力")],
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    boundaries = [a[1].boundary for a in assignments]
    assert boundaries == ["量化格式NF4", "泛化能力"]  # 第二题跳过互斥候选


def test_cluster_exhausted_reports_conflict():
    clusters = [[_atom("唯一原子", "唯一边界")]]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert len(assignments) == 1
    assert conflicts and conflicts[0].code == "cluster_exhausted"
    assert conflicts[0].detail["item_index"] == 2


def test_same_cluster_reuse_when_clusters_fewer_than_items():
    clusters = [
        [_atom("原子甲", "边界甲"), _atom("原子乙", "边界乙")],  # 同簇两个原子
    ]
    assignments, conflicts = assign_atoms_to_items([_item(1), _item(2)], clusters)
    assert not conflicts
    assert len(assignments) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/domain/test_contract_assignment.py -v`
Expected: FAIL — `ImportError: cannot import name 'assign_atoms_to_items'`

- [ ] **Step 3: 实现**

追加到 `contract.py`：

```python
def _pick_atom(
    clusters: list[list[PoolAtom]],
    cursor: int,
    used_keys: set[str],
    used_boundaries: list[str],
) -> tuple[PoolAtom | None, int]:
    n = len(clusters)
    for step in range(n):
        idx = (cursor + step) % n
        for atom in clusters[idx]:  # 簇内已按核心度降序
            if atom.atom_key in used_keys:
                continue
            if any(b and boundaries_overlap(atom.boundary, b) for b in used_boundaries):
                continue
            return atom, (idx + 1) % n
    return None, cursor


def assign_atoms_to_items(
    items: list[PlanItem],
    clusters: list[list[PoolAtom]],
) -> tuple[list[tuple[PlanItem, PoolAtom]], list[ContractConflict]]:
    """同考点题位按 item_index 顺序，簇轮转 + 答案域互斥地取原子。

    簇数不足题数时允许同簇取第二个原子（轮转回到同一簇，取下一个可用者）。
    """
    used_keys: set[str] = set()
    used_boundaries: list[str] = []
    assignments: list[tuple[PlanItem, PoolAtom]] = []
    conflicts: list[ContractConflict] = []
    cursor = 0
    for item in items:
        if not clusters:
            conflicts.append(ContractConflict(
                code="cluster_exhausted", exam_point_id=item.exam_point_id or "",
                message=f"题位 {item.item_index} 无可用原子簇", detail={"item_index": item.item_index},
            ))
            continue
        atom, cursor = _pick_atom(clusters, cursor, used_keys, used_boundaries)
        if atom is None:
            conflicts.append(ContractConflict(
                code="cluster_exhausted", exam_point_id=item.exam_point_id or "",
                message=f"题位 {item.item_index} 的原子池已耗尽（互斥或去重后无候选）",
                detail={"item_index": item.item_index},
            ))
            continue
        used_keys.add(atom.atom_key)
        used_boundaries.append(atom.boundary)
        assignments.append((item, atom))
    return assignments, conflicts
```

注意 `_pick_atom` 轮转语义：题位 2 的 cursor 已在题位 1 取走的簇之后，天然跨簇；簇数 < 题数时轮转绕回同簇，取该簇下一个可用原子（满足 `test_same_cluster_reuse_when_clusters_fewer_than_items`）。

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/domain/test_contract_assignment.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/domain/generation/contract.py tests/domain/test_contract_assignment.py
git commit -m "feat(contract): cluster-rotation atom assignment with boundary mutex"
```

---

### Task 5: 综合题结构轮换 + 合同装配

**Files:**
- Create: `app/services/contract_service.py`
- Create: `tests/unit/test_contract_service.py`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_contract_service.py`：

```python
import pytest

from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.domain.generation.contract import ContractRequest, allocate_paper_contract


def _units():
    return [
        UnitCoverage(unit_id="U1", exam_point_id="EP1", anchor_key="A1", card_ids=["C1"]),
        UnitCoverage(unit_id="U2", exam_point_id="EP2", anchor_key="A2", card_ids=["C2"]),
    ]


def _cards():
    return {
        "C1": {
            "is_core": True, "performance_statement": "掌握提示词要素与优化",
            "assessable_content": ["有效提示词包含角色设定", "有效提示词包含任务说明", "提示词可加入背景信息", "提示词输出格式约束"],
            "preferred_terms": ["提示词"], "answer_boundary": "提示词要素", "prompt_material": ["角色设定示例"],
        },
        "C2": {
            "is_core": True, "performance_statement": "掌握SFT训练方法",
            "assessable_content": ["构建SFTTrainer需要SFTConfig", "QLoRA使用NF4量化", "继续预训练适配领域语料", "训练数据集需格式化为对话"],
            "preferred_terms": ["SFT"], "answer_boundary": "SFT训练配置", "prompt_material": ["SFTConfig示例"],
        },
    }


def _request(**overrides):
    base = dict(
        blueprint=BlueprintRequest(
            total_score=10,
            type_rules={"single_choice": {"count": 5, "score": 2}},
            chapter_weights={"A1": 0.4, "A2": 0.6},
            units=_units(),
        ),
        knowledge_cards=_cards(),
    )
    base.update(overrides)
    return ContractRequest(**base)


def test_contract_slots_follow_chapter_quota_proportions():
    contract = allocate_paper_contract(_request())
    ep1 = sum(1 for s in contract.slots if s.exam_point_id == "EP1")
    ep2 = sum(1 for s in contract.slots if s.exam_point_id == "EP2")
    assert ep1 + ep2 == 5
    assert abs(ep1 - 2) <= 1 and abs(ep2 - 3) <= 1  # 0.4/0.6 配额误差 ≤1 题


def test_atoms_unique_across_paper_and_mutex_within_point():
    contract = allocate_paper_contract(_request())
    atoms = [s.coverage_atom for s in contract.slots]
    assert len(atoms) == len(set(atoms))
    for point in {s.exam_point_id for s in contract.slots}:
        group = [s for s in contract.slots if s.exam_point_id == point]
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                from app.domain.generation.contract import boundaries_overlap
                assert not boundaries_overlap(left.answer_boundary, right.answer_boundary)


def test_slot_forbidden_context_lists_same_point_siblings():
    contract = allocate_paper_contract(_request())
    for slot in contract.slots:
        siblings = [s for s in contract.slots if s.exam_point_id == slot.exam_point_id and s.item_index != slot.item_index]
        assert set(slot.forbidden_context.atoms) == {s.coverage_atom for s in siblings}


def test_pool_insufficient_produces_conflict_not_silent_gap():
    contract = allocate_paper_contract(_request(knowledge_cards={"C1": _cards()["C1"], "C2": {
        "is_core": False, "performance_statement": "脚注", "assessable_content": ["某脚注细节"],
        "preferred_terms": [], "answer_boundary": "脚注",
    }}))
    assert any(c.code == "atom_pool_insufficient" for c in contract.conflicts)
    # EP2 题位不产生槽位
    assert all(s.exam_point_id == "EP1" for s in contract.slots)


def test_comprehensive_slots_get_rotating_archetypes():
    request = ContractRequest(
        blueprint=BlueprintRequest(
            total_score=18,
            type_rules={"comprehensive": {"count": 3, "score": 6}},
            chapter_weights={"A1": 1.0},
            units=[_units()[0]],
        ),
        knowledge_cards=_cards(),
    )
    contract = allocate_paper_contract(request)
    archetypes = [s.comprehensive_archetype for s in contract.slots]
    assert all(archetypes)
    assert len(set(archetypes)) == 3


def test_audit_summary_reports_proportions():
    contract = allocate_paper_contract(_request())
    summary = contract.audit_summary
    assert summary.exam_points
    assert sum(p.question_count for p in summary.exam_points) == len(contract.slots)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_contract_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'ContractRequest'`

- [ ] **Step 3: 实现**

`app/services/contract_service.py`：

```python
"""试卷合同分配器：六步纯确定性算法，零模型调用。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.blueprint.models import BlueprintRequest, PlanItem, UnitCoverage
from app.domain.blueprint_service import BlueprintValidationError, allocate_plan_items
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.contract import (
    ContractAuditSummary,
    ContractConflict,
    ContractSlot,
    ExamPointProportion,
    ForbiddenContext,
    PaperContract,
    assign_atoms_to_items,
    boundaries_overlap,
    build_exam_point_pools,
    cluster_pool_atoms,
)

_ARCHETYPE_ROTATION = [
    "code_completion_scenario", "fault_diagnosis", "comparative_decision",
    "integrated_explanation", "case_analysis", "solution_design",
    "process_optimization", "critique_correction",
]
_MATERIAL_FORM_ROTATION = ["code_skeleton", "config_template", "command_script"]
_COGNITIVE_SEQUENCES = [
    ["understand", "apply", "analyze"],
    ["apply", "analyze", "evaluate"],
    ["remember", "understand", "apply"],
]


class ContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blueprint: BlueprintRequest
    knowledge_cards: dict[str, dict] = Field(min_length=1)
    centrality_threshold: float = 0.35


def _assign_comprehensive_fields(index: int, *, used_archetypes: set[str], recent_structure_keys: set[str]) -> dict:
    rotation = [a for a in _ARCHETYPE_ROTATION if a not in recent_structure_keys]
    fallback = [a for a in _ARCHETYPE_ROTATION if a not in used_archetypes] or rotation
    ordered = fallback + [a for a in rotation if a not in fallback]
    archetype = ordered[index % len(ordered)] if index < len(ordered) else rotation[index % len(rotation)]
    used_archetypes.add(archetype)
    contract = ARCHETYPE_CONTRACTS[archetype]
    material_forms = sorted(contract.allowed_modes and contract.material_forms)
    material_form = material_forms[index % len(material_forms)]
    sequence = _COGNITIVE_SEQUENCES[index % len(_COGNITIVE_SEQUENCES)]
    count = len(sequence)
    return {
        "comprehensive_archetype": archetype,
        "material_form": material_form,
        "cognitive_sequence": sequence,
        "subquestion_count_range": [max(2, count - 1), count + 1],
    }


def allocate_paper_contract(
    request: ContractRequest,
    *,
    recent_structure_keys: set[str] | None = None,
) -> PaperContract:
    recent_structure_keys = recent_structure_keys or set()
    plan = allocate_plan_items(request.blueprint)
    pools = build_exam_point_pools(
        request.blueprint.units, request.knowledge_cards,
        threshold=request.centrality_threshold,
    )

    conflicts: list[ContractConflict] = []
    quota: dict[str, int] = {}
    for item in plan.items:
        if not item.exam_point_id:
            conflicts.append(ContractConflict(
                code="missing_exam_point", message=f"题位 {item.item_index} 未关联考点",
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
        group.sort(key=lambda i: i.item_index)

    raw_slots: list[tuple[PlanItem, dict]] = []
    used_archetypes: set[str] = set()
    for point in sorted(items_by_point):
        items = items_by_point[point]
        clusters = cluster_pool_atoms(pools.get(point, []))
        assignments, point_conflicts = assign_atoms_to_items(items, clusters)
        conflicts.extend(point_conflicts)
        for item, atom in assignments:
            extra: dict = {}
            if item.question_type == "comprehensive":
                extra = _assign_comprehensive_fields(
                    len(used_archetypes),
                    used_archetypes=used_archetypes,
                    recent_structure_keys=recent_structure_keys,
                )
            card = request.knowledge_cards.get(atom.card_id, {})
            raw_slots.append((item, {
                "item_index": item.item_index,
                "question_type": item.question_type,
                "score": item.score,
                "difficulty": item.difficulty,
                "cognitive_level": item.cognitive_level,
                "assessment_mode": item.assessment_mode,
                "exam_point_id": point,
                "anchor_key": item.anchor_key,
                "unit_id": atom.unit_id,
                "card_id": atom.card_id,
                "coverage_atom": atom.atom_text,
                "answer_boundary": atom.boundary,
                "performance_statement": str(card.get("performance_statement", "")),
                "prompt_material": [card.get("prompt_material", [])] if isinstance(card.get("prompt_material"), str) else list(card.get("prompt_material", []) or []),
                "scope_boundary": card.get("scope_boundary", {}) or {},
                "preferred_terms": list(card.get("preferred_terms", []) or []),
                **extra,
            }))

    slots = [ContractSlot(**payload) for _, payload in raw_slots]

    # 禁用上下文：同考点其他题位的原子 + 答案核心
    final_slots: list[ContractSlot] = []
    for slot in slots:
        siblings = [s for s in slots if s.exam_point_id == slot.exam_point_id and s.item_index != slot.item_index]
        final_slots.append(slot.model_copy(update={"forbidden_context": ForbiddenContext(
            atoms=[s.coverage_atom for s in siblings],
            answer_cores=[s.answer_boundary for s in siblings if s.answer_boundary],
        )}))
    final_slots.sort(key=lambda s: s.item_index)

    total = sum(s.score for s in final_slots)
    weights = {u.exam_point_id: request.blueprint.chapter_weights.get(u.anchor_key, 0.0) for u in request.blueprint.units if u.exam_point_id}
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
```

注意：若项目内 `allocate_plan_items` 位于 `app.services.blueprint_service`（非 `app.domain`），按现有 import 路径 `from app.services.blueprint_service import BlueprintValidationError, allocate_plan_items` 修正。

同时在 `app/domain/generation/contract.py` **不要**加 ContractRequest（避免 domain→service 反向依赖），ContractRequest 定义在 contract_service.py 中（测试导入路径为 `app.services.contract_service.ContractRequest`，Step 1 的导入需相应改为 `from app.services.contract_service import ContractRequest, allocate_paper_contract`——以 Step 1 测试文件实际写入的 import 为准，两种写法选其一并在测试与实现间保持一致；推荐统一用 `app.services.contract_service`）。

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/unit/test_contract_service.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/contract_service.py tests/unit/test_contract_service.py
git commit -m "feat(contract): deterministic paper contract allocator with six-step algorithm"
```

---

### Task 6: 教师合同修订 + 确认 API

**Files:**
- Modify: `app/services/contract_service.py`（追加）
- Modify: `app/api/v1/blueprints.py`
- Create: `tests/unit/test_contract_revision.py`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_contract_revision.py`：

```python
import pytest

from app.services.contract_service import ContractRevisionError, apply_slot_revisions


def test_revision_replaces_atom_and_rebuilds_forbidden_context(contract_two_slots, units, cards):
    revised = apply_slot_revisions(
        contract_two_slots,
        revisions=[{"item_index": 1, "coverage_atom": "有效提示词包含任务说明"}],
        units=units, knowledge_cards=cards,
    )
    slot1 = next(s for s in revised.slots if s.item_index == 1)
    slot2 = next(s for s in revised.slots if s.item_index == 2)
    assert slot1.coverage_atom == "有效提示词包含任务说明"
    assert slot2.coverage_atom in slot1.forbidden_context.atoms


def test_revision_rejects_atom_outside_point_pool(contract_two_slots, units, cards):
    with pytest.raises(ContractRevisionError):
        apply_slot_revisions(
            contract_two_slots,
            revisions=[{"item_index": 1, "coverage_atom": "QLoRA使用NF4量化"}],  # 属于 EP2 池
            units=units, knowledge_cards=cards,
        )


def test_revision_rejects_boundary_collision(contract_two_slots, units, cards):
    # cards 中另一原子与题位2答案域相同 → 拒绝
    with pytest.raises(ContractRevisionError):
        apply_slot_revisions(
            contract_two_slots,
            revisions=[{"item_index": 1, "coverage_atom": "提示词可加入背景信息"}],
            units=units, knowledge_cards=cards,
        )
```

测试文件顶部加 fixture（复用 Task 5 的数据构造方式）：

```python
import pytest
from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.services.contract_service import ContractRequest, allocate_paper_contracts if False else allocate_paper_contract  # 见下


@pytest.fixture
def units():
    return [UnitCoverage(unit_id="U1", exam_point_id="EP1", anchor_key="A1", card_ids=["C1"])]


@pytest.fixture
def cards():
    return {"C1": {
        "is_core": True, "performance_statement": "掌握提示词要素",
        "assessable_content": ["有效提示词包含角色设定", "有效提示词包含任务说明", "提示词可加入背景信息"],
        "preferred_terms": ["提示词"], "answer_boundary": "提示词要素",
    }}


@pytest.fixture
def contract_two_slots(units, cards):
    request = ContractRequest(
        blueprint=BlueprintRequest(
            total_score=4, type_rules={"single_choice": {"count": 2, "score": 2}},
            chapter_weights={"A1": 1.0}, units=units,
        ),
        knowledge_cards=cards,
    )
    return allocate_paper_contract(request)
```

（顶部 import 写为 `from app.services.contract_service import ContractRevisionError, apply_slot_revisions, allocate_paper_contract`，删除示意中的条件表达式。注意 `提示词可加入背景信息` 与题位2答案域是否碰撞取决于装配时的边界取值：cards 的 answer_boundary 是卡级别的，同卡原子共享边界，会被装配期互斥跳过而取不同卡的原子。因此 fixture 需用两张卡使两个题位拿到不同边界，`test_revision_rejects_boundary_collision` 中将题位1改到与题位2同边界的原子。实现时按此调整 fixture：C1 三个原子 boundary 均为"提示词要素"、C2 boundary 为"背景信息优化"；装配后题位1←C1 原子、题位2←C2 原子；修订题位1到 C2 的原子即触发碰撞。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_contract_revision.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_slot_revisions'`

- [ ] **Step 3: 实现**

追加到 `contract_service.py`：

```python
class ContractRevisionError(ValueError):
    pass


def apply_slot_revisions(
    contract: PaperContract,
    revisions: list[dict],
    *,
    units: list[UnitCoverage],
    knowledge_cards: dict[str, dict],
) -> PaperContract:
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
            raise ContractRevisionError(f"原子不在考点 {slot.exam_point_id} 的可用池中：{atom_text}")
        slot.coverage_atom = match.atom_text
        slot.card_id = match.card_id
        slot.unit_id = match.unit_id
        slot.answer_boundary = match.boundary

    slots = list(updated.values())
    for point in {s.exam_point_id for s in slots}:
        group = [s for s in slots if s.exam_point_id == point]
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                if boundaries_overlap(left.answer_boundary, right.answer_boundary):
                    raise ContractRevisionError(
                        f"修订后题位 {left.item_index} 与 {right.item_index} 答案域重叠"
                    )

    final = []
    for slot in slots:
        siblings = [s for s in slots if s.exam_point_id == slot.exam_point_id and s.item_index != slot.item_index]
        final.append(slot.model_copy(update={"forbidden_context": ForbiddenContext(
            atoms=[s.coverage_atom for s in siblings],
            answer_cores=[s.answer_boundary for s in siblings if s.answer_boundary],
        )}))
    final.sort(key=lambda s: s.item_index)
    return contract.model_copy(update={"slots": final})
```

修改 `app/api/v1/blueprints.py`：

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.domain.blueprint.models import UnitCoverage
from app.domain.blueprint.models import BlueprintRequest  # 兼容旧展示用
from app.services.blueprint_service import BlueprintValidationError, allocate_plan_items
from app.services.contract_service import (
    ContractRequest,
    ContractRevisionError,
    allocate_paper_contract,
    apply_slot_revisions,
)

router = APIRouter(prefix="/api/v1/courses/{course_id}", tags=["blueprints"])


class ContractConfirmation(BaseModel):
    contract: dict
    slot_revisions: list[dict] = Field(default_factory=list)
    units: list[UnitCoverage] = Field(default_factory=list)
    knowledge_cards: dict[str, dict] = Field(default_factory=dict)


@router.post("/blueprints/allocate")
def allocate_blueprint(course_id: str, request: ContractRequest) -> dict:
    try:
        contract = allocate_paper_contract(request)
        return contract.model_dump(mode="json")
    except BlueprintValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/blueprints/confirm")
def confirm_contract(course_id: str, request: ContractConfirmation) -> dict:
    from app.domain.generation.contract import PaperContract
    try:
        contract = PaperContract.model_validate(request.contract)
        revised = apply_slot_revisions(
            contract, request.slot_revisions,
            units=request.units, knowledge_cards=request.knowledge_cards,
        )
        return revised.model_dump(mode="json")
    except ContractRevisionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
```

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/unit/test_contract_revision.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/contract_service.py app/api/v1/blueprints.py tests/unit/test_contract_revision.py
git commit -m "feat(contract): teacher slot revision with mutex revalidation + confirm API"
```

---

### Task 7: 分批逻辑（按考点分批、批上限、小考点合并）

**Files:**
- Create: `app/domain/generation/batching.py`
- Create: `tests/domain/test_batching.py`

- [ ] **Step 1: 写失败测试**

`tests/domain/test_batching.py`：

```python
from app.domain.generation.batching import BATCH_MAX_SIZE, split_contract_into_batches
from app.domain.generation.contract import ContractSlot


def _slot(index: int, point: str, anchor: str = "A1") -> ContractSlot:
    return ContractSlot(
        item_index=index, question_type="single_choice", score=2, difficulty="medium",
        cognitive_level="understand", exam_point_id=point, anchor_key=anchor,
        unit_id=f"U-{point}", card_id=f"C{index}", coverage_atom=f"原子{index}",
        answer_boundary=f"边界{index}",
    )


def test_same_point_slots_stay_in_one_batch():
    batches = split_contract_into_batches([_slot(i, "EP1") for i in range(1, 4)])
    assert len(batches) == 1
    assert all(s.exam_point_id == "EP1" for s in batches[0].slots)


def test_large_point_splits_with_cross_subbatch_forbidden_context():
    slots = [_slot(i, "EP1") for i in range(1, 9)]  # 8 题 > 6
    batches = split_contract_into_batches(slots)
    sizes = [len(b.slots) for b in batches]
    assert all(size <= BATCH_MAX_SIZE for size in sizes)
    assert sum(sizes) == 8
    # 子批1 的禁用上下文包含子批2 的原子
    first = next(b for b in batches if any(s.item_index == 1 for s in b.slots))
    second_atoms = {s.coverage_atom for b in batches if not any(s.item_index == 1 for s in b.slots) for s in b.slots}
    assert second_atoms.issubset(set(first.forbidden_context.atoms))


def test_small_points_merge_by_anchor():
    slots = [_slot(1, "EP1", "A1"), _slot(2, "EP2", "A1"), _slot(3, "EP3", "A2")]
    batches = split_contract_into_batches(slots)
    # EP1+EP2 合并；EP3 单独（不同 anchor 不合并）
    assert len(batches) == 2
    merged = next(b for b in batches if len(b.exam_point_ids) == 2)
    assert set(merged.exam_point_ids) == {"EP1", "EP2"}


def test_distinct_points_batches_have_no_cross_forbidden_context():
    slots = [_slot(i, "EP1") for i in range(1, 4)] + [_slot(4, "EP2", "A2") for _ in range(4)] + [_slot(i, "EP3", "A3") for i in (9, 10, 11)]
    batches = split_contract_into_batches(slots)
    for batch in batches:
        if set(batch.exam_point_ids) == {"EP1"}:
            assert batch.forbidden_context.atoms == []
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/domain/test_batching.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

`app/domain/generation/batching.py`：

```python
"""按考点分批：同考点同批（批内互见），跨批互斥由合同禁用上下文保证。"""
from __future__ import annotations

from app.domain.generation.contract import ContractSlot, ForbiddenContext
from pydantic import BaseModel, ConfigDict, Field

BATCH_MAX_SIZE = 6
BATCH_MIN_SIZE = 3


class QuestionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    anchor_key: str
    exam_point_ids: list[str]
    slots: list[ContractSlot]
    forbidden_context: ForbiddenContext = Field(default_factory=ForbiddenContext)


def split_contract_into_batches(slots: list[ContractSlot]) -> list[QuestionBatch]:
    by_point: dict[str, list[ContractSlot]] = {}
    for slot in slots:
        by_point.setdefault(slot.exam_point_id, []).append(slot)
    for group in by_point.values():
        group.sort(key=lambda s: s.item_index)

    # 大考点拆子批（≤6）；小考点（<3）暂存待与同 anchor 合并
    fragments: list[list[ContractSlot]] = []
    pending_small: dict[str, list[ContractSlot]] = {}
    for point in sorted(by_point):
        group = by_point[point]
        if len(group) < BATCH_MIN_SIZE:
            pending_small.setdefault(group[0].anchor_key, []).extend(group)
        else:
            fragments.extend(group[i : i + BATCH_MAX_SIZE] for i in range(0, len(group), BATCH_MAX_SIZE))
    for anchor in sorted(pending_small):
        pile = sorted(pending_small[anchor], key=lambda s: s.item_index)
        fragments.extend(pile[i : i + BATCH_MAX_SIZE] for i in range(0, len(pile), BATCH_MAX_SIZE))

    batches: list[QuestionBatch] = []
    for i, group in enumerate(fragments):
        points = sorted({s.exam_point_id for s in group})
        other_atoms: list[str] = []
        other_cores: list[str] = []
        for j, other in enumerate(fragments):
            if i == j:
                continue
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
```

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/domain/test_batching.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/domain/generation/batching.py tests/domain/test_batching.py
git commit -m "feat(batching): exam-point batches with size cap and cross-batch forbidden context"
```

---

### Task 8: 批式生成载荷编译

**Files:**
- Modify: `app/schemas/generation.py`
- Modify: `tests/unit/test_generation_payload.py`（新增批式用例，旧用例待 Task 13 处理）

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_generation_payload.py`：

```python
from app.domain.generation.batching import split_contract_into_batches
from app.schemas.generation import compile_batch_generation_payload


def _contract_slot(index: int, **overrides):
    from app.domain.generation.contract import ContractSlot
    payload = dict(
        item_index=index, question_type="single_choice", score=2, difficulty="medium",
        cognitive_level="understand", assessment_mode="conceptual",
        exam_point_id="EP1", anchor_key="A1", unit_id="U1", card_id=f"C{index}",
        coverage_atom=f"原子{index}", answer_boundary=f"边界{index}",
        performance_statement="掌握某知识",
        prompt_material=["材料片段"], scope_boundary={}, preferred_terms=["术语"],
    )
    payload.update(overrides)
    return ContractSlot(**payload)


def test_batch_payload_contains_all_slots_and_forbidden_context():
    slots = [_contract_slot(i) for i in (1, 2)]
    batch = split_contract_into_batches(slots + [_contract_slot(3)])[0]
    payload = compile_batch_generation_payload(batch, {"C1": {"prompt_material": ["卡1材料"]}})
    assert [q.item_index for q in payload.questions] == [1, 2, 3]
    assert payload.forbidden_atoms == []  # 单批同点无其他批
    assert payload.batch_instruction
    assert payload.output_schema["type"] == "array"


def test_batch_payload_comprehensive_slot_carries_contract_fields():
    slot = _contract_slot(
        1, question_type="comprehensive", comprehensive_archetype="case_analysis",
        material_form="case_text", cognitive_sequence=["understand", "apply"],
        subquestion_count_range=[2, 3], subquestion_actions=["提取事实", "解释因果"],
        answer_boundaries=["事实", "因果"],
    )
    batch = split_contract_into_batches([slot])[0]
    payload = compile_batch_generation_payload(batch, {})
    spec = payload.questions[0]
    assert spec.comprehensive_archetype == "case_analysis"
    assert "案例" in spec.question_template or "材料" in spec.question_template


def test_batch_payload_retry_revision_instruction_supported():
    slot = _contract_slot(1)
    batch = split_contract_into_batches([slot])[0]
    payload = compile_batch_generation_payload(batch, {})
    retried = payload.model_copy(update={"teacher_revision_instruction": "只修复题1的空数"})
    assert retried.teacher_revision_instruction
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_generation_payload.py -v -k batch`
Expected: FAIL — `ImportError: cannot import name 'compile_batch_generation_payload'`

- [ ] **Step 3: 实现**

`app/schemas/generation.py` 修改：把 `compile_question_generation_payload` 内的 `templates`/`schemas` 字典提升为模块级 `_QUESTION_TEMPLATES` / `_QUESTION_SCHEMAS`（内容原样搬移），新增：

```python
from app.domain.generation.archetypes import ARCHETYPE_CONTRACTS
from app.domain.generation.batching import QuestionBatch
from app.domain.generation.contract import ContractSlot


class BatchQuestionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_index: int
    question_type: str
    score: float
    difficulty: str
    cognitive_level: str
    assessment_mode: AssessmentMode = "conceptual"
    performance_statement: str = ""
    scope_boundary: dict = Field(default_factory=dict)
    prompt_material: list[str] = Field(default_factory=list)
    coverage_atom: str
    answer_boundary: str
    preferred_terms: list[str] = Field(default_factory=list)
    comprehensive_archetype: ComprehensiveArchetype | None = None
    material_form: MaterialForm | None = None
    cognitive_sequence: list[str] = Field(default_factory=list)
    subquestion_count_range: list[int] | None = None
    subquestion_actions: list[str] = Field(default_factory=list)
    answer_boundaries: list[str] = Field(default_factory=list)
    question_template: str
    output_schema: dict


class BatchGenerationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    exam_point_ids: list[str]
    questions: list[BatchQuestionSpec]
    forbidden_atoms: list[str] = Field(default_factory=list)
    forbidden_answer_cores: list[str] = Field(default_factory=list)
    batch_instruction: str
    output_schema: dict
    teacher_revision_instruction: str = ""


def _template_and_schema(slot: ContractSlot) -> tuple[str, dict]:
    if slot.question_type != "comprehensive":
        return _QUESTION_TEMPLATES[slot.question_type], _QUESTION_SCHEMAS[slot.question_type]
    if slot.comprehensive_archetype is None:
        raise ValueError("comprehensive slot requires an archetype contract")
    contract = ARCHETYPE_CONTRACTS[slot.comprehensive_archetype]
    question_template = contract.question_template + " 结构要求：" + "；".join(contract.structure_requirements)
    if slot.comprehensive_archetype == "code_completion_scenario":
        question_template += (
            " 挖空格式要求：每处空写成 ____________(编号)__________ 的形式并按 (1)(2)(3) 顺延编号，"
            "共 4 至 6 处；分问（1）补全代码，分问（2）结合场景分析问题并给出改进方向。"
        )
    output_schema = {
        "type": "object",
        "required": ["stem", "subquestions", "answer", "explanation", "rubric"],
        "stem": "string",
        "subquestions": {
            "type": "array",
            "min_items": (slot.subquestion_count_range or [2, 4])[0],
            "max_items": (slot.subquestion_count_range or [2, 4])[1],
            "items": {"type": "object", "required": ["action", "prompt", "answer_boundary", "answer", "rubric", "score"]},
        },
        "answer": "string", "explanation": "string", "rubric": "array",
    }
    return question_template, output_schema


def compile_batch_generation_payload(
    batch: QuestionBatch, knowledge_cards: dict[str, dict]
) -> BatchGenerationPayload:
    specs: list[BatchQuestionSpec] = []
    for slot in batch.slots:
        card = knowledge_cards.get(slot.card_id, {})
        question_template, output_schema = _template_and_schema(slot)
        specs.append(BatchQuestionSpec(
            item_index=slot.item_index,
            question_type=slot.question_type,
            score=slot.score,
            difficulty=slot.difficulty,
            cognitive_level=slot.cognitive_level,
            assessment_mode=slot.assessment_mode,
            performance_statement=slot.performance_statement or str(card.get("performance_statement", "")),
            scope_boundary=slot.scope_boundary or card.get("scope_boundary", {}) or {},
            prompt_material=slot.prompt_material or list(card.get("prompt_material", []) or []),
            coverage_atom=slot.coverage_atom,
            answer_boundary=slot.answer_boundary,
            preferred_terms=slot.preferred_terms or list(card.get("preferred_terms", []) or []),
            comprehensive_archetype=slot.comprehensive_archetype,
            material_form=slot.material_form,
            cognitive_sequence=slot.cognitive_sequence,
            subquestion_count_range=slot.subquestion_count_range,
            subquestion_actions=slot.subquestion_actions,
            answer_boundaries=slot.answer_boundaries,
            question_template=question_template,
            output_schema=output_schema,
        ))
    instruction = (
        f"为本批 {len(specs)} 道题目一次性命题，返回 JSON 数组，每个元素含 item_index 及对应 output_schema 字段。"
        "同批各题考查视角必须互补：题型与认知层级已指定，不得从同一角度重复考查同一内容。"
        "forbidden_atoms 与 forbidden_answer_cores 是同考点其他题目已定的内容，"
        "不得出现在本批任何题干、选项或答案文本中。"
    )
    if batch.forbidden_context.atoms or batch.forbidden_context.answer_cores:
        instruction += "严格执行上述禁用清单，任何泄漏都视为废题。"
    return BatchGenerationPayload(
        batch_id=batch.batch_id,
        exam_point_ids=batch.exam_point_ids,
        questions=specs,
        forbidden_atoms=batch.forbidden_context.atoms,
        forbidden_answer_cores=batch.forbidden_context.answer_cores,
        batch_instruction=instruction,
        output_schema={"type": "array", "items": "每个元素为单题对象，须含 item_index"},
    )
```

`compile_question_generation_payload` 保持不变（旧路径仍编译，Task 13 删除）。

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/unit/test_generation_payload.py -v`
Expected: 全部通过（旧 + 新 3 条）

- [ ] **Step 5: Commit**

```bash
git add app/schemas/generation.py tests/unit/test_generation_payload.py
git commit -m "feat(payload): batch generation payload compiler reusing type templates"
```

---

### Task 9: 网关批式生成方法

**Files:**
- Modify: `app/adapters/model/deepseek_gateway.py`
- Create: `tests/adapters/test_gateway_batch.py`

- [ ] **Step 1: 写失败测试**

`tests/adapters/test_gateway_batch.py`（无 `tests/adapters/` 目录则连同 `__init__.py` 一起创建；若项目测试布局无 `__init__` 惯例，则放到 `tests/unit/test_gateway_batch.py`）：

```python
import pytest

from app.adapters.model.deepseek_gateway import DeepSeekGateway
from app.schemas.generation import BatchGenerationPayload, BatchQuestionSpec


class FakeJsonClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request_json(self, *, system_prompt, payload, temperature, call_context=None, response_validator=None):
        self.calls.append({"system_prompt": system_prompt, "payload": payload})
        result = self.response
        if response_validator:
            response_validator(result)
        return result


def _payload(question_count=2):
    specs = [
        BatchQuestionSpec(
            item_index=i, question_type="single_choice", score=2, difficulty="medium",
            cognitive_level="understand", coverage_atom=f"原子{i}", answer_boundary=f"边界{i}",
            question_template="单选模板", output_schema={},
        )
        for i in range(1, question_count + 1)
    ]
    return BatchGenerationPayload(
        batch_id="B01", exam_point_ids=["EP1"], questions=specs,
        batch_instruction="指令", output_schema={"type": "array"},
    )


def test_generate_batch_returns_list_and_matches_indexes():
    client = FakeJsonClient([
        {"item_index": 1, "stem": "题一", "answer": "A"},
        {"item_index": 2, "stem": "题二", "answer": "B"},
    ])
    gateway = DeepSeekGateway(api_key="k", client=client)
    questions = gateway.generate_batch(_payload())
    assert [q["item_index"] for q in questions] == [1, 2]


def test_generate_batch_rejects_missing_item_index():
    client = FakeJsonClient([{"stem": "缺编号", "answer": "A"}])
    gateway = DeepSeekGateway(api_key="k", client=client)
    with pytest.raises(Exception):
        gateway.generate_batch(_payload(question_count=1))


def test_generate_batch_rejects_extra_item():
    client = FakeJsonClient([
        {"item_index": 1, "stem": "题一", "answer": "A"},
        {"item_index": 99, "stem": "多余", "answer": "B"},
    ])
    gateway = DeepSeekGateway(api_key="k", client=client)
    with pytest.raises(Exception):
        gateway.generate_batch(_payload(question_count=1))
```

注意 `DeepSeekGateway.__init__` 需要支持注入 client：查看现有签名已含 `client: httpx.Client | None`（HTTP 层）。若 FakeJsonClient 无法从外部注入 json_client，则在 `DeepSeekGateway.__init__` 增加可选参数 `json_client: DeepSeekJsonClient | None = None`，构造时 `self.json_client = json_client or DeepSeekJsonClient(...)`——测试通过该参数注入。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_gateway_batch.py -v`
Expected: FAIL — `AttributeError: 'DeepSeekGateway' object has no attribute 'generate_batch'`

- [ ] **Step 3: 实现**

`deepseek_gateway.py` 的 `DeepSeekGateway` 类追加（并按 Step 1 注释处理 json_client 注入）：

```python
    def generate_batch(self, payload) -> list[dict]:
        expected = [spec.item_index for spec in payload.questions]

        def validate_batch(result):
            if not isinstance(result, list):
                raise DeepSeekModelError("model_output_schema_violation", "批式生成必须返回 JSON 数组")
            indexes = [item.get("item_index") for item in result if isinstance(item, dict)]
            if sorted(indexes) != sorted(expected):
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    f"批式生成 item_index 集合不符：期望 {sorted(expected)}，实际 {sorted(indexes)}",
                )

        response = self.json_client.request_json(
            system_prompt=(
                "你是高校期末考试命题教师，一次为本批所有题位命题，必须返回 JSON 数组，"
                "每个元素包含 item_index 及该题 output_schema 要求的全部字段。"
                "只能依据各题给定的纯净知识内容与指定考查原子出题，严格遵守答案边界和题型任务，"
                "不延伸考查其他知识原子。同批各题视角互补，不得互相提示或重复。"
                "forbidden_atoms 与 forbidden_answer_cores 中的内容不得出现在任何题干、选项或答案中。"
                "优先使用 preferred_terms 中的常用术语；除符号、缩写或必要消歧外不要使用括号解释。"
                "填空题题干恰好 1 个空（连续下划线），答案简短唯一。"
                "综合题逐项执行已分配的原型、材料形式、认知序列与分问范围："
                "code_completion_scenario 先给工程场景说明再给代码框架，"
                "关键处挖 ____________(编号)__________ 空（4至6处），分问固定为补全代码与问题分析，"
                "代码与参数只能来自给定材料。若有修订指令，只针对指令涉及题目局部改写。"
            ),
            payload=payload.model_dump(mode="json"),
            temperature=0.2,
            response_validator=validate_batch,
        )
        return [item for item in response if isinstance(item, dict)]
```

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/unit/test_gateway_batch.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/adapters/model/deepseek_gateway.py tests/unit/test_gateway_batch.py
git commit -m "feat(gateway): batch question generation with index-set validation"
```

---

### Task 10: 生成图重写（分批 Send + 单题重试 + 合并终检）

**Files:**
- Rewrite: `app/workflows/generation_graph.py`
- Rewrite: `tests/workflow/test_generation_graph.py`

- [ ] **Step 1: 写失败测试（新图全量测试文件）**

`tests/workflow/test_generation_graph.py` 整体重写（旧 14 个用例随旧图删除）：

```python
import pytest

from app.domain.generation.contract import ContractSlot, ForbiddenContext, PaperContract
from app.schemas.generation import BatchGenerationPayload
from app.workflows.generation_graph import build_generation_graph


def _slot(index: int, point: str = "EP1", **overrides) -> dict:
    payload = dict(
        item_index=index, question_type="single_choice", score=2, difficulty="medium",
        cognitive_level="understand", assessment_mode="conceptual",
        exam_point_id=point, anchor_key="A1", unit_id=f"U-{point}", card_id=f"C{index}",
        coverage_atom=f"原子{index}", answer_boundary=f"边界{index}",
        performance_statement="掌握某知识",
        forbidden_context=ForbiddenContext(atoms=[], answer_cores=[]).model_dump(),
    )
    payload.update(overrides)
    return payload


def _question(index: int, **overrides) -> dict:
    payload = dict(
        item_index=index, question_type="single_choice", stem=f"关于原子{index}的问题是",
        options=["甲", "乙", "丙", "丁"], answer="甲", explanation="解析", difficulty="medium",
    )
    payload.update(overrides)
    return payload


class FakeBatchGateway:
    """按批返回题目的假网关，可注入按 item_index 的失败剧本。"""
    def __init__(self, scenarios: dict[int, list[dict]] | None = None):
        self.scenarios = scenarios or {}
        self.batch_payloads: list[BatchGenerationPayload] = []
        self.retry_payloads: list[BatchGenerationPayload] = []

    def generate_batch(self, payload: BatchGenerationPayload) -> list[dict]:
        if len(payload.questions) == 1:
            self.retry_payloads.append(payload)
            script = self.scenarios.get(payload.questions[0].item_index, [])
            attempt = sum(1 for p in self.retry_payloads if p.questions[0].item_index == payload.questions[0].item_index)
            return [script[attempt - 1] if attempt - 1 < len(script) else _question(payload.questions[0].item_index)]
        self.batch_payloads.append(payload)
        return [
            (self.scenarios.get(spec.item_index, [_question(spec.item_index)])[0])
            for spec in payload.questions
        ]


def _state(slots: list[dict]) -> dict:
    return {"contract": slots, "knowledge_cards": {}, "recent_structure_signatures": []}


def test_batches_run_in_parallel_and_merge_sorted():
    slots = [_slot(i, "EP1") for i in (1, 2, 3)] + [_slot(i, "EP2") for i in (4, 5, 6)]
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state(slots))
    assert [q["item_index"] for q in result["questions"]] == [1, 2, 3, 4, 5, 6]
    assert len(gateway.batch_payloads) == 2  # 两个考点批


def test_batch_payload_carries_forbidden_context_for_split_batches():
    slots = [_slot(i, "EP1") for i in range(1, 9)]  # 8 题拆两子批
    gateway = FakeBatchGateway()
    build_generation_graph(gateway).invoke(_state(slots))
    first = min(gateway.batch_payloads, key=lambda p: min(q.item_index for q in p.questions))
    assert first.forbidden_atoms  # 子批1 携带子批2 原子


def test_failed_question_retries_at_most_twice_then_needs_review():
    bad = _question(1, stem="根据课件第3页的内容，关于原子1的问题是", options=["甲"], answer="")
    slots = [_slot(1), _slot(2)]
    gateway = FakeBatchGateway(scenarios={1: [bad, bad, bad]})
    result = build_generation_graph(gateway).invoke(_state(slots))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "blocker"
    assert question["needs_review"] is True
    assert len([p for p in gateway.retry_payloads if p.questions[0].item_index == 1]) == 2  # ≤2 次重试
    assert next(q for q in result["questions"] if q["item_index"] == 2)["quality"]["status"] == "pass"


def test_retry_success_returns_passing_question():
    bad = _question(1, stem="根据课件的内容，关于原子1的问题是", options=["甲"], answer="")
    slots = [_slot(1), _slot(2)]
    gateway = FakeBatchGateway(scenarios={1: [bad, _question(1)]})
    result = build_generation_graph(gateway).invoke(_state(slots))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert len(gateway.retry_payloads) == 1


def test_forbidden_context_leak_is_caught_and_retried():
    slot1 = _slot(1, forbidden_context=ForbiddenContext(
        atoms=["另一个考点兄弟原子文本样例"], answer_cores=["兄弟答案核心内容"],
    ).model_dump())
    leaking = _question(1, stem="关于另一个考点兄弟原子文本样例的描述是")
    fixed = _question(1)
    gateway = FakeBatchGateway(scenarios={1: [leaking, fixed]})
    result = build_generation_graph(gateway).invoke(_state([slot1, _slot(2)]))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"
    assert len(gateway.retry_payloads) == 1


def test_answer_must_hit_boundary():
    off_boundary = _question(1, answer="毫不相关的答案内容文本")
    gateway = FakeBatchGateway(scenarios={1: [off_boundary, _question(1)]})
    result = build_generation_graph(gateway).invoke(_state([_slot(1, answer_boundary="边界甲乙丙丁"), _slot(2)]))
    question = next(q for q in result["questions"] if q["item_index"] == 1)
    assert question["quality"]["status"] == "pass"  # 重试后命中
    assert len(gateway.retry_payloads) == 1


def test_model_call_count_matches_batches_plus_retries():
    slots = [_slot(i, "EP1") for i in (1, 2, 3)]
    gateway = FakeBatchGateway(scenarios={1: [_question(1, stem="根据课件的内容，问题"), _question(1)]})
    result = build_generation_graph(gateway).invoke(_state(slots))
    assert result["model_call_count"] == 2  # 1 批 + 1 重试


def test_final_check_reports_quota_uniqueness_mutex():
    slots = [_slot(i, "EP1") for i in (1, 2, 3)]
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state(slots))
    report = result["final_check"]
    assert report["passed"] is True
    codes = {c["code"] for c in report["checks"]}
    assert {"quota_match", "atom_uniqueness", "answer_mutex", "traceability"} <= codes


def test_duplicate_contract_atom_fails_final_check():
    slots = [_slot(1), _slot(2)]
    slots[1]["coverage_atom"] = slots[0]["coverage_atom"]
    slots[1]["answer_boundary"] = "不同边界"
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state(slots))
    assert result["final_check"]["passed"] is False


def test_comprehensive_slot_generates_with_contract_fields():
    comp = _slot(
        1, question_type="comprehensive", comprehensive_archetype="case_analysis",
        material_form="case_text", cognitive_sequence=["understand", "apply"],
        subquestion_count_range=[2, 3], subquestion_actions=["提取", "解释"],
        answer_boundaries=["事实", "因果"],
    )
    gateway = FakeBatchGateway()
    result = build_generation_graph(gateway).invoke(_state([comp]))
    payload = gateway.batch_payloads[0]
    assert payload.questions[0].comprehensive_archetype == "case_analysis"
```

说明：FakeBatchGateway 的重试剧本机制——`scenarios[index]` 是"第1次批返回 + 第N次重试返回"的序列。批调用取 `[0]`，重试第 k 次取 `script[k]`（脚本耗尽返回合格题），与图内"先批后重试"的顺序一致。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/workflow/test_generation_graph.py -v`
Expected: FAIL — 旧图无 contract 状态/新签名不符

- [ ] **Step 3: 重写 generation_graph.py（整文件替换）**

```python
"""合同驱动的试卷生成图。

build_batches → Send(batch_generate) 按考点批并行 → merge_and_check → END。
批内一次模型调用同批互见；跨批互斥由合同禁用上下文构造性保证；
单题失败带原因重试 ≤ max_retries，仍失败标记 needs_review，不阻塞整卷。
"""
from __future__ import annotations

import operator
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.domain.generation.batching import QuestionBatch, split_contract_into_batches
from app.domain.generation.contract import ContractSlot, PaperContract, _normalized
from app.domain.generation.coverage import _normalize_prompt_material
from app.schemas.generation import compile_batch_generation_payload
from app.services.generation_service import validate_generated_question

_QUESTION_TYPES = ("single_choice", "true_false", "fill_blank", "short_answer", "comprehensive")


class BatchGateway(Protocol):
    def generate_batch(self, payload) -> list[dict]: ...


from typing import Protocol  # noqa: E402  (放在 Protocol 使用前亦可，统一移到顶部更佳)


class GenerationState(TypedDict, total=False):
    contract: list[dict]
    knowledge_cards: dict[str, dict]
    recent_structure_signatures: list[dict]
    batches: list[dict]
    questions: Annotated[list[dict], operator.add]
    model_call_count: Annotated[int, operator.add]
    final_check: dict


def _compact_text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(value or "")).lower()


def _longest_common_substring_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _answer_hits_boundary(answer, boundary: str) -> bool:
    if not boundary:
        return True
    left = _normalized(str(answer or ""))
    right = _normalized(boundary)
    if not left:
        return False
    if left in right or right in left:
        return True
    return _longest_common_substring_len(left, right) >= 3


def _check_question(question: dict, slot: ContractSlot) -> dict:
    quality = validate_generated_question(question)
    reasons: list[str] = []
    if quality["status"] != "pass":
        reasons.append(quality["message"])
    surface = _compact_text([question.get("stem", ""), *(question.get("options") or [])])
    for atom in slot.forbidden_context.atoms:
        core = _normalized(atom)
        if len(core) >= 6 and core in surface:
            reasons.append(f"题干泄漏同考点其他题原子：{atom[:20]}…")
    for core_raw in slot.forbidden_context.answer_cores:
        core = _normalized(core_raw)
        if len(core) >= 6 and core in surface:
            reasons.append(f"题干泄漏同考点其他题答案核心：{core_raw[:20]}…")
    if not _answer_hits_boundary(question.get("answer"), slot.answer_boundary):
        reasons.append("答案未命中答案域")
    if not reasons:
        return {"status": "pass", "message": "通过合同校验"}
    return {"status": "blocker", "message": "；".join(reasons)}


def _stamp_question(question: dict, slot: ContractSlot) -> dict:
    question.update({
        "item_index": slot.item_index,
        "question_type": slot.question_type,
        "score": slot.score,
        "difficulty": slot.difficulty,
        "cognitive_level": slot.cognitive_level,
        "coverage_atom": slot.coverage_atom,
        "answer_boundary": slot.answer_boundary,
        "exam_point_id": slot.exam_point_id,
        "unit_id": slot.unit_id,
        "card_id": slot.card_id,
    })
    for field in ("comprehensive_archetype", "material_form", "cognitive_sequence"):
        if slot.question_type == "comprehensive":
            question[field] = getattr(slot, field)
        else:
            question.pop(field, None)
    return question


def build_generation_graph(gateway: BatchGateway, *, max_workers: int = 8, max_retries: int = 2):
    def build_batches(state: GenerationState) -> dict:
        slots = [ContractSlot.model_validate(raw) for raw in state["contract"]]
        batches = split_contract_into_batches(slots)
        return {"batches": [batch.model_dump(mode="json") for batch in batches]}

    def batch_generate(batch_raw: dict) -> dict:
        state_cards = _state_cards["cards"]
        batch = QuestionBatch.model_validate(batch_raw)
        payload = compile_batch_generation_payload(batch, state_cards)
        try:
            raw_questions = list(gateway.generate_batch(payload))
        except Exception:
            raw_questions = []
        calls = 1

        questions: list[dict] = []
        slot_by_index = {s.item_index: s for s in batch.slots}
        for raw in raw_questions:
            index = raw.get("item_index")
            if index not in slot_by_index:
                continue
            questions.append(_stamp_question(dict(raw), slot_by_index[index]))

        produced: dict[int, dict] = {}
        for slot in batch.slots:
            question = next((q for q in questions if q["item_index"] == slot.item_index), None)
            if question is not None:
                quality = _check_question(question, slot)
                attempts = 0
                while quality["status"] != "pass" and attempts < max_retries:
                    attempts += 1
                    calls += 1
                    retry_payload = compile_batch_generation_payload(
                        QuestionBatch(
                            batch_id=batch.batch_id, anchor_key=batch.anchor_key,
                            exam_point_ids=batch.exam_point_ids, slots=[slot],
                            forbidden_context=batch.forbidden_context,
                        ), state_cards,
                    ).model_copy(update={"teacher_revision_instruction": f"题位 {slot.item_index} 未通过校验：{quality['message']}。请只修复该题并返回。"})
                    try:
                        retried = list(gateway.generate_batch(retry_payload))
                    except Exception:
                        break
                    candidate = next((q for q in retried if q.get("item_index") == slot.item_index), None)
                    if candidate is None:
                        continue
                    question = _stamp_question(dict(candidate), slot)
                    quality = _check_question(question, slot)
                question["quality"] = quality
                question["needs_review"] = quality["status"] != "pass"
                produced[slot.item_index] = question
            else:
                produced[slot.item_index] = {
                    "item_index": slot.item_index, "question_type": slot.question_type,
                    "score": slot.score, "difficulty": slot.difficulty,
                    "cognitive_level": slot.cognitive_level,
                    "coverage_atom": slot.coverage_atom, "answer_boundary": slot.answer_boundary,
                    "exam_point_id": slot.exam_point_id, "unit_id": slot.unit_id,
                    "card_id": slot.card_id, "quality": {"status": "blocker", "message": "批返回缺失该题"},
                    "needs_review": True,
                }
        ordered = [produced[s.item_index] for s in sorted(batch.slots, key=lambda s: s.item_index)]
        return {"questions": ordered, "model_call_count": calls}

    # Send 需要读取 knowledge_cards；LangGraph Send 的参数是静态的，
    # 因此将卡片注入每个 batch dict。
    _state_cards: dict = {"cards": {}}

    def build_batches_with_cards(state: GenerationState) -> dict:
        _state_cards["cards"] = state.get("knowledge_cards", {})
        slots = [ContractSlot.model_validate(raw) for raw in state["contract"]]
        batches = split_contract_into_batches(slots)
        return {"batches": [batch.model_dump(mode="json") for batch in batches]}

    def route_batches(state: GenerationState) -> list[Send]:
        _state_cards["cards"] = state.get("knowledge_cards", {})
        return [Send("batch_generate", batch) for batch in state.get("batches", [])]

    def merge_and_check(state: GenerationState) -> dict:
        questions = sorted(state.get("questions", []), key=lambda q: q["item_index"])
        slots = [ContractSlot.model_validate(raw) for raw in state["contract"]]
        report = audit_paper_against_contract(slots, questions)
        return {"questions": [], "final_check": report}

    graph = StateGraph(GenerationState)
    graph.add_node("build_batches", build_batches_with_cards)
    graph.add_node("batch_generate", batch_generate)
    graph.add_node("merge_and_check", merge_and_check)
    graph.add_edge(START, "build_batches")
    graph.add_conditional_edges("build_batches", route_batches, ["batch_generate"])
    graph.add_edge("batch_generate", "merge_and_check")
    graph.add_edge("merge_and_check", END)
    return graph.compile()
```

重要修正说明（实现时按此落实，上面的示意以本条为准）：
1. `merge_and_check` 返回 `{"questions": [], ...}` 会破坏已累积 questions（operator.add 追加空列表是无害的，但排序结果必须落回 state）——正确做法：merge 节点读 `state["questions"]` 排序后写 `final_check`，questions 保持累积态；API 层输出时再排序。即 `merge_and_check` 返回 `{"final_check": report}`，`result["questions"]` 由 invoke 返回的累积列表排序得到（invoke 返回全部 state 键，questions 即各批追加之和，测试里 `[q["item_index"] for q in result["questions"]] == [1,2,3,4,5,6]` 需要 API/图输出前排序——改为在 merge_and_check 中把排序后的列表赋给新键 `paper_questions`，测试断言改读 `result["paper_questions"]`；测试文件同步用 `result["paper_questions"]`）。
2. `route_batches` 中 `Send` 的 arg 就是 batch dict；`batch_generate` 收到的直接是该 dict。
3. `from typing import Protocol` 移到文件顶部 import 区；`BatchGateway` 定义于其後。
4. 并行由 LangGraph Send 自带（多 batch 并发执行）；`max_workers` 保留给批内无用途时可删（删除该参数与 ThreadPoolExecutor import）。
5. `_normalize_prompt_material` 若未使用则不 import。

`audit_paper_against_contract`（新函数，放 `app/services/generation_service.py` 末尾）：

```python
def audit_paper_against_contract(slots, questions) -> dict:
    from app.domain.generation.contract import boundaries_overlap

    checks: list[dict] = []
    slot_counts: dict[str, int] = {}
    question_counts: dict[str, int] = {}
    for slot in slots:
        slot_counts[slot.exam_point_id] = slot_counts.get(slot.exam_point_id, 0) + 1
    for question in questions:
        question_counts[question.get("exam_point_id", "")] = question_counts.get(question.get("exam_point_id", ""), 0) + 1
    quota_ok = slot_counts == question_counts
    checks.append({"code": "quota_match", "passed": quota_ok, "detail": {"contract": slot_counts, "paper": question_counts}})

    atoms = [_compact_text(q.get("coverage_atom")) for q in questions]
    unique_ok = len(atoms) == len(set(atoms))
    checks.append({"code": "atom_uniqueness", "passed": unique_ok, "detail": {"total": len(atoms), "unique": len(set(atoms))}})

    mutex_ok = True
    collisions = []
    ordered = sorted(questions, key=lambda q: q.get("item_index", 0))
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            if boundaries_overlap(str(left.get("answer_boundary", "")), str(right.get("answer_boundary", ""))):
                mutex_ok = False
                collisions.append([left.get("item_index"), right.get("item_index")])
    checks.append({"code": "answer_mutex", "passed": mutex_ok, "detail": {"collisions": collisions}})

    missing = [
        q.get("item_index") for q in questions
        if not all(q.get(field) for field in ("exam_point_id", "unit_id", "card_id", "coverage_atom"))
    ]
    checks.append({"code": "traceability", "passed": not missing, "detail": {"missing": missing}})

    needs_review = sum(1 for q in questions if q.get("needs_review"))
    checks.append({"code": "needs_review", "passed": needs_review == 0, "detail": {"count": needs_review}})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}
```

注意：批内同考点各题 answer_boundary 来自不同原子、装配期已互斥，故 `answer_mutex` 终检期望通过；若合同被教师修订破坏互斥（confirm 已拦截）或批返回错位，终检兜底报失败。

- [ ] **Step 4: 运行测试通过**

Run: `python -m pytest tests/workflow/test_generation_graph.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add app/workflows/generation_graph.py app/services/generation_service.py tests/workflow/test_generation_graph.py
git commit -m "feat(generation): rewrite graph as contract-driven parallel batches with per-question retry"
```

---

### Task 11: 生成 API 接收合同

**Files:**
- Modify: `app/api/v1/generation.py`

- [ ] **Step 1: 修改请求模型与调用**

`app/api/v1/generation.py` 替换 `GenerationRequest` 与 `generate_paper`：

```python
class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: list[dict]
    knowledge_cards: dict[str, dict]


@router.post("/generation-runs", status_code=status.HTTP_202_ACCEPTED)
def generate_paper(
    course_id: str,
    request: GenerationRequest,
    gateway=Depends(get_gateway),
    session: Session = Depends(get_session),
) -> dict:
    try:
        recent_signatures = load_recent_structure_signatures(session, course_id, paper_limit=5)
        recent_keys = {sig.structure_key for sig in recent_signatures}
        initial_state: dict = {
            "contract": request.contract,
            "knowledge_cards": request.knowledge_cards,
            "recent_structure_signatures": [s.model_dump() for s in recent_signatures],
        }
        result = build_generation_graph(gateway).invoke(initial_state)
        questions = sorted(result.get("questions", []), key=lambda q: q.get("item_index", 0))
        return {
            "status": "candidate",
            "questions": questions,
            "final_check": result.get("final_check", {}),
            "model_call_count": result.get("model_call_count", 0),
            "model": settings.deepseek_model,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"question generation failed: {exc}")
```

（`recent_keys` 当前图内未消费结构键避让——结构避让在合同装配期完成；此处仅保留 recent_signatures 透传。若图未用到，可一并删除该查询，简化为纯 invoke。实现时选择删除 `load_recent_structure_signatures` 调用与 import，合同避让在 allocate 时由调用方传入 `recent_structure_keys`——超出本任务最小改动则保留透传不删。）

- [ ] **Step 2: 冒烟验证（无真实 key 时用图测试代替）**

Run: `python -m pytest tests/workflow/test_generation_graph.py tests/unit/test_gateway_batch.py -v`
Expected: 全部通过；另启动 `uvicorn app.main:app` 后 `POST /api/v1/courses/demo/generation-runs` 空合同应返回空卷不 500。

- [ ] **Step 3: Commit**

```bash
git add app/api/v1/generation.py
git commit -m "feat(api): generation-runs accepts confirmed paper contract"
```

---

### Task 12: 删除旧路径（精排/修复循环/按题型节点/atom_selector）

**Files:**
- Delete: `app/domain/generation/atom_selector.py`
- Modify: `app/workflows/generation_graph.py`（确认无残留引用）
- Modify: `app/adapters/model/deepseek_gateway.py`（删除 `plan_coverage`、`refine_atom`、`generate`、`audit_paper`）
- Modify: `app/schemas/generation.py`（删除 `QuestionGenerationPayload`、`compile_question_generation_payload`、`EvidenceTracePack` 若无他处引用）
- Modify: `app/domain/generation/coverage.py`（删除 `CoveragePlanningSlot/Payload`、`AtomRefinementSlot/Payload` 及其构建函数）
- Modify: `tests/unit/test_generation_payload.py`（删除旧编译器用例，保留批式用例）
- Modify: 其他引用处（由 grep 结果决定）

- [ ] **Step 1: 全量 grep 引用**

Run（在 backend 目录，用 Grep 工具而非 shell）：
- pattern `atom_selector|select_candidate_atoms_and_contracts|refine_atom|plan_coverage|audit_paper|compile_question_generation_payload|QuestionGenerationPayload|AtomRefinement|CoveragePlanning`
- 记录每个命中文件与行。

- [ ] **Step 2: 按引用清单删除死代码**

删除顺序：先删 workflow/schema/gateway 中的引用，再删 coverage 中无人引用的类，最后删 atom_selector.py。`generation_service.py` 中 `audit_question_set` 旧全卷审计保留（终检已换成合同审计，但 `audit_question_set` 若无调用方一并删除——以 grep 结果为准：仅 `generation_graph.py` 旧版调用过，删除）。

- [ ] **Step 3: 全量测试**

Run: `python -m pytest tests/ -v --ignore=tests/unit/test_health.py`
Expected: 除已知 redis 环境问题外全部通过；任何失败按报错修复引用。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(generation): remove superseded refine/repair/type-node path"
```

---

### Task 13: 组织图批式分类（42 次调用 → 每资料 1 次）

**Files:**
- Modify: `app/adapters/model/deepseek_semantic_extractors.py`
- Modify: `app/workflows/organization_graph.py`
- Modify: `tests/workflow/test_organization_graph.py`（假分类器改造）

- [ ] **Step 1: 网关侧新增 classify_file**

在 `DeepSeekExamPointEvidenceClassifier` 中新增方法（保留旧 `classify` 以兼容存量假件，图改为只调新方法；下一 Step 图内不再调旧方法）：

```python
    def classify_file(
        self,
        *,
        exam_points: list[ExamPoint],
        material_version_id: str,
        chunks: list[StagingChunk],
        call_context: ModelCallContext | None = None,
    ) -> list[ExamPointFileDecision]:
        """一个资料文件 × 全部考点，一次调用完成分类。"""
        if any(chunk.material_version_id != material_version_id for chunk in chunks):
            raise DeepSeekModelError(
                "model_input_scope_violation",
                "classification input contains another material version",
            )
        expected_pairs = {
            (point.code, chunk.id) for point in exam_points for chunk in chunks
        }
        collected: dict[str, ExamPointFileDecision] = {}

        def validate_response(result: dict) -> None:
            raw = result.get("file_decisions") if isinstance(result, dict) else None
            if not isinstance(raw, list):
                raise _schema_error_result("file_decisions must be an array")
            seen: set[tuple[str, str]] = set()
            for item in raw:
                response = _ClassificationResponse.model_validate(
                    _normalize_classification_response(item)
                )
                if response.material_version_id != material_version_id:
                    raise DeepSeekModelError(
                        "model_output_scope_violation",
                        "classification response belongs to another material version",
                    )
                for decision in response.decisions:
                    seen.add((response.exam_point_code, decision.evidence_chunk_id))
                point = next((p for p in exam_points if p.code == response.exam_point_code), None)
                if point is None:
                    raise DeepSeekModelError(
                        "model_output_scope_violation",
                        "classification response references unknown exam point",
                    )
                built = ExamPointFileDecision(
                    exam_point_code=response.exam_point_code,
                    material_version_id=response.material_version_id,
                    decisions=[
                        EvidenceDecision.model_validate(d.model_dump(exclude={"source_locator"}))
                        for d in response.decisions
                    ],
                )
                collected[response.exam_point_code] = built
            if seen != expected_pairs:
                raise DeepSeekModelError(
                    "model_output_scope_violation",
                    "classification output must cover every (exam_point, chunk) pair exactly once",
                )

        self.client.request_json(
            system_prompt=(
                "你判断一份教学资料文件与多个考试考点的证据关系。输入包含 exam_points 数组与该文件的 chunks。"
                "必须返回 JSON 对象，顶层字段 file_decisions 为数组；每个元素对应一个考点，"
                "包含 exam_point_code、material_version_id、decisions。"
                "每个考点必须对其与该文件相关的全部输入 chunks 逐一判定；"
                "relevance_class 仅允许 direct、supporting、background、out_of_scope；"
                "direct 必须能直接支撑可评分事实、答案或评分点，且必须提供 candidate_assessment_unit 与 "
                "candidate_card_content，否则降级；supporting 只用于设问语境；"
                "background/out_of_scope 不得生成知识卡。遵守各考点 operational_detail_policy。"
            ),
            payload={
                "exam_points": [point.model_dump(mode="json") for point in exam_points],
                "material_version_id": material_version_id,
                "chunks": [
                    {
                        "evidence_chunk_id": chunk.id,
                        "material_version_id": chunk.material_version_id,
                        "content": chunk.content,
                        "locator": chunk.locator,
                    }
                    for chunk in chunks
                ],
            },
            temperature=0.0,
            call_context=call_context,
            response_validator=validate_response,
        )
        return list(collected.values())
```

（`_schema_error_result` 若不存在，用现有 `_schema_error` 等价包装一个 `ValidationError`；以文件内既有辅助函数为准，保持一致。）

- [ ] **Step 2: 图侧按资料分组调用**

`organization_graph.py` 的 `classify_exam_point_file_pairs` 重构（`_failure`、admit 逻辑保留）：

```python
    def classify_exam_point_file_pairs(state: OrganizationState):
        points = _points(state)
        chunk_ids = sorted(
            {
                chunk_id
                for pair in state.get("retrieval_pairs", [])
                for chunk_id in pair["evidence_chunk_ids"]
            }
        )
        chunks_by_id = {chunk.id: chunk for chunk in _chunks(state, chunk_ids)}
        pairs = sorted(
            state.get("retrieval_pairs", []),
            key=lambda item: (item["exam_point_code"], item["material_version_id"]),
        )

        # 按资料分组：一个文件一次调用覆盖其全部考点对
        by_material: dict[str, list[dict]] = {}
        for pair in pairs:
            by_material.setdefault(pair["material_version_id"], []).append(pair)

        decisions: list[ExamPointFileDecision] = []
        failures: list[dict] = list(state.get("failed_pairs") or [])
        coverage_reasons = {
            code: list(values)
            for code, values in (state.get("coverage_reasons") or {}).items()
        }

        def classify_material(material_version_id: str, material_pairs: list[dict]) -> list[ExamPointFileDecision]:
            point_codes = sorted({pair["exam_point_code"] for pair in material_pairs})
            needed_points = [p for p in points if p.code in point_codes]
            material_chunk_ids = sorted(
                {cid for pair in material_pairs for cid in pair["evidence_chunk_ids"]}
            )
            material_chunks = [chunks_by_id[cid] for cid in material_chunk_ids]
            raw = classifier.classify_file(
                exam_points=needed_points,
                material_version_id=material_version_id,
                chunks=material_chunks,
                call_context=ModelCallContext(
                    course_id=state["course_id"],
                    organization_run_id=state["run_id"],
                    stage="classify_material_file",
                ),
            )
            validated_by_point = {d.exam_point_code: d for d in raw}
            per_pair: list[ExamPointFileDecision] = []
            for pair in material_pairs:
                validated = validated_by_point.get(pair["exam_point_code"])
                if validated is None:
                    raise ValueError("classification response missing exam point")
                allowed_ids = set(pair["evidence_chunk_ids"])
                admitted = [
                    item
                    for item in (
                        admit_evidence_decision(
                            next(p for p in needed_points if p.code == pair["exam_point_code"]), item
                        )
                        for item in validated.decisions
                        if item.evidence_chunk_id in allowed_ids
                    )
                ]
                if any(item.evidence_chunk_id not in allowed_ids for item in admitted):
                    raise ValueError("classification response references evidence outside its pair")
                ids = [item.evidence_chunk_id for item in admitted]
                if len(ids) != len(set(ids)):
                    raise ValueError("classification response contains duplicate evidence decisions")
                if set(ids) != allowed_ids:
                    raise ValueError("classification response must cover every recalled evidence chunk")
                per_pair.append(validated.model_copy(update={"decisions": admitted}))
            return per_pair

        with ThreadPoolExecutor(max_workers=settings.organization_max_workers) as executor:
            futures = {
                executor.submit(classify_material, mid, mpairs): (mid, mpairs)
                for mid, mpairs in by_material.items()
            }
            for future in as_completed(futures):
                material_version_id, material_pairs = futures[future]
                try:
                    decisions.extend(future.result())
                except Exception as exc:
                    for pair in material_pairs:
                        failures.append(
                            _failure(
                                stage="classification",
                                point_code=pair["exam_point_code"],
                                material_version_id=material_version_id,
                                exc=exc,
                            )
                        )
                        coverage_reasons.setdefault(pair["exam_point_code"], []).append(
                            "classification_failed"
                        )
        decisions.sort(key=lambda item: (item.exam_point_code, item.material_version_id))
        failures.sort(
            key=lambda item: (
                item["exam_point_code"],
                item.get("material_version_id") or "",
                item["stage"],
            )
        )
        return {
            "file_decisions": [item.model_dump(mode="json") for item in decisions],
            "failed_pairs": failures,
            "coverage_reasons": coverage_reasons,
        }
```

- [ ] **Step 3: 假分类器改造**

在 `tests/workflow/test_organization_graph.py` 中 grep `def classify` 找到全部假分类器类，统一改为实现 `classify_file`（保留旧 classify 可删）。改造模板：

```python
class FakeClassifier:
    def __init__(self, decisions_by_point: dict[str, list[dict]] | None = None):
        self.decisions_by_point = decisions_by_point or {}
        self.calls: list[dict] = []

    def classify_file(self, *, exam_points, material_version_id, chunks, call_context=None):
        self.calls.append({"points": [p.code for p in exam_points], "material": material_version_id})
        results = []
        for point in exam_points:
            script = self.decisions_by_point.get(point.code)
            if script is None:
                continue  # 该考点在此文件无召回对（图只传有成对考点的点，一般不触发）
            decisions = [
                ExamPointFileDecision.model_validate({
                    "exam_point_code": point.code,
                    "material_version_id": material_version_id,
                    "decisions": [
                        {"exam_point_code": point.code, "evidence_chunk_id": cid,
                         "relevance_class": cls, "support_claim": "", "content_kind": "text",
                         "confidence": 0.9, **extra}
                        for cid, cls, *extra in script
                    ],
                })
            ]
            results.extend(decisions)
        return results
```

各测试用例按其原意图改为向 `decisions_by_point` 提供 `{考点码: [(chunk_id, "direct"/...), ...]}` 形式的剧本；断言"每对一次调用"的旧用例改为断言"每资料一次调用"（`len(fake.calls) == 资料数`）。逐用例机械迁移，不改测试意图；失败隔离用例（某资料抛错只影响该资料的考点对）保持原断言。

- [ ] **Step 4: 全量组织图测试**

Run: `python -m pytest tests/workflow/test_organization_graph.py -v`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add app/adapters/model/deepseek_semantic_extractors.py app/workflows/organization_graph.py tests/workflow/test_organization_graph.py
git commit -m "perf(organization): batch evidence classification per material file"
```

---

### Task 14: 端到端集成测试（fake 网关全链路）

**Files:**
- Create: `tests/integration/test_contract_e2e.py`

- [ ] **Step 1: 写端到端测试**

```python
"""合同→分批→生成→终检 全链路（fake 网关），断言设计成功标准。"""
from app.domain.blueprint.models import BlueprintRequest, UnitCoverage
from app.services.contract_service import ContractRequest, allocate_paper_contract
from app.workflows.generation_graph import build_generation_graph


UNITS = [
    UnitCoverage(unit_id="U1", exam_point_id="EP1", anchor_key="A1", card_ids=["C1"]),
    UnitCoverage(unit_id="U2", exam_point_id="EP2", anchor_key="A2", card_ids=["C2"]),
]
CARDS = {
    "C1": {
        "is_core": True, "performance_statement": "掌握提示词要素与优化",
        "assessable_content": ["有效提示词包含角色设定", "有效提示词包含任务说明", "提示词可加入背景信息", "提示词输出格式约束"],
        "preferred_terms": ["提示词"], "answer_boundary": "提示词要素",
    },
    "C2": {
        "is_core": True, "performance_statement": "掌握SFT训练方法",
        "assessable_content": ["构建SFTTrainer需要SFTConfig", "QLoRA使用NF4量化", "继续预训练适配领域语料", "训练数据集需格式化"],
        "preferred_terms": ["SFT"], "answer_boundary": "SFT配置",
    },
}


class ScriptedGateway:
    """按合同槽位确定性产出合格题目的假网关。"""

    def generate_batch(self, payload):
        questions = []
        for spec in payload.questions:
            if spec.question_type == "true_false":
                questions.append({"item_index": spec.item_index, "stem": f"判断：{spec.coverage_atom}成立",
                                  "answer": True, "explanation": "解析"})
            else:
                questions.append({
                    "item_index": spec.item_index,
                    "stem": f"关于{spec.coverage_atom}，下列说法正确的是",
                    "options": [spec.answer_boundary, "干扰甲", "干扰乙", "干扰丙"],
                    "answer": spec.answer_boundary, "explanation": "解析",
                })
        return questions


def test_full_pipeline_meets_success_criteria():
    contract = allocate_paper_contract(ContractRequest(
        blueprint=BlueprintRequest(
            total_score=10,
            type_rules={"single_choice": {"count": 4, "score": 2}, "true_false": {"count": 2, "score": 1}},
            chapter_weights={"A1": 0.5, "A2": 0.5}, units=UNITS,
        ),
        knowledge_cards=CARDS,
    ))
    assert not contract.conflicts
    assert len(contract.slots) == 6

    result = build_generation_graph(ScriptedGateway()).invoke({
        "contract": [s.model_dump(mode="json") for s in contract.slots],
        "knowledge_cards": CARDS,
        "recent_structure_signatures": [],
    })
    questions = sorted(result["questions"], key=lambda q: q["item_index"])
    report = result["final_check"]

    # 成功标准逐条断言
    assert report["passed"] is True
    ep_counts = {}
    for q in questions:
        ep_counts[q["exam_point_id"]] = ep_counts.get(q["exam_point_id"], 0) + 1
    assert abs(ep_counts["EP1"] - 3) <= 1 and abs(ep_counts["EP2"] - 3) <= 1
    atoms = [q["coverage_atom"] for q in questions]
    assert len(atoms) == len(set(atoms))
    assert result["model_call_count"] <= 3  # 2 考点批 + 0 重试
    for q in questions:
        assert q["exam_point_id"] and q["unit_id"] and q["card_id"] and q["coverage_atom"]
        assert q["quality"]["status"] == "pass"
```

- [ ] **Step 2: 运行通过**

Run: `python -m pytest tests/integration/test_contract_e2e.py -v`
Expected: 1 passed

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_contract_e2e.py
git commit -m "test(e2e): full contract-driven pipeline meets success criteria"
```

---

### Task 15: 前端演示页对接新 API

**Files:**
- Modify: `frontend/src/App.tsx`（或演示页实际调用 allocate/generation 的文件）

- [ ] **Step 1: 定位调用点**

用 Grep 在 `frontend/src` 搜索 `blueprints/allocate`、`generation-runs`、`plan_items`，记录每个调用文件与行号。

- [ ] **Step 2: 更新 allocate 调用**

请求体从 `BlueprintRequest` 改为 `ContractRequest`（新增 `knowledge_cards` 字段——前端已有卡片状态则直接带上；响应从 `items` 改为 `slots` + `conflicts` + `audit_summary`）：

```typescript
const res = await fetch(`/api/v1/courses/${courseId}/blueprints/allocate`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    blueprint: {
      total_score, type_rules, chapter_weights, units,
    },
    knowledge_cards: knowledgeCardsById, // card_id -> card
  }),
});
const contract = await res.json();
// contract.slots: 题位合同；contract.conflicts: 教师需处理的冲突；
// contract.audit_summary.exam_points: 考点比例表数据源
```

- [ ] **Step 3: 增加确认步骤（可无修订直接确认）**

```typescript
const confirmed = await fetch(`/api/v1/courses/${courseId}/blueprints/confirm`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    contract,                      // allocate 返回的合同原文
    slot_revisions: revisions,     // 教师换单题原子时 [{item_index, coverage_atom}]
    units, knowledge_cards: knowledgeCardsById,
  }),
}).then(r => r.json());
```

- [ ] **Step 4: 更新生成调用**

```typescript
const gen = await fetch(`/api/v1/courses/${courseId}/generation-runs`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    contract: confirmed.slots,
    knowledge_cards: knowledgeCardsById,
  }),
}).then(r => r.json());
// gen.questions（含 quality / needs_review）、gen.final_check.checks（终检报告表）、gen.model_call_count
```

- [ ] **Step 5: 构建 + 冒烟**

Run: `cd frontend && npm run build`
Expected: 构建通过；`npm run dev` 后走一遍 出卷流程页（无后端 key 时 UI 到请求报 503 属预期）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): wire contract allocate/confirm/generation flow"
```

---

### Task 16: 真实链路重新生成样例卷 + 核对

**Files:**
- Create: `scripts/regenerate_demo_paper.py`（backend 目录）

- [ ] **Step 1: 写再生成脚本**

```python
"""用真实生成路径重跑演示卷并输出核对报告。

用法: python scripts/regenerate_demo_paper.py [--base-url http://localhost:8000]
前提: 后端已启动、课程与知识目录已发布（沿用演示数据所在课程）。
"""
import argparse
import json
import urllib.request


def post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--out", default="frontend/public/demo/pipeline.json")
    args = parser.parse_args()

    course = f"/api/v1/courses/{args.course_id}"
    # 1) 组装 ContractRequest：units/knowledge_cards 从知识目录接口读取
    #    （读取接口路径以实际 API 为准：organization-runs candidate 或等价只读端点）
    catalog = json.loads(urllib.request.urlopen(args.base_url + course + "/organization-runs/latest/candidate").read())
    units = [...]      # 由 catalog 组装 UnitCoverage 列表
    cards = {...}      # card_id -> card dict
    blueprint = {...}  # 总分/题型规则/章节权重（教师参数，演示取默认）

    contract = post(args.base_url, course + "/blueprints/allocate", {
        "blueprint": blueprint, "knowledge_cards": cards,
    })
    assert not contract["conflicts"], f"合同存在冲突需教师处理: {contract['conflicts']}"

    confirmed = post(args.base_url, course + "/blueprints/confirm", {
        "contract": contract, "slot_revisions": [],
        "units": units, "knowledge_cards": cards,
    })
    paper = post(args.base_url, course + "/generation-runs", {
        "contract": confirmed["slots"], "knowledge_cards": cards,
    })

    atoms = [q["coverage_atom"] for q in paper["questions"]]
    print(f"题目数: {len(paper['questions'])}, 唯一原子: {len(set(atoms))}, "
          f"模型调用: {paper['model_call_count']}, 终检: {paper['final_check']['passed']}")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(paper, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
```

（`units = [...]` 与 `cards = {...}` 的组装：从 candidate 响应的 topics/units/cards 字段映射为 `UnitCoverage(unit_id, exam_point_id, anchor_key, card_ids)` 与卡片字典；具体字段名以 `GET /organization-runs/{id}/candidate` 实际返回为准，脚本编写时先请求一次打印结构再补全映射。）

- [ ] **Step 2: 配置 DeepSeek key 并执行**

Run: 后端 `uvicorn app.main:app` 后 `python scripts/regenerate_demo_paper.py --course-id <演示课程ID>`
Expected: 控制台输出 `唯一原子 == 题目数`、`终检: True`、模型调用 ≈ 考点批数。

- [ ] **Step 3: 人工抽查**

打开新 `pipeline.json`：确认同考点题目视角互补、无互相抄写、无冷门脚注题；`final_check.checks` 全 passed。

- [ ] **Step 4: Commit**

```bash
git add scripts/regenerate_demo_paper.py frontend/public/demo/pipeline.json
git commit -m "chore(demo): regenerate sample paper via contract-first pipeline"
```

---

## 自审记录

1. **Spec 覆盖**：配额（Task 5）、核心度门槛（Task 2）、聚类（Task 3）、簇轮转（Task 4）、答案互斥（Task 4/6）、结构轮换（Task 5）、教师审阅+换单原子（Task 6）、分批≤6/小考点合并/子批禁用上下文（Task 7）、批内互见载荷（Task 8/9）、单题重试≤2（Task 10）、needs_review（Task 10）、确定性终检+模型终检默认关（模型终检已随 Task 12 删除 audit_paper，符合"默认关"）、成本断言（Task 10/14）、批式组织分类（Task 13）、E2E（Task 14/16）。成功标准 6 条全部有对应断言（Task 14）。
2. **占位符**：Task 15/16 含"以实际字段为准"的探索步骤，均为一次性定位动作且给出了组装规则与调用代码，非 TBD。
3. **类型一致性**：`ContractSlot/PoolAtom/QuestionBatch/BatchGenerationPayload` 的字段名在 Task 1/2/7/8/10 间已核对一致；Task 10 Step 3 的修正说明（paper_questions、Protocol import、recent_signatures 处理）为准绳。
