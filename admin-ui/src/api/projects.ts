import { apiFetch } from './core';
import type { WarmPoolTargets, WarmPoolTargetSetResponse } from '../types/warmVm';
import type { ProblemModelPatch } from '../types';
import type {
  BuildConfig,
  MergeGateRuntime,
  MergeVerificationVerdict,
  ProjectResourceEnvelopeResponse,
  ResourceEnvelopeConfig,
} from '../types/project';

export async function getBlockDegradedUnverifiedMerge(
  projectId: string,
): Promise<{ block_degraded_unverified_merge: boolean; is_default: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-degraded-unverified-merge`,
  );
  if (!res.ok) throw new Error(`Failed to fetch block-degraded-unverified-merge: ${res.status}`);
  return res.json();
}

export async function setBlockDegradedUnverifiedMerge(
  projectId: string,
  enabled: boolean,
): Promise<{ block_degraded_unverified_merge: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-degraded-unverified-merge`,
    {
      method: 'PATCH',
      body: JSON.stringify({ block_degraded_unverified_merge: enabled }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update block-degraded-unverified-merge: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update block-degraded-unverified-merge: ${res.status}`);
  }
  return res.json();
}

export async function getBlockNoTestUnverifiedMerge(
  projectId: string,
): Promise<{ block_no_test_unverified_merge: boolean; is_default: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-no-test-unverified-merge`,
  );
  if (!res.ok) throw new Error(`Failed to fetch block-no-test-unverified-merge: ${res.status}`);
  return res.json();
}

export async function setBlockNoTestUnverifiedMerge(
  projectId: string,
  enabled: boolean,
): Promise<{ block_no_test_unverified_merge: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/block-no-test-unverified-merge`,
    {
      method: 'PATCH',
      body: JSON.stringify({ block_no_test_unverified_merge: enabled }),
    },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to update block-no-test-unverified-merge: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update block-no-test-unverified-merge: ${res.status}`);
  }
  return res.json();
}

export type ResourceEnvelopePatch = Partial<ResourceEnvelopeConfig>;

export async function fetchProjectResourceEnvelope(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectResourceEnvelopeResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/resource-envelope`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) {
    throw new Error(`Failed to fetch resource envelope: ${res.status}`);
  }
  return res.json();
}

export async function updateProjectResourceEnvelope(
  projectId: string,
  patch: ResourceEnvelopePatch,
): Promise<ProjectResourceEnvelopeResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/resource-envelope`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to save resource envelope: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to save resource envelope: ${res.status}`);
  }
  return res.json();
}


export async function onboardProject(
  owner: string,
  repo: string,
  name?: string,
  buildConfigOverride?: Record<string, unknown> | BuildConfig,
  promotionTarget?: string,
  goal?: string,
  initialSteering?: Pick<ProblemModelPatch, 'context' | 'constraints'>,
): Promise<{
  project_id: string;
  build_config: BuildConfig;
  default_branch?: string;
  /** Backend-computed merge-gate verdict for the freshly-onboarded repo (sibling of build_config). */
  merge_verification_verdict?: MergeVerificationVerdict;
  /** Optional live no-CI gate runtime evidence, present when onboarding response already carries it. */
  merge_gate_runtime?: MergeGateRuntime;
}> {
  const body: Record<string, unknown> = { github_owner: owner, github_repo: repo };
  if (name) body.name = name;
  if (buildConfigOverride) body.build_config_override = buildConfigOverride;
  if (promotionTarget) body.promotion_target = promotionTarget;
  if (goal) body.goal = goal;
  if (initialSteering) {
    const steering: Record<string, unknown> = {};
    const contextEntries = Object.entries(initialSteering.context ?? {})
      .map(([key, value]) => [key, typeof value === 'string' ? value.trim() : ''] as const)
      .filter(([, value]) => value.length > 0);
    if (contextEntries.length > 0) steering.context = Object.fromEntries(contextEntries);

    const constraints = initialSteering.constraints?.map((constraint) => constraint.trim()).filter((constraint) => constraint.length > 0);
    if (constraints && constraints.length > 0) steering.constraints = constraints;

    if (Object.keys(steering).length > 0) body.initial_steering = steering;
  }
  return submitOnboardProject(body);
}

export interface OnboardPreviewResponse {
  build_config: BuildConfig;
  default_branch?: string;
  merge_verification_verdict?: MergeVerificationVerdict;
}

export interface OnboardCreateResponse extends OnboardPreviewResponse {
  project_id: string;
}

async function submitOnboardProject(body: Record<string, unknown>): Promise<OnboardCreateResponse> {
  const res = await apiFetch('/api/admin/projects/onboard', { method: 'POST', body: JSON.stringify(body) });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Error ${res.status}`;
    throw new Error(msg);
  }
  return res.json();
}

export async function previewOnboardProject(
  owner: string,
  repo: string,
  name?: string,
  buildConfigOverride?: Record<string, unknown> | BuildConfig,
  promotionTarget?: string,
  goal?: string,
  initialSteering?: Pick<ProblemModelPatch, 'context' | 'constraints'>,
): Promise<OnboardPreviewResponse> {
  const body: Record<string, unknown> = { github_owner: owner, github_repo: repo, preview_only: true };
  if (name) body.name = name;
  if (buildConfigOverride) body.build_config_override = buildConfigOverride;
  if (promotionTarget) body.promotion_target = promotionTarget;
  if (goal) body.goal = goal;
  if (initialSteering) {
    const steering: Record<string, unknown> = {};
    const contextEntries = Object.entries(initialSteering.context ?? {})
      .map(([key, value]) => [key, typeof value === 'string' ? value.trim() : ''] as const)
      .filter(([, value]) => value.length > 0);
    if (contextEntries.length > 0) steering.context = Object.fromEntries(contextEntries);

    const constraints = initialSteering.constraints?.map((constraint) => constraint.trim()).filter((constraint) => constraint.length > 0);
    if (constraints && constraints.length > 0) steering.constraints = constraints;

    if (Object.keys(steering).length > 0) body.initial_steering = steering;
  }
  return submitOnboardProject(body);
}


export interface CiResolution {
  state: 'green' | 'red' | 'pending' | 'no_checks';
  resolved_sha: string | null;
  detail: string;
}

export interface VersionQualificationSubScores {
  audit: number;
  test_erosion: number;
  smoketest: number;
  ci_resolution?: CiResolution | null;
  debt_pressure?: number | null;
}

