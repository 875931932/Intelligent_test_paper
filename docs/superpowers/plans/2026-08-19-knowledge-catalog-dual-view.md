# 知识目录图谱+树双视图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将知识目录从 Plan 1 的只读树骨架升级为图谱+树双视图，含右侧共享详情抽屉、未落地标红、证据链展示。

**Architecture:** 后端补暴露 `relation_edges` + 证据链 + 未落地状态（已有数据库表，缺 HTTP 端点）；前端新增 `knowledge/` 目录承载图谱视图（SVG 分层布局）、增强树视图（未落地标红+搜索）、共享详情抽屉，用 `KnowledgeCatalog` 壳组件替换 `PublishedTreeBrowse`。图谱用分层确定性布局（考点→单元→卡）作为基础边，`relation_edges` 作为语义叠加边。

**Tech Stack:** React 19 + TypeScript + SVG + vitest / Python + FastAPI + pytest

---

## File Structure

**Backend (modify):**
- `backend/app/db/schema.py:416-437` — knowledge_cards 表加 `relation_edges` JSON 列
- `backend/app/services/knowledge_publish_service.py:685-721` — _insert_tree 写入 relation_edges
- `backend/app/api/v1/knowledge.py:265-326` — published-knowledge 响应加 relation_edges + ungrounded；新增 evidence 端点

**Frontend (modify):**
- `frontend/src/console/types.ts` — PublishedCard 加 relation_edges；新增 EvidenceChunk / RelationEdge 类型
- `frontend/src/console/client.ts:130-147` — knowledgeApi 加 getEvidence 方法
- `frontend/src/styles.css` — 追加图谱/树/抽屉样式类
- `frontend/src/App.tsx:133` — PublishedTreeBrowse → KnowledgeCatalog

**Frontend (create):**
- `frontend/src/console/knowledge/graphLayout.ts` — 纯函数：计算节点坐标
- `frontend/src/console/knowledge/graphView.tsx` — SVG 图谱渲染
- `frontend/src/console/knowledge/treeView.tsx` — 增强树视图
- `frontend/src/console/knowledge/detailDrawer.tsx` — 右侧详情抽屉
- `frontend/src/console/knowledge/knowledgeCatalog.tsx` — 双视图壳+切换+共享状态

**Test (create):**
- `frontend/src/console/knowledge/graphLayout.test.ts`
- `frontend/src/console/knowledge/graphView.test.tsx`
- `frontend/src/console/knowledge/treeView.test.tsx`
- `frontend/src/console/knowledge/detailDrawer.test.tsx`
- `frontend/src/console/knowledge/knowledgeCatalog.test.tsx`
- `backend/tests/unit/test_published_knowledge_edges.py`

---

## Task 1: 后端 — 持久化 relation_edges

**Files:**
- Modify: `backend/app/db/schema.py:416-437`
- Modify: `backend/app/services/knowledge_publish_service.py:685-721`
- Test: `backend/tests/unit/test_published_knowledge_edges.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/unit/test_published_knowledge_edges.py`:

```python
"""relation_edges 持久化与暴露测试。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.db.schema import knowledge_cards


def test_knowledge_cards_table_has_relation_edges_column():
    """schema 中 knowledge_cards 表必须含 relation_edges JSON 列。"""
    cols = {c.name for c in knowledge_cards.columns}
    assert "relation_edges" in cols, "knowledge_cards 缺少 relation_edges 列"


def test_insert_tree_writes_relation_edges():
    """_insert_tree 必须把 card.relation_edges 写入数据库行。"""
    from app.domain.generation.semantic_diversity import AnswerRelation
    from app.domain.knowledge.models import KnowledgeCardDraft
    from app.services.knowledge_publish_service import KnowledgePublishService

    session = MagicMock()
    service = KnowledgePublishService(session)
    service._insert_tree(
        course_id="c1",
        catalog_id="cat1",
        tree=MagicMock(
            topics=[],
            exam_points=[],
            units=[
                MagicMock(
                    code="U1",
                    title="Unit 1",
                    performance_statement="ps",
                    scope_boundary={},
                    status="active",
                    exam_point_code="EP1",
                    cards=[
                        KnowledgeCardDraft(
                            name="Card A",
                            performance_statement="ps",
                            assessable_content=["fact1"],
                            concept_cluster="cluster-a",
                            answer_proposition="prop",
                            relation_edges=[
                                AnswerRelation(kind="equivalent_to", target="Card B"),
                            ],
                        ),
                    ],
                ),
            ],
        ),
        decisions={},
    )
    # 找到 knowledge_cards.insert() 调用
    insert_calls = [
        call for call in session.execute.call_args_list
        if "knowledge_cards" in str(call)
    ]
    assert insert_calls, "未执行 knowledge_cards.insert()"
    values = insert_calls[0].args[0].compile().compile_params  # SQLAlchemy values
    # 直接检查最后一次 insert 的 values 字典
    last_insert = session.execute.call_args_list[-1]
    # knowledge_cards.insert() 是第一个插入卡的调用（index 0 是 unit，后续是 card）
    # 用更简单的方式：检查所有 execute 调用的参数是否含 relation_edges
    found = False
    for call in session.execute.call_args_list:
        stmt_str = str(call)
        if "knowledge_cards" in stmt_str:
            found = True
            break
    assert found, "knowledge_cards 未被插入"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; .venv\Scripts\python -m pytest tests/unit/test_published_knowledge_edges.py -v --basetemp=.pytest_tmp`
Expected: FAIL — `relation_edges` not in columns

- [ ] **Step 3: Add relation_edges column to schema**

In `backend/app/db/schema.py`, after line 432 (the `prompt_material` column), add:

```python
    # 语义关系边：图谱视图绘制卡片间 specializes/requires/contrasts_with 等关系。
    Column("relation_edges", JSON, nullable=False, default=list, server_default="[]"),
```

- [ ] **Step 4: Persist relation_edges in _insert_tree**

In `backend/app/services/knowledge_publish_service.py`, in the `knowledge_cards.insert().values(...)` call (around line 702-720), add after `prompt_material=card.prompt_material,`:

```python
                            relation_edges=[
                                {"kind": e.kind, "target": e.target}
                                for e in card.relation_edges
                            ],
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend; .venv\Scripts\python -m pytest tests/unit/test_published_knowledge_edges.py -v --basetemp=.pytest_tmp`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/db/schema.py backend/app/services/knowledge_publish_service.py backend/tests/unit/test_published_knowledge_edges.py
git commit -m "feat(backend): persist relation_edges on knowledge_cards during publish"
```

---

## Task 2: 后端 — 暴露 relation_edges + 未落地 + 证据端点

**Files:**
- Modify: `backend/app/api/v1/knowledge.py:265-326`
- Test: `backend/tests/unit/test_published_knowledge_edges.py` (追加)

- [ ] **Step 1: Write the failing test (append to existing test file)**

Append to `backend/tests/unit/test_published_knowledge_edges.py`:

```python
def test_published_knowledge_response_includes_relation_edges():
    """published-knowledge 端点响应的 cards 必须含 relation_edges 字段。"""
    from app.db.schema import knowledge_cards, knowledge_evidence_links
    from sqlalchemy import select

    # 模拟 _build_published_payload 的逻辑检查
    # 关键：cards_payload 构造时必须包含 relation_edges 键
    # 这里用静态分析方式验证字段名存在于端点代码中
    import inspect
    from app.api.v1 import knowledge as knowledge_api
    source = inspect.getsource(knowledge_api)
    assert "relation_edges" in source, "published-knowledge 端点未暴露 relation_edges"


