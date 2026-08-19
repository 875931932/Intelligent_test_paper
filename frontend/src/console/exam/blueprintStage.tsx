import { useCallback, useEffect, useMemo, useState } from 'react'
import { examPipelineApi, type PipelinePlanItem } from '../client'
import type { ExamProjectDetail } from '../types'
import { Button, Field, Notice } from '../ui'

const DEFAULT_TYPE_RULES: Record<string, { count: number; score: number }> = {
  single_choice: { count: 15, score: 2 },
  true_false: { count: 10, score: 1 },
  fill_blank: { count: 5, score: 2 },
  short_answer: { count: 4, score: 5 },
  comprehensive: { count: 3, score: 10 },
}

const DEFAULT_CHAPTER_WEIGHTS: Record<string, number> = {
  anchor_EP1: 30,
  anchor_EP2: 20,
  anchor_EP3: 15,
  anchor_EP4: 15,
  anchor_EP5: 10,
  anchor_EP6: 10,
}

interface Props {
  courseId: string
  project: ExamProjectDetail
  onConfirm: (projectId: string) => void
}

export function BlueprintStage({ courseId, project, onConfirm }: Props) {
  const confirmed = project.blueprint_confirmed
  const [frameworkVersionId, setFrameworkVersionId] = useState('framework-dev')
  const [catalogVersionId, setCatalogVersionId] = useState('catalog-dev')
  const [planItems, setPlanItems] = useState<PipelinePlanItem[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [confirmLoading, setConfirmLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const items = await examPipelineApi.getPlanItems(courseId, project.id)
        if (!cancelled) setPlanItems(items)
      } catch {
        /* ignore initial fetch failure */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [courseId, project.id])

  const totalScore = useMemo(() => planItems.reduce((s, it) => s + (it.score || 0), 0), [planItems])

  const handleGenerate = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await examPipelineApi.createBlueprint(courseId, project.id, {
        framework_version_id: frameworkVersionId,
        catalog_version_id: catalogVersionId,
        type_rules: DEFAULT_TYPE_RULES,
        chapter_weights: DEFAULT_CHAPTER_WEIGHTS,
        units: [],
        card_semantic_profiles: {},
        card_question_types: {},
      })
      setPlanItems(result.plan)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '生成蓝图失败'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [courseId, project.id, frameworkVersionId, catalogVersionId])

  const handleScoreBlur = useCallback(
    async (itemId: string, raw: string) => {
      const score = Number(raw)
      if (Number.isNaN(score)) return
      setPlanItems((prev) => prev.map((p) => (p.id === itemId ? { ...p, score } : p)))
      try {
        const updated = await examPipelineApi.patchPlanItem(courseId, itemId, { score })
        setPlanItems((prev) => prev.map((p) => (p.id === itemId ? updated : p)))
      } catch (err) {
        const msg = err instanceof Error ? err.message : '更新行失败'
        setError(msg)
      }
    },
    [courseId],
  )

  const handleConfirm = useCallback(async () => {
    setConfirmLoading(true)
    setError('')
    try {
      await examPipelineApi.confirmBlueprint(courseId, project.id)
      onConfirm(project.id)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '确认蓝图失败'
      setError(msg)
    } finally {
      setConfirmLoading(false)
    }
  }, [courseId, project.id, onConfirm])

  return (
    <div className="stage-panel">
      <div className="stage-panel-head">
        <h3>蓝图阶段</h3>
        <span className="muted small">
          {planItems.length > 0 ? `${planItems.length} 行 · 总分 ${totalScore}` : `项目总分 ${project.total_score}`}
        </span>
      </div>

      {error && (
        <Notice kind="error" role="alert">
          {error}
        </Notice>
      )}

      {planItems.length === 0 ? (
        <div className="blueprint-form" style={{ marginTop: 16 }}>
          <div className="form-grid">
            <Field label="框架版本 ID">
              <input
                type="text"
                className="text-input"
                aria-label="framework_version_id"
                value={frameworkVersionId}
                onChange={(e) => setFrameworkVersionId(e.target.value)}
              />
            </Field>
            <Field label="目录版本 ID">
              <input
                type="text"
                className="text-input"
                aria-label="catalog_version_id"
                value={catalogVersionId}
                onChange={(e) => setCatalogVersionId(e.target.value)}
              />
            </Field>
          </div>
          <div style={{ marginTop: 12 }} className="muted small">
            <div>题型预设：单选 15×2 / 判断 10×1 / 填空 5×2 / 简答 4×5 / 综合 3×10</div>
            <div>章节权重预设：anchor_EP1~6 分别为 30/20/15/15/10/10</div>
          </div>
          <div style={{ marginTop: 16 }}>
            <Button variant="primary" onClick={handleGenerate} loading={loading} data-testid="generate-blueprint-btn">
              生成蓝图
            </Button>
          </div>
        </div>
      ) : (
        <div style={{ marginTop: 16 }}>
          <table className="slot-table plan-table">
            <thead>
              <tr>
                <th>#</th>
                <th>题型</th>
                <th>分值</th>
                <th>难度</th>
                <th>认知</th>
                <th>评估单元</th>
                <th>知识点卡</th>
              </tr>
            </thead>
            <tbody>
              {planItems.map((row) => (
                <tr key={row.id} data-testid={`plan-row-${row.id}`}>
                  <td>{row.item_index}</td>
                  <td>{row.question_type}</td>
                  <td>
                    <input
                      type="number"
                      className="score-input"
                      data-testid={`score-input-${row.id}`}
                      defaultValue={row.score}
                      onBlur={(e) => handleScoreBlur(row.id, e.target.value)}
                    />
                  </td>
                  <td>{row.difficulty ?? '-'}</td>
                  <td>{row.cognitive_level ?? '-'}</td>
                  <td>{row.assessment_unit_title ?? row.assessment_unit_id ?? '-'}</td>
                  <td>{row.knowledge_card_name ?? row.knowledge_card_id ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className={`gate-banner ${confirmed ? 'verified' : ''}`} style={{ marginTop: 20 }}>
        <span className="gate-icon">{confirmed ? '✓' : '⚠'}</span>
        <span className="gate-text">
          {confirmed ? '蓝图已确认，可进入合同阶段。' : '蓝图待确认。确认后不可修改蓝图设置。'}
        </span>
        {!confirmed && (
          <div className="gate-actions">
            <Button
              variant="primary"
              onClick={handleConfirm}
              loading={confirmLoading}
              disabled={planItems.length === 0}
              data-testid="confirm-blueprint-btn"
            >
              确认蓝图
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
