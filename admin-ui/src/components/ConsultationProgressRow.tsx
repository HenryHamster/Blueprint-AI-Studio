import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import {
  consultationIsLive,
  consultationStateLabel,
  fetchConsultationDetail,
  type ConsultationDetail,
  type ConsultationIndexRow,
  type ConsultationState,
  type ConsultationStep,
} from '../consultationDetailApi';
import { timeAgo } from '../utils/time';

/**
 * The collapsed progress line for a Resident-invoked Workday sub-session.
 *
 * The consultation packet itself stays out of the operator transcript — that
 * audience boundary is deliberate and unchanged. What was missing was any way
 * to see *through* the handoff, so a sub-session read as a middle-man swallowing
 * the question. This is one line while it runs and the whole record on request.
 */

export interface ConsultationProgressRowProps {
  projectId: string;
  /** The public reply's message id, which is also the consultation id. */
  consultationId: string;
  row: ConsultationIndexRow | null;
}

const DOT_CLASS: Record<ConsultationState, string> = {
  requested: 'bg-neutral-500',
  queued: 'bg-neutral-500',
  running: 'bg-amber-400 animate-pulse',
  stalled: 'bg-red-400',
  delivering: 'bg-amber-400 animate-pulse',
  delivered: 'bg-emerald-500',
  closed_without_result: 'bg-red-400',
};

function dotClass(row: ConsultationIndexRow): string {
  if (row.state === 'delivered' && row.outcome && row.outcome !== 'completed') {
    return 'bg-red-400';
  }
  return DOT_CLASS[row.state] ?? 'bg-neutral-500';
}

/** One ledger entry as a sentence, from whichever identity fields it carries. */
function describeStep(step: ConsultationStep): string {
  const entity = step.entity_kind && step.entity_id
    ? `${step.entity_kind} ${step.entity_id.slice(0, 8)}`
    : step.entity_kind ?? '';
  const target = step.target_kind
    ? `${step.target_kind}${step.target_revision != null ? ` r${step.target_revision}` : ''}`
    : '';
  return [step.op, entity || target, step.effect].filter(Boolean).join(' · ');
}

function StepList({ steps, truncated }: { steps: ConsultationStep[]; truncated: boolean }) {
  if (steps.length === 0) {
    return <p className="text-ui-md text-neutral-600">No operations recorded yet.</p>;
  }
  return (
    <ol className="space-y-0.5">
      {steps.map((step, i) => (
        <li key={`${step.op}-${i}`} className="flex gap-1.5 text-ui-md text-neutral-400">
          <span className="shrink-0 font-mono text-neutral-600">{i + 1}.</span>
          <span className="min-w-0 break-words">{describeStep(step)}</span>
        </li>
      ))}
      {truncated && (
        <li className="text-ui-md text-neutral-600">
          Older operations are not carried — this is the most recent window.
        </li>
      )}
    </ol>
  );
}

function DetailBody({ detail }: { detail: ConsultationDetail }) {
  const claim = detail.claim;
  return (
    <div className="mt-1.5 space-y-2 border-l-2 border-neutral-700/50 pl-2">
      {detail.question && (
        <div>
          <p className="text-ui-md font-medium text-neutral-500">Asked</p>
          <p className="whitespace-pre-wrap break-words text-ui-md text-neutral-400">
            {detail.question.text}
            {detail.question.truncated && (
              <span className="text-neutral-600"> … (bounded for display)</span>
            )}
          </p>
        </div>
      )}
      {claim && (
        <p className="text-ui-md text-neutral-500">
          {claim.claimed_by_task_id
            ? <>Held by task <span className="font-mono">{claim.claimed_by_task_id.slice(0, 8)}</span></>
            : 'Held by an unnamed worker'}
          {claim.attempt != null && <> · attempt {claim.attempt}</>}
          {claim.expires_at && (
            <> · lease {claim.expired ? 'expired' : 'expires'} {timeAgo(claim.expires_at)}</>
          )}
        </p>
      )}
      <div>
        <p className="text-ui-md font-medium text-neutral-500">Did</p>
        <StepList steps={detail.steps} truncated={detail.steps_truncated} />
      </div>
      {detail.last_failure && (
        <p className="flex gap-1.5 text-ui-md text-amber-400/90">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span className="min-w-0 break-words">
            {detail.last_failure.gate ?? 'rejected'}
            {detail.last_failure.detail ? `: ${detail.last_failure.detail}` : ''}
          </span>
        </p>
      )}
      {detail.result && (
        <div>
          <p className="text-ui-md font-medium text-neutral-500">Returned</p>
          <p className="whitespace-pre-wrap break-words text-ui-md text-neutral-400">
            {detail.result.summary.text}
            {detail.result.summary.truncated && (
              <span className="text-neutral-600"> … (bounded for display)</span>
            )}
          </p>
          {detail.result.receipts.length > 0 && (
            <div className="mt-1">
              <StepList
                steps={detail.result.receipts}
                truncated={detail.result.receipts_have_more}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ConsultationProgressRow({
  projectId,
  consultationId,
  row,
}: ConsultationProgressRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<ConsultationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toggle = useCallback(() => setExpanded(open => !open), []);

  const state = row?.state;
  const stepCount = row?.step_count;
  // Lazy by design: the detail read costs a request, so it only happens once
  // somebody opens the row. It re-runs when the collapsed row advances, so an
  // open panel keeps telling the truth without a second timer.
  useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      try {
        const next = await fetchConsultationDetail(projectId, consultationId, controller.signal);
        if (cancelled) return;
        setDetail(next);
        setError(null);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : 'Failed to load consultation');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [expanded, projectId, consultationId, state, stepCount]);

  if (!row) return null;
  const Chevron = expanded ? ChevronDown : ChevronRight;
  const live = consultationIsLive(row.state);

  return (
    <div
      data-testid="consultation-progress-row"
      data-consultation-state={row.state}
      className="mt-1 w-full min-w-0 rounded border border-neutral-800/60 bg-workbench-base/40 px-2 pb-1.5"
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        className="flex min-h-[44px] w-full min-w-0 items-center gap-1.5 text-left workbench-focus"
      >
        <Chevron className="h-3.5 w-3.5 shrink-0 text-neutral-600" />
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotClass(row)}`} aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-ui-md text-neutral-400">
          {consultationStateLabel(row.state, row.outcome)}
        </span>
        <span className="shrink-0 text-ui-md text-neutral-600">
          {row.step_count > 0 && <>{row.step_count} step{row.step_count === 1 ? '' : 's'} · </>}
          {timeAgo(row.delivered_at ?? row.requested_at ?? new Date())}
        </span>
      </button>
      {!expanded && live && row.question_preview && (
        <p className="mt-0.5 truncate pl-5 text-ui-md text-neutral-600">
          {row.question_preview.text}
        </p>
      )}
      {expanded && (
        <div className="pl-5">
          {loading && !detail && (
            <p className="mt-1.5 flex items-center gap-1.5 text-ui-md text-neutral-600">
              <Loader2 className="h-3 w-3 animate-spin" /> Loading the record…
            </p>
          )}
          {error && <p className="mt-1.5 text-ui-md text-red-400">{error}</p>}
          {detail && <DetailBody detail={detail} />}
        </div>
      )}
    </div>
  );
}
