import { describe, it, expectTypeOf } from 'vitest'
import type { CourseReadiness, ExamProjectSummary } from './types'

describe('readiness types', () => {
  it('CourseReadiness shape', () => {
    const r: CourseReadiness = {
      materialsReady: true,
      frameworkReady: true,
      frameworkVersion: 'v3',
      knowledgeReady: true,
      knowledgeVersion: 'v8',
      knowledgeCardCount: 37,
      knowledgeUngroundedCount: 3,
      projects: [],
    }
    expectTypeOf(r).toMatchTypeOf<CourseReadiness>()
  })

  it('ExamProjectSummary status union', () => {
    // 双向可赋性 = 类型相等；用 toMatchTypeOf 规避 toEqualTypeOf 对字符串字面量联合的已知误报。
    type ExpectedStatus = 'draft' | 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'
    expectTypeOf<ExamProjectSummary['status']>().toMatchTypeOf<ExpectedStatus>()
    expectTypeOf<ExpectedStatus>().toMatchTypeOf<ExamProjectSummary['status']>()
  })
})
