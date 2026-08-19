/** 步骤二 · 考纲框架：双大纲语义提取 → 教师审阅权重与冲突 → 确认发布。 */

import { useCallback, useMemo, useState } from 'react'
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

  const openConflicts = candidate?.conflicts.filter((c) => c.status === 'open') ?? []
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
      {info ? <Notice kind={candidate ? 'info' : 'success'}>{info}</Notice> : null}

      {candidate ? (
        <>
          <Card
            title="考核锚点"
            sub="锚点权重决定整卷章节配比，合计须为 100。"
            actions={<Pill kind={weightsOk ? 'success' : 'warning'}>合计 {weightTotal.toFixed(1)} / 100</Pill>}
          >
            <table className="table">
              <thead>
                <tr>
                  <th>锚点</th>
                  <th>权重</th>
                  <th>能力要求</th>
                  <th>允许题型</th>
                </tr>
              </thead>
              <tbody>
                {candidate.anchors.map((anchor) => (
                  <tr key={anchor.key}>
                    <td>
                      <div className="cell-title">{anchor.title}</div>
                      <div className="cell-sub mono">{anchor.key}</div>
                    </td>
                    <td>
                      <input
                        className="input"
                        type="number"
                        min={0}
                        max={100}
                        step={0.5}
                        style={{ width: 90 }}
                        value={anchorWeights[anchor.key] ?? anchor.exam_weight}
                        onChange={(e) =>
                          setAnchorWeights({ ...anchorWeights, [anchor.key]: Number(e.target.value) })
                        }
                      />
                    </td>
                    <td className="cell-sub">{anchor.ability_requirements.join('；') || '—'}</td>
                    <td className="cell-sub">{anchor.allowed_question_types.join(' / ') || '不限'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Card title={`考点（${candidate.exam_points.length}）`} sub="考点是知识整理与命题合同的最小分配单元。">
            <table className="table">
              <thead>
                <tr>
                  <th>编码</th>
                  <th>考点</th>
                  <th>锚点</th>
                  <th>权重</th>
                  <th>考核要求</th>
                </tr>
              </thead>
              <tbody>
                {candidate.exam_points.map((point: ExamPoint) => (
                  <tr key={point.code}>
                    <td className="num">{point.code}</td>
                    <td className="cell-title">{point.title}</td>
                    <td className="cell-sub mono">{point.anchor_key}</td>
                    <td className="num">{point.weight_value}</td>
                    <td className="cell-sub">{point.assessment_requirement}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {openConflicts.length > 0 ? (
            <Card title="待处理冲突" sub="为每处冲突填写处理说明后才能确认框架。">
              {openConflicts.map((conflict) => (
                <div key={conflict.key} style={{ marginBottom: 14 }}>
                  <div className="section-title">
                    {conflict.kind} <span className="muted small mono">{conflict.key}</span>
                  </div>
                  <div className="cell-sub" style={{ marginBottom: 6 }}>{conflict.message}</div>
                  <input
                    className="input"
                    placeholder="处理说明，如：以考核大纲为准，教学深度按大纲要求补齐"
                    value={resolutions[conflict.key] ?? ''}
                    onChange={(e) => setResolutions({ ...resolutions, [conflict.key]: e.target.value })}
                  />
                </div>
              ))}
            </Card>
          ) : null}

          <Card title="教学主题覆盖" sub="教学大纲提取的主题，用于交叉验证考核覆盖。">
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {candidate.teaching_topics.map((topic) => (
                <Pill key={topic.key} kind="neutral">{topic.title}</Pill>
              ))}
            </div>
          </Card>

          <div className="form-row">
            <Button variant="primary" loading={confirming} onClick={() => void confirm()}>
              确认并发布框架
            </Button>
            <Button variant="secondary" loading={confirming} onClick={() => void reject()}>
              驳回候选
            </Button>
          </div>
        </>
      ) : null}
    </>
  )
}
