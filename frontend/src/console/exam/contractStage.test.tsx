import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ContractStage } from './contractStage'
import type { ExamProjectDetail, PaperContract } from '../types'

const contract: PaperContract = {
  total_score: 100,
  slots: [
    { item_index: 1, question_type: 'single_choice', score: 5, difficulty: 'medium', cognitive_level: '应用', assessment_mode: 'conceptual', exam_point_id: 'ep1', anchor_key: 'k1', unit_id: 'u1', card_id: 'c1', coverage_atom: '原子1', answer_boundary: 'b1', performance_statement: 'ps1', prompt_material: [], scope_boundary: {}, preferred_terms: [], forbidden_context: { atoms: [], answer_cores: [] }, cognitive_sequence: [], subquestion_actions: [], answer_boundaries: [] },
  ],
  conflicts: [
    { code: 'duplicate_atom', exam_point_id: 'ep1', message: '原子重复', detail: {} },
  ],
  audit_summary: { exam_points: [], type_counts: { single_choice: 1 }, difficulty_counts: { medium: 1 } },
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'contract',
  total_score: 100, question_count: 1, pending_review: 0,
  blueprint_confirmed: true, version_confirmed: false,
  contract,
}

describe('ContractStage', () => {
  it('renders contract slots table', () => {
    render(<ContractStage project={project} onGenerate={vi.fn()} />)
    expect(screen.getByText('合同阶段')).toBeInTheDocument()
    expect(screen.getByText('原子1')).toBeInTheDocument()
    expect(screen.getByText('single_choice')).toBeInTheDocument()
  })

  it('renders conflicts list', () => {
    render(<ContractStage project={project} onGenerate={vi.fn()} />)
    expect(screen.getByText('原子重复')).toBeInTheDocument()
  })

  it('shows generate button', () => {
    render(<ContractStage project={project} onGenerate={vi.fn()} />)
    expect(screen.getByText('生成试卷')).toBeInTheDocument()
  })

  it('calls onGenerate when button clicked', async () => {
    const onGenerate = vi.fn()
    const user = userEvent.setup()
    render(<ContractStage project={project} onGenerate={onGenerate} />)
    await user.click(screen.getByText('生成试卷'))
    expect(onGenerate).toHaveBeenCalledWith('p1')
  })

  it('shows empty state when no contract', () => {
    const noContract = { ...project, contract: undefined }
    render(<ContractStage project={noContract} onGenerate={vi.fn()} />)
    expect(screen.getByText('合同未生成')).toBeInTheDocument()
  })
})
