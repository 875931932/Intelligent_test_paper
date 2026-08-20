import { Pill } from './ui'
import type { CourseReadiness } from './types'
import type { CourseSection } from './nav'
import { ArrowRight, Boxes, ClipboardList, FileText, Network } from 'lucide-react'
import { ProgressFeedback } from './ProgressFeedback'

const WORKFLOW = [
  { key: 'materials' as const, title: '资料库', kicker: '01 · 准备素材', description: '上传考纲、教案和教学材料，解析状态集中查看。', icon: FileText },
  { key: 'framework' as const, title: '命题框架', kicker: '02 · 明确考查范围', description: '从考核大纲整理考点、权重和认知要求。', icon: ClipboardList },
  { key: 'knowledge' as const, title: '知识目录', kicker: '03 · 形成知识底座', description: '只浏览已确认的纯净知识卡与来源证据。', icon: Network },
  { key: 'projects' as const, title: '试卷项目', kicker: '04 · 生成与审核', description: '按学期创建试卷，逐步确认蓝图、合同和成卷。', icon: Boxes },
]

export function CourseSpaceHome({ readiness, onOpenSection }: {
  readiness: CourseReadiness
  onOpenSection: (section: CourseSection) => void
}) {
  const inProgress = readiness.projects.filter((p) => p.status !== 'exported').length
  const idle = { percent: 0, status: 'idle' as const, message: '尚未开始' }
  return (
    <div className="content-inner">
      <div className="workspace-hero">
        <div>
          <span className="eyebrow-label">课程空间</span>
          <h2>把一次准备，变成持续可用的命题底座</h2>
          <p>课程资料长期复用；每份试卷独立归档。按顺序完成准备，系统只在下一步需要时展示信息。</p>
        </div>
        <div className="workspace-hero-note">
          <span>当前状态</span>
          <strong>{readiness.knowledgeReady ? '可以开始出卷' : readiness.frameworkReady ? '等待知识目录发布' : readiness.materialsReady ? '等待构建命题框架' : '从上传资料开始'}</strong>
        </div>
      </div>
      <ProgressFeedback
        materialProgress={readiness.materialProgress ?? idle}
        blueprintProgress={readiness.blueprintProgress ?? idle}
        paperProgress={readiness.paperProgress ?? idle}
      />
      <section className="workflow-section">
        <div className="section-bar">
          <div><h3>命题流程</h3><span>从课程资产到可打印试卷</span></div>
          <span className="section-bar-note">点击下一步继续</span>
        </div>
        <div className="workflow-grid">
          {WORKFLOW.map(({ key, title, kicker, description, icon: Icon }) => {
            const status = key === 'materials'
              ? readiness.materialsReady ? '已就绪' : '待准备'
              : key === 'framework'
                ? readiness.frameworkReady ? `published ${readiness.frameworkVersion ?? ''}`.trim() : '待构建'
                : key === 'knowledge'
                  ? readiness.knowledgeReady ? `published ${readiness.knowledgeVersion ?? ''}`.trim() : '未发布'
                  : `${readiness.projects.length} 个`
            const kind = key === 'materials' ? readiness.materialsReady : key === 'framework' ? readiness.frameworkReady : key === 'knowledge' ? readiness.knowledgeReady : false
            return (
              <button className="workflow-card" key={key} onClick={() => onOpenSection(key)}>
                <div className="workflow-card-top"><span className="workflow-icon"><Icon size={18} strokeWidth={1.8} /></span><span className="workflow-kicker">{kicker}</span><ArrowRight className="workflow-arrow" size={17} /></div>
                <h3>{title}</h3>
                <p>{description}</p>
                <div className="workflow-card-foot">
                  <Pill kind={kind ? 'success' : 'neutral'}>{status}</Pill>
                  {key === 'knowledge' && readiness.knowledgeReady ? <span>{readiness.knowledgeCardCount} 卡{readiness.knowledgeUngroundedCount > 0 ? ` · ${readiness.knowledgeUngroundedCount} 未落地` : ''}</span> : null}
                  {key === 'projects' ? <span>{inProgress} 进行中</span> : null}
                </div>
              </button>
            )
          })}
        </div>
      </section>
      <div className="overview-note">
        <strong>建议顺序</strong><span>先确认命题框架，再整理教学材料；知识目录发布后，才会进入试卷项目。</span>
      </div>
    </div>
  )
}
