import type { Course } from './types'

export type CourseSection = 'home' | 'materials' | 'framework' | 'knowledge' | 'projects'

export type Route =
  | { page: 'courses' }
  | { page: 'course-space'; course: Course; section: CourseSection }
  | { page: 'exam-project'; course: Course; projectId: string }
  | { page: 'demo' }

export const COURSE_SECTIONS: { key: CourseSection; label: string }[] = [
  { key: 'materials', label: '资料库' },
  { key: 'framework', label: '命题框架' },
  { key: 'knowledge', label: '知识目录' },
  { key: 'projects', label: '试卷项目' },
]

export const SECTION_LABELS: Record<CourseSection, string> = {
  home: '课程空间',
  materials: '资料库',
  framework: '命题框架',
  knowledge: '知识目录',
  projects: '试卷项目',
}

/** 打开课程空间，默认落在首页（四宫格概览）。 */
export function openCourseSpace(course: Course): Route {
  return { page: 'course-space', course, section: 'home' }
}

/** 在课程空间内切换区块；非课程空间路由原样返回。 */
export function goToSection(route: Route, section: CourseSection): Route {
  if (route.page !== 'course-space') return route
  return { ...route, section }
}

export function isCourseSpace(route: Route): route is Extract<Route, { page: 'course-space' }> {
  return route.page === 'course-space'
}
