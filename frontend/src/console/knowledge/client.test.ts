import { describe, it, expect, vi, afterEach } from 'vitest'
import { knowledgeApi } from '../client'

describe('knowledgeApi evidence method', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('getEvidence calls correct endpoint and returns EvidenceLink[]', async () => {
    const payload = [
      { evidence_role: 'direct', confidence: 0.9, content: '...', locator: null, material_version_id: 'mv1' },
    ]
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(payload),
    } as Response)

    const result = await knowledgeApi.getEvidence('course-1', 'card-1')

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/v1/courses/course-1/published-knowledge/cards/card-1/evidence',
      expect.objectContaining({
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    )
    expect(result).toHaveLength(1)
    expect(result[0].evidence_role).toBe('direct')
  })
})
