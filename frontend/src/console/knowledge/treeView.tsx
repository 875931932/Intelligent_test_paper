import { useState, useMemo } from 'react'
import type { PublishedKnowledge } from '../types'

interface Props {
  data: PublishedKnowledge
  onSelectCard: (id: string) => void
  selectedId: string | null
}

export function TreeView({ data, onSelectCard, selectedId }: Props) {
  const [query, setQuery] = useState('')
  const cards = data.knowledge_cards

  const filtered = useMemo(() => {
    if (!query.trim()) return data.exam_points
    const q = query.toLowerCase()
    return data.exam_points.filter((ep) => {
      const units = data.units.filter((u) => u.exam_point_id === ep.id)
      return units.some((u) =>
        u.card_ids.some((cid) => cards[cid]?.name.toLowerCase().includes(q)),
      )
    })
  }, [query, data])

  return (
    <div className="tree-view">
      <input
        className="ktree-search"
        placeholder="搜索知识卡…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="ktree-root">
        {filtered.map((ep) => {
          const units = data.units.filter((u) => u.exam_point_id === ep.id)
          return (
            <div className="ktree-node domain" key={ep.id}>
              <div className="ktree-node-head">
                <span className="code">{ep.code}</span>
                <span>{ep.title}</span>
              </div>
              <div className="ktree-children">
                {units.map((u) => (
                  <div className="ktree-node unit" key={u.unit_id}>
                    <div className="ktree-node-head">
                      <b>{u.code}</b>
                      <span className="muted small">{u.title}</span>
                    </div>
                    <div className="ktree-children">
                      {u.card_ids.map((cid) => {
                        const c = cards[cid]
                        if (!c) return null
                        if (query.trim()) {
                          const q = query.toLowerCase()
                          if (!c.name.toLowerCase().includes(q)) return null
                        }
                        return (
                          <div
                            className={`ktree-node card ${selectedId === cid ? 'selected' : ''}`}
                            key={cid}
                            onClick={() => onSelectCard(cid)}
                          >
                            <span>{c.name}</span>
                            <span className={`ktree-badge ${c.grounded ? 'grounded' : 'ungrounded'}`}>
                              {c.grounded ? '●' : '●'} {c.assessable_content.length} 原子
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