export interface CompletionDebtQualItem {
  id: string;
  kind: string;
  title: string;
  description: string;
  created_at: string | null;
  age_days: number | null;
}

export interface CompletionDebtQualSnapshot {
  items: CompletionDebtQualItem[];
  total_open: number;
}

export type QualificationStatus =
  | 'not_promoted'
  | 'dry_run'
  | 'cached_verdict'
  | 'promoted'
  | 'promotion_failed'
  | 'error'
  | 'sha_error'
  | 'infra_failure';

export interface AuditReconciliation {
  model_reported_score: number | null;
  findings_derived_score: number | null;
  score_delta: number | null;
  excluded_findings_count: number;
}

export interface GoalAlignment {
  assessed_grade?: number | null;
  state?: string | null;  // 'aligned' | 'shortfall' | 'not_applicable'
  biggest_gap?: string | null;
  reasoning_summary?: string | null;
}

export interface BlindMergeGrade {
  coverage_verdict?: string | null;  // 'covers' | 'partial' | 'misses'
  biggest_gap?: string | null;
}

export interface AuditFinding {
  severity: string;
  summary: string;
  // File attribution carried when the raw finding named one (additive; older
  // records predating stable-identity carry no file).
  file?: string;
  // Deterministic content-derived key (sha256→16-hex of normalized file|summary)
  // stable across re-extractions, so findings can be keyed in a resolution ledger.
  key?: string;
}

export interface ChallengerRefutedFinding {
  severity: string;
  summary: string;
  challenger_reason: string;
}

export interface LatestAudit {
  audit_task_id: string;
  task_status: string;
  candidate_sha: string | null;
  created_at: string | null;
  report: {
    findings?: AuditFinding[];
    audit_score?: number | null;
    test_erosion?: number | null;
  } | null;
}

export interface VersionQualification {
  status: QualificationStatus | null;
  confidence: number | null;
  threshold: number | null;
  sub_scores: VersionQualificationSubScores | null;
  ci_resolution: CiResolution | null;
  blocker: string | null;
  started_at?: string | null;
  finalized_at: string | null;
  // The candidate commit this qualification was evaluated against. Used to detect
  // when the latest audit's findings were computed against a different commit.
  candidate_sha?: string | null;
  // Infra-failure context (populated only when status=infra_failure).
  error?: string | null;
  consecutive_infra_errors?: number | null;
  // Audit observability: why the audit sub-score is missing/unscoreable.
  audit_missing_reason?: string | null;
  // Audit reconciliation: model vs findings-derived scores and excluded count.
  audit_reconciliation?: AuditReconciliation | null;
  // Audit findings carried from the qualification run (severity + summary per finding).
  audit_findings?: AuditFinding[] | null;
  excluded_audit_findings?: AuditFinding[] | null;
  // Pre-cap totals + truncation flags so the UI can tell whether the carried
  // findings lists were clipped at the carry cap (additive; absent on older records).
  audit_findings_total?: number | null;
  audit_findings_truncated?: boolean | null;
  excluded_audit_findings_total?: number | null;
  excluded_audit_findings_truncated?: boolean | null;
  // Challenger-confirmed false positives: findings refuted by an independent challenger (with reason).
  challenger_refuted_audit_findings?: ChallengerRefutedFinding[] | null;
  migration_safety?: string | null;
  migration_safety_detail?: string | null;
  migration_apply_safety?: string | null;
  migration_apply_safety_detail?: string | null;
  test_erosion_detail?: Record<string, unknown> | null;
  smoketest_detail?: Record<string, unknown> | null;
  // Whether the smoketest dimension was enabled for this qualification run.
  // False means the dimension was disabled and the 0.0 sub-score is a
  // zero-weighted placeholder, not an active lane. Null/absent on legacy records.
  smoketest_enabled?: boolean | null;
  promotion_pr_url?: string | null;
  promotion_conflict?: boolean | null;
  promotion_error_detail?: string | null;
  goal_alignment?: GoalAlignment | null;
  blind_merge_grade?: BlindMergeGrade | null;
  completion_debt?: CompletionDebtQualSnapshot | null;
  debt_enforce_confidence_preview?: number | null;
  debt_enforce_weight_preview?: number | null;
}

/** Single resolved serving block — the canonical answer to "is this version live?"
 *  Derived by the backend from the aggregate artifact evidence so every read site
 *  (UI panel, CLI) reads one field instead of re-implementing the three-field
 *  precedence chain (aggregate_serving_status ?? serving_status ?? qual_state.serving_status). */
export interface ResolvedServing {
  status?: string | null;
  sha?: string | null;
  confirmed_at?: string | null;
  failure_reason?: string | null;
  artifacts?: ReleaseArtifactEvidence[];
}

/** Per-artifact serving evidence for a release version. One entry per registered
 *  deploy artifact (e.g. the backend Cloud Run revision, plus any other required
 *  artifact). serving_status is one of 'unconfirmed' | 'serving_confirmed' |
 *  'deploy_failed' | null (null = no serving expectation, e.g. a never-promoted
 *  version). reason_code carries the failure/pending detail. */
export interface ReleaseArtifactEvidence {
  artifact_kind: string;
  required: boolean;
  serving_status?: string | null;
  served_identifier?: string | null;
  served_at?: string | null;
  reason_code?: string | null;
}

export interface ManagedCheckWaitSnapshotCheck {
  name: string;
  status?: string | null;
  conclusion?: string | null;
}

export interface ManagedCheckWaitSnapshot {
  state: string;
  classified_at: string;
  head_sha?: string | null;
  total_checks?: number | null;
  checks?: ManagedCheckWaitSnapshotCheck[];
  truncated?: boolean | null;
  reason?: string | null;
  detail?: string | null;
}

// The authoritative closed vocabulary of qualification states. This mirrors
// the backend's summarize_qualification_state (backend/app/release_version.py):
// every branch there returns exactly one of these state strings. Keeping this
// union in sync with that producer is what lets TypeScript catch a new backend
// state that lacks a QUAL_STATE_STYLE entry or a stale literal comparison in
// the lifecycle panels — the drift that let the 'qualifying' wait-guard
// regression ship silently.
export type QualificationStateValue =
  | 'qualifying'
  | 'audit_crashed'
  | 'promotion_check_wait'
  | 'promotion_in_flight'
  | 'promotion_failed'
  | 'required_check_failed'
  | 'protection_blocked_review'
  | 'not_promoted'
  | 'dry_run'
  | 'promoted'
  | 'promoted_unconfirmed'
  | 'promotion_deploy_failed'
  | 'promotion_confirmation_timed_out'
  | 'deferred_unsynced'
  | 'error'
  | 'infra_failure';

