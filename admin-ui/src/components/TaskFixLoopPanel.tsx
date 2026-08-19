import { useState, useEffect, useCallback, useRef, type ReactElement } from 'react';
import { apiFetch } from '../api';
import type { FixLoopAttempt } from '../types/task';
import { FIX_LOOP_LAYERS, LOOP_LABELS } from '../utils/fixLoopLayers';
import { parseTimestamp } from '../utils/time';

interface LoopSummary {
  attempts: number;
  last_exit_reason: string | null;
}

interface FixLoopHistoryResponse {
  task_id: string;
  fix_loop_history: Record<string, FixLoopAttempt[]>;
  summary: Record<string, LoopSummary>;
}

const EXIT_REASON_STYLE: Record<string, string> = {
  passed: 'text-emerald-400',
  early_unchanged: 'text-yellow-400',
  max_attempts: 'text-red-400',
  divergence_detected: 'text-orange-400',
  runaway_detected: 'text-red-500',
  in_progress: 'text-blue-400',
};

function formatDuration(startedAt: string | null | undefined, endedAt: string | null | undefined): string | null {
  if (!startedAt || !endedAt) return null;
  const start = parseTimestamp(startedAt).getTime();
  const end = parseTimestamp(endedAt).getTime();
  if (!start || !end || start >= end) return null;
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return secs === 0 ? `${minutes}m` : `${minutes}m ${secs}s`;
}

function formatAbsoluteTime(isoString: string | null | undefined): string | null {
  if (!isoString) return null;
  const date = parseTimestamp(isoString);
  if (isNaN(date.getTime())) return null;
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true });
}

function exitReasonBadge(reason: string | null | undefined): ReactElement | null {
  if (!reason) return null;
  const cls = EXIT_REASON_STYLE[reason] ?? 'text-neutral-400';
  return <span className={`text-ui-sm font-medium ${cls}`}>{reason.replace(/_/g, ' ')}</span>;
}

function generateLoopOutcome(
  loopName: string,
  attempts: FixLoopAttempt[],
  summary: LoopSummary,
): string | null {
  const label = LOOP_LABELS[loopName] || loopName;
  const { attempts: attemptCount, last_exit_reason: exitReason } = summary;

  if (!exitReason || exitReason === 'in_progress') {
    return null;
  }

  if (exitReason === 'passed') {
    const plural = attemptCount === 1 ? 'iteration' : 'iterations';
    return `${label} converged green after ${attemptCount} ${plural}.`;
  }

  if (exitReason === 'early_unchanged') {
    return `${label} stopped early with no changes.`;
  }

  if (exitReason === 'max_attempts') {
    const plural = attemptCount === 1 ? 'iteration' : 'iterations';
    return `${label} exhausted ${attemptCount} ${plural} without converging.`;
  }

  if (exitReason === 'runaway_detected') {
    return `${label} killed by runaway watchdog after ${attemptCount} invocations.`;
  }

  if (exitReason.startsWith('divergence_')) {
    const type = exitReason.replace('divergence_', '');
    const typeText =
      type === 'oscillation' ? 'oscillation between two fixes' :
      type === 'similar_error' ? 'similar errors repeating' :
      type === 'diff_revert' ? 'changes reverting each iteration' :
      'pattern detected';

    const divergenceAttempt = attempts.find(a => a.exit_reason?.startsWith('divergence_'));
    const divergenceIteration = divergenceAttempt?.iteration || attemptCount;

    return `${label} loop diverged at iteration ${divergenceIteration} — ${typeText}.`;
  }

  return `${label} stopped (${exitReason.replace(/_/g, ' ')}).`;
}

