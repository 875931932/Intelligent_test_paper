/**
 * 教师控制台应用壳：两层导航（课程空间 + 试卷项目）。
 * 课程空间=长期可复用资产；试卷项目=按学期归档的单次命题。
 */

import { useCallback, useEffect, useState } from 'react'
import { CoursesPage } from './console/CoursesPage'
import { MaterialsStep } from './console/steps/MaterialsStep'
import { FrameworkStep } from './console/steps/FrameworkStep'
import { CourseSpaceHome } from './console/CourseSpaceHome'
import { KnowledgeCatalog } from './console/knowledge/knowledgeCatalog'
import { ExamProjectList } from './console/ExamProjectList'
import { ExamProjectWorkspace } from './console/exam/examProjectWorkspace'
import { Layout } from './console/shell/Layout'
import { frameworkApi, knowledgeApi, materialsApi } from './console/client'
import { openCourseSpace, goToSection, type CourseSection, type Route } from './console/nav'
import type { Course, CourseReadiness, Material } from './console/types'
import { DemoApp } from './demo/DemoApp'

const EMPTY_READINESS: CourseReadiness = {
  materialsReady: false,
  frameworkReady: false,
  frameworkVersion: null,
  knowledgeReady: false,
  knowledgeVersion: null,
  knowledgeCardCount: 0,
  knowledgeUngroundedCount: 0,
  projects: [],
}

export default function App() {
  const [route, setRoute] = useState<Route>({ page: 'courses' })
  const [materials, setMaterials] = useState<Material[]>([])
  const [readiness, setReadiness] = useState<CourseReadiness>(EMPTY_READINESS)

  const refreshMaterials = useCallback(async (courseId: string) => {
    try {
      const mats = await materialsApi.list(courseId)
      setMaterials(mats)
      return mats
    } catch {
      setMaterials([])
      return []
    }
  }, [])

  const refreshReadiness = useCallback(async (courseId: string, materialsCount = 0) => {
    const next: CourseReadiness = { ...EMPTY_READINESS }
    next.materialsReady = materialsCount > 0
    try {
      await frameworkApi.getCurrent(courseId)
      next.frameworkReady = true
    } catch {
      next.frameworkReady = false
    }
    try {
      const k = await knowledgeApi.getPublished(courseId)
      next.knowledgeReady = true
      next.knowledgeVersion = (k.catalog_version_id ?? '').slice(0, 8) || null
      next.knowledgeCardCount = Object.keys(k.knowledge_cards).length
    } catch {
      next.knowledgeReady = false
    }
    setReadiness(next)
  }, [])

  const openCourse = useCallback(
    async (course: Course) => {
      setRoute(openCourseSpace(course))
      setMaterials([])
      setReadiness(EMPTY_READINESS)
      const mats = await refreshMaterials(course.id)
      await refreshReadiness(course.id, mats.length)
    },
    [refreshMaterials, refreshReadiness],
  )

  const backToCourses = useCallback(() => {
    setRoute({ page: 'courses' })
    setMaterials([])
  }, [])

  const navigateSection = useCallback((section: CourseSection) => {
    setRoute((r) => goToSection(r, section))
  }, [])

  // 工作台视图下轮询进行中的解析状态（与资料步骤内部轮询互补）
  useEffect(() => {
    if (route.page !== 'course-space' && route.page !== 'exam-project') return
    const hasActive = materials.some(
      (m) => m.parse_status != null && !['ready', 'failed'].includes(m.parse_status.status),
    )
    if (!hasActive) return
    const timer = setInterval(() => {
      void refreshMaterials(route.course.id)
    }, 4000)
    return () => clearInterval(timer)
  }, [route, materials, refreshMaterials])

  if (route.page === 'demo') {
    return (
      <div style={{ minHeight: '100vh' }}>
        <div style={{ maxWidth: 1160, margin: '0 auto', padding: '16px 24px' }}>
          <button className="btn ghost" onClick={() => setRoute({ page: 'courses' })}>
            ← 返回教师控制台
          </button>
        </div>
        <DemoApp />
      </div>
    )
  }

  return (
    <Layout
      route={route}
      onNavigateSection={navigateSection}
      onBackToCourses={backToCourses}
      onOpenDemo={() => setRoute({ page: 'demo' })}
    >
      {route.page === 'courses' ? (
        <CoursesPage onOpen={(course) => void openCourse(course)} />
      ) : route.page === 'course-space' ? (
        route.section === 'home' ? (
          <CourseSpaceHome readiness={readiness} onOpenSection={navigateSection} />
        ) : route.section === 'materials' ? (
          <MaterialsStep
            courseId={route.course.id}
            materials={materials}
            onRefresh={async () => { await refreshMaterials(route.course.id) }}
          />
        ) : route.section === 'framework' ? (
          <FrameworkStep
            courseId={route.course.id}
            materials={materials}
            onDone={() => refreshReadiness(route.course.id, materials.length)}
          />
        ) : route.section === 'knowledge' ? (
          <KnowledgeCatalog courseId={route.course.id} />
        ) : route.section === 'projects' ? (
          <ExamProjectList
            courseId={route.course.id}
            onOpenProject={(pid) => setRoute({ page: 'exam-project', course: route.course, projectId: pid })}
          />
        ) : (
          null
        )
      ) : route.page === 'exam-project' ? (
        <ExamProjectWorkspace courseId={route.course.id} projectId={route.projectId} />
      ) : null}
    </Layout>
  )
}
