import { describe, it, expect } from 'vitest'
import { openCourseSpace, goToSection, isCourseSpace, COURSE_SECTIONS, SECTION_LABELS } from './nav'
import type { Course } from './types'

const course: Course = { id: 'c1', name: '大模型调优', slug: 'sk3020' }

describe('nav', () => {
  it('openCourseSpace lands on course-space home', () => {
    const r = openCourseSpace(course)
    expect(r.page).toBe('course-space')
    if (r.page === 'course-space') {
      expect(r.course.id).toBe('c1')
      expect(r.section).toBe('home')
    }
  })

  it('goToSection switches section within course-space, preserves course', () => {
    const r = openCourseSpace(course)
    const next = goToSection(r, 'knowledge')
    expect(next).not.toBe(r)
    if (next.page === 'course-space') {
      expect(next.section).toBe('knowledge')
      expect(next.course.id).toBe('c1')
    }
  })

  it('goToSection is no-op when not in course-space', () => {
    const r = { page: 'courses' as const }
    expect(goToSection(r, 'knowledge')).toBe(r)
  })

  it('isCourseSpace narrows route type', () => {
    expect(isCourseSpace(openCourseSpace(course))).toBe(true)
    expect(isCourseSpace({ page: 'courses' })).toBe(false)
  })

  it('COURSE_SECTIONS covers materials/framework/knowledge/projects', () => {
    const keys = COURSE_SECTIONS.map((s) => s.key)
    expect(keys).toEqual(['materials', 'framework', 'knowledge', 'projects'])
  })

  it('SECTION_LABELS has a label for every section', () => {
    expect(SECTION_LABELS.home).toBe('课程空间')
    expect(SECTION_LABELS.knowledge).toBe('知识目录')
  })
})
