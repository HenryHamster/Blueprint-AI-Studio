// Canonical shape of the `/api/admin/digest` response.
//
// Previously DashboardHome.tsx and CatchUpPanel.tsx each declared their own
// `DigestData` / `FailureGroup` / `RecentCompletion` interfaces plus (in the
// dashboard's case) the `assertDigestContract` guard. Because both render the
// SAME backend payload, that was a two-place-edit divergence trap: a backend
// rename had to be mirrored by hand in both files, and drift was silent.
//
// This module is the single source of truth. `DigestData` is the UNION of every
// field either consumer reads, with per-field types chosen so the same object is
// assignable to BOTH consumers' (and their child sections') prop contracts:
//   - review sub-objects (self_review_*, review_accuracy, first_*_rate) use the
//     STRICTER (required-sample_size) shape
//     CatchUpReviewQualitySection demands, plus the dashboard's extra optional
//     fields — strict objects are assignable to the dashboard's looser props.
//   - `self_review_prediction_accuracy_trends` uses the richer
//     SrTrendWithFixIterations (a subtype of SrTrendData), assignable wherever
//     either is expected.
//
// `direction_progress` is the one field whose ELEMENT type genuinely diverges:
// DashboardHome feeds DirectionPortfolioSection's `DirectionProgress` while
// CatchUpPanel feeds DirectionProgressSection's structurally-incompatible
// `DirectionProgress`. `DigestData` is therefore generic in that element type so
// each consumer keeps its own strict typing without an unsafe widen/downcast.

import type { EdgesTrend } from '../components/SrTrendBreakdown';
import type { SrTrendWithFixIterations } from '../components/ReviewQualitySection';
export interface ActiveTask {
  task_id: string;
  title?: string | null;
  status: string;
  task_type?: string;
  started_at?: string | null;
  running_cost_usd?: number | null;
  direction_text?: string | null;
  scope?: string | null;
  review_iteration?: number | null;
  review_cap?: number | null;
}

export interface NeedsAttentionItem {
  task_id: string;
  title?: string | null;
  reason: string;
  error_snippet?: string | null;
  pr_url?: string | null;
  failure_class?: string | null;
  scope?: string | null;
  direction_text?: string | null;
  merge_retries_saved?: number;
  waiting_hours?: number;
  spend_cap_usd?: number | null;
  deadline_seconds?: number | null;
}

export interface FailureGroupTask {
  task_id: string;
  title: string;
  // Required to match the backend payload and the stricter CatchUp consumers
  // (RecentFailureGroupsSection / CatchUpCostDiagnosticsSection). Strict fields
  // remain assignable to the dashboard's looser optional-chained reads.
  error_snippet: string | null;
  attempt_count: number;
  waiting_hours?: number;
  taxonomy?: string;
  timeout_subtype?: string | null;
  failure_stage?: string | null;
  direction_id?: string | null;
  direction_text?: string | null;
}

export interface FailureGroup {
  failure_class: string;
  label: string;
  count: number;
  tasks: FailureGroupTask[];
}

export interface RecentCompletion {
  task_id: string;
  title: string;
  completed_at: string | null;
  pr_url: string | null;
  task_type: string;
  direction_id?: string | null;
  direction_text?: string | null;
  scope?: string | null;
  duration_seconds?: number | null;
  review_iterations?: number | null;
  // No-CI merge gate fall-open reason KEY; null/undefined for verified and
  // older merges. Rendered as a compact UNVERIFIED indicator in the Recent
  // Merges timeline (label via humanizeUnverifiedReason).
  unverified_merge_reason?: string | null;
}

// Compact, websocket-safe rollup of a strategic-judgment challenge. Mirrors the
// backend `strategic_challenge_summary` (strategic_completion.py): only the four
// scalar fields travel into completion receipts, recent-cycle summaries, and
// digest cards — the full bounded item list stays in durable cycle_metadata.
// All fields optional so a partial/legacy payload degrades gracefully; the field
// is OMITTED entirely for cycles that carried no challenge, so absence never
// reads as a failure.
export interface StrategicChallengeSummary {
  // The record's own status.
  mode?: 'ok' | 'blocked' | 'invalid';
  // Rolled-up overall verdict across challenged judgments.
  verdict?: 'approved' | 'warned' | 'blocked';
  // Challenged judgments the challenger could not confirm (uncertain).
  warned_count?: number;
  // Challenged judgments the challenger disputed (rejected).
  blocked_count?: number;
}

