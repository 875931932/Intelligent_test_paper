import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
import { api } from '@/api/client';
import { getErrorMessage } from '@/api/errors';
import { useAuthStore } from '@/stores/auth';
import { useToastStore } from '@/stores/toast';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { toText } from '@/lib/examDisplay';
import type { ContractExplainResult, TaskRun } from '@/types/api';

/** 任务终态（与后端 task_runs 状态机一致） */
const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

/**
 * 合同槽位 AI 解释面板：解释确定性算法为什么把该原子分给这个槽位，
 * 并给出调整建议。纯只读——AI 不落任何改动，实际调整仍由教师走既有
 * 「合同修订」（revise → confirm）或切换分配方案重新分配。
 */
export function ContractExplainPanel({
  courseId, projectId, itemIndex, allocationSeed, blueprintVersionId,
}: {
  courseId: string;
  projectId: string;
  /** 合同槽位 item_index（与 plan_items.item_index 同源，1 起） */
  itemIndex: number;
  /** 当前「分配方案」对应的种子（第 N 版 = N-1），解释须与所见方案一致 */
  allocationSeed: number;
  blueprintVersionId?: string | null;
}) {
  const token = useAuthStore((s) => s.token);
  const addToast = useToastStore((s) => s.addToast);

  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [taskRunId, setTaskRunId] = useState<string | null>(null);
  const [result, setResult] = useState<ContractExplainResult | null>(null);

  // 轮询解释任务（与 AiRevisePanel 同款：依赖只取 id，终态自停）
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
          setResult(tr.result as unknown as ContractExplainResult);
        } else if (tr.status === 'failed') {
          addToast('AI 解释失败: ' + (tr.error_message || '未知错误'), 'error');
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
      const res = await api.examProjects.explainContractSlot(courseId, projectId, itemIndex, {
        allocation_seed: allocationSeed,
        blueprint_version_id: blueprintVersionId ?? undefined,
        instruction: instruction.trim() || undefined,
      }, token ?? undefined);
      setTaskRunId(res.task_run_id);
    } catch (e) {
      setBusy(false);
      addToast('发起 AI 解释失败: ' + getErrorMessage(e), 'error');
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

      {/* 追问输入在面板底部（见下），解释可任意加长而不挤压输入区 */}

      {result && (
        <>
          {!result.validated && (
            <Badge variant="warning">内容未通过校验 · 仅供参考</Badge>
          )}
          {result.instruction_response && (
            <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.7, margin: 0, whiteSpace: 'pre-wrap' }}>
              <span style={{ fontWeight: 600, color: '#5856d6' }}>追问回答：</span>
              {result.instruction_response}
            </p>
          )}
          <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.7, margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {toText(result.explanation)}
          </p>
          {result.suggestions.length > 0 && (
            <div>
              <div style={{ fontSize: '0.75rem', fontWeight: 700, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                调整建议
              </div>
              <ul style={{ margin: 0, paddingLeft: '18px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {result.suggestions.map((sug, i) => (
                  <li key={i} style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', lineHeight: 1.65 }}>
                    <span style={{ fontWeight: 600 }}>{toText(sug.concern)}</span>
                    {' → '}{toText(sug.suggestion)}
                    {sug.target_item_index != null && (
                      <span style={{ color: 'var(--text-tertiary)' }}>（涉及题位 {sug.target_item_index}）</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      <p style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', margin: 0 }}>
        AI 只提供解释与建议，不直接改动合同；实际调整请走「合同修订」，或切换分配方案后重新分配。
      </p>

      {/* 追问输入（悬浮）：置于面板末尾并 sticky 钉住滚动容器底边，
          解释再长也不会把输入条推出视野；不透明背景 + 阴影保证浮在解释
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
          placeholder="可选追问：如「为什么不是另一个原子？」「换一个知识点行不行？」留空则生成标准解释"
          rows={2}
          disabled={running}
          style={{
            resize: 'vertical', padding: '8px 10px', borderRadius: 8, fontSize: '0.875rem',
            border: '1px solid rgba(0,0,0,0.12)', background: 'rgba(255,255,255,0.75)',
            fontFamily: 'inherit', lineHeight: 1.6,
          }}
        />
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <Button size="sm" loading={running} onClick={() => void submit()} icon={<Sparkles size={14} />}>
            {running ? '解释生成中…（约几秒）' : result ? '再问一次' : '生成解释'}
          </Button>
          {result && !running && (
            <Button size="sm" variant="ghost" onClick={() => setResult(null)}>清空</Button>
          )}
        </div>
      </div>
    </div>
  );
}
