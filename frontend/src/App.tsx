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
import { examPipelineApi, frameworkApi, knowledgeApi, materialsApi, projectsApi } from './console/client'
import { openCourseSpace, goToSection, type CourseSection, type Route } from './console/nav'
import type { Course, CourseReadiness, ExamProjectSummary, Material, WorkflowProgress } from './console/types'
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

const IDLE_PROGRESS: WorkflowProgress = { percent: 0, status: 'idle', message: '尚未开始' }

function materialProgress(materials: Material[], knowledgeReady: boolean): WorkflowProgress {
  if (materials.length === 0) return { ...IDLE_PROGRESS, message: '等待上传课程资料' }
  const ready = materials.filter((m) => m.parse_status?.status === 'ready').length
  const failed = materials.filter((m) => m.parse_status?.status === 'failed').length
  const started = materials.some((m) => m.parse_status != null)
  const percent = Math.round((ready / materials.length) * 80)
  if (!started) return { ...IDLE_PROGRESS, message: '等待启动资料解析' }
  if (failed > 0 && ready < materials.length) {
    return { percent, status: 'warning', message: `${ready}/${materials.length} 份资料已解析`, detail: `${failed} 份资料解析失败，请重试` }
  }
  if (ready < materials.length) {
    return { percent: Math.max(8, percent), status: 'running', message: `${ready}/${materials.length} 份资料解析中` }
  }
  if (!knowledgeReady) {
    return { percent: 80, status: 'success', message: '资料解析完成，等待知识目录发布' }
  }
  return { percent: 100, status: 'success', message: '资料整理完成，知识目录已发布' }
}

function blueprintProgress(projects: ExamProjectSummary[]): WorkflowProgress {
  if (projects.length === 0) return { ...IDLE_PROGRESS, message: '创建试卷项目后生成蓝图' }
  const active = projects.find((p) => p.status !== 'exported') ?? projects[0]
  if (active.status === 'draft') return { ...IDLE_PROGRESS, message: '等待生成蓝图' }
  if (active.status === 'blueprint') return { percent: 50, status: 'warning', message: '蓝图已生成，等待教师确认' }
  return { percent: 100, status: 'success', message: '蓝图已确认' }
}

function paperProgress(projects: ExamProjectSummary[]): WorkflowProgress {
  if (projects.length === 0) return { ...IDLE_PROGRESS, message: '蓝图确认后生成试卷' }
  const active = projects.find((p) => p.status === 'generating') ?? projects.find((p) => p.status !== 'exported') ?? projects[0]
  if (active.status === 'generating') {
    const percent = typeof active.generation_progress === 'number' ? active.generation_progress : 5
    if (active.generation_error) return { percent, status: 'error', message: '试卷生成失败', detail: active.generation_error }
    return { percent, status: 'running', message: active.generation_stage ? `正在${active.generation_stage}` : '试卷生成中' }
  }
  if (active.status === 'review') return { percent: 100, status: 'success', message: '试卷已生成，等待审核' }
  if (active.status === 'exported') return { percent: 100, status: 'success', message: '试卷已导出' }
  return { ...IDLE_PROGRESS, message: '蓝图确认后生成试卷' }
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

  const refreshReadiness = useCallback(async (courseId: string, materialsSnapshot: Material[] = []) => {
    const next: CourseReadiness = { ...EMPTY_READINESS }
    next.materialsReady = materialsSnapshot.length > 0
    try {
      const fw = await frameworkApi.getCurrent(courseId)
      next.frameworkReady = fw?.published !== false
    } catch {
      next.frameworkReady = false
    }
    try {
      const k = await knowledgeApi.getPublished(courseId)
      if (k?.published === false) {
        next.knowledgeReady = false
      } else {
        next.knowledgeReady = true
        next.knowledgeVersion = (k.catalog_version_id ?? '').slice(0, 8) || null
        next.knowledgeCardCount = Object.keys(k.knowledge_cards ?? {}).length
      }
    } catch {
      next.knowledgeReady = false
    }
    try {
      const rawProjects = await projectsApi.list(courseId)
      const projects: ExamProjectSummary[] = await Promise.all(rawProjects.map(async (p) => {
        let generationProgress: number | null = p.generation?.status === 'succeeded' ? 100 : null
        let generationStage: string | null = null
        let generationError: string | null = null
        if (p.active_task_run_id) {
          try {
            const run = await examPipelineApi.getTaskRun(courseId, p.active_task_run_id)
            generationProgress = typeof run.progress === 'number' ? run.progress : generationProgress
            generationStage = run.stage ?? null
            generationError = run.error_message ?? null
          } catch {
            // 项目列表仍可正常展示；任务详情失败时保留项目状态。
          }
        }
        return {
          id: p.id,
          semester_label: p.semester_label ?? '',
          status: p.status,
          total_score: p.total_score ?? 0,
          question_count: p.question_count ?? 0,
          pending_review: p.pending_review ?? 0,
          active_task_run_id: p.active_task_run_id,
          active_blueprint_version_id: p.active_blueprint_version_id,
          active_paper_version_id: p.active_paper_version_id,
          generation_progress: generationProgress,
          generation_stage: generationStage,
          generation_error: generationError,
        }
      }))
      next.projects = projects
    } catch {
      next.projects = []
    }
    next.materialProgress = materialProgress(materialsSnapshot, next.knowledgeReady)
    next.blueprintProgress = blueprintProgress(next.projects)
    next.paperProgress = paperProgress(next.projects)
    setReadiness(next)
  }, [])

  const openCourse = useCallback(
    async (course: Course) => {
      setRoute(openCourseSpace(course))
      setMaterials([])
      setReadiness(EMPTY_READINESS)
      const mats = await refreshMaterials(course.id)
      await refreshReadiness(course.id, mats)
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
    const hasGeneratingProject = route.page === 'course-space' && readiness.projects.some((p) => p.status === 'generating')
    if (!hasActive && !hasGeneratingProject) return
    const timer = setInterval(() => {
      void (async () => {
        const mats = await refreshMaterials(route.course.id)
        await refreshReadiness(route.course.id, mats)
      })()
    }, 4000)
    return () => clearInterval(timer)
  }, [route, materials, readiness.projects, refreshMaterials, refreshReadiness])

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
            onRefresh={async () => {
              const mats = await refreshMaterials(route.course.id)
              await refreshReadiness(route.course.id, mats)
            }}
          />
        ) : route.section === 'framework' ? (
          <FrameworkStep
            courseId={route.course.id}
            materials={materials}
            onDone={async () => {
              const mats = await refreshMaterials(route.course.id)
              await refreshReadiness(route.course.id, mats)
            }}
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
