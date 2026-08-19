import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../client', () => ({
  projectsApi: {
    get: vi.fn(),
    updateStatus: vi.fn(),
  },
}))

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { projectsApi } from '../client'
import { ExamProjectWorkspace } from './examProjectWorkspace'
import type { ExamProjectDetail } from '../types'

const project: ExamProjectDetail = {
  id: 'p1', name: '2024期末', semester_label: '2024秋', status: 'blueprint',
  total_score: 100, question_count: 0, pending_review: 0,
  blueprint_confirmed: false, version_confirmed: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(projectsApi.get as ReturnType<typeof vi.fn>).mockResolvedValue(project)
  ;(projectsApi.updateStatus as ReturnType<typeof vi.fn>).mockResolvedValue({ ...project, blueprint_confirmed: true })
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
    expect(screen.getByText('确认蓝图')).toBeInTheDocument()
  })

  it('calls updateStatus when confirming blueprint', async () => {
    const user = userEvent.setup()
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByText('确认蓝图')).toBeInTheDocument())
    await user.click(screen.getByText('确认蓝图'))
    expect(projectsApi.updateStatus).toHaveBeenCalled()
  })

  it('shows contract stage when status is contract', async () => {
    ;(projectsApi.get as ReturnType<typeof vi.fn>).mockResolvedValue({ ...project, status: 'contract', blueprint_confirmed: true })
    render(<ExamProjectWorkspace courseId="c1" projectId="p1" />)
    await waitFor(() => expect(screen.getByText('合同阶段')).toBeInTheDocument())
  })
})
