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
  spend_warning_fraction?: number | null;
  max_concurrent_compute_jobs?: number | null;
  /** Hardware policy: if set, CPU compute jobs must use one of these machine
   *  types. `null`/undefined means unrestricted (any machine type). */
  allowed_machine_types?: string[] | null;
  /** Hardware policy: if set, GPU jobs must use one of these accelerator types.
   *  `null`/undefined means unrestricted; an empty list `[]` blocks all GPU jobs. */
  allowed_accelerator_types?: string[] | null;
  /** When true, this project's compute experiments only run on registered
   *  self-hosted runners and never fall back to billable cloud compute. With no
   *  live runner, experiments are held until one comes online (and time out after
   *  a few hours rather than wedging). `false`/undefined means cloud fallback is
   *  allowed (the default). */
  require_local_runner?: boolean | null;
  /** Placement for all executable work. `local_only` never falls back to
   *  managed execution; `local_preferred` uses local capacity first; and
   *  `managed_allowed` permits either placement. Absent preserves legacy
   *  behavior, including the narrower `require_local_runner` flag. */
  execution_placement_policy?: 'local_only' | 'local_preferred' | 'managed_allowed' | null;
  /** When true, exceeding the spend ceiling automatically pauses the project
   *  (project.paused = True) until an operator manually unpauses it. `false`/
   *  undefined means soft-defer tasks but don't pause the project (the default). */
  pause_on_ceiling_exceeded?: boolean;
}

export interface BuildConfig {
  language?: string | null;
  directories?: BuildConfigDirectory[];
  git_user_name?: string;
  git_user_email?: string;
  env_vars?: Record<string, string>;
  detected_at?: string;
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
  /** ISO timestamp stamped when domain knowledge was last captured/refreshed
   *  during a bootstrap. Absent/null means domain knowledge extraction has not
   *  yet run for this project. */
  bootstrap_domain_knowledge_refreshed_at?: string | null;
  /** Branch the refreshed overview describes (set alongside bootstrap_overview_refreshed_at). */
  bootstrap_overview_source_branch?: string | null;
  /** Commit SHA the refreshed overview was captured at (optional; may be absent). */
  bootstrap_overview_source_revision?: string | null;
  /** Outcome of the repo_bootstrap command write-back, recorded independently of
   *  the repo overview. `confirmed` = commands written back and verified;
   *  `missing_commands` = overview produced but no commands to confirm;
   *  `writeback_failed` = commands existed but the write-back did not land;
   *  `unknown` = a legacy agent omitted the status payload. Absent on projects
   *  that predate the command-memory status contract. */
  bootstrap_command_memory_status?:
    | 'confirmed'
    | 'missing_commands'
    | 'writeback_failed'
    | 'unknown'
    | string
    | null;
  /** True only when the command write-back was confirmed (status === 'confirmed').
   *  False means the command memory is partial/degraded even if an overview exists. */
  bootstrap_command_memory_complete?: boolean | null;
  /** Human-readable detail explaining a non-confirmed command-memory outcome. */
  bootstrap_command_memory_detail?: string | null;
  /** Paths written back during command confirmation, when any landed. */
  bootstrap_command_memory_paths?: string[] | null;
  /** ISO timestamp stamped when the command-memory status was last recorded. */
  bootstrap_command_memory_updated_at?: string | null;
}

/** A directory path paired with the test command the no-CI merge gate would run there. */
interface MergeVerificationTestCommand {
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
interface MergeGateOutcome {
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
  /** Honest-block fields — present only when the project has opted into the
   *  `block_degraded_unverified_merge` policy AND a block event has occurred.
   *  Absent on legacy records and projects where the policy has never fired. */
  last_degraded_block_at?: string | null;
  last_degraded_block_reason?: string | null;
  degraded_block_counts?: Record<string, number>;
  /** No-test-command hold fields — present only when the project has opted into
   *  the `block_no_test_unverified_merge` policy AND a hold event has occurred.
   *  Absent on legacy records and projects where the policy has never fired. */
  last_no_test_block_at?: string;
  last_no_test_block_reason?: string;
  no_test_block_counts?: Record<string, number>;
}

interface CapabilityProofSummary {
  proof_count: number;
  latest_proof_kind?: string;
  latest_proved_at?: string;
  latest_commit_sha?: string;
}

interface CapabilityAdoptionVerdict {
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
  recent_spend_compute_usd: number;
  recent_spend_agent_vm_usd: number;
  recent_spend_idle_usd: number;
  is_throttled: boolean;
  /** Whether execution_placement_policy was explicitly stored rather than
   *  resolved to its legacy default. */
  execution_placement_policy_is_explicit?: boolean;
  /** True only for the old compute-experiment-only local-runner setting when
   *  no broad placement policy is active. */
  legacy_require_local_runner_active?: boolean;
}

/**
 * Per-project task-status rollup returned by GET /api/admin/projects
 * (`items[].task_status_counts`). Covers EVERY task in the project, not just the
 * current paginated task page, so operator surfaces can render project-wide
 * truth instead of a page slice. Additive — absent on legacy payloads, so every
 * read must guard for `undefined`.
 */
interface TaskStatusCounts {
  queued: number;
  starting: number;
  running: number;
  evaluating: number;
  blocked: number;
  parked: number;
  failed: number;
  completed: number;
  cancelled: number;
  total: number;
}

interface RepoOverview {
  tech_stack?: string | null;
  directory_layout?: string | null;
  first_steps_summary?: string | null;
  entry_points?: string | null;
  test_layout?: string | null;
  key_conventions?: string | null;
  environment_requirements?: string | null;
  provenance?: {
    refreshed_at?: string | null;
    source_branch?: string | null;
    source_revision?: string | null;
  } | null;
}

export interface OnboardingProof {
  repo_added_at?: string | null;
  bootstrap_completed_at?: string | null;
  first_task_spawned_at?: string | null;
  first_pr_opened_at?: string | null;
  first_merge_gate_verdict_at?: string | null;
  first_merge_gate_executed_tests?: boolean | null;
  first_merge_gate_unverified_reason?: string | null;
  first_pr_merged_at?: string | null;
  first_autonomous_improvement_at?: string | null;
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
  /** Project-wide task-status rollup from GET /api/admin/projects. Covers every
   *  task in the project (not the current task page), so operator surfaces can
   *  render project-scoped truth. Additive — absent on legacy payloads. */
  task_status_counts?: TaskStatusCounts;
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
  /** Durable onboarding-proof milestone timeline for externally-onboarded repos.
   *  Present on detail endpoint; absent on list endpoint and self-repo projects.
   *  All milestone fields are nullable — null means the event has not yet occurred. */
  onboarding_proof?: OnboardingProof | null;
}

export interface AutoRemediationConfig {
  enabled: boolean;
  branch_whitelist: string[];
  max_per_hour: number;
  max_concurrent: number;
  require_review: boolean;
}

interface AutoRemediationRecentTask {
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
  user_id: string | null;
  email?: string;
  role: 'owner' | 'member' | 'viewer';
  status?: 'pending' | 'active';
  created_at: string | null;
}