export interface QualificationState {
  state: QualificationStateValue;
  summary: string;
  promotion_pr_number?: number | null;
  promotion_pr_url?: string | null;
  // Additive promotion freshness context for the shared "promotion in flight"
  // state. When absent, callers must fall back to the timeless PR-only copy.
  promotion_enabled_at?: string | null;
  promotion_age_seconds?: number | null;
  promotion_timeout_seconds?: number | null;
  promotion_timeout_at?: string | null;
  promotion_past_timeout?: boolean | null;
  promotion_nearing_timeout?: boolean | null;
  error?: string | null;
  // Failing-check names / branch-protection detail surfaced for the
  // required_check_failed and protection_blocked_review states.
  detail?: string | null;
  // Managed promotion check-wait context: a migration-carrying promotion PR is
  // open and Blueprint is waiting for required checks before retrying the merge
  // (distinct from native auto-merge 'promotion_in_flight'). These are additive
  // and absent for native auto-merge.
  migration_carrying?: boolean | null;
  check_snapshot?: ManagedCheckWaitSnapshot | null;
  ci_state?: string | null;
  blocker?: string | null;
  // Post-promotion serving confirmation. A 'promoted' state is provisional:
  // serving_status is 'unconfirmed' while the deploy is still within its
  // confirmation window, 'serving_confirmed' once the promoted SHA is live, or
  // 'deploy_failed' (state becomes 'promotion_deploy_failed') when the deploy
  // never stabilized. deploy_failure_reason carries the failure detail.
  serving_status?: string | null;
  deploy_failure_reason?: string | null;
}

export interface OutOfBandPromotion {
  source: string;
  recorded_at: string;
  candidate_sha: string;
  promoted_sha: string;
}

export interface ChangelogEntry {
  task_type: string | null;
  title: string;
  pr_number: number | null;
  pr_url?: string | null;
}

export interface CriterionCoverageEntry {
  criterion_key: string;
  text: string;
  coverage: 'covered' | 'attested' | 'backfilled' | 'probed' | 'partial' | 'uncovered';
  status?: 'active' | 'retracted' | 'superseded';
  evidence_count: number;
  stuck_reason?: 'no_mapping' | 'direction_retired' | null;
  /** ISO-8601 UTC timestamp when the operator attestation expires.
   *  Present only when coverage='attested' and the attestation carries a
   *  parseable attested_at timestamp. Null/absent for legacy (non-expiring)
   *  attestations and for all non-attested coverage tiers. */
  attestation_expires_at?: string | null;
}

export interface VersionCriterionCoverage {
  // Active criteria only; inactive criteria may still appear in criteria for audit.
  total: number;
  covered: number;
  /** Active criteria covered ONLY by a non-expired operator attestation — a
   *  distinct, lower-confidence tier that is NOT included in `covered`. May be
   *  absent from older backends; treat undefined as 0. */
  attested?: number;
  /** Active criteria covered by backfilled evidence (merge-before-CI, no
   *  machine-verified artifact). Distinct from attested — backfilled is
   *  derived from task history, not operator attestation. May be absent from
   *  older backends; treat undefined as 0. */
  backfilled?: number;
  /** Active criteria covered ONLY by a non-expired probe (lower-confidence,
   *  time-bounded machine signal — an expired attestation or stale probe row).
   *  A distinct tier that is NOT included in `covered`, `attested`, or
   *  `backfilled`. May be absent from older backends; treat undefined as 0. */
  probed?: number;
  partial: number;
  uncovered: number;
  retracted?: number;
  superseded?: number;
  inactive?: number;
  stuck_count?: number;
  /** Per-reason merge-evidence gap accumulator.
   *  Records counts per reason_key (no_criteria, verdict_not_proven, no_active_criteria,
   *  no_open_version) and an optional list of criterion IDs that found no active row
   *  (unmatched_criterion_ids). Null/undefined until the first gap is recorded. */
  merge_evidence_gaps?: Record<string, number | string[]> | null;
  criteria: CriterionCoverageEntry[];
}

/** Per-dimension qualification state derived from last_qualification.
 *  Surfaced in current_version.dimension_states and recent_versions entries. */
export interface DimensionState {
  /** Stable identifier: audit | test_erosion | smoketest | ci | migration_identity
   *  | migration_apply | merge_grade | criterion_coverage | debt_pressure | goal_alignment */
  key: string;
  /** Human-readable name for display. */
  label: string;
  /** "passed" | "failed" | "unavailable" | "not_applicable" | "pending" */
  status: string;
  /** Numeric score in [0,1] when the dimension produces one; null otherwise. */
  score: number | null;
  /** Short reason string (blocker code or infra error). */
  reason: string | null;
}

