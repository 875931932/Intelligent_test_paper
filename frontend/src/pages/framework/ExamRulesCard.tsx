import { useState } from 'react';
import { Check, Pencil, Plus, Trash2, X } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import { QUESTION_TYPE_OPTIONS, qlabel } from '@/lib/examDisplay';
import type { ExamRules } from '@/types/api';

const EMPTY_RULES: ExamRules = {
  exam_form: '',
  duration_minutes: null,
  total_score: null,
  question_type_ratios: [],
  chapter_weights: [],
};

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
  }));
  const [saving, setSaving] = useState(false);

  const startEdit = () => {
    setDraft({
      ...EMPTY_RULES,
      ...(rules ?? EMPTY_RULES),
      // 后端应对旧框架补齐字段，这里再兜一层：不假设 API 一定返回完整数组
      question_type_ratios: [...(rules?.question_type_ratios ?? [])],
      chapter_weights: [...(rules?.chapter_weights ?? [])],
    });
    setEditing(true);
  };

  const ratioSum = (draft.question_type_ratios ?? []).reduce((s, r) => s + (Number(r.ratio) || 0), 0);
  const chapterSum = (draft.chapter_weights ?? []).reduce((s, c) => s + (Number(c.weight) || 0), 0);
  const hasRules = (rules?.question_type_ratios?.length ?? 0) > 0;

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.framework.updateExamRules(courseId, draft);
      addToast('考核规则已保存，下次出卷将按新比例分配', 'success');
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
            来自考核大纲的考试形式、题型比例与章节命题权重；蓝图按此推导试卷结构
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
                      {qlabel(r.question_type)} {r.ratio}%
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
                      {anchors.find((a) => a.key === c.anchor_key)?.title || c.anchor_key}：<strong style={{ color: 'var(--text)' }}>{c.weight}%</strong>
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
                  合计 {ratioSum.toFixed(1)}%
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
                合计 {chapterSum.toFixed(1)}%
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

          <div style={{ padding: '10px 14px', borderRadius: 8, background: 'var(--info-subtle)', fontSize: '0.78rem', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
            比例不需要手工凑满 100：保存时会自动归一到 100。题型比例决定试卷的题型分布与分值，
            章节权重决定各章出题占比；考纲未声明的章节按 0 处理。
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
