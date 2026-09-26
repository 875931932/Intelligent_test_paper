import { memo } from 'react';
import { BookOpen, ChevronDown, ChevronRight, Circle, Eye, Target } from 'lucide-react';
import { Badge } from '@/components/ui';
import { formatPercent } from '@/lib/format';
import type { FrameworkExamPoint, AssessmentUnit, KnowledgeCard } from '@/types/api';
import { truncate } from './knowledgeShared';

// ─── Tree View ───

export const TreeView = memo(function TreeView(props: {
  examPoints: FrameworkExamPoint[];
  units: AssessmentUnit[];
  cardsDict: Record<string, KnowledgeCard>;
  filteredCardIds: Set<string>;
  expandedPoints: Set<string>;
  expandedUnits: Set<string>;
  togglePoint: (id: string) => void;
  toggleUnit: (id: string) => void;
  onCardClick: (id: string) => void;
}) {
  const { examPoints, units, cardsDict, filteredCardIds, expandedPoints, expandedUnits, togglePoint, toggleUnit, onCardClick } = props;

  if (examPoints.length === 0 && Object.keys(cardsDict).length === 0) {
    return (
      <div style={{ padding: '48px 0', textAlign: 'center', color: 'var(--text-tertiary)' }}>
        <BookOpen size={40} style={{ margin: '0 auto 12px', opacity: 0.4 }} />
        <p style={{ fontSize: '0.875rem' }}>暂无知识目录</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '8px 0' }}>
      {examPoints.map((point) => {
        const pointUnits = units.filter((u) => u.exam_point_id === point.id);
        const isExp = expandedPoints.has(point.id);
        return (
          <div key={point.id}>
            <div
              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 12px', borderRadius: '10px', cursor: 'pointer', transition: 'background 0.2s' }}
              onClick={() => togglePoint(point.id)}
            >
              <span style={{ color: 'var(--text-tertiary)' }}>{isExp ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
              <span style={{ color: '#0071e3' }}><Target size={14} /></span>
              <span style={{ fontWeight: 600, fontSize: '0.875rem' }}>{point.title || point.code}</span>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>({point.code})</span>
              <span style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', marginLeft: 'auto' }}>{formatPercent(point.weight_value)}</span>
            </div>
            {isExp && (
              <div style={{ marginLeft: '24px' }}>
                {pointUnits.map((unit) => {
                  const unitCards = unit.card_ids
                    .map((cid) => cardsDict[cid])
                    .filter((c): c is KnowledgeCard => !!(c && filteredCardIds.has(c.id)));
                  const isUExp = expandedUnits.has(unit.unit_id);
                  const ungrounded = unitCards.some((c) => !c.grounded);
                  return (
                    <div key={unit.unit_id}>
                      <div
                        style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '8px 12px', borderRadius: '10px', cursor: 'pointer', transition: 'background 0.2s' }}
                        onClick={() => toggleUnit(unit.unit_id)}
                      >
                        <span style={{ color: 'var(--text-tertiary)' }}>{isUExp ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
                        <span style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)' }}>{unit.code}</span>
                        <span style={{ fontSize: '0.875rem', fontWeight: 500 }}>{unit.title}</span>
                        <span style={{ fontSize: '0.8125rem', marginLeft: 'auto', color: ungrounded ? '#ff3b30' : '#34c759' }}>
                          {unitCards.length}卡
                        </span>
                      </div>
                      {isUExp && unitCards.length > 0 && (
                        <div style={{ marginLeft: '24px' }}>
                          {unitCards.map((card) => (
                            <div
                              key={card.id}
                              style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', transition: 'background 0.2s' }}
                              onClick={() => onCardClick(card.id)}
                            >
                              <span style={{ color: card.grounded ? '#34c759' : '#ff3b30' }}>
                                <Circle size={8} fill="currentColor" />
                              </span>
                              <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '2px' }}>
                                <span style={{ fontSize: '0.875rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{card.name}</span>
                                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  {truncate(card.performance_statement || '暂无性能表述', 46)}
                                </span>
                              </div>
                              <Badge variant={card.grounded ? 'success' : 'error'}>
                                {card.grounded ? '已落地' : '未落地'}
                              </Badge>
                              <Eye size={12} style={{ color: 'var(--text-tertiary)', opacity: 0.6 }} />
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
});
