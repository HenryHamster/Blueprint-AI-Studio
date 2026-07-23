import { useId, useRef, useState } from 'react';

export type ToolResidentHandoffIntent = 'share' | 'ask';

interface ToolResidentHandoffControlsProps {
  toolLabel: string;
  disabledReason?: string | null;
  preparing?: boolean;
  onHandoff: (intent: ToolResidentHandoffIntent) => Promise<void>;
}

export function ToolResidentHandoffControls({
  toolLabel,
  disabledReason,
  preparing = false,
  onHandoff,
}: ToolResidentHandoffControlsProps) {
  const [busy, setBusy] = useState<ToolResidentHandoffIntent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const busyRef = useRef(false);
  const disabledReasonId = useId();
  const statusId = useId();

  const run = async (intent: ToolResidentHandoffIntent) => {
    if (busyRef.current || busy || preparing || disabledReason) return;
    busyRef.current = true;
    setBusy(intent);
    setError(null);
    setStatus(`${intent === 'ask' ? 'Attaching for Workday review' : 'Attaching for Workday context'}…`);
    try {
      await onHandoff(intent);
      setStatus(`${toolLabel} revision durably queued for your next conversation draft. Open Conversation, review, and send it to share.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `Could not share ${toolLabel}`);
      setStatus('');
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  };

  return (
    <div className="rounded border border-sky-500/25 bg-sky-950/15 p-2">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={Boolean(disabledReason || busy || preparing)}
          aria-describedby={disabledReason ? disabledReasonId : statusId}
          onClick={() => void run('share')}
          className="min-h-[44px] rounded border border-sky-500/40 px-3 text-xs text-sky-200 hover:bg-sky-500/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === 'share' || preparing ? 'Preparing…' : 'Share in conversation'}
        </button>
        <button
          type="button"
          disabled={Boolean(disabledReason || busy || preparing)}
          aria-describedby={disabledReason ? disabledReasonId : statusId}
          onClick={() => void run('ask')}
          className="min-h-[44px] rounded border border-violet-500/40 px-3 text-xs text-violet-200 hover:bg-violet-500/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === 'ask' || preparing ? 'Preparing…' : 'Ask in conversation'}
        </button>
      </div>
      {disabledReason && <p id={disabledReasonId} className="mt-1 text-xs text-amber-300">{disabledReason}</p>}
      {preparing && <p className="mt-1 text-xs text-sky-300" role="status" aria-live="polite">Preparing {toolLabel} for your conversation…</p>}
      <p
        id={statusId}
        className={status ? 'mt-1 text-xs text-emerald-300' : 'sr-only'}
        role="status"
        aria-live="polite"
      >{status}</p>
      {error && <p className="mt-1 text-xs text-red-300" role="alert">{error}</p>}
    </div>
  );
}
