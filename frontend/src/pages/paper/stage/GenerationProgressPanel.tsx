import { useState, useEffect } from 'react';
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { ProgressPanel, type ProgressStatus } from '@/components/ui';
import type { TaskRun } from '@/types/api';
import { isInFlight, isTerminal } from './stageShared';

// 生成阶段的阶段性文案。后端任务只上报「开始 5%」与「完成 100%」两档，
// 中间没有细分百分比，所以这里用轮旋文案 + 已等待时长表达推进感，
// 而不是伪造一个会跳变的假进度条。
const GENERATION_MESSAGES = [
  '正在按合同生成题目…',
  '正在进行答案与解析质检…',
  '正在校验收分与题型…',
  '正在整理试卷版本…',
];

const GENERATION_QUEUED_MESSAGES = ['等待 Celery worker 接管任务…'];

// queued 超过该时长提示排查 worker：worker 未启动或繁忙时任务会一直排队，
// 用户侧只看到一个"排队中"无法区分是正常等待还是卡死。
const QUEUED_HINT_SECONDS = 60;

// ═══════════════════════════════════════════════
//  生成进度面板（状态感知）
// ═══════════════════════════════════════════════
export function GenerationProgressPanel({
  taskRun, onRetry, onBack, onOpenPaper,
}: {
  taskRun: TaskRun;
  onRetry: () => void;
  onBack: () => void;
  onOpenPaper: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const inFlight = isInFlight(taskRun.status);

  useEffect(() => {
    if (!inFlight) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [inFlight]);

  const startedAt = Date.parse(taskRun.created_at);
  const elapsedSeconds = Number.isFinite(startedAt)
    ? Math.max(0, Math.floor((now - startedAt) / 1000))
    : 0;

  const status: ProgressStatus =
    taskRun.status === 'succeeded'
      ? 'succeeded'
      : taskRun.status === 'queued'
        ? 'queued'
        : taskRun.status === 'running'
          ? 'running'
          // failed 与 cancelled 都切到错误面板：ProgressStatus 没有「已取消」档，
          // 走同一套红色面板 + 重试入口，别让已取消的卡显示成「正在生成」。
          : 'failed';

  // succeeded 后 result 带题目数与 paper_version_id；缺省时退化为通用文案
  const result = (taskRun.result ?? {}) as Record<string, unknown>;
  const dropped = typeof result.dropped_questions === 'number' ? result.dropped_questions : 0;
  const resultMessage =
    taskRun.status === 'succeeded'
      ? typeof result.generated_questions === 'number'
        ? `已生成 ${result.generated_questions} 道试题。`
          + (dropped > 0 ? ` 其中 ${dropped} 道因缺题干或缺答案未写入试卷，请重试或补充知识卡后再次生成。` : '')
        : '试题已生成完毕。'
      : undefined;

  const title =
    taskRun.status === 'succeeded'
      ? '试题生成完成'
      : taskRun.status === 'queued'
        ? '任务排队中，等待执行…'
        : taskRun.status === 'cancelled'
          ? '任务已取消'
          : taskRun.status === 'failed'
            ? '试题生成失败'
            : '正在生成试题，请稍候…';

  return (
    <ProgressPanel
      title={title}
      messages={
        isTerminal(taskRun.status)
          ? undefined
          : taskRun.status === 'queued'
            ? GENERATION_QUEUED_MESSAGES
            : GENERATION_MESSAGES
      }
      progress={taskRun.progress ?? null}
      stageLabel={taskRun.stage && taskRun.status !== 'succeeded' ? `阶段：${taskRun.stage}` : undefined}
      status={status}
      elapsedSeconds={elapsedSeconds}
      queuedHintAfterSeconds={QUEUED_HINT_SECONDS}
      errorMessage={
        taskRun.status === 'cancelled'
          ? '任务已取消，可重新发起生成。'
          : taskRun.error_message || taskRun.error_code || '未知错误，请重试或联系管理员'
      }
      resultMessage={resultMessage}
      footer={
        taskRun.status === 'failed' || taskRun.status === 'cancelled' ? (
          <>
            <Button variant="secondary" onClick={onBack}><ArrowLeft size={16} /> 返回合同</Button>
            <Button onClick={onRetry} icon={<RefreshCw size={16} />}>重新生成</Button>
          </>
        ) : taskRun.status === 'succeeded' ? (
          <Button onClick={onOpenPaper} icon={<ArrowRight size={16} />}>查看试卷</Button>
        ) : undefined
      }
    />
  );
}
