import type { ReactNode, ButtonHTMLAttributes } from 'react';

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  icon?: ReactNode;
  loading?: boolean;
}

export function Button({
  variant = 'primary',
  size = 'md',
  icon,
  loading = false,
  children,
  className = '',
  disabled,
  ...props
}: Props) {
  const sizeClass = size !== 'md' ? `btn-${size}` : '';
  const classes = `btn btn-${variant} ${sizeClass} ${className}`.trim();
  const isSolid = variant === 'primary' || variant === 'danger';

  return (
    <button className={classes} disabled={disabled || loading} {...props}>
      {loading ? (
        <span
          className="spinner"
          style={{
            width: 14,
            height: 14,
            borderWidth: 2,
            borderColor: isSolid ? 'rgba(255,255,255,0.35)' : 'rgba(0,0,0,0.12)',
            borderTopColor: isSolid ? '#fff' : 'var(--accent)',
          }}
        />
      ) : (
        icon
      )}
      {children}
    </button>
  );
}
