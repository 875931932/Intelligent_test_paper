import { describe, it, expectTypeOf } from 'vitest'
import type { ExamProjectDetail, PipelineStage } from '../types'

describe('exam project pipeline types', () => {
  it('PipelineStage has 5 stages', () => {
    type Expected = 'blueprint' | 'contract' | 'generating' | 'review' | 'exported'
    expectTypeOf<PipelineStage>().toEqualTypeOf<Expected>()
  })

  it('ExamProjectDetail has pipeline fields', () => {
    type Expected = {
      id: string
      name: string
      status: string
      blueprint_confirmed: boolean
      version_confirmed: boolean
    }
    expectTypeOf<ExamProjectDetail>().toMatchTypeOf<Expected>()
  })
})
