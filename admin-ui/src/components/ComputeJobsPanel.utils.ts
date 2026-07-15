import type { ComputeJob, ComputeRpcDiagnostic } from '../api';

/** Evidence tier for a compute job. Prefers the canonical `evidence_tier` served
 *  first-class by the backend (knowledge_completion.py::_compute_evidence_tier),
 *  and falls back to the client derivation below only for older/cached payloads
 *  that omit the field — keeping a single source of truth on fresh responses
 *  while remaining backward compatible.
 *
 *  Fallback semantics mirror the backend: `covered` renders as "Machine
 *  verified", so it requires BOTH that the job ran on operator hardware AND that
 *  it produced a measurable result. A local run that reported no metric proves
 *  only WHERE it ran — labelling that "Machine verified" is a claimed, not
 *  earned, status. It is demoted to `partial`: real execution provenance,
 *  nothing verified. */
export function computeJobEvidenceTier(
  job: Pick<
    ComputeJob,
    'backend' | 'status' | 'outcome' | 'criterion_verdict' | 'per_arm_verdicts'
  > & { evidence_tier?: ComputeJob['evidence_tier'] },
): 'covered' | 'attested' | 'backfilled' | 'partial' | 'uncovered' {
  const served = job.evidence_tier;
  if (
    served === 'covered' ||
    served === 'attested' ||
    served === 'backfilled' ||
    served === 'partial' ||
    served === 'uncovered'
  ) {
    return served;
  }
  const backend = job.backend ?? '';
  const ranOnOperatorHardware =
    backend.startsWith('local-') || backend === 'runner-subprocess';
  const producedResult =
    (job.per_arm_verdicts != null && Object.keys(job.per_arm_verdicts).length > 0) ||
    job.criterion_verdict != null;
  if (ranOnOperatorHardware) return producedResult ? 'covered' : 'partial';
  if (backend) return 'attested';
  if (producedResult) return 'backfilled';
  return 'uncovered';
}
/** Digest-pin status of the container image a compute job ran on, derived from
 *  the resolved `launch_params.container_image` string.
 *
 *  - `null`    — no image declared (bare `bash -lc` job); nothing to pin.
 *  - `pinned`  — content-addressed reference (`registry/repo@sha256:...`); the
 *    same layers pull every time, so runs are bit-identical over time.
 *  - `mutable` — a plain tag (`registry/repo:latest`); layers can drift between
 *    runs, silently defeating A/B reproducibility. */
export function computeJobImagePinStatus(
  job: Pick<ComputeJob, 'launch_params'>,
): 'pinned' | 'mutable' | null {
  const image = job.launch_params?.container_image;
  if (!image) return null;
  return image.includes('@sha256:') ? 'pinned' : 'mutable';
}
import { parseTimestamp } from '../utils/time';
export { formatDurationSec as fmtDuration } from '../utils/time';

const ACTIVE_STATUSES: ReadonlySet<ComputeJob['status']> = new Set([
  'QUEUED',
  'STARTING',
  'RUNNING',
  'POLLING',
]);

export function isComputeJobActive(job: ComputeJob): boolean {
  return ACTIVE_STATUSES.has(job.status);
}

/** Coarse trust grouping for a terminal job's evidence tier — the two buckets
 *  operators triage on. `machine-verified` is the single highest-trust tier
 *  ('covered' — executed AND verified on operator hardware); `low-trust` is
 *  every other terminal tier (attested / backfilled / partial /
 *  uncovered), i.e. anything NOT machine-verified. Active jobs have no tier yet,
 *  so they return `null` and are excluded from any tier narrowing. Reuses
 *  {@link computeJobEvidenceTier} — does not re-derive tier semantics. */
export function computeJobEvidenceGroup(
  job: ComputeJob,
): 'machine-verified' | 'low-trust' | null {
  if (isComputeJobActive(job)) return null;
  return computeJobEvidenceTier(job) === 'covered' ? 'machine-verified' : 'low-trust';
}

/** Deterministic sort rank for a job's evidence tier, ordered from highest-trust
 *  ('covered' = 0) down to lowest-trust ('uncovered'). Jobs with no tier (active
 *  jobs, which carry no evidence tier) sort consistently last. Reuses
 *  {@link computeJobEvidenceTier} for the tier value — does not re-derive it. */
