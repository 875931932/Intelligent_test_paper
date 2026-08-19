import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BlueprintStage } from './blueprintStage'
import type { ExamProjectDetail } from '../types'

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'blueprint',
  total_score: 100, question_count: 0, pending_review: 0,
  blueprint_confirmed: false, version_confirmed: false,
}

describe('BlueprintStage', () => {
  it('renders blueprint summary with total score', () => {
    render(<BlueprintStage project={project} onConfirm={vi.fn()} />)
    expect(screen.getByText('蓝图阶段')).toBeInTheDocument()
    expect(screen.getByText(/100/)).toBeInTheDocument()
  })

  it('shows gate banner when not confirmed', () => {
    render(<BlueprintStage project={project} onConfirm={vi.fn()} />)
    expect(screen.getByText(/确认蓝图/)).toBeInTheDocument()
  })

  it('shows verified banner when confirmed', () => {
    const confirmed = { ...project, blueprint_confirmed: true }
    render(<BlueprintStage project={confirmed} onConfirm={vi.fn()} />)
    const banner = screen.getByText(/蓝图已确认/).closest('.gate-banner')
    expect(banner?.classList.contains('verified')).toBe(true)
  })

  it('calls onConfirm when gate button clicked', async () => {
    const onConfirm = vi.fn()
    const user = userEvent.setup()
    render(<BlueprintStage project={project} onConfirm={onConfirm} />)
    await user.click(screen.getByText('确认蓝图'))
    expect(onConfirm).toHaveBeenCalledWith('p1')
  })
})
