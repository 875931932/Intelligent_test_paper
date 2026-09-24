import { useEffect, useState } from 'react';
import { Check, RotateCcw, Sparkles, X } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import type { AiReviseResult, PaperVersionItem, TaskRun } from '@/types/api';

/** 任务终态（与后端 task_runs 状态机一致） */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

const PRESETS = [
  '让题干更简洁',
  '重写选项，使四个选项表述更平行',
  '优化解析，讲清楚为什么',
  '换一种问法，但考点和难度不变',
];

type FieldKey = 'stem' | 'options' | 'answer' | 'explanation';

const FIELD_LABEL: Record<FieldKey, string> = {
  stem: '题干',
  options: '选项',
  answer: '答案',
  explanation: '解析',
};

const FIELD_KEYS: FieldKey[] = ['stem', 'options', 'answer', 'explanation'];

/** 结构化比较：提案与原题一致的字段不进 diff（也就不进 PATCH） */
function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** 任意题面字段转可读文本（选项对象/数组、布尔答案都要能展示） */
function toText(value: unknown): string {
  if (value == null || value === '') return '（空）';
  if (typeof value === 'boolean') return value ? '正确' : '错误';
  if (Array.isArray(value)) {
    return value.map((v, i) => `${i + 1}. ${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`).join('\n');
  }
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${k}. ${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
      .join('\n');
  }
  return String(value);
}

/**
 * 单题 AI 改题面板：指令 → 轮询提案任务 → diff 预览 + 校验徽标 →
 * 确认后走既有 PATCH teacher_override 落库；应用后可一键撤销。
 */
