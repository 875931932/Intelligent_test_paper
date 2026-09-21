import { useEffect, useState, type ReactNode } from 'react';
import { AlertCircle, Clock, Loader } from 'lucide-react';

/** 任务状态：queued 排队中 / running（含 waiting_external）执行中 / failed 失败 */
export type ProgressStatus = 'queued' | 'running' | 'failed';

interface ProgressPanelProps {
  /** 主标题，默认“正在处理，请稍候…” */
  title?: string;
  /** 轮换展示的阶段性文案，让等待过程更有“进行感” */
  messages?: string[];
  /** 0-100 的真实进度；不传或为 null/undefined 时显示不确定动画 */
  progress?: number | null;
  /** 阶段标签（如后端返回的 stage） */
  stageLabel?: string;
  /** 任务状态。queued 时强调排队等待并给出超时提示；failed 时切换为错误面板 */
  status?: ProgressStatus;
  /** 已等待秒数，展示在状态行，用于 queued/running 的等待时长提示 */
  elapsedSeconds?: number;
  /** queued 超过该秒数时提示排查 worker（默认 60 秒） */
  queuedHintAfterSeconds?: number;
  /** queued 超时提示的自定义文案 */
  queuedHint?: string;
  /** failed 状态的错误详情 */
  errorMessage?: string;
  /** 底部操作区（如「重试」「返回」按钮），由调用方注入 */
  footer?: ReactNode;
}

const DEFAULT_QUEUED_HINT =
  '排队已久。Celery worker 可能未启动或正忙，请联系管理员检查 worker 进程；确认 worker 正常后任务会自动继续。';

function formatElapsed(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds > 0 ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
}

export function ProgressPanel({
  title,
  messages,
  progress,
  stageLabel,
  status = 'running',
  elapsedSeconds,
  queuedHintAfterSeconds = 60,
  queuedHint,
  errorMessage,
  footer,
}: ProgressPanelProps) {
  const [messageIndex, setMessageIndex] = useState(0);

  const failed = status === 'failed';
  const queued = status === 'queued';

  useEffect(() => {
    if (!messages || messages.length === 0 || failed) return;
    const id = setInterval(() => setMessageIndex((i) => (i + 1) % messages.length), 3200);
    return () => clearInterval(id);
  }, [messages, failed]);

  const determinate = typeof progress === 'number' && progress >= 0;
  const currentMessage = messages && messages.length > 0 ? messages[messageIndex] : null;
  const pct = determinate ? Math.min(100, Math.max(0, progress as number)) : 0;

  // ── 失败态：不再转圈，直接暴露错误与补救入口 ──
  if (failed) {
    return (
      <div
        className="glass-panel"
        style={{
          padding: '40px 24px',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: '14px',
          textAlign: 'center',
        }}
      >
        <span style={{
          width: 44,
          height: 44,
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'rgba(255,59,48,0.1)',
          color: '#ff3b30',
        }}>
          <AlertCircle size={22} />
        </span>
        <div>
          <p style={{ fontWeight: 600, fontSize: '0.95rem' }}>{title ?? '处理失败'}</p>
          {errorMessage && (
            <p style={{ fontSize: '0.83rem', color: 'var(--text-secondary)', marginTop: '6px', maxWidth: '520px' }}>
              {errorMessage}
            </p>
          )}
        </div>
        {footer && <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>{footer}</div>}
      </div>
    );
  }

  const showQueuedHint =
    queued && typeof elapsedSeconds === 'number' && elapsedSeconds >= queuedHintAfterSeconds;

  return (
    <div
      className="glass-panel"
      style={{
        padding: '48px 24px',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: '14px',
        textAlign: 'center',
      }}
    >
      <div className="spinner spinner-lg" />

      <div>
        <p style={{ fontWeight: 600, fontSize: '0.95rem' }}>
          {title ?? (queued ? '排队中，等待执行…' : '正在处理，请稍候…')}
        </p>
        {currentMessage && (
          <p
            key={messageIndex}
            className="progress-message"
            style={{ fontSize: '0.83rem', color: 'var(--text-secondary)', marginTop: '4px' }}
          >
            {currentMessage}
          </p>
        )}
      </div>

      {/* 进度条：后端给出真实百分比时确定展示，否则以滑动动画表示仍在推进 */}
      <div className="progress-track" style={{ width: 'min(420px, 100%)' }}>
        <div
          className={`progress-fill${determinate ? '' : ' progress-indeterminate'}`}
          style={determinate ? { width: `${pct}%` } : undefined}
        />
      </div>

      <div
        style={{
          display: 'flex',
          gap: '12px',
          fontSize: '0.78rem',
          color: 'var(--text-tertiary)',
          flexWrap: 'wrap',
          justifyContent: 'center',
        }}
      >
        {determinate && <span>{pct}%</span>}
        {stageLabel && <span>{stageLabel}</span>}
        {typeof elapsedSeconds === 'number' && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
            <Clock size={12} />已等待 {formatElapsed(elapsedSeconds)}
          </span>
        )}
      </div>

      {showQueuedHint && (
        <p
          style={{
            fontSize: '0.78rem',
            color: '#b36b00',
            background: 'rgba(255,149,0,0.08)',
            border: '1px solid rgba(255,149,0,0.25)',
            borderRadius: '8px',
            padding: '8px 14px',
            maxWidth: '520px',
            display: 'flex',
            alignItems: 'flex-start',
            gap: '6px',
            textAlign: 'left',
          }}
        >
          <Loader size={13} style={{ marginTop: '2px', flexShrink: 0 }} />
          <span>{queuedHint ?? DEFAULT_QUEUED_HINT}</span>
        </p>
      )}

      {footer && <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>{footer}</div>}
    </div>
  );
}
