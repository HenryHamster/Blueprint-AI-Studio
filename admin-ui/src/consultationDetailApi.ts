import { apiFetch } from './api';

/**
 * Read path for private Workday consultations.
 *
 * The consultation packet and its result never enter the operator transcript —
 * that audience boundary is deliberate. These two reads are what makes a
 * Resident-invoked sub-session legible anyway: a collapsed progress line the
 * conversation can render inline, and an explicit expand for the detail.
 */

export type ConsultationState =
  | 'requested'
  | 'queued'
  | 'running'
  | 'stalled'
  | 'delivering'
  | 'delivered'
  | 'closed_without_result';

export type ConsultationOutcome = 'completed' | 'failed' | 'abandoned';

export interface ConsultationBoundedText {
  text: string;
  truncated: boolean;
  chars: number;
}

/** One durable operation the worker recorded while it ran. */
export interface ConsultationStep {
  op: string;
  entity_kind?: string;
  entity_id?: string;
  effect?: string;
  target_kind?: string;
  target_revision?: string | number;
  accepted_at?: string;
  recorded_at?: string;
}

export interface ConsultationClaim {
  claimed_by_task_id: string | null;
  claim_id: string | null;
  incarnation: string | null;
  generation: number | null;
  attempt: number | null;
  expires_at: string | null;
  expired: boolean;
}

export type ConsultationTraceKind = 'text' | 'tool' | 'tool_result' | 'error' | 'result';

export interface ConsultationTraceEvent {
  elapsed_ms: number;
  kind: ConsultationTraceKind;
  label?: string;
  is_error?: boolean;
}

/** One immutable terminal attempt, including bounded provider diagnostics. */
export interface ConsultationAttempt {
  attempt: number;
  workday_claim_id: string;
  outcome: ConsultationOutcome;
  terminal_predicate: string;
  provider_terminal_reason: string | null;
  provider_output_tail: string | null;
  started_at: string;
  ended_at: string;
  turn_ms: number;
  ttft_ms: number | null;
  tool_call_count: number;
  tool_call_duration_ms: number;
  result_chars: number;
  trace: ConsultationTraceEvent[];
  trace_truncated: boolean;
}

export interface ConsultationIndexRow {
  consultation_id: string;
  state: ConsultationState;
  outcome: ConsultationOutcome | null;
  requested_at: string | null;
  question_preview: ConsultationBoundedText | null;
  step_count: number;
  steps_truncated: boolean;
  claim_expired: boolean;
  delivered_at: string | null;
}

export interface ConsultationIndexResponse {
  consultations: ConsultationIndexRow[];
  limit: number;
}

export interface ConsultationDetail {
  consultation_id: string;
  state: ConsultationState;
  outcome: ConsultationOutcome | null;
  requested_at: string | null;
  continuation_key: string | null;
  question: ConsultationBoundedText | null;
  packet: {
    id: string;
    created_at: string | null;
    updated_at: string | null;
    handling_state: string;
  } | null;
  claim: ConsultationClaim | null;
  steps: ConsultationStep[];
  steps_truncated: boolean;
  attempts: ConsultationAttempt[];
  last_failure: {
    gate: string | null;
    detail: string | null;
    recorded_at: string | null;
  } | null;
  result: {
    id: string;
    created_at: string | null;
    summary: ConsultationBoundedText;
    receipts: ConsultationStep[];
    receipts_have_more: boolean;
  } | null;
  delivered_at: string | null;
}

/** States where the sub-session is still expected to move on its own. */
const LIVE_STATES: ReadonlySet<ConsultationState> = new Set([
  'requested',
  'queued',
  'running',
  'delivering',
]);

export function consultationIsLive(state: ConsultationState): boolean {
  return LIVE_STATES.has(state);
}

/**
 * What the collapsed line says. `stalled` is called out by name because it is
 * the one state nobody is acting on — reading it as "working" is exactly the
 * middle-man opacity this surface exists to remove.
 */
export function consultationStateLabel(
  state: ConsultationState,
  outcome: ConsultationOutcome | null,
): string {
  switch (state) {
    case 'requested':
      return 'Handing off to Workday';
    case 'queued':
      return 'Queued for Workday';
    case 'running':
      return 'Workday investigating';
    case 'stalled':
      return 'Workday lease expired — waiting to be reclaimed';
    case 'delivering':
      return outcome === 'failed' || outcome === 'abandoned'
        ? 'Workday could not finish — returning to the Resident'
        : 'Findings ready — returning to the Resident';
    case 'delivered':
      return outcome === 'failed' || outcome === 'abandoned'
        ? 'Workday could not finish'
        : 'Workday finished';
    case 'closed_without_result':
      return 'Workday closed this without a result';
    default:
      return 'Workday consultation';
  }
}

export async function fetchProjectConsultations(
  projectId: string,
  signal?: AbortSignal,
  limit?: number,
): Promise<ConsultationIndexResponse> {
  const query = limit ? `?limit=${encodeURIComponent(String(limit))}` : '';
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}`
      + `/strategic-session/consultations${query}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch consultations: ${res.status}`);
  return res.json() as Promise<ConsultationIndexResponse>;
}

export async function fetchConsultationDetail(
  projectId: string,
  consultationId: string,
  signal?: AbortSignal,
): Promise<ConsultationDetail> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}`
      + `/strategic-session/consultations/${encodeURIComponent(consultationId)}`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch consultation detail: ${res.status}`);
  return res.json() as Promise<ConsultationDetail>;
}
