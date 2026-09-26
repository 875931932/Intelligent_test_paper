import { forwardRef, useEffect, useImperativeHandle, useState } from 'react';
import { Check, Plus, Save, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { DIFFICULTY_OPTIONS, QUESTION_TYPE_OPTIONS } from '@/lib/examDisplay';
import {
  type Draft, type EditorSubmit,
  answerForSubmit, entriesToOptions, hasOptions, optionKeysOf, toggleAnswerKey,
} from './questionShared';

// ─── 题目编辑器（右栏原地编辑与「新增题目」弹窗共用） ───

export interface QuestionEditorHandle {
  submit: () => void;
  /** 用外部来源（AI 生成提案）整体替换表单草稿 */
  setDraft: (d: Draft) => void;
}

export const QuestionEditor = forwardRef<QuestionEditorHandle, {
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