export interface CurrentVersion {
  version_number: number;
  phase: string;
  phase_changed_at: string | null;
  phase_reasoning: string | null;
  goal: string | null;
  goal_updated_at?: string | null;
  goal_editor_info?: string | null;
  live_current_focus?: string | null;
  opened_at: string;
  qualification_attempts: number;
  merged_task_count: number;
  /** Human-readable what's-new headline + at-a-glance metrics (merged / proven /
   *  conf-thr) for the current version, mirroring the per-version summary
   *  endpoint's highlights. The 'serving' word in the headline derives from the
   *  aggregate required-artifact serving truth, not the scalar serving_status.
   *  Optional/additive — absent on older backends. */
  highlights?: VersionSummaryHighlights | null;
  /** Tasks merged at/after the last qualification verdict's finalized_at — a
   *  verdict-staleness signal. >0 means work landed since the verdict was
   *  computed, so the displayed confidence no longer reflects the current tree.
   *  null when there is no verdict or no parseable finalized_at. */
  merged_since_verdict?: number | null;
  /** Non-terminal tasks still in flight against this version. A version cut does
   *  not stop them — they continue and land against the freshly-opened version. */
  in_flight_task_count?: number;
  qualification_state?: QualificationState | null;
  last_qualification: VersionQualification | null;
  // The candidate commit currently under evaluation for this open version
  // (mirror of last_qualification.candidate_sha / the resolver's evaluated SHA).
  // Surfaced so the UI can show what is being qualified and, when audit evidence
  // is stale, contrast it against the historical audited candidate.
  active_candidate_sha?: string | null;
  latest_audit?: LatestAudit | null;
  // Version-scoped audit evidence for the open candidate. `latest_audit` is
  // current only when audit_evidence_state='current'; `stale_audit` carries
  // historical provenance when the newest scoped audit belongs to an older
  // candidate, and 'none' means this candidate has no scoped audit evidence yet.
  audit_evidence_state?: 'current' | 'stale' | 'none';
  stale_audit?: LatestAudit | null;
  // Post-promotion serving confirmation (additive). serving_status is one of
  // 'unconfirmed' | 'serving_confirmed' | 'deploy_failed'; served_sha/served_at
  // record the confirmed-serving commit, and deploy_failure_reason the detail
  // when the deploy never stabilized.
  serving_status?: string | null;
  served_sha?: string | null;
  served_at?: string | null;
  deploy_failure_reason?: string | null;
  // Aggregate required-artifact serving truth (additive). The headline
  // 'is it actually shipped' decision derives from this, NOT the backend-only
  // scalar: 'serving_confirmed' only when every required artifact confirms,
  // 'deploy_failed' when any required artifact failed, 'unconfirmed' while any
  // required artifact is still pending. `artifacts` is the per-artifact evidence
  // list (one entry per registered deploy artifact). Both may be absent when the
  // API has no artifact registry — fall back to the scalar fields above.
  aggregate_serving_status?: string | null;
  aggregate_deploy_failure_reason?: string | null;
  artifacts?: ReleaseArtifactEvidence[];
  /** Single resolved serving block — prefer this over the three peer fields.
   *  Absent on older backends (optional for zero-downtime mixed deploys). */
  serving?: ResolvedServing | null;
  // Post-promotion deploy-confirmation window deadline (ISO string). The sweep
  // resolves serving_status to confirmed/failed when this elapses; the panel
  // computes remaining/overdue at render time.
  deploy_confirm_deadline?: string | null;
  // Out-of-band promotion provenance. Non-null only when an operator force-merged
  // this version outside the normal qualification flow.
  out_of_band_promotion?: OutOfBandPromotion | null;
  /** Per-task changelog for this version window: type, title, and PR number for
   *  each merged task, newest-first. Capped at 50 newest; tasks_merged_has_more
   *  is true when the version has additional older tasks beyond the limit. */
  tasks_merged?: ChangelogEntry[];
  /** True when the version has more merged tasks than the 50 returned. */
  tasks_merged_has_more?: boolean;
  /** Acceptance-criteria bucket counts for all merged tasks in this version. */
  acceptance_aggregate?: Record<string, number> | null;
  /** Version-scoped criterion coverage derived from the canonical acceptance contract. */
  criterion_coverage?: VersionCriterionCoverage | null;
  /** Event-driven qualification trigger explain-surface (additive; nullable).
   *  Answers 'why hasn't qualification fired / what will fire it next'. */
  qualification_trigger_state?: QualificationTriggerState | null;
  /** Per-dimension qualification state. Always present when a qualification has
   *  run; empty array when no qualification data is available. Additive field —
   *  older backends omit it. */
  dimension_states?: DimensionState[];
}

/** Auto-trigger mode for the qualification trigger resolver. */
export type QualificationTriggerAutoMode =
  | 'armed'
  | 'waiting'
  | 'in_flight'
  | 'capped'
  | 'settled'
  | 'observe'
  | 'off';

/** Output of explain_qualification_trigger — surfaced on the lifecycle endpoint
 *  and strategic payload to answer 'why hasn't qualification fired'. */
export interface QualificationTriggerState {
  auto_trigger: QualificationTriggerAutoMode;
  last_attempt: Record<string, unknown> | null;
  next_expected_trigger: string;
  blocking: string[];
  attempts: { count: number; cap: number };
  knob: 'on' | 'observe' | 'off';
  evaluated_at: string;
  decision?: {
    fire: boolean;
    trigger: string;
    reason: string;
    blocking: string[];
  };
}

export interface RecentVersion {
  id?: string;
  version_number: number;
  status: string;
  phase?: string | null;
  opened_at: string;
  promoted_at: string | null;
  qualification_attempts: number;
  last_qualification: VersionQualification | null;
  abandoned_at?: string | null;
  abandoned_by?: string | null;
  abandon_reason?: string | null;
  // Post-promotion serving confirmation (additive) — see CurrentVersion.
  qualification_state?: QualificationState | null;
  serving_status?: string | null;
  served_sha?: string | null;
  served_at?: string | null;
  deploy_failure_reason?: string | null;
  // Aggregate required-artifact serving truth (additive) — see CurrentVersion.
  aggregate_serving_status?: string | null;
  aggregate_deploy_failure_reason?: string | null;
  artifacts?: ReleaseArtifactEvidence[];
  /** Single resolved serving block — prefer this over the three peer fields.
   *  Absent on older backends (optional for zero-downtime mixed deploys). */
  serving?: ResolvedServing | null;
  // Post-promotion deploy-confirmation window deadline (ISO string) — see
  // CurrentVersion. Present on the recently-promoted history row whose
  // serving_status is still 'unconfirmed'.
  deploy_confirm_deadline?: string | null;
  // Out-of-band promotion provenance — see CurrentVersion.
  out_of_band_promotion?: OutOfBandPromotion | null;
  // Server-computed highlights (headline + metrics) — additive; null on older backends.
  highlights?: VersionSummaryHighlights | null;
  // Merged-task count for this version's window. Both keys present for consistency.
  merged_count?: number | null;
  merged_task_count?: number | null;
  acceptance_aggregate?: Record<string, number> | null;
  /** Version-scoped criterion coverage derived from the canonical acceptance contract. */
  criterion_coverage?: VersionCriterionCoverage | null;
  /** Per-dimension qualification state — additive, same shape as CurrentVersion. */
  dimension_states?: DimensionState[];
}

export interface PromotionReadinessConfidence {
  mean: number;
  stdev: number;
  min: number;
  max: number;
  stable: boolean;
}

