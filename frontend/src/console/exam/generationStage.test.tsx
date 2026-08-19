import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GenerationStage } from './generationStage'
import type { ExamProjectDetail, GenerationRunResult } from '../types'

const genResult: GenerationRunResult = {
  status: 'completed',
  questions: [
    { item_index: 1, question_type: 'single_choice', score: 5, stem: 'QLoRA 的量化精度是？', options: ['4bit', '8bit', '16bit', '32bit'], answer: 0, explanation: 'QLoRA 使用 4bit 量化', needs_review: false, exam_point_id: 'ep1', coverage_atom: '原子1' },
    { item_index: 2, question_type: 'short_answer', score: 10, stem: '解释梯度累加的原理', needs_review: true, exam_point_id: 'ep1', coverage_atom: '原子2' },
  ],
  final_check: { passed: true, checks: [] },
  model_call_count: 2,
  model: 'gpt-4',
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'generating',
  total_score: 100, question_count: 2, pending_review: 1,
  blueprint_confirmed: true, version_confirmed: false,
  generation: genResult,
}

describe('GenerationStage', () => {
  it('renders generation summary', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('生成阶段')).toBeInTheDocument()
  })

  it('renders question preview cards', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('QLoRA 的量化精度是？')).toBeInTheDocument()
    expect(screen.getByText('解释梯度累加的原理')).toBeInTheDocument()
  })

  it('marks questions needing review with warn tag', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    const warnTags = screen.getAllByText('待审')
    expect(warnTags).toHaveLength(1)
  })

  it('shows proceed button when generation complete', () => {
    render(<GenerationStage project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('进入审核')).toBeInTheDocument()
  })

  it('calls onProceed when button clicked', async () => {
    const onProceed = vi.fn()
    const user = userEvent.setup()
    render(<GenerationStage project={project} onProceed={onProceed} />)
    await user.click(screen.getByText('进入审核'))
    expect(onProceed).toHaveBeenCalledWith('p1')
  })

  it('shows empty state when no generation', () => {
    const noGen = { ...project, generation: undefined }
    render(<GenerationStage project={noGen} onProceed={vi.fn()} />)
    expect(screen.getByText('尚未生成')).toBeInTheDocument()
  })
})