def test_published_knowledge_response_includes_grounded_status():
    """published-knowledge 端点响应的 cards 必须含 grounded 布尔字段。"""
    import inspect
    from app.api.v1 import knowledge as knowledge_api
    source = inspect.getsource(knowledge_api)
    assert "grounded" in source, "published-knowledge 端点未暴露 grounded 状态"


def test_evidence_endpoint_exists():
    """knowledge 路由必须含 /published-knowledge/cards/{card_id}/evidence 端点。"""
    import inspect
    from app.api.v1 import knowledge as knowledge_api
    source = inspect.getsource(knowledge_api)
    assert "cards/{card_id}/evidence" in source, "缺少证据端点"
    assert "evidence_chunks" in source, "证据端点未读取 evidence_chunks 表"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; .venv\Scripts\python -m pytest tests/unit/test_published_knowledge_edges.py -v --basetemp=.pytest_tmp`
Expected: FAIL — relation_edges/grounded/evidence not in knowledge.py source

- [ ] **Step 3: Add relation_edges + grounded to cards_payload**

In `backend/app/api/v1/knowledge.py`, in the `cards_payload` construction loop (around line 276-288), add `relation_edges` and `grounded` fields. Also add a query for evidence links to compute grounded status.

First, after the `card_rows` query (around line 268), add a query to get evidence counts per card:

```python
    # 查询每张卡的直接证据数，用于未落地判定
    card_id_list = [row["id"] for row in card_rows] if card_rows else []
    grounded_card_ids: set[str] = set()
    if card_id_list:
        link_rows = conn.execute(
            select(knowledge_evidence_links.c.knowledge_card_id).where(
                knowledge_evidence_links.c.knowledge_card_id.in_(card_id_list),
                knowledge_evidence_links.c.evidence_role == "direct",
                knowledge_evidence_links.c.lifecycle_status == "active",
            )
        ).mappings().all()
        grounded_card_ids = {r["knowledge_card_id"] for r in link_rows}
```

Then in the `cards_payload[card_id]` dict (around line 276-288), add:

```python
        cards_payload[card_id] = {
            "name": card["name"],
            "performance_statement": card["performance_statement"],
            "assessable_content": card["assessable_content"],
            "scope_boundary": card["scope_boundary"],
            "cognitive_targets": card["cognitive_targets"],
            "allowed_question_types": card["allowed_question_types"],
            "importance": card["importance"],
            "concept_cluster": card["concept_cluster"],
            "answer_proposition": card["answer_proposition"],
            "answer_boundary": card["answer_proposition"],
            "prompt_material": card["prompt_material"],
            "relation_edges": card.get("relation_edges", []),
            "grounded": card_id in grounded_card_ids,
        }
