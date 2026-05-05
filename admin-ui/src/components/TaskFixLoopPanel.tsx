import { useState, useEffect, useCallback, useRef, type ReactElement } from 'react';
import { apiFetch } from '../api';
import type { FixLoopAttempt } from '../types/task';

interface LoopSummary {
  attempts: number;
  last_exit_reason: string | null;
}

interface FixLoopHistoryResponse {
  task_id: string;
  fix_loop_history: Record<string, FixLoopAttempt[]>;
  summary: Record<string, LoopSummary>;
}

const LOOP_LABELS: Record<string, string> = { lint: 'Lint', test: 'Test', check: 'Check' };
const LOOP_ORDER = ['lint', 'test', 'check'];

const EXIT_REASON_STYLE: Record<string, string> = {
  passed: 'text-emerald-400',
  early_unchanged: 'text-yellow-400',
  max_attempts: 'text-red-400',
  divergence_detected: 'text-orange-400',
  runaway_detected: 'text-red-500',
  in_progress: 'text-blue-400',
};

function exitReasonBadge(reason: string | null | undefined): ReactElement | null {
  if (!reason) return null;
  const cls = EXIT_REASON_STYLE[reason] ?? 'text-neutral-400';
  return <span className={`text-[10px] font-medium ${cls}`}>{reason.replace(/_/g, ' ')}</span>;
}

function AttemptRow({ attempt, index }: { attempt: FixLoopAttempt; index: number }) {
  const isSynth = attempt.source === 'trace_synthesized';
  const isTerminal = attempt.exit_reason && attempt.exit_reason !== 'in_progress';

  return (
    <div className={`flex items-start gap-2 text-xs py-1.5 border-b border-neutral-800/50 last:border-0 ${isSynth ? 'opacity-70' : ''}`}>
      <div className="flex flex-col items-center shrink-0 w-6 gap-0.5 pt-0.5">
        <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0 ${
          attempt.exit_reason === 'passed' ? 'bg-emerald-600/30 text-emerald-300' :
          isTerminal ? 'bg-red-600/30 text-red-300' :
          'bg-neutral-700 text-neutral-400'
        }`}>
          {index + 1}
        </div>
        {isSynth && (
          <span className="text-[8px] text-purple-500 leading-none">synth</span>
        )}
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          {exitReasonBadge(attempt.exit_reason)}
          {attempt.fingerprint_hash && (
            <span className="font-mono text-[10px] text-neutral-600">{attempt.fingerprint_hash.slice(0, 12)}</span>
          )}
          {attempt.change_summary && (
            <span className="text-[10px] text-neutral-500">{attempt.change_summary}</span>
          )}
        </div>
        {attempt.error_tail && (
          <pre className="mt-0.5 text-[10px] text-neutral-400 font-mono whitespace-pre-wrap break-words leading-snug line-clamp-3">
            {attempt.error_tail.slice(0, 300)}
          </pre>
        )}
      </div>
    </div>
  );
}

function LoopSection({ name, attempts, summary }: { name: string; attempts: FixLoopAttempt[]; summary: LoopSummary }) {
  const [expanded, setExpanded] = useState(false);
  const label = LOOP_LABELS[name] ?? name;
  const { last_exit_reason } = summary;
  const converged = last_exit_reason === 'passed';
  const dot = converged ? 'bg-emerald-500' : last_exit_reason ? 'bg-red-500' : 'bg-neutral-600';

  return (
    <div>
      <button
        className="w-full flex items-center gap-2 py-1.5 text-left hover:bg-neutral-800/30 rounded px-1 -mx-1"
        onClick={() => setExpanded(e => !e)}
      >
        <div className={`w-2 h-2 rounded-full shrink-0 ${dot}`} />
        <span className="text-xs font-medium text-neutral-200">{label}</span>
        <span className="text-xs text-neutral-500">{summary.attempts} iter</span>
        {last_exit_reason && exitReasonBadge(last_exit_reason)}
        <span className="ml-auto text-neutral-600 text-[10px]">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && attempts.length > 0 && (
        <div className="ml-4 mt-1 bg-neutral-900/50 rounded-lg border border-neutral-800 p-2">
          {attempts.map((a, i) => (
            <AttemptRow key={i} attempt={a} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

export function TaskFixLoopPanel({ taskId }: { taskId: string }) {
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

  const activeLoops = LOOP_ORDER.filter(l => (data.summary[l]?.attempts ?? 0) > 0);
  if (activeLoops.length === 0) return null;

  return (
    <>
      <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5">Fix Loops</h3>
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
