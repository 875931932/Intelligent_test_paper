import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import {
  Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, FileJson, FileText, KeySquare,
  Pencil, Plus, RotateCcw, Save, Trash2,
} from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui';
import { useNameMaps } from '@/hooks/useNameMaps';
import {
  DIFFICULTY_OPTIONS, EXAM_PROJECT_STATUS_META, PAPER_STATUS_META,
  QUESTION_TYPE_OPTIONS, QUESTION_TYPE_ORDER, dlabel, qlabel, sectionLabel,
} from '@/lib/examDisplay';
import type { ExamProject, PaperVersion, PaperVersionItem } from '@/types/api';

// ─── 题型与选项工具 ───

function isChoiceType(t: string): boolean {
  return t === 'single_choice' || t === 'multiple_choice' || t === 'true_false';
}

type OptEntry = { key: string; text: string };

function optionsToEntries(options?: Record<string, string> | string[]): OptEntry[] {
  if (Array.isArray(options)) {
    return options.map((v, i) => ({ key: String.fromCharCode(65 + i), text: String(v) }));
  }
  return Object.entries(options || {}).map(([k, v]) => ({ key: k, text: String(v) }));
}

function entriesToOptions(entries: OptEntry[]): Record<string, string> {
  const o: Record<string, string> = {};
  entries.forEach((e, i) => {
    o[e.key || String.fromCharCode(65 + i)] = e.text;
  });
  return o;
}

/** 多选答案在选项行上切换；单选直接替换。兼容 "AB" / "A,B" / "A、B" 等写法 */
function toggleAnswerKey(answer: string, key: string, multi: boolean): string {
  if (!multi) return key;
  const set = new Set((answer || '').toUpperCase().replace(/[^A-Z]/g, '').split(''));
  if (set.has(key)) set.delete(key);
  else set.add(key);
  return [...set].sort().join('');
}

function answerKeys(answer: string): Set<string> {
  return new Set((answer || '').toUpperCase().replace(/[^A-Z]/g, '').split(''));
}

// ─── 编辑草稿 ───

interface Draft {
  stem: string;
  question_type: string;
  difficulty: string;
  score: string;
  answer: string;
  explanation: string;
  options: OptEntry[];
}

interface EditorSubmit {
  stem: string;
  question_type: string;
  difficulty: string;
  score: number;
  answer: string;
  explanation: string;
  options: Record<string, string>;
  clear_needs_review?: boolean;
}

function draftFromItem(item: PaperVersionItem): Draft {
  return {
    stem: item.stem ?? '',
    question_type: item.question_type || 'short_answer',
    difficulty: item.difficulty || 'medium',
    score: String(item.score ?? 0),
    answer: item.answer ?? '',
    explanation: item.explanation ?? '',
    options: optionsToEntries(item.options),
  };
}

const emptyDraft = (): Draft => ({
  stem: '',
  question_type: 'short_answer',
  difficulty: 'medium',
  score: '5',
  answer: '',
  explanation: '',
  options: [{ key: 'A', text: '' }, { key: 'B', text: '' }],
});

// ─── 题目编辑器（右栏原地编辑与「新增题目」弹窗共用） ───

export interface QuestionEditorHandle {
  submit: () => void;
}

