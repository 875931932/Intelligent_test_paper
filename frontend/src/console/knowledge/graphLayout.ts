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
const UNIT_R = 24
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