interface LatestCycle {
  cycle_id?: string | null;
  // Required (non-null) so `completed_at ?? started_at` stays `string` for
  // timeAgo() in SystemStatusCard — matches the backend (a cycle always has a
  // start time) and the dashboard's prior shape.
  started_at: string;
  completed_at?: string | null;
  outcome?: string | null;
  directions_created?: number | null;
  directions_retired?: number | null;
  vision_updated?: boolean | null;
  summary_text?: string | null;
  memos_available?: number | null;
  memos_cited?: number | null;
  // Present only when this cycle carried a strategic-judgment challenge record;
  // omitted for legacy cycles so absence is not rendered as a warning state.
  strategic_judgment_challenge?: StrategicChallengeSummary;
}

export interface RecentFailure {
  task_id: string;
  title: string;
  failed_at: string | null;
  error_snippet: string | null;
  waiting_hours?: number;
  attempt_count?: number;
  timeout_subtype?: string | null;
  direction_id?: string | null;
  direction_text?: string | null;
  task_type?: string;
  failure_class?: string | null;
  scope?: string | null;
}

export interface DirectionPortfolioCompositionEntry {
  direction_id: string;
  direction_text: string;
  status: string | null;
  priority: string;
  quiet_this_window: boolean;
}

export interface DirectionPortfolio {
  active_count: number;
  self_sufficient_count: number;
  composition: DirectionPortfolioCompositionEntry[];
}

export interface DirectionFindingChallengeSummary {
  challenged_findings: number;
  agreement_distribution: { agrees?: number; disagrees?: number; indeterminate?: number };
  boundary_distribution: { reproduce_absent?: number; low_actionability_decay?: number; reproduce_indeterminate_dispatch?: number };
  fail_closed_unparseable?: number;
}

interface LaneRelayTimings {
  wall_clock_seconds?: number | null;
  active_seconds?: number | null;
  gap_seconds?: number | null;
  task_count?: number | null;
  pr_count?: number | null;
}

export interface LaneRelayComparison {
  source_version?: {
    version_number?: number | null;
    captured_at?: string | null;
    terminal_status?: string | null;
  };
  relay?: LaneRelayTimings;
  lane?: LaneRelayTimings;
  deltas?: {
    wall_clock_seconds?: number | null;
    active_seconds?: number | null;
    gap_seconds?: number | null;
    task_count?: number | null;
    pr_count?: number | null;
  };
}

export interface CapabilityTrialVerdict {
  verdict: 'adopt' | 'reject' | 'no_effect' | 'continue-trial' | 'ADOPT' | 'KILL' | 'CONTINUE';
  lift?: number | null;
  n_with?: number;
  n_without?: number;
  n_lane?: number;
  n_control?: number;
  lane_merge_rate?: number | null;
  control_merge_rate?: number | null;
  lane_cost_per_merged?: number | null;
  control_cost_per_merged?: number | null;
  gate_integrity_violations?: number;
  checkpoint_loss_rate?: number | null;
  // Treatment-engagement telemetry (absent when no fix tasks recorded yet).
  warm_resume_fraction?: number | null;
  checkpoint_fraction?: number | null;
  lane_fix_tasks_sampled?: number;
  // Set to true when the futility engagement guard fired (treatment never engaged).
  engagement_low?: boolean;
  immediate_alert?: boolean;
  evaluated_at?: string;
  window_hours?: number | null;
  verdict_reason?: string;
  thresholds?: {
    min_n_per_arm?: number;
    adopt_lift?: number;
    reject_lift?: number;
    futility_n?: number;
    min_n?: number;
    kill_merge_rate_floor?: number;
    kill_cost_ceiling?: number;
    kill_gate_violations_max?: number;
    kill_checkpoint_loss_ceiling?: number;
    adopt_merge_lift_floor?: number;
    adopt_cost_improvement?: number;
    futility_min_engagement?: number;
  };
  accumulation_basis?: string;
  arm_composition?: { with_assigned?: number; with_receipt?: number; without_assigned?: number; without_receipt?: number; without_contaminated?: number };
  accrual_projection?: { with_7d?: number; without_7d?: number; window_days?: number; holdback_fraction?: number; days_to_min_n?: number | null; alternative_projections?: Record<string, number | null> };
  lane_relay_comparison?: LaneRelayComparison | null;
  // True when the trial is permanently stalled (continue-trial with days_to_min_n=null).
  is_stalled?: boolean;
}

type ScopeConversionRates = Record<string, { created: number; merged: number; calibrated_in?: number; calibrated_out?: number }>;

/**
 * Canonical `/api/admin/digest` response shape, generic in the
 * `direction_progress` element type (see module header).
 *
 * `DP` defaults to `unknown` so consumers that never read `direction_progress`
 * (e.g. SystemStatusCard) can use the bare `DigestData`.
 */