const QuestionEditor = forwardRef<QuestionEditorHandle, {
  initial: Draft;
  needsReview: boolean;
  submitting: boolean;
  submitLabel: string;
  showActions: boolean;
  onSubmit: (v: EditorSubmit) => void;
  onCancel: () => void;
}>(function QuestionEditor(
  { initial, needsReview, submitting, submitLabel, showActions, onSubmit, onCancel },
  ref,
) {
  const [d, setD] = useState<Draft>(initial);
  const [clearReview, setClearReview] = useState(true);
  const choice = isChoiceType(d.question_type);
  const multi = d.question_type === 'multiple_choice';

  const patch = (p: Partial<Draft>) => setD((prev) => ({ ...prev, ...p }));

  const changeType = (t: string) => {
    const nextChoice = isChoiceType(t);
    patch({
      question_type: t,
      options: nextChoice ? (d.options.length > 0 ? d.options : [{ key: 'A', text: '' }, { key: 'B', text: '' }]) : [],
      answer: nextChoice ? d.answer : '',
    });
  };

  const submit = () => {
    onSubmit({
      stem: d.stem.trim(),
      question_type: d.question_type,
      difficulty: d.difficulty,
      score: Number(d.score) || 0,
      answer: d.answer,
      explanation: d.explanation,
      options: choice ? entriesToOptions(d.options) : {},
      clear_needs_review: needsReview ? clearReview : undefined,
    });
  };

  // 供 Modal footer 之类的容器触发提交，避免把表单 state 提到父级
  useImperativeHandle(ref, () => ({ submit }));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
        <FieldLabel label="题型">
          <select className="input-field" value={d.question_type} onChange={(e) => changeType(e.target.value)}>
            {QUESTION_TYPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </FieldLabel>
        <FieldLabel label="难度">
          <select className="input-field" value={d.difficulty} onChange={(e) => patch({ difficulty: e.target.value })}>
            {DIFFICULTY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </FieldLabel>
        <FieldLabel label="分值">
          <input className="input-field" type="number" min={0} step="0.5" style={{ width: 90 }} value={d.score} onChange={(e) => patch({ score: e.target.value })} />
        </FieldLabel>
      </div>

      <FieldLabel label="题干">
        <textarea className="input-field" rows={3} value={d.stem} onChange={(e) => patch({ stem: e.target.value })} />
      </FieldLabel>

      {choice && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
            <span style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>选项</span>
            <Button variant="ghost" size="sm" onClick={() => patch({ options: [...d.options, { key: String.fromCharCode(65 + d.options.length), text: '' }] })} icon={<Plus size={14} />}>加选项</Button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {d.options.map((o, oi) => (
              <div key={oi} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ width: 18, fontWeight: 600 }}>{o.key}.</span>
                <input className="input-field" style={{ flex: 1 }} value={o.text} onChange={(e) => patch({ options: d.options.map((x, i) => (i === oi ? { ...x, text: e.target.value } : x)) })} />
                <Button
                  variant="ghost" size="sm"
                  onClick={() => patch({ answer: toggleAnswerKey(d.answer, o.key, multi) })}
                  style={answerKeys(d.answer).has(o.key) ? { color: 'var(--success)' } : undefined}
                  title={multi ? '切换选中' : '设为答案'}
                >
                  <Check size={14} /> {answerKeys(d.answer).has(o.key) ? '是答案' : '设为答案'}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => patch({ options: d.options.filter((_, i) => i !== oi) })} icon={<Trash2 size={14} />} />
              </div>
            ))}
          </div>
        </div>
      )}

      <FieldLabel label="答案">
        <input className="input-field" value={d.answer} onChange={(e) => patch({ answer: e.target.value })} placeholder={choice ? (multi ? '如 AB' : '如 B') : '填写参考答案或评分要点'} />
      </FieldLabel>

      <FieldLabel label="解析">
        <textarea className="input-field" rows={2} value={d.explanation} onChange={(e) => patch({ explanation: e.target.value })} />
      </FieldLabel>

      {needsReview && (
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.8125rem', color: 'var(--text-secondary)', cursor: 'pointer' }}>
          <input type="checkbox" checked={clearReview} onChange={(e) => setClearReview(e.target.checked)} />
          本题已处理完毕，保存时清除「需审核」标记
        </label>
      )}

      {showActions && (
        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
          <Button variant="secondary" size="sm" onClick={onCancel}>取消</Button>
          <Button size="sm" loading={submitting} onClick={submit} icon={<Save size={14} />}>{submitLabel}</Button>
        </div>
      )}
    </div>
  );
});

function FieldLabel({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', minWidth: 0 }}>
      <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>
      {children}
    </div>
  );
}

// ─── 试卷档案卡 ───

