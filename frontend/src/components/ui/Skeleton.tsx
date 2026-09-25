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

/** 行卡列表骨架（列表页/行式内容首屏） */
export const SkeletonList: FC<{ rows?: number }> = ({ rows = 4 }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', width: '100%' }}>
    {Array.from({ length: rows }).map((_, i) => (
      <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <div className="skeleton" style={{ width: 40, height: 40, borderRadius: 12, flexShrink: 0 }} />
        <div className="skeleton" style={{ height: 16, flex: 1, borderRadius: 8 }} />
      </div>
    ))}
  </div>
);

/** 表单/详情骨架（标题 + 字段行，贴详情/编辑器布局） */
export const SkeletonForm: FC<{ fields?: number }> = ({ fields = 4 }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', width: '100%' }}>
    <div className="skeleton skeleton-title" />
    {Array.from({ length: fields }).map((_, i) => (
      <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        <div className="skeleton" style={{ height: 12, width: '24%', borderRadius: 8 }} />
        <div className="skeleton" style={{ height: 36, borderRadius: 'var(--radius-md)' }} />
      </div>
    ))}
  </div>
);

/** 表格骨架（标题行 + 等高数据行，贴表格布局） */
export const SkeletonTable: FC<{ rows?: number }> = ({ rows = 6 }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', width: '100%' }}>
    <div className="skeleton" style={{ height: 16, width: '30%', borderRadius: 8, marginBottom: '4px' }} />
    {Array.from({ length: rows }).map((_, i) => (
      <div key={i} className="skeleton" style={{ height: 40, borderRadius: 'var(--radius-md)' }} />
    ))}
  </div>
);