import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import {
  Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, ClipboardList, ExternalLink, Eye,
  FileJson, FileSearch, FileText, KeySquare,
  Pencil, Plus, RefreshCw, RotateCcw, Save, Sparkles, Trash2,
} from 'lucide-react';
import { api } from '@/api/client';
import type { PaperExportKind } from '@/api/domains/paperVersions';
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
import { formatScore, friendlyId } from '@/lib/format';
import type { AiCreateProposal, ExamProject, PaperVersion, PaperVersionItem } from '@/types/api';
import { AiCreatePanel } from './AiCreatePanel';
import { AiRevisePanel } from './AiRevisePanel';
import { PaperReviewPanel } from './PaperReviewPanel';

// ─── 题型与选项工具 ───

/** 有选项需要编辑的题型。判断题答案是布尔值、没有 options 字段，不能混进来 */
function hasOptions(t: string): boolean {
  return t === 'single_choice' || t === 'multiple_choice';
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

/**
 * 判断题答案在后端是布尔值（true/false）且没有 options；展示层统一成字符串口径，
 * 否则对布尔值调字符串方法会直接抛 TypeError。
 */
function normalizeAnswer(answer: unknown): string {
  if (typeof answer === 'boolean') return answer ? '正确' : '错误';
  if (answer == null) return '';
  return String(answer);
}

/**
 * 答案解析成选项字母，与后端 answer_option_keys 同口径：
 * 模型有时返回 'B' / 'ABD'，有时按 schema 约定返回选项原文，教师手写时还会写成
 * '甲、丙' 这种并列原文。只有整串都是选项字母时才按字母解析，否则按原文匹配，
 * 避免把 'LoRA' 里的 L/O/R/A 误当成选项字母。
 */
function optionKeysOf(answer: unknown, optionTexts: string[]): Set<string> {
  const text = normalizeAnswer(answer);
  if (!text) return new Set();
  const keys = optionTexts.map((_, i) => String.fromCharCode(65 + i));

  const resolveOne = (part: string): Set<string> => {
    const upper = part.toUpperCase();
    if (upper && [...upper].every((c) => keys.includes(c))) return new Set(upper);
    const exact = new Set<string>();
    optionTexts.forEach((t, i) => {
      if (t === part) exact.add(keys[i]);
    });
    return exact;
  };

  const compact = text.toUpperCase().replace(/[,，、；;\s]+/g, '');
  if (compact && [...compact].every((c) => keys.includes(c))) return new Set(compact);
  const parts = text.split(/[,，、；;]+/).filter((p) => p.trim());
  if (parts.length > 1) {
    const resolved = new Set<string>();
    parts.forEach((p) => resolveOne(p.trim()).forEach((k) => resolved.add(k)));
    return resolved;
  }
  return resolveOne(text);
}

/** 在已解析的选项字母上切换（多选累加，单选替换） */
function toggleAnswerKey(keys: Set<string>, key: string, multi: boolean): string {
  if (!multi) return key;
  const next = new Set(keys);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return [...next].sort().join('');
}

// ─── 编辑草稿 ───

/** rubric 两态统一成换行文本：生成侧是要点数组，表单按文本编辑 */
function rubricText(v: string | string[] | null | undefined): string {
  return Array.isArray(v) ? v.join('\n') : (v ?? '');
}

interface Draft {
  stem: string;
  question_type: string;
  difficulty: string;
  score: string;
  answer: string;
  explanation: string;
  /** 评分细则（主观题）：每行一个要点 */
  rubric: string;
  options: OptEntry[];
}

interface EditorSubmit {
  stem: string;
  question_type: string;
  difficulty: string;
  score: number;
  /** 判断题提交布尔值（后端该题型强校验 bool），其余题型为字符串 */
  answer: string | boolean;
  explanation: string;
  /** 评分细则（主观题）；原文进 teacher_override / POST items，客观题为空串 */
  rubric: string;
  options: Record<string, string>;
  clear_needs_review?: boolean;
}

/** 编辑框按文本编辑，提交时判断题答案规范化回布尔值 */
function answerForSubmit(questionType: string, answer: string): string | boolean {
  if (questionType !== 'true_false') return answer;
  const a = answer.trim();
  if (['true', '正确', '对', '是', 'T'].includes(a)) return true;
  if (['false', '错误', '错', '否', 'F'].includes(a)) return false;
  return a;
}

function draftFromItem(item: PaperVersionItem): Draft {
  return {
    stem: item.stem ?? '',
    question_type: item.question_type || 'short_answer',
    difficulty: item.difficulty || 'medium',
    score: String(item.score ?? 0),
    answer: normalizeAnswer(item.answer),
    explanation: item.explanation ?? '',
    rubric: rubricText(item.rubric),
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
  rubric: '',
  options: [{ key: 'A', text: '' }, { key: 'B', text: '' }],
});

/**
 * AI 整题提案 → 新增表单草稿：题面字段全量回填，教师微调后走既有「加入试卷」。
 * 分值不进提案（是教师的总分决策），保留表单默认值由教师自己定。
 */
function draftFromProposal(p: AiCreateProposal): Draft {
  const choice = hasOptions(p.question_type);
  let options = choice ? optionsToEntries(p.options ?? undefined) : [];
  // 选择题提案缺选项时兜底给 A-D 空行，教师可直接补（正常不会发生：校验已拦）
  if (choice && options.length === 0) {
    options = [
      { key: 'A', text: '' }, { key: 'B', text: '' }, { key: 'C', text: '' }, { key: 'D', text: '' },
    ];
  }
  const difficulty = DIFFICULTY_OPTIONS.some((o) => o.value === p.difficulty)
    ? (p.difficulty as string)
    : 'medium';
  return {
    stem: p.stem ?? '',
    question_type: p.question_type || 'short_answer',
    difficulty,
    score: '5',
    answer: normalizeAnswer(p.answer),
    explanation: p.explanation ?? '',
    rubric: rubricText(p.rubric),
    options,
  };
}

// ─── 题目编辑器（右栏原地编辑与「新增题目」弹窗共用） ───

export interface QuestionEditorHandle {
  submit: () => void;
  /** 用外部来源（AI 生成提案）整体替换表单草稿 */
  setDraft: (d: Draft) => void;
}

const QuestionEditor = forwardRef<QuestionEditorHandle, {
  initial: Draft;
  needsReview: boolean;
  submitting: boolean;
  submitLabel: string;
  showActions: boolean;
  onSubmit: (v: EditorSubmit) => void;
  onCancel: () => void;
  /** 草稿相对 initial 是否有改动；父级凭它在切题/翻题/换序前拦一次 */
  onDirtyChange?: (dirty: boolean) => void;
}>(function QuestionEditor(
  { initial, needsReview, submitting, submitLabel, showActions, onSubmit, onCancel, onDirtyChange },
  ref,
) {
  const [d, setD] = useState<Draft>(initial);
  const [clearReview, setClearReview] = useState(true);
  const choice = hasOptions(d.question_type);
  const multi = d.question_type === 'multiple_choice';
  // 与生成链路 validate_generated_question 同口径：简答/综合必须给评分细则
  const subjective = d.question_type === 'short_answer' || d.question_type === 'comprehensive';

  const patch = (p: Partial<Draft>) => setD((prev) => ({ ...prev, ...p }));

  // initial 每次渲染都是新对象，按内容比较而非引用；只在草稿变化时上报。
  useEffect(() => {
    onDirtyChange?.(JSON.stringify(d) !== JSON.stringify(initial));
  }, [d, initial, onDirtyChange]);

  const changeType = (t: string) => {
    const nextChoice = hasOptions(t);
    const options = nextChoice
      ? (d.options.length > 0 ? d.options : [{ key: 'A', text: '' }, { key: 'B', text: '' }])
      : [];
    // 切到选择题时旧答案多半是一段文字（如简答的评分要点），作为选项答案非法；
    // 只有能解析成选项字母（或恰好等于某个选项原文）才保留，否则清空重填。
    // 判断题答案是布尔值、本就无对应字母，沿用原逻辑直接清空。
    const answer = nextChoice
      ? (optionKeysOf(d.answer, options.map((o) => o.text)).size > 0 ? d.answer : '')
      : '';
    patch({ question_type: t, options, answer });
  };

  const submit = () => {
    onSubmit({
      stem: d.stem.trim(),
      question_type: d.question_type,
      difficulty: d.difficulty,
      score: Number(d.score) || 0,
      answer: answerForSubmit(d.question_type, d.answer),
      explanation: d.explanation,
      rubric: d.rubric,
      options: choice ? entriesToOptions(d.options) : {},
      clear_needs_review: needsReview ? clearReview : undefined,
    });
  };

  // 供 Modal footer 之类的容器触发提交，避免把表单 state 提到父级
  // setDraft：AI 生成提案整体覆盖草稿（props 只在挂载时生效，外部覆盖须走 handle）
  useImperativeHandle(ref, () => ({ submit, setDraft: setD }));

  // 选项字母随 d.answer / d.options 每次渲染都要用；原先在选项行里内联算 3 次
  // （图标、配色、文案各一次），提到这里算一次即可。
  const optionTexts = d.options.map((o) => o.text);
  const ansKeys = choice ? optionKeysOf(d.answer, optionTexts) : new Set<string>();

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
                  onClick={() => patch({ answer: toggleAnswerKey(ansKeys, o.key, multi) })}
                  style={ansKeys.has(o.key) ? { color: 'var(--success)' } : undefined}
                  title={multi ? '切换选中' : '设为答案'}
                >
                  <Check size={14} /> {ansKeys.has(o.key) ? '是答案' : '设为答案'}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => patch({ options: d.options.filter((_, i) => i !== oi) })} icon={<Trash2 size={14} />} />
              </div>
            ))}
          </div>
        </div>
      )}

      <FieldLabel label="答案">
        <input className="input-field" value={d.answer} onChange={(e) => patch({ answer: e.target.value })} placeholder={d.question_type === 'true_false' ? '正确 或 错误' : choice ? (multi ? '如 AB' : '如 B') : '填写参考答案或评分要点'} />
      </FieldLabel>

      <FieldLabel label="解析">
        <textarea className="input-field" rows={2} value={d.explanation} onChange={(e) => patch({ explanation: e.target.value })} />
      </FieldLabel>

      {subjective && (
        <FieldLabel label="评分细则">
          <textarea
            className="input-field"
            rows={2}
            value={d.rubric}
            onChange={(e) => patch({ rubric: e.target.value })}
            placeholder="每行一个评分要点（阅卷细则；AI 提案会带入）"
          />
        </FieldLabel>
      )}

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

