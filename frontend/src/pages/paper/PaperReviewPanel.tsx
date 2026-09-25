import { useEffect, useState } from 'react';
import { FileSearch } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { toText } from '@/lib/examDisplay';
import type { PaperReviewResult, TaskRun } from '@/types/api';

/** 任务终态（与后端 task_runs 状态机一致） */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

/**
 * 整卷 AI 质量评审面板：可选关注点 → 轮询评审任务 → 只读质量报告
 * （难度分布/题面表述/答案与解析一致性/覆盖与配额/风险题的解读）。
 * 纯只读——AI 不落任何改动，报告只针对试卷稿、不含学生答卷评分；
 * 修复问题由教师走试卷页既有编辑/AI 改题/重新生成功能。
 */
export function PaperReviewPanel({
  courseId, pvId,
}: {
  courseId: string;
  pvId: string;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<PaperReviewResult | null>(null);

  // 轮询评审任务（与 AiRevisePanel 同款：依赖只取 id，终态自停）
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
          setResult(tr.result as unknown as PaperReviewResult);
        } else if (tr.status === 'failed') {
          addToast('AI 质量评审失败: ' + (tr.error_message || '未知错误'), 'error');
        }
      } catch {
        /* 瞬时轮询错误忽略，下一轮重试 */
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [taskRunId, courseId, token, addToast]);

  const running = busy || !!taskRunId;

  const submit = async () => {
    setBusy(true);
    setResult(null);
    try {
      const res = await api.paperVersions.aiReview(courseId, pvId, instruction.trim(), token ?? undefined);
      setTaskRunId(res.task_run_id);
    } catch (e) {
      setBusy(false);
      addToast('发起 AI 质量评审失败: ' + getErrorMessage(e), 'error');
    }
  };

  return (
    <div
      style={{
        padding: '16px 24px 16px 22px',
        borderLeft: '3px solid #5856d6',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}
    >
      {/* 头部（标题/最小化/关闭）由 FloatingPanel 外壳统一提供 */}

      {/* 关注点输入在面板底部（见下），报告可任意加长而不挤压输入区 */}

      {result && (
        <>
          {/* 结论徽标 + 确定性数据快照 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            {result.verdict === 'pass'
              ? <Badge variant="success">结论：通过</Badge>
              : <Badge variant="warning">结论：需关注</Badge>}
            {!result.validated && (
              <Badge variant="warning">内容未通过校验 · 仅供参考</Badge>
            )}
            <span style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
              待审核 {result.deterministic.needs_review_count} 题 · 合同终检
              {result.deterministic.final_check_available ? '已纳入评审' : '不可用（无合同快照）'}
            </span>
          </div>

          {/* 总评 */}
          <p style={{ fontSize: '0.84rem', color: 'var(--text-secondary)', lineHeight: 1.7, margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            <span style={{ fontWeight: 600, color: '#5856d6' }}>总评：</span>
            {toText(result.summary)}
          </p>

          {/* 分维度报告块 */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {result.sections.map((s, i) => (
              <div
                key={i}
                style={{
                  padding: '10px 12px', borderRadius: 8,
                  borderLeft: s.severity === 'warn' ? '3px solid var(--warning)' : '3px solid rgba(0,0,0,0.12)',
                  background: 'rgba(255,255,255,0.55)',
                  display: 'flex', flexDirection: 'column', gap: '6px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 700, fontSize: '0.8rem' }}>{s.dimension}</span>
                  {s.severity === 'warn'
                    ? <Badge variant="warning">需处理</Badge>
                    : <Badge variant="info">提示</Badge>}
                  {s.item_indexes.length > 0 && (
                    <span style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                      {s.item_indexes.map((idx) => (
                        <span
                          key={idx}
                          style={{
                            padding: '1px 8px', borderRadius: 999, fontSize: '0.72rem', fontWeight: 600,
                            background: 'var(--accent-subtle)', color: 'var(--accent)',
                          }}
                        >
                          第 {idx} 题
                        </span>
                      ))}
                    </span>
                  )}
                </div>
                <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.65, margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                  {toText(s.finding)}
                </p>
                {s.suggestion && (
                  <p style={{ fontSize: '0.8rem', color: 'var(--text-tertiary)', lineHeight: 1.6, margin: 0 }}>
                    <span style={{ fontWeight: 600 }}>建议：</span>
                    {toText(s.suggestion)}
                  </p>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      {/* 页脚：范围定位（试卷稿，不是学生答卷评分）+ 落地路径；关闭由外壳提供 */}
      <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: 0, lineHeight: 1.6, paddingTop: '10px', borderTop: '1px solid rgba(0,0,0,0.06)' }}>
        AI 只读评审，不含学生答卷评分；修复请用试卷页既有编辑/改题功能。
      </p>

      {/* 关注点输入（悬浮）：置于面板末尾并 sticky 钉住滚动容器底边，
          报告再长也不会把输入条推出视野；不透明背景 + 阴影保证浮在报告
          之上时文字不透出，z-index 低于弹窗与 toast */}
      <div
        style={{
          display: 'flex', flexDirection: 'column', gap: '8px',
          position: 'sticky', bottom: 12, zIndex: 5,
          padding: '12px 14px', borderRadius: 12,
          background: 'var(--surface-elevated)',
          backdropFilter: 'var(--glass-blur)',
          WebkitBackdropFilter: 'var(--glass-blur)',
          border: '1px solid rgba(0,0,0,0.06)',
          boxShadow: '0 8px 32px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.05)',
        }}
      >
        <textarea
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="可选关注点：如「重点关注难度分布」「检查答案与解析是否一致」，留空则做标准整卷评审"
          rows={2}
          disabled={running}
          style={{
            resize: 'vertical', padding: '8px 10px', borderRadius: 8, fontSize: '0.875rem',
            border: '1px solid rgba(0,0,0,0.12)', background: 'rgba(255,255,255,0.75)',
            fontFamily: 'inherit', lineHeight: 1.6,
          }}
        />
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <Button size="sm" loading={running} onClick={() => void submit()} icon={<FileSearch size={14} />}>
            {running ? '评审中…（约几秒）' : result ? '重新评审' : '发起 AI 质量评审'}
          </Button>
          {result && !running && (
            <Button size="sm" variant="ghost" onClick={() => setResult(null)}>清空报告</Button>
          )}
        </div>
      </div>
    </div>
  );
}
