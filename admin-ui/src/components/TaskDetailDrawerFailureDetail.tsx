import { FAILURE_CLASS_CONFIG } from './TaskCard';
import type { Task } from './TaskCard';
import { parseTimestamp } from '../utils/time';
import { TIMEOUT_SUBTYPE_LABELS, EXTERNAL_API_SUBTYPE_LABELS, RATE_LIMIT_KIND_LABELS, PARK_WAKE_FAILURE_DETAIL_LABELS } from './catchupHelpers';

/** Selects the persisted failure sub-type value + friendly label map for a failure class.
 *  Returns null when the class has no sub-type concept or the context field is absent. */
function failureSubtype(
  failureClass: string,
  context: Task['context'],
): { value: string; label: string } | null {
  const ctx = (context ?? {}) as Record<string, unknown>;
  let raw: unknown;
  let labels: Record<string, string>;
  if (failureClass === 'AGENT_TIMEOUT') {
    raw = ctx.timeout_subtype;
    labels = TIMEOUT_SUBTYPE_LABELS;
  } else if (failureClass === 'EXTERNAL_API') {
    raw = ctx.external_api_subtype;
    labels = EXTERNAL_API_SUBTYPE_LABELS;
  } else if (failureClass === 'RATE_LIMIT') {
    raw = ctx.rate_limit_kind;
    labels = RATE_LIMIT_KIND_LABELS;
  } else if (failureClass === 'park_wake_timeout') {
    const detail = ctx.failure_detail as string | undefined;
    if (!detail) return null;
    if (detail === 'compute_job_terminal_job_not_terminal_by_deadline') {
      // Refine sub-cause from provisioning-vs-runtime diagnostics when available.
      const diag = ctx.park_wake_timeout_diagnostics as Record<string, unknown> | undefined;
      if (diag && 'job_started_at' in diag) {
        if (diag.job_started_at === null) {
          return { value: detail, label: 'provisioning starved run' };
        }
        return { value: detail, label: 'ran out of time' };
      }
    }
    return { value: detail, label: PARK_WAKE_FAILURE_DETAIL_LABELS[detail] ?? detail };
  } else {
    return null;
  }
  if (typeof raw !== 'string' || !raw) return null;
  return { value: raw, label: labels[raw] ?? raw };
}

function fmtSeconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.round(s / 60)}m`;
}

function ParkWakeTimeoutDiagnosticsPanel({ context }: { context: Task['context'] }) {
  const ctx = (context ?? {}) as Record<string, unknown>;
  const diag = ctx.park_wake_timeout_diagnostics as Record<string, unknown> | undefined;
  if (!diag) return null;

  const jobStartedAt = diag.job_started_at as string | null | undefined;
  const provSecs = diag.provisioning_seconds as number | null | undefined;
  const execSecs = diag.execution_seconds as number | null | undefined;
  const jobStatus = diag.job_status as string | undefined;
  const detail = ctx.failure_detail as string | undefined;

  const isWakeLost = detail === 'compute_job_terminal_wake_lost_after_job_completed';
  const isNotTerminal = detail === 'compute_job_terminal_job_not_terminal_by_deadline';
  const jobEverStarted = jobStartedAt !== undefined && jobStartedAt !== null;

  let suggestion: string | null = null;
  if (isWakeLost) {
    suggestion = 'The GPU job completed but its wake signal was never delivered. Investigate park/wake delivery health before retrying.';
  } else if (isNotTerminal && !jobEverStarted && provSecs != null) {
    suggestion = `The job never reached running state — ${fmtSeconds(provSecs)} was spent waiting for provisioning. Retry with a larger deadline or check GPU availability.`;
  } else if (isNotTerminal && jobEverStarted) {
    suggestion = 'The job started running but did not complete within the deadline. Retry with a larger deadline or optimize the experiment.';
  }

  return (
    <div className="mt-2 pt-2 border-t border-neutral-800">
      <div className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">Park/Wake Diagnostics</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs">
        {jobStatus && (
          <>
            <span className="text-neutral-500">Job status</span>
            <span className="text-neutral-300 font-mono">{jobStatus}</span>
          </>
        )}
        <span className="text-neutral-500">Job started</span>
        <span className={jobEverStarted ? 'text-emerald-400' : 'text-amber-400'}>
          {jobEverStarted ? 'yes' : 'no (provisioning starved)'}
        </span>
        {provSecs != null && (
          <>
            <span className="text-neutral-500">Provisioning</span>
            <span className="text-neutral-300">{fmtSeconds(provSecs)}</span>
          </>
        )}
        {execSecs != null && (
          <>
            <span className="text-neutral-500">Execution</span>
            <span className="text-neutral-300">{fmtSeconds(execSecs)}</span>
          </>
        )}
      </div>
      {suggestion && (
        <p className="text-xs text-amber-300 mt-1.5">{suggestion}</p>
      )}
    </div>
  );
}

function renderRetryEta(retryAfter: string | undefined) {
  if (!retryAfter) return null;
  const retryTime = parseTimestamp(retryAfter).getTime();
  const ms = retryTime - Date.now();
  if (ms > 0) {
    const mins = Math.ceil(ms / 60000);
    return <p className="text-xs text-emerald-400">Auto-retrying in {mins}m</p>;
  }
  return <p className="text-xs text-amber-400">Auto-retry pending</p>;
}

export function TeardownForensicsPanel({ task }: { task: Task }) {
  const forensics = task.context?.teardown_forensics;
  if (!forensics?.serial_tail) return null;

  return (
    <div className="bg-workbench-base/50 rounded-lg border border-neutral-800 p-3 mb-2">
      <div className="flex items-center gap-2 mb-2">
        <span className="inline-block text-ui-sm px-1.5 py-0.5 rounded font-semibold bg-orange-900/60 text-orange-300 border border-orange-800">
          VM Teardown
        </span>
        <span className="text-xs text-neutral-400 truncate" title={forensics.reason}>{forensics.reason}</span>
      </div>
      <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-workbench-shell rounded p-2 max-h-64 overflow-y-auto custom-scrollbar border border-neutral-800 font-mono">
        {forensics.serial_tail}
      </pre>
    </div>
  );
}

export function FailureDetailPanel({ task }: { task: Task }) {
  if (!(task.status === 'FAILED' && task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class])) {
    return null;
  }

  return (
    <div className="bg-workbench-base/50 rounded-lg border border-neutral-800 p-3 mb-2">
      <div className="flex items-center gap-2 mb-1">
        <span className={`inline-block text-ui-sm px-1.5 py-0.5 rounded font-semibold ${FAILURE_CLASS_CONFIG[task.failure_class].color}`}>
          {FAILURE_CLASS_CONFIG[task.failure_class].label}
        </span>
        <span className="text-xs text-neutral-400">{FAILURE_CLASS_CONFIG[task.failure_class].suggestion}</span>
      </div>
      {(() => {
        const subtype = failureSubtype(task.failure_class, task.context);
        if (!subtype) return null;
        return (
          <div className="text-xs text-neutral-300 mb-1">
            <span className="text-neutral-500">Subtype:</span>{' '}
            <span className="font-medium" title={subtype.value}>{subtype.label}</span>
          </div>
        );
      })()}
      {task.failure_class === 'park_wake_timeout' && (
        <ParkWakeTimeoutDiagnosticsPanel context={task.context} />
      )}
      {renderRetryEta(task.context?.retry_after as string | undefined)}
    </div>
  );
}
