/** 步骤四 · 命题与出卷：蓝图设置 → 合同分配 → 教师确认 → 生成候选试卷。 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { examApi, knowledgeApi } from '../client'
import {
  ARCHETYPE_LABELS,
  DIFFICULTY_LABELS,
  QUESTION_TYPE_LABELS,
  type PaperContract,
  type PublishedKnowledge,
  type Question,
} from '../types'
import { Button, Card, EmptyState, Field, LoadingLine, Notice, Pill } from '../ui'

type TypeRuleState = { count: number; score: number }

type GenerationResultCheck = {
  passed?: boolean
  checks?: Array<{ code: string; passed: boolean; detail?: Record<string, unknown> }>
}

const DEFAULT_RULES: Record<string, TypeRuleState> = {
  single_choice: { count: 15, score: 2 },
  true_false: { count: 10, score: 2 },
  short_answer: { count: 5, score: 10 },
  comprehensive: { count: 0, score: 20 },
}

export function ExamStep({ courseId }: { courseId: string }) {
  const [knowledge, setKnowledge] = useState<PublishedKnowledge | null>(null)
  const [loadingKnowledge, setLoadingKnowledge] = useState(true)
  const [rules, setRules] = useState<Record<string, TypeRuleState>>({ ...DEFAULT_RULES })
  const [seed, setSeed] = useState('')
  const [contract, setContract] = useState<PaperContract | null>(null)
  const [confirmed, setConfirmed] = useState<PaperContract | null>(null)
  const [questions, setQuestions] = useState<Question[] | null>(null)
  const [finalCheck, setFinalCheck] = useState<GenerationResultCheck | null>(null)
  const [modelInfo, setModelInfo] = useState('')
  const [busy, setBusy] = useState<'allocate' | 'confirm' | 'generate' | null>(null)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoadingKnowledge(true)
    knowledgeApi
      .getPublished(courseId)
      .then((data) => {
        if (!cancelled) setKnowledge(data)
      })
      .catch(() => {
        if (!cancelled) setError('尚未发布知识目录，请先完成「知识整理」步骤。')
      })
      .finally(() => {
        if (!cancelled) setLoadingKnowledge(false)
      })
    return () => {
      cancelled = true
    }
  }, [courseId])

  const totalScore = useMemo(
    () => Object.values(rules).reduce((sum, r) => sum + r.count * r.score, 0),
    [rules],
  )

  const examPointByAnchor = useMemo(() => {
    const byAnchor: Record<string, { code: string; weight: number }> = {}
    for (const point of knowledge?.exam_points ?? []) {
      const entry = byAnchor[point.anchor_key]
      if (entry) entry.weight += point.weight_value
      else byAnchor[point.anchor_key] = { code: point.code, weight: point.weight_value }
    }
    return byAnchor
  }, [knowledge])

  const allocate = useCallback(async () => {
    if (!knowledge) return
    if (totalScore <= 0) {
      setError('题型分值配置无效：总分必须大于 0。')
      return
    }
    setBusy('allocate')
    setError('')
    setInfo('')
    setQuestions(null)
    setConfirmed(null)
    try {
      const typeRules: Record<string, Record<string, unknown>> = {}
      for (const [type, rule] of Object.entries(rules)) {
        if (rule.count <= 0) continue
        typeRules[type] = {
          count: rule.count,
          score: rule.score,
          difficulty_distribution: { low: 30, medium: 45, high: 25 },
        }
      }
      const chapterWeights = Object.fromEntries(
        Object.entries(examPointByAnchor).map(([anchor, entry]) => [anchor, entry.weight]),
      )
      const units = (knowledge?.units ?? []).map((unit) => ({
        unit_id: unit.unit_id,
        exam_point_id: unit.exam_point_id,
        anchor_key: unit.anchor_key,
        card_ids: unit.card_ids,
      }))
      const seedValue = seed.trim() === '' ? null : Number(seed)
      const data = await examApi.allocate(courseId, {
        blueprint: {
          total_score: totalScore,
          type_rules: typeRules,
          chapter_weights: chapterWeights,
          units,
        },
        knowledge_cards: knowledge.knowledge_cards,
        allocation_seed: Number.isFinite(seedValue as number) ? (seedValue as number) : null,
      })
      setContract(data)
      setInfo(
        data.conflicts.length > 0
          ? `合同分配完成，但存在 ${data.conflicts.length} 处冲突，请处理后重新分配。`
          : `合同已分配 ${data.slots.length} 个题位（总分 ${data.total_score} 分），请核对后确认。`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : '合同分配失败')
    } finally {
      setBusy(null)
    }
  }, [courseId, knowledge, rules, totalScore, examPointByAnchor, seed])

  const confirm = useCallback(async () => {
    if (!contract || !knowledge) return
    setBusy('confirm')
    setError('')
    try {
      const units = knowledge.units.map((unit) => ({
        unit_id: unit.unit_id,
        exam_point_id: unit.exam_point_id,
        anchor_key: unit.anchor_key,
        card_ids: unit.card_ids,
      }))
      const revised = await examApi.confirm(courseId, {
        contract,
        slot_revisions: [],
        units,
        knowledge_cards: knowledge.knowledge_cards,
      })
      setConfirmed(revised)
      setInfo('合同已确认，可以生成候选试卷。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '合同确认失败')
    } finally {
      setBusy(null)
    }
  }, [courseId, contract, knowledge])

  const generate = useCallback(async () => {
    if (!confirmed || !knowledge) return
    setBusy('generate')
    setError('')
    setInfo('正在按合同批量生成题目，模型调用可能需要几分钟…')
    try {
      const result = await examApi.generate(courseId, confirmed.slots, knowledge.knowledge_cards)
      setQuestions(result.questions)
      setFinalCheck(result.final_check ?? null)
      setModelInfo(`${result.model} · ${result.model_call_count} 次模型调用`)
      setInfo(result.final_check?.passed ? '试卷生成完成，终检通过。' : '试卷生成完成，请关注终检未通过项。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '试卷生成失败')
    } finally {
      setBusy(null)
    }
  }, [courseId, confirmed, knowledge])

  if (loadingKnowledge) {
    return <LoadingLine>正在读取已发布知识目录…</LoadingLine>
  }

  if (!knowledge) {
    return (
      <Card title="命题与出卷">
        <EmptyState>尚未发布知识目录，请先完成「知识整理」步骤。</EmptyState>
      </Card>
    )
  }

  const cardCount = Object.keys(knowledge.knowledge_cards).length
  const pointById = Object.fromEntries(knowledge.exam_points.map((p) => [p.id, p]))

  return (
    <>
      <div className="metric-row">
        <div className="metric">
          <span className="eyebrow">考点</span>
          <strong className="value">{knowledge.exam_points.length}</strong>
          <span className="meta">来自已确认考纲框架</span>
        </div>
        <div className="metric">
          <span className="eyebrow">考核单元</span>
          <strong className="value">{knowledge.units.length}</strong>
          <span className="meta">命题选题单元</span>
        </div>
        <div className="metric">
          <span className="eyebrow">知识卡</span>
          <strong className="value">{cardCount}</strong>
          <span className="meta">可考核知识卡片</span>
        </div>
        <div className="metric">
          <span className="eyebrow">卷面总分</span>
          <strong className="value">{totalScore}</strong>
          <span className="meta">按题型分值自动合计</span>
        </div>
      </div>

      <Card title="蓝图设置" sub="题型数量与分值决定合同题位；章节配比自动取自考纲权重。">
        <div className="form-grid" style={{ marginBottom: 14 }}>
          {Object.keys(DEFAULT_RULES).map((type) => (
            <div key={type} className="card" style={{ padding: 12, boxShadow: 'none' }}>
              <div className="section-title" style={{ marginTop: 0 }}>
                {QUESTION_TYPE_LABELS[type] ?? type}
              </div>
              <div className="form-row">
                <Field label="数量">
                  <input
                    className="input"
                    type="number"
                    min={0}
                    value={rules[type].count}
                    onChange={(e) =>
                      setRules({ ...rules, [type]: { ...rules[type], count: Number(e.target.value) } })
                    }
                  />
                </Field>
                <Field label="每题分值">
                  <input
                    className="input"
                    type="number"
                    min={1}
                    value={rules[type].score}
                    onChange={(e) =>
                      setRules({ ...rules, [type]: { ...rules[type], score: Number(e.target.value) } })
                    }
                  />
                </Field>
              </div>
            </div>
          ))}
        </div>
        <div className="form-row">
          <Field label="分配种子" hint="留空为确定性分配；填整数可在同一知识池上换卷。">
            <input
              className="input"
              placeholder="可选，如 2026"
              value={seed}
              onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ''))}
            />
          </Field>
          <Button variant="primary" loading={busy === 'allocate'} onClick={() => void allocate()}>
            分配命题合同
          </Button>
        </div>
      </Card>

      {error ? <Notice kind="error">{error}</Notice> : null}
      {info && busy === 'generate' ? <LoadingLine>{info}</LoadingLine> : info ? <Notice kind={questions ? 'success' : 'info'}>{info}</Notice> : null}

      {contract ? (
        <>
          <Card
            title="命题合同"
            sub={`${contract.slots.length} 个题位 · 总分 ${contract.total_score} 分`}
            actions={
              confirmed ? (
                <Pill kind="success">已确认</Pill>
              ) : (
                <Button
                  variant="primary"
                  size="sm"
                  loading={busy === 'confirm'}
                  disabled={contract.conflicts.length > 0}
                  onClick={() => void confirm()}
                >
                  确认合同
                </Button>
              )
            }
          >
            {contract.conflicts.length > 0 ? (
              <div style={{ marginBottom: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
                {contract.conflicts.map((conflict, index) => (
                  <Notice key={index} kind="warning">
                    <strong>{conflict.code}</strong> · {pointById[conflict.exam_point_id]?.code ?? conflict.exam_point_id} — {conflict.message}
                  </Notice>
                ))}
              </div>
            ) : null}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
              {Object.entries(contract.audit_summary.type_counts).map(([type, count]) => (
                <Pill key={type} kind="info">{QUESTION_TYPE_LABELS[type] ?? type} × {count}</Pill>
              ))}
              {Object.entries(contract.audit_summary.difficulty_counts).map(([level, count]) => (
                <Pill key={level} kind="neutral">{DIFFICULTY_LABELS[level] ?? level} × {count}</Pill>
              ))}
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th>题号</th>
                  <th>题型</th>
                  <th>分值</th>
                  <th>难度</th>
                  <th>考点</th>
                  <th>考核原子</th>
                </tr>
              </thead>
              <tbody>
                {contract.slots.map((slot) => (
                  <tr key={slot.item_index}>
                    <td className="num">{slot.item_index}</td>
                    <td>
                      {QUESTION_TYPE_LABELS[slot.question_type] ?? slot.question_type}
                      {slot.comprehensive_archetype ? (
                        <div className="cell-sub">{ARCHETYPE_LABELS[slot.comprehensive_archetype] ?? slot.comprehensive_archetype}</div>
                      ) : null}
                    </td>
                    <td className="num">{slot.score}</td>
                    <td>{DIFFICULTY_LABELS[slot.difficulty] ?? slot.difficulty}</td>
                    <td className="cell-sub mono">{pointById[slot.exam_point_id]?.code ?? slot.exam_point_id}</td>
                    <td className="cell-sub">{slot.coverage_atom}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {confirmed ? (
            <div className="form-row">
              <Button variant="primary" loading={busy === 'generate'} onClick={() => void generate()}>
                生成候选试卷
              </Button>
              {modelInfo ? <span className="muted small" style={{ paddingBottom: 8 }}>{modelInfo}</span> : null}
            </div>
          ) : null}
        </>
      ) : null}

      {finalCheck ? (
        <Card
          title="终检结果"
          actions={<Pill kind={finalCheck.passed ? 'success' : 'warning'}>{finalCheck.passed ? '通过' : '存在待关注项'}</Pill>}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {(finalCheck.checks ?? []).map((check) => (
              <div key={check.code} className="check-line">
                <Pill kind={check.passed ? 'success' : 'warning'}>{check.passed ? '通过' : '未过'}</Pill>
                <span className="mono small">{check.code}</span>
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {questions ? (
        <Card title={`候选试卷（${questions.length} 题）`} sub={modelInfo}>
          {questions.map((question, index) => (
            <QuestionView key={question.item_index ?? index} question={question} />
          ))}
        </Card>
      ) : null}
    </>
  )
}

/** 单题渲染：按题型展示题干、选项、答案与解析。 */
function QuestionView({ question }: { question: Question }) {
  const options = Array.isArray(question.options)
    ? question.options
    : question.options
      ? Object.entries(question.options).map(([key, value]) => `${key}. ${value}`)
      : null

  return (
    <div className="question-card">
      <div className="question-head">
        <span className="no">第 {(question.item_index ?? 0) + 1} 题</span>
        <Pill kind="info">{QUESTION_TYPE_LABELS[question.question_type] ?? question.question_type}</Pill>
        <Pill kind="neutral">{question.score} 分</Pill>
        {question.comprehensive_archetype ? (
          <Pill kind="neutral">{ARCHETYPE_LABELS[question.comprehensive_archetype] ?? question.comprehensive_archetype}</Pill>
        ) : null}
        {question.needs_review ? <Pill kind="warning" dot>需人工复核</Pill> : null}
        <span className="spacer" style={{ flex: 1 }} />
        {question.quality?.status && question.quality.status !== 'ok' ? (
          <Pill kind="danger">{question.quality.message ?? question.quality.code}</Pill>
        ) : null}
      </div>
      <div className="question-body">
        {question.stem ? <div className="stem">{question.stem}</div> : null}
        {options ? (
          <ul className="options">
            {options.map((option, index) => (
              <li key={index}>{String.fromCharCode(65 + index)}. {option}</li>
            ))}
          </ul>
        ) : null}
        {question.subquestions ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {question.subquestions.map((sub, index) => {
              const subText = typeof sub === 'string' ? sub : (sub.question ?? sub.prompt ?? '')
              const subAnswer = typeof sub === 'string' ? '' : (sub.answer ?? '')
              return (
                <div key={index} className="tree-card">
                  <b>分问 {index + 1}</b>
                  <div style={{ marginTop: 4 }}>{subText}</div>
                  {subAnswer !== '' ? (
                    <div className="answer-line" style={{ marginTop: 6 }}>答案：{String(subAnswer)}</div>
                  ) : null}
                </div>
              )
            })}
          </div>
        ) : null}
        {question.answer != null ? (
          <div className="answer-line">
            答案：
            {question.question_type === 'true_false'
              ? question.answer === true || question.answer === 'true' || question.answer === '对'
                ? '正确'
                : question.answer === false || question.answer === 'false' || question.answer === '错'
                  ? '错误'
                  : String(question.answer)
              : typeof question.answer === 'string'
                ? question.answer
                : JSON.stringify(question.answer)}
          </div>
        ) : null}
        {question.rubric && question.rubric.length > 0 ? (
          <div className="explain-line">
            评分要点：
            {question.rubric.map((point, index) => (
              <div key={index}>· {point.point}（{point.score} 分）</div>
            ))}
          </div>
        ) : null}
        {question.explanation ? <div className="explain-line">{question.explanation}</div> : null}
      </div>
    </div>
  )
}
