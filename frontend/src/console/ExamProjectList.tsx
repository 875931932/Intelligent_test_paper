import { useEffect, useState } from 'react'
import { Button, Card, EmptyState, LoadingLine, Notice, Pill } from './ui'
import { projectsApi } from './client'
import type { ExamProjectDetail } from './types'
import { ArrowUpRight, FileText } from 'lucide-react'

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
        <div><span className="eyebrow-label">课程资产 / 按学期归档</span><h2>试卷项目</h2><p className="desc">每个项目都是一份独立试卷，从蓝图到导出分步确认。</p></div>
      </div>
      {projects.length === 0 ? (
        <Card title="项目列表">
          <EmptyState>暂无试卷项目</EmptyState>
        </Card>
      ) : (
        <div className="project-grid">
          {projects.map((p) => (
            <button className="project-card" key={p.id} onClick={() => onOpenProject(p.id)}>
              <div className="project-card-main">
                <span className="project-card-icon"><FileText size={18} strokeWidth={1.8} /></span>
                <div><b>{p.name}</b><span>{p.semester_label || '未设置学期'}</span></div>
              </div>
              <div className="project-card-meta"><Pill kind={p.status === 'exported' ? 'success' : p.status === 'review' ? 'warning' : 'info'}>{STATUS_LABELS[p.status] ?? p.status}</Pill><span>{p.question_count} 题 · {p.total_score} 分</span><ArrowUpRight size={17} /></div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
