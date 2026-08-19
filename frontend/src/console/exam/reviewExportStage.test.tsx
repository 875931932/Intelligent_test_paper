import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ReviewExportStage } from './reviewExportStage'
import type { ExamProjectDetail, GenerationRunResult } from '../types'

const gen: GenerationRunResult = {
  status: 'completed',
  questions: [
    { item_index: 1, question_type: 'single_choice', score: 5, stem: 'QLoRA 量化精度？', answer: 0, needs_review: false },
    { item_index: 2, question_type: 'short_answer', score: 10, stem: '解释梯度累加', answer: '样例答案', needs_review: true },
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

// --- Module-level mocks for review stage
const mockGetCurrentPV = vi.fn()
const mockListNeedsReview = vi.fn()
const mockPatchPaperItem = vi.fn()
const mockConfirmPV = vi.fn()
const mockRevertPV = vi.fn()

vi.mock('../client', async () => {
  const actual: any = await vi.importActual('../client')
  return {
    ...actual,
    examPipelineApi: {
      ...(actual.examPipelineApi ?? {}),
      getCurrentPaperVersion: (...a: unknown[]) => mockGetCurrentPV(...a),
      listNeedsReview: (...a: unknown[]) => mockListNeedsReview(...a),
      patchPaperItem: (...a: unknown[]) => mockPatchPaperItem(...a),
      confirmPaperVersion: (...a: unknown[]) => mockConfirmPV(...a),
      revertPaperVersion: (...a: unknown[]) => mockRevertPV(...a),
    },
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  mockGetCurrentPV.mockResolvedValue({
    id: 'pv-test',
    status: 'candidate',
    items: [
      { item_index: 1, question_type: 'single_choice', score: 5, stem: 'S1', needs_review: false },
      { item_index: 2, question_type: 'short_answer', score: 10, stem: 'S2', needs_review: true },
    ],
  })
  mockListNeedsReview.mockResolvedValue([
    { item_index: 2, question_type: 'short_answer', reason: 'R2', quality_message: 'quality2' },
    { item_index: 3, question_type: 'comprehensive', reason: 'R3', quality_message: 'quality3' },
  ])
  mockPatchPaperItem.mockImplementation((_c: string, _p: string, _i: number, b: unknown) =>
    Promise.resolve(b),
  )
  mockConfirmPV.mockResolvedValue({ status: 'finalized', unresolved: 0 })
  mockRevertPV.mockResolvedValue({})
})

describe('ReviewExportStage (TR-8.3)', () => {
  it('renders review summary', async () => {
    render(<ReviewExportStage courseId="c1" project={project} onExport={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('审核与导出')).toBeInTheDocument())
  })

  it('listNeedsReview returns 2 items; both rendered in priority list', async () => {
    render(<ReviewExportStage courseId="c1" project={project} onExport={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('needs-review-list')).toBeInTheDocument())
    expect(screen.getByTestId('review-item-2')).toBeInTheDocument()
    expect(screen.getByTestId('review-item-3')).toBeInTheDocument()
  })

  it('click "标记已审阅" on item 1 → PATCH with clear_needs_review:true, badge hidden on rerender', async () => {
    const user = userEvent.setup()
    render(<ReviewExportStage courseId="c1" project={project} onExport={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('r-badge-2')).toBeInTheDocument())
    await user.click(screen.getByTestId('clear-review-2'))
    await waitFor(() => expect(mockPatchPaperItem).toHaveBeenCalled())
    const [, pvId, itemIndex, body] = mockPatchPaperItem.mock.calls[0] as [
      string, string, number, { teacher_override_patch: unknown; clear_needs_review?: boolean }
    ]
    expect(pvId).toBe('pv-test')
    expect(itemIndex).toBe(2)
    expect(body.clear_needs_review).toBe(true)
    // Rerender should hide the needs_review badge
    await waitFor(() => expect(screen.queryByTestId('r-badge-2')).not.toBeInTheDocument())
  })
})

describe('ReviewExportStage existing behavior preserved', () => {
  it('shows export gate banner when not confirmed', async () => {
    render(<ReviewExportStage courseId="c1" project={project} onExport={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('confirm-final-btn')).toBeInTheDocument())
  })

  it('shows verified banner when version confirmed', async () => {
    const confirmed = { ...project, version_confirmed: true, status: 'exported' as const }
    render(<ReviewExportStage courseId="c1" project={confirmed} onExport={vi.fn()} />)
    await waitFor(() => expect(screen.getByText(/已导出/)).toBeInTheDocument())
  })

  it('calls onExport when confirm succeeds', async () => {
    const onExport = vi.fn()
    const user = userEvent.setup()
    render(<ReviewExportStage courseId="c1" project={project} onExport={onExport} />)
    await waitFor(() => expect(screen.getByTestId('confirm-final-btn')).toBeInTheDocument())
    await user.click(screen.getByTestId('confirm-final-btn'))
    await waitFor(() => expect(mockConfirmPV).toHaveBeenCalled())
    expect(onExport).toHaveBeenCalledWith('p1')
  })
})