function PaperProfile({
  pv, project, examPointCount, onExport, onFinalize, onRevert,
}: {
  pv: PaperVersion;
  project?: ExamProject;
  examPointCount: number;
  onExport: (kind: 'student' | 'answer' | 'json') => void;
  onFinalize: () => void;
  onRevert: () => void;
}) {
  const questions = pv.questions;
  const typeAcc = new Map<string, { score: number; count: number }>();
  const diffAcc = new Map<string, number>();
  questions.forEach((q) => {
    const t = typeAcc.get(q.question_type) ?? { score: 0, count: 0 };
    t.score += q.score || 0;
    t.count += 1;
    typeAcc.set(q.question_type, t);
    const dk = q.difficulty || 'medium';
    diffAcc.set(dk, (diffAcc.get(dk) || 0) + 1);
  });
  const pending = questions.filter((q) => q.needs_review || q.needs_review_reason).length;
  const overridden = questions.filter((q) => q.has_override).length;
  const psm = PAPER_STATUS_META[pv.status] ?? { label: pv.status, variant: 'default' as const };
  const orderedTypes = [...typeAcc.keys()].sort(
    (a, b) => QUESTION_TYPE_ORDER.indexOf(a) - QUESTION_TYPE_ORDER.indexOf(b),
  );

  return (
    <div className="glass-card" style={{ padding: '18px 22px' }}>
      <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div style={{ minWidth: 104 }}>
          <div style={{ fontSize: '2rem', fontWeight: 700, lineHeight: 1, letterSpacing: '-0.03em' }}>{pv.total_score}</div>
          <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: 5 }}>
            总分 · {questions.length} 题 · v{pv.version_no}
          </div>
          <div style={{ marginTop: '8px' }}><Badge variant={psm.variant}>{psm.label}</Badge></div>
        </div>

        <div style={{ flex: 1, minWidth: 240, display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {orderedTypes.map((t) => {
              const v = typeAcc.get(t)!;
              return (
                <span key={t} style={{
                  padding: '4px 10px', borderRadius: 999, fontSize: '0.76rem', fontWeight: 600,
                  background: 'var(--accent-subtle)', color: 'var(--accent)',
                }}>
                  {qlabel(t)} {v.score}分·{v.count}题
                </span>
              );
            })}
          </div>
          <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            <span>难度：{['easy', 'medium', 'hard'].map((d) => `${dlabel(d)} ${diffAcc.get(d) ?? 0}`).join(' · ')}</span>
            <span>覆盖 {examPointCount} 个考点</span>
            {overridden > 0 && <span>已修改 {overridden} 题</span>}
            {pending > 0 && <span style={{ color: 'var(--warning)', fontWeight: 600 }}>待审核 {pending} 题</span>}
          </div>
          {project && (
            <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              所属项目：{project.name} · {(EXAM_PROJECT_STATUS_META[project.status] ?? { label: project.status }).label}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <Button variant="secondary" size="sm" onClick={() => onExport('student')} icon={<FileText size={14} />}>学生卷</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('answer')} icon={<KeySquare size={14} />}>答卷</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('json')} icon={<FileJson size={14} />}>答案细则</Button>
          {pv.status === 'finalized' ? (
            <Button variant="secondary" size="sm" onClick={onRevert} icon={<RotateCcw size={14} />}>撤销定稿</Button>
          ) : (
            <Button size="sm" onClick={onFinalize} icon={<Check size={14} />}>确认定稿</Button>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── 左栏：题号索引 ───

function QuestionIndex({
  groups, selected, onSelect,
}: {
  groups: Array<{ key: string; label: string; items: PaperVersionItem[] }>;
  selected: number;
  onSelect: (itemIndex: number) => void;
}) {
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
                <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>{item.score}分</span>
                <span style={{ marginLeft: 'auto', display: 'flex', gap: '4px' }}>
                  {flagged && (
                    <span title="待审核" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--warning)' }} />
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

// ─── 右栏：当前题目详情 / 编辑器 ───

function QuestionDetail({
  item, examPointName, editing, readonly, submitting,
  hasPrev, hasNext, onEdit, onCancelEdit, onSave, onDelete, onMove, onPrev, onNext,
}: {
  item: PaperVersionItem;
  examPointName?: string;
  editing: boolean;
  readonly: boolean;
  submitting: boolean;
  hasPrev: boolean;
  hasNext: boolean;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSave: (v: EditorSubmit) => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const flagged = item.needs_review || !!item.needs_review_reason;
  const keys = answerKeys(item.answer);
  const opts = optionsToEntries(item.options);

  return (
    <div
      className="glass-card"
      style={{
        padding: '22px 26px',
        borderLeft: '3px solid ' + (flagged ? 'var(--warning)' : 'rgba(0,113,227,0.35)'),
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-tertiary)' }}>{item.item_index}.</span>
        <Badge variant="info">{qlabel(item.question_type)}</Badge>
        <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{item.score} 分</span>
        {item.difficulty && <Badge variant="default">{dlabel(item.difficulty)}</Badge>}
        {item.has_override && <Badge variant="purple">已修改</Badge>}
        {flagged && <Badge variant="warning">需审核</Badge>}
      </div>

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
              background: item.answer ? 'var(--accent-subtle)' : 'var(--warning-subtle)',
              color: item.answer ? 'var(--text)' : 'var(--warning)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              <span style={{ fontWeight: 600, fontSize: '0.78rem', display: 'block', marginBottom: 3, opacity: 0.7 }}>答案</span>
              {item.answer || '未填写答案'}
            </div>
          )}

          {item.explanation && (
            <details style={{ marginTop: '14px', fontSize: '0.875rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>解析</summary>
              <div style={{ marginTop: '6px', color: 'var(--text-secondary)', lineHeight: 1.75, whiteSpace: 'pre-wrap' }}>{item.explanation}</div>
            </details>
          )}

          {flagged && item.needs_review_reason && (
            <div style={{
              marginTop: '14px', padding: '10px 14px', borderRadius: 8, fontSize: '0.825rem', lineHeight: 1.65,
              background: 'var(--warning-subtle)', color: 'var(--text-secondary)',
            }}>
              <span style={{ fontWeight: 600, color: 'var(--warning)' }}>待审核原因：</span>{item.needs_review_reason}
            </div>
          )}

          {(examPointName || item.exam_point_id) && (
            <div style={{ marginTop: '14px', fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              考点：{examPointName || item.exam_point_id}
            </div>
          )}

          <div style={{ display: 'flex', gap: '8px', marginTop: '18px', paddingTop: '16px', borderTop: '1px solid rgba(0,0,0,0.06)', flexWrap: 'wrap', alignItems: 'center' }}>
            {!readonly && (
              <Button size="sm" onClick={onEdit} icon={<Pencil size={14} />}>编辑本题</Button>
            )}
            <div style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
              <Button variant="secondary" size="sm" disabled={!hasPrev} onClick={onPrev} icon={<ChevronLeft size={14} />}>上一题</Button>
              <Button variant="secondary" size="sm" disabled={!hasNext} onClick={onNext}>下一题<ChevronRight size={14} /></Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════
//  试卷面板（双栏阅读器）
// ═══════════════════════════════════════════════
export default function PaperPanel({
  pv, project, courseId, onChanged,
}: {
  pv: PaperVersion;
  project?: ExamProject;
  courseId: string;
  /** 增删改后通知父级刷新试卷与项目摘要 */
  onChanged: () => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const { maps, reload: reloadMaps } = useNameMaps(courseId);

  const questions = pv.questions;
  const readonly = pv.status === 'finalized';

  const [selected, setSelected] = useState<number>(() => questions[0]?.item_index ?? 0);
  const [editing, setEditing] = useState(false);
  const [onlyNeedsReview, setOnlyNeedsReview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [finalizeOpen, setFinalizeOpen] = useState(false);
  const addEditorRef = useRef<QuestionEditorHandle | null>(null);

  useEffect(() => {
    void reloadMaps();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  // 试卷刷新后保持选中项；原选中题被删则落到第一题
  useEffect(() => {
    if (questions.length > 0 && !questions.some((q) => q.item_index === selected)) {
      setSelected(questions[0].item_index);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pv.id, questions.length]);

  // 分组（按题型分节，节内保持卷面顺序）
  const groups = useMemo(() => {
    const filtered = onlyNeedsReview
      ? questions.filter((q) => q.needs_review || q.needs_review_reason)
      : questions;
    const byType = new Map<string, PaperVersionItem[]>();
    filtered.forEach((q) => {
      const arr = byType.get(q.question_type) ?? [];
      arr.push(q);
      byType.set(q.question_type, arr);
    });
    return [...byType.entries()]
      .sort((a, b) => QUESTION_TYPE_ORDER.indexOf(a[0]) - QUESTION_TYPE_ORDER.indexOf(b[0]))
      .map(([t, items]) => ({ key: t, label: sectionLabel(t), items }));
  }, [questions, onlyNeedsReview]);

  // 上一题/下一题与键盘导航一律按「卷面顺序」走（不是左栏的分组顺序），
  // 否则从简答下一题会跳到另一节的简答，看着像漏了一题。
  const navList = useMemo(
    () => (onlyNeedsReview ? questions.filter((q) => q.needs_review || q.needs_review_reason) : questions),
    [questions, onlyNeedsReview],
  );
  const navIdx = navList.findIndex((q) => q.item_index === selected);
  // 右栏只渲染可见（未被筛选掉）的题目
  const current = navIdx >= 0 ? navList[navIdx] : undefined;
  const pendingItems = useMemo(
    () => questions.filter((q) => q.needs_review || q.needs_review_reason),
    [questions],
  );

  const step = (dir: -1 | 1) => {
    const next = navList[navIdx + dir];
    if (next) {
      setSelected(next.item_index);
      setEditing(false);
    }
  };

  // 打开「仅看待审核」时，若当前题被滤掉就跳到第一道待审题，避免右栏空着
  const toggleFilter = () => {
    const next = !onlyNeedsReview;
    setOnlyNeedsReview(next);
    if (next) {
      const first = questions.find((q) => q.needs_review || q.needs_review_reason);
      if (first) {
        setSelected(first.item_index);
        setEditing(false);
      }
    }
  };

  // 键盘 ↑/↓ 翻题：焦点在输入框内或正在编辑时不抢占
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (editing || addOpen || finalizeOpen) return;
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (e.key === 'ArrowUp') { e.preventDefault(); step(-1); }
      if (e.key === 'ArrowDown') { e.preventDefault(); step(1); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navList, navIdx, editing, addOpen, finalizeOpen]);

  // ── 编辑操作 ──

  const handleSave = async (idx: number, v: EditorSubmit) => {
    const { clear_needs_review, ...patch } = v;
    setSaving(true);
    try {
      await api.paperVersions.patchItem(courseId, pv.id, idx, {
        teacher_override_patch: patch,
        clear_needs_review,
      });
      addToast(`第 ${idx} 题已保存`, 'success');
      setEditing(false);
      onChanged();
    } catch (e) {
      addToast('保存失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (idx: number) => {
    if (!window.confirm(`确认删除第 ${idx} 题？删除后其后的题目题号会前移。`)) return;
    setSaving(true);
    try {
      await api.paperVersions.deleteItem(courseId, pv.id, idx, token ?? undefined);
      addToast('题目已删除', 'success');
      setEditing(false);
      onChanged();
    } catch (e) {
      addToast('删除失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleMove = async (pos: number, dir: -1 | 1) => {
    const ordered = questions.map((q) => q.item_index);
    const newPos = pos + dir;
    if (newPos < 0 || newPos >= ordered.length) return;
    const tmp = ordered[pos];
    ordered[pos] = ordered[newPos];
    ordered[newPos] = tmp;
    setSaving(true);
    try {
      await api.paperVersions.reorderItems(courseId, pv.id, ordered, token ?? undefined);
      onChanged();
    } catch (e) {
      addToast('调整顺序失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleAdd = async (v: EditorSubmit) => {
    setAdding(true);
    try {
      await api.paperVersions.createItem(courseId, pv.id, { ...v }, token ?? undefined);
      addToast('新题已加入试卷末尾', 'success');
      setAddOpen(false);
      onChanged();
    } catch (e) {
      addToast('新增失败: ' + getErrorMessage(e), 'error');
    } finally {
      setAdding(false);
    }
  };

  // ── 定稿 ──

  const doFinalize = async (force: boolean) => {
    try {
      await api.paperVersions.confirm(courseId, pv.id, force ? { force_ignore_needs_review: true } : {}, token ?? undefined);
      addToast('试卷已定稿', 'success');
      setFinalizeOpen(false);
      onChanged();
    } catch (e) {
      addToast('定稿失败: ' + getErrorMessage(e), 'error');
    }
  };

  const handleFinalizeClick = () => {
    if (pendingItems.length > 0) {
      setFinalizeOpen(true);
      return;
    }
    void doFinalize(false);
  };

  const handleRevert = async () => {
    if (!window.confirm('撤销定稿并回到待审核状态？')) return;
    try {
      await api.paperVersions.revert(courseId, pv.id, token ?? undefined);
      addToast('已撤销定稿', 'success');
      onChanged();
    } catch (e) {
      addToast('撤销失败: ' + getErrorMessage(e), 'error');
    }
  };

  // ── 导出 ──

  const handleExport = (kind: 'student' | 'answer' | 'json') => {
    const url =
      kind === 'student'
        ? api.paperVersions.exportStudent(courseId, project?.id ?? '', pv.id)
        : kind === 'answer'
          ? api.paperVersions.exportAnswerKey(courseId, project?.id ?? '', pv.id)
          : api.paperVersions.exportJson(courseId, project?.id ?? '', pv.id);
    window.open(url, '_blank', 'noopener');
  };

  if (questions.length === 0) {
    return (
      <div className="glass-card" style={{ padding: '48px 20px', textAlign: 'center' }}>
        <h3 style={{ fontWeight: 600, fontSize: '1rem', marginBottom: '8px' }}>这份试卷还没有题目</h3>
        <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
          生成完成后题目会出现在这里；也可以手动新增一道题目。
        </p>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
      <PaperProfile
        pv={pv}
        project={project}
        examPointCount={new Set(questions.map((q) => q.exam_point_id).filter(Boolean)).size}
        onExport={handleExport}
        onFinalize={handleFinalizeClick}
        onRevert={handleRevert}
      />

      {/* 双栏：左题号索引，右当前题目 */}
      <div style={{ display: 'flex', gap: '14px', alignItems: 'flex-start' }}>
        <div
          className="glass-card"
          style={{
            width: 240, flexShrink: 0, padding: '10px 8px 14px',
            position: 'sticky', top: 16, maxHeight: 'calc(100vh - 140px)', overflowY: 'auto',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '0 6px 8px' }}>
            <Button
              variant={onlyNeedsReview ? 'primary' : 'secondary'} size="sm"
              onClick={toggleFilter}
            >
              仅看待审核{`（${pendingItems.length}）`}
            </Button>
          </div>
          {groups.length === 0 ? (
            <p style={{ padding: '16px 8px', fontSize: '0.8rem', color: 'var(--text-tertiary)', textAlign: 'center' }}>
              没有待审核的题目
            </p>
          ) : (
            <QuestionIndex groups={groups} selected={selected} onSelect={(idx) => { setSelected(idx); setEditing(false); }} />
          )}
          {!readonly && (
            <div style={{ padding: '10px 6px 0', marginTop: '6px', borderTop: '1px solid rgba(0,0,0,0.06)' }}>
              <Button variant="secondary" size="sm" onClick={() => setAddOpen(true)} icon={<Plus size={14} />} style={{ width: '100%' }}>
                新增题目
              </Button>
            </div>
          )}
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          {current ? (
            <QuestionDetail
              key={current.item_index}
              item={current}
              examPointName={current.exam_point_id ? maps.examPoints[current.exam_point_id] : undefined}
              editing={editing}
              readonly={readonly}
              submitting={saving}
              hasPrev={navIdx > 0}
              hasNext={navIdx >= 0 && navIdx < navList.length - 1}
              onEdit={() => setEditing(true)}
              onCancelEdit={() => setEditing(false)}
              onSave={(v) => handleSave(current.item_index, v)}
              onDelete={() => handleDelete(current.item_index)}
              onMove={(dir) => handleMove(questions.findIndex((q) => q.item_index === current.item_index), dir)}
              onPrev={() => step(-1)}
              onNext={() => step(1)}
            />
          ) : (
            <div className="glass-card" style={{ padding: '40px 20px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>
              请在左侧选择题号
            </div>
          )}
          <p style={{ marginTop: '10px', fontSize: '0.75rem', color: 'var(--text-tertiary)', textAlign: 'center' }}>
            提示：可用键盘 ↑ / ↓ 快速翻题
          </p>
        </div>
      </div>

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="新增题目"
        maxWidth="720px"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAddOpen(false)}>取消</Button>
            <Button loading={adding} onClick={() => addEditorRef.current?.submit()} icon={<Plus size={14} />}>加入试卷</Button>
          </>
        }
      >
        <QuestionEditor
          ref={addEditorRef}
          initial={emptyDraft()}
          needsReview={false}
          submitting={adding}
          submitLabel="加入试卷"
          showActions={false}
          onSubmit={handleAdd}
          onCancel={() => setAddOpen(false)}
        />
      </Modal>

      <Modal
        open={finalizeOpen}
        onClose={() => setFinalizeOpen(false)}
        title="还有待审核的题目"
        maxWidth="520px"
        footer={
          <>
            <Button variant="secondary" onClick={() => setFinalizeOpen(false)}>返回处理</Button>
            <Button onClick={() => doFinalize(true)}>仍要定稿</Button>
          </>
        }
      >
        <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
          以下 {pendingItems.length} 道题被质量检查标记为待审核，建议先逐题处理：
        </p>
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '12px' }}>
          {pendingItems.map((q) => (
            <button
              key={q.item_index}
              onClick={() => { setSelected(q.item_index); setOnlyNeedsReview(false); setFinalizeOpen(false); }}
              style={{
                padding: '3px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                background: 'var(--warning-subtle)', color: 'var(--warning)', border: 'none', cursor: 'pointer',
              }}
            >
              第 {q.item_index} 题
            </button>
          ))}
        </div>
        <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '14px', lineHeight: 1.6 }}>
          也可以打开「仅看待审核」逐题核对。确已知悉时可选择「仍要定稿」。
        </p>
      </Modal>
    </div>
  );
}