/** 可导出的三份卷面 + 答案细则 JSON；前三种同时也是整体预览的页签 */
type ExportKind = PaperExportKind;
type PreviewKind = Exclude<ExportKind, 'json'>;

const PREVIEW_TABS: Array<{ key: PreviewKind; label: string }> = [
  { key: 'student', label: '学生卷' },
  { key: 'card', label: '答题卡' },
  { key: 'answer', label: '答卷（含答案）' },
];

function PaperProfile({
  pv, project, examPointCount, onExport, onPreview, onFinalize, onRevert, onRegenerate, onReview,
}: {
  pv: PaperVersion;
  project?: ExamProject;
  examPointCount: number;
  onExport: (kind: ExportKind) => void;
  onPreview: () => void;
  onFinalize: () => void;
  onRevert: () => void;
  onRegenerate: () => void;
  onReview: () => void;
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
  const missing = questions.filter((q) => !normalizeAnswer(q.answer)).length;
  const psm = PAPER_STATUS_META[pv.status] ?? { label: pv.status, variant: 'default' as const };
  const orderedTypes = [...typeAcc.keys()].sort(
    (a, b) => QUESTION_TYPE_ORDER.indexOf(a) - QUESTION_TYPE_ORDER.indexOf(b),
  );

  return (
    <div className="glass-card" style={{ padding: '24px' }}>
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
                  {qlabel(t)} {formatScore(v.score)}分·{v.count}题
                </span>
              );
            })}
          </div>
          <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
            <span>难度：{['easy', 'medium', 'hard'].map((d) => `${dlabel(d)} ${diffAcc.get(d) ?? 0}`).join(' · ')}</span>
            <span>覆盖 {examPointCount} 个考点</span>
            {overridden > 0 && <span>已修改 {overridden} 题</span>}
            {missing > 0 && <span style={{ color: 'var(--error)', fontWeight: 600 }}>缺答案 {missing} 题</span>}
            {pending > 0 && <span style={{ color: 'var(--warning)', fontWeight: 600 }}>待审核 {pending} 题</span>}
          </div>
          {project && (
            <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              所属项目：{project.name} · {(EXAM_PROJECT_STATUS_META[project.status] ?? { label: project.status }).label}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <Button variant="secondary" size="sm" onClick={onRegenerate} icon={<RefreshCw size={14} />} title="按当前合同重新生成，会创建新版本的试卷">
            重新生成
          </Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('student')} icon={<FileText size={14} />}>学生卷</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('card')} icon={<ClipboardList size={14} />}>答题卡</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('answer')} icon={<KeySquare size={14} />}>答卷</Button>
          <Button variant="secondary" size="sm" onClick={() => onExport('json')} icon={<FileJson size={14} />}>答案细则</Button>
          <Button variant="secondary" size="sm" onClick={onPreview} icon={<Eye size={14} />}>整体预览</Button>
          {/* 只读评审对定稿卷同样可用：不按 readonly/finalized 收起 */}
          <Button variant="secondary" size="sm" onClick={onReview} icon={<FileSearch size={14} />} title="AI 对整份试卷稿出一份只读质量评审报告（不含学生答卷评分）">
            AI 质量评审
          </Button>
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

