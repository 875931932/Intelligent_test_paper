import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../../test/render'
import { Layout } from './Layout'
import { openCourseSpace, type Route } from '../nav'
import type { Course } from '../types'

const course: Course = { id: 'c1', name: '大模型调优', slug: 'sk3020' }

describe('Layout', () => {
  it('renders brand and section nav in course-space', async () => {
    const onNavigate = vi.fn()
    const route = openCourseSpace(course)
    renderWithProviders(
      <Layout route={route} onNavigateSection={onNavigate} onBackToCourses={() => {}} onOpenDemo={() => {}}>
        <div>CONTENT</div>
      </Layout>,
    )
    expect(screen.getByText('砚卷工作台')).toBeInTheDocument()
    // 课程名同时出现在侧栏 .sidebar-course 和顶栏 <h1>，使用 getAllByText 容纳重复。
    expect(screen.getAllByText('大模型调优')[0]).toBeInTheDocument()
    expect(screen.getByText('知识目录')).toBeInTheDocument()
    expect(screen.getByText('CONTENT')).toBeInTheDocument()
  })

  it('clicking a section calls onNavigateSection with that key', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    const route = openCourseSpace(course)
    renderWithProviders(
      <Layout route={route} onNavigateSection={onNavigate} onBackToCourses={() => {}} onOpenDemo={() => {}}>
        <div />
      </Layout>,
    )
    await user.click(screen.getByText('命题框架'))
    expect(onNavigate).toHaveBeenCalledWith('framework')
  })

  it('shows courses-list nav when not in workspace', () => {
    const route: Route = { page: 'courses' }
    renderWithProviders(
      <Layout route={route} onNavigateSection={() => {}} onBackToCourses={() => {}} onOpenDemo={() => {}}>
        <div />
      </Layout>,
    )
    // "课程列表" 同时作为侧栏 nav-item 和顶栏 <h1> 渲染。
    expect(screen.getAllByText('课程列表')[0]).toBeInTheDocument()
  })
})
