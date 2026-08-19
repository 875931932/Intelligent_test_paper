import { describe, it, expect, vi, beforeEach } from 'vitest'

// Module-level mock for ../client covering projectsApi AND examPipelineApi
// (ExamProjectWorkspace renders stages that call examPipelineApi methods.)
const mockProjectsGet = vi.fn()
const mockProjectsUpdateStatus = vi.fn()

const mockExamCreateBP = vi.fn()
const mockExamGetPlanItems = vi.fn()
const mockExamConfirmBP = vi.fn()
const mockExamPatchPlanItem = vi.fn()

const mockExamAllocateContract = vi.fn()
const mockExamConfirmContract = vi.fn()
const mockExamReviseContract = vi.fn()

const mockExamStartGen = vi.fn()
const mockExamGetTaskRun = vi.fn()
const mockExamGetCurrentPV = vi.fn()

const mockExamListNeedsReview = vi.fn()
const mockExamPatchPaper = vi.fn()
const mockExamConfirmPV = vi.fn()
const mockExamRevertPV = vi.fn()

vi.mock('../client', async () => {
  const actual: any = await vi.importActual('../client')
  return {
    ...actual,
    projectsApi: {
      ...(actual.projectsApi ?? {}),
      list: vi.fn(),
      create: vi.fn(),
      get: (...a: unknown[]) => mockProjectsGet(...a),
      updateStatus: (...a: unknown[]) => mockProjectsUpdateStatus(...a),
    },
    examPipelineApi: {
      ...(actual.examPipelineApi ?? {}),
      createBlueprint: (...a: unknown[]) => mockExamCreateBP(...a),
      getPlanItems: (...a: unknown[]) => mockExamGetPlanItems(...a),
      patchPlanItem: (...a: unknown[]) => mockExamPatchPlanItem(...a),
      confirmBlueprint: (...a: unknown[]) => mockExamConfirmBP(...a),
      allocateContract: (...a: unknown[]) => mockExamAllocateContract(...a),
      reviseContract: (...a: unknown[]) => mockExamReviseContract(...a),
      confirmContract: (...a: unknown[]) => mockExamConfirmContract(...a),
      startGeneration: (...a: unknown[]) => mockExamStartGen(...a),
      getTaskRun: (...a: unknown[]) => mockExamGetTaskRun(...a),
      getCurrentPaperVersion: (...a: unknown[]) => mockExamGetCurrentPV(...a),
      listNeedsReview: (...a: unknown[]) => mockExamListNeedsReview(...a),
      patchPaperItem: (...a: unknown[]) => mockExamPatchPaper(...a),
      confirmPaperVersion: (...a: unknown[]) => mockExamConfirmPV(...a),
      revertPaperVersion: (...a: unknown[]) => mockExamRevertPV(...a),
    },
  }
})

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExamProjectWorkspace } from './examProjectWorkspace'
import type { ExamProjectDetail } from '../types'

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'blueprint',
  total_score: 100, question_count: 0, pending_review: 0,
  blueprint_confirmed: false, version_confirmed: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  mockProjectsGet.mockResolvedValue(project)
  mockProjectsUpdateStatus.mockImplementation((_c, _p, status) =>
    Promise.resolve({ ...project, status, blueprint_confirmed: true, active_paper_version_id: 'pv1' }),
  )
  mockExamGetPlanItems.mockResolvedValue([])
  mockExamConfirmBP.mockResolvedValue({ status: 'confirmed', blueprint_version_id: 'bv1' })
  mockExamCreateBP.mockResolvedValue({ blueprint_version_id: 'bv1', plan: [] })
  mockExamAllocateContract.mockResolvedValue({
    used_threshold: 0.8,
    conflicts_history: [],
    contract_snapshot: {
      slots: [
        { item_index: 1, question_type: 'single_choice', score: 5, card_id: 'c1', difficulty: 'medium', cognitive_level: '记忆' },
      ],
    },
  })
  mockExamConfirmContract.mockResolvedValue({
    generation_run_id: 'gr1',
    threshold: 0.8,
    slot_count: 37,
  })
  mockExamReviseContract.mockResolvedValue({ revised_contract_snapshot: { slots: [] } })
  mockExamStartGen.mockResolvedValue({ task_run_id: 'tr1' })
  mockExamGetTaskRun.mockResolvedValue({ id: 'tr1', status: 'succeeded', progress: 100, stage: 'done' })
  mockExamGetCurrentPV.mockResolvedValue({ id: 'pv1', items: [] })
  mockExamListNeedsReview.mockResolvedValue([])
  mockExamConfirmPV.mockResolvedValue({ status: 'finalized', unresolved: 0 })
  mockExamPatchPaper.mockResolvedValue({})
  mockExamRevertPV.mockResolvedValue({})
})

