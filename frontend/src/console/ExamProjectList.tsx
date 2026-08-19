import { useEffect, useState } from 'react'
import { Card, EmptyState, LoadingLine, Notice } from './ui'
import { projectsApi } from './client'
import type { ExamProjectDetail } from './types'

interface Props {
  courseId: string
  onOpenProject: (projectId: string) => void
}

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', blueprint: '蓝图', contract: '合同', generating: '生成中', review: '审核', exported: '已导出',
}

export function ExamProjectList({ courseId, onOpenProject }: Props) {
  const [projects, setProjects] = useState<ExamProjectDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    projectsApi
      .list(courseId)
      .then((ps) => { if (!cancelled) setProjects(ps) })
      .catch(() => { if (!cancelled) setError('加载项目列表失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [courseId])

  if (loading) return <LoadingLine>加载项目列表…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>试卷项目</h2>
        <div className="desc">按学期归档的单次命题对象。进入后是 5 阶段生产线。</div>
      </div>
      {projects.length === 0 ? (
        <Card title="项目列表">
          <EmptyState>暂无试卷项目</EmptyState>
        </Card>
      ) : (
        <div>
          {projects.map((p) => (
            <div className="project-card" key={p.id} onClick={() => onOpenProject(p.id)}>
              <div>
                <b>{p.name}</b>
                <span className="muted small" style={{ marginLeft: 12 }}>{p.semester_label || '—'}</span>
              </div>
              <span className={`project-status ${p.status}`}>{STATUS_LABELS[p.status] ?? p.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
