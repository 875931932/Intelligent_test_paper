import { useEffect, useState } from 'react';
import { CheckCircle2, Eye, Sparkles, Wrench } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Button } from '@/components/ui/Button';
import type { FrameworkReviewFinding, FrameworkReviewResult, TaskRun } from '@/types/api';

/** 任务终态（与后端 task_runs 状态机一致） */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

const AREA_LABELS: Record<FrameworkReviewFinding['area'], string> = {
  coverage: '考点覆盖',
  weight: '权重分配',
  question_type: '题型可行性',
  cognitive: '认知分层',
  rules: '考试规则',
  conflicts: '冲突处置',
  other: '其他',
};

const SEVERITY_STYLES: Record<
  FrameworkReviewFinding['severity'],
  { label: string; color: string; background: string }
> = {
  info: { label: '提示', color: 'var(--info)', background: 'var(--info-subtle)' },
  warning: { label: '注意', color: 'var(--warning)', background: 'var(--warning-subtle)' },
  critical: { label: '重要', color: 'var(--error-ink)', background: 'var(--error-subtle)' },
};

/**
 * 框架候选 AI 评审面板：一句话（可空）→ 评审任务 → 轮询 → 只读报告。
 * 报告（verdict/summary/findings）仅供教师参考——不改框架、不代替裁决冲突，
 * 确认/拒绝仍由教师点既有按钮完成。
 */
export function FrameworkReviewPanel({ courseId }: { courseId: string }) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<FrameworkReviewResult | null>(null);

  const running = busy || !!taskRunId;

  // 轮询评审任务（与 ExamRulesCard 同款：依赖只取 id，终态自停）
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
          const res = tr.result as unknown as FrameworkReviewResult;
          setResult(res);
          addToast(
            res.findings.length > 0
              ? `AI 评审完成：${res.findings.length} 条发现`
              : 'AI 评审完成：未发现问题',
            'success',
          );
        } else if (tr.status === 'failed') {
          addToast('AI 评审失败: ' + (tr.error_message || '未知错误'), 'error');
        }
      } catch {
        /* 瞬时轮询错误忽略，下一轮重试 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [taskRunId, courseId, token, addToast]);

  const handleReview = async () => {
    setBusy(true);
    try {
      const res = await api.framework.reviewFramework(courseId, instruction.trim(), token ?? undefined);
      setTaskRunId(res.task_run_id);
    } catch (e) {
      setBusy(false);
      addToast('发起 AI 评审失败: ' + getErrorMessage(e), 'error');
    }
  };

  const isReady = result?.verdict === 'ready';

  return (
    <div
      className="glass-card"
      style={{ padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}
    >
      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
        <span
          style={{
            display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap',
            fontSize: '0.875rem', fontWeight: 600, color: 'var(--purple)',
          }}
        >
          <Sparkles size={16} /> AI 评审
          <span style={{ fontSize: '0.75rem', fontWeight: 400, color: 'var(--text-tertiary)' }}>只读参考</span>
        </span>
        <input
          className="input-field"
          style={{ flex: 1, minWidth: 220 }}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void handleReview(); }}
          placeholder="可留空做常规评审；也可写：重点看权重与覆盖"
          aria-label="评审要求（一句话，可空）"
          disabled={running}
        />
        <Button size="sm" variant="secondary" loading={running} onClick={() => void handleReview()} icon={<Eye size={14} />}>
          {running ? '评审中…' : '生成评审'}
        </Button>
      </div>

      {result && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
            <span
              style={{
                display: 'inline-flex', alignItems: 'center', gap: '5px',
                padding: '3px 10px', borderRadius: '999px',
                fontSize: '0.78rem', fontWeight: 600,
                background: isReady ? 'var(--success-subtle)' : 'var(--warning-subtle)',
                color: isReady ? 'var(--success)' : 'var(--warning)',
              }}
            >
              {isReady ? <CheckCircle2 size={13} /> : <Wrench size={13} />}
              参考意见：{isReady ? '可确认发布' : '建议先修改'}
            </span>
          </div>

          <p style={{ fontSize: '0.875rem', lineHeight: 1.7, color: 'var(--text-secondary)', margin: 0 }}>
            {result.summary}
          </p>

          {result.findings.length === 0 ? (
            <p style={{ fontSize: '0.8125rem', color: 'var(--text-tertiary)', margin: 0 }}>
              未发现需要处理的问题。
            </p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {result.findings.map((f, i) => {
                const style = SEVERITY_STYLES[f.severity] ?? SEVERITY_STYLES.info;
                return (
                  <div
                    key={i}
                    style={{
                      padding: '10px 12px', borderRadius: 'var(--radius-sm)',
                      background: style.background, border: '1px solid var(--line-soft)',
                      display: 'flex', flexDirection: 'column', gap: '4px',
                    }}
                  >
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                      <span style={{ fontSize: '0.7rem', fontWeight: 600, color: style.color }}>
                        {style.label}
                      </span>
                      <span
                        style={{
                          fontSize: '0.7rem', fontWeight: 500, padding: '1px 8px', borderRadius: 'var(--radius-full)',
                          background: 'var(--fill-strong)', color: 'var(--text-secondary)',
                        }}
                      >
                        {AREA_LABELS[f.area] ?? f.area}
                      </span>
                    </div>
                    <p style={{ fontSize: '0.8125rem', lineHeight: 1.6, margin: 0 }}>{f.message}</p>
                    {f.suggestion && (
                      <p style={{ fontSize: '0.78rem', lineHeight: 1.6, margin: 0, color: 'var(--text-secondary)' }}>
                        建议：{f.suggestion}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: 0 }}>
            评审为只读参考，不修改框架、不代替你裁决冲突——确认或拒绝仍由你决定。
          </p>
        </div>
      )}
    </div>
  );
}
