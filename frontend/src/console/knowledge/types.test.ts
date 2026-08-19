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
