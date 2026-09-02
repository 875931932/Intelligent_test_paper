import { useEffect, useState } from 'react';

interface ProgressPanelProps {
  /** 主标题，默认“正在处理，请稍候…” */
  title?: string;
  /** 轮换展示的阶段性文案，让等待过程更有“进行感” */
  messages?: string[];
  /** 0-100 的真实进度；不传或为 null/undefined 时显示不确定动画 */
  progress?: number | null;
  /** 阶段标签（如后端返回的 stage） */
  stageLabel?: string;
}

export function ProgressPanel({
  title = '正在处理，请稍候…',
  messages,
  progress,
  stageLabel,
}: ProgressPanelProps) {
  const [messageIndex, setMessageIndex] = useState(0);

  useEffect(() => {
    if (!messages || messages.length === 0) return;
    const id = setInterval(() => setMessageIndex((i) => (i + 1) % messages.length), 3200);
    return () => clearInterval(id);
  }, [messages]);

  const determinate = typeof progress === 'number' && progress >= 0;
  const currentMessage = messages && messages.length > 0 ? messages[messageIndex] : null;
  const pct = determinate ? Math.min(100, Math.max(0, progress as number)) : 0;

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
        <p style={{ fontWeight: 600, fontSize: '0.95rem' }}>{title}</p>
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
      </div>
    </div>
  );
}
