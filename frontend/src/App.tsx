/**
 * 教师控制台应用壳：两层导航（课程空间 + 试卷项目）。
 * 课程空间=长期可复用资产；试卷项目=按学期归档的单次命题。
 */

import { useCallback, useEffect, useState } from 'react'
import { CoursesPage } from './console/CoursesPage'
import { MaterialsStep } from './console/steps/MaterialsStep'
import { FrameworkStep } from './console/steps/FrameworkStep'
import { CourseSpaceHome } from './console/CourseSpaceHome'
import { PublishedTreeBrowse } from './console/PublishedTreeBrowse'
import { ExamProjectList } from './console/ExamProjectList'
import { Layout } from './console/shell/Layout'
import { frameworkApi, knowledgeApi, materialsApi } from './console/client'
import { openCourseSpace, goToSection, type CourseSection, type Route } from './console/nav'
import type { Course, CourseReadiness, Material } from './console/types'
import { Notice } from './console/ui'
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
      setMaterials(await materialsApi.list(courseId))
    } catch {
      setMaterials([])
    }
  }, [])

  const refreshReadiness = useCallback(async (courseId: string) => {
    const next: CourseReadiness = { ...EMPTY_READINESS }
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
      await refreshMaterials(course.id)
      await refreshReadiness(course.id)
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
            onRefresh={() => refreshMaterials(route.course.id)}
          />
        ) : route.section === 'framework' ? (
          <FrameworkStep
            courseId={route.course.id}
            materials={materials}
            onDone={() => refreshReadiness(route.course.id)}
          />
        ) : route.section === 'knowledge' ? (
          <PublishedTreeBrowse courseId={route.course.id} />
        ) : (
          <ExamProjectList />
        )
      ) : route.page === 'exam-project' ? (
        <div className="content-inner">
          <div className="page-head">
            <h2>试卷项目生产线</h2>
            <div className="desc">蓝图 → 合同 → 生成 → 审核 → 导出 · 5 阶段流水线将在 Plan 3 交付。</div>
          </div>
          <Notice kind="info">S2/S3 占位</Notice>
        </div>
      ) : null}
    </Layout>
  )
}
