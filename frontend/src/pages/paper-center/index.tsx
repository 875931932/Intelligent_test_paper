import { useEffect, useState, useMemo } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft, ChevronDown, ChevronUp, Pencil, Trash2, Plus, Check,
  FileJson, FileText, KeySquare, RotateCcw, Save,
} from 'lucide-react';
import { api } from '@/api/client';
import { isApiError } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { SkeletonCardGrid } from '@/components/ui/Skeleton';
import type { ExamProject, PaperVersion } from '@/types/api';

const QUESTION_TYPE_LABELS: Record<string, string> = {
  single_choice: '单选',
  multiple_choice: '多选',
  true_false: '判断',
  fill_blank: '填空',
  short_answer: '简答',
  comprehensive: '综合',
  essay: '论述',
};
const QUESTION_TYPE_OPTIONS = Object.entries(QUESTION_TYPE_LABELS).map(([value, label]) => ({ value, label }));

const DIFFICULTY_LABELS: Record<string, string> = {
  easy: '易', medium: '中', hard: '难',
};
const DIFFICULTY_OPTIONS = Object.entries(DIFFICULTY_LABELS).map(([value, label]) => ({ value, label }));

function qlabel(t: string): string {
  return QUESTION_TYPE_LABELS[t] || t;
}

