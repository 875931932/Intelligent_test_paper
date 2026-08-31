import { useToastStore } from '../../stores/toast';
import { X, CheckCircle, AlertCircle, Info } from 'lucide-react';

export function ToastContainer() {
  const { toasts, removeToast } = useToastStore();

  if (toasts.length === 0) return null;

  const icons: Record<string, React.ReactNode> = {
    success: <CheckCircle size={18} />,
    error: <AlertCircle size={18} />,
    info: <Info size={18} />,
  };

  return (
    <div className="toast-container">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast-${toast.type}`} onClick={() => removeToast(toast.id)}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' }}>
            {icons[toast.type]}
            <span>{toast.message}</span>
            <X size={14} style={{ marginLeft: 'auto', opacity: 0.5, flexShrink: 0 }} />
          </div>
        </div>
      ))}
    </div>
  );
}
