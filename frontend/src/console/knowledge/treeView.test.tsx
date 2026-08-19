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
  exam_points: [{ id: 'ep1', code: 'EP1', title: '量化微调考点', assessment_requirement: '', anchor_key: 'k1', weight_value: 0.5, weight_source: 'manual', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
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
