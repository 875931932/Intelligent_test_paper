import { useCallback, useEffect, useState } from 'react'
import { examPipelineApi } from '../client'
import type { ExamProjectDetail } from '../types'
import { Button, EmptyState, Notice } from '../ui'

type Slot = Record<string, unknown> & {
  item_index?: number
  question_type?: string
  score?: number
  difficulty?: string
  cognitive_level?: string
  card_id?: string
  knowledge_card_id?: string
}

interface Props {
  courseId: string
  project: ExamProjectDetail
  onGenerate: (projectId: string) => void
}

export function ContractStage({ courseId, project, onGenerate }: Props) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [usedThreshold, setUsedThreshold] = useState(0)
  const [conflictsHistory, setConflictsHistory] = useState<Array<[number, number]>>([])
  const [slots, setSlots] = useState<Slot[]>([])
  const [slotRevisions, setSlotRevisions] = useState<Array<unknown>>([])
  const [confirmLoading, setConfirmLoading] = useState(false)
  const [reviseLoading, setReviseLoading] = useState(false)

  const runAllocate = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await examPipelineApi.allocateContract(courseId, project.id)
      setUsedThreshold(result.used_threshold)
      setConflictsHistory(result.conflicts_history)
      setSlots(result.contract_snapshot.slots as Slot[])
    } catch (err) {
      const msg = err instanceof Error ? err.message : '分配合同失败'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [courseId, project.id])

  useEffect(() => {
    void runAllocate()
  }, [runAllocate])

  const handleRevise = useCallback(async () => {
    if (slots.length === 0) return
    setReviseLoading(true)
    setError('')
    try {
      const first = slots[0]
      const revision = {
        item_index: first.item_index,
        knowledge_card_id: `revised-${first.card_id ?? first.knowledge_card_id ?? 'card'}`,
      }
      const result = await examPipelineApi.reviseContract(courseId, project.id, {
        slot_revisions: [revision],
      })
      const snapshot = (result.revised_contract_snapshot as { slots?: Slot[] } | undefined) ?? {}
      if (Array.isArray(snapshot.slots)) {
        setSlots(snapshot.slots)
      }
      setSlotRevisions((prev) => [...prev, revision])
    } catch (err) {
      const msg = err instanceof Error ? err.message : '修订合同失败'
      setError(msg)
    } finally {
      setReviseLoading(false)
    }
  }, [courseId, project.id, slots])

  const handleConfirm = useCallback(async () => {
    setConfirmLoading(true)
    setError('')
    try {
      await examPipelineApi.confirmContract(courseId, project.id, {
        slot_revisions: slotRevisions,
      })
      onGenerate(project.id)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '确认合同失败'
      setError(msg)
    } finally {
      setConfirmLoading(false)
    }
  }, [courseId, project.id, slotRevisions, onGenerate])

  if (loading) {
    return (
      <div className="stage-panel">
        <div className="stage-panel-head">
          <h3>合同阶段</h3>
          <span className="muted small">分配中…</span>
        </div>
        <EmptyState>分配中…</EmptyState>
      </div>
    )
  }

  const totalScore = slots.reduce((s, it) => s + (Number(it.score) || 0), 0)
  const groupedCount = slots.reduce<Record<string, number>>((acc, it) => {
    const t = String(it.question_type ?? 'unknown')
    acc[t] = (acc[t] ?? 0) + 1
    return acc
  }, {})

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>合同阶段</h3>
        <span className="muted small">
          {slots.length} 个题位 · 总分 {totalScore} · 阈值 {usedThreshold}
        </span>
      </div>

      {error && (
        <Notice kind="error" role="alert">
          {error}
        </Notice>
      )}

      {slots.length === 0 ? (
        <EmptyState>合同未生成</EmptyState>
      ) : (
        <>
          <div className="muted small" style={{ marginBottom: 8 }}>
            题型分配：
            {Object.entries(groupedCount)
              .map(([t, n]) => `${t} ×${n}`)
              .join('，')}
          </div>

          <table className="slot-table" data-testid="contract-slots-table">
            <thead>
              <tr>
                <th>#</th>
                <th>题型</th>
                <th>分值</th>
                <th>难度</th>
                <th>认知</th>
                <th>知识卡</th>
              </tr>
            </thead>
            <tbody>
              {slots.map((slot, idx) => (
                <tr key={`${slot.item_index ?? idx}`} data-testid={`contract-slot-${slot.item_index ?? idx}`}>
                  <td>{slot.item_index ?? idx + 1}</td>
                  <td>{String(slot.question_type ?? '-')}</td>
                  <td>{String(slot.score ?? '-')}</td>
                  <td>{String(slot.difficulty ?? '-')}</td>
                  <td>{String(slot.cognitive_level ?? '-')}</td>
                  <td>{String(slot.card_id ?? slot.knowledge_card_id ?? '-')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {conflictsHistory.length > 0 && (
        <div className="gate-banner" style={{ marginTop: 16 }} data-testid="conflicts-history">
          <span className="gate-icon">⚠</span>
          <div className="gate-text">
            <b>冲突历史（{conflictsHistory.length}）</b>
            <ul style={{ margin: '4px 0 0 16px' }}>
              {conflictsHistory.map((c, i) => (
                <li key={i}>
                  运行 {c[0]}：{c[1]} 处冲突
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div className="stage-actions">
        <Button variant="secondary" onClick={handleRevise} loading={reviseLoading} disabled={slots.length === 0}>
          修订（演示：修改首个题位卡）
        </Button>
        <Button
          variant="primary"
          onClick={handleConfirm}
          loading={confirmLoading}
          disabled={slots.length === 0}
          data-testid="confirm-contract-btn"
        >
          确认合同
        </Button>
      </div>
    </div>
  )
}
