import { AlertCircle, Check, Circle, LoaderCircle } from 'lucide-react'
import type { WorkflowProgress, WorkflowProgressStatus } from './types'

type Props = {
  materialProgress: WorkflowProgress
  blueprintProgress: WorkflowProgress
  paperProgress: WorkflowProgress
}

const STEPS = [
  { key: 'materialProgress' as const, index: '01', title: '资料整理' },
  { key: 'blueprintProgress' as const, index: '02', title: '生成蓝图' },
  { key: 'paperProgress' as const, index: '03', title: '生成试卷' },
]

const STATUS_LABELS: Record<WorkflowProgressStatus, string> = {
  idle: '待开始',
  running: '进行中',
  success: '已完成',
  warning: '需处理',
  error: '失败',
}

function StatusIcon({ status }: { status: WorkflowProgressStatus }) {
  if (status === 'success') return <Check size={16} strokeWidth={2.4} aria-hidden="true" />
  if (status === 'running') return <LoaderCircle className="workflow-progress-spinner" size={16} strokeWidth={2.2} aria-hidden="true" />
  if (status === 'warning' || status === 'error') return <AlertCircle size={16} strokeWidth={2.2} aria-hidden="true" />
  return <Circle size={14} strokeWidth={1.8} aria-hidden="true" />
}

export function ProgressFeedback({ materialProgress, blueprintProgress, paperProgress }: Props) {
  const progress = { materialProgress, blueprintProgress, paperProgress }
  return (
    <section className="workflow-progress" aria-labelledby="workflow-progress-title">
      <div className="workflow-progress-head">
        <div>
          <span className="eyebrow-label">实时状态</span>
          <h3 id="workflow-progress-title">出卷进度</h3>
        </div>
        <span className="workflow-progress-caption">状态随服务端任务更新</span>
      </div>
      <div className="workflow-progress-track" role="status" aria-live="polite" aria-atomic="true">
        {STEPS.map(({ key, index, title }, stepIndex) => {
          const item = progress[key]
          const percent = Math.max(0, Math.min(100, Math.round(item.percent)))
          return (
            <div className="workflow-progress-step" key={key} data-status={item.status}>
              <div className="workflow-progress-step-head">
                <div className="workflow-progress-marker" aria-hidden="true"><StatusIcon status={item.status} /></div>
                <div className="workflow-progress-title"><span>{index}</span><strong>{title}</strong></div>
                <span className="workflow-progress-percent">{percent}%</span>
              </div>
              <div className="workflow-progress-bar" aria-hidden="true"><div style={{ width: `${percent}%` }} /></div>
              <div className="workflow-progress-message">
                <span>{STATUS_LABELS[item.status]}</span>
                <span title={item.detail ?? item.message}>{item.message}</span>
              </div>
              {stepIndex < STEPS.length - 1 ? <div className="workflow-progress-connector" aria-hidden="true" /> : null}
            </div>
          )
        })}
      </div>
    </section>
  )
}

export function InlineProgress({ label, message, percent, status = 'running' }: {
  label: string
  message: string
  percent?: number
  status?: WorkflowProgressStatus
}) {
  const bounded = typeof percent === 'number' ? Math.max(0, Math.min(100, Math.round(percent))) : null
  return (
    <div className={`inline-progress inline-progress-${status}`} role="status" aria-live="polite">
      <div className="inline-progress-head"><strong>{label}</strong>{bounded != null ? <span>{bounded}%</span> : null}</div>
      <div className={`inline-progress-bar${bounded == null ? ' is-indeterminate' : ''}`} role={bounded != null ? 'progressbar' : undefined} aria-valuemin={bounded != null ? 0 : undefined} aria-valuemax={bounded != null ? 100 : undefined} aria-valuenow={bounded != null ? bounded : undefined}>
        <div style={{ width: bounded == null ? '34%' : `${bounded}%` }} />
      </div>
      <span>{message}</span>
    </div>
  )
}
