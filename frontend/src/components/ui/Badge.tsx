import type { ReactNode } from 'react';

export type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'info' | 'purple';

interface Props {
  children: ReactNode;
  className?: string;
  variant?: string;
}

export function Badge({ children, className = '', variant = 'default' }: Props) {
  return <span className={`badge badge-${variant} ${className}`.trim()}>{children}</span>;
}

export function BadgeSuccess({ children }: { children: ReactNode }) {
  return <span className="badge badge-success">{children}</span>;
}

export function BadgeWarning({ children }: { children: ReactNode }) {
  return <span className="badge badge-warning">{children}</span>;
}

export function BadgeError({ children }: { children: ReactNode }) {
  return <span className="badge badge-error">{children}</span>;
}

export function BadgeInfo({ children }: { children: ReactNode }) {
  return <span className="badge badge-info">{children}</span>;
}

export function BadgePurple({ children }: { children: ReactNode }) {
  return <span className="badge badge-purple">{children}</span>;
}