// ─── 右栏：当前题目详情 / 编辑器 ───

function QuestionDetail({
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
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-tertiary)' }}>{item.item_index}.</span>
        <Badge variant="info">{qlabel(item.question_type)}</Badge>
        <span style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>{formatScore(item.score)} 分</span>
        {item.difficulty && <Badge variant="default">{dlabel(item.difficulty)}</Badge>}
        {item.has_override && <Badge variant="purple">已修改</Badge>}
        {flagged && <Badge variant="warning">需审核</Badge>}
        {!answerText && <Badge variant="error">缺答案</Badge>}
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

          {flagged && item.needs_review_reason && (
            <div style={{
              marginTop: '14px', padding: '10px 14px', borderRadius: 8, fontSize: '0.825rem', lineHeight: 1.65,
              background: 'var(--warning-subtle)', color: 'var(--text-secondary)',
            }}>
              <span style={{ fontWeight: 600, color: 'var(--warning)' }}>待审核原因：</span>{item.needs_review_reason}
            </div>
          )}

          {(examPointName || item.exam_point_id) && (
            <div
              style={{ marginTop: '14px', fontSize: '0.75rem', color: 'var(--text-tertiary)' }}
              title={!examPointName && item.exam_point_id ? item.exam_point_id : undefined}
            >
              考点：{examPointName || friendlyId(item.exam_point_id, '未匹配考点')}
            </div>
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

// ═══════════════════════════════════════════════
//  试卷面板（双栏阅读器）
// ═══════════════════════════════════════════════
export default function PaperPanel({
  pv, project, courseId, onChanged, onRegenerate,
}: {
  pv: PaperVersion;
  project?: ExamProject;
  courseId: string;
  /** 增删改后通知父级刷新试卷与项目摘要 */
  onChanged: () => void;
  /** 请求重新生成：父级切到流水线生成阶段 */
  onRegenerate: () => void;
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
  // 单题 AI 改题面板的展开状态：一次会话属于一道题，切题即收起
  const [aiOpen, setAiOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewKind, setPreviewKind] = useState<PreviewKind>('student');
  // 整体预览的带鉴权 blob object URL（作 iframe src）；加载中/失败时为 null
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  // 右栏编辑器草稿是否有未保存改动：切题/翻题/换序都会让编辑器随 key 重挂载、
  // 草稿蒸发，靠它在这些动作前拦一次确认。
  const [dirty, setDirty] = useState(false);
  const addEditorRef = useRef<QuestionEditorHandle | null>(null);

  // QuestionEditor 未挂载时不主动清脏标记：保存成功后由 handleSave 归零，
  // 否则「刚保存完就切题」仍会被自己拦住。
  const reportDirty = (v: boolean) => setDirty(v);

  /** 放行前确认丢弃未保存草稿；返回 false 表示教师选择留下 */
  const guardDirty = (): boolean => {
    if (!editing || !dirty) return true;
    const ok = window.confirm('当前题目的修改尚未保存，切换将丢弃这些改动。仍要切换吗？');
    if (ok) setDirty(false);
    return ok;
  };

  /** 定稿提示里的题号 chip：关弹窗、退出筛选、落到该题 */
  const jumpTo = (idx: number) => {
    setOnlyNeedsReview(false);
    setEditing(false);
    setDirty(false);
    setSelected(idx);
    setFinalizeOpen(false);
  };

  useEffect(() => {
    void reloadMaps();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  // 试卷刷新后保持选中项；原选中题被删则落到最近的位置——删的是最后一题就
  // 落到新的最后一题（即原前一题），否则一律落到第一题。
  useEffect(() => {
    if (questions.length > 0 && !questions.some((q) => q.item_index === selected)) {
      const last = questions[questions.length - 1].item_index;
      setSelected(selected > last ? last : questions[0].item_index);
      setEditing(false);
      setDirty(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pv.id, questions.length]);

  // 换题即收起 AI 改题面板：提案与 diff 都是单题会话，留在新题上会误导
  useEffect(() => {
    setAiOpen(false);
  }, [selected]);

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
  // 缺答案的题导出答卷时会标「缺答案」：定稿前必须让教师知道，
  // 否则定稿后才发现，只能撤销定稿再补。
  const missingItems = useMemo(
    () => questions.filter((q) => !normalizeAnswer(q.answer)),
    [questions],
  );

  const step = (dir: -1 | 1) => {
    const next = navList[navIdx + dir];
    if (!next || !guardDirty()) return;
    setSelected(next.item_index);
    setEditing(false);
  };

  // 打开「仅看待审核」时，若当前题被滤掉就跳到第一道待审题，避免右栏空着
  const toggleFilter = () => {
    if (!guardDirty()) return;
    const next = !onlyNeedsReview;
    setOnlyNeedsReview(next);
    setEditing(false);
    if (next) {
      const first = questions.find((q) => q.needs_review || q.needs_review_reason);
      if (first) setSelected(first.item_index);
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
  }, [navList, navIdx, editing, dirty, addOpen, finalizeOpen]);

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
      setDirty(false);
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
      setDirty(false);
      onChanged();
    } catch (e) {
      addToast('删除失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleMove = async (pos: number, dir: -1 | 1) => {
    // 换序同样会让编辑器重挂载（item_index 变了、key 变了），先过一遍脏检查
    if (!guardDirty()) return;
    const ordered = questions.map((q) => q.item_index);
    const newPos = pos + dir;
    if (newPos < 0 || newPos >= ordered.length) return;
    const movedIndex = ordered[pos];
    const tmp = ordered[pos];
    ordered[pos] = ordered[newPos];
    ordered[newPos] = tmp;
    setSaving(true);
    try {
      await api.paperVersions.reorderItems(courseId, pv.id, ordered, token ?? undefined);
      // display_order 会被后端重排成 1..N：被移动的题从原题号变成 newPos+1。
      // 不跟着改 selected 的话，右栏会停在同一个题号上、内容却换成了另一道题，
      // 此时若仍处编辑态，保存会把 A 题的草稿写进 B 题的槽位。
      if (movedIndex !== newPos + 1) {
        setSelected(newPos + 1);
        setEditing(false);
        setDirty(false);
      }
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
      // 新题排在末尾，题号 = 原题数 + 1；加完直接跳过去，省一次手动找题。
      // 「仅看待审核」开着时新题不在左栏，顺手关掉，否则跳过去右栏是空的。
      setOnlyNeedsReview(false);
      setSelected(questions.length + 1);
      setEditing(false);
      setDirty(false);
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
    // 待审核与缺答案任一存在都先拦一道：前者后端会 409，后者不会——
    // 不在前端提示就只能等导出答卷时看到「缺答案」标注。
    if (pendingItems.length > 0 || missingItems.length > 0) {
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

  // ── 导出与整体预览 ──
  // 导出端点需要 Authorization 头：iframe / window.open 裸开 URL 都带不了，
  // 也禁止把 token 拼进 query（会进浏览器历史与日志）。统一做法是带鉴权
  // 拉取 Blob → 用 object URL 作 iframe src / 程序化 <a download> 下载。

  // 打开预览或切换页签时拉一次；关闭/切页签的 cleanup 负责释放 object URL。
  useEffect(() => {
    if (!previewOpen) return;
    const pid = project?.id ?? '';
    if (!pid) return;
    let created: string | null = null;
    let cancelled = false;
    setPreviewLoading(true);
    api.paperVersions
      .fetchExport(previewKind, courseId, pid, pv.id, token ?? undefined)
      .then(({ blob }) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setPreviewUrl(created);
        setPreviewLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setPreviewLoading(false);
        addToast('预览加载失败: ' + getErrorMessage(e), 'error');
        setPreviewOpen(false);
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
      setPreviewUrl(null);
    };
  }, [previewOpen, previewKind, project?.id, pv.id, courseId, token, addToast]);

  const handleExport = async (kind: ExportKind) => {
    const pid = project?.id ?? '';
    if (!pid) return;
    try {
      const { blob, filename } = await api.paperVersions.fetchExport(
        kind, courseId, pid, pv.id, token ?? undefined,
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 下载启动后再回收；提前 revoke 会让个别浏览器拿不到文件
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (e) {
      addToast('导出失败: ' + getErrorMessage(e), 'error');
    }
  };

  const previewModal = (
    <Modal
      open={previewOpen}
      onClose={() => setPreviewOpen(false)}
      title="整体预览"
      maxWidth="min(1120px, 96vw)"
      footer={
        <div style={{
          display: 'flex', width: '100%', gap: '12px',
          alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap',
        }}>
          <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
            {PREVIEW_TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setPreviewKind(t.key)}
                style={{
                  padding: '6px 14px', borderRadius: 999, border: 'none', cursor: 'pointer',
                  fontSize: '0.8rem', fontWeight: 600,
                  background: previewKind === t.key ? 'var(--accent)' : 'var(--accent-subtle)',
                  color: previewKind === t.key ? '#fff' : 'var(--accent)',
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={<ExternalLink size={14} />}
            onClick={() => {
              // 同一份已拉取的 Blob 换个标签页看；URL 是 blob: object URL，
              // 不含任何鉴权信息
              if (previewUrl) window.open(previewUrl, '_blank', 'noopener');
            }}
          >
            新标签打开
          </Button>
        </div>
      }
    >
      {previewUrl ? (
        <iframe
          key={previewKind}
          src={previewUrl}
          title="试卷整体预览"
          style={{
            display: 'block', width: '100%', height: '64vh',
            border: '1px solid rgba(0,0,0,0.08)', borderRadius: 8, background: '#fff',
          }}
        />
      ) : (
        <div
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            width: '100%', height: '64vh',
            border: '1px solid rgba(0,0,0,0.08)', borderRadius: 8,
            background: '#fff', color: 'var(--text-tertiary)', fontSize: '0.85rem',
          }}
        >
          {previewLoading ? '预览加载中…' : '预览不可用'}
        </div>
      )}
    </Modal>
  );

  const addModal = (
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
      {/* AI 生成整题：提案 → 填入表单 → 教师微调后走既有「加入试卷」落库 */}
      {!readonly && addOpen && (
        <div style={{ marginBottom: '14px' }}>
          <AiCreatePanel
            courseId={courseId}
            pvId={pv.id}
            onFill={(proposal) => addEditorRef.current?.setDraft(draftFromProposal(proposal))}
          />
        </div>
      )}
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
  );
  if (questions.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <div className="glass-card" style={{ padding: '48px 24px', textAlign: 'center' }}>
          <h3 style={{ fontWeight: 600, fontSize: '1rem', marginBottom: '8px' }}>这份试卷还没有题目</h3>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '18px' }}>
            生成完成后题目会出现在这里；也可以手动新增一道题目。
          </p>
          {/* 文案承诺了「手动新增」就得给出入口，否则空卷无路可走 */}
          {!readonly && (
            <Button onClick={() => setAddOpen(true)} icon={<Plus size={16} />}>新增题目</Button>
          )}
        </div>
        {addModal}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <PaperProfile
        pv={pv}
        project={project}
        examPointCount={new Set(questions.map((q) => q.exam_point_id).filter(Boolean)).size}
        onExport={handleExport}
        onPreview={() => setPreviewOpen(true)}
        onFinalize={handleFinalizeClick}
        onRevert={handleRevert}
        onRegenerate={onRegenerate}
        onReview={() => setReviewOpen((v) => !v)}
      />

      {/* AI 质量评审面板：只读报告，工具栏按钮开关；试卷加载即可用（含 readonly/定稿态） */}
      {reviewOpen && (
        <PaperReviewPanel
          courseId={courseId}
          pvId={pv.id}
          onClose={() => setReviewOpen(false)}
        />
      )}

      {/* 双栏：左题号索引，右当前题目。两卡等高平齐：高度的唯一来源是这一行
          （max-height 从左卡上移到行本身），默认 stretch 让两张卡都由行高撑开，
          两卡底部即行底，天然平齐；左卡超长时仍靠自身 overflowY 内部滚动。 */}
      <div style={{ display: 'flex', gap: '16px', maxHeight: 'calc(100vh - 140px)' }}>
        <div
          className="glass-card"
          style={{
            width: 240, flexShrink: 0, padding: '10px 8px 14px',
            position: 'sticky', top: 16, overflowY: 'auto',
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
            <QuestionIndex
              groups={groups}
              selected={selected}
              onSelect={(idx) => {
                // 同题重复点击不弹确认；换题才过脏检查（换题会让编辑器重挂载）
                if (idx === selected || !guardDirty()) return;
                setSelected(idx);
                setEditing(false);
                setDirty(false);
              }}
            />
          )}
          {!readonly && (
            <div style={{ padding: '10px 6px 0', marginTop: '6px', borderTop: '1px solid rgba(0,0,0,0.06)' }}>
              <Button variant="secondary" size="sm" onClick={() => setAddOpen(true)} icon={<Plus size={14} />} style={{ width: '100%' }}>
                新增题目
              </Button>
            </div>
          )}
        </div>

        {/* 右栏纵向 flex：本身被行 stretch 到与左卡同高，题目卡 flex:1 撑满，
            两卡底部平齐；AI 改题面板是卡片下方的兄弟节点，开启时贴行底对齐。 */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '16px' }}>
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
              onEdit={() => { setDirty(false); setEditing(true); setAiOpen(false); }}
              onAiRevise={() => setAiOpen((v) => !v)}
              onCancelEdit={() => { setDirty(false); setEditing(false); }}
              onSave={(v) => handleSave(current.item_index, v)}
              onDelete={() => handleDelete(current.item_index)}
              onMove={(dir) => handleMove(questions.findIndex((q) => q.item_index === current.item_index), dir)}
              onPrev={() => step(-1)}
              onNext={() => step(1)}
              onDirtyChange={reportDirty}
            />
          ) : (
            <div
              className="glass-card"
              style={{
                flex: 1, padding: '40px 24px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.875rem',
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
              }}
            >
              请在左侧选择题号
            </div>
          )}
          {/* AI 改题面板：提案 → diff 预览 → 确认后走既有 PATCH 落库 */}
          {current && !editing && aiOpen && (
            <AiRevisePanel
              key={current.item_index}
              courseId={courseId}
              pvId={pv.id}
              item={current}
              onApplied={onChanged}
              onClose={() => setAiOpen(false)}
            />
          )}
        </div>
      </div>

      {addModal}
      {previewModal}

      <Modal
        open={finalizeOpen}
        onClose={() => setFinalizeOpen(false)}
        title={pendingItems.length > 0 ? '还有待审核的题目' : '有题目缺少答案'}
        maxWidth="520px"
        footer={
          <>
            <Button variant="secondary" onClick={() => setFinalizeOpen(false)}>返回处理</Button>
            <Button onClick={() => doFinalize(true)}>仍要定稿</Button>
          </>
        }
      >
        {pendingItems.length > 0 && (
          <>
            <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', lineHeight: 1.7 }}>
              以下 {pendingItems.length} 道题被质量检查标记为待审核，建议先逐题处理：
            </p>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '12px' }}>
              {pendingItems.map((q) => (
                <button
                  key={q.item_index}
                  onClick={() => jumpTo(q.item_index)}
                  style={{
                    padding: '3px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                    background: 'var(--warning-subtle)', color: 'var(--warning)', border: 'none', cursor: 'pointer',
                  }}
                >
                  第 {q.item_index} 题
                </button>
              ))}
            </div>
          </>
        )}
        {missingItems.length > 0 && (
          <>
            <p style={{
              fontSize: '0.875rem', color: 'var(--text-secondary)', lineHeight: 1.7,
              marginTop: pendingItems.length > 0 ? '16px' : 0,
            }}>
              以下 {missingItems.length} 道题没有参考答案，导出答卷时会标注「缺答案」：
            </p>
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '12px' }}>
              {missingItems.map((q) => (
                <button
                  key={q.item_index}
                  onClick={() => jumpTo(q.item_index)}
                  style={{
                    padding: '3px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                    background: 'var(--error-subtle)', color: 'var(--error)', border: 'none', cursor: 'pointer',
                  }}
                >
                  第 {q.item_index} 题
                </button>
              ))}
            </div>
          </>
        )}
        <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '14px', lineHeight: 1.6 }}>
          也可以打开「仅看待审核」逐题核对。确已知悉时可选择「仍要定稿」。
        </p>
      </Modal>
    </div>
  );
}
