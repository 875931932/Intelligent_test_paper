import type { ExamProjectDetail } from '../types'
import { Button, EmptyState } from '../ui'

interface Props {
  project: ExamProjectDetail
  onProceed: (projectId: string) => void
}

export function GenerationStage({ project, onProceed }: Props) {
  const gen = project.generation

  if (!gen) {
    return (
      <div className="stage-panel">
        <div className="stage-panel-head"><h3>生成阶段</h3></div>
        <EmptyState>尚未生成</EmptyState>
      </div>
    )
  }

  const pending = gen.questions.filter((q) => q.needs_review).length

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>生成阶段</h3>
        <span className="muted small">{gen.questions.length} 题 · {pending} 待审 · {gen.model_call_count} 次模型调用</span>
      </div>

      <div className="gen-progress">
        <div className="gen-bar">
          <div className="gen-bar-fill" style={{ width: gen.status === 'completed' ? '100%' : '60%' }} />
        </div>
        <span className="muted small">状态：{gen.status}</span>
      </div>

      <div style={{ marginTop: 20 }}>
        {gen.questions.map((q, i) => (
          <div className="q-preview" key={i}>
            <div className="q-stem"><span>{q.item_index}.</span> <span>{q.stem}</span></div>
            <div className="q-meta">
              <span className="q-tag">{q.question_type}</span>
              <span className="q-tag">{q.score} 分</span>
              {q.needs_review && <span className="q-tag warn">待审</span>}
            </div>
          </div>
        ))}
      </div>

      {gen.status === 'completed' && (
        <div style={{ marginTop: 20 }}>
          <Button variant="primary" onClick={() => onProceed(project.id)}>进入审核</Button>
        </div>
      )}
    </div>
  )
}