// Fleet-wide no-CI merge-gate verification health (read-only observability;
// never influences a merge decision). Mirrors the backend
// `compute_merge_gate_fleet_health` return shape.
export interface MergeGateFleetHealth {
  verified_count: number;
  fell_open_count: number;
  verification_rate: number | null;
  most_common_reason: string | null;
  degraded_project_count: number;
  project_count: number;
}

export interface ParkWakeHealth {
  parked_task_count: number;
  oldest_parked_age_seconds: number | null;
  fired_undelivered_count: number;
  oldest_fired_age_seconds: number | null;
  delivery_stalled: boolean;
  delivery_stall_threshold_seconds: number;
}

export interface FindingStructuralGiveUpItem {
  task_id: string;
  title: string;
  reason: string;
  direction_id?: string | null;
}

// Per-effort-tier / per-actual-model LLM efficiency rollup (read-only).
// Mirrors the backend `compute_effort_model_efficiency` payload
// (efficiency_snapshot.py): each row carries run/landed counts + rate, priced
// LLM cost, cost-per-merge, token volume, and priced/unpriced coverage so an
// understated (unpriced) cost is visible rather than silently folded into the
// priced total.
export interface EfficiencyBucket {
  run_count: number;
  landed_count: number;
  landed_rate: number | null;
  // Verified-merge payoff alongside the RAW landed figures: degraded (no-CI
  // fall-open) merges stay in landed_count/landed_rate but are EXCLUDED here, so
  // a fall-open merge is not presented as a genuine landing. Optional so older
  // backends (predating the verified-merge rollup) degrade gracefully — the
  // panel falls back to the raw landed figures when these are absent.
  verified_landed_count?: number;
  verified_landed_rate?: number | null;
  total_cost_usd: number;
  // Priced LLM dollars per VERIFIED PR that landed; null when nothing
  // verified-landed (or the bucket is unpriced).
  cost_per_merge: number | null;
  input_tokens: number;
  output_tokens: number;
  llm_call_count: number;
  // Token-bearing ("priceable") runs split by whether the (provider, model)
  // combo has a pricing entry. Unpriced runs contribute 0 cost.
  priced_run_count: number;
  unpriced_run_count: number;
  // Headline pricing honesty is spend-weighted; this preserves the old
  // token-bearing run fraction as a secondary diagnostic.
  unpriced_coverage_fraction: number | null;
  task_coverage_fraction?: number | null;
  estimated_unpriced_cost_usd?: number;
  unpriced_input_tokens?: number;
  unpriced_output_tokens?: number;
}

// Which actual models a requested effort tier ran on (requested-vs-actual
// divergence — e.g. a high-effort run that fell back to a cheaper model).
export interface EfficiencyResolvedModel {
  provider: string;
  model: string;
  run_count: number;
  landed_count: number;
  priced: boolean;
}

export interface EffortEfficiencyRow extends EfficiencyBucket {
  effort: string;
  resolved_models: EfficiencyResolvedModel[];
}

export interface ModelEfficiencyRow extends EfficiencyBucket {
  provider: string;
  model: string;
  priced: boolean;
}

// Aggregated model-fallback (downgrade) records: a requested model resolved to
// a different effective model (preview->GA, rate-limit fallback, etc.).
interface EfficiencyFallbackCombo {
  requested: string;
  effective: string;
  provider: string;
  reason: string;
  count: number;
}

// Provider routing A/B verdict (ADOPT/REJECT/INSUFFICIENT_DATA). Mirrors the
// backend compute_provider_routing_verdict return shape so the UI can render
// routing-payoff state alongside the per-model efficiency rows that informed it.
export interface ProviderRoutingVerdict {
  state: 'ADOPT' | 'REJECT' | 'INSUFFICIENT_DATA';
  supporting: {
    deepseek_run_count: number;
    deepseek_pr_eligible_run_count: number;
    deepseek_verified_landed_count: number;
    deepseek_verified_landed_rate: number | null;
    deepseek_cost_per_merge: number | null;
    deepseek_total_cost_usd: number;
    anthropic_run_count: number;
    anthropic_pr_eligible_run_count: number;
    anthropic_verified_landed_count: number;
    anthropic_verified_landed_rate: number | null;
    anthropic_cost_per_merge: number | null;
    anthropic_total_cost_usd: number;
    cost_ratio: number | null;
    verified_rate_delta_pct: number | null;
  };
  recommended_action: string;
}

// Per-provider routing share (run count, verified merge rate, cost) aggregated
// from the by_model rows. Mirrors the backend compute_provider_routing_share
// return shape so the UI can display the distribution at a glance.
export interface ProviderRoutingShareEntry {
  provider: string;
  run_count: number;
  pr_eligible_run_count: number;
  verified_landed_count: number;
  verified_landed_rate: number | null;
  total_cost_usd: number;
  cost_per_merge: number | null;
}