function isChoiceType(t: string): boolean {
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

interface Draft {
  stem: string;
  question_type: string;
  difficulty: string;
  score: string; // 文本便于编辑
  answer: string;
  explanation: string;
  options: OptEntry[];
}

const emptyDraft = (): Draft => ({
  stem: '',
  question_type: 'single_choice',
  difficulty: 'medium',
  score: '5',
  answer: '',
  explanation: '',
  options: [{ key: 'A', text: '' }, { key: 'B', text: '' }],
});

export default function PaperCenterPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [projects, setProjects] = useState<ExamProject[]>([]);
  const [loading, setLoading] = useState(true);
  // 允许从试卷项目页带 ?project=<id> 直接选中目标项目
  const initialProjectId = searchParams.get('project') || '';
  const [selectedProjectId, setSelectedProjectId] = useState<string>(initialProjectId);
  const [paperVersion, setPaperVersion] = useState<PaperVersion | null>(null);
  const [pvLoading, setPvLoading] = useState(false);
  // 每题编辑草稿（item_index → Draft）；新增题用 -1 占位
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [expanded, setExpanded] = useState<number>(0);
  const [saving, setSaving] = useState(false);
  const [adding, setAdding] = useState(false);

  const loadProjects = async () => {
    if (!courseId) return;
    try {
      const list = await api.examProjects.list(courseId, token ?? undefined);
      setProjects(list);
      // 优先采用查询参数指定的项目；参数无效时回退到列表第一项
      if (!selectedProjectId || !list.some((p) => p.id === selectedProjectId)) {
        setSelectedProjectId(list.length > 0 ? list[0].id : '');
      }
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
      setExpanded(0);
    } catch (e) {
      if (!isApiError(e) || e.status !== 404) {
        addToast('加载试卷失败', 'error');
      }
      setPaperVersion(null);
    } finally {
      setPvLoading(false);
    }
  };

  // 首次加载项目列表
  useEffect(() => {
    loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  // 切换项目时载入对应试卷
  useEffect(() => {
    setPaperVersion(null);
    setDrafts({});
    if (selectedProjectId) {
      void loadPaperVersion(selectedProjectId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  const withProjects = useMemo(() => projects.filter((p) => (p.total_score ?? 0) > 0 || (p.item_count ?? 0) > 0), [projects]);

  // 有新试卷时自动选中第一个有试卷的项目
  useEffect(() => {
    if (!selectedProjectId && withProjects.length > 0) {
      setSelectedProjectId(withProjects[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [withProjects.length]);

  const statusMeta: Record<string, { label: string; variant: any }> = {
    draft: { label: '草稿', variant: 'info' },
    candidate: { label: '待审核', variant: 'warning' },
    finalized: { label: '已定稿', variant: 'success' },
  };
  const pvStatus = paperVersion ? (statusMeta[paperVersion.status] ?? { label: paperVersion.status, variant: 'default' }) : null;

  const refreshAll = async () => {
    await loadProjects();
    if (selectedProjectId) await loadPaperVersion(selectedProjectId);
  };

  const getDraft = (idx: number): Draft => {
    if (drafts[idx]) return drafts[idx];
    const item = paperVersion?.questions.find((q) => q.item_index === idx);
    if (!item) return drafts[-1] ?? emptyDraft();
    return {
      stem: item.stem ?? '',
      question_type: item.question_type || 'short_answer',
      difficulty: item.difficulty || 'medium',
      score: String(item.score ?? 0),
      answer: item.answer ?? '',
      explanation: item.explanation ?? '',
      options: optionsToEntries(item.options),
    };
  };

  const updateDraft = (idx: number, patch: Partial<Draft>) => {
    setDrafts((prev) => ({ ...prev, [idx]: { ...getDraft(idx), ...patch } }));
  };

  const updateOption = (idx: number, optIdx: number, text: string) => {
    const d = getDraft(idx);
    const options = d.options.map((o, i) => (i === optIdx ? { ...o, text } : o));
    updateDraft(idx, { options });
  };

  const addOption = (idx: number) => {
    const d = getDraft(idx);
    const nextKey = d.options.length > 0
      ? String.fromCharCode(65 + d.options.length)
      : 'A';
    updateDraft(idx, { options: [...d.options, { key: nextKey, text: '' }] });
  };

  const removeOption = (idx: number, optIdx: number) => {
    const d = getDraft(idx);
    updateDraft(idx, { options: d.options.filter((_, i) => i !== optIdx) });
  };

  // 保存单题修改：把草稿合并进 teacher_override_patch 全量覆盖
  const handleSave = async (idx: number) => {
    const pvId = paperVersion?.id;
    if (!pvId) return;
    const d = getDraft(idx);
    setSaving(true);
    try {
      const patch: Record<string, unknown> = {
        stem: d.stem,
        question_type: d.question_type,
        difficulty: d.difficulty,
        answer: d.answer,
        explanation: d.explanation,
        score: Number(d.score) || 0,
      };
      if (isChoiceType(d.question_type)) {
        patch.options = entriesToOptions(d.options);
      } else {
        patch.options = [];
      }
      await api.paperVersions.patchItem(courseId!, pvId, idx, { teacher_override_patch: patch });
      addToast(`第 ${idx} 题已保存`, 'success');
      await loadPaperVersion(selectedProjectId);
    } catch {
      addToast('保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (idx: number) => {
    const pvId = paperVersion?.id;
    if (!pvId) return;
    if (!window.confirm('确认删除第 ' + idx + ' 题？此操作不可撤销。')) return;
    setSaving(true);
    try {
      await api.paperVersions.deleteItem(courseId!, pvId, idx, token ?? undefined);
      addToast('题目已删除', 'success');
      await loadPaperVersion(selectedProjectId);
    } catch {
      addToast('删除失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleMove = async (pos: number, dir: -1 | 1) => {
    const pvId = paperVersion?.id;
    const qs = paperVersion?.questions;
    if (!pvId || !qs) return;
    const newPos = pos + dir;
    if (newPos < 0 || newPos >= qs.length) return;
    const ordered = qs.map((q) => q.item_index);
    const tmp = ordered[pos];
    ordered[pos] = ordered[newPos];
    ordered[newPos] = tmp;
    setSaving(true);
    try {
      await api.paperVersions.reorderItems(courseId!, pvId, ordered, token ?? undefined);
      addToast('顺序已调整', 'success');
      await loadPaperVersion(selectedProjectId);
    } catch {
      addToast('调整顺序失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleAdd = async () => {
    const pvId = paperVersion?.id;
    if (!pvId) return;
    const d = getDraft(-1);
    setAdding(true);
    try {
      const body: Record<string, unknown> = {
        stem: d.stem,
        question_type: d.question_type,
        difficulty: d.difficulty,
        answer: d.answer,
        explanation: d.explanation,
        score: Number(d.score) || 0,
      };
      if (isChoiceType(d.question_type)) {
        body.options = entriesToOptions(d.options);
      } else {
        body.options = [];
      }
      await api.paperVersions.createItem(courseId!, pvId, body, token ?? undefined);
      addToast('新题已加入', 'success');
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[-1];
        return next;
      });
      await loadPaperVersion(selectedProjectId);
    } catch {
      addToast('新增失败', 'error');
    } finally {
      setAdding(false);
    }
  };

  const handleConfirm = async () => {
    const pvId = paperVersion?.id;
    if (!pvId) return;
    try {
      await api.paperVersions.confirm(courseId!, pvId, {});
      addToast('试卷已定稿', 'success');
      await refreshAll();
    } catch (e) {
      addToast('定稿失败: ' + ((e as Error).message || '请先处理待审核题'), 'error');
    }
  };

  const handleRevert = async () => {
    const pvId = paperVersion?.id;
    if (!pvId) return;
    if (!window.confirm('撤销定稿并回到待审核状态？')) return;
    try {
      await api.paperVersions.revert(courseId!, pvId, token ?? undefined);
      addToast('已撤销定稿', 'success');
      await refreshAll();
    } catch {
      addToast('撤销失败', 'error');
    }
  };

  // ── 页面骨架 ──
  if (loading) {
    return (
      <div className="page-enter">
        <div style={{ display: 'flex', gap: '12px', alignItems: 'center', marginBottom: '20px' }}>
          <button onClick={() => navigate(-1)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '0.8125rem', display: 'flex', alignItems: 'center', gap: '4px' }}>
            <ArrowLeft size={16} /> 返回
          </button>
        </div>
        <SkeletonCardGrid count={3} />
      </div>
    );
  }

  return (
    <div className="page-enter">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
        <div>
          <h1 style={{ fontSize: '1.6rem', fontWeight: 700, letterSpacing: '-0.03em', marginBottom: '6px' }}>试卷中心</h1>
          <p style={{ fontSize: '0.9375rem', color: 'var(--text-secondary)' }}>
            独立编辑试卷：修改题目、调顺序、改分值、增删题目，定稿后可导出
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => navigate(-1)} icon={<ArrowLeft size={14} />}>返回</Button>
      </div>

      {/* 项目选择 */}
      <div className="glass-card" style={{ padding: '16px 20px', marginBottom: '16px', display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
        <label style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)' }}>选择项目</label>
        <select
          value={selectedProjectId}
          onChange={(e) => setSelectedProjectId(e.target.value)}
          className="input-field"
          style={{ width: '260px' }}
        >
          {projects.length === 0 && <option value="">暂无项目</option>}
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name}（{p.total_score ? p.total_score + '分·' + (p.item_count || 0) + '题' : '待生成'}）</option>
          ))}
        </select>
        {pvStatus && (
          <Badge variant={pvStatus.variant}>{pvStatus.label}</Badge>
        )}
      </div>

      {pvLoading && !paperVersion ? (
        <div className="glass-card" style={{ padding: '48px 20px', textAlign: 'center', color: 'var(--text-tertiary)', fontSize: '0.875rem' }}>
          正在加载试卷…
        </div>
      ) : !paperVersion ? (
        <div className="glass-card" style={{ padding: '48px 20px', textAlign: 'center', color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
          该项目还没有生成试卷。请先进入「试卷项目 → 生成」生成后再来编辑。
          <div style={{ marginTop: '16px' }}>
            <Button variant="secondary" size="sm" onClick={() => navigate(`/courses/${courseId}/exam-projects`)}>前往生成</Button>
          </div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {/* 试卷汇总 + 操作 */}
          <div className="glass-card" style={{ padding: '18px 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px' }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>{paperVersion.total_score}</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)' }}>总分 · {paperVersion.questions.length} 题 · v{paperVersion.version_no}</div>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
              <a
                href={api.paperVersions.exportJson(courseId!, selectedProjectId, paperVersion.id)}
                target="_blank" rel="noreferrer"
              >
                <Button variant="secondary" size="sm" icon={<FileJson size={14} />}>答案细则</Button>
              </a>
              <a
                href={api.paperVersions.exportStudent(courseId!, selectedProjectId, paperVersion.id)}
                target="_blank" rel="noreferrer"
              >
                <Button variant="secondary" size="sm" icon={<FileText size={14} />}>学生卷</Button>
              </a>
              <a
                href={api.paperVersions.exportAnswerKey(courseId!, selectedProjectId, paperVersion.id)}
                target="_blank" rel="noreferrer"
              >
                <Button variant="secondary" size="sm" icon={<KeySquare size={14} />}>答卷</Button>
              </a>
              {paperVersion.status === 'finalized' ? (
                <Button variant="secondary" size="sm" onClick={handleRevert} icon={<RotateCcw size={14} />}>撤销定稿</Button>
              ) : (
                <Button size="sm" onClick={handleConfirm} icon={<Check size={14} />}>确认定稿</Button>
              )}
            </div>
          </div>

          {/* 题目编辑列表 */}
          {paperVersion.questions.map((item, pos) => {
            const d = getDraft(item.item_index);
            const open = expanded === item.item_index;
            const flagged = item.needs_review || !!item.needs_review_reason;
            return (
              <div key={item.item_index} className="glass-card" style={{ padding: '16px 20px', borderLeft: '3px solid ' + (flagged ? '#ff9500' : 'rgba(0,113,227,0.35)') }}>
                {/* 头部：题号 + 大概信息 + 排序/展开 */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-tertiary)' }}>#{item.item_index}</span>
                  <Badge variant="info">{qlabel(d.question_type)}</Badge>
                  <strong style={{ fontSize: '0.85rem' }}>{d.score} 分</strong>
                  {flagged && <Badge variant="warning">需审核</Badge>}
                  <div style={{ marginLeft: 'auto', display: 'flex', gap: '4px' }}>
                    <Button variant="ghost" size="sm" disabled={pos === 0} onClick={() => handleMove(pos, -1)} icon={<ChevronUp size={16} />} />
                    <Button variant="ghost" size="sm" disabled={pos === paperVersion.questions.length - 1} onClick={() => handleMove(pos, 1)} icon={<ChevronDown size={16} />} />
                    <Button variant="ghost" size="sm" onClick={() => setExpanded(open ? -1 : item.item_index)} icon={open ? undefined : <Pencil size={15} />}>
                      {open ? '收起' : d.stem ? truncate(d.stem, 60) : '编辑'}
                    </Button>
                  </div>
                </div>

                {open && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '14px' }}>
                    <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                      <Selectln
                        label="题型"
                        value={d.question_type}
                        options={QUESTION_TYPE_OPTIONS}
                        onChange={(e) => updateDraft(item.item_index, { question_type: e.target.value })}
                      />
                      <Selectln
                        label="难度"
                        value={d.difficulty}
                        options={DIFFICULTY_OPTIONS}
                        onChange={(e) => updateDraft(item.item_index, { difficulty: e.target.value })}
                      />
                      <Inputln label="分值" type="number" min={0} step="0.5" value={d.score} onChange={(e) => updateDraft(item.item_index, { score: e.target.value })} />
                    </div>

                    <Field label="题干">
                      <textarea
                        className="input-field"
                        rows={2}
                        value={d.stem}
                        onChange={(e) => updateDraft(item.item_index, { stem: e.target.value })}
                      />
                    </Field>

                    {isChoiceType(d.question_type) ? (
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                          <span style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>选项</span>
                          <Button variant="ghost" size="sm" onClick={() => addOption(item.item_index)} icon={<Plus size={14} />}>加选项</Button>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                          {d.options.map((o, oi) => (
                            <div key={oi} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                              <span style={{ width: '18px', fontWeight: 600 }}>{o.key}.</span>
                              <input
                                className="input-field"
                                style={{ flex: 1 }}
                                value={o.text}
                                onChange={(e) => updateOption(item.item_index, oi, e.target.value)}
                              />
                              <Button
                                variant="ghost" size="sm"
                                onClick={() => updateDraft(item.item_index, { answer: o.key })}
                                style={d.answer === o.key ? { color: 'var(--success)' } : undefined}
                              >
                                <Check size={14} /> {d.answer === o.key ? '是答案' : '设为答案'}
                              </Button>
                              <Button variant="ghost" size="sm" onClick={() => removeOption(item.item_index, oi)} icon={<Trash2 size={14} />} />
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : null}

                    <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                      <div style={{ flex: 1, minWidth: '220px' }}>
                        <Field label="答案">
                          <input className="input-field" value={d.answer} onChange={(e) => updateDraft(item.item_index, { answer: e.target.value })} />
                        </Field>
                      </div>
                    </div>

                    <Field label="解析">
                      <textarea
                        className="input-field"
                        rows={2}
                        value={d.explanation}
                        onChange={(e) => updateDraft(item.item_index, { explanation: e.target.value })}
                      />
                    </Field>

                    <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                      <Button variant="danger" size="sm" onClick={() => handleDelete(item.item_index)} icon={<Trash2 size={14} />}>删除</Button>
                      <Button size="sm" loading={saving} onClick={() => handleSave(item.item_index)} icon={<Save size={14} />}>保存</Button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {/* 新增题目 */}
          <div className="glass-card" style={{ padding: '16px 20px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
              <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>新增题目</span>
              <Button variant="secondary" size="sm" onClick={() => setExpanded(-1)} icon={<Plus size={14} />}>展开编辑</Button>
            </div>
            {(expanded === -1) && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                  <Selectln label="题型" value={getDraft(-1).question_type} options={QUESTION_TYPE_OPTIONS} onChange={(e) => updateDraft(-1, { question_type: e.target.value })} />
                  <Selectln label="难度" value={getDraft(-1).difficulty} options={DIFFICULTY_OPTIONS} onChange={(e) => updateDraft(-1, { difficulty: e.target.value })} />
                  <Inputln label="分值" type="number" min={0} step="0.5" value={getDraft(-1).score} onChange={(e) => updateDraft(-1, { score: e.target.value })} />
                </div>
                <Field label="题干">
                  <textarea className="input-field" rows={2} value={getDraft(-1).stem} onChange={(e) => updateDraft(-1, { stem: e.target.value })} />
                </Field>
                {isChoiceType(getDraft(-1).question_type) ? (
                  <div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                      <span style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>选项</span>
                      <Button variant="ghost" size="sm" onClick={() => addOption(-1)} icon={<Plus size={14} />}>加选项</Button>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                      {getDraft(-1).options.map((o, oi) => (
                        <div key={oi} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <span style={{ width: '18px', fontWeight: 600 }}>{o.key}.</span>
                          <input className="input-field" style={{ flex: 1 }} value={o.text} onChange={(e) => updateOption(-1, oi, e.target.value)} />
                          <Button variant="ghost" size="sm" onClick={() => updateDraft(-1, { answer: o.key })}>
                            <Check size={14} /> {getDraft(-1).answer === o.key ? '是答案' : '设为答案'}
                          </Button>
                          <Button variant="ghost" size="sm" onClick={() => removeOption(-1, oi)} icon={<Trash2 size={14} />} />
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
                <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
                  <div style={{ flex: 1, minWidth: '220px' }}>
                    <Field label="答案">
                      <input className="input-field" value={getDraft(-1).answer} onChange={(e) => updateDraft(-1, { answer: e.target.value })} />
                    </Field>
                  </div>
                </div>
                <Field label="解析">
                  <textarea className="input-field" rows={2} value={getDraft(-1).explanation} onChange={(e) => updateDraft(-1, { explanation: e.target.value })} />
                </Field>
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <Button loading={adding} onClick={handleAdd} icon={<Plus size={14} />}>加入试卷</Button>
                </div>
              </div>
            )}
            {expanded !== -1 && (
              <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>在试卷末尾新增一道自拟题目（题干/答案由您填写）。</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s && s.length > n ? s.slice(0, n) + '…' : s || '';
}

// 轻量包装，复用现有样式类
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>
      {children}
    </div>
  );
}

function Selectln(props: React.SelectHTMLAttributes<HTMLSelectElement> & { label: string; options: { value: string; label: string }[] }) {
  const { label, options, ...rest } = props;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '120px' }}>
      <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>
      <select className="input-field" {...rest}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  );
}

function Inputln(props: React.InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  const { label, ...rest } = props;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '90px' }}>
      <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>
      <input className="input-field" {...rest} />
    </div>
  );
}