export interface PromotionReadiness {
  ready: boolean;
  reason: string;
  verdicts_evaluated: number;
  would_promote_count: number;
  would_promote_rate: number;
  consecutive_clean_passes: number;
  confidence: PromotionReadinessConfidence;
  recurring_blockers: Record<string, number>;
  blocker_counts: Record<string, number>;
  thresholds: Record<string, number>;
  // True when ground truth is validated (sufficient resolved-outcome sample exists);
  // false otherwise. When true, `validated_precision`, `serving_confirmed_count`,
  // `deploy_failed_count`, and `resolved_outcome_sample` are populated.
  ground_truth_validated?: boolean;
  caveat?: string;
  // Acceptance caveat when merged work is satisfied-but-not-machine-proven;
  // omitted when no caveat (all work proven or no unproven work present).
  acceptance_caveat?: string | null;
  // The readiness window only measures DRY-RUN-regime verdicts, so it reflects
  // *current* readiness only pre-go-live. Once promotion is effectively enabled
  // the window is frozen pre-go-live history: regime='live', is_current_readiness=false.
  regime?: 'pre_go_live_calibration' | 'live';
  is_current_readiness?: boolean;
  promotion_enabled?: boolean;
  // Ground-truth-validated qualifier precision (when validated_precision is non-null).
  validated_precision?: number | null;
  // Number of promotion outcomes resolved (served or failed) used to derive validated_precision.
  resolved_outcome_sample?: number;
  // Number of promoted verdicts that were confirmed serving (contributing to validated_precision).
  serving_confirmed_count?: number;
  // Number of promoted verdicts whose deploy failed without stabilizing (contributing to validated_precision).
  deploy_failed_count?: number;
}

export type PromotionPolicySource = 'runtime_config' | 'yaml_file' | 'hardcoded_defaults';

export interface StagedDirection {
  id: string | null;
  text: string | null;
  priority: string | null;
  staged_for_version: number | null;
  staged_at: string | null;
  staged_age_days: number | null;
}

export interface ChallengedVsUnchallengedCohort {
  sample_size: number;
  insufficient_sample: boolean;
  exclusion_rate?: number;
  exclusion_count?: number;
  disagreement_rate?: number;
  disagreement_count?: number;
  wrong_decision_rate?: number | null;
  wrong_decision_resolved_sample?: number;
  /** Count of findings the adversarial challenger explicitly refuted as false positives (challenged cohort only). */
  challenge_refuted_count?: number;
  /** Rate of challenger-refuted false positives per verdict in the challenged cohort. Distinct from exclusion_rate. */
  challenge_refuted_rate?: number;
}

export interface ChallengedVsUnchallengedTelemetry {
  challenged: ChallengedVsUnchallengedCohort;
  unchallenged: ChallengedVsUnchallengedCohort;
  total_verdicts: number;
}

export interface VersionLifecycleResponse {
  current_version: CurrentVersion | null;
  recent_versions: RecentVersion[];
  promotion_enabled?: boolean;
  /** The promotion gate value actually consulted for this project (override when set, else fleet default). */
  promotion_enabled_effective?: boolean;
  /** The explicit per-project override, or null when the project rides the fleet default. */
  promotion_enabled_override?: boolean | null;
  /** Which value is in effect: a per-project override or the fleet-wide default. */
  promotion_enabled_source?: 'project' | 'fleet';
  promotion_readiness?: PromotionReadiness | null;
  promotion_policy_source?: PromotionPolicySource;
  promotion_policy_error?: string | null;
  staged_directions?: StagedDirection[];
  /** Challenged-vs-unchallenged decision-quality cohort comparison. Advisory/display only. */
  challenged_vs_unchallenged?: ChallengedVsUnchallengedTelemetry | null;
}

export interface AgentServingShaBucket {
  source_sha: string | null;
  count: number;
  warm_pool_count: number;
  task_vm_count: number;
  image_names: string[];
  latest_build_ts: number | null;
  last_heartbeat_at: string | null;
  matches_backend_served?: boolean;
  matches_active_candidate?: boolean;
  matches_integration_head?: boolean;
  matches_current_image?: boolean;
}

export interface AgentServingShaFleet {
  total_observed: number;
  unknown_count: number;
  mixed: boolean;
  buckets: AgentServingShaBucket[];
}

export interface AgentCurrentImage {
  image_name: string | null;
  source_git_sha: string | null;
  build_ts: number | null;
}

export interface AgentServingShasResponse {
  fleet: AgentServingShaFleet;
  current_image: AgentCurrentImage | null;
  backend_served_sha: string | null;
  active_candidate_sha: string | null;
  integration_branch: string | null;
  integration_head_sha: string | null;
  promotion_target: string | null;
  promotion_head_sha: string | null;
}

export async function fetchAgentServingShas(
  projectId: string,
): Promise<AgentServingShasResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/agent-serving-shas`);
  if (!res.ok) throw new Error(`Failed to fetch agent serving SHAs: ${res.status}`);
  return res.json();
}

export interface SetPromotionEnabledResponse {
  project_id: string;
  override: boolean | null;
  effective: boolean;
  source: 'project' | 'fleet';
  cleared: boolean;
}

/** Arm (true), hold (false), or clear (null) this project's per-project live-promotion override. */
export async function setProjectPromotionEnabled(
  projectId: string,
  enabled: boolean | null,
): Promise<SetPromotionEnabledResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/promotion-enabled`, {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`Failed to set promotion enablement: ${res.status}`);
  return res.json();
}

export interface SelfAuditResponse {
  task_id: string;
  project_id: string;
  status: string;
}

export async function triggerSelfAudit(projectId: string): Promise<SelfAuditResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/self-audit`, {
    method: 'POST',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to trigger self-audit: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to trigger self-audit: ${res.status}`);
  }
  return res.json();
}

export async function fetchVersionLifecycle(projectId: string): Promise<VersionLifecycleResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-lifecycle`);
  if (!res.ok) throw new Error(`Failed to fetch version lifecycle: ${res.status}`);
  return res.json();
}

export interface VersionSummaryHighlights {
  headline: string;
  merged_count: number;
  proven: number;
  total_merged_for_acceptance: number;
  qualification_confidence: number | null;
  qualification_threshold: number | null;
  narrative?: string | null;
}

export interface VersionSummaryResponse {
  version_number: number;
  phase: string;
  status: string;
  goal: string | null;
  opened_at: string | null;
  promoted_at: string | null;
  served_at: string | null;
  phase_changed_at: string | null;
  merged_count: number;
  acceptance_aggregate: Record<string, number>;
  qualification_state: QualificationState | null;
  serving_status: string | null;
  served_sha: string | null;
  deploy_failure_reason: string | null;
  highlights: VersionSummaryHighlights;
  tasks_merged?: ChangelogEntry[];
}

export async function fetchVersionSummary(versionId: string): Promise<VersionSummaryResponse> {
  const res = await apiFetch(`/api/admin/release-versions/${encodeURIComponent(versionId)}/summary`);
  if (!res.ok) throw new Error(`Failed to fetch version summary: ${res.status}`);
  return res.json();
}

export interface SetVersionPhaseResponse {
  version_number: number;
  phase: string;
  phase_changed_at: string | null;
  phase_reasoning: string | null;
}

export async function setVersionPhase(
  projectId: string,
  phase: 'build' | 'stabilize',
  reasoning?: string,
): Promise<SetVersionPhaseResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-phase`, {
    method: 'POST',
    body: JSON.stringify({ phase, reasoning: reasoning ?? '' }),
  });
  if (!res.ok) throw new Error(`Failed to set version phase: ${res.status}`);
  return res.json();
}

