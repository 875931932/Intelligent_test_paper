import { useEffect, useState, useCallback } from 'react'
import { knowledgeApi } from '../client'
import { LoadingLine, EmptyState, Notice } from '../ui'
import { computeGraphLayout } from './graphLayout'
import { GraphView } from './graphView'
import { TreeView } from './treeView'
import { DetailDrawer } from './detailDrawer'
import type { PublishedKnowledge, PublishedCard, EvidenceLink } from '../types'

type ViewMode = 'graph' | 'tree'

export function KnowledgeCatalog({ courseId }: { courseId: string }) {
  const [data, setData] = useState<PublishedKnowledge | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [view, setView] = useState<ViewMode>('tree')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<EvidenceLink[]>([])
  const [evidenceLoading, setEvidenceLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    setSelectedId(null)
    setEvidence([])
    knowledgeApi
      .getPublished(courseId)
      .then((d) => { if (!cancelled) setData(d) })
      .catch(() => { if (!cancelled) setError('尚未发布知识目录，请先完成知识整理。') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [courseId])

  const selectedCard: PublishedCard | null = selectedId && data ? data.knowledge_cards[selectedId] ?? null : null

  const handleSelect = useCallback(async (cardId: string) => {
    setSelectedId(cardId)
    setEvidenceLoading(true)
    setEvidence([])
    try {
      const ev = await knowledgeApi.getEvidence(courseId, cardId)
      setEvidence(ev)
    } catch {
      setEvidence([])
    } finally {
      setEvidenceLoading(false)
    }
  }, [courseId])

  const handleJumpRelation = useCallback((targetName: string) => {
    if (!data) return
    const entry = Object.entries(data.knowledge_cards).find(([, c]) => c.name === targetName)
    if (entry) handleSelect(entry[0])
  }, [data, handleSelect])

  if (loading) return <LoadingLine>加载知识目录…</LoadingLine>
  if (error) return <Notice kind="warning">{error}</Notice>
  if (!data || data.published === false) return <EmptyState>暂无已发布的知识目录，请先在「知识整理」步骤完成构建与发布。</EmptyState>

  const layout = computeGraphLayout(data)
  const cardCount = Object.keys(data.knowledge_cards).length
  const ungroundedCount = Object.values(data.knowledge_cards).filter((c) => !c.grounded).length

  return (
    <div className="content-inner">
      <div className="page-head">
        <h2>知识目录</h2>
        <div className="desc">
          {cardCount} 张知识卡 · {ungroundedCount} 未落地 · 基于 {data.framework_version_id.slice(0, 8)}
        </div>
      </div>

      <div className="catalog-toolbar">
        <div className="view-toggle">
          <button className={view === 'graph' ? 'active' : ''} onClick={() => setView('graph')}>图谱</button>
          <button className={view === 'tree' ? 'active' : ''} onClick={() => setView('tree')}>树</button>
        </div>
      </div>

      <div className="catalog-main">
        <div className="catalog-canvas view-enter" key={view}>
          {view === 'graph' ? (
            <GraphView layout={layout} onSelectNode={handleSelect} selectedId={selectedId} />
          ) : (
            <TreeView data={data} onSelectCard={handleSelect} selectedId={selectedId} />
          )}
        </div>
        <DetailDrawer
          card={selectedCard}
          evidence={evidence}
          loading={evidenceLoading}
          onJumpRelation={handleJumpRelation}
        />
      </div>
    </div>
  )
}
