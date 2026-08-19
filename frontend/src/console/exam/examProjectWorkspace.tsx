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

  const refresh = useCallback(async () => {
    try {
      const p = await projectsApi.get(courseId, projectId)
      setProject(p)
      setError('')
    } catch {
      setError('加载项目失败')
    } finally {
      setLoading(false)
    }
  }, [courseId, projectId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    projectsApi
      .get(courseId, projectId)
      .then((p) => { if (!cancelled) setProject(p) })
      .catch(() => { if (!cancelled) setError('加载项目失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [courseId, projectId])

  const handleConfirmBlueprint = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'contract')
    await refresh()
  }, [courseId, refresh])

  const handleGenerate = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'generating')
    await refresh()
  }, [courseId, refresh])

  const handleProceedToReview = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'review')
    await refresh()
  }, [courseId, refresh])

  const handleExport = useCallback(async (pid: string) => {
    await projectsApi.updateStatus(courseId, pid, 'exported')
    await refresh()
  }, [courseId, refresh])

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

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>{project.name}</h2>
        <div className="desc">{project.semester_label} · 总分 {project.total_score} · {project.question_count} 题</div>
      </div>

      <PipelineNav current={currentStage} completed={completedStages} onJump={handleJump} />

      {currentStage === 'blueprint' && (
        <BlueprintStage project={project} onConfirm={handleConfirmBlueprint} />
      )}
      {currentStage === 'contract' && (
        <ContractStage project={project} onGenerate={handleGenerate} />
      )}
      {currentStage === 'generating' && (
        <GenerationStage project={project} onProceed={handleProceedToReview} />
      )}
      {(currentStage === 'review' || currentStage === 'exported') && (
        <ReviewExportStage project={project} onExport={handleExport} />
      )}
    </div>
  )
}