export interface SetVersionGoalResponse {
  version_number: number;
  goal: string | null;
  goal_updated_at: string | null;
  goal_editor_info: string | null;
}

export async function setVersionGoal(
  projectId: string,
  goal: string | null,
): Promise<SetVersionGoalResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-goal`, {
    method: 'POST',
    body: JSON.stringify({ goal: goal ?? null }),
  });
  if (!res.ok) throw new Error(`Failed to set version goal: ${res.status}`);
  return res.json();
}

export interface CutVersionResponse {
  abandoned_version: {
    version_number: number;
    status: string;
  };
  new_version: {
    version_number: number;
    phase: string;
    goal: string | null;
    opened_at: string;
  };
}

export async function cutVersion(
  projectId: string,
  reason?: string,
  idempotencyKey?: string,
): Promise<CutVersionResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/version-cut`, {
    method: 'POST',
    body: JSON.stringify({
      reason: reason ?? '',
      ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || data?.error?.message || `Failed to cut version: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export interface ClearDeployFailedResponse {
  version_number: number;
  serving_status: string | null;
  served_sha: string | null;
  served_at: string | null;
  deploy_failure_reason: string | null;
  clear_mode: 'verified' | 'operator_attested';
  operator_attested: boolean;
  attestation_reason: string | null;
}

// Clear a stale post-promotion deploy_failed serving state. Without `force`, the
// backend only clears when the promoted SHA is independently observed serving;
// with `force` + a `reason` it records an operator-attested clear (used in the
// interim where the live serving SHA is unobservable).
export async function clearDeployFailed(
  projectId: string,
  force?: boolean,
  reason?: string,
): Promise<ClearDeployFailedResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/clear-deploy-failed`,
    {
      method: 'POST',
      body: JSON.stringify({
        ...(force ? { force: true } : {}),
        ...(reason ? { reason } : {}),
      }),
    },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail =
      data?.detail || data?.error?.message || `Failed to clear deploy-failed: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export interface CreateCriterionResponse {
  criterion_key: string;
  text: string;
  release_version_id: string;
}

export async function createAcceptanceCriterion(
  projectId: string,
  text: string,
): Promise<CreateCriterionResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria`,
    { method: 'POST', body: JSON.stringify({ text }) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to create criterion: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export async function deleteAcceptanceCriterion(
  projectId: string,
  criterionKey: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/${encodeURIComponent(criterionKey)}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = data?.detail || `Failed to delete criterion: ${res.status}`;
    throw new Error(detail);
  }
}

export async function reinstateAcceptanceCriterion(
  projectId: string,
  criterionKey: string,
): Promise<{ status: string; criterion_key: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/${encodeURIComponent(criterionKey)}/reinstate`,
    { method: 'POST' },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to reinstate criterion: ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export async function attestAcceptanceCriterion(
  projectId: string,
  criterionKey: string,
  reason: string,
): Promise<{ status: string; criterion_key: string; coverage: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/${encodeURIComponent(criterionKey)}/attest`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to attest criterion: ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : `Failed to attest criterion: ${res.status}`);
  }
  return data;
}

export async function linkCriteriaToDirection(
  projectId: string,
  directionKey: string,
  criterionKeys: string[],
): Promise<{ status: string; direction_key: string; criterion_keys: string[] }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/acceptance-criteria/link-direction`,
    { method: 'POST', body: JSON.stringify({ direction_key: directionKey, criterion_keys: criterionKeys }) },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data?.detail || `Failed to link criteria to direction: ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : `Failed to link criteria to direction: ${res.status}`);
  }
  return data;
}

export type QualifyTriggerStatus =
  | 'wrong_phase'
  | 'already_pending'
  | 'project_not_found'
  | 'no_credentials'
  | 'sha_error'
  | 'version_not_found'
  | 'cached_verdict'
  | 'spawn_failed'
  | 'not_promoted'
  | 'dry_run'
  | 'promoted'
  | 'promotion_failed'
  | 'error';

export interface QualifyTriggerResponse {
  status: QualifyTriggerStatus;
  error?: string | null;
  conflict?: boolean | null;
}

export async function triggerQualify(projectId: string): Promise<QualifyTriggerResponse> {
  const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/qualify`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Failed to trigger qualify: ${res.status}`);
  return res.json();
}

export interface QualificationVerdictRecord {
  id: string;
  release_version_id: string;
  candidate_sha: string;
  confidence: number | null;
  sub_scores: VersionQualificationSubScores | null;
  blocker: string | null;
  audit_missing_reason: string | null;
  audit_findings?: AuditFinding[] | null;
  excluded_audit_findings?: AuditFinding[] | null;
  // Challenger-confirmed false positives: findings refuted by an independent challenger (with reason).
  challenger_refuted_audit_findings?: ChallengerRefutedFinding[] | null;
  migration_safety?: string | null;
  migration_safety_detail?: string | null;
  migration_apply_safety?: string | null;
  migration_apply_safety_detail?: string | null;
  test_erosion_detail?: Record<string, unknown> | null;
  smoketest_detail?: Record<string, unknown> | null;
  // Whether the smoketest dimension was enabled for this qualification run.
  // False means the 0.0 sub-score is a zero-weighted placeholder, not an
  // active lane. Null/absent on legacy verdict rows that predate this field.
  smoketest_enabled?: boolean | null;
  audit_reconciliation?: AuditReconciliation | null;
  would_promote: boolean | null;
  promotion_enabled: boolean | null;
  terminal_status: string | null;
  threshold: number | null;
  started_at: string | null;
  finalized_at: string | null;
  created_at: string;
  // How this verdict's promotion actually played out in production:
  // 'serving_confirmed' | 'deploy_failed' | 'unconfirmed' (null until recorded).
  post_promotion_outcome?: string | null;
  post_promotion_outcome_at?: string | null;
  promotion_pr_url?: string | null;
  promotion_conflict?: boolean | null;
  promotion_error_detail?: string | null;
  goal_alignment?: GoalAlignment | null;
  blind_merge_grade?: BlindMergeGrade | null;
  completion_debt?: CompletionDebtQualSnapshot | null;
  debt_enforce_confidence_preview?: number | null;
  debt_enforce_weight_preview?: number | null;
}

