import { describe, it, expect, vi } from 'vitest'

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
