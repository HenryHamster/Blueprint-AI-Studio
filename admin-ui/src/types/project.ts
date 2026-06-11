export interface Direction {
  id: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
  added_at: string;
  status: 'active' | 'completed' | 'paused' | 'proposed';
  paths?: string[];
  last_researched_at?: string | null;
  refractory_until?: string | null;
  refractory_reason?: 'standard' | 'saturation' | null;
  refractory_remaining_minutes?: number | null;
  proposed_by?: string;
  proposal_rationale?: string;
  proposed_at?: string;
  pending_update?: {
    suggested_text?: string;
    suggested_priority?: string;
    suggested_paths?: string[];
    rationale: string;
    proposed_at: string;
    proposed_by: string;
  };
}

export interface BuildConfigDirectory {
  path: string;
  type: string;
  install_cmd?: string;
  lint_cmd?: string;
  build_cmd?: string;
  test_cmd?: string;
}

export interface ResourceEnvelopeConfig {
  max_concurrent_vms?: number | null;
  spend_ceiling_usd?: number | null;
  spend_window_days?: number;
}

export interface BuildConfig {
  language?: string | null;
  directories?: BuildConfigDirectory[];
  git_user_name?: string;
  git_user_email?: string;
  env_vars?: Record<string, string>;
  detected_at?: string;
  enable_fresh_reviewer?: boolean;
  enable_parallel_review?: boolean;
  enable_parallel_fix?: boolean;
  task_branch_namespace?: string;
  task_branch_prefix?: string;
  pr_title_prefix?: string;
  staging_branch_prefixes?: string[] | null;
  /** Whether the repository has required CI checks. Default/undefined means CI-gated
   *  (the merge gate waits for required checks). False means the repo has no required
   *  CI checks and a definitive absence of checks is treated as "CI not applicable". */
  requires_ci_checks?: boolean;
  /** Tri-state CI-presence signal derived from `.github/workflows` during build-config
   *  detection. `true` = a GitHub Actions workflow file was found (merges will be CI-gated);
   *  `false` = the repo walk completed and found none (no CI — PRs will stall unless
   *  `requires_ci_checks` is turned off); `null`/undefined = undetected/legacy. Read-only:
   *  detection + guidance only, never auto-flips `requires_ci_checks`. */
  has_ci?: boolean | null;
  /** Per-project parallelism overrides (1..50). Undefined/null = use the global
   *  runtime settings (`max_parallel_research_per_project`,
   *  `max_total_research_per_project`, `max_autonomous_tasks_per_hour`). */
  max_parallel_research?: number | null;
  max_total_research?: number | null;
  max_autonomous_tasks_per_hour?: number | null;
  resource_envelope?: ResourceEnvelopeConfig | null;
  /** ISO timestamp stamped when a repo_bootstrap task finishes successfully.
   *  Absent/null means bootstrap has not yet completed. */
  bootstrap_completed_at?: string | null;
  /** True when detection determined the repository has no committed content yet. */
  repo_is_empty?: boolean;
  /** Reason string stamped when a repo_bootstrap task completes but without
   *  fully succeeding (e.g. partial failure). When set and bootstrap_completed_at
   *  is absent, the project bootstrap is in a degraded state. */
  bootstrap_degraded_reason?: string | null;
  bootstrap_recovery_attempts?: number | null;
  bootstrap_recovery_state?: 'backoff' | 'exhausted' | 'operator_retry' | 'completed' | string | null;
  bootstrap_recovery_next_at?: string | null;
  bootstrap_recovery_last_degraded_at?: string | null;
  bootstrap_recovery_exhausted_at?: string | null;
  /** ISO timestamp stamped when a refresh bootstrap completed and replaced the
   *  prior repo_overview. Absent/null means no refresh has run yet. */
  bootstrap_overview_refreshed_at?: string | null;
  /** Branch the refreshed overview describes (set alongside bootstrap_overview_refreshed_at). */
  bootstrap_overview_source_branch?: string | null;
  /** Commit SHA the refreshed overview was captured at (optional; may be absent). */
  bootstrap_overview_source_revision?: string | null;
}

export interface StagingBranchPrefixesConfig {
  staging_branch_prefixes: string[];
  is_default: boolean;
}

/** A directory path paired with the test command the no-CI merge gate would run there. */
export interface MergeVerificationTestCommand {
  path: string;
  test_cmd: string;
}

/**
 * Read-only, backend-computed verdict describing what Blueprint's merge gate will
 * do for a project's normal PRs — the single source of truth the UI renders so it
 * can never drift from the actual gate. Attached as a sibling of `build_config` on
 * the onboard, project-detail, list, update, and re-detect responses.
 *
 *  - `ci_checks`    — merges wait for the repository's required GitHub CI checks.
 *  - `tests_gated`  — `requires_ci_checks` is off AND a test command was detected;
 *                     each `test_commands` entry runs on the PR head as the verdict.
 *  - `unverified`   — `requires_ci_checks` is off and no test command was detected;
 *                     PRs merge with no test verification at all.
 *
 * `test_commands` is non-empty only for the `tests_gated` state.
 */
export interface MergeVerificationVerdict {
  state: 'ci_checks' | 'tests_gated' | 'unverified';
  test_commands: MergeVerificationTestCommand[];
}

/** One retained entry in the no-CI merge gate's bounded rolling outcome history.
 *  `verified` is true when the gate ran real tests; false when it fell open to an
 *  UNVERIFIED merge (in which case `reason` carries the fall-open reason key). */
export interface MergeGateOutcome {
  at: string;
  verified: boolean;
  reason?: string | null;
}

