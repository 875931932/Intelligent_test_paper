import { forwardRef } from 'react';
import type { InputHTMLAttributes } from 'react';

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export const Input = forwardRef<HTMLInputElement, Props>(
  ({ label, error, className = '', ...props }, ref) => {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {label && <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>}
        <input ref={ref} className={`input-field ${className}`} {...props} />
        {error && <span style={{ fontSize: '0.75rem', color: 'var(--error)' }}>{error}</span>}
      </div>
    );
  },
);
Input.displayName = 'Input';