const EVIDENCE_TIER_RANK: Record<string, number> = {
  covered: 0,
  attested: 1,
  backfilled: 2,
  partial: 3,
  uncovered: 4,
};

export function computeJobEvidenceTierRank(job: ComputeJob): number {
  if (isComputeJobActive(job)) return Number.MAX_SAFE_INTEGER;
  const tier = computeJobEvidenceTier(job);
  return EVIDENCE_TIER_RANK[tier] ?? Number.MAX_SAFE_INTEGER;
}

/** Human-readable terminal reason for a finished job, distinguishing the three
 *  kill paths (operator / budget / deadline / no-progress) plus natural success/failure. */
export function computeJobTerminalLabel(job: ComputeJob): string {
  const legacyRunnerNoProgress =
    job.outcome === 'FAILURE'
    && job.backend === 'runner-subprocess'
    && ((job.terminal_reason || '').toLowerCase().includes('no_progress_timeout')
      || job.exit_code === 125);
  if (legacyRunnerNoProgress) return 'Killed: no progress';

  switch (job.outcome) {
    case 'SPEND_CAP_EXCEEDED':
      return 'Killed: budget cap';
    case 'TIMED_OUT':
      return job.terminal_kind === 'no_progress' ? 'Killed: no progress' : 'Killed: deadline';
    case 'CANCELLED':
      return (job.terminal_reason || '').toLowerCase().includes('operator')
        ? 'Killed by operator'
        : 'Cancelled';
    case 'SUCCESS':
      return 'Succeeded';
    case 'FAILURE':
      return 'Failed';
    default:
      return job.status;
  }
}

export function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `$${v.toFixed(2)}`;
}

/** Remaining time until the wall-clock deadline, as a compact countdown. */
export function fmtCountdown(deadlineIso: string | null, nowMs: number): string {
  if (!deadlineIso) return '—';
  const t = parseTimestamp(deadlineIso).getTime();
  if (Number.isNaN(t)) return '—';
  const remaining = Math.round((t - nowMs) / 1000);
  if (remaining <= 0) return 'expired';
  if (remaining < 60) return `${remaining}s`;
  if (remaining < 3600) return `${Math.floor(remaining / 60)}m ${remaining % 60}s`;
  return `${Math.floor(remaining / 3600)}h ${Math.floor((remaining % 3600) / 60)}m`;
}

/** Backend RPC failure diagnostic for a job, reading the dedicated field and
 *  falling back to the legacy `polling_metadata.compute_rpc_diagnostic` shape so
 *  older API responses still surface the warning. Returns null when absent. */
export function computeJobRpcDiagnostic(job: ComputeJob): ComputeRpcDiagnostic | null {
  const direct = job.compute_rpc_diagnostic;
  if (direct && typeof direct === 'object') return direct;
  const legacy = (job.polling_metadata as Record<string, unknown> | undefined)?.[
    'compute_rpc_diagnostic'
  ];
  if (legacy && typeof legacy === 'object') return legacy as ComputeRpcDiagnostic;
  return null;
}

/** Spend used so far for a job — accrued while active, final once terminal. */
export function computeJobSpent(job: ComputeJob): number {
  if (isComputeJobActive(job)) return job.accrued_cost_usd ?? 0;
  return job.final_cost_usd ?? job.accrued_cost_usd ?? 0;
}

/** Actual wall-clock runtime in seconds for a job that has both timestamps.
 *  Returns null when either timestamp is absent or unparseable. */
