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

  it('produces hierarchical edges (domain->unit->card)', () => {
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
