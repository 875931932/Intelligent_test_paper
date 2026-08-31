import type { SelectHTMLAttributes } from 'react';

export interface SelectOption {
  value: string;
  label: string;
}

interface Props extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  options?: SelectOption[];
}

export function Select({ label, options, children, className = '', ...props }: Props) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
      {label && <label style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</label>}
      <select className={`input-field ${className}`} {...props}>
        {options
          ? options.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))
          : children}
      </select>
    </div>
  );
}
