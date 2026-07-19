import { useId, useRef, useState } from 'react';

export type ToolResidentHandoffIntent = 'share' | 'ask';

interface ToolResidentHandoffControlsProps {
  toolLabel: string;
  disabledReason?: string | null;
  preparing?: boolean;
  onHandoff: (intent: ToolResidentHandoffIntent, laneOwner?: string) => Promise<void>;
}

export function ToolResidentHandoffControls({
  toolLabel,
  disabledReason,
  preparing = false,
  onHandoff,
}: ToolResidentHandoffControlsProps) {
  const [laneOwner, setLaneOwner] = useState('');
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
    setStatus(`${intent === 'ask' ? 'Attaching for Resident review' : 'Attaching for Resident context'}…`);
    try {
      await onHandoff(intent, laneOwner.trim() || undefined);
      setStatus(`${toolLabel} revision queued in your Resident message draft. Review and send it to share.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `Could not share ${toolLabel}`);
      setStatus('');
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  };

  return (
    <div className="rounded border border-sky-500/25 bg-sky-950/15 p-2" data-viewport-allow-small-target>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={laneOwner}
          onChange={event => setLaneOwner(event.target.value)}
          placeholder="Lane ownership (optional)"
          aria-label={`${toolLabel} lane ownership`}
          className="min-h-[44px] min-w-[12rem] flex-1 rounded border border-neutral-700 bg-neutral-950/60 px-2 text-xs text-neutral-200 placeholder:text-neutral-600"
        />
        <button
          type="button"
          disabled={Boolean(disabledReason || busy || preparing)}
          aria-describedby={disabledReason ? disabledReasonId : statusId}
          onClick={() => void run('share')}
          className="min-h-[44px] rounded border border-sky-500/40 px-3 text-xs text-sky-200 hover:bg-sky-500/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === 'share' || preparing ? 'Preparing…' : 'Share with Resident'}
        </button>
        <button
          type="button"
          disabled={Boolean(disabledReason || busy || preparing)}
          aria-describedby={disabledReason ? disabledReasonId : statusId}
          onClick={() => void run('ask')}
          className="min-h-[44px] rounded border border-violet-500/40 px-3 text-xs text-violet-200 hover:bg-violet-500/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === 'ask' || preparing ? 'Preparing…' : 'Ask Resident'}
        </button>
      </div>
      {disabledReason && <p id={disabledReasonId} className="mt-1 text-xs text-amber-300">{disabledReason}</p>}
      {preparing && (
        <p className="mt-1 text-xs text-sky-300" role="status" aria-live="polite">
          Preparing {toolLabel} for the Resident…
        </p>
      )}
      <p id={statusId} className="sr-only" role="status" aria-live="polite">{status}</p>
      {error && <p className="mt-1 text-xs text-red-300" role="alert">{error}</p>}
    </div>
  );
}
