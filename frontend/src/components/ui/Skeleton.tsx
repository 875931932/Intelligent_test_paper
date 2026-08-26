import type { CSSProperties, FC } from 'react';

interface SkeletonProps {
  className?: string;
  style?: CSSProperties;
  /** 附加 CSS 类，覆盖默认骨架类型 */
  variant?: 'text' | 'title' | 'card' | 'avatar' | 'button' | 'row';
}

const VARIANT_CLASS: Record<string, string> = {
  text: 'skeleton-text',
  title: 'skeleton-title',
  card: 'skeleton-card',
  avatar: 'skeleton-avatar',
  button: 'skeleton-btn',
  row: 'skeleton-row',
};

export const Skeleton: FC<SkeletonProps> = ({ className = '', variant = 'text', style }) => {
  const base = variant === 'row' ? 'skeleton-row' : `skeleton ${VARIANT_CLASS[variant] || ''}`;
  return <div className={`${base} ${className}`.trim()} style={style} aria-hidden="true" />;
};

/** 骨架屏容器：一段文本骨架 */
export const SkeletonBlock: FC<{ lines?: number; className?: string }> = ({ lines = 3, className }) => (
  <div className={className} style={{ width: '100%' }}>
    {Array.from({ length: lines }).map((_, i) => (
      <div key={i} className="skeleton skeleton-text" style={i === lines - 1 ? { width: '60%' } : undefined} />
    ))}
  </div>
);

/** 骨架屏卡片网格 */
export const SkeletonCardGrid: FC<{ count?: number; className?: string }> = ({ count = 4, className }) => (
  <div className={`card-grid ${className || ''}`.trim()}>
    {Array.from({ length: count }).map((_, i) => (
      <div key={i} className="skeleton skeleton-card" />
    ))}
  </div>
);