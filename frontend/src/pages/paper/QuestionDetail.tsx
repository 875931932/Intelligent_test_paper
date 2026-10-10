import { Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Pencil, Sparkles, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { clabel, dlabel, qlabel } from '@/lib/examDisplay';
import { formatScore, friendlyId } from '@/lib/format';
import type { PaperVersionItem } from '@/types/api';
import { QuestionEditor } from './QuestionEditor';
import { StemBlocks } from './StemBlocks';
import {
  type EditorSubmit,
  draftFromItem, normalizeAnswer, optionKeysOf, optionsToEntries, rubricText,
  splitInlineSubList, stripSubIndex,
} from './questionShared';

// ─── 右栏：当前题目详情 / 编辑器 ───

// 难度依据面板的展示词表（码 → 中文；未知码原样显示，不掩盖新增检查项）
const CHECK_LABELS: Record<string, string> = {
  difficulty_enum: '难度枚举',
  cognitive_enum: '认知枚举',
  self_report_cross_check: '自陈交叉核对',
  cognitive_tier_fit: '认知档次匹配',
  task_expression: '任务表达识别',
};
const CHECK_RESULTS: Record<string, string> = {
  ok: '通过', absent: '未返回', absent_default_medium: '缺省中档',
  risk: '有风险', violation: '违反', invalid: '非法', skipped_absent: '未检验',
};
const UNVERIFIED_LABELS: Record<string, string> = {
  context_novelty: '情境陌生度',
  reasoning_depth: '推理复杂度',
};
const SELF_CONTEXT_LABELS: Record<string, string> = {
  none: '无情境',
  textbook_original: '教材原情境',
  regular_variant: '常规新情境',
  novel: '陌生情境',
  investigative: '探究性任务',
};

export function QuestionDetail({
  item, no, examPointName, editing, readonly, submitting,
  hasPrev, hasNext, onEdit, onAiRevise, onCancelEdit, onSave, onDelete, onMove, onPrev, onNext, onDirtyChange,
  onSetDifficultyConfirm,
}: {
  item: PaperVersionItem;
  /** 卷面题号（每题型从 1）；item_index 仍作编辑/删除端点的稳定 id */
  no: number;
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
  /** 难度依据确认（改进方案 2.4）：true=确认、false=撤销；内容改题由后端自动重置 */
  onSetDifficultyConfirm?: (confirmed: boolean) => void;
}) {
  const flagged = item.needs_review || !!item.needs_review_reason;
  const opts = optionsToEntries(item.options);
  const keys = optionKeysOf(item.answer, opts.map((o) => o.text));
  const answerText = normalizeAnswer(item.answer);

  // 综合题分问：subquestions 此前从未在站内渲染（教师只能看到引导段 + 一坨合并答案）。
  // 结构化逐问题面/答案各自成块，与导出 HTML 的 q-sub 形态对齐。
  const subs = (Array.isArray(item.subquestions) ? item.subquestions : []).map((s) => {
    const o = (s ?? {}) as Record<string, unknown>;
    return {
      prompt: stripSubIndex(typeof o.prompt === 'string' ? o.prompt : ''),
      score: typeof o.score === 'number' ? o.score : null,
      answer: normalizeAnswer(o.answer),
    };
  });
  // 逐问答案齐全才走逐问展示；有缺漏则回落原合并答案块，不藏信息
  const subAnswersComplete = subs.length > 0 && subs.every((s) => s.answer.trim());

  // 难度依据（改进方案 2.4）：v2 报告 {checks, risks, unverified} 存于 quality_audit.quality；
  // v2 之前生成的卷没有分层报告 → hasReport=false 显示降级文案
  const quality = (item.quality_audit as { quality?: Record<string, unknown> } | undefined)?.quality;
  const rep = quality as
    | { checks?: Array<{ code?: string; result?: string }>; risks?: string[]; unverified?: string[] }
    | undefined;
  const hasReport = Array.isArray(rep?.checks);
  const sr = (item.difficulty_self_report ?? {}) as {
    steps?: unknown; context_type?: unknown; cognitive_claim?: unknown; basis?: unknown;
  };

  // 元数据（考点/难度/认知/审核原因）默认收进一行摘要徽标，details 展开看详情（F2 渐进披露）
  const epText = examPointName || (item.exam_point_id ? friendlyId(item.exam_point_id, '未匹配考点') : '');
  const epTitle = !examPointName && item.exam_point_id ? item.exam_point_id : undefined;
  const hasMeta = !!(epText || item.cognitive_level || (flagged && item.needs_review_reason));
  const headerBadges = (
    <>
      <span style={{ fontSize: '1.05rem', fontWeight: 600, color: 'var(--text-tertiary)' }}>{no}.</span>
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
          <div style={{ marginTop: '8px', padding: '8px 12px', borderRadius: 'var(--radius-sm)', background: 'var(--fill)', fontSize: '0.8rem', lineHeight: 1.7, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: '6px' }}>
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
              marginBottom: '12px', padding: '8px 12px', borderRadius: 'var(--radius-sm)', fontSize: '0.8rem', lineHeight: 1.6,
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
            <div style={{ marginTop: '14px', fontSize: '1rem', lineHeight: 1.8 }}>
              {/* 表格/代码块按导出同款渲染，综合题对比表不再是裸竖线纯文字 */}
              <StemBlocks text={item.stem} />
            </div>
          )}

          {/* 综合题分问：每问独立成块（浅底 + 左侧强调线），题面与分值分列两端 */}
          {subs.length > 0 && (
            <div style={{ marginTop: '12px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {subs.map((s, i) => (
                <div key={i} style={{
                  display: 'flex', gap: '10px', alignItems: 'flex-start',
                  padding: '8px 14px', borderRadius: 'var(--radius-sm)',
                  background: 'var(--fill)', borderLeft: '3px solid var(--accent)',
                  fontSize: '0.95rem', lineHeight: 1.75,
                }}>
                  <span style={{ fontWeight: 600, color: 'var(--accent)', flexShrink: 0 }}>（{i + 1}）</span>
                  <span style={{ flex: 1, minWidth: 0 }}>
                    {s.prompt
                      ? <StemBlocks text={s.prompt} />
                      : <span style={{ color: 'var(--text-tertiary)' }}>（缺分问题面）</span>}
                  </span>
                  {s.score != null && (
                    <span style={{ flexShrink: 0, fontSize: '0.8rem', color: 'var(--text-tertiary)', whiteSpace: 'nowrap', marginTop: 2 }}>
                      （{formatScore(s.score)} 分）
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}

          {opts.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '14px' }}>
              {opts.map((o) => {
                const isAns = keys.has(o.key.toUpperCase());
                return (
                  <div key={o.key} style={{
                    display: 'flex', gap: '10px', alignItems: 'flex-start', padding: '8px 12px', borderRadius: 'var(--radius-sm)',
                    background: isAns ? 'var(--success-subtle)' : 'var(--fill)',
                    fontSize: '0.925rem', lineHeight: 1.65,
                  }}>
                    <span style={{ fontWeight: 600, color: isAns ? 'var(--success)' : 'var(--text-tertiary)', minWidth: 16 }}>{o.key}.</span>
                    <span style={{ flex: 1, color: isAns ? 'var(--text)' : 'var(--text-secondary)' }}>{o.text}</span>
                    {isAns && <Check size={15} style={{ color: 'var(--success)', flexShrink: 0, marginTop: 3 }} />}
                  </div>
                );
              })}
            </div>
          ) : subAnswersComplete ? (
            /* 逐问答案：每问独立成块（成功色底），合并答案收进折叠区可查 */
            <div style={{ marginTop: '14px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {subs.map((s, i) => (
                <div key={i} style={{
                  padding: '8px 14px', borderRadius: 'var(--radius-sm)',
                  background: 'var(--success-subtle)', fontSize: '0.925rem', lineHeight: 1.75,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                }}>
                  <span style={{ fontWeight: 600, fontSize: '0.78rem', display: 'block', marginBottom: 3, opacity: 0.7, color: 'var(--success)' }}>
                    答案（{i + 1}）
                  </span>
                  {splitInlineSubList(s.answer)}
                </div>
              ))}
              {answerText && (
                <details style={{ fontSize: '0.875rem' }}>
                  <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>整题合并答案</summary>
                  <div style={{
                    marginTop: '6px', padding: '10px 14px', borderRadius: 'var(--radius-sm)',
                    background: 'var(--accent-subtle)', fontSize: '0.925rem', lineHeight: 1.75,
                    whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  }}>
                    {splitInlineSubList(answerText)}
                  </div>
                </details>
              )}
            </div>
          ) : (
            <div style={{
              marginTop: '14px', padding: '10px 14px', borderRadius: 'var(--radius-sm)', fontSize: '0.925rem', lineHeight: 1.75,
              background: answerText ? 'var(--accent-subtle)' : 'var(--warning-subtle)',
              color: answerText ? 'var(--text)' : 'var(--warning)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              <span style={{ fontWeight: 600, fontSize: '0.78rem', display: 'block', marginBottom: 3, opacity: 0.7 }}>答案</span>
              {/* 内联「（1）…（2）…」合并答案同样拆行，不再连排一段 */}
              {splitInlineSubList(answerText) || '未填写答案 —— 这道题导出答卷时会标注为「缺答案」，请补上'}
            </div>
          )}

          {item.explanation && (
            <details style={{ marginTop: '14px', fontSize: '0.875rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>解析</summary>
              <div style={{ marginTop: '6px', color: 'var(--text-secondary)', lineHeight: 1.75 }}>
                <StemBlocks text={item.explanation} />
              </div>
            </details>
          )}

          {item.rubric && (
            <details style={{ marginTop: '14px', fontSize: '0.875rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>评分细则</summary>
              <div style={{ marginTop: '6px', color: 'var(--text-secondary)', lineHeight: 1.75, whiteSpace: 'pre-wrap' }}>{rubricText(item.rubric)}</div>
            </details>
          )}

          {/* 难度依据（改进方案 2.4）：目标/规格/模型自陈/分层报告/教师确认，阅读态可见 */}
          <details
            style={{
              marginTop: '14px', fontSize: '0.875rem',
              border: '1px solid var(--line-soft)', borderRadius: '8px', padding: '8px 12px',
            }}
          >
            <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>
              难度依据{item.difficulty_confirmed ? ' · 已确认' : ''}
            </summary>
            <div style={{ marginTop: '8px', display: 'grid', gap: '8px', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              {/* 目标：难度档 + 认知层次（头部徽标是摘要，这里是面板的对照基准） */}
              <div>
                <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>目标：</span>
                {dlabel(item.difficulty ?? '')} · {clabel(item.cognitive_level ?? '')}
              </div>

              {/* 规格：difficulty_standard 确定性给出，历史题同样重建 */}
              {item.difficulty_spec?.features && (
                <div>
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>
                    要求（{item.difficulty_spec.label || item.difficulty_spec.tier}）
                    {item.difficulty_spec.standard_version ? ` · ${item.difficulty_spec.standard_version}` : ''}：
                  </span>
                  <div>
                    {String(item.difficulty_spec.features.operation ?? '')}
                    {typeof item.difficulty_spec.features.reasoning_step_range === 'string' &&
                      `；推理步数 ${item.difficulty_spec.features.reasoning_step_range}`}
                    {typeof item.difficulty_spec.features.context === 'string' &&
                      `；${item.difficulty_spec.features.context}`}
                  </div>
                </div>
              )}

              {/* 模型自陈：待核查说明，不是证据——展示供教师对照目标复核 */}
              {(sr.steps !== undefined || !!sr.context_type || !!sr.cognitive_claim || !!sr.basis) && (
                <div>
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>模型自陈（待核查说明）：</span>
                  {[
                    sr.steps !== undefined ? `步数 ${String(sr.steps)}` : '',
                    sr.context_type ? SELF_CONTEXT_LABELS[String(sr.context_type)] ?? String(sr.context_type) : '',
                    sr.cognitive_claim ? clabel(String(sr.cognitive_claim)) : '',
                  ].filter(Boolean).join(' · ')}
                  {typeof sr.basis === 'string' && sr.basis && (
                    <div style={{ color: 'var(--text-tertiary)' }}>依据：{sr.basis}</div>
                  )}
                </div>
              )}

              {/* 分层报告：检查了什么 / 风险 / 还有什么没验证 */}
              {hasReport ? (
                <div>
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>已检查：</span>
                  {(rep?.checks ?? [])
                    .map((c) => `${CHECK_LABELS[c.code ?? ''] ?? c.code ?? '?'}（${CHECK_RESULTS[c.result ?? ''] ?? c.result ?? '?'}）`)
                    .join('、')}
                  <div>
                    <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>风险：</span>
                    {(rep?.risks ?? []).length === 0 ? '无' : (
                      <ul style={{ margin: '2px 0 0', paddingLeft: '18px' }}>
                        {(rep?.risks ?? []).map((risk, i) => (
                          <li key={i} style={{ color: '#b45309' }}>{risk}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div>
                    <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>未验证（需人工）：</span>
                    {(rep?.unverified ?? []).map((u) => UNVERIFIED_LABELS[u] ?? u).join('、') || '无'}
                  </div>
                </div>
              ) : (
                <div style={{ color: 'var(--text-tertiary)' }}>
                  本卷按难度标准 v2 之前生成，暂无分层检查报告。
                </div>
              )}

              {/* 确认：谁、何时；改题后由后端自动重置回待确认 */}
              <div
                style={{
                  display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap',
                  borderTop: '1px dashed var(--line-soft)', paddingTop: '8px',
                }}
              >
                {item.difficulty_confirmed ? (
                  <>
                    <Badge variant="success">已确认</Badge>
                    <span style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>
                      {item.difficulty_confirmed_by || '—'}
                      {item.difficulty_confirmed_at
                        ? ` · ${new Date(item.difficulty_confirmed_at).toLocaleString()}`
                        : ''}
                    </span>
                    {!readonly && onSetDifficultyConfirm && (
                      <Button variant="secondary" size="sm" onClick={() => onSetDifficultyConfirm(false)}>
                        撤销确认
                      </Button>
                    )}
                  </>
                ) : (
                  <>
                    <Badge variant="default">待确认</Badge>
                    {!readonly && onSetDifficultyConfirm && (
                      <Button
                        size="sm"
                        onClick={() => onSetDifficultyConfirm(true)}
                        icon={<Check size={14} />}
                        disabled={submitting}
                      >
                        确认难度依据
                      </Button>
                    )}
                  </>
                )}
              </div>
            </div>
          </details>

          {/* marginTop:auto 把按钮行推到卡片底部（拉伸后的卡片内部留白落在内容与按钮之间） */}
          <div style={{ display: 'flex', gap: '8px', marginTop: 'auto', paddingTop: '16px', borderTop: '1px solid var(--line-soft)', flexWrap: 'wrap', alignItems: 'center' }}>
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