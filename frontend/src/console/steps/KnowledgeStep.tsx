/** 步骤三 · 知识整理：多资料语义组织 → 教师审阅知识树 → 发布知识目录。 */

import { useCallback, useMemo, useState } from 'react'
import { knowledgeApi } from '../client'
import type { KnowledgeTreeCandidate, Material } from '../types'
import { Button, Card, EmptyState, Notice, Pill } from '../ui'

const ORIGIN_LABELS: Record<string, string> = {
  syllabus_core: '考纲核心',
  material_evidence: '材料证据',
}

const CARD_STATUS_PILL: Record<string, 'neutral' | 'info' | 'warning'> = {
  active: 'neutral',
  material_only: 'info',
  needs_teacher_review: 'warning',
  excluded: 'neutral',
}

export function KnowledgeStep({ courseId, materials, onDone }: {
  courseId: string
  materials: Material[]
  onDone: () => Promise<void>
}) {
  const selectable = useMemo(
    () =>
      materials.filter(
        (m) =>
          (m.material_type === 'teaching_material' || m.material_type === 'exercise') &&
          m.parse_status?.status === 'ready',
      ),
    [materials],
  )

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [running, setRunning] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [runId, setRunId] = useState('')
  const [candidate, setCandidate] = useState<KnowledgeTreeCandidate | null>(null)
  const [excluded, setExcluded] = useState<Set<string>>(new Set())

  const toggleMaterial = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleExcluded = (code: string) => {
    setExcluded((prev) => {
      const next = new Set(prev)
      if (next.has(code)) next.delete(code)
      else next.add(code)
      return next
    })
  }

  const run = useCallback(async () => {
    if (selected.size === 0) {
      setError('请至少选择一份解析就绪的教学材料。')
      return
    }
    setRunning(true)
    setError('')
    setInfo('')
    setCandidate(null)
    try {
      const created = await knowledgeApi.createRun(courseId, [...selected])
      setRunId(created.run_id)
      const data = await knowledgeApi.getCandidate(courseId, created.run_id)
      setCandidate(data)
      setExcluded(new Set())
      setInfo('知识树候选已生成：请审阅各主题下的单元与知识卡，勾除不需要的内容后发布。')
    } catch (err) {
      setError(err instanceof Error ? err.message : '知识整理失败')
    } finally {
      setRunning(false)
    }
  }, [courseId, selected])

  const publish = useCallback(async () => {
    if (!candidate || !runId) return
    setPublishing(true)
    setError('')
    try {
      await knowledgeApi.publish(courseId, runId, {
        operations: [],
        reviewed_topic_codes: candidate.topics
          .filter((t) => !excluded.has(t.code))
          .map((t) => t.code),
        reviewed_exam_point_codes: [],
        teacher_exclusions: [...excluded],
      })
      setInfo('知识目录已发布，可以进入「命题与出卷」。')
      setCandidate(null)
      await onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : '知识目录发布失败')
    } finally {
      setPublishing(false)
    }
  }, [candidate, runId, courseId, excluded, onDone])

  if (selectable.length === 0) {
    return (
      <Card title="知识整理" sub="需要解析就绪的教学材料或习题资料。">
        <EmptyState>暂无可整理资料。请先在「课程资料」步骤上传教学材料并完成解析。</EmptyState>
      </Card>
    )
  }

  return (
    <>
      <Card title="整理知识" sub="按已确认的考纲框架，将教学材料组织为「主题 → 单元 → 知识卡」三级目录。">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14 }}>
          {selectable.map((material) => (
            <label key={material.id} className="check-line">
              <input
                type="checkbox"
                checked={selected.has(material.latest_version!.id)}
                onChange={() => toggleMaterial(material.latest_version!.id)}
              />
              {material.logical_name}
              <span className="muted small">v{material.latest_version!.version_no}</span>
            </label>
          ))}
        </div>
        <Button variant="primary" loading={running} disabled={selected.size === 0} onClick={() => void run()}>
          开始整理
        </Button>
      </Card>

      {error ? <Notice kind="error">{error}</Notice> : null}
      {info ? <Notice kind={candidate ? 'info' : 'success'}>{info}</Notice> : null}

      {candidate ? (
        <>
          <Card
            title="知识树候选"
            sub="勾选复选框表示剔除该单元/主题；未勾选内容将进入知识目录。"
            actions={
              <Button variant="primary" size="sm" loading={publishing} onClick={() => void publish()}>
                发布知识目录
              </Button>
            }
          >
            {candidate.topics.map((topic) => (
              <div key={topic.code} className="tree-topic">
                <div className="tree-topic-head">
                  <input
                    type="checkbox"
                    checked={excluded.has(topic.code)}
                    onChange={() => toggleExcluded(topic.code)}
                    style={{ accentColor: 'var(--danger)' }}
                    title="剔除整个主题"
                  />
                  <span>{topic.name}</span>
                  <span className="code">{topic.code}</span>
                  <span className="spacer" style={{ flex: 1 }} />
                  <Pill kind="neutral">{topic.units.filter((u) => !excluded.has(u.code)).length} 单元</Pill>
                </div>
                {topic.units.map((unit) => (
                  <div key={unit.code} className="tree-unit" style={{ opacity: excluded.has(unit.code) || excluded.has(topic.code) ? 0.5 : 1 }}>
                    <div className="tree-unit-head">
                      <input
                        type="checkbox"
                        checked={excluded.has(unit.code)}
                        onChange={() => toggleExcluded(unit.code)}
                        style={{ accentColor: 'var(--danger)' }}
                        title="剔除该单元"
                      />
                      <b>{unit.title}</b>
                      <span className="code muted mono">{unit.code}</span>
                      <Pill kind={unit.origin === 'syllabus_core' ? 'info' : 'neutral'}>
                        {ORIGIN_LABELS[unit.origin] ?? unit.origin}
                      </Pill>
                      {unit.exam_point_code ? <Pill kind="neutral">考点 {unit.exam_point_code}</Pill> : null}
                    </div>
                    <div className="tree-cards">
                      {unit.cards.map((card) => (
                        <div key={card.name} className="tree-card">
                          <b>{card.name}</b>
                          {card.importance != null ? <span className="muted small"> · 重要度 {card.importance}</span> : null}
                          {card.status !== 'active' ? (
                            <Pill kind={CARD_STATUS_PILL[card.status] ?? 'neutral'}>{card.status}</Pill>
                          ) : null}
                          <div className="meta">{card.performance_statement}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ))}
            {candidate.unmatched.length > 0 ? (
              <>
                <div className="section-title" style={{ marginTop: 16 }}>未匹配内容（不会进入知识目录）</div>
                {candidate.unmatched.map((item, index) => (
                  <div key={index} className="cell-sub" style={{ marginBottom: 4 }}>
                    {item.label} — {item.reason}
                  </div>
                ))}
              </>
            ) : null}
          </Card>
        </>
      ) : null}
    </>
  )
}