export interface ProviderRoutingShare {
  by_provider: ProviderRoutingShareEntry[];
  total_run_count: number;
  total_pr_eligible_run_count: number;
}

// Cost-telemetry reconciliation: structurally independent recomputation of LLM
// cost and cache-hit rate compared against the primary cost-telemetry path.
// Mirrors the backend ReconciliationResult.asdict() shape so the UI can render
// a trust indicator alongside the efficiency breakdown.
export interface CostTelemetryReconciliation {
  total_cost_snapshot_path: number;
  total_cost_direct_path: number;
  absolute_delta: number;
  relative_delta: number | null;
  within_tolerance: boolean;
  tolerance: number;
  total_input_tokens_snapshot_path: number;
  total_input_tokens_direct_path: number;
  total_cache_read_input_tokens_snapshot_path: number;
  total_cache_read_input_tokens_direct_path: number;
  total_cache_creation_input_tokens_snapshot_path: number;
  total_cache_creation_input_tokens_direct_path: number;
  cache_hit_rate_snapshot_path: number;
  cache_hit_rate_direct_path: number;
  cache_hit_rate_delta: number;
  cache_hit_rate_within_tolerance: boolean;
  task_count: number;
}

export interface EffortModelEfficiency {
  by_effort: EffortEfficiencyRow[];
  by_model: ModelEfficiencyRow[];
  totals: EfficiencyBucket;
  unpriced_combos: string[];
  fallbacks: {
    total: number;
    combos: EfficiencyFallbackCombo[];
  };
  provider_routing_verdict?: ProviderRoutingVerdict;
  provider_routing_share?: ProviderRoutingShare;
  cost_telemetry_reconciliation?: CostTelemetryReconciliation | null;
}

// Dependency/foundation-resolution observability (read-only). Mirrors the
// backend `compute_dependency_health_signals` return shape: parked blockers
// (BLOCKED/held tasks pushed off the recovery queue), duplicate-foundation
// candidates (two tasks independently building the same files), and
// unresumable cancellations (blocked dependents given up on). Each block
// carries a total plus a capped list for operator drill-down.
export interface ParkedBlockerTask {
  task_id: string;
  title: string;
  project_id: string | null;
  blocker_kind: string | null;
  blocked_reason: string | null;
  blocked_at: string | null;
  recheck_after: string | null;
  direction_id?: string | null;
}
export interface DuplicateFoundationCandidate {
  task_ids: string[];
  overlap_ratio: number;
  shared_files: string[];
}
export interface UnresumableCancellationTask {
  task_id: string;
  title: string;
  project_id: string | null;
  give_up_class: string;
  need?: string | null;
  direction_id?: string | null;
  at: string | null;
}
// Live currently-BLOCKED population — the healthy waiting set (includes
// not-yet-escalated dependents). Mirrors backend compute_live_blocked_summary.
export interface LiveBlockedTask {
  task_id: string;
  title: string;
  project_id: string | null;
  blocker_kind: string | null;
  blocked_reason: string | null;
  blocked_at: string | null;
  age_bucket: string;
  direction_id?: string | null;
}
// A blocked dependent that successfully RESUMED when its foundation landed.
// Mirrors backend compute_blocked_resume_summary.
export interface BlockedResumeTask {
  task_id: string;
  title: string;
  project_id: string | null;
  resolved_by: string;
  foundation?: string | null;
  direction_id?: string | null;
  at: string | null;
}
export interface DependencyHealthSignals {
  parked_blockers: { total: number; tasks: ParkedBlockerTask[] };
  duplicate_foundation_candidates: { total: number; candidates: DuplicateFoundationCandidate[] };
  unresumable_cancellations: {
    total: number;
    by_class?: Record<string, number>;
    by_direction?: Record<string, number>;
    tasks: UnresumableCancellationTask[];
  };
  // Affirmative health signals (additive). live_blocked: the current BLOCKED
  // population with coarse age bucketing; resumes: successful blocked-dependent
  // resumes over the digest window with an edge-vs-completed breakdown.
  live_blocked?: {
    total: number;
    by_age?: Record<string, number>;
    tasks: LiveBlockedTask[];
  };
  resumes?: {
    total: number;
    by_resolved_by?: Record<string, number>;
    by_direction?: Record<string, number>;
    tasks: BlockedResumeTask[];
  };
  finding_structural_give_up?: {
    total: number;
    by_reason: Record<string, number>;
    by_direction: Record<string, number>;
    findings: FindingStructuralGiveUpItem[];
  };
  // Stranded capability/foundation prerequisites surfaced as missing-foundation
  // demand. Mirrors backend compute_missing_capability_demand_summary: distinct
  // demand total, the still_blocked-vs-already_cancelled split, a per-direction
  // demand count, and a bounded largest-demand-first sample.
  missing_capability_demand?: {
    total: number;
    still_blocked: number;
    already_cancelled: number;
    by_direction: Record<string, number>;
    demands: MissingCapabilityDemand[];
  };
}

