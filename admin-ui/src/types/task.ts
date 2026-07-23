import type { TaskType } from '../taskCapabilities';
import type { ProjectMessagePreview } from './messages';

export type TaskStatus = 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'BLOCKED' | 'PARKED' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type { TaskType };

type WakeDeliveryState = 'PENDING' | 'FIRED' | 'DELIVERED' | 'EXPIRED' | 'FAILED';

export interface TaskWakeRegistration {
  wake_source_kind: string;
  wake_source_ref?: string | null;
  delivery_state: WakeDeliveryState;
  deadline_at?: string | null;
  fired_at?: string | null;
  result_state?: string | null;
  result_detail?: string | null;
}

export interface FindingChallenge {
  challenged_boundary?: string;
  first_verdict?: string;
  second_opinion_verdict?: string;
  agreement?: 'agrees' | 'disagrees' | 'indeterminate' | string;
  reason?: string;
  sidecar_task_id?: string;
}

export interface ResearchFinding {
  type?: string;
  title: string;
  description: string;
  prompt: string;
  scope: 'small' | 'medium' | 'large';
  risk: 'low' | 'medium' | 'high';
  priority: 'high' | 'medium' | 'low';
  related_files: string[];
  direction_id?: string;
  claims?: string[];
  sources?: Array<{ type?: string; ref: string; excerpt?: string }>;
  _provenance_warnings?: { broken_source_ref_count: number; broken_refs: string[] };
  finding_hash?: string;
  finding_challenge?: FindingChallenge;
}

interface TaskEvaluationSummaryItem {
  label: string;
  value: string;
}

export interface TaskEvaluationSummary {
  status: string;
  verdict: string;
  evidence?: TaskEvaluationSummaryItem[];
  highlights?: TaskEvaluationSummaryItem[];
}


export interface TaskArtifact {
  artifact_type: string;
  pr_url?: string | null;
  commit_hash?: string | null;
  repo_url?: string | null;
  storage_uri?: string | null;
  logs?: string | null;
  iteration: number;
  created_at?: string | null;
}

export interface TaskSideboardContext {
  task?: {
    id: string;
    title?: string | null;
    status: string;
    task_type?: string | null;
    latest_status_message?: string | null;
    blocker?: {
      kind?: string | null;
      reason?: string | null;
      blocked_at?: string | null;
      recheck_after?: string | null;
      parked_at?: string | null;
    };
    runner?: {
      vm_instance_id?: string | null;
      runner_target_id?: string | null;
      // Operator/agent-requested runner pin from the launch contract
      // (task.context.preferred_runner_target_id). Set even before dispatch.
      preferred_runner_target_id?: string | null;
      // Present while a strict runner pin is holding dispatch: the
      // machine-readable reason the pinned runner cannot take the task.
      // reason_code is one of offline | not_enrolled | not_found | at_capacity |
      // missing_gpu | missing_container_runtime | task_type_not_allowed | not_eligible.
      runner_pin_hold?: {
        runner_id?: string | null;
        strict?: boolean | null;
        reason_code?: string | null;
        detail?: string | null;
      } | null;
      machine_type?: string | null;
      spot?: boolean | null;
      warm_start?: boolean | null;
      provider?: string | null;
      account_id?: string | null;
    };
    cost?: {
      estimated_vm_usd?: number | null;
      llm_usd?: number | null;
      llm_any_unpriced?: boolean | null;
    };
    next_action?: {
      label: string;
      reason?: string | null;
      target?: {
        surface: string;
        section?: string | null;
        id?: string | null;
        task_id?: string | null;
        url?: string | null;
      } | null;
    };
  };
  project: {
    id: string;
    name?: string | null;
    repo?: string | null;
  };
  purpose?: string | null;
  strategic: {
    direction: {
      id: string;
      text?: string | null;
      priority?: string | null;
      status?: string | null;
    } | null;
    cycle: {
      id: string;
      phase: 'design_doc_update' | 'strategic_review' | string | null;
      outcome: string;
    } | null;
    parent_strategic_task_id?: string | null;
  };
  pipeline: {
    parent_task_id?: string | null;
    child_count: number;
    dependency_count: number;
    blocked_reason?: string | null;
    movement_explanation?: string | null;
  };
  artifacts: {
    total: number;
    by_type: Record<string, number>;
    primary_pr_url?: string | null;
    primary_commit?: string | null;
  };
  messages: {
    total_recent: number;
    items: ProjectMessagePreview[];
  };
  links: Array<{
    id: string;
    label: string;
    target: {
      surface: string;
      section?: string | null;
      id?: string | null;
      task_id?: string | null;
      direction_id?: string | null;
      url?: string | null;
    };
  }>;
}

