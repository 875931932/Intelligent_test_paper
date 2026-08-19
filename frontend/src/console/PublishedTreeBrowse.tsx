import { useEffect, useState } from 'react'
import { knowledgeApi } from './client'
import { Card, LoadingLine, EmptyState, Notice } from './ui'
import type { PublishedKnowledge } from './types'

export function PublishedTreeBrowse({ courseId }: { courseId: string }) {
  const [data, setData] = useState<PublishedKnowledge | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    knowledgeApi
      .getPublished(courseId)
      .then((d) => {
        if (!cancelled) setData(d)
      })
      .catch(() => {
        if (!cancelled) setError('尚未发布知识目录，请先完成知识整理。')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [courseId])

  if (loading) return <LoadingLine>加载知识目录…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>
  if (!data) return <EmptyState>无知识目录</EmptyState>

  const cards = data.knowledge_cards
  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>知识目录</h2>
        <div className="desc">只读浏览。图谱+树双视图与证据中心将在后续交付。</div>
      </div>
      <Card title={`已发布 · ${Object.keys(cards).length} 张知识卡`} sub={`基于框架 v${(data.framework_version_id ?? '').slice(0, 8)}`}>
        {data.exam_points.map((ep) => {
          const units = data.units.filter((u) => u.exam_point_id === ep.id)
          return (
            <div className="tree-topic" key={ep.id}>
              <div className="tree-topic-head">
                <span className="code">{ep.code}</span>
                {ep.title}
              </div>
              {units.map((u) => (
                <div className="tree-unit" key={u.unit_id}>
                  <div className="tree-unit-head">
                    <b>{u.code}</b>
                    <span className="muted small">{u.title}</span>
                  </div>
                  <div className="tree-cards">
                    {u.card_ids.map((cid) => {
                      const c = cards[cid]
                      if (!c) return null
                      return (
                        <div className="tree-card" key={cid}>
                          <b>{c.name}</b>
                          <div className="meta">
                            {c.assessable_content.length} 原子 · <span>{c.concept_cluster}</span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
          )
        })}
      </Card>
    </div>
  )
}