// A single stranded capability/foundation prerequisite aggregated across the
// dependents that need it. Mirrors a capability_demand demand entry.
export interface MissingCapabilityDemand {
  kind: string;
  ref: string | null;
  label: string | null;
  dependent_count: number;
  still_blocked: number;
  already_cancelled: number;
  sample_task_ids: string[];
  direction_ids: string[];
  oldest_blocked_at: string | null;
  max_parked_reevals: number;
}

export interface DigestData<DP = unknown> {
  // ----- staleness signal -----
  // Set by the backend when the L3 (on-demand) cache serves a last-good
  // fallback because the live compute failed. Absent on L1/L2 (precomputed)
  // responses. The dashboard renders a per-source staleness banner when true.
  served_stale?: boolean;

  // ----- core contract (validated by assertDigestContract) -----
  failed_count: number;
  pending_approval_count: number;
  direction_progress?: DP[];
  direction_portfolio?: DirectionPortfolio;
  recent_completions?: RecentCompletion[];
  failure_groups?: FailureGroup[];
  capability_adoption_verdicts?: Record<string, CapabilityTrialVerdict> | null;

  // ----- catch-up window metadata -----
  // Counts are required (number) — the backend always emits them and CatchUp
  // reads them directly (no `?? 0`). The dashboard reads completed_count via
  // `?? 0`, which a required field satisfies harmlessly.
  window_hours?: number;
  generated_at?: string;
  active_count: number;
  running_count: number;
  starting_count: number;
  evaluating_count: number;
  queued_count: number;
  // Fleet- or project-scoped count of IN_PROGRESS tasks whose last heartbeat is
  // older than 5 minutes. Covers the whole fleet (or the selected project), not
  // the current task page, so the health bar's "N stale" warning reflects real
  // scope. Additive — absent on legacy payloads, so reads must guard.
  stale_heartbeat_count?: number;
  research_cycles: number;
  prs_opened: number;
  active_tasks?: ActiveTask[];
  needs_attention?: NeedsAttentionItem[];
  recent_failures?: RecentFailure[];

  // ----- headline metrics -----
  completed_count: number;
  success_rate?: number | null;
  trend?: 'improving' | 'declining' | 'stable';
  trend_history?: {
    period_end: string;
    success_rate: number | null;
    completed: number;
    failed: number;
    failed_by_class?: Record<string, number>;
    failed_by_taxonomy?: Record<string, number>;
    first_attempt_success_rate?: number | null;
    period_cost_usd?: number | null;
  }[];
  overall_product_ratio?: number | null;
  pipeline_conversion_rate?: number | null;
  pipeline_acceptance_rate?: number | null;
  overall_type_distribution?: Record<string, number> | null;
  narrative_summary?: string | null;
  latest_cycle?: LatestCycle | null;

  // ----- period deltas (catch-up) -----
  prev_completed_count?: number;
  prev_failed_count?: number;
  prev_success_rate?: number | null;
  completed_delta?: number;
  failed_delta?: number;
  success_rate_delta?: number | null;

  // ----- cost -----
  total_estimated_cost_usd?: number | null;
  daily_cost_usd?: number | null;
  daily_cost_ceiling_usd?: number | null;
  spot_cost_usd?: number | null;
  on_demand_cost_usd?: number | null;
  provisioning_overhead_usd?: number | null;
  provisioning_overhead_pct?: number | null;
  cost_by_failure_class?: Record<string, number> | null;
  total_failed_cost_usd?: number | null;
  median_cost_per_merged_pr?: number | null;
  // Per-task-type cost-per-merge attribution: total VM cost / merged-PR count
  // for each task type, with exceeds_threshold flagging types whose CPM runs
  // far above the per-type median. Consumed by CatchUpCostDiagnosticsSection.
  cost_per_merge_by_type?: Record<
    string,
    {
      total_cost_usd: number;
      merged_count: number;
      cost_per_merge: number | null;
      exceeds_threshold: boolean;
    }
  > | null;
  median_cost_per_merge_by_type?: number | null;
  // Per-effort-tier / per-actual-model efficiency rollup (additive; absent on
  // older backends that predate compute_effort_model_efficiency).
  effort_model_efficiency?: EffortModelEfficiency | null;
  // Cost-telemetry reconciliation trust indicator (derived from the same task
  // window as effort_model_efficiency). Absent on legacy payloads or when the
  // task window is empty — the EffortEfficiencyPanel handles absence gracefully.
  cost_telemetry_reconciliation?: CostTelemetryReconciliation | null;

