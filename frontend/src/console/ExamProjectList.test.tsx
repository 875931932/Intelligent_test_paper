import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ExamProjectList } from './ExamProjectList'

// Mock projectsApi 避免真实 HTTP
vi.mock('./client', () => ({
  projectsApi: {
    list: vi.fn().mockResolvedValue([]),
  },
}))

describe('ExamProjectList', () => {
  it('renders empty state when no projects', async () => {
    const { container } = render(<ExamProjectList courseId="c1" onOpenProject={vi.fn()} />)
    // 等待异步加载完成
    expect(await screen.findByText('试卷项目')).toBeInTheDocument()
    expect(screen.getByText(/暂无试卷项目/)).toBeInTheDocument()
  })
})
