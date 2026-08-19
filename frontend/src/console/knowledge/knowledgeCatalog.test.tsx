import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../client', () => ({
  knowledgeApi: {
    getPublished: vi.fn(),
    getEvidence: vi.fn(),
  },
}))

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { knowledgeApi } from '../client'
import { KnowledgeCatalog } from './knowledgeCatalog'
import type { PublishedKnowledge } from '../types'

const data: PublishedKnowledge = {
  catalog_version_id: 'cat1', framework_version_id: 'fw1',
  exam_points: [{ id: 'ep1', code: 'EP1', title: '量化微调考点', assessment_requirement: '', anchor_key: 'k1', weight_value: 0.5, weight_source: 'manual', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
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
    render(<KnowledgeCatalog courseId="c1" />)
    expect(screen.getByText('加载知识目录…')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
  })

  it('defaults to tree view and shows toggle', async () => {
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    expect(screen.getByText('图谱')).toBeInTheDocument()
    expect(screen.getByText('树')).toBeInTheDocument()
    const treeBtn = screen.getByText('树')
    expect(treeBtn.closest('button')?.classList.contains('active')).toBe(true)
  })

  it('switches to graph view on toggle click', async () => {
    const user = userEvent.setup()
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    await user.click(screen.getByText('图谱'))
    const graphBtn = screen.getByText('图谱')
    expect(graphBtn.closest('button')?.classList.contains('active')).toBe(true)
    expect(document.querySelector('svg')).toBeInTheDocument()
  })

  it('shows detail drawer empty state initially', async () => {
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    expect(screen.getByText('选择知识卡查看详情')).toBeInTheDocument()
  })

  it('fetches evidence when a card is selected', async () => {
    const user = userEvent.setup()
    render(<KnowledgeCatalog courseId="c1" />)
    await waitFor(() => expect(screen.getByText('知识目录')).toBeInTheDocument())
    await user.click(screen.getByText('量化微调'))
    expect(knowledgeApi.getEvidence).toHaveBeenCalledWith('c1', 'c1')
  })
})