  // ----- scope conversion -----
  scope_conversion_rates?: ScopeConversionRates | null;
  prev_scope_conversion_rates?: ScopeConversionRates | null;
  scope_mismatch_summary?: {
    total_calibrated: number;
    calibrated_success_rate: number | null;
    by_original_scope: Record<string, { total: number; completed: number; failed: number }>;
    top_directions: {
      direction_id: string;
      direction_text: string;
      calibrated_count: number;
      total_tasks: number;
      calibration_rate: number;
    }[];
  } | null;

  // ----- failure distributions -----
  failure_taxonomy_distribution?: {
    categories: Record<string, { count: number; percentage: number }>;
    total_failures: number;
    dominant_category: string | null;
  } | null;
  failure_stage_distribution?: {
    stages: Record<string, number>;
    total_classified: number;
    dominant_stage: string | null;
  } | null;
  pipeline_filter_funnel?: Record<string, number> | null;

  // ----- retry / resurrection (catch-up) -----
  retry_overhead_cost_usd?: number | null;
  retry_waste_by_direction?: { direction_id: string; direction_text: string; retry_cost_usd: number; retry_task_count: number }[] | null;
  retry_task_count?: number | null;
  avg_retries_before_failure?: number | null;
  retry_success_count?: number | null;
  retry_wasted_rate?: number | null;
  retry_recovery_cost_usd?: number | null;
  retry_success_rate_by_class?: Record<string, { merged: number; failed: number; success_rate: number }> | null;
  retry_pattern_summary?: {
    total_multi_attempt_failures: number;
    repeat_class_count: number;
    top_patterns: { sequence: string; count: number }[];
  } | null;
  resurrection_outcome_summary?: {
    total: number;
    completed: number;
    failed_again: number;
    success_rate: number | null;
  } | null;

  // ----- operational health (catch-up) -----
  merge_gate_fleet_health?: MergeGateFleetHealth | null;
  park_wake_health?: ParkWakeHealth | null;
  dependency_health_signals?: DependencyHealthSignals | null;
  migration_health?: {
    current_schema_revision: string | null;
    migration_tasks_in_window: number;
    migration_tasks_succeeded: number;
    migration_tasks_failed: number;
    migration_errors_in_window: number;
    auto_generated_migrations: number;
    migration_slot_contention?: {
      status: 'idle' | 'held' | 'contention' | 'elevated';
      severity: 'info' | 'warning' | 'critical';
      holder: {
        task_id: string;
        title: string | null;
        started_at: string | null;
        hold_seconds: number | null;
      } | null;
      deferred_count: number;
      longest_wait: {
        task_id: string;
        title: string | null;
        waiting_since: string | null;
        wait_seconds: number | null;
      } | null;
    };
  } | null;
  migration_slot_contention?: {
    slot_held: boolean;
    holder_task_id: string | null;
    holder_title: string | null;
    holder_project_id: string | null;
    holder_held_seconds: number | null;
    waiting_count: number;
    longest_wait_seconds: number | null;
    threshold_exceeded: boolean;
    projects: {
      project_id: string | null;
      slot_held: boolean;
      holder_task_id: string | null;
      holder_title: string | null;
      holder_held_seconds: number | null;
      holder_count: number;
      waiting_count: number;
      longest_wait_seconds: number | null;
      threshold_exceeded: boolean;
    }[];
  } | null;
  prevention_efficacy?: {
    preventive_tasks_created: number;
    preventive_tasks_merged: number;
    preventive_tasks_failed: number;
    preventive_merge_rate: number | null;
    overall_merge_rate: number | null;
    assessment: 'positive' | 'negative' | 'neutral' | 'no_data';
  } | null;
  release_qualifier_infra_metrics?: {
    qualifier_runs_total: number;
    release_infra_failure_total: number;
    audit_report_missing_count: number;
    audit_report_parseable_but_unscoreable_count: number;
    smoketest_report_missing_count: number;
    release_infra_failure_streak_max: number;
  } | null;
  total_merge_retries_saved?: number | null;
  merge_early_exit_count?: number | null;
  review_cap_exhaustion_summary?: {
    count: number;
    distinct_direction_count?: number;
    avg_iterations: number | null;
    total_cost_usd: number;
    avg_cost_usd: number | null;
    top_directions: { direction_id: string; count: number }[];
  } | null;
  analysis_overflow_summary?: {
    total_count: number;
    by_scope: Record<string, number>;
    cost_usd: number;
  } | null;
  provision_waste_cost_usd?: number | null;
  provision_waste_by_direction?: {
    direction_id: string;
    direction_text: string;
    provision_cost_usd: number;
    provision_task_count: number;
  }[] | null;
  escalation_outcome_summary?: {
    total_attempted: number;
    total_merged: number;
    total_failed: number;
    overall_success_rate: number | null;
    by_path: Record<string, { attempted: number; merged: number; failed: number; success_rate: number | null }>;
  } | null;

