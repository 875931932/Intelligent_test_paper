import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ReviewExportStage } from './reviewExportStage'
import type { ExamProjectDetail, GenerationRunResult } from '../types'

const gen: GenerationRunResult = {
  status: 'completed',
  questions: [
    { item_index: 1, question_type: 'single_choice', score: 5, stem: 'QLoRA 量化精度？', answer: 0, needs_review: false },
    { item_index: 2, question_type: 'short_answer', score: 10, stem: '解释梯度累加', needs_review: true },
  ],
  final_check: { passed: true },
  model_call_count: 2,
  model: 'gpt-4',
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'review',
  total_score: 100, question_count: 2, pending_review: 1,
  blueprint_confirmed: true, version_confirmed: false,
  generation: gen,
}

describe('ReviewExportStage', () => {
  it('renders review summary', () => {
    render(<ReviewExportStage project={project} onExport={vi.fn()} />)
    expect(screen.getByText('审核与导出')).toBeInTheDocument()
  })

  it('shows pending review questions with warn tag', () => {
    render(<ReviewExportStage project={project} onExport={vi.fn()} />)
    const warnTags = screen.getAllByText('待审')
    expect(warnTags).toHaveLength(1)
  })

  it('shows export gate banner when not confirmed', () => {
    render(<ReviewExportStage project={project} onExport={vi.fn()} />)
    expect(screen.getByText('确认导出')).toBeInTheDocument()
  })

  it('shows verified banner when version confirmed', () => {
    const confirmed = { ...project, version_confirmed: true, status: 'exported' as const }
    render(<ReviewExportStage project={confirmed} onExport={vi.fn()} />)
    expect(screen.getByText(/已导出/)).toBeInTheDocument()
  })

  it('calls onExport when confirm button clicked', async () => {
    const onExport = vi.fn()
    const user = userEvent.setup()
    render(<ReviewExportStage project={project} onExport={onExport} />)
    await user.click(screen.getByText('确认导出'))
    expect(onExport).toHaveBeenCalledWith('p1')
  })
})
