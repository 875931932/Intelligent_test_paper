import { useEffect, useRef } from 'react';
import { qlabel } from '@/lib/examDisplay';
import { formatScore } from '@/lib/format';
import type { PaperVersionItem } from '@/types/api';
import { normalizeAnswer } from './questionShared';

// ─── 左栏：题号索引 ───

export function QuestionIndex({
  groups, selected, onSelect,
}: {
  groups: Array<{ key: string; label: string; items: PaperVersionItem[] }>;
  selected: number;
  onSelect: (itemIndex: number) => void;
}) {
  // 键盘 ↑/↓ 翻到视野外的题时，左栏要跟着滚，否则教师看不到高亮跳到了哪。
  const activeRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
      {groups.map((g) => (
        <div key={g.key}>
          <div style={{
            display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
            padding: '10px 8px 6px', position: 'sticky', top: 0,
            background: 'var(--bg)', zIndex: 1,
          }}>
            <span style={{ fontSize: '0.78rem', fontWeight: 700, color: 'var(--text-secondary)' }}>{g.label}</span>
            <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>{g.items.length} 题</span>
          </div>
          {g.items.map((item) => {
            const active = item.item_index === selected;
            const flagged = item.needs_review || !!item.needs_review_reason;
            return (
              <button
                key={item.item_index}
                ref={active ? activeRef : undefined}
                onClick={() => onSelect(item.item_index)}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center', gap: '8px',
                  padding: '8px 10px', borderRadius: 10, border: 'none', cursor: 'pointer', textAlign: 'left',
                  background: active ? 'var(--accent-subtle)' : 'transparent',
                  transition: 'background 120ms ease',
                }}
              >
                <span style={{
                  fontSize: '0.8rem', fontWeight: 700, minWidth: 22,
                  color: active ? 'var(--accent)' : 'var(--text-tertiary)',
                }}>
                  {item.item_index}
                </span>
                <span style={{ fontSize: '0.8rem', color: active ? 'var(--accent)' : 'var(--text-secondary)', minWidth: 28 }}>
                  {qlabel(item.question_type)}
                </span>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{formatScore(item.score)}分</span>
                <span style={{ marginLeft: 'auto', display: 'flex', gap: '4px' }}>
                  {flagged && (
                    <span title="待审核" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--warning)' }} />
                  )}
                  {!normalizeAnswer(item.answer) && (
                    <span title="缺答案" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--error)' }} />
                  )}
                  {item.has_override && (
                    <span title="已修改" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--purple)' }} />
                  )}
                </span>
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}