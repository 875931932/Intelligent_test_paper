import { useState } from 'react';
import { Check, Pencil, Plus, Trash2, X } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { QUESTION_TYPE_OPTIONS, qlabel, mlabel } from '@/lib/examDisplay';
import { formatPercent, friendlyId } from '@/lib/format';
import type { ExamRuleFocus, ExamRules } from '@/types/api';

const EMPTY_RULES: ExamRules = {
  exam_form: '',
  duration_minutes: null,
  total_score: null,
  question_type_ratios: [],
  chapter_weights: [],
  assessment_focus: [],
};

/** 五项考查方式（侧重点权重编辑行的固定顺序） */
const FOCUS_MODES = [
  'theory_recall',
  'conceptual',
  'application',
  'problem_solving',
  'practical_operation',
] as const;

/**
 * 考试侧重点预设：均衡 = 不声明（蓝图按题型默认分布），其余是权重组合。
 * 预设只是快捷入口——教师仍可逐项微调，全部归零即回到均衡。
 */
const FOCUS_PRESETS: Array<{ key: string; label: string; weights: Record<string, number> }> = [
  { key: 'balanced', label: '均衡', weights: {} },
  { key: 'theory', label: '偏理论', weights: { theory_recall: 45, conceptual: 25, application: 20, problem_solving: 10 } },
  { key: 'understand', label: '偏理解', weights: { conceptual: 45, application: 25, problem_solving: 15, theory_recall: 15 } },
  { key: 'practice', label: '偏实操', weights: { practical_operation: 40, application: 25, problem_solving: 20, conceptual: 15 } },
];

function focusEntries(weights: Record<string, number>): ExamRuleFocus[] {
  return Object.entries(weights).map(([assessment_mode, weight]) => ({
    assessment_mode: assessment_mode as ExamRuleFocus['assessment_mode'],
    weight,
  }));
}

/** 归一后的权重签名（去零、取整、排序），用于判断当前侧重点命中了哪个预设 */
function focusSignature(entries: ExamRuleFocus[] | undefined): string {
  return (entries ?? [])
    .filter((e) => Number(e.weight) > 0)
    .map((e) => `${e.assessment_mode}:${Math.round(Number(e.weight))}`)
    .sort()
    .join(',');
}

/**
 * 考核规则卡：考核大纲声明的题型比例与章节命题权重。
 * 这是出卷的硬约束——蓝图按这里的比例推导题型分布与章节分布，
 * 因此必须让教师看得见、改得动。
 */
