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
