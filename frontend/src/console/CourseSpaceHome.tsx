import { Pill } from './ui'
import type { CourseReadiness } from './types'
import type { CourseSection } from './nav'

export function CourseSpaceHome({ readiness, onOpenSection }: {
  readiness: CourseReadiness
  onOpenSection: (section: CourseSection) => void
}) {
  const inProgress = readiness.projects.filter((p) => p.status !== 'exported').length
  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>课程空间</h2>
        <div className="desc">长期可复用的课程级资产。维护一次，用于多个学期的命题。</div>
      </div>
      <div className="metric-row">
        <button className="metric" onClick={() => onOpenSection('materials')}>
          <span className="eyebrow">资料库</span>
          <Pill kind={readiness.materialsReady ? 'success' : 'neutral'}>
            {readiness.materialsReady ? '已就绪' : '未就绪'}
          </Pill>
          <span className="meta">四区文件 · 上传一次多次复用</span>
        </button>
        <button className="metric" onClick={() => onOpenSection('framework')}>
          <span className="eyebrow">命题框架</span>
          <Pill kind={readiness.frameworkReady ? 'success' : 'neutral'}>
            {readiness.frameworkReady ? `published ${readiness.frameworkVersion ?? ''}`.trim() : '未构建'}
          </Pill>
          <span className="meta">双大纲 → 考点表</span>
        </button>
        <button className="metric" onClick={() => onOpenSection('knowledge')}>
          <span className="eyebrow">知识目录</span>
          <Pill kind={readiness.knowledgeReady ? 'success' : 'neutral'}>
            {readiness.knowledgeReady ? `published ${readiness.knowledgeVersion ?? ''}`.trim() : '未发布'}
          </Pill>
          <span className="meta">
            {readiness.knowledgeReady
              ? `${readiness.knowledgeCardCount} 卡${readiness.knowledgeUngroundedCount > 0 ? ` · ${readiness.knowledgeUngroundedCount} 未落地` : ''}`
              : '图谱+树双视图'}
          </span>
        </button>
        <button className="metric" onClick={() => onOpenSection('projects')}>
          <span className="eyebrow">试卷项目</span>
          <Pill kind="neutral">{readiness.projects.length} 个</Pill>
          <span className="meta">{inProgress} 进行中</span>
        </button>
      </div>
    </div>
  )
}