export function ExamRulesCard({
  courseId,
  rules,
  anchors,
  onSaved,
}: {
  courseId: string;
  rules?: ExamRules;
  anchors: Array<{ key: string; title: string }>;
  onSaved: () => void;
}) {
  const addToast = useToastStore((s) => s.addToast);
  const [editing, setEditing] = useState(false);
  // 初始态也补齐数组：不能假设接口一定返回完整形态（旧框架的规则是空 dict）
  const [draft, setDraft] = useState<ExamRules>(() => ({
    ...EMPTY_RULES,
    ...(rules ?? {}),
    question_type_ratios: [...(rules?.question_type_ratios ?? [])],
    chapter_weights: [...(rules?.chapter_weights ?? [])],
    assessment_focus: [...(rules?.assessment_focus ?? [])],
  }));
  const [saving, setSaving] = useState(false);

  const startEdit = () => {
    setDraft({
      ...EMPTY_RULES,
      ...(rules ?? EMPTY_RULES),
      // 后端应对旧框架补齐字段，这里再兜一层：不假设 API 一定返回完整数组
      question_type_ratios: [...(rules?.question_type_ratios ?? [])],
      chapter_weights: [...(rules?.chapter_weights ?? [])],
      assessment_focus: [...(rules?.assessment_focus ?? [])],
    });
    setEditing(true);
  };

  const ratioSum = (draft.question_type_ratios ?? []).reduce((s, r) => s + (Number(r.ratio) || 0), 0);
  const chapterSum = (draft.chapter_weights ?? []).reduce((s, c) => s + (Number(c.weight) || 0), 0);
  const focusSum = (draft.assessment_focus ?? []).reduce((s, e) => s + (Number(e.weight) || 0), 0);
  const hasRules = (rules?.question_type_ratios?.length ?? 0) > 0;
  // 当前权重命中了哪个预设（全零/空 = 均衡；其余不匹配则视为自定义微调）
  const activePreset = FOCUS_PRESETS.find(
    (p) => focusSignature(draft.assessment_focus) === focusSignature(focusEntries(p.weights)),
  )?.key;

  const setFocusWeight = (mode: string, weight: number) => {
    const current = draft.assessment_focus ?? [];
    const idx = current.findIndex((e) => e.assessment_mode === mode);
    const next = [...current];
    if (idx >= 0) next[idx] = { ...next[idx], weight };
    else next.push({ assessment_mode: mode as ExamRuleFocus['assessment_mode'], weight });
    // 全部归零 = 均衡：清空声明，蓝图回退题型默认分布
    setDraft({ ...draft, assessment_focus: next.filter((e) => Number(e.weight) > 0) });
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.framework.updateExamRules(courseId, draft);
      addToast('考核规则已保存，下次出卷按新比例与侧重点分配', 'success');
      setEditing(false);
      onSaved();
    } catch (e) {
      addToast('保存失败: ' + getErrorMessage(e), 'error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="glass-card" style={{ padding: '18px 22px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '8px' }}>
        <div>
          <h3 style={{ fontSize: '1rem', fontWeight: 700, letterSpacing: '-0.01em' }}>考核规则</h3>
          <p style={{ fontSize: '0.78rem', color: 'var(--text-tertiary)', marginTop: '3px' }}>
            考纲声明的考试形式、题型比例与章节命题权重，可另设考试侧重点；蓝图按此推导试卷结构
          </p>
        </div>
        {editing ? (
          <div style={{ display: 'flex', gap: '8px' }}>
            <Button variant="secondary" size="sm" onClick={() => setEditing(false)} icon={<X size={14} />}>取消</Button>
            <Button size="sm" onClick={handleSave} loading={saving} icon={<Check size={14} />}>保存</Button>
          </div>
        ) : (
          <Button variant="secondary" size="sm" onClick={startEdit} icon={<Pencil size={14} />}>
            {hasRules ? '修改规则' : '补充规则'}
          </Button>
        )}
      </div>

      {!editing ? (
        hasRules || rules?.exam_form || rules?.total_score ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ display: 'flex', gap: '18px', flexWrap: 'wrap', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              <span>考试形式：<strong style={{ color: 'var(--text)' }}>{rules?.exam_form || '—'}</strong></span>
              <span>考试时长：<strong style={{ color: 'var(--text)' }}>{rules?.duration_minutes ? `${rules.duration_minutes} 分钟` : '—'}</strong></span>
              <span>试卷满分：<strong style={{ color: 'var(--text)' }}>{rules?.total_score ?? '—'}</strong></span>
            </div>

            {(rules?.question_type_ratios?.length ?? 0) > 0 && (
              <div>
                <p style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '6px' }}>题型比例</p>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  {(rules?.question_type_ratios ?? []).map((r) => (
                    <span key={r.question_type} style={{
                      padding: '4px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                      background: 'var(--accent-subtle)', color: 'var(--accent)',
                    }}>
                      {qlabel(r.question_type)} {formatPercent(r.ratio)}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {(rules?.assessment_focus?.length ?? 0) > 0 && (
              <div>
                <p style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '6px' }}>考试侧重点</p>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                  {(rules?.assessment_focus ?? []).map((e) => (
                    <span key={e.assessment_mode} style={{
                      padding: '4px 10px', borderRadius: 999, fontSize: '0.78rem', fontWeight: 600,
                      background: 'var(--accent-subtle)', color: 'var(--accent)',
                    }}>
                      {mlabel(e.assessment_mode)} {formatPercent(e.weight)}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {(rules?.chapter_weights?.length ?? 0) > 0 && (
              <div>
                <p style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '6px' }}>章节命题权重</p>
                <div style={{ display: 'flex', gap: '6px 16px', flexWrap: 'wrap', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
                  {(rules?.chapter_weights ?? []).map((c) => (
                    <span key={c.anchor_key}>
                      {anchors.find((a) => a.key === c.anchor_key)?.title || friendlyId(c.anchor_key, '未匹配范围')}：<strong style={{ color: 'var(--text)' }}>{formatPercent(c.weight)}</strong>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            考核大纲里没有解析出考试规则。可点击「补充规则」手动填写题型比例与章节权重，
            否则将按系统默认题型分布出卷。
          </p>
        )
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
            <FieldLabel label="考试形式">
              <input className="input-field" style={{ width: 140 }} value={draft.exam_form ?? ''} onChange={(e) => setDraft({ ...draft, exam_form: e.target.value })} placeholder="如 闭卷笔试" />
            </FieldLabel>
            <FieldLabel label="考试时长（分钟）">
              <input className="input-field" type="number" min={0} style={{ width: 120 }} value={draft.duration_minutes ?? ''} onChange={(e) => setDraft({ ...draft, duration_minutes: e.target.value === '' ? null : Number(e.target.value) })} />
            </FieldLabel>
            <FieldLabel label="试卷满分">
              <input className="input-field" type="number" min={0} step="0.5" style={{ width: 120 }} value={draft.total_score ?? ''} onChange={(e) => setDraft({ ...draft, total_score: e.target.value === '' ? null : Number(e.target.value) })} />
            </FieldLabel>
          </div>

          <div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
              <p style={{ fontSize: '0.82rem', fontWeight: 600 }}>题型比例</p>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <span style={{ fontSize: '0.75rem', color: Math.abs(ratioSum - 100) < 0.5 ? 'var(--success)' : 'var(--warning)' }}>
                  合计 {formatPercent(ratioSum)}
                </span>
                <Button variant="ghost" size="sm" onClick={() => setDraft({ ...draft, question_type_ratios: [...draft.question_type_ratios, { question_type: 'single_choice', ratio: 0 }] })} icon={<Plus size={14} />}>加题型</Button>
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {draft.question_type_ratios.map((r, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <select
                    className="input-field" style={{ width: 130 }} value={r.question_type}
                    onChange={(e) => {
                      const next = [...draft.question_type_ratios];
                      next[i] = { ...r, question_type: e.target.value };
                      setDraft({ ...draft, question_type_ratios: next });
                    }}
                  >
                    {QUESTION_TYPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                  <input
                    className="input-field" type="number" min={0} max={100} step="0.5" style={{ width: 100 }}
                    value={r.ratio}
                    onChange={(e) => {
                      const next = [...draft.question_type_ratios];
                      next[i] = { ...r, ratio: Number(e.target.value) || 0 };
                      setDraft({ ...draft, question_type_ratios: next });
                    }}
                  />
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>%</span>
                  <Button variant="ghost" size="sm" onClick={() => setDraft({ ...draft, question_type_ratios: draft.question_type_ratios.filter((_, j) => j !== i) })} icon={<Trash2 size={14} />} />
                </div>
              ))}
              {draft.question_type_ratios.length === 0 && (
                <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>暂无题型比例，保存后将按系统默认分布出卷。</p>
              )}
            </div>
          </div>

          <div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
              <p style={{ fontSize: '0.82rem', fontWeight: 600 }}>章节命题权重</p>
              <span style={{ fontSize: '0.75rem', color: Math.abs(chapterSum - 100) < 0.5 ? 'var(--success)' : 'var(--warning)' }}>
                合计 {formatPercent(chapterSum)}
              </span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {anchors.map((a) => {
                const idx = draft.chapter_weights.findIndex((c) => c.anchor_key === a.key);
                const value = idx >= 0 ? draft.chapter_weights[idx].weight : 0;
                const setValue = (w: number) => {
                  const next = [...draft.chapter_weights];
                  if (idx >= 0) next[idx] = { ...next[idx], weight: w };
                  else next.push({ anchor_key: a.key, weight: w });
                  setDraft({ ...draft, chapter_weights: next });
                };
                return (
                  <div key={a.key} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ flex: 1, fontSize: '0.85rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={a.title}>{a.title}</span>
                    <input className="input-field" type="number" min={0} max={100} step="0.5" style={{ width: 100 }} value={value} onChange={(e) => setValue(Number(e.target.value) || 0)} />
                    <span style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>%</span>
                  </div>
                );
              })}
              {anchors.length === 0 && (
                <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>框架还没有考核范围锚点。</p>
              )}
            </div>
          </div>

          <div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px', flexWrap: 'wrap', gap: '8px' }}>
              <p style={{ fontSize: '0.82rem', fontWeight: 600 }}>考试侧重点</p>
              <div style={{ display: 'flex', gap: '8px' }}>
                {FOCUS_PRESETS.map((p) => (
                  <Button
                    key={p.key}
                    variant={activePreset === p.key ? 'primary' : 'secondary'}
                    size="sm"
                    onClick={() => setDraft({ ...draft, assessment_focus: focusEntries(p.weights) })}
                  >
                    {p.label}
                  </Button>
                ))}
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))', gap: '8px 16px' }}>
              {FOCUS_MODES.map((mode) => (
                <div key={mode} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span style={{ flex: 1, fontSize: '0.85rem' }}>{mlabel(mode)}</span>
                  <input
                    className="input-field" type="number" min={0} max={100} step="5" style={{ width: 100 }}
                    aria-label={`${mlabel(mode)}占比`}
                    value={(draft.assessment_focus ?? []).find((e) => e.assessment_mode === mode)?.weight ?? 0}
                    onChange={(e) => setFocusWeight(mode, Number(e.target.value) || 0)}
                  />
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)' }}>%</span>
                </div>
              ))}
            </div>
            <p style={{ fontSize: '0.75rem', color: focusSum > 0 && Math.abs(focusSum - 100) > 0.5 ? 'var(--warning)' : 'var(--text-tertiary)', marginTop: '8px', lineHeight: 1.7 }}>
              {focusSum > 0
                ? `合计 ${formatPercent(focusSum)}（保存时自动归一到 100）。侧重点决定蓝图各题型的考查方式分布；无可直考实操单元的课程，实操占比会自动收敛为 0，出卷不受影响，题位表可逐题查看考查方式。`
                : '均衡：不声明侧重点，蓝图按题型默认分布分配考查方式。'}
            </p>
          </div>

          <div style={{ padding: '10px 14px', borderRadius: 8, background: 'var(--info-subtle)', fontSize: '0.78rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            比例不需要手工凑满 100：保存时会自动归一到 100。题型比例决定试卷的题型分布与分值，
            章节权重决定各章出题占比，考试侧重点决定各题型的考查方式；考纲未声明的章节按 0 处理。
          </div>
        </div>
      )}
    </div>
  );
}

function FieldLabel({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      <label style={{ fontSize: '0.78rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>
      {children}
    </div>
  );
}