export interface QualificationVerdictsInfraContext {
  infra_failure_count: number;
  consecutive_infra_errors: number;
}

export interface QualificationVerdictsResponse {
  verdicts: QualificationVerdictRecord[];
  total: number;
  infra_context?: QualificationVerdictsInfraContext;
}

export async function fetchQualificationVerdicts(
  projectId: string,
  limit = 20,
): Promise<QualificationVerdictsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/qualification-verdicts?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch qualification verdicts: ${res.status}`);
  return res.json();
}


export interface RecentFinding {
  task_id: string;
  title: string;
  type: string;
  scope: string;
  priority: string;
  actionability_grade: 'HIGH' | 'MEDIUM' | 'LOW';
  direction_id: string | null;
  researched_at: string | null;
}

export interface RecentFindingsResponse {
  findings: RecentFinding[];
  grade_distribution: { HIGH: number; MEDIUM: number; LOW: number };
  grade_distribution_pct: { HIGH: number; MEDIUM: number; LOW: number };
  total: number;
}

export async function fetchRecentFindings(
  projectId: string,
  limit = 50,
): Promise<RecentFindingsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/recent-findings?limit=${limit}`,
  );
  if (!res.ok) throw new Error(`Failed to fetch recent findings: ${res.status}`);
  return res.json();
}

export interface ChallengeSummary {
  challenged_boundary: string | null;
  agreement: 'agrees' | 'disagrees' | 'indeterminate';
  second_opinion_verdict: string | null;
  reason: string;
}

export interface StagingBranchPrefixesResponse {
  project_id: string;
  staging_branch_prefixes: string[];
  is_default: boolean;
}


/**
 * Owner-only project delete. The backend cascades to tasks/artifacts/history
 * and, when `force` is true, bulk-cancels active tasks and tears down their VMs
 * before removing the project. Without `force` the server refuses (409) if the
 * project still has active tasks. Surfaces the server `detail` (403 permission,
 * 409 active-tasks, 429 contention) as the thrown error message.
 */
export async function deleteProject(
  projectId: string,
  force = false,
): Promise<{ message: string; active_tasks_cancelled: number }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}?force=${force}`,
    { method: 'DELETE' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to delete project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to delete project: ${res.status}`);
  }
  return res.json();
}

export async function resetProject(
  projectId: string,
): Promise<{ message: string; active_tasks_cancelled: number; tasks_deleted: number }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/reset`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to reset project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to reset project: ${res.status}`);
  }
  return res.json();
}

export interface BootstrapRetryResponse {
  state: 'spawned' | 'already_active' | 'already_completed' | 'not_eligible';
  task_id?: string | null;
  reason?: string | null;
  [key: string]: unknown;
}

export async function retryProjectBootstrap(
  projectId: string,
): Promise<BootstrapRetryResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/bootstrap/retry`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to retry bootstrap: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to retry bootstrap: ${res.status}`);
  }
  return res.json();
}

export async function refreshProjectBootstrap(
  projectId: string,
): Promise<BootstrapRetryResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/bootstrap/refresh`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to refresh bootstrap: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to refresh bootstrap: ${res.status}`);
  }
  return res.json();
}

/**
 * Owner-only project cancel. Marks the project terminal, cancels all active
 * tasks, and tears down their VMs. Idempotent on the server: already-terminal
 * projects return `ignored` rather than an error.
 */
export async function cancelProject(
  projectId: string,
): Promise<{ status: 'cancelled' | 'ignored'; message: string }> {
  const res = await apiFetch(
    `/api/projects/${encodeURIComponent(projectId)}/cancel`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to cancel project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to cancel project: ${res.status}`);
  }
  return res.json();
}

export async function setProjectPaused(
  projectId: string,
  paused: boolean,
): Promise<{ paused: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/pause`,
    { method: 'PATCH', body: JSON.stringify({ paused }) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to update pause state: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to update pause state: ${res.status}`);
  }
  return res.json();
}

/**
 * Owner-only project archive. Sets `archived=True` so the project disappears
 * from the active dashboard and stops generating/dispatching new work; all
 * project data is retained for a later unarchive. Idempotent on the server.
 * Surfaces the server `detail` (403 permission, etc.) as the thrown error.
 */
export async function archiveProject(
  projectId: string,
): Promise<{ message: string; archived: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/archive`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to archive project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to archive project: ${res.status}`);
  }
  return res.json();
}

/**
 * Owner-only project unarchive. Clears `archived` so the project reappears in
 * the active list and regains normal eligibility. Idempotent on the server.
 */
export async function unarchiveProject(
  projectId: string,
): Promise<{ message: string; archived: boolean }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/unarchive`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = (data?.error?.message) || (data?.detail) || `Failed to unarchive project: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to unarchive project: ${res.status}`);
  }
  return res.json();
}

/**
 * Fetch the archived-only project set (the restore view). The active project
 * list is fetched elsewhere (useProjectsState); this dedicated helper requests
 * `GET /api/admin/projects?archived=true` and returns the lightweight `items`
 * array. Each item carries `archived: true`.
 */
export async function fetchArchivedProjects(
  limit = 100,
): Promise<import('../types/project').Project[]> {
  const res = await apiFetch(`/api/admin/projects?archived=true&limit=${limit}`);
  if (!res.ok) throw new Error(`Failed to fetch archived projects: ${res.status}`);
  const data = await res.json().catch(() => ({}));
  return Array.isArray(data?.items) ? data.items : [];
}


export interface NullBranchConfigEntry {
  id: string;
  name: string;
  github_owner: string;
  github_repo: string;
  is_self_repo: boolean;
  created_at: string | null;
}

export interface NullBranchConfigResponse {
  projects: NullBranchConfigEntry[];
  self_repo_skipped: NullBranchConfigEntry[];
  total: number;
}