  // ----- failure signals (catch-up) -----
  recurring_error_clusters?: {
    fingerprint: string;
    count: number;
    error_snippet: string;
    direction_ids: string[];
    task_types: string[];
    first_task_id: string;
  }[] | null;
  early_warnings?: {
    failure_class: string;
    rate_progression: number[];
    affected_direction_count: number;
    suggested_action?: string | null;
    severity?: string | null;
  }[] | null;
  recurring_patterns?: {
    fingerprint: string;
    occurrence_count: number;
    first_seen?: string | null;
    last_seen?: string | null;
    severity?: string | null;
    example_message?: string | null;
    affected_direction_count: number;
    affected_directions?: { direction_id: string; direction_text: string }[];
    remediation_status: 'fix_verified' | 'fix_shipped' | 'investigating' | 'unaddressed';
    proactive?: boolean;
  }[] | null;

  // ----- remediation funnel -----
  // Core counts are required to match the backend payload and the stricter
  // CatchUpRemediationPipelineSection (RemediationFunnelSlice) prop; required
  // is assignable to the dashboard's looser RemediationFunnelSection prop.
  remediation_funnel?: {
    investigations_spawned: number;
    investigations_completed: number;
    investigations_with_diagnosis: number;
    remediations_spawned: number;
    remediations_merged: number;
    remediations_failed: number;
    fixes_held?: number;
    avg_time_to_remediation_hours: number | null;
    active_investigations: number;
    // No `| null` on fields that also appear in CatchUp's RemediationFunnelSlice
    // (which omits null) so the whole funnel stays assignable to that prop.
    investigations_by_confidence?: Record<string, number>;
    failed_remediations_by_class?: Record<string, number> | null;
    merged_remediations_by_trigger_class?: Record<string, number> | null;
    cumulative_merged_by_trigger_class?: Record<string, number> | null;
    investigations_spawned_by_class?: Record<string, number> | null;
    investigations_completed_by_class?: Record<string, number> | null;
    remediations_spawned_by_class?: Record<string, number> | null;
    ci_auto_remediation?: {
      spawned: number;
      merged: number;
      failed: number;
      by_ci_failure_type?: Record<string, { spawned: number; merged: number; failed: number }>;
    };
    remediation_success_by_confidence?: Record<string, { merged: number; failed: number }>;
    recent_investigations?: Array<{
      task_id: string;
      direction_id?: string | null;
      summary?: string | null;
      confidence?: string | null;
      severity?: string | null;
      completed_at?: string | null;
    }>;
    active_investigations_detail?: Array<{
      task_id: string;
      direction_id?: string | null;
      elapsed_hours?: number | null;
    }>;
    diagnosis_drop_off_by_reason?: Record<string, number> | null;
    residual_remediation_funnel?: {
      open_cases_by_error_class?: {
        error_class: string;
        count: number;
        most_recent_at: string | null;
        cases: {
          fingerprint: string;
          status: string;
          hit_count: number;
          last_seen_at: string | null;
          last_spawned_at?: string | null;
          sample_message?: string | null;
          consecutive_failure_count?: number | null;
        }[];
      }[];
    } | null;
  } | null;
  investigation_conversion?: {
    total_completed?: number;
    parseable?: number;
    unparseable?: number;
    structural?: number;
    low_confidence?: number;
    actionable?: number;
    remediation_spawned?: number;
  } | null;

  // ----- anomaly throttle (dashboard) -----
  anomaly_adjustment?: {
    active: boolean;
    adjusted_at?: string | null;
    trigger_anomalies?: string[];
    pre_adjustment_max_vms?: number | null;
    adjusted_max_vms?: number | null;
    failure_gate_threshold?: number | null;
    pre_adjustment_failure_gate?: number | null;
    forced_restoration_count?: number;
    forced_restored_at?: string | null;
  } | null;

