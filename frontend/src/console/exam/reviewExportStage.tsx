import type { ExamProjectDetail } from '../types'
import { Button } from '../ui'

interface Props {
  project: ExamProjectDetail
  onExport: (projectId: string) => void
}

export function ReviewExportStage({ project, onExport }: Props) {
  const gen = project.generation
  const pending = gen?.questions.filter((q) => q.needs_review).length ?? 0
  const exported = project.version_confirmed

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>审核与导出</h3>
        <span className="muted small">{project.question_count} 题 · {pending} 待审</span>
      </div>

      {gen && (
        <div style={{ marginBottom: 20 }}>
          {gen.questions.map((q, i) => (
            <div className="q-preview" key={i}>
              <div className="q-stem">{q.item_index}. {q.stem}</div>
              <div className="q-meta">
                <span className="q-tag">{q.question_type}</span>
                <span className="q-tag">{q.score} 分</span>
                {q.needs_review && <span className="q-tag warn">待审</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className={`gate-banner ${exported ? 'verified' : ''}`}>
        <span className="gate-icon">{exported ? '✓' : '⚠'}</span>
        <span className="gate-text">
          {exported
            ? '已导出。试卷版本已确认，可交付使用。'
            : '确认试卷版本后将正式导出。确认后不可修改题目。'}
        </span>
        {!exported && (
          <div className="gate-actions">
            <Button variant="primary" onClick={() => onExport(project.id)}>确认导出</Button>
          </div>
        )}
      </div>
    </div>
  )
}