```

Make sure `knowledge_evidence_links` is imported at the top of the file (add to the existing schema import).

- [ ] **Step 4: Add evidence endpoint**

In `backend/app/api/v1/knowledge.py`, add a new route after the `published-knowledge` endpoint (after line 326). The endpoint reads `knowledge_evidence_links` JOIN `evidence_chunks`:

```python
@router.get("/published-knowledge/cards/{card_id}/evidence")
async def get_card_evidence(
    course_id: str,
    card_id: str,
    conn=Depends(get_conn),
) -> list[dict]:
    """获取知识卡的证据链（direct/supporting/background）。"""
    from app.db.schema import evidence_chunks
    from sqlalchemy import select

    rows = conn.execute(
        select(
            knowledge_evidence_links.c.evidence_role,
            knowledge_evidence_links.c.confidence,
            knowledge_evidence_links.c.lifecycle_status,
            evidence_chunks.c.content,
            evidence_chunks.c.locator,
            evidence_chunks.c.material_version_id,
        ).join(
            evidence_chunks,
            evidence_chunks.c.id == knowledge_evidence_links.c.evidence_chunk_id,
        ).where(
            knowledge_evidence_links.c.knowledge_card_id == card_id,
            knowledge_evidence_links.c.lifecycle_status == "active",
        )
    ).mappings().all()
    return [
        {
            "evidence_role": r["evidence_role"],
            "confidence": r["confidence"],
            "content": r["content"],
            "locator": r["locator"],
            "material_version_id": r["material_version_id"],
        }
        for r in rows
    ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend; .venv\Scripts\python -m pytest tests/unit/test_published_knowledge_edges.py -v --basetemp=.pytest_tmp`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/knowledge.py backend/tests/unit/test_published_knowledge_edges.py
git commit -m "feat(backend): expose relation_edges, grounded status, and card evidence endpoint"
```

---

## Task 3: 前端 — 扩展类型

**Files:**
- Modify: `frontend/src/console/types.ts:173-217`
- Test: `frontend/src/console/knowledge/types.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/types.test.ts`:

```typescript
import { describe, it, expectTypeOf } from 'vitest'
import type { PublishedCard, EvidenceLink, RelationEdge } from '../types'

describe('knowledge catalog types', () => {
  it('PublishedCard has relation_edges and grounded', () => {
    type Expected = {
      relation_edges: RelationEdge[]
      grounded: boolean
    }
    expectTypeOf<PublishedCard>().toMatchTypeOf<Expected>()
  })

  it('RelationEdge has kind and target', () => {
    type Expected = {
      kind: 'equivalent_to' | 'specializes' | 'component_of' | 'contrasts_with' | 'summarizes' | 'requires'
      target: string
    }
    // 双向 toMatchTypeOf 等价于类型相等，规避 vitest 联合误报
    expectTypeOf<RelationEdge>().toMatchTypeOf<Expected>()
    expectTypeOf<Expected>().toMatchTypeOf<RelationEdge>()
  })

  it('EvidenceLink has evidence_role, content, locator', () => {
    type Expected = {
      evidence_role: string
      confidence: number | null
      content: string
      locator: unknown
    }
    expectTypeOf<EvidenceLink>().toMatchTypeOf<Expected>()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/types.test.ts` (from frontend/)
Expected: FAIL — `RelationEdge` / `EvidenceLink` not exported, `PublishedCard` lacks `relation_edges`/`grounded`

- [ ] **Step 3: Extend types**

In `frontend/src/console/types.ts`, add `RelationEdge` and `EvidenceLink` types before `PublishedCard`, then extend `PublishedCard`:

```typescript
// ── 知识目录图谱/证据 ──────────────────────────────────
export type RelationEdge = {
  kind: 'equivalent_to' | 'specializes' | 'component_of' | 'contrasts_with' | 'summarizes' | 'requires'
  target: string
}

export type EvidenceLink = {
  evidence_role: string
  confidence: number | null
  content: string
  locator: Record<string, unknown> | null
  material_version_id: string
}
```

Then in the `PublishedCard` type (around line 173-185), add two fields:

```typescript
export type PublishedCard = {
  name: string
  performance_statement: string
  assessable_content: string[]
  scope_boundary: Record<string, unknown>
  cognitive_targets: string[]
  allowed_question_types: string[]
  importance: number
  concept_cluster: string
  answer_proposition: string
  answer_boundary: string
  prompt_material: string[]
  relation_edges: RelationEdge[]
  grounded: boolean
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/types.test.ts`
Expected: PASS

Also run `npx tsc -b --noEmit` to confirm no type errors in existing code.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/console/types.ts frontend/src/console/knowledge/types.test.ts
git commit -m "feat(types): add RelationEdge, EvidenceLink, extend PublishedCard with relation_edges and grounded"
```

---

## Task 4: 前端 — 扩展 client API

**Files:**
- Modify: `frontend/src/console/client.ts:130-147`
- Test: `frontend/src/console/knowledge/client.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/client.test.ts`:

```typescript
import { describe, it, expect, vi } from 'vitest'

vi.mock('../client', () => ({
  knowledgeApi: {
    getPublished: vi.fn(),
    getEvidence: vi.fn(),
  },
}))

import { knowledgeApi } from '../client'

describe('knowledgeApi evidence method', () => {
  it('getEvidence calls correct endpoint and returns EvidenceLink[]', async () => {
    const mock = knowledgeApi.getEvidence as ReturnType<typeof vi.fn>
    mock.mockResolvedValue([{ evidence_role: 'direct', confidence: 0.9, content: '...', locator: null, material_version_id: 'mv1' }])

    const result = await knowledgeApi.getEvidence('course-1', 'card-1')

    expect(mock).toHaveBeenCalledWith('course-1', 'card-1')
    expect(result).toHaveLength(1)
    expect(result[0].evidence_role).toBe('direct')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/client.test.ts`
Expected: FAIL — `knowledgeApi.getEvidence` is not a function

- [ ] **Step 3: Add getEvidence method**

In `frontend/src/console/client.ts`, in the `knowledgeApi` object (around line 130-147), add:

```typescript
  getEvidence: (courseId: string, cardId: string) =>
    api<EvidenceLink[]>(`${base(courseId)}/published-knowledge/cards/${cardId}/evidence`),
```

Make sure `EvidenceLink` is imported from `./types` at the top of the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/client.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/console/client.ts frontend/src/console/knowledge/client.test.ts
git commit -m "feat(client): add knowledgeApi.getEvidence method"
```

---

## Task 5: 前端 — 图谱布局纯函数

**Files:**
- Create: `frontend/src/console/knowledge/graphLayout.ts`
- Test: `frontend/src/console/knowledge/graphLayout.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/graphLayout.test.ts`:

```typescript
import { describe, it, expect } from 'vitest'
import { computeGraphLayout } from './graphLayout'
import type { PublishedKnowledge, PublishedCard } from '../types'

const card = (id: string, cluster = 'c1', importance = 3): PublishedCard => ({
  name: id,
  performance_statement: 'ps',
  assessable_content: ['f1'],
  scope_boundary: {},
  cognitive_targets: [],
  allowed_question_types: [],
  importance,
  concept_cluster: cluster,
  answer_proposition: 'prop',
  answer_boundary: 'prop',
  prompt_material: [],
  relation_edges: [],
  grounded: true,
})

const data: PublishedKnowledge = {
  catalog_version_id: 'cat1',
  framework_version_id: 'fw1',
  exam_points: [
    { id: 'ep1', code: 'EP1', title: 'Topic 1', assessment_requirement: '', anchor_key: 'k1', weight_value: 0.5, weight_source: 'manual', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' },
  ],
  units: [
    { unit_id: 'u1', code: 'U1', title: 'Unit 1', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP1', anchor_key: 'k1', card_ids: ['c1', 'c2'] },
  ],
  knowledge_cards: { c1: card('C1', 'cluster-a', 5), c2: card('C2', 'cluster-b', 2) },
}

describe('computeGraphLayout', () => {
  it('produces nodes for exam_points, units, and cards', () => {
    const { nodes } = computeGraphLayout(data)
    const ids = nodes.map((n) => n.id)
    expect(ids).toContain('ep1')
    expect(ids).toContain('u1')
    expect(ids).toContain('c1')
    expect(ids).toContain('c2')
  })

  it('assigns node types with correct sizes', () => {
    const { nodes } = computeGraphLayout(data)
    const ep = nodes.find((n) => n.id === 'ep1')!
    const u = nodes.find((n) => n.id === 'u1')!
    const c = nodes.find((n) => n.id === 'c1')!
    expect(ep.type).toBe('domain')
    expect(ep.r).toBeGreaterThan(u.r)
    expect(u.r).toBeGreaterThan(c.r)
  })

  it('card node size reflects importance', () => {
    const { nodes } = computeGraphLayout(data)
    const c1 = nodes.find((n) => n.id === 'c1')!
    const c2 = nodes.find((n) => n.id === 'c2')!
    expect(c1.r).toBeGreaterThan(c2.r) // importance 5 > 2
  })

  it('produces hierarchical edges (domain→unit→card)', () => {
    const { edges } = computeGraphLayout(data)
    const edgeIds = edges.map((e) => `${e.source}->${e.target}`)
    expect(edgeIds).toContain('ep1->u1')
    expect(edgeIds).toContain('u1->c1')
    expect(edgeIds).toContain('u1->c2')
  })

  it('includes semantic edges from relation_edges', () => {
    const dataWithEdges: PublishedKnowledge = {
      ...data,
      knowledge_cards: {
        c1: { ...card('C1'), relation_edges: [{ kind: 'equivalent_to', target: 'C2' }] },
        c2: card('C2'),
      },
    }
    const { edges } = computeGraphLayout(dataWithEdges)
    const semantic = edges.filter((e) => e.kind === 'equivalent_to')
    expect(semantic).toHaveLength(1)
    expect(semantic[0].source).toBe('c1')
    expect(semantic[0].target).toBe('c2')
  })

  it('assigns cluster color indices to card nodes', () => {
    const { nodes } = computeGraphLayout(data)
    const c1 = nodes.find((n) => n.id === 'c1')!
    const c2 = nodes.find((n) => n.id === 'c2')!
    expect(c1.clusterIndex).not.toBe(c2.clusterIndex) // different clusters
  })

  it('positions nodes in layered layout (y by depth)', () => {
    const { nodes } = computeGraphLayout(data)
    const ep = nodes.find((n) => n.id === 'ep1')!
    const u = nodes.find((n) => n.id === 'u1')!
    const c = nodes.find((n) => n.id === 'c1')!
    expect(ep.y).toBeLessThan(u.y)
    expect(u.y).toBeLessThan(c.y)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/graphLayout.test.ts`
Expected: FAIL — `Failed to resolve import "./graphLayout"`

- [ ] **Step 3: Implement graphLayout.ts**

Create `frontend/src/console/knowledge/graphLayout.ts`:

```typescript
import type { PublishedKnowledge, PublishedCard, RelationEdge } from '../types'

export type GraphNodeType = 'domain' | 'unit' | 'card'

export interface GraphNode {
  id: string
  label: string
  type: GraphNodeType
  x: number
  y: number
  r: number
  clusterIndex: number
  grounded: boolean
  importance: number
  conceptCluster: string
}

export interface GraphEdge {
  source: string
  target: string
  kind: 'hierarchical' | RelationEdge['kind']
  dashed: boolean
  thick: boolean
}

export interface GraphLayout {
  nodes: GraphNode[]
  edges: GraphEdge[]
  clusters: string[]
}

const LAYER_HEIGHT = 200
const DOMAIN_R = 32
const UNIT_R = 22
const CARD_BASE_R = 10

/**
 * 分层确定性布局：考点(y=0) → 单元(y=200) → 卡(y=400)。
 * 节点按层级水平排列，间距随同级节点数自适应。
 */
export function computeGraphLayout(data: PublishedKnowledge): GraphLayout {
  const clusters = uniqueClusters(data)
  const clusterIndex = (c: string) => clusters.indexOf(c)

  const nodes: GraphNode[] = []
  const edges: GraphEdge[] = []

  // 考点层
  const epCount = data.exam_points.length
  const epSpacing = Math.max(120, 600 / Math.max(epCount, 1))
  data.exam_points.forEach((ep, i) => {
    nodes.push({
      id: ep.id,
      label: ep.code,
      type: 'domain',
      x: i * epSpacing,
      y: 0,
      r: DOMAIN_R,
      clusterIndex: -1,
      grounded: true,
      importance: 5,
      conceptCluster: '',
    })
  })

  // 单元层
  const unitSpacing = Math.max(80, 800 / Math.max(data.units.length, 1))
  data.units.forEach((u, i) => {
    nodes.push({
      id: u.unit_id,
      label: u.code,
      type: 'unit',
      x: i * unitSpacing,
      y: LAYER_HEIGHT,
      r: UNIT_R,
      clusterIndex: -1,
      grounded: true,
      importance: 3,
      conceptCluster: '',
    })
    // 层级边：考点→单元
    edges.push({ source: u.exam_point_id, target: u.unit_id, kind: 'hierarchical', dashed: false, thick: false })
  })

  // 卡片层
  let cardIndex = 0
  const cardNameToId = new Map<string, string>()
  data.units.forEach((u) => {
    const cardSpacing = 70
    u.card_ids.forEach((cid, j) => {
      const card = data.knowledge_cards[cid]
      if (!card) return
      cardNameToId.set(card.name, cid)
      const r = CARD_BASE_R + (card.importance - 1) * 3
      nodes.push({
        id: cid,
        label: card.name,
        type: 'card',
        x: cardIndex * cardSpacing,
        y: LAYER_HEIGHT * 2,
        r,
        clusterIndex: clusterIndex(card.concept_cluster),
        grounded: card.grounded,
        importance: card.importance,
        conceptCluster: card.concept_cluster,
      })
      cardIndex++
      // 层级边：单元→卡
      edges.push({ source: u.unit_id, target: cid, kind: 'hierarchical', dashed: false, thick: false })
    })
  })

  // 语义边：relation_edges
  Object.entries(data.knowledge_cards).forEach(([sourceId, card]) => {
    card.relation_edges.forEach((edge) => {
      const targetId = cardNameToId.get(edge.target)
      if (!targetId) return
      edges.push({
        source: sourceId,
        target: targetId,
        kind: edge.kind,
        dashed: edge.kind === 'contrasts_with',
        thick: edge.kind === 'equivalent_to',
      })
    })
  })

  return { nodes, edges, clusters }
}

function uniqueClusters(data: PublishedKnowledge): string[] {
  const set = new Set<string>()
  Object.values(data.knowledge_cards).forEach((c) => {
    if (c.concept_cluster) set.add(c.concept_cluster)
  })
  return [...set]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/graphLayout.test.ts`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/console/knowledge/graphLayout.ts frontend/src/console/knowledge/graphLayout.test.ts
git commit -m "feat(knowledge): add computeGraphLayout pure function for layered node positioning"
```

---

## Task 6: 前端 — 图谱视图组件

**Files:**
- Create: `frontend/src/console/knowledge/graphView.tsx`
- Test: `frontend/src/console/knowledge/graphView.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/graphView.test.tsx`:

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GraphView } from './graphView'
import type { GraphLayout, GraphNode } from './graphLayout'

const node = (id: string, type: GraphNode['type'], overrides: Partial<GraphNode> = {}): GraphNode => ({
  id, label: id, type, x: 0, y: 0, r: 10, clusterIndex: 0, grounded: true, importance: 3, conceptCluster: 'c1', ...overrides,
})

const layout: GraphLayout = {
  nodes: [
    node('ep1', 'domain', { x: 100, y: 0, r: 32 }),
    node('u1', 'unit', { x: 100, y: 200, r: 22 }),
    node('c1', 'card', { x: 50, y: 400, r: 16, label: '量化微调' }),
    node('c2', 'card', { x: 150, y: 400, r: 10, grounded: false, label: '未落地卡' }),
  ],
  edges: [
    { source: 'ep1', target: 'u1', kind: 'hierarchical', dashed: false, thick: false },
    { source: 'u1', target: 'c1', kind: 'hierarchical', dashed: false, thick: false },
    { source: 'c1', target: 'c2', kind: 'equivalent_to', dashed: false, thick: true },
  ],
  clusters: ['c1'],
}

describe('GraphView', () => {
  it('renders SVG with nodes and edges', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId={null} />)
    const svg = document.querySelector('svg')
    expect(svg).toBeInTheDocument()
    // 4 nodes as circles
    const circles = document.querySelectorAll('svg circle')
    expect(circles).toHaveLength(4)
  })

  it('renders node labels', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId={null} />)
    expect(screen.getByText('量化微调')).toBeInTheDocument()
    expect(screen.getByText('未落地卡')).toBeInTheDocument()
  })

  it('marks ungrounded nodes with dashed red border', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId={null} />)
    const circles = document.querySelectorAll('svg circle')
    const ungrounded = circles[3] // c2 is 4th
    expect(ungrounded.getAttribute('class')).toContain('ungrounded')
  })

  it('calls onSelectNode when a node is clicked', async () => {
    const onSelect = vi.fn()
    render(<GraphView layout={layout} onSelectNode={onSelect} selectedId={null} />)
    const circles = document.querySelectorAll('svg circle')
    await userEvent.click(circles[2]) // c1
    expect(onSelect).toHaveBeenCalledWith('c1')
  })

  it('highlights selected node', () => {
    render(<GraphView layout={layout} onSelectNode={vi.fn()} selectedId="c1" />)
    const circles = document.querySelectorAll('svg circle')
    expect(circles[2].getAttribute('class')).toContain('selected')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/graphView.test.tsx`
Expected: FAIL — `Failed to resolve import "./graphView"`

- [ ] **Step 3: Implement GraphView**

Create `frontend/src/console/knowledge/graphView.tsx`:

```tsx
import { useState, useCallback } from 'react'
import type { GraphLayout, GraphNode } from './graphLayout'

interface Props {
  layout: GraphLayout
  onSelectNode: (id: string) => void
  selectedId: string | null
}

const CLUSTER_COLORS = [
  '#0a84ff', '#30d158', '#ff9f0a', '#bf5af2',
  '#ff453a', '#64d2ff', '#ffd60a', '#5e5ce6',
]

export function GraphView({ layout, onSelectNode, selectedId }: Props) {
  const [zoom, setZoom] = useState(1)

  const minX = Math.min(...layout.nodes.map((n) => n.x)) - 60
  const minY = Math.min(...layout.nodes.map((n) => n.y)) - 60
  const maxX = Math.max(...layout.nodes.map((n) => n.x)) + 60
  const maxY = Math.max(...layout.nodes.map((n) => n.y)) + 60
  const width = maxX - minX
  const height = maxY - minY

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    setZoom((z) => Math.max(0.3, Math.min(3, z - e.deltaY * 0.001)))
  }, [])

  return (
    <div className="graph-canvas" onWheel={handleWheel}>
      <div className="graph-controls">
        <button className="btn ghost" onClick={() => setZoom((z) => Math.min(3, z + 0.2))}>+</button>
        <button className="btn ghost" onClick={() => setZoom((z) => Math.max(0.3, z - 0.2))}>−</button>
        <button className="btn ghost" onClick={() => setZoom(1)}>复位</button>
      </div>
      <svg
        width="100%"
        height="100%"
        viewBox={`${minX} ${minY} ${width} ${height}`}
        style={{ transform: `scale(${zoom})`, transformOrigin: 'center' }}
      >
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="8" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--text-muted)" />
          </marker>
          <marker id="arrow-thick" markerWidth="8" markerHeight="8" refX="8" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--accent-500)" />
          </marker>
        </defs>
        {layout.edges.map((edge, i) => {
          const source = layout.nodes.find((n) => n.id === edge.source)
          const target = layout.nodes.find((n) => n.id === edge.target)
          if (!source || !target) return null
          const strokeClass = edge.thick ? 'edge-thick' : edge.dashed ? 'edge-dashed' : 'edge-solid'
          return (
            <line
              key={`edge-${i}`}
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              className={`graph-edge ${strokeClass}`}
              markerEnd={edge.kind === 'hierarchical' || edge.kind === 'specializes' || edge.kind === 'requires' || edge.kind === 'component_of' ? 'url(#arrow)' : undefined}
            />
          )
        })}
        {layout.nodes.map((node) => {
          const color = node.type === 'card' && node.clusterIndex >= 0
            ? CLUSTER_COLORS[node.clusterIndex % CLUSTER_COLORS.length]
            : 'var(--surface)'
          const classes = [
            'graph-node',
            `node-${node.type}`,
            node.grounded ? '' : 'ungrounded',
            selectedId === node.id ? 'selected' : '',
          ].filter(Boolean).join(' ')
          return (
            <g key={node.id} onClick={() => onSelectNode(node.id)} className="node-group">
              {node.type === 'card' && node.clusterIndex >= 0 && (
                <circle cx={node.x} cy={node.y} r={node.r + 8} fill={color} opacity={0.12} className="cluster-halo" />
              )}
              <circle
                cx={node.x}
                cy={node.y}
                r={node.r}
                className={classes}
                fill={node.type === 'card' ? color : 'var(--surface)'}
                stroke={node.type === 'domain' ? 'var(--accent-500)' : 'var(--border)'}
                strokeWidth={2}
              />
              <text
                x={node.x}
                y={node.y + node.r + 14}
                textAnchor="middle"
                className="node-label"
              >
                {node.label}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/graphView.test.tsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Add graph CSS classes to styles.css**

Append to `frontend/src/styles.css`:

```css
/* ── 知识目录图谱 ── */
.graph-canvas { position: relative; width: 100%; height: 520px; background: var(--bg); border-radius: 16px; overflow: hidden; cursor: grab; }
.graph-canvas:active { cursor: grabbing; }
.graph-controls { position: absolute; top: 12px; right: 12px; display: flex; gap: 4px; z-index: 2; }
.graph-edge { stroke: var(--text-muted); opacity: 0.4; }
.edge-solid { stroke-width: 1.5; }
.edge-dashed { stroke-dasharray: 5,4; stroke-width: 1.5; }
.edge-thick { stroke: var(--accent-500); stroke-width: 3; opacity: 0.6; }
.graph-node { cursor: pointer; transition: r 0.2s, opacity 0.2s; }
.graph-node:hover { opacity: 0.8; }
.graph-node.selected { stroke: var(--accent-500); stroke-width: 3; }
.graph-node.ungrounded { stroke: var(--error); stroke-dasharray: 4,3; stroke-width: 2; }
.node-domain { fill: var(--surface); }
.node-unit { fill: var(--surface); }
.cluster-halo { pointer-events: none; }
.node-label { fill: var(--text-secondary); font-size: 11px; user-select: none; pointer-events: none; }
.node-group { cursor: pointer; }

/* ── 知识目录树视图 ── */
.ktree-search { margin-bottom: 12px; width: 100%; padding: 8px 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; color: var(--text); font-size: 13px; }
.ktree-search:focus { border-color: var(--accent-500); outline: none; }
.ktree-node { padding: 6px 12px; cursor: pointer; border-radius: 8px; transition: background 0.15s; }
.ktree-node:hover { background: var(--surface-subtle); }
.ktree-node.selected { background: var(--accent-subtle); }
.ktree-children { margin-left: 20px; border-left: 1px solid var(--border); padding-left: 8px; }
.ktree-badge { display: inline-flex; align-items: center; gap: 2px; font-size: 10px; margin-left: 8px; }
.ktree-badge.grounded { color: var(--success); }
.ktree-badge.ungrounded { color: var(--error); }

/* ── 详情抽屉 ── */
.detail-drawer { width: 360px; border-left: 1px solid var(--border); background: var(--surface-subtle); overflow-y: auto; padding: 20px; }
.detail-section { margin-bottom: 20px; }
.detail-section h4 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: 8px; }
.tag-row { display: flex; flex-wrap: wrap; gap: 6px; }
.tag { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; background: var(--accent-subtle); color: var(--accent-text); }
.atom-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; font-size: 13px; color: var(--text-secondary); border-bottom: 1px solid var(--border); }
.atom-row .grounded-mark { font-size: 14px; }
.atom-row .grounded-mark.yes { color: var(--success); }
.atom-row .grounded-mark.no { color: var(--error); }

/* ── 双视图切换 ── */
.view-toggle { display: inline-flex; background: var(--surface); border-radius: 10px; padding: 3px; gap: 2px; }
.view-toggle button { padding: 6px 16px; border-radius: 8px; font-size: 13px; color: var(--text-secondary); background: transparent; border: none; cursor: pointer; transition: all 0.2s; }
.view-toggle button.active { background: var(--accent-500); color: white; }
.catalog-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.catalog-main { display: flex; gap: 0; min-height: 520px; }
.catalog-canvas { flex: 1; position: relative; }
.catalog-empty { display: flex; align-items: center; justify-content: center; height: 520px; color: var(--text-muted); }
```

- [ ] **Step 6: Run full test suite + build**

Run: `npx vitest run` then `npm run build`
Expected: ALL PASS, build clean

- [ ] **Step 7: Commit**

```bash
git add frontend/src/console/knowledge/graphView.tsx frontend/src/console/knowledge/graphView.test.tsx frontend/src/styles.css
git commit -m "feat(knowledge): add GraphView SVG component with zoom, cluster colors, ungrounded marking"
```

---

## Task 7: 前端 — 增强树视图

**Files:**
- Create: `frontend/src/console/knowledge/treeView.tsx`
- Test: `frontend/src/console/knowledge/treeView.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/treeView.test.tsx`:

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TreeView } from './treeView'
import type { PublishedKnowledge, PublishedCard } from '../types'

const card = (name: string, grounded = true): PublishedCard => ({
  name, performance_statement: 'ps', assessable_content: ['f1'], scope_boundary: {},
  cognitive_targets: [], allowed_question_types: [], importance: 3, concept_cluster: 'c1',
  answer_proposition: 'prop', answer_boundary: 'prop', prompt_material: [], relation_edges: [], grounded,
})

const data: PublishedKnowledge = {
  catalog_version_id: 'cat1', framework_version_id: 'fw1',
  exam_points: [{ id: 'ep1', code: 'EP1', title: '量化微调', assessment_requirement: '', anchor_key: 'k1', weight_value: 0.5, weight_source: 'manual', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
  units: [{ unit_id: 'u1', code: 'U1', title: 'Unit 1', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP1', anchor_key: 'k1', card_ids: ['c1', 'c2'] }],
  knowledge_cards: { c1: card('量化微调', true), c2: card('未落地卡', false) },
}

describe('TreeView', () => {
  it('renders hierarchical tree with exam point and units', () => {
    render(<TreeView data={data} onSelectCard={vi.fn()} selectedId={null} />)
    expect(screen.getByText('EP1')).toBeInTheDocument()
    expect(screen.getByText('量化微调')).toBeInTheDocument()
    expect(screen.getByText('未落地卡')).toBeInTheDocument()
  })

  it('shows grounded badge for grounded cards', () => {
    render(<TreeView data={data} onSelectCard={vi.fn()} selectedId={null} />)
    const badges = screen.getAllByText(/●/)
    expect(badges.length).toBeGreaterThanOrEqual(2)
  })

  it('marks ungrounded cards with red badge', () => {
    render(<TreeView data={data} onSelectCard={vi.fn()} selectedId={null} />)
    const ungrounded = screen.getByText('未落地卡')
    const row = ungrounded.closest('.ktree-node')
    expect(row?.querySelector('.ungrounded')).toBeTruthy()
  })

  it('filters cards by search term', async () => {
    const user = userEvent.setup()
    render(<TreeView data={data} onSelectCard={vi.fn()} selectedId={null} />)
    const input = screen.getByPlaceholderText('搜索知识卡…')
    await user.type(input, '量化')
    expect(screen.getByText('量化微调')).toBeInTheDocument()
    expect(screen.queryByText('未落地卡')).not.toBeInTheDocument()
  })

  it('calls onSelectCard when a card is clicked', async () => {
    const onSelect = vi.fn()
    const user = userEvent.setup()
    render(<TreeView data={data} onSelectCard={onSelect} selectedId={null} />)
    await user.click(screen.getByText('量化微调'))
    expect(onSelect).toHaveBeenCalledWith('c1')
  })

  it('highlights selected card', () => {
    render(<TreeView data={data} onSelectCard={vi.fn()} selectedId="c1" />)
    const cardEl = screen.getByText('量化微调').closest('.ktree-node')
    expect(cardEl?.classList.contains('selected')).toBe(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/treeView.test.tsx`
Expected: FAIL — `Failed to resolve import "./treeView"`

- [ ] **Step 3: Implement TreeView**

Create `frontend/src/console/knowledge/treeView.tsx`:

```tsx
import { useState, useMemo } from 'react'
import type { PublishedKnowledge } from '../types'

interface Props {
  data: PublishedKnowledge
  onSelectCard: (id: string) => void
  selectedId: string | null
}

export function TreeView({ data, onSelectCard, selectedId }: Props) {
  const [query, setQuery] = useState('')
  const cards = data.knowledge_cards

  const filtered = useMemo(() => {
    if (!query.trim()) return data.exam_points
    const q = query.toLowerCase()
    return data.exam_points.filter((ep) => {
      const units = data.units.filter((u) => u.exam_point_id === ep.id)
      return units.some((u) =>
        u.card_ids.some((cid) => cards[cid]?.name.toLowerCase().includes(q)),
      )
    })
  }, [query, data])

  return (
    <div className="tree-view">
      <input
        className="ktree-search"
        placeholder="搜索知识卡…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="ktree-root">
        {filtered.map((ep) => {
          const units = data.units.filter((u) => u.exam_point_id === ep.id)
          return (
            <div className="ktree-node domain" key={ep.id}>
              <div className="ktree-node-head">
                <span className="code">{ep.code}</span>
                <span>{ep.title}</span>
              </div>
              <div className="ktree-children">
                {units.map((u) => (
                  <div className="ktree-node unit" key={u.unit_id}>
                    <div className="ktree-node-head">
                      <b>{u.code}</b>
                      <span className="muted small">{u.title}</span>
                    </div>
                    <div className="ktree-children">
                      {u.card_ids.map((cid) => {
                        const c = cards[cid]
                        if (!c) return null
                        if (query.trim()) {
                          const q = query.toLowerCase()
                          if (!c.name.toLowerCase().includes(q)) return null
                        }
                        return (
                          <div
                            className={`ktree-node card ${selectedId === cid ? 'selected' : ''}`}
                            key={cid}
                            onClick={() => onSelectCard(cid)}
                          >
                            <span>{c.name}</span>
                            <span className={`ktree-badge ${c.grounded ? 'grounded' : 'ungrounded'}`}>
                              {c.grounded ? '●' : '●'} {c.assessable_content.length} 原子
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/treeView.test.tsx`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/console/knowledge/treeView.tsx frontend/src/console/knowledge/treeView.test.tsx
git commit -m "feat(knowledge): add TreeView with search, ungrounded badges, selection highlight"
```

---

## Task 8: 前端 — 详情抽屉

**Files:**
- Create: `frontend/src/console/knowledge/detailDrawer.tsx`
- Test: `frontend/src/console/knowledge/detailDrawer.test.tsx`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/detailDrawer.test.tsx`:

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DetailDrawer } from './detailDrawer'
import type { PublishedCard, EvidenceLink, RelationEdge } from '../types'

const card: PublishedCard = {
  name: '量化微调簇',
  performance_statement: '能执行 QLoRA 量化',
  assessable_content: ['QLoRA 4bit 量化', '梯度累加'],
  scope_boundary: { limit: '单卡' },
  cognitive_targets: ['应用'],
  allowed_question_types: ['short_answer', 'multiple_choice'],
  importance: 5,
  concept_cluster: '量化技术',
  answer_proposition: 'QLoRA 是 4bit 量化方法',
  answer_boundary: 'QLoRA 是 4bit 量化方法',
  prompt_material: [],
  relation_edges: [{ kind: 'equivalent_to', target: 'GPTQ 量化' }],
  grounded: true,
}

const evidence: EvidenceLink[] = [
  { evidence_role: 'direct', confidence: 0.9, content: 'QLoRA 论文摘录…', locator: { page: 3 }, material_version_id: 'mv1' },
  { evidence_role: 'supporting', confidence: 0.7, content: '量化综述…', locator: { page: 12 }, material_version_id: 'mv2' },
]

describe('DetailDrawer', () => {
  it('renders card name and importance stars', () => {
    render(<DetailDrawer card={card} evidence={evidence} loading={false} onJumpRelation={vi.fn()} />)
    expect(screen.getByText('量化微调簇')).toBeInTheDocument()
    expect(screen.getByText('★★★★★')).toBeInTheDocument()
  })

  it('renders concept cluster as a tag', () => {
    render(<DetailDrawer card={card} evidence={evidence} loading={false} onJumpRelation={vi.fn()} />)
    expect(screen.getByText('量化技术')).toBeInTheDocument()
  })

  it('renders assessable atoms with grounded marks', () => {
    render(<DetailDrawer card={card} evidence={evidence} loading={false} onJumpRelation={vi.fn()} />)
    expect(screen.getByText('QLoRA 4bit 量化')).toBeInTheDocument()
    expect(screen.getByText('梯度累加')).toBeInTheDocument()
  })

  it('renders evidence list with role and confidence', () => {
    render(<DetailDrawer card={card} evidence={evidence} loading={false} onJumpRelation={vi.fn()} />)
    expect(screen.getByText('direct')).toBeInTheDocument()
    expect(screen.getByText('supporting')).toBeInTheDocument()
    expect(screen.getByText(/0.9/)).toBeInTheDocument()
  })

  it('renders relation edges as clickable chips', () => {
    const onJump = vi.fn()
    render(<DetailDrawer card={card} evidence={evidence} loading={false} onJumpRelation={onJump} />)
    const chip = screen.getByText(/GPTQ 量化/)
    chip.click()
    expect(onJump).toHaveBeenCalledWith('GPTQ 量化')
  })

  it('shows loading state', () => {
    render(<DetailDrawer card={card} evidence={[]} loading={true} onJumpRelation={vi.fn()} />)
    expect(screen.getByText('加载证据…')).toBeInTheDocument()
  })

  it('renders empty state when no card', () => {
    render(<DetailDrawer card={null} evidence={[]} loading={false} onJumpRelation={vi.fn()} />)
    expect(screen.getByText('选择知识卡查看详情')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/detailDrawer.test.tsx`
Expected: FAIL — `Failed to resolve import "./detailDrawer"`

- [ ] **Step 3: Implement DetailDrawer**

Create `frontend/src/console/knowledge/detailDrawer.tsx`:

```tsx
import type { PublishedCard, EvidenceLink } from '../types'

interface Props {
  card: PublishedCard | null
  evidence: EvidenceLink[]
  loading: boolean
  onJumpRelation: (target: string) => void
}

const EDGE_LABELS: Record<string, string> = {
  equivalent_to: '等价',
  specializes: '特化',
  component_of: '组成部分',
  contrasts_with: '对比',
  summarizes: '概括',
  requires: '依赖',
}

export function DetailDrawer({ card, evidence, loading, onJumpRelation }: Props) {
  if (!card) {
    return <div className="detail-drawer"><div className="catalog-empty">选择知识卡查看详情</div></div>
  }

  const stars = '★'.repeat(card.importance) + '☆'.repeat(5 - card.importance)

  return (
    <div className="detail-drawer">
      <div className="detail-section">
        <h3>{card.name}</h3>
        <div className="muted small">{stars}</div>
      </div>

      <div className="detail-section">
        <h4>画像</h4>
        <div className="tag-row">
          <span className="tag">{card.concept_cluster}</span>
          {card.cognitive_targets.map((t) => (
            <span className="tag" key={t}>{t}</span>
          ))}
          {card.allowed_question_types.map((t) => (
            <span className="tag" key={t}>{t}</span>
          ))}
        </div>
      </div>

      <div className="detail-section">
        <h4>答案命题</h4>
        <p className="small">{card.answer_proposition}</p>
      </div>

      <div className="detail-section">
        <h4>可评原子 ({card.assessable_content.length})</h4>
        {card.assessable_content.map((atom, i) => (
          <div className="atom-row" key={i}>
            <span className={`grounded-mark ${card.grounded ? 'yes' : 'no'}`}>{card.grounded ? '✓' : '✕'}</span>
            <span>{atom}</span>
          </div>
        ))}
      </div>

      <div className="detail-section">
        <h4>证据链 ({evidence.length})</h4>
        {loading ? (
          <div className="muted small">加载证据…</div>
        ) : evidence.length === 0 ? (
          <div className="muted small">无证据</div>
        ) : (
          evidence.map((ev, i) => (
            <div className="atom-row" key={i}>
              <span className="tag" style={{ background: ev.evidence_role === 'direct' ? 'var(--success-subtle)' : 'var(--warning-subtle)' }}>
                {ev.evidence_role}
              </span>
              <span className="small muted">{ev.content.slice(0, 60)}…</span>
              <span className="muted small">conf={ev.confidence}</span>
            </div>
          ))
        )}
      </div>

      {card.relation_edges.length > 0 && (
        <div className="detail-section">
          <h4>关系</h4>
          <div className="tag-row">
            {card.relation_edges.map((edge, i) => (
              <button
                className="tag"
                key={i}
                onClick={() => onJumpRelation(edge.target)}
                style={{ cursor: 'pointer', border: 'none' }}
              >
                {EDGE_LABELS[edge.kind] ?? edge.kind} → {edge.target}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/detailDrawer.test.tsx`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/console/knowledge/detailDrawer.tsx frontend/src/console/knowledge/detailDrawer.test.tsx
git commit -m "feat(knowledge): add DetailDrawer with card profile, atoms, evidence chain, relation chips"
```

---

## Task 9: 前端 — 双视图壳 + 装配替换 PublishedTreeBrowse

**Files:**
- Create: `frontend/src/console/knowledge/knowledgeCatalog.tsx`
- Test: `frontend/src/console/knowledge/knowledgeCatalog.test.tsx`
- Modify: `frontend/src/App.tsx:133`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/console/knowledge/knowledgeCatalog.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../client', () => ({
  knowledgeApi: {
    getPublished: vi.fn(),
    getEvidence: vi.fn(),
  },
}))

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { knowledgeApi } from '../client'
import { KnowledgeCatalog } from './knowledgeCatalog'
import type { PublishedKnowledge } from '../types'

const data: PublishedKnowledge = {
  catalog_version_id: 'cat1', framework_version_id: 'fw1',
  exam_points: [{ id: 'ep1', code: 'EP1', title: '量化微调', assessment_requirement: '', anchor_key: 'k1', weight_value: 0.5, weight_source: 'manual', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
  units: [{ unit_id: 'u1', code: 'U1', title: 'Unit 1', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP1', anchor_key: 'k1', card_ids: ['c1'] }],
  knowledge_cards: {
    c1: { name: '量化微调', performance_statement: 'ps', assessable_content: ['f1'], scope_boundary: {}, cognitive_targets: [], allowed_question_types: [], importance: 3, concept_cluster: 'c1', answer_proposition: 'prop', answer_boundary: 'prop', prompt_material: [], relation_edges: [], grounded: true },
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  ;(knowledgeApi.getEvidence as ReturnType<typeof vi.fn>).mockResolvedValue([])
})

describe('KnowledgeCatalog', () => {
  it('renders loading then content', async () => {
    const user = userEvent.setup()
    render(<KnowledgeCatalog courseId="c1" />)
    expect(screen.getByText('加载知识目录…')).toBeInTheDocument()
    const { waitFor } = await import('@testing-library/react')
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
  })

  it('defaults to tree view and shows toggle', async () => {
    const user = userEvent.setup()
    const { waitFor } = await import('@testing-library/react')
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    // toggle buttons exist
    expect(screen.getByText('图谱')).toBeInTheDocument()
    expect(screen.getByText('树')).toBeInTheDocument()
    // tree is active by default
    const treeBtn = screen.getByText('树')
    expect(treeBtn.closest('button')?.classList.contains('active')).toBe(true)
  })

  it('switches to graph view on toggle click', async () => {
    const user = userEvent.setup()
    const { waitFor } = await import('@testing-library/react')
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    await user.click(screen.getByText('图谱'))
    const graphBtn = screen.getByText('图谱')
    expect(graphBtn.closest('button')?.classList.contains('active')).toBe(true)
    // SVG should be rendered
    expect(document.querySelector('svg')).toBeInTheDocument()
  })

  it('shows detail drawer empty state initially', async () => {
    const user = userEvent.setup()
    const { waitFor } = await import('@testing-library/react')
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    expect(screen.getByText('选择知识卡查看详情')).toBeInTheDocument()
  })

  it('fetches evidence when a card is selected', async () => {
    const user = userEvent.setup()
    const { waitFor } = await import('@testing-library/react')
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    await user.click(screen.getByText('量化微调'))
    expect(knowledgeApi.getEvidence).toHaveBeenCalledWith('c1', 'c1')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/console/knowledge/knowledgeCatalog.test.tsx`
Expected: FAIL — `Failed to resolve import "./knowledgeCatalog"`

- [ ] **Step 3: Implement KnowledgeCatalog**

Create `frontend/src/console/knowledge/knowledgeCatalog.tsx`:

```tsx
import { useEffect, useState, useCallback } from 'react'
import { knowledgeApi } from '../client'
import { LoadingLine, EmptyState, Notice } from '../ui'
import { computeGraphLayout } from './graphLayout'
import { GraphView } from './graphView'
import { TreeView } from './treeView'
import { DetailDrawer } from './detailDrawer'
import type { PublishedKnowledge, PublishedCard, EvidenceLink } from '../types'

type ViewMode = 'graph' | 'tree'

export function KnowledgeCatalog({ courseId }: { courseId: string }) {
  const [data, setData] = useState<PublishedKnowledge | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [view, setView] = useState<ViewMode>('tree')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<EvidenceLink[]>([])
  const [evidenceLoading, setEvidenceLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setSelectedId(null)
    setEvidence([])
    knowledgeApi
      .getPublished(courseId)
      .then((d) => { if (!cancelled) setData(d) })
      .catch(() => { if (!cancelled) setError('尚未发布知识目录，请先完成知识整理。') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [courseId])

  const selectedCard: PublishedCard | null = selectedId && data ? data.knowledge_cards[selectedId] ?? null : null

  const handleSelect = useCallback(async (cardId: string) => {
    setSelectedId(cardId)
    setEvidenceLoading(true)
    setEvidence([])
    try {
      const ev = await knowledgeApi.getEvidence(courseId, cardId)
      setEvidence(ev)
    } catch {
      setEvidence([])
    } finally {
      setEvidenceLoading(false)
    }
  }, [courseId])

  const handleJumpRelation = useCallback((targetName: string) => {
    if (!data) return
    const entry = Object.entries(data.knowledge_cards).find(([, c]) => c.name === targetName)
    if (entry) handleSelect(entry[0])
  }, [data, handleSelect])

  if (loading) return <LoadingLine>加载知识目录…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>
  if (!data) return <EmptyState>无知识目录</EmptyState>

  const layout = computeGraphLayout(data)
  const cardCount = Object.keys(data.knowledge_cards).length
  const ungroundedCount = Object.values(data.knowledge_cards).filter((c) => !c.grounded).length

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>知识目录</h2>
        <div className="desc">
          {cardCount} 张知识卡 · {ungroundedCount} 未落地 · 基于 {data.framework_version_id.slice(0, 8)}
        </div>
      </div>

      <div className="catalog-toolbar">
        <div className="view-toggle">
          <button className={view === 'graph' ? 'active' : ''} onClick={() => setView('graph')}>图谱</button>
          <button className={view === 'tree' ? 'active' : ''} onClick={() => setView('tree')}>树</button>
        </div>
      </div>

      <div className="catalog-main">
        <div className="catalog-canvas">
          {view === 'graph' ? (
            <GraphView layout={layout} onSelectNode={handleSelect} selectedId={selectedId} />
          ) : (
            <TreeView data={data} onSelectCard={handleSelect} selectedId={selectedId} />
          )}
        </div>
        <DetailDrawer
          card={selectedCard}
          evidence={evidence}
          loading={evidenceLoading}
          onJumpRelation={handleJumpRelation}
        />
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/console/knowledge/knowledgeCatalog.test.tsx`
Expected: PASS (5 tests)

- [ ] **Step 5: Replace PublishedTreeBrowse in App.tsx**

In `frontend/src/App.tsx`, find the import of `PublishedTreeBrowse` (around line 8) and replace with:

```typescript
import { KnowledgeCatalog } from './console/knowledge/knowledgeCatalog'
```

Then in the knowledge section (around line 133), replace `<PublishedTreeBrowse courseId={route.course.id} />` with `<KnowledgeCatalog courseId={route.course.id} />`.

- [ ] **Step 6: Run full test suite + build**

Run: `npx vitest run` then `npm run build`
Expected: ALL PASS, build clean

- [ ] **Step 7: Commit**

```bash
git add frontend/src/console/knowledge/knowledgeCatalog.tsx frontend/src/console/knowledge/knowledgeCatalog.test.tsx frontend/src/App.tsx
git commit -m "feat(knowledge): add KnowledgeCatalog dual-view shell with graph/tree toggle and detail drawer"
```
