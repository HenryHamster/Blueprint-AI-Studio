import { useState } from 'react';
import { ModalShell } from './ui/ModalShell';

interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel?: string;
  attestationInput?: { label: string; placeholder?: string };
  onConfirm: (reason?: string) => void;
  onCancel: () => void;
}

export function ConfirmDialog({ title, message, confirmLabel = 'Confirm', attestationInput, onConfirm, onCancel }: ConfirmDialogProps) {
  const [inputVal, setInputVal] = useState('');
  const canConfirm = !attestationInput || inputVal.trim().length > 0;

  return (
    <ModalShell onClose={onCancel}>
      <h3 className="text-sm font-semibold text-neutral-100 mb-2">{title}</h3>
      <p className="text-xs text-neutral-400 mb-5 whitespace-pre-line">{message}</p>
      {attestationInput && (
        <div className="mb-4">
          <label className="block text-xs text-neutral-400 mb-1">{attestationInput.label}</label>
          <input
            type="text"
            value={inputVal}
            onChange={(e) => setInputVal(e.target.value)}
            placeholder={attestationInput.placeholder}
            className="w-full text-xs bg-neutral-800 border border-neutral-700 text-neutral-300 rounded px-2 py-1.5 focus:outline-none focus:border-neutral-600"
            data-testid="confirm-dialog-attestation-input"
          />
        </div>
      )}
      <div className="flex gap-2 justify-end">
        <button
          onClick={onCancel}
          data-testid="confirm-dialog-cancel"
          className="min-h-11 min-w-11 px-4 py-2 rounded-lg text-xs font-medium bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={() => onConfirm(attestationInput ? inputVal.trim() : undefined)}
          data-testid="confirm-dialog-confirm"
          disabled={!canConfirm}
          className="min-h-11 min-w-11 px-4 py-2 rounded-lg text-xs font-medium bg-amber-500/20 hover:bg-amber-500/30 text-amber-400 border border-amber-500/40 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {confirmLabel}
        </button>
      </div>
    </ModalShell>
  );
}
