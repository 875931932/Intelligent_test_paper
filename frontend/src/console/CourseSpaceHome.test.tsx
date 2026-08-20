import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/render'
import { CourseSpaceHome } from './CourseSpaceHome'
import type { CourseReadiness } from './types'

const ready: CourseReadiness = {
  materialsReady: true,
  frameworkReady: true,
  frameworkVersion: 'v3',
  knowledgeReady: true,
  knowledgeVersion: 'v8',
  knowledgeCardCount: 37,
  knowledgeUngroundedCount: 3,
  projects: [
    { id: 'p1', semester_label: '2026-2027-I', status: 'review', total_score: 100, question_count: 37, pending_review: 2 },
  ],
}

describe('CourseSpaceHome', () => {
  it('renders status pills from readiness', () => {
    renderWithProviders(<CourseSpaceHome readiness={ready} onOpenSection={() => {}} />)
    expect(screen.getByText('资料库')).toBeInTheDocument()
    expect(screen.getByText('published v3')).toBeInTheDocument()
    expect(screen.getByText(/37 卡/)).toBeInTheDocument()
    expect(screen.getByText(/3 未落地/)).toBeInTheDocument()
    expect(screen.getByText('1 个')).toBeInTheDocument()
    expect(screen.getByText('出卷进度')).toBeInTheDocument()
    expect(screen.getByText('资料整理')).toBeInTheDocument()
    expect(screen.getByText('生成蓝图')).toBeInTheDocument()
    expect(screen.getByText('生成试卷')).toBeInTheDocument()
  })

  it('clicking a card calls onOpenSection with that section', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    renderWithProviders(<CourseSpaceHome readiness={ready} onOpenSection={onOpen} />)
    await user.click(screen.getByText('知识目录'))
    expect(onOpen).toHaveBeenCalledWith('knowledge')
  })

  it('renders neutral pill when knowledge not ready', () => {
    renderWithProviders(
      <CourseSpaceHome readiness={{ ...ready, knowledgeReady: false, knowledgeVersion: null, knowledgeCardCount: 0, knowledgeUngroundedCount: 0 }} onOpenSection={() => {}} />,
    )
    expect(screen.getByText('未发布')).toBeInTheDocument()
  })
})
