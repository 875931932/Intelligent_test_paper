import { useEffect, useState, useCallback } from 'react'
import { projectsApi } from '../client'
import { LoadingLine, Notice } from '../ui'
import { PipelineNav } from './pipelineNav'
import { BlueprintStage } from './blueprintStage'
import { ContractStage } from './contractStage'
import { GenerationStage } from './generationStage'
import { ReviewExportStage } from './reviewExportStage'
import type { ExamProjectDetail, PipelineStage } from '../types'

const STATUS_TO_STAGE: Record<string, PipelineStage> = {
  blueprint: 'blueprint',
  contract: 'contract',
  generating: 'generating',
  review: 'review',
  exported: 'exported',
}

export function ExamProjectWorkspace({ courseId, projectId }: { courseId: string; projectId: string }) {
  const [project, setProject] = useState<ExamProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refreshProject = useCallback(async () => {
    try {
      const p = await projectsApi.get(courseId, projectId)
      setProject(p)
      setError('')
      return p
    } catch {
      setError('加载项目失败')
      return null
    }
  }, [courseId, projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    projectsApi
      .get(courseId, projectId)
      .then((p) => {
        if (!cancelled) {
          setProject(p)
          setError('')
        }
      })
      .catch(() => {
        if (!cancelled) setError('加载项目失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [courseId, projectId])

  // ── 四个阶段"进入下一步"回调 ──
  // 不再只调用 updateStatus；先让阶段内部完成对应 API（见各 *Stage.tsx），
  // 再统一刷新项目以展示后端真实状态与 active_*_id 字段。

  const handleConfirmBlueprint = useCallback(
    async (_pid: string) => {
      // BlueprintStage 已调用 confirmBlueprint；此处仅负责刷新项目状态
      const p = await refreshProject()
      if (p && !p.blueprint_confirmed) {
        // 兜底：若后端尚未自动推进，则尝试推进 status
        try {
          await projectsApi.updateStatus(courseId, projectId, 'contract')
          await refreshProject()
        } catch {
          /* ignore */
        }
      }
    },
    [courseId, projectId, refreshProject],
  )

  const handleGenerate = useCallback(
    async (_pid: string) => {
      // ContractStage 已调用 confirmContract；刷新项目，兜底推进到 generating
      const p = await refreshProject()
      if (p && p.status !== 'generating' && p.status !== 'review') {
        try {
          await projectsApi.updateStatus(courseId, projectId, 'generating')
          await refreshProject()
        } catch {
          /* ignore */
        }
      }
    },
    [courseId, projectId, refreshProject],
  )

  const handleProceedToReview = useCallback(
    async (_pid: string) => {
      // GenerationStage 已完成生成；刷新项目，兜底推进到 review
      const p = await refreshProject()
      if (p && p.status !== 'review' && p.status !== 'exported') {
        try {
          await projectsApi.updateStatus(courseId, projectId, 'review')
          await refreshProject()
        } catch {
          /* ignore */
        }
      }
    },
    [courseId, projectId, refreshProject],
  )

  const handleExport = useCallback(
    async (_pid: string) => {
      // ReviewExportStage 已调用 confirmPaperVersion；刷新项目，兜底推进到 exported
      const p = await refreshProject()
      if (p && p.status !== 'exported') {
        try {
          await projectsApi.updateStatus(courseId, projectId, 'exported')
          await refreshProject()
        } catch {
          /* ignore */
        }
      }
    },
    [courseId, projectId, refreshProject],
  )

  const handleJump = useCallback((_stage: PipelineStage) => {
    // 阶段跳转占位：由 PipelineNav 的 isClickable 控制可跳转范围
  }, [])

  if (loading) return <LoadingLine>加载项目…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>
  if (!project) return <Notice kind="warning">项目不存在</Notice>

  const currentStage = STATUS_TO_STAGE[project.status] ?? 'blueprint'
  const completedStages: PipelineStage[] = []
  const order: PipelineStage[] = ['blueprint', 'contract', 'generating', 'review', 'exported']
  const currentIdx = order.indexOf(currentStage)
  for (let i = 0; i < currentIdx; i++) completedStages.push(order[i])

  const extraIds = (project as unknown as {
    active_blueprint_version_id?: string | null
    active_contract_snapshot_id?: string | null
    active_paper_version_id?: string | null
    active_task_run_id?: string | null
  }) ?? {}

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>{project.name}</h2>
        <div className="desc">
          {project.semester_label} · 总分 {project.total_score} · {project.question_count} 题
        </div>
        <div className="muted small" style={{ marginTop: 4 }} data-testid="active-ids">
          状态：{project.status}
          {extraIds.active_blueprint_version_id
            ? ` · 蓝图版本 ${extraIds.active_blueprint_version_id}`
            : ''}
          {extraIds.active_paper_version_id
            ? ` · 试卷版本 ${extraIds.active_paper_version_id}`
            : ''}
          {extraIds.active_task_run_id ? ` · 任务 ${extraIds.active_task_run_id}` : ''}
        </div>
      </div>

      <PipelineNav current={currentStage} completed={completedStages} onJump={handleJump} />

      {currentStage === 'blueprint' && (
        <BlueprintStage courseId={courseId} project={project} onConfirm={handleConfirmBlueprint} />
      )}
      {currentStage === 'contract' && (
        <ContractStage courseId={courseId} project={project} onGenerate={handleGenerate} />
      )}
      {currentStage === 'generating' && (
        <GenerationStage courseId={courseId} project={project} onProceed={handleProceedToReview} />
      )}
      {(currentStage === 'review' || currentStage === 'exported') && (
        <ReviewExportStage courseId={courseId} project={project} onExport={handleExport} />
      )}
    </div>
  )
}