  // ----- review quality -----
  // Required-sample_size shape so the object is assignable to BOTH the strict
  // CatchUpReviewQualitySection props and the dashboard's looser ReviewQualityDigest.
  first_attempt_success_rate?: {
    // Required (number | null) to match CatchUp's comparison helpers, which take
    // `number | null` (no undefined). The dashboard only reads the optional
    // first_attempt_by_review_category, so requiring these siblings is harmless.
    overall: number | null;
    sample_size: number;
    with_playbook: number | null;
    without_playbook: number | null;
    with_arch_context: number | null;
    without_arch_context: number | null;
    with_measurements?: number | null;
    without_measurements?: number | null;
    first_attempt_by_review_category?: Record<string, number> | null;
    trend?: string | null;
    by_type?: Record<string, number> | null;
    by_scope?: Record<string, number> | null;
  } | null;
  first_review_approval_rate?: {
    overall: number | null;
    sample_size: number;
    by_type?: Record<string, number> | null;
    by_scope?: Record<string, number> | null;
    with_playbook?: number | null;
    without_playbook?: number | null;
    with_measurements?: number | null;
    without_measurements?: number | null;
  } | null;
  self_review_effectiveness?: {
    with_self_review_approval_rate: number | null;
    without_self_review_approval_rate: number | null;
    clean_verdict_approval_rate: number | null;
    fixed_verdict_approval_rate: number | null;
    sample_size: number;
    sr_fixed_count?: number | null;
    sr_clean_count?: number | null;
  } | null;
  self_review_prediction_accuracy?: {
    prediction_hit_rate: number | null;
    miss_rate: number | null;
    false_alarm_rate: number | null;
    sample_size: number;
    self_flagged_shipped_unfixed_task_count?: number | null;
    self_flagged_shipped_unfixed_task_rate?: number | null;
  } | null;
  self_review_prediction_accuracy_trends?: SrTrendWithFixIterations | null;
  sr_regression_risk_test_signal_trends?: {
    computed_at?: string | null;
    bucket_mode?: string | null;
    trend?: string | null;
    miss_by_test_signal_rate_delta?: number | null;
    current?: {
      miss_by_test_signal_rate?: number | null;
      numerator?: number | null;
      denominator?: number | null;
      sample_size?: number | null;
    } | null;
    previous?: {
      miss_by_test_signal_rate?: number | null;
      numerator?: number | null;
      denominator?: number | null;
      sample_size?: number | null;
    } | null;
  } | null;
  self_review_skip_rate?: {
    overall_rate: number;
    skipped_count: number;
    sample_size: number;
    by_task_type: Record<string, { rate: number; skipped_count: number; sample_size: number }>;
  } | null;
  sr_disallowed_blind_spot_rate?: {
    overall_rate: number;
    violation_count: number;
    sample_size: number;
    by_task_type: Record<string, { rate: number; violation_count: number; sample_size: number }>;
  } | null;
  review_accuracy?: {
    total_reviews: number;
    overrides: number;
    dismissals: number;
    misses: number;
    total_approvals: number;
    precision: number | null;
    recall: number | null;
  } | null;
  edges_trend?: EdgesTrend | null;
  sr_false_attestation_trends?: {
    computed_at?: string | null;
    bucket_mode?: string | null;
    per_category?: Record<string, {
      false_attestation_rate?: number | null;
      previous_false_attestation_rate?: number | null;
      delta?: number | null;
      trend?: string | null;
      ci_confirmed?: number | null;
      false_attested?: number | null;
      ci_data_missing?: number | null;
    }> | null;
  } | null;

  // ----- prediction calibration (dashboard) -----
  prediction_calibration_factor?: number | null;
  prediction_calibration_note?: string | null;
  prediction_calibration_sample_count?: number | null;

  // ----- human rejection patterns -----
  rejection_pattern_summary?: {
    total_rejected: number;
    top_notes: { snippet: string; count: number }[];
  } | null;

  // ----- memo pathway -----
  tasks_truncated?: boolean;
  memo_pathway_exits?: number | null;
  memo_ever_cited?: { ever_cited: boolean; first_cycle_id: string | null; first_cited_at: string | null } | null;
  recent_cycle_memo_stats?: Array<{ cycle_id: string | null; started_at: string | null; memos_available: number; memos_cited: number }> | null;
}

/**
 * Type guard for the `/api/admin/digest` response. Checks the fields the
 * dashboard requires so any backend rename or removal is caught at fetch time
 * rather than silently rendering stale or missing data.
 *
 * Generic in the `direction_progress` element type so callers can narrow to
 * their own `DigestData<DP>` without a cast; the runtime check only validates
 * that `direction_progress` is an array.
 */
export function assertDigestContract<DP = unknown>(data: unknown): data is DigestData<DP> {
  if (typeof data !== 'object' || data === null) return false;
  const d = data as Record<string, unknown>;
  return (
    typeof d.failed_count === 'number' &&
    typeof d.pending_approval_count === 'number' &&
    Array.isArray(d.direction_progress) &&
    Array.isArray(d.recent_completions) &&
    Array.isArray(d.failure_groups)
  );
}
