import { FAILURE_CLASS_CONFIG } from './TaskCard';
import type { Task } from './TaskCard';
import { parseTimestamp } from '../utils/time';
import { TIMEOUT_SUBTYPE_LABELS, EXTERNAL_API_SUBTYPE_LABELS, RATE_LIMIT_KIND_LABELS } from './catchupHelpers';

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
  } else {
    return null;
  }
  if (typeof raw !== 'string' || !raw) return null;
  return { value: raw, label: labels[raw] ?? raw };
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

export function FailureDetailPanel({ task }: { task: Task }) {
  if (!(task.status === 'FAILED' && task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class])) {
    return null;
  }

  return (
    <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 p-3 mb-2">
      <div className="flex items-center gap-2 mb-1">
        <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded font-semibold ${FAILURE_CLASS_CONFIG[task.failure_class].color}`}>
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
      {renderRetryEta(task.context?.retry_after as string | undefined)}
    </div>
  );
}
