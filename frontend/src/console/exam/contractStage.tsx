import type { ExamProjectDetail } from '../types'
import { Button, EmptyState } from '../ui'

interface Props {
  project: ExamProjectDetail
  onGenerate: (projectId: string) => void
}

export function ContractStage({ project, onGenerate }: Props) {
  const contract = project.contract

  if (!contract) {
    return (
      <div className="stage-panel">
        <div className="stage-panel-head"><h3>合同阶段</h3></div>
        <EmptyState>合同未生成</EmptyState>
      </div>
    )
  }

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>合同阶段</h3>
        <span className="muted small">{contract.slots.length} 个题位 · 总分 {contract.total_score}</span>
      </div>

      <table className="slot-table">
        <thead>
          <tr>
            <th>#</th>
            <th>题型</th>
            <th>分值</th>
            <th>难度</th>
            <th>认知</th>
            <th>覆盖原子</th>
          </tr>
        </thead>
        <tbody>
          {contract.slots.map((slot) => (
            <tr key={slot.item_index}>
              <td>{slot.item_index}</td>
              <td>{slot.question_type}</td>
              <td>{slot.score}</td>
              <td>{slot.difficulty}</td>
              <td>{slot.cognitive_level}</td>
              <td>{slot.coverage_atom}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {contract.conflicts.length > 0 && (
        <div className="gate-banner">
          <span className="gate-icon">⚠</span>
          <div className="gate-text">
            <b>冲突 ({contract.conflicts.length})</b>
            <ul style={{ margin: '4px 0 0 16px' }}>
              {contract.conflicts.map((c, i) => (
                <li key={i}>{c.message}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div style={{ marginTop: 20 }}>
        <Button variant="primary" onClick={() => onGenerate(project.id)}>生成试卷</Button>
      </div>
    </div>
  )
}
