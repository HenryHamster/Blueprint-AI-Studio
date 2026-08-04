/* eslint-disable react-refresh/only-export-components */
import { useCallback, useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';

export interface Toast {
  id: string;
  message: string;
  type: 'success' | 'error' | 'info';
  /** When true the toast stays until dismissed instead of auto-hiding after 4s. */
  persistent?: boolean;
}

const styles: Record<Toast['type'], string> = {
  success: 'bg-emerald-950/90 border-emerald-800/50 text-emerald-300',
  error: 'bg-red-950/90 border-red-800/50 text-red-300',
  info: 'bg-neutral-900/90 border-neutral-700/50 text-neutral-300',
};

const toastCounterRef = { current: 0 };

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((message: string, type: Toast['type'] = 'info', persistent = false) => {
    const id = `toast-${++toastCounterRef.current}`;
    setToasts(prev => [...prev, { id, message, type, persistent }]);
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  return { toasts, addToast, dismissToast };
}

function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: (id: string) => void }) {
  const [visible, setVisible] = useState(false);
  const [exiting, setExiting] = useState(false);
  const autoDismissRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const exitTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dismissedRef = useRef(false);

  const startExit = useCallback(() => {
    if (dismissedRef.current) return;
    dismissedRef.current = true;
    if (autoDismissRef.current) {
      clearTimeout(autoDismissRef.current);
      autoDismissRef.current = null;
    }
    setExiting(true);
    exitTimeoutRef.current = setTimeout(() => onDismiss(toast.id), 300);
  }, [toast.id, onDismiss]);

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));

    if (!toast.persistent) {
      autoDismissRef.current = setTimeout(startExit, 4000);
    }

    return () => {
      if (autoDismissRef.current) clearTimeout(autoDismissRef.current);
      if (exitTimeoutRef.current) clearTimeout(exitTimeoutRef.current);
    };
  }, [startExit, toast.persistent]);

  return (
    <div
      className={`${styles[toast.type]} border rounded-lg px-4 py-3 shadow-lg flex items-center gap-3 min-w-[280px] max-w-[420px] transition-all duration-300 ease-out ${
        visible && !exiting ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2'
      }`}
    >
      <span className="flex-1 text-sm">{toast.message}</span>
      <button
        onClick={startExit}
        className="opacity-60 hover:opacity-100 transition-opacity"
        aria-label="Dismiss"
      >
        <X size={14} />
      </button>
    </div>
  );
}

export function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2" role="status" aria-live="polite">
      {toasts.map(toast => (
        <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>
  );
}
