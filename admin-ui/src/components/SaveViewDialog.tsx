import { X } from 'lucide-react';

interface SaveViewDialogProps {
  open: boolean;
  mode: 'create' | 'rename';
  name: string;
  description: string;
  error: string | null;
  summaryText?: string;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onCommit: () => void;
  onClose: () => void;
}

export function SaveViewDialog({ open, mode, name, description, error, summaryText, onNameChange, onDescriptionChange, onCommit, onClose }: SaveViewDialogProps) {
  if (!open) return null;
  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} />
      <div
        role="dialog"
        aria-label={mode === 'rename' ? 'Rename saved view' : 'Save current filters as a view'}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(420px,90vw)] bg-workbench-base border border-neutral-700 rounded-lg shadow-2xl p-4 space-y-3"
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-neutral-100">
            {mode === 'rename' ? 'Rename view' : 'Save view'}
          </h3>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 p-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center">
            <X className="w-4 h-4" />
          </button>
        </div>
        <label className="block text-xs text-neutral-400">
          Name
          <input
            type="text"
            value={name}
            onChange={e => { onNameChange(e.target.value); }}
            onKeyDown={e => { if (e.key === 'Enter') onCommit(); if (e.key === 'Escape') onClose(); }}
            placeholder="e.g. Unresolved BLOCKERs"
            className="mt-1 w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs placeholder-neutral-600 workbench-focus"
            autoFocus
          />
        </label>
        <label className="block text-xs text-neutral-400">
          Description (optional)
          <input
            type="text"
            value={description}
            onChange={e => onDescriptionChange(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') onCommit(); if (e.key === 'Escape') onClose(); }}
            placeholder="What is this view for?"
            className="mt-1 w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs placeholder-neutral-600 workbench-focus"
          />
        </label>
        {mode === 'create' && summaryText && (
          <div className="text-ui-md text-neutral-500 bg-workbench-shell/50 border border-neutral-800/60 rounded-md px-3 py-2">
            <span className="text-neutral-400">Saving:</span>{' '}
            {summaryText}
          </div>
        )}
        {error && (
          <div role="alert" className="text-ui-md text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
            {error}
          </div>
        )}
        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded-lg text-neutral-400 hover:text-neutral-200 border border-neutral-800 hover:border-neutral-700 transition-colors min-h-[36px]"
          >
            Cancel
          </button>
          <button
            onClick={onCommit}
            className="text-xs px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white transition-colors min-h-[36px]"
          >
            {mode === 'rename' ? 'Save' : 'Save view'}
          </button>
        </div>
      </div>
    </>
  );
}
