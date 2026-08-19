import type { PublishedCard, EvidenceLink } from '../types'

interface Props {
  card: PublishedCard | null
  evidence: EvidenceLink[]
  loading: boolean
  onJumpRelation: (target: string) => void
}

const EDGE_LABELS: Record<string, string> = {
  equivalent_to: '等价',
  specializes: '特化',
  component_of: '组成部分',
  contrasts_with: '对比',
  summarizes: '概括',
  requires: '依赖',
}

export function DetailDrawer({ card, evidence, loading, onJumpRelation }: Props) {
  if (!card) {
    return <div className="detail-drawer"><div className="catalog-empty">选择知识卡查看详情</div></div>
  }

  const stars = '★'.repeat(card.importance) + '☆'.repeat(5 - card.importance)

  return (
    <div className="detail-drawer">
      <div className="detail-section">
        <h3>{card.name}</h3>
        <div className="muted small">{stars}</div>
      </div>

      <div className="detail-section">
        <h4>画像</h4>
        <div className="tag-row">
          <span className="tag">{card.concept_cluster}</span>
          {card.cognitive_targets.map((t) => (
            <span className="tag" key={t}>{t}</span>
          ))}
          {card.allowed_question_types.map((t) => (
            <span className="tag" key={t}>{t}</span>
          ))}
        </div>
      </div>

      <div className="detail-section">
        <h4>答案命题</h4>
        <p className="small">{card.answer_proposition}</p>
      </div>

      <div className="detail-section">
        <h4>可评原子 ({card.assessable_content.length})</h4>
        {card.assessable_content.map((atom, i) => (
          <div className="atom-row" key={i}>
            <span className={`grounded-mark ${card.grounded ? 'yes' : 'no'}`}>{card.grounded ? '✓' : '✕'}</span>
            <span>{atom}</span>
          </div>
        ))}
      </div>

      <div className="detail-section">
        <h4>证据链 ({evidence.length})</h4>
        {loading ? (
          <div className="muted small">加载证据…</div>
        ) : evidence.length === 0 ? (
          <div className="muted small">无证据</div>
        ) : (
          evidence.map((ev, i) => (
            <div className="atom-row" key={i}>
              <span className="tag" style={{ background: ev.evidence_role === 'direct' ? 'var(--success-subtle)' : 'var(--warning-subtle)' }}>
                {ev.evidence_role}
              </span>
              <span className="small muted">{ev.content.slice(0, 60)}…</span>
              <span className="muted small">conf={ev.confidence}</span>
            </div>
          ))
        )}
      </div>

      {card.relation_edges.length > 0 && (
        <div className="detail-section">
          <h4>关系</h4>
          <div className="tag-row">
            {card.relation_edges.map((edge, i) => (
              <button
                className="tag"
                key={i}
                onClick={() => onJumpRelation(edge.target)}
                style={{ cursor: 'pointer', border: 'none' }}
              >
                {EDGE_LABELS[edge.kind] ?? edge.kind} → {edge.target}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
