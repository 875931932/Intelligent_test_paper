/** 步骤二 · 考纲框架：双大纲语义提取 → 教师审阅权重与冲突 → 确认发布。 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { frameworkApi } from '../client'
import type { AssessmentAnchor, ExamPoint, FrameworkCandidate, Material } from '../types'
import { Button, Card, EmptyState, Field, Notice, Pill } from '../ui'

/** 从已解析就绪的资料中筛出指定类型。 */
function readyOf(materials: Material[], type: string): Material[] {
  return materials.filter((m) => m.material_type === type && m.parse_status?.status === 'ready')
}

export function FrameworkStep({ courseId, materials, onDone }: {
  courseId: string
  materials: Material[]
  onDone: () => Promise<void>
}) {
  const teaching = useMemo(() => readyOf(materials, 'teaching_syllabus'), [materials])
  const assessment = useMemo(() => readyOf(materials, 'assessment_syllabus'), [materials])

  const [teachingId, setTeachingId] = useState('')
  const [assessmentId, setAssessmentId] = useState('')
  const [running, setRunning] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [runId, setRunId] = useState('')
  const [candidate, setCandidate] = useState<FrameworkCandidate | null>(null)
  const [anchorWeights, setAnchorWeights] = useState<Record<string, number>>({})
  const [resolutions, setResolutions] = useState<Record<string, string>>({})
  const [showExamPoints, setShowExamPoints] = useState(false)

  // 自动恢复：进入页面时加载最近的候选
  useEffect(() => {
    if (candidate) return
    let cancelled = false
    ;(async () => {
      try {
        const latest = await frameworkApi.getLatestRun(courseId)
        if (cancelled || !latest) return
        if (latest.status === 'awaiting_teacher_confirmation') {
          const data = await frameworkApi.getCandidate(courseId, latest.id)
          if (cancelled) return
          setRunId(latest.id)
          setCandidate(data)
          setAnchorWeights(Object.fromEntries(data.anchors.map((a) => [a.key, a.exam_weight])))
        }
      } catch {
        // 无历史 run 或加载失败，静默处理
      }
    })()
    return () => { cancelled = true }
  }, [courseId, candidate])

  const openConflicts = candidate?.conflicts?.filter((c) => c.status === 'open') ?? []
  const weightTotal = Object.values(anchorWeights).reduce((sum, w) => sum + (Number.isFinite(w) ? w : 0), 0)
  const weightsOk = Math.abs(weightTotal - 100) < 0.01
  const conflictsOk = openConflicts.every((c) => (resolutions[c.key] ?? '').trim().length > 0)

  const run = useCallback(async () => {
    if (!teachingId || !assessmentId) {
      setError('请选择教学大纲与考核大纲各一份（需解析就绪）。')
      return
    }
    setRunning(true)
    setError('')
    setInfo('')
    setCandidate(null)
    try {
      const created = await frameworkApi.createRun(courseId, teachingId, assessmentId)
      setRunId(created.run_id)
      const data = await frameworkApi.getCandidate(courseId, created.run_id)
      setCandidate(data)
      setAnchorWeights(Object.fromEntries(data.anchors.map((a) => [a.key, a.exam_weight])))
      setResolutions({})
      setInfo(
        data.conflicts.some((c) => c.status === 'open')
          ? '框架候选已生成，存在待处理冲突：请填写处理说明并核对权重（合计须为 100）。'
          : '框架候选已生成，请核对锚点权重与考点后确认发布。',
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : '框架提取失败')
    } finally {
      setRunning(false)
    }
  }, [courseId, teachingId, assessmentId])

  const confirm = useCallback(async () => {
    if (!candidate || !runId) return
    if (!weightsOk) {
      setError(`锚点权重合计为 ${weightTotal.toFixed(1)}，须为 100 后才能确认。`)
      return
    }
    if (!conflictsOk) {
      setError('所有未解决冲突都需要填写处理说明。')
      return
    }
    setConfirming(true)
    setError('')
    try {
      const anchors: AssessmentAnchor[] = candidate.anchors.map((anchor) => ({
        ...anchor,
        exam_weight: anchorWeights[anchor.key] ?? anchor.exam_weight,
      }))
      await frameworkApi.confirm(courseId, runId, {
        anchors,
        exam_points: candidate.exam_points,
        conflict_resolutions: resolutions,
        teacher_exclusions: [],
      })
      setInfo('考纲框架已确认发布，可以进入「知识整理」。')
      setCandidate(null)
      await onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : '框架确认失败')
    } finally {
      setConfirming(false)
    }
  }, [candidate, runId, courseId, anchorWeights, resolutions, weightsOk, conflictsOk, weightTotal, onDone])

  const reject = useCallback(async () => {
    if (!runId) return
    setConfirming(true)
    try {
      await frameworkApi.reject(courseId, runId)
      setCandidate(null)
      setInfo('已驳回本次框架候选，可调整资料后重新提取。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '驳回失败')
    } finally {
      setConfirming(false)
    }
  }, [courseId, runId])

  if (teaching.length === 0 || assessment.length === 0) {
    return (
      <Card title="考纲框架" sub="需要教学大纲与考核大纲各一份且解析就绪。">
        <EmptyState>
          {teaching.length === 0 ? '缺少解析就绪的教学大纲。' : ''}
          {teaching.length === 0 && assessment.length === 0 ? ' ' : ''}
          {assessment.length === 0 ? '缺少解析就绪的考核大纲。' : ''}
          请先在「课程资料」步骤完成上传与解析。
        </EmptyState>
      </Card>
    )
  }

  const hasCandidate = candidate && candidate.anchors && candidate.exam_points

  return (
    <>
      <Card title="提取考纲框架" sub="从教学大纲提取教学主题，从考核大纲提取考核锚点与考点，并交叉比对。">
        <div className="form-row">
          <Field label="教学大纲">
            <select className="select" value={teachingId} onChange={(e) => setTeachingId(e.target.value)}>
              <option value="">选择教学大纲…</option>
              {teaching.map((m) => (
                <option key={m.id} value={m.latest_version!.id}>{m.logical_name}</option>
              ))}
            </select>
          </Field>
          <Field label="考核大纲">
            <select className="select" value={assessmentId} onChange={(e) => setAssessmentId(e.target.value)}>
              <option value="">选择考核大纲…</option>
              {assessment.map((m) => (
                <option key={m.id} value={m.latest_version!.id}>{m.logical_name}</option>
              ))}
            </select>
          </Field>
          <Button variant="primary" loading={running} disabled={!teachingId || !assessmentId} onClick={() => void run()}>
            提取框架
          </Button>
        </div>
      </Card>

      {error ? <Notice kind="error">{error}</Notice> : null}
      {info ? <Notice kind={hasCandidate ? 'info' : 'success'}>{info}</Notice> : null}

      {hasCandidate ? (
        <div className="framework-layout">
          {/* 左侧：摘要 + 操作 */}
          <aside className="fw-sidebar">
            <div className="fw-summary">
              <h3>概览</h3>
              <div className="fw-stat-grid">
                <div className="fw-stat">
                  <div className="fw-stat-num">{candidate.anchors.length}</div>
                  <div className="fw-stat-label">考核锚点</div>
                </div>
                <div className="fw-stat">
                  <div className="fw-stat-num">{candidate.exam_points.length}</div>
                  <div className="fw-stat-label">考点</div>
                </div>
                <div className="fw-stat">
                  <div className="fw-stat-num">{candidate.teaching_topics.length}</div>
                  <div className="fw-stat-label">教学主题</div>
                </div>
                <div className="fw-stat">
                  <div className={`fw-stat-num ${openConflicts.length > 0 ? 'text-warn' : 'text-ok'}`}>{openConflicts.length}</div>
                  <div className="fw-stat-label">待处理冲突</div>
                </div>
              </div>

              <div className="fw-weight-meter">
                <div className="fw-meter-label">
                  <span>权重合计</span>
                  <span className={weightsOk ? 'text-ok' : 'text-warn'}>{weightTotal.toFixed(1)} / 100</span>
                </div>
                <div className="fw-meter-bar">
                  <div
                    className={`fw-meter-fill ${weightsOk ? 'ok' : 'warn'}`}
                    style={{ width: `${Math.min(weightTotal, 100)}%` }}
                  />
                </div>
              </div>
            </div>

            <div className="fw-actions">
              <Button
                variant="primary"
                loading={confirming}
                disabled={!weightsOk || !conflictsOk}
                onClick={() => void confirm()}
              >
                确认并发布框架
              </Button>
              <Button variant="secondary" loading={confirming} onClick={() => void reject()}>
                驳回候选
              </Button>
              {!weightsOk ? <div className="fw-hint">权重合计须为 100</div> : null}
              {!conflictsOk ? <div className="fw-hint">请处理所有冲突</div> : null}
            </div>
          </aside>

          {/* 中间：主要内容 */}
          <div className="fw-main">
            <Card title="考核锚点权重" sub="调整每个锚点权重，合计须为 100。">
              <div className="fw-anchor-grid">
                {candidate.anchors.map((anchor) => (
                  <div key={anchor.key} className="fw-anchor-card">
                    <div className="fw-anchor-title">{anchor.title}</div>
                    <div className="fw-anchor-key mono">{anchor.key}</div>
                    <div className="fw-anchor-meta">{anchor.ability_requirements.join('；') || '—'}</div>
                    <div className="fw-anchor-weight">
                      <input
                        className="input"
                        type="number"
                        min={0}
                        max={100}
                        step={0.5}
                        value={anchorWeights[anchor.key] ?? anchor.exam_weight}
                        onChange={(e) =>
                          setAnchorWeights({ ...anchorWeights, [anchor.key]: Number(e.target.value) })
                        }
                      />
                      <span className="fw-anchor-weight-pct">%</span>
                    </div>
                  </div>
                ))}
              </div>
            </Card>

            <Card
              title={`考点 ${candidate.exam_points.length} 个`}
              sub="按锚点分组的考点列表。"
              actions={
                <button className="collapse-toggle" onClick={() => setShowExamPoints(!showExamPoints)}>
                  {showExamPoints ? '收起' : '展开'}
                </button>
              }
            >
              {showExamPoints ? (
                <div className="fw-exam-points">
                  {candidate.anchors.map((anchor) => {
                    const points = candidate.exam_points.filter((p) => p.anchor_key === anchor.key)
                    if (points.length === 0) return null
                    return (
                      <div key={anchor.key} className="fw-ep-group">
                        <div className="fw-ep-group-title">{anchor.title} <span className="muted">({points.length})</span></div>
                        <ul className="fw-ep-list">
                          {points.map((p) => (
                            <li key={p.code} className="fw-ep-item">
                              <span className="mono muted">{p.code}</span>
                              <span className="fw-ep-title">{p.title}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="cell-sub">点击「展开」查看 {candidate.exam_points.length} 个考点的分组详情。</div>
              )}
            </Card>
          </div>

          {/* 右侧：冲突 */}
          {openConflicts.length > 0 ? (
            <aside className="fw-conflicts">
              <Card title={`冲突 ${openConflicts.length}`} sub="填写处理说明后即可确认框架。">
                <div className="fw-conflict-list">
                  {openConflicts.map((conflict) => (
                    <div key={conflict.key} className="fw-conflict-item">
                      <div className="fw-conflict-kind">{conflict.kind.replace(/_/g, ' ')}</div>
                      <div className="fw-conflict-msg">{conflict.message}</div>
                      <input
                        className="input input-sm"
                        placeholder="处理说明"
                        value={resolutions[conflict.key] ?? ''}
                        onChange={(e) => setResolutions({ ...resolutions, [conflict.key]: e.target.value })}
                      />
                    </div>
                  ))}
                </div>
              </Card>
            </aside>
          ) : null}
        </div>
      ) : null}
    </>
  )
}