export interface Task {
  id: string;
  project_id?: string;
  title: string;
  description: string;
  status: TaskStatus;
  task_type?: TaskType;
  vm_instance_id?: string;
  latest_status_message?: string;
  movement_explanation?: string | null;
  status_log?: { t: string; msg: string }[];
  created_by: string;
  requires_approval: boolean;
  parent_task_id?: string;
  depends_on?: string[];
  plan_output?: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  last_heartbeat?: string;
  attempt_count?: number;
  // Durable blocker metadata for the non-terminal BLOCKED state (backend
  // alembic 0137 / models.py). All four are nullable — older task payloads
  // (and any non-BLOCKED task) carry null/undefined, so every consumer must
  // treat these as optional and guard on presence before rendering.
  blocker_kind?: string | null;
  blocked_reason?: string | null;
  blocked_at?: string | null;
  recheck_after?: string | null;
  // Durable operator-visible metadata for the non-terminal PARKED state (backend
  // alembic 0187 / models.py). Nullable — older payloads and any non-PARKED task
  // carry null/undefined, so guard on presence before rendering.
  parked_at?: string | null;
  park_reason?: string | null;
  wake_registration?: TaskWakeRegistration | null;
  // R3-W2 (H1): heartbeat-hot fields promoted from Task.context to dedicated
  // columns in Round 2 (B2 / F). The frontend reads from the columns; the
  // legacy `task.context.last_metrics` mirror was retired and reading it
  // silently breaks the System Metrics panel.
  last_metrics_json?: {
    rss_mb?: number;
    heap_used_mb?: number;
    disk_free_mb?: number;
    disk_total_mb?: number;
    cgroup_mb?: number;
    cgroup_limit_mb?: number;
  } | null;
  last_tool_use_at?: string | null;
  // Slim, read-only mirror of the no-CI merge gate's UNVERIFIED-merge reason
  // (backend admin.py mirrors review_loop["unverified_merge_reason"]). Null /
  // absent for CI-verified, tests-gated, or otherwise normal merges. The Task
  // Detail drawer's Merge Test Gate section reads this to render the UNVERIFIED
  // verdict + human-readable reason when no MERGE_TEST_RESULT artifact exists.
  unverified_merge_reason?: string | null;
  evaluation_summary?: TaskEvaluationSummary | null;
  payloads?: Record<string, unknown>[];
  artifacts?: TaskArtifact[];
  pr_url?: string;
  commit_hash?: string;
  repo_url?: string;
  error_logs?: string;
  failure_class?: string | null;
  research_findings?: ResearchFinding[] | null;
  context?: {
    fix_loop_abandoned?: { loop: string; exit_reason: string; at: string };
    teardown_forensics?: {
      reason: string;
      serial_tail: string;
      captured_at: string;
      vm_instance_id: string;
      zone?: string | null;
    };
    // Spawn-gate provenance — set on research-derived child tasks by research_completion.py
    research_task_id?: string;
    finding_index?: number;
    research_spawn_gate_challenged?: boolean;
    research_spawn_gate_verdict?: 'pass' | null;
    research_spawn_gate_explicit?: boolean | null;
    research_spawn_gate_reason?: string | null;
    finding_hash?: string;
    finding_challenge?: {
      challenged_boundary?: string;
      first_verdict?: string | null;
      second_opinion_verdict?: string | null;
      agreement?: string | null;
      reason?: string | null;
      sidecar_task_id?: string | null;
    };
    // Aggregate gate counts and deferred findings — set on the PARENT research task
    deferred_findings?: Array<{
      finding_hash?: string;
      title?: string;
      deferred_reason?: string;
      adversarial_gate_reason?: string | null;
      adversarial_challenger_task_id?: string | null;
    }>;
    // Bounded provenance for the last adversarial challenger — written by
    // _resolve_quality_grader_completion in task_lifecycle.py when the adversarial
    // grader completes or fails.
    adversarial_gate_diagnostic?: AdversarialGateDiagnostic | null;
    research_spawn_gate_challenged_count?: number;
    research_spawn_gate_refuted_count?: number;
    research_spawn_gate_survived_count?: number;
    // Decision-quality breakdown of the inclusive survived/refuted buckets
    // (backend research_completion.py / direction_outcomes.py). All optional —
    // older parent-task payloads omit them, so every consumer must tolerate
    // their absence.
    research_spawn_gate_explicit_pass_count?: number;
    research_spawn_gate_fail_open_count?: number;
    research_spawn_gate_unresolved_count?: number;
    research_spawn_gate_refuted_reasons?: string[];
    research_spawn_gate_manifest_excluded_fail_closed_count?: number;
    research_spawn_gate_manifest_excluded_fail_closed_samples?: Array<{
      hash?: string;
      title?: string;
      reason?: string;
    }>;
    research_spawn_gate_challenged_spawned?: number;
    research_spawn_gate_challenged_merged?: number;
    research_spawn_gate_unchallenged_spawned?: number;
    research_spawn_gate_unchallenged_merged?: number;
    // Durable acceptance-gate verdict stamped by acceptance_gate.py at completion.
    // Absent for tasks that declared no acceptance evidence (legacy + undeclared).
    // When present: kind mirrors AcceptanceEvidence.kind, proven is stricter than
    // satisfied (machine-checked proof vs merely-delivered work).
    code_acceptance_verdict?: {
      satisfied: boolean;
      proven: boolean;
      kind: string | null;
      reason: string;
      consumed?: boolean;
      consumed_by_task_id?: string;
    };
    [key: string]: unknown;
  };
  history?: { timestamp: string; action: string; details: Record<string, unknown> }[];
  child_task_ids?: string[];
  estimated_cost_usd?: number | null;
  vm_machine_type?: string | null;
  vm_spot?: boolean;
  vm_cold_start_ms?: number | null;
  vm_warm_start?: boolean | null;
  task_llm_cost_usd?: number | null;
  task_llm_cost_any_unpriced?: boolean;
  api_call_summary?: {
    llm_call_count?: number;
    total_input_tokens?: number;
    total_output_tokens?: number;
    last_updated_at?: string;
  } | null;
  fix_loop_history?: Record<string, FixLoopAttempt[]> | null;
  fix_loop_summary?: FixLoopSummary[] | null;
  plan_subtasks?: Array<{ title: string; depends_on_indices?: number[] }> | null;
  edges_considered?: EdgeConsidered[] | null;
  // Transitional state: agent run completed but post-commit side effects
  // (PR submission, child task spawns, merge) haven't fired yet.
  post_commit_pending?: boolean | null;
  sideboard_context?: TaskSideboardContext | null;
}

