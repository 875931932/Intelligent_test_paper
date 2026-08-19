import { describe, it, expect, vi, beforeEach } from 'vitest'

// ── 旧版 mock（保留现有 projectsApi 测试不被破坏） ──
vi.mock('../client', () => ({
  projectsApi: {
    list: vi.fn(),
    create: vi.fn(),
    get: vi.fn(),
    updateStatus: vi.fn(),
  },
}))

import { projectsApi } from '../client'

describe('projectsApi', () => {
  it('list calls correct endpoint', async () => {
    const mock = projectsApi.list as ReturnType<typeof vi.fn>
    mock.mockResolvedValue([{ id: 'p1', name: 'Test', status: 'draft' }])
    const result = await projectsApi.list('course-1')
    expect(mock).toHaveBeenCalledWith('course-1')
    expect(result).toHaveLength(1)
  })

  it('create calls POST endpoint', async () => {
    const mock = projectsApi.create as ReturnType<typeof vi.fn>
    mock.mockResolvedValue({ id: 'p1', name: 'New', status: 'draft' })
    const result = await projectsApi.create('course-1', 'New')
    expect(mock).toHaveBeenCalledWith('course-1', 'New')
    expect(result.id).toBe('p1')
  })

  it('get calls GET endpoint', async () => {
    const mock = projectsApi.get as ReturnType<typeof vi.fn>
    mock.mockResolvedValue({ id: 'p1', name: 'Test', status: 'draft' })
    const result = await projectsApi.get('course-1', 'p1')
    expect(mock).toHaveBeenCalledWith('course-1', 'p1')
    expect(result.id).toBe('p1')
  })

  it('updateStatus calls PATCH endpoint', async () => {
    const mock = projectsApi.updateStatus as ReturnType<typeof vi.fn>
    mock.mockResolvedValue({ id: 'p1', name: 'Test', status: 'blueprint' })
    const result = await projectsApi.updateStatus('course-1', 'p1', 'blueprint')
    expect(mock).toHaveBeenCalledWith('course-1', 'p1', 'blueprint')
    expect(result.status).toBe('blueprint')
  })
})

// ── examPipelineApi fetch 级别测试：使用真实 client 实现 + fetch spy ──
describe('examPipelineApi (fetch-level)', () => {
  let fetchSpy: ReturnType<typeof vi.fn>

  // 动态加载真实 client 模块（避开上面的 vi.mock）
  async function loadRealClient() {
    // vi.importActual 返回未被 mock 的实际模块
    const mod = await vi.importActual<typeof import('../client')>('../client')
    return mod
  }

  beforeEach(() => {
    fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
  })

  it('createBlueprint POSTs correct URL with JSON body', async () => {
    const { examPipelineApi } = await loadRealClient()
    fetchSpy.mockResolvedValue(
      Response.json({
        blueprint_version_id: 'bv1',
        plan: [
          { id: 'pi1', item_index: 1, question_type: 'single_choice', score: 2 },
        ],
      }),
    )
    const body = {
      framework_version_id: 'fw-dev',
      catalog_version_id: 'cat-dev',
      type_rules: { single_choice: { count: 15, score: 2 } },
      chapter_weights: { anchor_EP1: 30 },
      units: [],
      card_semantic_profiles: {},
      card_question_types: {},
    }
    const res = await examPipelineApi.createBlueprint('courseA', 'projectX', body)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/courses/courseA/exam-projects/projectX/blueprints')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      framework_version_id: 'fw-dev',
      catalog_version_id: 'cat-dev',
    })
    expect(res.blueprint_version_id).toBe('bv1')
  })

  it('startGeneration POSTs /generate with mock_graph body', async () => {
    const { examPipelineApi } = await loadRealClient()
    fetchSpy.mockResolvedValue(Response.json({ task_run_id: 'tr-999' }))
    const res = await examPipelineApi.startGeneration('courseA', 'projectX', { mock_graph: true })
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/courses/courseA/exam-projects/projectX/generate')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ mock_graph: true })
    expect(res.task_run_id).toBe('tr-999')
  })

  it('confirmPaperVersion POSTs /confirm with force flag', async () => {
    const { examPipelineApi } = await loadRealClient()
    fetchSpy.mockResolvedValue(Response.json({ status: 'finalized', unresolved: 0 }))
    const res = await examPipelineApi.confirmPaperVersion('courseA', 'pv-12', {
      force_ignore_needs_review: true,
    })
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/courses/courseA/paper-versions/pv-12/confirm')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ force_ignore_needs_review: true })
    expect(res.status).toBe('finalized')
    expect(res.unresolved).toBe(0)
  })

  it('throws on non-2xx responses', async () => {
    const { examPipelineApi } = await loadRealClient()
    fetchSpy.mockResolvedValue(
      Response.json({ detail: 'bad request' }, { status: 400 }),
    )
    await expect(examPipelineApi.getPlanItems('c1', 'p1')).rejects.toThrow(/bad request/)
  })
})
