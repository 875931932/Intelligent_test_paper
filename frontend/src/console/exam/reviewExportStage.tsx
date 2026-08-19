import { useCallback, useEffect, useMemo, useState } from 'react'
import { examPipelineApi } from '../client'
import type { ExamProjectDetail } from '../types'
import { Button, Notice, Field } from '../ui'

type PaperItem = {
  item_index?: number
  question_type?: string
  stem?: string
  options?: unknown
  answer?: unknown
  score?: number
  exam_point_id?: string | null
  card_id?: string | null
  quality?: Record<string, unknown> | null
  teacher_override?: Record<string, unknown> | null
  finalized_text?: string | null
  needs_review?: boolean
  needs_review_reason?: string | null
}

type NeedsReviewItem = {
  item_index?: number
  question_type?: string
  reason?: string
  quality_message?: string
  exam_point_id?: string | null
  card_id?: string | null
}

interface Props {
  courseId: string
  project: ExamProjectDetail
  onExport: (projectId: string) => void
}

function pvIdOf(project: ExamProjectDetail): string | null {
  // Prefer project field (added by backend), fallback to generation->paper_version_id, fallback hard-coded
  const proj = project as unknown as { active_paper_version_id?: string | null }
  if (proj.active_paper_version_id) return proj.active_paper_version_id
  const gen = (project.generation as { paper_version_id?: string } | undefined) ?? {}
  if (gen.paper_version_id) return gen.paper_version_id
  return null
}

