import { Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Pencil, Sparkles, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { clabel, dlabel, qlabel } from '@/lib/examDisplay';
import { formatScore, friendlyId } from '@/lib/format';
import type { PaperVersionItem } from '@/types/api';
import { QuestionEditor } from './QuestionEditor';
import {
  type EditorSubmit,
  draftFromItem, normalizeAnswer, optionKeysOf, optionsToEntries, rubricText,
} from './questionShared';

// ─── 右栏：当前题目详情 / 编辑器 ───

export function QuestionDetail({
  item, examPointName, editing, readonly, submitting,
  hasPrev, hasNext, onEdit, onAiRevise, onCancelEdit, onSave, onDelete, onMove, onPrev, onNext, onDirtyChange,
}: {
  item: PaperVersionItem;
  examPointName?: string;
  editing: boolean;
  readonly: boolean;
  submitting: boolean;
  hasPrev: boolean;
  hasNext: boolean;
  onEdit: () => void;
  /** 展开/收起单题 AI 改题面板（仅非编辑、非定稿态可见） */
  onAiRevise?: () => void;
  onCancelEdit: () => void;
  onSave: (v: EditorSubmit) => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
  onPrev: () => void;
  onNext: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const flagged = item.needs_review || !!item.needs_review_reason;
  const opts = optionsToEntries(item.options);
  const keys = optionKeysOf(item.answer, opts.map((o) => o.text));
  const answerText = normalizeAnswer(item.answer);

  // 元数据（考点/难度/认知/审核原因）默认收进一行摘要徽标，details 展开看详情（F2 渐进披露）
  const epText = examPointName || (item.exam_point_id ? friendlyId(item.exam_point_id, '未匹配考点') : '');
  const epTitle = !examPointName && item.exam_point_id ? item.exam_point_id : undefined;
  const hasMeta = !!(epText || item.cognitive_level || (flagged && item.needs_review_reason));
  const headerBadges = (
    <>
      <span style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-tertiary)' }}>{item.item_index}.</span>
      <Badge variant="info">{qlabel(item.question_type)}</Badge>
      <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{formatScore(item.score)} 分</span>
      {item.difficulty && <Badge variant="default">{dlabel(item.difficulty)}</Badge>}
      {item.cognitive_level && <Badge variant="default">{clabel(item.cognitive_level)}</Badge>}
      {item.has_override && <Badge variant="purple">已修改</Badge>}
      {flagged && <Badge variant="warning">需审核</Badge>}
      {!answerText && <Badge variant="error">缺答案</Badge>}
    </>
  );

  return (
    <div
      className="glass-card"
      style={{
        // 等高平齐：flex:1 撑满右栏（右栏又拉伸到双栏行高），minHeight:0 允许内容
        // 超高时收缩并由 overflowY 卡内滚动，卡片边框底部始终与左卡底部平齐。
        flex: 1, minHeight: 0, overflowY: 'auto',
        display: 'flex', flexDirection: 'column',
        padding: '24px 24px 24px 22px',
        borderLeft: '3px solid ' + (flagged ? 'var(--warning)' : 'rgba(0,113,227,0.35)'),
      }}
    >
      {/* 一行摘要徽标：阅读态作为 details 摘要（展开看考点全文/审核原因），编辑态直接平铺 */}
      {editing || !hasMeta ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>{headerBadges}</div>
      ) : (
        <details>
          <summary style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', cursor: 'pointer', userSelect: 'none', listStyle: 'none' }}>
            {headerBadges}
            {epText && (
              <span title={epTitle} style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', maxWidth: '240px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {epText}
              </span>
            )}
            <span style={{ marginLeft: 'auto', fontSize: '0.75rem', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>详情 ▾</span>
          </summary>
          <div style={{ marginTop: '8px', padding: '8px 12px', borderRadius: 8, background: 'rgba(0,0,0,0.03)', fontSize: '0.8rem', lineHeight: 1.7, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {epText && (
              <div title={epTitle}>
                考点：{epText}{item.exam_point_code ? `（${item.exam_point_code}）` : ''}
              </div>
            )}
            {item.cognitive_level && <div>认知：{clabel(item.cognitive_level)}</div>}
            {flagged && item.needs_review_reason && (
              <div title={item.needs_review_reason} style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', wordBreak: 'break-word' }}>
                <span style={{ fontWeight: 600, color: 'var(--warning)' }}>待审核原因：</span>{item.needs_review_reason}
              </div>
            )}
          </div>
        </details>
      )}

      {editing ? (
        <div style={{ marginTop: '16px' }}>
          {/* 编辑态也要能看到标记原因，否则教师不知道该修什么 */}
          {flagged && item.needs_review_reason && (
            <div style={{
              marginBottom: '12px', padding: '8px 12px', borderRadius: 8, fontSize: '0.8rem', lineHeight: 1.6,
              background: 'var(--warning-subtle)', color: 'var(--text-secondary)',
            }}>
              <span style={{ fontWeight: 600, color: 'var(--warning)' }}>待审核原因：</span>{item.needs_review_reason}
            </div>
          )}
          <QuestionEditor
            initial={draftFromItem(item)}
            needsReview={flagged}
            submitting={submitting}
            submitLabel="保存本题"
            showActions
            onSubmit={onSave}
            onCancel={onCancelEdit}
            onDirtyChange={onDirtyChange}
          />
          <div style={{ display: 'flex', gap: '8px', marginTop: '12px', flexWrap: 'wrap' }}>
            <Button variant="danger" size="sm" onClick={onDelete} icon={<Trash2 size={14} />}>删除本题</Button>
            <Button variant="secondary" size="sm" onClick={() => onMove(-1)} icon={<ChevronUp size={14} />}>与上一题交换</Button>
            <Button variant="secondary" size="sm" onClick={() => onMove(1)} icon={<ChevronDown size={14} />}>与下一题交换</Button>
          </div>
        </div>
      ) : (
        <>
          {item.stem && (
            <div style={{ marginTop: '14px', fontSize: '1rem', lineHeight: 1.8, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{item.stem}</div>
          )}

          {opts.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '14px' }}>
              {opts.map((o) => {
                const isAns = keys.has(o.key.toUpperCase());
                return (
                  <div key={o.key} style={{
                    display: 'flex', gap: '10px', alignItems: 'flex-start', padding: '8px 12px', borderRadius: 8,
                    background: isAns ? 'var(--success-subtle)' : 'rgba(0,0,0,0.02)',
                    fontSize: '0.925rem', lineHeight: 1.65,
                  }}>
                    <span style={{ fontWeight: 600, color: isAns ? 'var(--success)' : 'var(--text-tertiary)', minWidth: 16 }}>{o.key}.</span>
                    <span style={{ flex: 1, color: isAns ? 'var(--text)' : 'var(--text-secondary)' }}>{o.text}</span>
                    {isAns && <Check size={15} style={{ color: 'var(--success)', flexShrink: 0, marginTop: 3 }} />}
                  </div>
                );
              })}
            </div>
          ) : (
            <div style={{
              marginTop: '14px', padding: '10px 14px', borderRadius: 8, fontSize: '0.925rem', lineHeight: 1.75,
              background: answerText ? 'var(--accent-subtle)' : 'var(--warning-subtle)',
              color: answerText ? 'var(--text)' : 'var(--warning)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              <span style={{ fontWeight: 600, fontSize: '0.78rem', display: 'block', marginBottom: 3, opacity: 0.7 }}>答案</span>
              {answerText || '未填写答案 —— 这道题导出答卷时会标注为「缺答案」，请补上'}
            </div>
          )}

          {item.explanation && (
            <details style={{ marginTop: '14px', fontSize: '0.875rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>解析</summary>
              <div style={{ marginTop: '6px', color: 'var(--text-secondary)', lineHeight: 1.75, whiteSpace: 'pre-wrap' }}>{item.explanation}</div>
            </details>
          )}

          {item.rubric && (
            <details style={{ marginTop: '14px', fontSize: '0.875rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>评分细则</summary>
              <div style={{ marginTop: '6px', color: 'var(--text-secondary)', lineHeight: 1.75, whiteSpace: 'pre-wrap' }}>{rubricText(item.rubric)}</div>
            </details>
          )}

          {/* marginTop:auto 把按钮行推到卡片底部（拉伸后的卡片内部留白落在内容与按钮之间） */}
          <div style={{ display: 'flex', gap: '8px', marginTop: 'auto', paddingTop: '16px', borderTop: '1px solid rgba(0,0,0,0.06)', flexWrap: 'wrap', alignItems: 'center' }}>
            {!readonly && (
              <Button size="sm" onClick={onEdit} icon={<Pencil size={14} />}>编辑本题</Button>
            )}
            {!readonly && onAiRevise && (
              <Button variant="secondary" size="sm" onClick={onAiRevise} icon={<Sparkles size={14} />}>AI 改题</Button>
            )}
            <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
              <Button variant="secondary" size="sm" disabled={!hasPrev} onClick={onPrev} icon={<ChevronLeft size={14} />}>上一题</Button>
              <Button variant="secondary" size="sm" disabled={!hasNext} onClick={onNext}>下一题<ChevronRight size={14} /></Button>
            </div>
          </div>
        </>
      )}
      {/* 键盘翻题提示放在卡片内底部：挪到卡外会让右卡比左卡矮一截，底部不再平齐 */}
      <p style={{ marginTop: '10px', fontSize: '0.75rem', color: 'var(--text-tertiary)', textAlign: 'center' }}>
        提示：可用键盘 ↑ / ↓ 快速翻题
      </p>
    </div>
  );
}