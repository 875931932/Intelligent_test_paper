import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './test/render'
import App from './App'

vi.mock('./console/client', () => ({
  coursesApi: { list: vi.fn(), create: vi.fn() },
  materialsApi: { list: vi.fn(), upload: vi.fn(), remove: vi.fn(), startParse: vi.fn(), pollParse: vi.fn() },
  frameworkApi: { createRun: vi.fn(), getCandidate: vi.fn(), confirm: vi.fn(), reject: vi.fn(), getCurrent: vi.fn() },
  knowledgeApi: { createRun: vi.fn(), getCandidate: vi.fn(), publish: vi.fn(), getPublished: vi.fn() },
  examApi: { allocate: vi.fn(), confirm: vi.fn(), generate: vi.fn() },
}))

import { coursesApi, materialsApi, frameworkApi, knowledgeApi } from './console/client'

beforeEach(() => {
  vi.clearAllMocks()
  ;(coursesApi.list as ReturnType<typeof vi.fn>).mockResolvedValue([
    { id: 'c1', name: '大模型调优', slug: 'sk3020' },
  ])
  ;(materialsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue([])
  ;(frameworkApi.getCurrent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('no framework'))
  ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('no knowledge'))
})

describe('App two-layer navigation', () => {
  it('starts at courses list', async () => {
    renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByText('大模型调优')).toBeInTheDocument())
    // "课程列表" 同时作为侧栏 nav-item 与顶栏 <h1> 渲染（见 Layout），用 getAllByText 容纳重复。
    expect(screen.getAllByText('课程列表')[0]).toBeInTheDocument()
  })

  it('opening a course lands on course-space home', async () => {
    const user = userEvent.setup()
    renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByText('大模型调优')).toBeInTheDocument())
    await user.click(screen.getByText('进入工作台'))
    // "课程空间" 同时作为顶栏 section 标签与 CourseSpaceHome <h2> 渲染，用 getAllByText。
    await waitFor(() => expect(screen.getAllByText('课程空间')[0]).toBeInTheDocument())
    expect(screen.getByText('未发布')).toBeInTheDocument() // knowledge not ready
  })

  it('clicking knowledge section in sidebar shows browse', async () => {
    const user = userEvent.setup()
    ;(knowledgeApi.getPublished as ReturnType<typeof vi.fn>).mockResolvedValue({
      catalog_version_id: 'cat-1', framework_version_id: 'fw-1',
      exam_points: [{ id: 'ep1', code: 'EP2', title: '参数高效微调', assessment_requirement: '', anchor_key: 'a', weight_value: 25, weight_source: '', cognitive_targets: [], allowed_question_types: [], operational_detail_policy: '' }],
      units: [{ unit_id: 'u1', code: 'AU03', title: '参数高效微调', performance_statement: '', exam_point_id: 'ep1', exam_point_code: 'EP2', anchor_key: 'a', card_ids: ['k1'] }],
      knowledge_cards: { k1: { name: 'QLoRA', performance_statement: '', assessable_content: ['a'], scope_boundary: {}, cognitive_targets: [], allowed_question_types: [], importance: 4, concept_cluster: '簇', answer_proposition: '', answer_boundary: '', prompt_material: [] } },
    })
    renderWithProviders(<App />)
    await waitFor(() => expect(screen.getByText('大模型调优')).toBeInTheDocument())
    await user.click(screen.getByText('进入工作台'))
    await waitFor(() => expect(screen.getAllByText('课程空间')[0]).toBeInTheDocument())
    // 侧栏点"知识目录"
    const knowledgeNav = screen.getAllByText('知识目录').find((el) => el.closest('.nav-item'))
    await user.click(knowledgeNav!)
    await waitFor(() => expect(screen.getByText('QLoRA')).toBeInTheDocument())
  })
})
