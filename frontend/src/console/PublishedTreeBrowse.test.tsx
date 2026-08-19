import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/render'
import { PublishedTreeBrowse } from './PublishedTreeBrowse'

vi.mock('./client', () => ({
  knowledgeApi: {
    getPublished: vi.fn(),
  },
}))

import { knowledgeApi } from './client'

beforeEach(() => vi.clearAllMocks())

describe('PublishedTreeBrowse', () => {
  it('renders published knowledge tree', async () => {
    ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockResolvedValue({
      catalog_version_id: 'cat-1',
      framework_version_id: 'fw-1',
      exam_points: [{ id: 'ep1', code: 'EP2', title: '参数高效微调', assessment_requirement: '', anchor_key: 'a', weight_value: 25, weight_source: '', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
      units: [{ unit_id: 'u1', code: 'AU03', title: '参数高效微调', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP2', anchor_key: 'a', card_ids: ['k1'] }],
      knowledge_cards: {
        k1: { name: 'QLoRA 量化微调', performance_statement: '', assessable_content: ['NF4 量化', '双量化'], scope_boundary: {}, cognitive_targets: [], allowed_question_types: [], importance: 4, concept_cluster: '量化微调簇', answer_proposition: '', answer_boundary: '', prompt_material: [] },
      },
    })
    renderWithProviders(<PublishedTreeBrowse courseId="c1" />)
    await waitFor(() => expect(screen.getByText('QLoRA 量化微调')).toBeInTheDocument())
    expect(screen.getByText(/2 原子/)).toBeInTheDocument()
    expect(screen.getByText('量化微调簇')).toBeInTheDocument()
  })

  it('shows warning when knowledge not published', async () => {
    ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('not found'))
    renderWithProviders(<PublishedTreeBrowse courseId="c1" />)
    await waitFor(() => expect(screen.getByText(/尚未发布知识目录/)).toBeInTheDocument())
  })
})