export function computeJobRuntimeSec(job: ComputeJob): number | null {
  if (!job.started_at || !job.completed_at) return null;
  const start = parseTimestamp(job.started_at).getTime();
  const end = parseTimestamp(job.completed_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return null;
  return Math.round((end - start) / 1000);
}

/** Burn rate in $/hr for an active job. Returns null when elapsed time is too
 *  short (<60s) or data is missing. */
export function computeJobBurnRate(
  job: ComputeJob,
  nowMs: number,
): number | null {
  if (!job.started_at) return null;
  const accrued = job.accrued_cost_usd;
  if (accrued == null || accrued <= 0) return null;
  const startMs = parseTimestamp(job.started_at).getTime();
  if (Number.isNaN(startMs)) return null;
  const elapsedSec = (nowMs - startMs) / 1000;
  if (elapsedSec < 60) return null;
  return (accrued / elapsedSec) * 3600;
}

/** Projected total spend at the wall-clock deadline, given the current burn
 *  rate. Returns null when burn rate or deadline is unavailable. */
export function computeJobProjectedSpend(
  job: ComputeJob,
  burnRatePerHr: number | null,
  nowMs: number,
): number | null {
  if (burnRatePerHr == null || !job.wall_clock_deadline) return null;
  const deadlineMs = parseTimestamp(job.wall_clock_deadline).getTime();
  if (Number.isNaN(deadlineMs)) return null;
  const remainingSec = Math.max(0, (deadlineMs - nowMs) / 1000);
  const accrued = job.accrued_cost_usd ?? 0;
  return accrued + burnRatePerHr * (remainingSec / 3600);
}

/** Map a job backend string to a human-readable execution location label and
 *  a zero-cost flag. Unknown backends fall back to the raw backend string. */
export function computeJobExecutionLocation(backend: string): {
  label: string;
  isOperatorHardware: boolean;
} {
  switch (backend) {
    case 'local-subprocess':
      return { label: 'Operator hardware', isOperatorHardware: true };
    case 'local-gpu':
      return { label: 'Operator hardware · GPU', isOperatorHardware: true };
    case 'runner-subprocess':
      return { label: 'Operator hardware · Runner', isOperatorHardware: true };
    case 'gcp':
      return { label: 'GCP cloud', isOperatorHardware: false };
    default:
      return { label: backend, isOperatorHardware: false };
  }
}

/** One gallery-renderable entry normalized from the job's first-class
 *  `viewable_artifacts` manifest field. */
export interface GalleryViewableArtifact {
  name: string;
  storage_uri: string;
  kind: 'image' | 'video' | 'pdf';
  bytes: number;
}

/** Defensive parse of the job's first-class `viewable_artifacts` field — the
 *  backend's normalized manifest ({filename, storage_uri, content_type,
 *  size_bytes, kind}) of preview-able files persisted from the job's
 *  artifacts. Entries missing a `storage_uri` or with an unknown `kind` are
 *  dropped so a malformed manifest degrades to an empty gallery instead of a
 *  render crash. */
export function computeJobViewableArtifacts(job: ComputeJob): GalleryViewableArtifact[] {
  const raw: unknown = job.viewable_artifacts;
  if (!Array.isArray(raw)) return [];
  const items: GalleryViewableArtifact[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const e = entry as Record<string, unknown>;
    if (typeof e.storage_uri !== 'string' || e.storage_uri.length === 0) continue;
    if (e.kind !== 'image' && e.kind !== 'video' && e.kind !== 'pdf') continue;
    const fallbackName = e.storage_uri.split('/').pop() || 'artifact';
    items.push({
      name: typeof e.filename === 'string' && e.filename.length > 0 ? e.filename : fallbackName,
      storage_uri: e.storage_uri,
      kind: e.kind,
      bytes: typeof e.size_bytes === 'number' && Number.isFinite(e.size_bytes) ? e.size_bytes : 0,
    });
  }
  return items;
}

/** Compact byte-count label for artifact captions. Empty string when unknown. */
export function fmtBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Calculate the margin (distance from threshold to actual value).
 *  Positive margin means the result beat the threshold; negative means it missed.
 *  For >= / > operators: margin = value - threshold
 *  For <= / < operators: margin = threshold - value
 *  Returns null when value or threshold is missing. */
export function computeJobMargin(job: ComputeJob): number | null {
  const verdict = job.criterion_verdict;
  if (!verdict || verdict.value === null || verdict.threshold === null) return null;
  const op = verdict.operator;
  if (op === '>=' || op === '>') {
    return verdict.value - verdict.threshold;
  }
  if (op === '<=' || op === '<') {
    return verdict.threshold - verdict.value;
  }
  return null;
}