export function ReviewExportStage({ courseId, project, onExport }: Props) {
  const [pvId, setPvId] = useState<string | null>(pvIdOf(project))
  const [loading, setLoading] = useState(false)
  const [items, setItems] = useState<PaperItem[]>([])
  const [needsReview, setNeedsReview] = useState<NeedsReviewItem[]>([])
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [forceDialog, setForceDialog] = useState(false)
  const [confirmLoading, setConfirmLoading] = useState(false)
  const [revertLoading, setRevertLoading] = useState(false)
  const [finalized, setFinalized] = useState<boolean>(project.version_confirmed)

  // Inline override editor state
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [overrideStem, setOverrideStem] = useState('')
  const [overrideAnswer, setOverrideAnswer] = useState('')
  const [patchLoading, setPatchLoading] = useState(false)

  useEffect(() => {
    void (async () => {
      setLoading(true)
      try {
        let localPvId = pvIdOf(project)
        if (!localPvId) {
          const pv = (await examPipelineApi.getCurrentPaperVersion(courseId, project.id)) as {
            id?: string
            status?: string
            items?: PaperItem[]
          }
          if (pv?.id) {
            localPvId = pv.id
            setPvId(pv.id)
          }
          if (Array.isArray(pv?.items)) setItems(pv.items as PaperItem[])
        } else {
          const pv = (await examPipelineApi.getCurrentPaperVersion(courseId, project.id)) as {
            id?: string
            status?: string
            items?: PaperItem[]
          }
          if (Array.isArray(pv?.items)) setItems(pv.items as PaperItem[])
        }
        if (localPvId) {
          try {
            const list = (await examPipelineApi.listNeedsReview(courseId, localPvId)) as NeedsReviewItem[]
            setNeedsReview(Array.isArray(list) ? list : [])
          } catch {
            /* swallow if endpoint not yet supported */
          }
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : '加载试卷版本失败')
      } finally {
        setLoading(false)
      }
    })()
  }, [courseId, project.id, project])

  const pendingCount = useMemo(
    () => items.filter((it) => it.needs_review).length,
    [items],
  )

  const handleSelectItem = useCallback((idx: number) => {
    setSelectedIndex(idx)
    const it = items.find((i) => i.item_index === idx) ?? items.find((_, i) => i === idx - 1)
    setOverrideStem(it?.stem ?? '')
    setOverrideAnswer(it?.answer != null ? String(it.answer) : '')
  }, [items])

  const handlePatchClear = useCallback(
    async (itemIndex: number) => {
      if (!pvId) {
        setError('试卷版本未就绪')
        return
      }
      setPatchLoading(true)
      setError('')
      try {
        const patch: Record<string, unknown> = {}
        if (overrideStem) patch.stem = overrideStem
        if (overrideAnswer) patch.answer = overrideAnswer
        const res = (await examPipelineApi.patchPaperItem(courseId, pvId, itemIndex, {
          teacher_override_patch: patch,
          clear_needs_review: true,
        })) as PaperItem
        setItems((prev) =>
          prev.map((it) => ((it.item_index ?? -1) === itemIndex ? { ...it, ...res, needs_review: false } : it)),
        )
        setNeedsReview((prev) => prev.filter((n) => (n.item_index ?? -1) !== itemIndex))
        setSuccess(`已更新第 ${itemIndex} 题`)
        setTimeout(() => setSuccess(''), 2500)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新题目失败')
      } finally {
        setPatchLoading(false)
      }
    },
    [courseId, pvId, overrideStem, overrideAnswer],
  )

  const handleQuickClear = useCallback(
    async (itemIndex: number) => {
      if (!pvId) {
        setError('试卷版本未就绪')
        return
      }
      try {
        await examPipelineApi.patchPaperItem(courseId, pvId, itemIndex, {
          teacher_override_patch: {},
          clear_needs_review: true,
        })
        setItems((prev) =>
          prev.map((it) => ((it.item_index ?? -1) === itemIndex ? { ...it, needs_review: false } : it)),
        )
        setNeedsReview((prev) => prev.filter((n) => (n.item_index ?? -1) !== itemIndex))
      } catch (err) {
        setError(err instanceof Error ? err.message : '清除待审标记失败')
      }
    },
    [courseId, pvId],
  )

  const runConfirm = useCallback(
    async (force = false) => {
      if (!pvId) {
        setError('试卷版本未就绪')
        return
      }
      setConfirmLoading(true)
      setError('')
      setForceDialog(false)
      try {
        await examPipelineApi.confirmPaperVersion(courseId, pvId, {
          force_ignore_needs_review: force ? true : undefined,
        })
        setFinalized(true)
        setSuccess('试卷终版已确认')
        onExport(project.id)
      } catch (err) {
        const message = err instanceof Error ? err.message : '确认失败'
        // 409 behavior: open force confirm dialog
        if (message.includes('409') || /needs.?review|pending/i.test(message)) {
          setForceDialog(true)
        } else {
          setError(message)
        }
      } finally {
        setConfirmLoading(false)
      }
    },
    [courseId, pvId, onExport, project.id],
  )

  const handleRevert = useCallback(async () => {
    if (!pvId) return
    setRevertLoading(true)
    setError('')
    try {
      await examPipelineApi.revertPaperVersion(courseId, pvId)
      setFinalized(false)
      setSuccess('已撤销确认，回到候选状态')
      setTimeout(() => setSuccess(''), 2500)
    } catch (err) {
      setError(err instanceof Error ? err.message : '撤销失败')
    } finally {
      setRevertLoading(false)
    }
  }, [courseId, pvId])

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>审核与导出</h3>
        <span className="muted small">
          {items.length} 题 · {pendingCount} 待审 · {finalized ? '已确认' : '候选状态'}
        </span>
      </div>

      {error && (
        <Notice kind="error" role="alert" data-testid="review-error">
          {error}
        </Notice>
      )}
      {success && (
        <Notice kind="success" data-testid="review-success">
          {success}
        </Notice>
      )}
      {loading && items.length === 0 && <div className="loading-line muted small">加载试卷版本…</div>}

      {needsReview.length > 0 && !finalized && (
        <div className="card" style={{ marginTop: 12 }} data-testid="needs-review-list">
          <div className="card-head">
            <div>
              <h4 style={{ margin: 0 }}>待审核列表（优先处理）</h4>
              <div className="sub muted small">共 {needsReview.length} 项需要教师确认</div>
            </div>
          </div>
          <div className="card-body">
            <ul style={{ margin: 0, padding: '0 0 0 18px' }}>
              {needsReview.map((n, i) => {
                const idx = n.item_index ?? i + 1
                return (
                  <li key={i} data-testid={`review-item-${idx}`} style={{ marginBottom: 8 }}>
                    <div>
                      <b>第 {idx} 题</b> · {n.question_type ?? ''}
                      <div className="muted small">{n.reason ?? n.quality_message ?? '无详情'}</div>
                    </div>
                    <div style={{ marginTop: 4 }}>
                      <Button
                        size="sm"
                        variant="secondary"
                        data-testid={`clear-review-${idx}`}
                        onClick={() => handleQuickClear(idx)}
                      >
                        标记已审阅
                      </Button>
                    </div>
                  </li>
                )
              })}
            </ul>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <div style={{ marginBottom: 20 }} data-testid="review-item-list">
          {items.map((q, i) => {
            const idx = q.item_index ?? i + 1
            return (
              <div className="q-preview" key={`${idx}`} data-testid={`r-item-${idx}`}>
                <div className="q-stem">
                  {idx}. {q.stem ?? '(题干缺失)'}
                </div>
                <div className="q-meta">
                  <span className="q-tag">{q.question_type ?? 'unknown'}</span>
                  <span className="q-tag">{q.score ?? 0} 分</span>
                  {q.needs_review ? (
                    <span className="q-tag warn" data-testid={`r-badge-${idx}`}>
                      待审
                    </span>
                  ) : null}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => handleSelectItem(idx)}
                    data-testid={`select-edit-${idx}`}
                  >
                    编辑
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {selectedIndex != null && !finalized && (
        <div className="card" style={{ marginBottom: 20 }} data-testid="override-editor">
          <div className="card-head">
            <div>
              <h4 style={{ margin: 0 }}>编辑第 {selectedIndex} 题</h4>
              <div className="sub muted small">覆盖题干或答案；保存后自动清除待审</div>
            </div>
          </div>
          <div className="card-body">
            <div className="form-grid">
              <Field label="题干覆盖">
                <textarea
                  className="text-input"
                  rows={3}
                  data-testid="override-stem"
                  value={overrideStem}
                  onChange={(e) => setOverrideStem(e.target.value)}
                />
              </Field>
              <Field label="答案覆盖">
                <input
                  type="text"
                  className="text-input"
                  data-testid="override-answer"
                  value={overrideAnswer}
                  onChange={(e) => setOverrideAnswer(e.target.value)}
                />
              </Field>
            </div>
            <div style={{ marginTop: 12 }}>
              <Button
                variant="primary"
                size="sm"
                loading={patchLoading}
                onClick={() => handlePatchClear(selectedIndex)}
                data-testid="save-override-btn"
              >
                保存并清除待审
              </Button>
            </div>
          </div>
        </div>
      )}

      {forceDialog && (
        <div className="card" style={{ marginBottom: 20, borderColor: '#f5c26b' }} role="dialog" data-testid="force-dialog">
          <div className="card-head">
            <div>
              <h4 style={{ margin: 0 }}>仍有待审项目未解决</h4>
              <div className="sub muted small">您可以继续处理，或强制确认并忽略它们。</div>
            </div>
          </div>
          <div className="card-body" style={{ display: 'flex', gap: 12 }}>
            <Button variant="secondary" onClick={() => setForceDialog(false)}>
              返回处理
            </Button>
            <Button variant="danger" loading={confirmLoading} onClick={() => runConfirm(true)} data-testid="force-confirm-btn">
              强制确认
            </Button>
          </div>
        </div>
      )}

      <div className={`gate-banner ${finalized ? 'verified' : ''}`}>
        <span className="gate-icon">{finalized ? '✓' : '⚠'}</span>
        <span className="gate-text">
          {finalized ? '已导出。试卷版本已确认，可交付使用。' : '确认试卷版本后将正式导出。确认后不可修改题目。'}
        </span>
        {!finalized ? (
          <div className="gate-actions">
            <Button variant="primary" onClick={() => runConfirm(false)} loading={confirmLoading} data-testid="confirm-final-btn">
              确认终版
            </Button>
          </div>
        ) : (
          <div className="gate-actions">
            <Button variant="danger-ghost" onClick={handleRevert} loading={revertLoading} data-testid="revert-btn">
              撤销确认（回到候选）
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
