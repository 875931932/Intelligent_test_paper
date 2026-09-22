import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  ArrowRight, Check, ChevronDown, ChevronUp, FileJson, FileText, KeySquare,
  LayoutGrid, ListOrdered, Pencil, Plus, RotateCcw, Save, Trash2,
} from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage, isApiError } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui';
import { SkeletonCardGrid } from '@/components/ui/Skeleton';
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

// ─── 题目编辑器（原地编辑与「新增题目」弹窗共用） ───

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

// ─── 题目卡片：阅读态 / 编辑态 ───

function QuestionCard({
  item, pos, total, examPointName, editing, readonly, reorderable, submitting,
  onEdit, onCancelEdit, onSave, onDelete, onMove,
}: {
  item: PaperVersionItem;
  pos: number;
  total: number;
  examPointName?: string;
  editing: boolean;
  readonly: boolean;
  reorderable: boolean;
  submitting: boolean;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSave: (v: EditorSubmit) => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const flagged = item.needs_review || !!item.needs_review_reason;
  const keys = answerKeys(item.answer);
  const opts = optionsToEntries(item.options);

  return (
    <div
      className="glass-card"
      style={{
        padding: '18px 22px',
        borderLeft: '3px solid ' + (flagged ? 'var(--warning)' : 'rgba(0,113,227,0.35)'),
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '0.9rem', fontWeight: 700, color: 'var(--text-tertiary)', minWidth: 26 }}>{item.item_index}.</span>
        <Badge variant="info">{qlabel(item.question_type)}</Badge>
        <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{item.score} 分</span>
        {item.difficulty && <Badge variant="default">{dlabel(item.difficulty)}</Badge>}
        {item.has_override && <Badge variant="purple">已修改</Badge>}
        {flagged && <Badge variant="warning">需审核</Badge>}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: '4px' }}>
          {reorderable && (
            <>
              <Button variant="ghost" size="sm" disabled={pos === 0} onClick={() => onMove(-1)} title="与上一题交换" icon={<ChevronUp size={16} />} />
              <Button variant="ghost" size="sm" disabled={pos === total - 1} onClick={() => onMove(1)} title="与下一题交换" icon={<ChevronDown size={16} />} />
            </>
          )}
          {!editing && !readonly && (
            <Button variant="ghost" size="sm" onClick={onEdit} icon={<Pencil size={15} />}>编辑</Button>
          )}
        </div>
      </div>

      {editing ? (
        <div style={{ marginTop: '14px' }}>
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
          <div style={{ marginTop: '10px' }}>
            <Button variant="danger" size="sm" onClick={onDelete} icon={<Trash2 size={14} />}>删除本题</Button>
          </div>
        </div>
      ) : (
        <div style={{ marginTop: '10px' }}>
          {item.stem && (
            <div style={{ fontSize: '0.95rem', lineHeight: 1.75, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{item.stem}</div>
          )}

          {opts.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '10px' }}>
              {opts.map((o) => {
                const isAns = keys.has(o.key.toUpperCase());
                return (
                  <div key={o.key} style={{
                    display: 'flex', gap: '8px', alignItems: 'flex-start', padding: '6px 10px', borderRadius: 8,
                    background: isAns ? 'var(--success-subtle)' : 'transparent',
                    fontSize: '0.9rem', lineHeight: 1.6,
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
              marginTop: '10px', padding: '8px 12px', borderRadius: 8, fontSize: '0.875rem', lineHeight: 1.7,
              background: item.answer ? 'var(--accent-subtle)' : 'var(--warning-subtle)',
              color: item.answer ? 'var(--text)' : 'var(--warning)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>
              <span style={{ fontWeight: 600, fontSize: '0.78rem', display: 'block', marginBottom: 2, opacity: 0.7 }}>答案</span>
              {item.answer || '未填写答案'}
            </div>
          )}

          {item.explanation && (
            <details style={{ marginTop: '10px', fontSize: '0.85rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)', userSelect: 'none' }}>解析</summary>
              <div style={{ marginTop: '6px', color: 'var(--text-secondary)', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{item.explanation}</div>
            </details>
          )}

          {flagged && item.needs_review_reason && (
            <div style={{
              marginTop: '10px', padding: '8px 12px', borderRadius: 8, fontSize: '0.8rem', lineHeight: 1.6,
              background: 'var(--warning-subtle)', color: 'var(--text-secondary)',
            }}>
              <span style={{ fontWeight: 600, color: 'var(--warning)' }}>待审核原因：</span>{item.needs_review_reason}
            </div>
          )}

          {(examPointName || item.exam_point_id) && (
            <div style={{ marginTop: '10px', fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              考点：{examPointName || item.exam_point_id}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── 项目切换条 ───

function ProjectSwitcher({
  projects, selectedId, onSelect,
}: {
  projects: ExamProject[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  if (projects.length === 0) return null;
  return (
    <div style={{ display: 'flex', gap: '10px', overflowX: 'auto', paddingBottom: '2px' }}>
      {projects.map((p) => {
        const active = p.id === selectedId;
        const hasPaper = (p.total_score ?? 0) > 0 || (p.item_count ?? 0) > 0;
        const sm = EXAM_PROJECT_STATUS_META[p.status] ?? { label: p.status, variant: 'default' as const };
        return (
          <button
            key={p.id}
            onClick={() => onSelect(p.id)}
            style={{
              flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 3,
              padding: '8px 14px', borderRadius: 12, cursor: 'pointer', textAlign: 'left',
              border: '1px solid ' + (active ? 'var(--accent)' : 'rgba(0,0,0,0.08)'),
              background: active ? 'var(--accent-subtle)' : 'var(--surface)',
              transition: 'all 150ms ease',
            }}
          >
            <span style={{ fontSize: '0.85rem', fontWeight: active ? 600 : 500, color: active ? 'var(--accent)' : 'var(--text)' }}>{p.name}</span>
            <span style={{ fontSize: '0.72rem', color: 'var(--text-tertiary)' }}>
              {hasPaper ? `${p.total_score ?? 0} 分 · ${p.item_count ?? 0} 题` : '待生成'} · {sm.label}
            </span>
          </button>
        );
      })}
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
    <div className="glass-card" style={{ padding: '20px 24px' }}>
      <div style={{ display: 'flex', gap: '24px', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div style={{ minWidth: 110 }}>
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

// ─── 主页面 ───

export default function PaperCenterPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const { maps, reload: reloadMaps } = useNameMaps(courseId);

  const [projects, setProjects] = useState<ExamProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedProjectId, setSelectedProjectId] = useState(searchParams.get('project') || '');
  const [paperVersion, setPaperVersion] = useState<PaperVersion | null>(null);
  const [pvLoading, setPvLoading] = useState(false);

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [onlyNeedsReview, setOnlyNeedsReview] = useState(false);
  const [orderView, setOrderView] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [finalizeOpen, setFinalizeOpen] = useState(false);
  const addEditorRef = useRef<QuestionEditorHandle | null>(null);

  const selectedProject = projects.find((p) => p.id === selectedProjectId);
  const readonly = paperVersion?.status === 'finalized';

  const loadProjects = async () => {
    if (!courseId) return;
    try {
      const list = await api.examProjects.list(courseId, token ?? undefined);
      setProjects(list);
      setSelectedProjectId((prev) => {
        if (prev && list.some((p) => p.id === prev)) return prev;
        const withPaper = list.find((p) => (p.total_score ?? 0) > 0 || (p.item_count ?? 0) > 0);
        return (withPaper ?? list[0])?.id ?? '';
      });
    } catch {
      addToast('加载试卷项目失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  const loadPaperVersion = async (projectId: string) => {
    if (!courseId || !projectId) return;
    setPvLoading(true);
    try {
      const pv = await api.paperVersions.getCurrent(courseId, projectId, token ?? undefined);
      setPaperVersion(pv);
      setEditingIndex(null);
    } catch (e) {
      if (!isApiError(e) || e.status !== 404) {
        addToast('加载试卷失败', 'error');
      }
      setPaperVersion(null);
    } finally {
      setPvLoading(false);
    }
  };

  useEffect(() => {
    loadProjects();
    void reloadMaps();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  useEffect(() => {
    setPaperVersion(null);
    setEditingIndex(null);
    setOnlyNeedsReview(false);
    if (selectedProjectId) {
      void loadPaperVersion(selectedProjectId);
      // 选中项写回 URL：刷新或分享链接后仍停在同一个项目
      setSearchParams({ project: selectedProjectId }, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  const refresh = async () => {
    await loadProjects();
    if (selectedProjectId) await loadPaperVersion(selectedProjectId);
  };

  // ── 编辑操作 ──

  const handleSave = async (idx: number, v: EditorSubmit) => {
    if (!courseId || !paperVersion) return;
    const { clear_needs_review, ...patch } = v;
    setSaving(true);
    try {
      await api.paperVersions.patchItem(courseId, paperVersion.id, idx, {
        teacher_override_patch: patch,
        clear_needs_review,
      });
      addToast(`第 ${idx} 题已保存`, 'success');
      setEditingIndex(null);
      await loadPaperVersion(selectedProjectId);
    } catch (e) {
      addToast('保存失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (idx: number) => {
    if (!courseId || !paperVersion) return;
    if (!window.confirm(`确认删除第 ${idx} 题？删除后其后的题目题号会前移。`)) return;
    setSaving(true);
    try {
      await api.paperVersions.deleteItem(courseId, paperVersion.id, idx, token ?? undefined);
      addToast('题目已删除', 'success');
      setEditingIndex(null);
      await loadPaperVersion(selectedProjectId);
    } catch (e) {
      addToast('删除失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleMove = async (pos: number, dir: -1 | 1) => {
    if (!courseId || !paperVersion) return;
    const qs = paperVersion.questions;
    const newPos = pos + dir;
    if (newPos < 0 || newPos >= qs.length) return;
    const ordered = qs.map((q) => q.item_index);
    const tmp = ordered[pos];
    ordered[pos] = ordered[newPos];
    ordered[newPos] = tmp;
    setSaving(true);
    try {
      await api.paperVersions.reorderItems(courseId, paperVersion.id, ordered, token ?? undefined);
      await loadPaperVersion(selectedProjectId);
    } catch (e) {
      addToast('调整顺序失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleAdd = async (v: EditorSubmit) => {
    if (!courseId || !paperVersion) return;
    setAdding(true);
    try {
      await api.paperVersions.createItem(courseId, paperVersion.id, { ...v }, token ?? undefined);
      addToast('新题已加入试卷末尾', 'success');
      setAddOpen(false);
      await loadPaperVersion(selectedProjectId);
    } catch (e) {
      addToast('新增失败: ' + getErrorMessage(e), 'error');
    } finally {
      setAdding(false);
    }
  };

  // ── 定稿 ──

  const pendingItems = useMemo(
    () => (paperVersion?.questions ?? []).filter((q) => q.needs_review || q.needs_review_reason),
    [paperVersion],
  );

  const doFinalize = async (force: boolean) => {
    if (!courseId || !paperVersion) return;
    try {
      await api.paperVersions.confirm(courseId, paperVersion.id, force ? { force_ignore_needs_review: true } : {}, token ?? undefined);
      addToast('试卷已定稿', 'success');
      setFinalizeOpen(false);
      await refresh();
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
    if (!courseId || !paperVersion) return;
    if (!window.confirm('撤销定稿并回到待审核状态？')) return;
    try {
      await api.paperVersions.revert(courseId, paperVersion.id, token ?? undefined);
      addToast('已撤销定稿', 'success');
      await refresh();
    } catch (e) {
      addToast('撤销失败: ' + getErrorMessage(e), 'error');
    }
  };

  // ── 导出 ──

  const handleExport = (kind: 'student' | 'answer' | 'json') => {
    if (!courseId || !paperVersion) return;
    const url =
      kind === 'student'
        ? api.paperVersions.exportStudent(courseId, selectedProjectId, paperVersion.id)
        : kind === 'answer'
          ? api.paperVersions.exportAnswerKey(courseId, selectedProjectId, paperVersion.id)
          : api.paperVersions.exportJson(courseId, selectedProjectId, paperVersion.id);
    window.open(url, '_blank', 'noopener');
  };

  const goPipeline = () =>
    navigate(`/courses/${courseId}/exam-projects` + (selectedProjectId ? '?project=' + selectedProjectId : ''));

  // ── 卷面分组 / 过滤 ───

  const groups = useMemo(() => {
    const qs = paperVersion?.questions ?? [];
    const filtered = onlyNeedsReview ? qs.filter((q) => q.needs_review || q.needs_review_reason) : qs;
    if (orderView) {
      return [{ key: '__order__', label: '按试卷顺序', items: filtered, score: filtered.reduce((s, q) => s + (q.score || 0), 0) }];
    }
    const byType = new Map<string, PaperVersionItem[]>();
    filtered.forEach((q) => {
      const arr = byType.get(q.question_type) ?? [];
      arr.push(q);
      byType.set(q.question_type, arr);
    });
    return [...byType.entries()]
      .sort((a, b) => QUESTION_TYPE_ORDER.indexOf(a[0]) - QUESTION_TYPE_ORDER.indexOf(b[0]))
      .map(([t, items]) => ({
        key: t,
        label: sectionLabel(t),
        items,
        score: items.reduce((s, q) => s + (q.score || 0), 0),
      }));
  }, [paperVersion, onlyNeedsReview, orderView]);

  // ── 页面骨架 ──

  if (loading) {
    return (
      <div className="page-enter">
        <SkeletonCardGrid count={3} />
      </div>
    );
  }

  return (
    <div className="page-enter">
      <div style={{ marginBottom: '20px' }}>
        <h1 style={{ fontSize: '1.6rem', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '6px' }}>试卷中心</h1>
        <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>
          查看、审核并导出试卷。出卷流水线（蓝图 → 合同 → 生成）请前往「试卷项目」。
        </p>
      </div>

      <div style={{ maxWidth: 960, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {projects.length === 0 ? (
          <div className="glass-card" style={{ padding: '56px 20px', textAlign: 'center' }}>
            <h3 style={{ fontWeight: 600, fontSize: '1.05rem', marginBottom: '8px' }}>还没有试卷项目</h3>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '18px' }}>
              试卷由「试卷项目」流水线生成，先去创建一个项目吧。
            </p>
            <Button onClick={() => navigate(`/courses/${courseId}/exam-projects`)} icon={<ArrowRight size={16} />}>前往试卷项目</Button>
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <ProjectSwitcher projects={projects} selectedId={selectedProjectId} onSelect={setSelectedProjectId} />
              </div>
              <Button variant="secondary" size="sm" onClick={goPipeline} icon={<ArrowRight size={14} />} title="前往出卷流水线">
                出卷流水线
              </Button>
            </div>

            {pvLoading && !paperVersion ? (
              <div className="glass-card" style={{ padding: '48px 20px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>
                正在加载试卷…
              </div>
            ) : !paperVersion ? (
              <div className="glass-card" style={{ padding: '48px 20px', textAlign: 'center' }}>
                <h3 style={{ fontWeight: 600, fontSize: '1rem', marginBottom: '8px' }}>该项目还没有生成试卷</h3>
                <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '18px' }}>
                  在出卷流水线中完成蓝图、合同并生成后，试卷会出现在这里。
                </p>
                <Button variant="secondary" onClick={goPipeline}>前往出卷流水线</Button>
              </div>
            ) : (
              <>
                <PaperProfile
                  pv={paperVersion}
                  project={selectedProject}
                  examPointCount={new Set(paperVersion.questions.map((q) => q.exam_point_id).filter(Boolean)).size}
                  onExport={handleExport}
                  onFinalize={handleFinalizeClick}
                  onRevert={handleRevert}
                />

                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                  <Button
                    variant={onlyNeedsReview ? 'primary' : 'secondary'} size="sm"
                    onClick={() => setOnlyNeedsReview((v) => !v)}
                  >
                    仅看待审核{`（${pendingItems.length}）`}
                  </Button>
                  <div style={{ display: 'flex', borderRadius: 10, overflow: 'hidden', border: '1px solid rgba(0,0,0,0.1)' }}>
                    {([
                      { key: false, icon: <LayoutGrid size={14} />, label: '按题型' },
                      { key: true, icon: <ListOrdered size={14} />, label: '按顺序' },
                    ] as const).map((opt) => (
                      <button
                        key={String(opt.key)}
                        onClick={() => setOrderView(opt.key)}
                        style={{
                          display: 'flex', alignItems: 'center', gap: 6, padding: '7px 12px', border: 'none', cursor: 'pointer',
                          fontSize: '0.8rem', fontWeight: orderView === opt.key ? 600 : 400,
                          background: orderView === opt.key ? 'var(--accent-subtle)' : 'var(--surface)',
                          color: orderView === opt.key ? 'var(--accent)' : 'var(--text-secondary)',
                        }}
                      >
                        {opt.icon}{opt.label}
                      </button>
                    ))}
                  </div>
                  <div style={{ marginLeft: 'auto' }}>
                    {!readonly && (
                      <Button variant="secondary" size="sm" onClick={() => setAddOpen(true)} icon={<Plus size={14} />}>新增题目</Button>
                    )}
                  </div>
                </div>

                {groups.length === 0 ? (
                  <div className="glass-card" style={{ padding: '40px 20px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>
                    {onlyNeedsReview ? '没有待审核的题目，全部通过。' : '这份试卷还没有题目。'}
                  </div>
                ) : (
                  groups.map((g) => (
                    <div key={g.key} style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                      <div style={{
                        display: 'flex', alignItems: 'baseline', gap: '10px',
                        padding: '0 4px 8px', borderBottom: '1px solid rgba(0,0,0,0.06)',
                      }}>
                        <h2 style={{ fontSize: '1rem', fontWeight: 700, letterSpacing: '-0.01em' }}>{g.label}</h2>
                        <span style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>
                          {g.items.length} 题{g.score ? ` · ${g.score} 分` : ''}
                        </span>
                      </div>
                      {g.items.map((item) => {
                        const pos = paperVersion.questions.findIndex((q) => q.item_index === item.item_index);
                        return (
                          <QuestionCard
                            key={item.item_index}
                            item={item}
                            pos={pos}
                            total={paperVersion.questions.length}
                            examPointName={item.exam_point_id ? maps.examPoints[item.exam_point_id] : undefined}
                            editing={editingIndex === item.item_index}
                            readonly={!!readonly}
                            reorderable={orderView && !readonly}
                            submitting={saving}
                            onEdit={() => setEditingIndex(item.item_index)}
                            onCancelEdit={() => setEditingIndex(null)}
                            onSave={(v) => handleSave(item.item_index, v)}
                            onDelete={() => handleDelete(item.item_index)}
                            onMove={(dir) => handleMove(pos, dir)}
                          />
                        );
                      })}
                    </div>
                  ))
                )}
              </>
            )}
          </>
        )}
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
            <span key={q.item_index} style={{
              padding: '3px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
              background: 'var(--warning-subtle)', color: 'var(--warning)',
            }}>
              第 {q.item_index} 题
            </span>
          ))}
        </div>
        <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', marginTop: '14px', lineHeight: 1.6 }}>
          也可以在工具栏打开「仅看待审核」逐题核对。确已知悉时可选择「仍要定稿」。
        </p>
      </Modal>
    </div>
  );
}
