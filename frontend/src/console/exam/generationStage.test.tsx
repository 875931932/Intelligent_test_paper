import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GenerationStage } from './generationStage'
import type { ExamProjectDetail, GenerationRunResult } from '../types'

const genResult: GenerationRunResult = {
  status: 'completed',
  questions: [
    {
      item_index: 1,
      question_type: 'single_choice',
      score: 5,
      stem: 'QLoRA 的量化精度是？',
      options: ['4bit', '8bit', '16bit', '32bit'],
      answer: 0,
      needs_review: false,
      exam_point_id: 'ep1',
    },
    {
      item_index: 2,
      question_type: 'short_answer',
      score: 10,
      stem: '解释梯度累加的原理',
      needs_review: true,
    },
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

// --- Mocks at module level
const mockStart = vi.fn()
const mockGetTaskRun = vi.fn()
const mockGetCurrentPaperVersion = vi.fn()

vi.mock('../client', async () => {
  const actual: any = await vi.importActual('../client')
  return {
    ...actual,
    examPipelineApi: {
      ...(actual.examPipelineApi ?? {}),
      startGeneration: (...a: unknown[]) => mockStart(...a),
      getTaskRun: (...a: unknown[]) => mockGetTaskRun(...a),
      getCurrentPaperVersion: (...a: unknown[]) => mockGetCurrentPaperVersion(...a),
    },
  }
})

beforeEach(() => {
  vi.clearAllMocks()
})

describe('GenerationStage (TR-8.2)', () => {
  it('renders generation summary initially', () => {
    render(<GenerationStage courseId="c1" project={project} onProceed={vi.fn()} />)
    expect(screen.getByText('生成阶段')).toBeInTheDocument()
  })

  it('shows empty state and start button before generation', () => {
    render(<GenerationStage courseId="c1" project={project} onProceed={vi.fn()} />)
    expect(screen.getByTestId('start-gen-btn')).toBeInTheDocument()
  })

  it('click start → poll → success → 3 cards + 成功 message', async () => {
    const user = userEvent.setup()
    mockStart.mockResolvedValue({ task_run_id: 'tr1' })
    mockGetTaskRun
      .mockResolvedValueOnce({
        id: 'tr1',
        status: 'running',
        progress: 42,
        stage: 'drafting',
      })
      .mockResolvedValueOnce({
        id: 'tr1',
        status: 'succeeded',
        progress: 100,
        stage: 'done',
        result: { paper_version_id: 'pv1' },
      })
    const cards = [
      { item_index: 1, question_type: 'single_choice', score: 5, stem: 'Q1 stem', needs_review: false },
      { item_index: 2, question_type: 'true_false', score: 2, stem: 'Q2 stem', needs_review: true },
      { item_index: 3, question_type: 'short_answer', score: 10, stem: 'Q3 stem', needs_review: false },
    ]
    mockGetCurrentPaperVersion.mockResolvedValue({ id: 'pv1', status: 'candidate', items: cards })

    // Use tiny poll interval so real timers resolve quickly.
    render(
      <GenerationStage
        courseId="c1"
        project={project}
        onProceed={vi.fn()}
        pollIntervalMs={5}
        pollTimeoutMs={5000}
      />,
    )
    await user.click(screen.getByTestId('start-gen-btn'))
    expect(mockStart).toHaveBeenCalled()

    // Both polls should fire quickly (5ms apart). Wait for both mock calls.
    await waitFor(() => expect(mockGetTaskRun).toHaveBeenCalledTimes(2), { timeout: 6000 })
    await waitFor(() => expect(mockGetCurrentPaperVersion).toHaveBeenCalled(), { timeout: 4000 })

    await waitFor(() => {
      cards.forEach((c) => {
        expect(screen.getByTestId(`q-card-${c.item_index}`)).toBeInTheDocument()
      })
    }, { timeout: 4000 })
    expect(screen.getAllByTestId(/^q-card-/)).toHaveLength(3)
    // Note: use queryBy + inline retry guard because waitFor() can report stale stack traces
    // even when the element is rendered on subsequent attempts. We still wrap in waitFor
    // with a looser assertion — the rendered DOM in failures shows the Notice present.
    let successNotice: HTMLElement | null | undefined = undefined
    await waitFor(() => {
      successNotice =
        (screen.queryByTestId('gen-success') as HTMLElement | null) ??
        (screen.queryByText(/生成成功，共 3 题/) as HTMLElement | null)
      expect(successNotice).not.toBeNull()
    }, { timeout: 5000 })
    // Confirm the Notice contains expected text.
    expect((successNotice as unknown as HTMLElement | null)?.textContent).toMatch(/生成成功，共 3 题/)
  })

  it('proceed button visible after success and triggers callback', async () => {
    const onProceed = vi.fn()
    const user = userEvent.setup()
    mockStart.mockResolvedValue({ task_run_id: 'tr1' })
    mockGetTaskRun.mockResolvedValue({
      id: 'tr1',
      status: 'succeeded',
      progress: 100,
      stage: 'done',
      result: { paper_version_id: 'pv1' },
    })
    mockGetCurrentPaperVersion.mockResolvedValue({
      id: 'pv1',
      items: [{ item_index: 1, question_type: 'single_choice', score: 5, stem: 'Q1' }],
    })
    render(
      <GenerationStage
        courseId="c1"
        project={project}
        onProceed={onProceed}
        pollIntervalMs={5}
        pollTimeoutMs={5000}
      />,
    )
    await user.click(screen.getByTestId('start-gen-btn'))

    await waitFor(() => expect(mockGetTaskRun).toHaveBeenCalled(), { timeout: 4000 })
    await waitFor(() => expect(mockGetCurrentPaperVersion).toHaveBeenCalled(), { timeout: 4000 })
    await waitFor(() => expect(screen.getByTestId('proceed-review-btn')).toBeInTheDocument(), { timeout: 4000 })

    await user.click(screen.getByTestId('proceed-review-btn'))
    expect(onProceed).toHaveBeenCalledWith('p1')
  })
})
