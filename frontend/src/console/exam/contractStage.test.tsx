import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ContractStage } from './contractStage'
import type { ExamProjectDetail, PaperContract } from '../types'

const contract: PaperContract = {
  total_score: 100,
  slots: [
    {
      item_index: 1,
      question_type: 'single_choice',
      score: 5,
      difficulty: 'medium',
      cognitive_level: '应用',
      assessment_mode: 'conceptual',
      exam_point_id: 'ep1',
      anchor_key: 'k1',
      unit_id: 'u1',
      card_id: 'c1',
      coverage_atom: '原子1',
      answer_boundary: 'b1',
      performance_statement: 'ps1',
      prompt_material: [],
      scope_boundary: {},
      preferred_terms: [],
      forbidden_context: { atoms: [], answer_cores: [] },
      cognitive_sequence: [],
      subquestion_actions: [],
      answer_boundaries: [],
    },
  ],
  conflicts: [{ code: 'duplicate_atom', exam_point_id: 'ep1', message: '原子重复', detail: {} }],
  audit_summary: {
    exam_points: [],
    type_counts: { single_choice: 1 },
    difficulty_counts: { medium: 1 },
  },
}

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'contract',
  total_score: 100, question_count: 1, pending_review: 0,
  blueprint_confirmed: true, version_confirmed: false,
  contract,
}

// Module-level mocks
const mockAllocate = vi.fn()
const mockRevise = vi.fn()
const mockConfirm = vi.fn()

vi.mock('../client', async () => {
  const actual: any = await vi.importActual('../client')
  return {
    ...actual,
    examPipelineApi: {
      ...(actual.examPipelineApi ?? {}),
      allocateContract: (...a: unknown[]) => mockAllocate(...a),
      reviseContract: (...a: unknown[]) => mockRevise(...a),
      confirmContract: (...a: unknown[]) => mockConfirm(...a),
    },
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  mockAllocate.mockResolvedValue({
    used_threshold: 0.9,
    conflicts_history: [],
    contract_snapshot: {
      slots: [
        {
          item_index: 1,
          question_type: 'single_choice',
          score: 5,
          difficulty: 'medium',
          cognitive_level: '应用',
          card_id: 'c1',
          coverage_atom: '原子1',
        },
      ],
    },
  })
  mockRevise.mockResolvedValue({ revised_contract_snapshot: { slots: [] } })
  mockConfirm.mockResolvedValue({
    generation_run_id: 'gr1',
    threshold: 0.9,
    slot_count: 1,
  })
})

describe('ContractStage', () => {
  it('renders contract slots table', async () => {
    render(<ContractStage courseId="c1" project={project} onGenerate={vi.fn()} />)
    await screen.findByTestId('contract-slot-1', undefined, { timeout: 5000 })
    // slot columns: card_id is shown; mock returns c1. Also single_choice present.
    expect(screen.getByText('c1')).toBeInTheDocument()
    expect(screen.getByText('single_choice')).toBeInTheDocument()
  })

  it('renders conflict history banner if present', async () => {
    mockAllocate.mockResolvedValueOnce({
      used_threshold: 0.9,
      conflicts_history: [[1, 2]] as Array<[number, number]>,
      contract_snapshot: { slots: [] },
    })
    render(<ContractStage courseId="c1" project={project} onGenerate={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('conflicts-history')).toBeInTheDocument())
  })

  it('shows confirm button', async () => {
    render(<ContractStage courseId="c1" project={project} onGenerate={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('confirm-contract-btn')).toBeInTheDocument())
  })

  it('calls confirmContract + onGenerate when confirm clicked', async () => {
    const onGenerate = vi.fn()
    const user = userEvent.setup()
    render(<ContractStage courseId="c1" project={project} onGenerate={onGenerate} />)
    await waitFor(() => expect(screen.getByTestId('confirm-contract-btn')).toBeInTheDocument())
    await user.click(screen.getByTestId('confirm-contract-btn'))
    await waitFor(() => expect(mockConfirm).toHaveBeenCalled())
    expect(onGenerate).toHaveBeenCalledWith('p1')
  })
})
