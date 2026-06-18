interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ title, message, confirmLabel = 'Confirm', onConfirm, onCancel }: ConfirmDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onCancel}>
      <div
        className="bg-neutral-900 border border-neutral-700 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-sm font-semibold text-neutral-100 mb-2">{title}</h3>
        <p className="text-xs text-neutral-400 mb-5">{message}</p>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg text-xs font-medium bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            data-testid="confirm-dialog-confirm"
            className="px-4 py-2 rounded-lg text-xs font-medium bg-amber-500/20 hover:bg-amber-500/30 text-amber-400 border border-amber-500/40 transition-colors"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