export async function fetchNullBranchConfig(): Promise<NullBranchConfigResponse> {
  const res = await apiFetch('/api/admin/diagnostics/null-branch-config');
  if (!res.ok) throw new Error(`Failed to fetch null branch config: ${res.status}`);
  return res.json();
}

export async function backfillProjectBranches(projectId: string): Promise<unknown> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/backfill-branches`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to backfill branches: ${res.status}`);
  return res.json();
}

// Enable the warm pool for a single project: scopes the allowlist to it, forces
// the master switch on, establishes a sensible target, and warms first members.
// Backs POST /api/admin/projects/{id}/warm-pool/enable.
export async function enableWarmPoolForProject(projectId: string): Promise<unknown> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/warm-pool/enable`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to enable warm pool: ${res.status}`);
  return res.json();
}

// Disable the warm pool for a single project: drains its members and removes it
// from the allowlist without affecting peers that are still scoped.
// Backs POST /api/admin/projects/{id}/warm-pool/disable.
export async function disableWarmPoolForProject(projectId: string): Promise<unknown> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/warm-pool/disable`,
    { method: 'POST' },
  );
  if (!res.ok) throw new Error(`Failed to disable warm pool: ${res.status}`);
  return res.json();
}

// Read the current per-project warm-pool TARGET map (and the global fallback)
// from the settings endpoint. Returns the two target fields; the rest of the
// settings payload is ignored. An older backend that predates per-project
// targets omits the map — coerce that to {} so callers always get a record.
export async function fetchWarmPoolTargets(): Promise<WarmPoolTargets> {
  const res = await apiFetch('/api/admin/settings');
  if (!res.ok) throw new Error(`Failed to fetch warm-pool targets: ${res.status}`);
  const data = await res.json();
  return {
    warm_pool_target_size: Number(data?.warm_pool_target_size ?? 0),
    warm_pool_target_size_by_project: data?.warm_pool_target_size_by_project ?? {},
  };
}

export async function patchAdminSettings(settings: { promotion_enabled: boolean }): Promise<unknown> {
  const res = await apiFetch('/api/admin/settings', {
    method: 'PATCH',
    body: JSON.stringify(settings),
  });
  if (!res.ok) throw new Error(`Failed to patch admin settings: ${res.status}`);
  return res.json();
}

// Set (targetSize 0..50) or clear (null) a single project's explicit warm-pool
// target. Backs POST /api/admin/projects/{id}/warm-pool/target with body
// {target_size}. Clearing falls the project back to the global default.
export async function setWarmPoolTargetForProject(
  projectId: string,
  targetSize: number | null,
): Promise<WarmPoolTargetSetResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/warm-pool/target`,
    { method: 'POST', body: JSON.stringify({ target_size: targetSize }) },
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const msg = data?.detail || `Failed to set warm-pool target: ${res.status}`;
    throw new Error(typeof msg === 'string' ? msg : `Failed to set warm-pool target: ${res.status}`);
  }
  return res.json();
}

// ── Compute jobs ────────────────────────────────────────────────────────────

/** Bounded diagnostic recorded by the backend when a compute backend RPC
 *  (launch / poll / cost / termination) fails in the monitor or kill path.
 *  Mirrors `_build_compute_rpc_diagnostic` in backend/app/compute_jobs.py. */

export interface BuildConfigCommandEntry {
  path: string | null;
  type: string | null;
  detected_test_cmd: string | null;
  detected_build_cmd: string | null;
  detected_install_cmd: string | null;
  detected_lint_cmd: string | null;
  confirmed_test_cmd: string | null;
  confirmed_build_cmd: string | null;
  confirmed_install_cmd: string | null;
  confirmed_lint_cmd: string | null;
  commands_confirmed_at: string | null;
  confirming_task_id: string | null;
  commands_stale: boolean;
}

/** Outcome of the repo_bootstrap command write-back, recorded independently of
 *  the repo overview. `confirmed` = commands were written back and verified;
 *  `missing_commands` = bootstrap produced an overview but no commands to confirm;
 *  `writeback_failed` = commands existed but the write-back did not land;
 *  `unknown` = a legacy agent omitted the status payload entirely. */
export type BuildConfigCommandMemoryStatus =
  | 'confirmed'
  | 'missing_commands'
  | 'writeback_failed'
  | 'unknown';

export interface BuildConfigCommandMemory {
  status: BuildConfigCommandMemoryStatus | string;
  complete?: boolean | null;
  detail?: string | null;
  paths?: string[] | null;
  updated_at?: string | null;
}

export interface BuildConfigCommandsResponse {
  project_id: string;
  directories: BuildConfigCommandEntry[];
  /** Bootstrap command-memory outcome. Null/absent on legacy projects that
   *  predate the command-memory status contract. */
  command_memory?: BuildConfigCommandMemory | null;
}

export async function fetchBuildConfigCommands(
  projectId: string,
  signal?: AbortSignal,
): Promise<BuildConfigCommandsResponse> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/build-config/commands`,
    undefined,
    2,
    30000,
    signal,
  );
  if (!res.ok) throw new Error(`Failed to fetch build config commands: ${res.status}`);
  return res.json() as Promise<BuildConfigCommandsResponse>;
}


export async function declareCapabilityVerdict(
  projectId: string,
  capName: string,
  verdict: 'adopt' | 'reject' | 'continue-trial',
  rationale?: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${projectId}/capability-trials/declare`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cap_name: capName, verdict, rationale: rationale ?? null }),
    },
  );
  if (!res.ok) throw new Error(`Failed to declare capability verdict: ${res.status}`);
}

export async function dismissPendingConfirmation(
  projectId: string,
  pendingId: string,
  note?: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/pending-confirm/${encodeURIComponent(pendingId)}/dismiss`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: note ?? null }),
    },
  );
  if (!res.ok) throw new Error(`Failed to dismiss pending confirmation: ${res.status}`);
}

export async function acceptPendingConfirmation(
  projectId: string,
  pendingId: string,
  answer?: string,
): Promise<{ status: string; type: string }> {
  const res = await apiFetch(
    `/api/admin/projects/${encodeURIComponent(projectId)}/pending-confirm/${encodeURIComponent(pendingId)}/accept`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer: answer ?? null }),
    },
  );
  if (!res.ok) throw new Error(`Failed to accept pending confirmation: ${res.status}`);
  return res.json();
}

/** A pull-based fleet runner registered to a project. */