describe('ExamProjectWorkspace', () => {
  it('renders loading then pipeline nav', async () => {
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    expect(screen.getByText('加载项目…')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('蓝图')).toBeInTheDocument())
    expect(screen.getByText('合同')).toBeInTheDocument()
    expect(screen.getByText('生成')).toBeInTheDocument()
    expect(screen.getByText('审核')).toBeInTheDocument()
    expect(screen.getByText('导出')).toBeInTheDocument()
  })

  it('shows blueprint stage detail for blueprint status', async () => {
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByText('蓝图阶段')).toBeInTheDocument())
    expect(screen.getByTestId('confirm-blueprint-btn')).toBeInTheDocument()
  })

  it('TR-8.4.1: click confirm blueprint → calls blueprint confirm API, then refresh project', async () => {
    // Prefill a plan so confirm button isn't disabled
    mockExamGetPlanItems.mockResolvedValueOnce([
      { id: 'pi1', item_index: 1, question_type: 'single_choice', score: 2 },
    ])
    const user = userEvent.setup()
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByTestId('confirm-blueprint-btn')).not.toBeDisabled())
    await user.click(screen.getByTestId('confirm-blueprint-btn'))
    await waitFor(() => expect(mockExamConfirmBP).toHaveBeenCalled())
    // refresh -> projectsApi.get re-called (called at least 2x: initial + refresh)
    await waitFor(() => expect(mockProjectsGet.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('shows contract stage when status is contract', async () => {
    mockProjectsGet.mockResolvedValue({
      ...project,
      status: 'contract',
      blueprint_confirmed: true,
    })
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await screen.findByText('合同阶段', undefined, { timeout: 5000 })
    await screen.findByTestId('confirm-contract-btn', undefined, { timeout: 5000 })
    expect(screen.getByTestId('confirm-contract-btn')).toBeInTheDocument()
  })

  it('TR-8.4.2: confirm contract → calls confirmContract API then refresh', async () => {
    mockProjectsGet.mockResolvedValue({
      ...project,
      status: 'contract',
      blueprint_confirmed: true,
    })
    const user = userEvent.setup()
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    const btn = await screen.findByTestId('confirm-contract-btn', undefined, { timeout: 5000 })
    await user.click(btn)
    await waitFor(() => expect(mockExamConfirmContract).toHaveBeenCalled())
    await waitFor(() => expect(mockProjectsGet.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('TR-8.4.3: generating stage → renders start button, wiring works', async () => {
    mockProjectsGet.mockResolvedValue({
      ...project,
      status: 'generating',
      blueprint_confirmed: true,
    })
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await screen.findByText('生成阶段', undefined, { timeout: 5000 })
    expect(screen.getByTestId('start-gen-btn')).toBeInTheDocument()
  })

  it('TR-8.4.4: review stage → confirm final → calls confirmPaperVersion API then refresh', async () => {
    mockProjectsGet.mockResolvedValue({
      ...project,
      status: 'review',
      blueprint_confirmed: true,
    })
    const user = userEvent.setup()
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    const finalBtn = await screen.findByTestId('confirm-final-btn', undefined, { timeout: 5000 })
    await user.click(finalBtn)
    await waitFor(() => expect(mockExamConfirmPV).toHaveBeenCalled())
    await waitFor(() => expect(mockProjectsGet.mock.calls.length).toBeGreaterThanOrEqual(2))
  })
})
