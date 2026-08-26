import type { ReactNode } from 'react';

interface Props {
  title?: string;
  subtitle?: string;
  className?: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function Card({ title, subtitle, className = '', actions, children }: Props) {
  return (
    <div className={`glass-card ${className}`.trim()} style={{ padding: '24px' }}>
      {(title || actions) && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: subtitle ? '4px' : '0' }}>
          <div>
            {title && <h3 style={{ fontSize: '1.05rem', fontWeight: 600, letterSpacing: '-0.01em' }}>{title}</h3>}
            {subtitle && <p style={{ fontSize: '0.8125rem', color: 'var(--text-secondary)', marginTop: '2px' }}>{subtitle}</p>}
          </div>
          {actions && <div style={{ display: 'flex', gap: '8px' }}>{actions}</div>}
        </div>
      )}
      <div style={title || subtitle ? {} : undefined}>
        {children}
      </div>
    </div>
  );
}
