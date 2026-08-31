import type { ReactNode } from 'react';
import { X } from 'lucide-react';
import { Button } from './Button';

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  maxWidth?: string;
  onConfirm?: () => void | Promise<void>;
  confirmLabel?: string;
  loading?: boolean;
  danger?: boolean;
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  maxWidth = '560px',
  onConfirm,
  confirmLabel = '确认',
  loading = false,
  danger = false,
}: Props) {
  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" style={{ maxWidth }} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3 className="modal-title">{title}</h3>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: '4px',
            borderRadius: '6px', color: 'var(--text-tertiary)', display: 'flex',
          }}
            onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0,0,0,0.05)'; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'none'; }}
          >
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">
          {children}
        </div>
        {footer ? (
          <div className="modal-footer">{footer}</div>
        ) : onConfirm ? (
          <div className="modal-footer">
            <Button variant="secondary" onClick={onClose}>取消</Button>
            <Button
              variant={danger ? 'danger' : 'primary'}
              loading={loading}
              disabled={loading}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
