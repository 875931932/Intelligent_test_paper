import type { ExamProjectDetail } from '../types'
import { Button } from '../ui'

interface Props {
  project: ExamProjectDetail
  onConfirm: (projectId: string) => void
}

export function BlueprintStage({ project, onConfirm }: Props) {
  const confirmed = project.blueprint_confirmed

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>蓝图阶段</h3>
        <span className="muted small">总分 {project.total_score}</span>
      </div>

      <div className="form-grid">
        <div className="field">
          <label className="field-label">项目名称</label>
          <div className="field-value">{project.name}</div>
        </div>
        <div className="field">
          <label className="field-label">学期标签</label>
          <div className="field-value">{project.semester_label}</div>
        </div>
      </div>

      <div className={`gate-banner ${confirmed ? 'verified' : ''}`}>
        <span className="gate-icon">{confirmed ? '✓' : '⚠'}</span>
        <span className="gate-text">
          {confirmed ? '蓝图已确认，可进入合同阶段。' : '蓝图待确认。确认后不可修改蓝图设置。'}
        </span>
        {!confirmed && (
          <div className="gate-actions">
            <Button variant="primary" onClick={() => onConfirm(project.id)}>确认蓝图</Button>
          </div>
        )}
      </div>
    </div>
  )
}