/** Derived at-a-glance aggregate over the retained outcome window. `window` is the
 *  number of entries retained; `verified_runs` + `fell_open_runs` partition it;
 *  `total_fall_opens` is the all-time tally (can exceed the window once older
 *  entries are evicted); `most_common_reason` is the most-tallied fall-open reason
 *  key (or null when there have been no fall-opens). */
export interface MergeGateSummary {
  window: number;
  verified_runs: number;
  fell_open_runs: number;
  total_fall_opens: number;
  most_common_reason?: string | null;
}

export interface MergeGateRuntime {
  last_run_at?: string | null;
  executed_real_tests?: boolean;
  last_unverified_at?: string | null;
  last_unverified_reason?: string | null;
  /** Additive history fields (backend `ci_resolution.merge_gate_runtime_block`).
   *  Omitted on never-ran projects and legacy four-key-only records, so every
   *  consumer must guard reads — absence renders exactly as before. */
  recent_outcomes?: MergeGateOutcome[];
  unverified_reason_counts?: Record<string, number>;
  summary?: MergeGateSummary;
}

export interface CapabilityProofSummary {
  proof_count: number;
  latest_proof_kind?: string;
  latest_proved_at?: string;
  latest_commit_sha?: string;
}

export interface CapabilityAdoptionVerdict {
  verdict?: 'adopt' | 'reject' | 'continue-trial' | 'ADOPT' | 'KILL' | 'CONTINUE';
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
  immediate_alert?: boolean;
  window_hours?: number | null;
  evaluated_at?: string;
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
  };
  verdict_reason?: string;
  accumulation_basis?: string;
  arm_composition?: { with_assigned?: number; with_receipt?: number; without_assigned?: number; without_receipt?: number };
}

export interface ProjectResourceEnvelopeResponse extends ResourceEnvelopeConfig {
  project_id: string;
  active_vm_count: number;
  recent_spend_usd: number;
}

export interface RepoOverview {
  tech_stack?: string | null;
  directory_layout?: string | null;
  first_steps_summary?: string | null;
  entry_points?: string | null;
  test_layout?: string | null;
  key_conventions?: string | null;
  environment_requirements?: string | null;
}

export interface Project {
  id: string;
  name: string;
  status: string;
  github_owner: string;
  github_repo: string;
  autonomous_enabled: boolean;
  /** Owner-toggled pause switch. When true, the orchestrator dispatches no new
   *  QUEUED work for the project. Optional/additive: absent on older responses,
   *  which are treated as not-paused. Distinct from lifecycle `status` and from
   *  the `autonomous_enabled` toggle. */
  paused?: boolean;
  /** True when the project has been archived (hidden from the active list). Present on list endpoint. */
  archived?: boolean;
  /** Full directions array — present on detail endpoint; absent on list endpoint. */
  directions?: Direction[];
  /** Count of active directions — present on list endpoint. */
  directions_count?: number;
  created_at: string;
  my_role?: 'owner' | 'member' | 'viewer';
  build_config?: BuildConfig | null;
  /** Backend-computed merge-gate verdict (sibling of `build_config`). Present on
   *  the list and detail endpoints; optional/additive so older responses omit it. */
  merge_verification_verdict?: MergeVerificationVerdict;
  /** Optional runtime evidence for the live no-CI merge gate. Omitted on older responses
   *  and on brand-new projects that have never exercised the gate. */
  merge_gate_runtime?: MergeGateRuntime;
  is_self_repo?: boolean;
  sr_catch_rate_targets?: Record<string, number>;
  /** Per-capability proof summary (proof count and latest entry details).
   *  Present on detail endpoint; absent on list endpoint. */
  capability_proof_summary?: Record<string, CapabilityProofSummary>;
  /** Durable adopt-or-reject verdicts for capability trials.
   *  Present on detail endpoint; absent on list endpoint. */
  capability_adoption_verdicts?: Record<string, CapabilityAdoptionVerdict>;
  /** Design doc produced by the bootstrap agent. `repo_overview` holds the
   *  structured summary of the repository (tech stack, layout, first steps).
   *  Present on detail endpoint when a bootstrap has completed. */
  design_doc?: { repo_overview?: RepoOverview } | null;
}

export interface AutoRemediationConfig {
  enabled: boolean;
  branch_whitelist: string[];
  max_per_hour: number;
  max_concurrent: number;
  require_review: boolean;
}

export interface AutoRemediationRecentTask {
  id: string;
  title: string;
  status: string;
  branch: string;
  created_at: string | null;
}

export interface AutoRemediationStats {
  spawned_24h: number;
  spawned_7d: number;
  success_rate: number | null;
  recent_tasks: AutoRemediationRecentTask[];
}

export interface IssueImplementationConfig {
  enabled: boolean;
  trigger_label: string;
  max_per_hour: number;
  max_concurrent: number;
  /** Live-derived from project.integration_branch. Returned by GET; rejected by PATCH. */
  readonly base_branch: string;
  default_effort: 'low' | 'medium' | 'high';
}

/** Fields admin-ui may PATCH on issue-implementation config — base_branch excluded (server-derived). */
export type IssueImplementationConfigUpdate = Omit<IssueImplementationConfig, 'base_branch'>;

export interface AutoReviewConfig {
  enabled: boolean;
  max_per_hour: number;
  max_concurrent: number;
}

export interface ProjectMemberEntry {
  id: string;
  user_id: string;
  email?: string;
  role: 'owner' | 'member' | 'viewer';
  created_at: string | null;
}
