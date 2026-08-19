import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BlueprintStage } from './blueprintStage'
import type { ExamProjectDetail } from '../types'

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'blueprint',
  total_score: 100, question_count: 0, pending_review: 0,
  blueprint_confirmed: false, version_confirmed: false,
}

// Mock examPipelineApi via module-level mock so component uses fake implementation
const mockCreateBlueprint = vi.fn()
const mockGetPlanItems = vi.fn()
const mockPatchPlanItem = vi.fn()
const mockConfirmBlueprint = vi.fn()

vi.mock('../client', async () => {
  const actual: any = await vi.importActual('../client')
  return {
    ...actual,
    examPipelineApi: {
      ...(actual.examPipelineApi ?? {}),
      createBlueprint: (...args: unknown[]) => mockCreateBlueprint(...args),
      getPlanItems: (...args: unknown[]) => mockGetPlanItems(...args),
      patchPlanItem: (...args: unknown[]) => mockPatchPlanItem(...args),
      confirmBlueprint: (...args: unknown[]) => mockConfirmBlueprint(...args),
    },
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  mockGetPlanItems.mockResolvedValue([])
})

afterEach(() => {
  vi.restoreAllMocks
})

// Helpers to build plan items of size N
const makePlan = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    id: `pi${i + 1}`,
    item_index: i + 1,
    question_type: ['single_choice', 'true_false', 'fill_blank', 'short_answer', 'comprehensive'][i % 5],
    score: 2 + i,
    difficulty: 'medium',
    cognitive_level: '记忆',
    assessment_unit_id: `au${i + 1}`,
    assessment_unit_title: `单元${i + 1}`,
    knowledge_card_id: `kc${i + 1}`,
    knowledge_card_name: `知识卡${i + 1}`,
    exam_point_id: `ep${i + 1}`,
  }))

describe('BlueprintStage (TR-8.1)', () => {
  it('renders blueprint summary with total score', () => {
    render(<BlueprintStage courseId="c1" project={project} onConfirm={vi.fn()} />)
    expect(screen.getByText('蓝图阶段')).toBeInTheDocument()
    expect(screen.getByText(/100/)).toBeInTheDocument()
  })

  it('shows gate banner when not confirmed', () => {
    render(<BlueprintStage courseId="c1" project={project} onConfirm={vi.fn()} />)
    expect(screen.getByText(/确认蓝图/)).toBeInTheDocument()
  })

  it('shows verified banner when confirmed', () => {
    const confirmed = { ...project, blueprint_confirmed: true }
    render(<BlueprintStage courseId="c1" project={confirmed} onConfirm={vi.fn()} />)
    const banner = screen.getByText(/蓝图已确认/).closest('.gate-banner')
    expect(banner?.classList.contains('verified')).toBe(true)
  })

  it('shows generate blueprint form when planItems empty (before POST)', () => {
    render(<BlueprintStage courseId="c1" project={project} onConfirm={vi.fn()} />)
    expect(screen.getByLabelText('framework_version_id')).toBeInTheDocument()
    expect(screen.getByLabelText('catalog_version_id')).toBeInTheDocument()
    expect(screen.getByTestId('generate-blueprint-btn')).toBeInTheDocument()
  })

  it('POST /blueprints → renders 5 editable table rows', async () => {
    const user = userEvent.setup()
    const plan5 = makePlan(5)
    mockCreateBlueprint.mockResolvedValue({ blueprint_version_id: 'bv1', plan: plan5 })
    render(<BlueprintStage courseId="c1" project={project} onConfirm={vi.fn()} />)
    await user.click(screen.getByTestId('generate-blueprint-btn'))
    await waitFor(() => {
      plan5.forEach((p) => {
        expect(screen.getByTestId(`plan-row-${p.id}`)).toBeInTheDocument()
      })
    })
    // Check 5 rows rendered
    const rows = plan5
      .map((p) => screen.queryByTestId(`plan-row-${p.id}`))
      .filter(Boolean)
    expect(rows).toHaveLength(5)
    // Ensure score inputs present and editable
    plan5.forEach((p) => {
      const input = screen.getByTestId(`score-input-${p.id}`) as HTMLInputElement
      expect(input).toBeInTheDocument()
      expect(Number(input.value)).toBe(p.score)
    })
  })

  it('editing score cell and blur triggers patchPlanItem', async () => {
    const plan5 = makePlan(5)
    // Simulate prefilled by getPlanItems (so initial render shows table)
    mockGetPlanItems.mockResolvedValueOnce(plan5)
    mockPatchPlanItem.mockImplementation((_c: string, _id: string, changes: any) =>
      Promise.resolve({ ...plan5[0], ...changes }),
    )
    render(<BlueprintStage courseId="c1" project={project} onConfirm={vi.fn()} />)
    // Wait for initial getPlanItems result to render rows
    await waitFor(() => expect(screen.getByTestId(`plan-row-${plan5[0].id}`)).toBeInTheDocument())
    const input = screen.getByTestId(`score-input-${plan5[0].id}`) as HTMLInputElement
    fireEvent.change(input, { target: { value: '99' } })
    fireEvent.blur(input)
    await waitFor(() => expect(mockPatchPlanItem).toHaveBeenCalled())
    const [courseIdArg, planItemIdArg, changesArg] = mockPatchPlanItem.mock
      .calls[0] as [string, string, { score: number }]
    expect(courseIdArg).toBe('c1')
    expect(planItemIdArg).toBe(plan5[0].id)
    expect(changesArg).toEqual({ score: 99 })
  })
})