export function AiRevisePanel({
  courseId, pvId, item, onApplied, onClose,
}: {
  courseId: string;
  pvId: string;
  item: PaperVersionItem;
  /** 应用/撤销后通知父级刷新试卷 */
  onApplied: () => void;
  onClose: () => void;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<AiReviseResult | null>(null);
  const [applying, setApplying] = useState(false);
  // 已应用字段的「应用前旧值」：撤销 = 把这些旧值 PATCH 回去（原题本就分层保留在 payload）
  const [snapshot, setSnapshot] = useState<Partial<Record<FieldKey, unknown>> | null>(null);

  // 轮询提案任务（与 PipelinePanel 的生成轮询同款：依赖只取 id，终态自停）
  useEffect(() => {
    if (!taskRunId) return;
    const timer = setInterval(async () => {
      try {
        const tr: TaskRun = await api.examProjects.getTaskRun(courseId, taskRunId, token ?? undefined);
        if (!TERMINAL.has(tr.status)) return;
        clearInterval(timer);
        setTaskRunId(null);
        setBusy(false);
        if (tr.status === 'succeeded' && tr.result) {
          setResult(tr.result as unknown as AiReviseResult);
        } else if (tr.status === 'failed') {
          addToast('AI 改题失败: ' + (tr.error_message || '未知错误'), 'error');
        }
      } catch {
        /* 瞬时轮询错误忽略，下一轮重试 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [taskRunId, courseId, token, addToast]);

  const submit = async (text?: string) => {
    const ins = (text ?? instruction).trim();
    if (!ins) {
      addToast('请先填写修改要求', 'info');
      return;
    }
    setInstruction(ins);
    setBusy(true);
    setResult(null);
    setSnapshot(null);
    try {
      const res = await api.paperVersions.aiRevise(courseId, pvId, item.item_index, ins, token ?? undefined);
      setTaskRunId(res.task_run_id);
    } catch (e) {
      setBusy(false);
      addToast('发起 AI 改题失败: ' + getErrorMessage(e), 'error');
    }
  };

  const changedKeys = result
    ? FIELD_KEYS.filter((k) => !sameValue(result.proposal[k], result.current[k]))
    : [];
  const canApply = !!result?.validation.passed && changedKeys.length > 0 && !snapshot;

  const apply = async () => {
    if (!result || !canApply) return;
    setApplying(true);
    const patch: Record<string, unknown> = {};
    const prev: Partial<Record<FieldKey, unknown>> = {};
    for (const k of changedKeys) {
      patch[k] = result.proposal[k];
      prev[k] = result.current[k];
    }
    try {
      await api.paperVersions.patchItem(courseId, pvId, item.item_index, { teacher_override_patch: patch }, token ?? undefined);
      setSnapshot(prev);
      addToast(`第 ${item.item_index} 题已应用 AI 修改`, 'success');
      onApplied();
    } catch (e) {
      addToast('应用失败: ' + getErrorMessage(e), 'error');
    } finally {
      setApplying(false);
    }
  };

  const undo = async () => {
    if (!snapshot) return;
    setApplying(true);
    try {
      await api.paperVersions.patchItem(courseId, pvId, item.item_index, { teacher_override_patch: { ...snapshot } }, token ?? undefined);
      setSnapshot(null);
      setResult(null);
      addToast('已撤销本次 AI 修改', 'success');
      onApplied();
    } catch (e) {
      addToast('撤销失败: ' + getErrorMessage(e), 'error');
    } finally {
      setApplying(false);
    }
  };

  const running = busy || !!taskRunId;

  return (
    <div
      className="glass-card"
      style={{
        padding: '16px 18px',
        borderLeft: '3px solid var(--purple, #7c5cff)',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <Sparkles size={16} style={{ color: 'var(--purple, #7c5cff)' }} />
        <span style={{ fontWeight: 700, fontSize: '0.9rem' }}>AI 改题 · 第 {item.item_index} 题</span>
        <Badge variant="purple">提案需确认</Badge>
        <button
          onClick={onClose}
          aria-label="关闭"
          style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', display: 'flex' }}
        >
          <X size={16} />
        </button>
      </div>

      {/* 指令输入：始终可见，便于提案后追加要求再来一轮 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="告诉 AI 怎么改这道题（只改表述与选项，题型/分值/难度由合同锁定）"
          rows={2}
          disabled={running}
          style={{
            resize: 'vertical', padding: '8px 10px', borderRadius: 8, fontSize: '0.875rem',
            border: '1px solid rgba(0,0,0,0.12)', background: 'rgba(255,255,255,0.75)',
            fontFamily: 'inherit', lineHeight: 1.6,
          }}
        />
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
          {PRESETS.map((p) => (
            <button
              key={p}
              disabled={running}
              onClick={() => void submit(p)}
              style={{
                padding: '3px 10px', borderRadius: 999, fontSize: '0.75rem', fontWeight: 600,
                background: 'rgba(124,92,255,0.08)', color: 'var(--purple, #7c5cff)',
                border: '1px solid rgba(124,92,255,0.25)', cursor: running ? 'default' : 'pointer',
                opacity: running ? 0.5 : 1,
              }}
            >
              {p}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <Button size="sm" loading={running} onClick={() => void submit()} icon={<Sparkles size={14} />}>
            {running ? 'AI 改题中…（约几秒）' : result ? '按新要求再改一版' : '生成修改提案'}
          </Button>
          {result && !running && (
            <Button size="sm" variant="ghost" onClick={() => setResult(null)}>清空提案</Button>
          )}
        </div>
      </div>

      {result && (
        <>
          {/* 校验徽标 + AI 自述 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            {result.validation.passed
              ? <Badge variant="success">校验通过</Badge>
              : <Badge variant="error">未通过校验 · {result.validation.code}</Badge>}
            {!result.validation.passed && (
              <span style={{ fontSize: '0.78rem', color: 'var(--error)' }}>{result.validation.message}</span>
            )}
            {changedKeys.length === 0 && <Badge variant="default">提案与原题无差异</Badge>}
          </div>
          {result.change_summary && (
            <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.7, margin: 0 }}>
              <span style={{ fontWeight: 600, color: 'var(--purple, #7c5cff)' }}>AI 说明：</span>
              {result.change_summary}
            </p>
          )}

          {/* 字段级 diff：只展示有变化的字段 */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {changedKeys.map((k) => (
              <div key={k}>
                <div style={{ fontSize: '0.75rem', fontWeight: 700, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                  {FIELD_LABEL[k]}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  <div style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: '0.82rem', lineHeight: 1.65,
                    background: 'var(--error-subtle, rgba(220,38,38,0.06))',
                    color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                    textDecoration: 'line-through', textDecorationColor: 'rgba(220,38,38,0.4)',
                  }}>
                    {toText(result.current[k])}
                  </div>
                  <div style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: '0.82rem', lineHeight: 1.65,
                    background: 'var(--success-subtle, rgba(22,163,74,0.08))',
                    color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  }}>
                    {toText(result.proposal[k])}
                  </div>
                </div>
              </div>
            ))}
            {changedKeys.length === 0 && (
              <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', margin: 0 }}>
                提案没有改动任何字段，可换一种要求重新生成。
              </p>
            )}
          </div>

          {/* 确认 / 撤销 */}
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', paddingTop: '10px', borderTop: '1px solid rgba(0,0,0,0.06)' }}>
            {!snapshot ? (
              <>
                <Button
                  size="sm"
                  disabled={!canApply || applying}
                  loading={applying}
                  onClick={() => void apply()}
                  icon={<Check size={14} />}
                >
                  确认应用到本题
                </Button>
                {!result.validation.passed && (
                  <span style={{ fontSize: '0.75rem', color: 'var(--warning)' }}>
                    校验未通过，不能应用——可调整要求后重新生成
                  </span>
                )}
              </>
            ) : (
              <>
                <Badge variant="success">已应用</Badge>
                <Button size="sm" variant="secondary" disabled={applying} loading={applying} onClick={() => void undo()} icon={<RotateCcw size={14} />}>
                  撤销本次修改
                </Button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