function AttemptRow({ attempt, index }: { attempt: FixLoopAttempt; index: number }) {
  const isSynth = attempt.source === 'trace_synthesized';
  const isTerminal = attempt.exit_reason && attempt.exit_reason !== 'in_progress';
  const duration = formatDuration(attempt.started_at, attempt.ended_at);
  const startTime = formatAbsoluteTime(attempt.started_at);
  const endTime = formatAbsoluteTime(attempt.ended_at);
  const tooltipText = startTime && endTime ? `${startTime} → ${endTime}` : null;

  return (
    <div className={`flex items-start gap-2 text-xs py-1.5 border-b border-neutral-800/50 last:border-0 ${isSynth ? 'opacity-70' : ''}`}>
      <div className="flex flex-col items-center shrink-0 w-6 gap-0.5 pt-0.5">
        <div className={`w-5 h-5 rounded-full flex items-center justify-center text-ui-sm font-bold shrink-0 ${
          attempt.exit_reason === 'passed' ? 'bg-emerald-600/30 text-emerald-300' :
          isTerminal ? 'bg-red-600/30 text-red-300' :
          'bg-workbench-layer text-neutral-400'
        }`}>
          {index + 1}
        </div>
        {isSynth && (
          <span className="text-ui-2xs text-purple-500 leading-none">synth</span>
        )}
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          {exitReasonBadge(attempt.exit_reason)}
          {attempt.fingerprint_hash && (
            <span className="font-mono text-ui-sm text-neutral-600">{attempt.fingerprint_hash.slice(0, 12)}</span>
          )}
          {attempt.change_summary && (
            <span className="text-ui-sm text-neutral-500">{attempt.change_summary}</span>
          )}
        </div>
        {attempt.error_tail && (
          <pre className="mt-0.5 text-ui-sm text-neutral-400 font-mono whitespace-pre-wrap break-words leading-snug line-clamp-3">
            {attempt.error_tail.slice(0, 300)}
          </pre>
        )}
      </div>

      {duration && (
        <div className="shrink-0 text-right" title={tooltipText ?? undefined}>
          <span className="font-mono text-ui-sm text-neutral-500">{duration}</span>
        </div>
      )}
    </div>
  );
}

function LoopSection({ name, attempts, summary }: { name: string; attempts: FixLoopAttempt[]; summary: LoopSummary }) {
  const [expanded, setExpanded] = useState(false);
  const label = LOOP_LABELS[name] ?? name;
  const { last_exit_reason } = summary;
  const converged = last_exit_reason === 'passed';
  const dot = converged ? 'bg-emerald-500' : last_exit_reason ? 'bg-red-500' : 'bg-workbench-card';
  const outcome = generateLoopOutcome(name, attempts, summary);

  return (
    <div>
      {outcome && (
        <div className="text-xs text-neutral-400 mb-1 pl-1">
          {outcome}
        </div>
      )}
      {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
      <button
        className="w-full flex items-center gap-2 py-1.5 text-left hover:bg-workbench-hover/30 rounded px-1 -mx-1"
        onClick={() => setExpanded(e => !e)}
      >
        <div className={`w-2 h-2 rounded-full shrink-0 ${dot}`} />
        <span className="text-xs font-medium text-neutral-200">{label}</span>
        <span className="text-xs text-neutral-500">{summary.attempts} iter</span>
        {last_exit_reason && exitReasonBadge(last_exit_reason)}
        <span className="ml-auto text-neutral-600 text-ui-sm">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && attempts.length > 0 && (
        <div className="ml-4 mt-1 bg-workbench-base/50 rounded-lg border border-neutral-800 p-2">
          {attempts.map((a, i) => (
            <AttemptRow key={i} attempt={a} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

export function TaskFixLoopPanel({
  taskId,
  fixLoopAbandoned,
}: {
  taskId: string;
  fixLoopAbandoned?: { loop: string; exit_reason: string; at: string } | null;
}) {
  const [data, setData] = useState<FixLoopHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(true);

  const fetchData = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/admin/tasks/${taskId}/fix-loop-history`);
      if (!mountedRef.current) return;
      if (!res.ok) { setLoading(false); return; }
      const body = (await res.json()) as FixLoopHistoryResponse;
      if (!mountedRef.current) return;
      setData(body);
      setLoading(false);
    } catch {
      if (mountedRef.current) setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    mountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchData();
    return () => { mountedRef.current = false; };
  }, [fetchData]);

  if (loading || !data) return null;

  const activeLoops = FIX_LOOP_LAYERS.filter(l => (data.summary[l]?.attempts ?? 0) > 0);
  if (activeLoops.length === 0) return null;

  return (
    <>
      <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5">Fix Loops</h3>
      {fixLoopAbandoned && (
        <div className="mb-2 px-2 py-1.5 rounded bg-orange-950/40 border border-orange-800/50 text-xs text-orange-300">
          Retries suppressed — fix loop diverged ({fixLoopAbandoned.exit_reason.replace(/_/g, ' ')} in {fixLoopAbandoned.loop} loop)
        </div>
      )}
      <div className="space-y-1">
        {activeLoops.map(loop => (
          <LoopSection
            key={loop}
            name={loop}
            attempts={data.fix_loop_history[loop] ?? []}
            summary={data.summary[loop]}
          />
        ))}
      </div>
    </>
  );
}