export interface EdgeConsidered {
  case: string;
  handled: boolean;
  evidence?: string;
  blind_spot?: string;
}

export interface FixLoopSummary {
  loop: string;
  iterations: number;
  last_exit_reason: string | null;
  non_success: boolean;
}

export interface FixLoopAttempt {
  loop: string;
  iteration: number;
  fingerprint_hash?: string | null;
  error_tail?: string | null;
  change_summary?: string | null;
  exit_reason?: string | null;
  source?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
}

/**
 * Bounded provenance recorded on the source research task after an adversarial
 * challenger returns (or fails). Lets operators root-cause absent verdicts
 * (stale serving image vs malformed/crashed grader) without storing raw logs.
 * All VM identity fields are optional — they are absent when the grader ran on
 * a local/subprocess worker that never sent a heartbeat.
 */
export interface AdversarialGateDiagnostic {
  challenger_task_id: string;
  completion_state: 'completed' | 'failed';
  challenged_count: number;
  parse_state?: 'ok' | 'challenger_failed' | 'empty_logs' | 'no_json_array' | 'invalid_json' | 'no_hash_matches';
  parse_cause?: string | null;
  parsed_verdict_count: number;
  unresolved_count: number;
  vm_image_name?: string | null;
  vm_image_build_ts?: number | null;
  vm_runtime_fingerprint?: string | null;
  vm_source_sha?: string | null;
}

/**
 * Aggregate research spawn-gate telemetry rolled up across a direction's
 * research cycles. Two backend producers emit nearly-identical shapes:
 *  - the digest direction-progress entry's `research_spawn_gate` payload
 *    (backend digest_direction_progress.py) uses the `*_count` count keys; and
 *  - the strategist's `spawn_gate_metrics` (compute_spawn_gate_aggregate) uses
 *    the bare `challenged`/`refuted`/`survived` keys.
 * This interface accepts BOTH spellings so a single UI type can read either; all
 * fields are optional so older payloads (which carry only the legacy
 * challenged/refuted/survived counts and merge rates) still render.
 */
export interface ResearchSpawnGateAggregate {
  // Legacy inclusive counters — digest (`*_count`) and strategist (bare) spellings.
  challenged_count?: number;
  refuted_count?: number;
  survived_count?: number;
  challenged?: number;
  refuted?: number;
  survived?: number;
  // Decision-quality breakdown: explicit challenger pass vs fail-open/no-verdict
  // pass (both subsets of survived) and unresolved/no-verdict (subset of refuted).
  explicit_pass_count?: number;
  fail_open_count?: number;
  unresolved_count?: number;
  manifest_excluded_fail_closed_count?: number;
  manifest_excluded_fail_closed_samples?: Array<{
    hash?: string;
    title?: string;
    reason?: string;
  }>;
  // Preserved merge-rate sample sizes so a shown rate always travels with its
  // spawned/merged denominators.
  challenged_spawned?: number;
  challenged_merged?: number;
  challenged_merge_rate?: number | null;
  unchallenged_spawned?: number;
  unchallenged_merged?: number;
  unchallenged_merge_rate?: number | null;
  // Compact, deduped, capped sample of refuted/unresolved challenger reasons —
  // a degraded-gate signal the strategist/operator can act on without opening
  // individual tasks.
  refuted_reasons?: string[];
  // Compact parse-outcome histogram rolled up from research cycle records.
  parse_outcome_distribution?: Record<string, number>;
}

/** Discriminated union of spawn-gate outcomes for a research-derived child task. */
export type SpawnGateState =
  | { kind: 'no-gate' }
  | { kind: 'unchallenged' }
  | { kind: 'challenged-pass'; reason: string | null }
  | { kind: 'fail-open-pass'; reason: string | null };

/** Derive the spawn-gate state for a (possibly research-derived) task from its context. */
export function deriveSpawnGateState(context: Task['context']): SpawnGateState {
  if (!context?.research_task_id) return { kind: 'no-gate' };
  if (!context.research_spawn_gate_challenged) return { kind: 'unchallenged' };
  const reason = (context.research_spawn_gate_reason as string | null | undefined) ?? null;
  if (context.research_spawn_gate_explicit === true) return { kind: 'challenged-pass', reason };
  return { kind: 'fail-open-pass', reason };
}

export interface SerialConsoleResponse {
  vm_instance_id: string;
  zone: string | null;
  vm_status: string | null;
  serial_log: string | null;
  captured_at: string;
  source: 'live' | 'cached';
}

export type TrackLogType = 'log' | 'status' | 'artifact' | 'error' | 'system';

export interface TrackLogEntry {
  id: number;
  t: string;
  type: TrackLogType;
  msg: string;
}
