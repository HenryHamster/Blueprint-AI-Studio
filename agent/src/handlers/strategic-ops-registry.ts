/**
 * SINGLE SOURCE OF TRUTH for the strategist's mutating operation surface.
 *
 * The strategist's mutating ops used to be advertised in several
 * hand-maintained lists that drifted out of sync:
 * the backend dispatch chain (routers/admin_agent_ops.py `strategic_operation`),
 * the generated `./strategic` CLI script (strategic-helpers.ts), and the
 * strategist bootstrap prompt op list. This ordered registry
 * is the ONE place the op surface is declared; the CLI `case`/usage string and
 * the bootstrap prompt op list are DERIVED from it (no more hand-typed lists),
 * and a checked-in shared manifest (`backend/app/strategic_ops_manifest.json`)
 * mirrors it so the Python backend and the TypeScript agent stay in lockstep.
 * After editing this registry, REGENERATE that manifest:
 *   cd agent && npx tsx scripts/regen-strategic-ops-manifest.mjs
 *
 * Drift-guard tests keep every surface honest:
 *  - agent-side (strategic-ops-registry.test.ts): the generated CLI + bootstrap
 *    prompt list EXACTLY the registry's cli/prompt-flagged aliases, and the
 *    registry equals the shared JSON manifest.
 *  - backend-side (test_strategic_ops_registry.py): the dispatch chain accepts
 *    EXACTLY the registry's backend op names — every registry op has a live
 *    handler, and every accepted op is in the registry (no silent additions).
 *
 * This module is intentionally pure and dependency-free (no `../index.js`, no
 * `fs`/`ws` imports) so any consumer and the helper generator can
 * import it without pulling in the heavy agent surface.
 */

export interface StrategicOpEntry {
  /** Backend op name accepted by POST /api/agent/strategic (e.g. `stage_direction`). */
  op: string;
  /** CLI alias exposed by the `./strategic` script (e.g. `stage-direction`), or
   *  null for backend ops that are intentionally NOT surfaced on the CLI. */
  alias: string | null;
  /** One-line description of what the op does. */
  description: string;
  /** True when the op appears in the generated `./strategic` CLI (case + usage). */
  cli: boolean;
  /** True when the op is advertised in the strategist bootstrap prompt op list. */
  prompt: boolean;
  /** For ops with cli=false/prompt=false: WHY they are intentionally excluded
   *  (internal-only / auto-invoked). Null for surfaced ops. Marks exclusion
   *  explicitly rather than by omission. */
  internalReason: string | null;
  /**
   * A compact, machine-readable input contract used by the generated helper's
   * offline `help` and `schema` commands.  Runtime validation remains owned by
   * the backend; this is deliberately a discoverability surface, not a second
   * validator.
   */
  input?: StrategicOpInputContract;
}

export interface StrategicOpInputContract {
  /** JSON is the normal transport. `markdown` accepts a raw --file/--stdin. */
  kind: 'json' | 'markdown';
  /** Required top-level fields for the canonical JSON form. */
  required?: readonly string[];
  /** Accepted top-level fields when the operation has a narrow envelope. */
  fields?: readonly string[];
  /** A valid, deliberately small example used by `strategic schema <op>`. */
  example?: Record<string, unknown>;
  /** Compatibility detail worth surfacing before an agent learns it by 400. */
  notes?: readonly string[];
}

const jsonInput = (): StrategicOpInputContract => ({ kind: 'json' });
const markdownInput = (): StrategicOpInputContract => ({
  kind: 'markdown',
  fields: ['markdown'],
  example: { markdown: '# Current status\n...' },
  notes: ['Use --file for markdown. --stdin accepts raw markdown. --json accepts {"markdown":"..."}.'],
});

const INPUTS = {
  set_problem_model: {
    kind: 'json' as const,
    notes: ['Legacy alias for patch-problem-model. Omitted fields are preserved; use replace-problem-model only for bootstrap/recovery.'],
  },
  patch_problem_model: {
    kind: 'json' as const,
    required: ['base_revision', 'patch'],
    fields: ['base_revision', 'patch', 'transient_actions', 'dry_run'],
    example: { base_revision: 4, patch: { current_focus: 'Validate the local runner' } },
    notes: ['Patch preserves omitted fields. Set dry_run=true to inspect the diff without writing.'],
  },
  replace_problem_model: {
    kind: 'json' as const,
    required: ['base_revision', 'model', 'confirm_replace'],
    fields: ['base_revision', 'model', 'confirm_replace', 'dry_run'],
    example: { base_revision: 4, model: { current_focus: 'Bootstrap model' }, confirm_replace: true },
    notes: ['Reserved for bootstrap/recovery. confirm_replace must be true.'],
  },
  update_direction: {
    kind: 'json' as const,
    required: ['direction_id'],
    fields: ['direction_id', 'text', 'priority', 'status', 'gap_id', 'success_evidence', 'retirement_condition', 'rationale', 'terminal_reason', 'criterion_keys', 'recommended_mode', 'system_id', 'conflicts_with'],
    example: { direction_id: 'd-123', priority: 'high', gap_id: 'g-4' },
    notes: [
      'Pass fields directly. The legacy {direction_id, changes:{...}} wrapper remains temporarily supported.',
      'A terminal status update follows the same rationale and terminal_reason contract as retire_direction.',
    ],
  },
  create_direction: {
    kind: 'json' as const,
    required: ['text', 'success_evidence', 'retirement_condition'],
    fields: [
      'text', 'success_evidence', 'retirement_condition', 'rationale',
      'priority', 'gap_id', 'implementation_constraints', 'criterion_keys',
      'recommended_mode', 'system_id', 'conflicts_with', 'staged',
      'dedup_reasoning', 'category', 'paths',
    ],
    example: {
      text: 'Deliver a reproducible CPU-scale behavior sweep with a self-contained comparison artifact.',
      success_evidence: 'A clean-clone run produces validated results and an offline comparison artifact.',
      retirement_condition: 'The reproducible sweep and artifact are merged, or two sound trials invalidate the bet.',
      gap_id: 'g-evaluation',
    },
    notes: [
      'A direction is a durable strategic outcome or bet, not a task, file/PR shard, implementation checklist, or live status update.',
      'Creating an active direction opens a recurring autonomous research lane. Use staged=true when it must not begin yet.',
      'priority is high|medium|low; category is product|structural|mixed; recommended_mode is audit|fundamentals|ship|pause.',
      'Before creating, inspect existing and recently retired directions. Prefer updating an existing outcome and dispatch bounded one-shot work with spawn-task.',
      'Use multiple spawn-task calls under one direction to split implementation across tasks — chain them with depends_on when they share contracts or files, and leave them independent only when the work genuinely does not overlap.',
    ],
  },
  resolve_discovery: {
    kind: 'json' as const,
    required: ['direction_id', 'cycle_id', 'cycle_revision', 'action'],
    fields: ['direction_id', 'cycle_id', 'cycle_revision', 'action', 'next_bet', 'note'],
    example: {
      direction_id: 'd-pipeline-reliability',
      cycle_id: '123e4567-e89b-42d3-a456-426614174000',
      cycle_revision: 4,
      action: 'continue',
      next_bet: {
        mode: 'measure',
        claim: 'The repaired scheduler dispatches one useful research parent.',
        decision_question: 'Does one autonomous cycle complete without intervention?',
        success_evidence: 'A cycle receipt records a completed parent and decision.',
        retirement_condition: 'One sound cycle succeeds or two sound cycles fail.',
      },
      note: 'Continue with the bounded measurement proposed in the operator reply.',
    },
    notes: [
      'Use the exact cycle_id and cycle_revision from the discovery operator event; stale revisions are rejected.',
      'Use action=continue with a complete next_bet to resume that evidence-pending, parked, or operator-blocked discovery cycle.',
      'Use action=park to pause pending stronger evidence or action=close to stop the direction; omit next_bet for either.',
      'An identical replay is idempotent; changing any field after resolution is rejected.',
    ],
  },
  spawn_task: {
    kind: 'json' as const,
    required: ['type', 'title'],
    fields: [
      'type', 'title', 'prompt', 'direction_id', 'criterion_ids',
      'acceptance_evidence', 'debt_id', 'idempotency_key', 'watch',
      'watch_purpose', 'preview_only', 'command', 'hypothesis', 'spend_cap_usd',
      'deadline_seconds', 'success_criterion', 'success_criteria', 'machine_type',
      'runner_target', 'accelerator', 'vram_gb', 'min_compute_capability',
      'cwd', 'env', 'container_image', 'allow_host_fallback', 'depends_on',
      'require_deps_landed_on', 'release_base_branch',
      // Compute fields the launch payload has always accepted but this schema
      // never listed, so an agent reading `./strategic schema spawn-task` could
      // not discover them. `arms`/`declared_metric` are the multi-arm form the
      // thinker prompt already instructs the strategist to send.
      'arms', 'declared_metric', 'setup_step', 'require_durable_artifact',
      'input_artifact_job_id', 'no_progress_timeout_seconds',
    ],
    example: {
      type: 'create',
      title: 'Implement the verified pipeline repair',
      prompt: 'Apply the scoped fix and add regression coverage.',
      direction_id: 'd-pipeline-reliability',
      idempotency_key: 'pipeline-repair-v1',
    },
    notes: [
      'Use this operation for bounded implementation, research, plan, design, and compute work under an existing strategic outcome.',
      'Split implementation across multiple tasks under one direction as the work naturally divides; do not create file-, PR-, or phase-sharded directions.',
      'To author an ORDERED sequence — the norm when a successor builds on a foundation\'s contract (shared types, schema, API surface) — spawn the foundation first, then pass depends_on: [<foundation task_id>] on each successor spawn. The edge waits for the foundation to complete AND merge before the successor dispatches (same-base dependency semantics).',
      'depends_on entries are validated at authoring time: each must be a same-project task id (the foundation you spawned, never a cross-project or cross-base task), and the declaration is rejected if it references the calling task itself, lists a task twice, names a terminal-failed task, or forms a cyclic prerequisite set — the spawn returns a clear 400 instead of silently dropping a bad edge.',
      'To gate a successor on dependencies LANDING on a named branch instead of only completing, declare require_deps_landed_on: "<branch>" — dispatch then holds until every depends_on task is COMPLETED with its merged work on that branch, bypassing the file-overlap heuristic. Pair it with release_base_branch: "<branch>" so the successor base is re-cut from that branch at dependency-release time and starts from the branch tip including the dependencies merged code. The branch MUST be the project integration branch (the base branch this spawn builds from): a direct spawn never creates a shared/campaign branch, so any other name is rejected with a 400 that names the allowed integration branch. Keep genuinely independent work parallel by omitting depends_on.',
      'fix is not spawnable: it needs a live-PR target (pr_number + branch) bound by the normal review pipeline, which the strategist cannot resolve at spawn — route existing-PR work through direction → research → plan → create.',
      'Write/plan/design tasks and standalone research are watched by default. Direction-linked research is filtered through discovery review; set watch=true only when its raw completion itself needs Resident attention.',
      'Exceptional project bursts return a durable deferred request instead of HTTP 429.',
      'For compute_experiment, set preview_only=true to validate and inspect the normalized launch without dispatching.',
      'Omit accelerator for a CPU-only job. Preview reports the selected resource and estimated billable cost.',
      'Use success_criteria for simple all-required (AND) measurement contracts; success_criterion remains supported.',
      'A compute_experiment states its result contract one of two ways. A MEASURED goal declares success_criterion {metric, operator, threshold} and its command must emit that metric, or the run fails compute_unmeasured_criterion. A PRODUCTION run — one whose value IS its output — declares NO metric and NO criterion: exit 0 with durable artifacts in $BLUEPRINT_ARTIFACT_DIR is the result and an empty delivery fails. Tighten the second with success_criterion {kind: "artifact_produced", min_file_count: N} or require_durable_artifact. Never declare a metric that restates delivery (artifacts_returned >= 2) — it is met whenever the job merely ran.',
      'input_artifact_job_id rehydrates the durable bundle of a TERMINAL compute job into $BLUEPRINT_ARTIFACT_DIR before the command runs, for train-then-evaluate, resuming a deadline-capped run, or sharing one preprocessing step. It requires declared_metric and no_progress_timeout_seconds and the launch is refused without that pair. declared_metric names the result key used for scoring/arm comparison; it is NOT the watchdog signal. The no-progress watchdog polls the MTIME of the file named by $BLUEPRINT_PROGRESS_FILE and kills the job (exit 125, no_progress_timeout) if it stops advancing, so the command MUST append a JSON line per step to that file (JSONL, one {"metric": ..., "value": ...} record per line, appended and flushed -- never rewritten).',
    ],
  },
  request_qualification: {
    kind: 'json' as const,
    fields: ['reason', 'probes'],
    example: {
      reason: 'The candidate is stable and the remaining release claims are ready for judgment.',
      probes: ['walkthrough_e2e'],
    },
    notes: [
      'Qualification is expensive; request it only at a meaningful stable checkpoint.',
      'Omit probes for ordinary code/release judgment. Request walkthrough_e2e only when the release claim requires live product behavior.',
    ],
  },
  report_autonomous_finding: {
    kind: 'json' as const,
    required: ['kind', 'summary'],
    fields: ['kind', 'summary', 'evidence_refs'],
    example: {
      kind: 'finding',
      summary: 'The queued task is blocked on an operator-owned credential.',
      evidence_refs: ['123e4567-e89b-42d3-a456-426614174000'],
    },
    notes: [
      'Available only inside the exact server-leased typed-autonomous Workday turn.',
      'kind is question|blocker|finding. Fast remains the sole public author.',
      'Evidence refs are bounded correlation candidates; they do not establish managed state.',
    ],
  },
  cancel_task: {
    kind: 'json' as const,
    required: ['reason'],
    fields: ['target_task_id', 'request_id', 'reason'],
    example: {
      request_id: '7b48ce0f-55c6-4f62-904f-ec624bc32763',
      reason: 'This deferred request is no longer needed.',
    },
    notes: [
      'Pass exactly one of target_task_id or request_id.',
      'request_id cancels durable work only while it remains deferred.',
    ],
  },
  force_promote: {
    kind: 'json' as const,
    required: ['reason'],
    fields: ['reason', 'candidate_sha'],
    example: {
      reason: 'Release claims are proven by landed work; only the audit lane is outstanding.',
      candidate_sha: '0c45d166455a0f6d3c1a2b4e5d6f708192a3b4c5',
    },
    notes: [
      'DOES: cut the release branch, open the release-vN -> promotion-target PR, and hand it to the normal CI-wait / managed-check-wait machinery.',
      'SKIPS: the release AUDIT / qualification scoring gate, and ONLY that.',
      'DOES NOT SKIP CI. The promotion PR must still go green on the promotion-target required branch-protection checks before anything merges. Never report this to the operator as "skipping CI".',
      'Two-phase by design: the first call cuts the PR and returns a waiting status (commonly promotion_check_wait); call it again once CI is green to land it.',
      'Refuses as a no-op status rather than an error when there is no open version, credentials are missing, or a promotion is already in flight. Report the returned status and detail verbatim instead of asserting success.',
      'candidate_sha defaults to the current integration-branch HEAD. Pin it only to promote a specific proven commit.',
      'Use request-qualification instead when the audit verdict is what is actually wanted.',
    ],
  },
  requeue_task: {
    kind: 'json' as const,
    required: ['target_task_id', 'reason'],
    fields: ['target_task_id', 'reason', 'guidance'],
    example: {
      target_task_id: '7b48ce0f-55c6-4f62-904f-ec624bc32763',
      reason: 'Platform-killed on an AGENT_TIMEOUT; the work itself was never attempted.',
      guidance: 'The previous attempt died before writing any code. Start from the branch as it stands.',
    },
    notes: [
      'DOES: re-drive one same-project task back to QUEUED through the SAME admin status-transition path an operator PATCH uses, so attempt/runtime reset, cascade rules, and review/fix PR admission all behave identically.',
      'This is also the RETRY verb. Retrying a failed task IS requeueing it; there is no separate retry op.',
      'Retryable source statuses are FAILED, CANCELLED, BLOCKED, EVALUATING, and PARKED. Anything else is refused 409 with the current status as the reason.',
      'DOES NOT re-run a failed release qualification (use request-qualification), DOES NOT unpark a review-fix loop or restore an exhausted iteration cap (use reset-iteration-cap), and DOES NOT resurrect a COMPLETED task.',
      'guidance is an optional one-shot retry instruction appended to the requeued prompt; it defaults to the reason, because requeueing with no instruction at all is how an agent repeats the attempt that just failed. Never write _original_prompt_suffix; that key is internal retry-dedup metadata and is refused.',
      'Scoped to the calling project. A cross-project target is refused 403.',
    ],
  },
  reset_iteration_cap: {
    kind: 'json' as const,
    required: ['target_task_id', 'reason'],
    fields: ['target_task_id', 'reason'],
    example: {
      target_task_id: '7b48ce0f-55c6-4f62-904f-ec624bc32763',
      reason: 'The cap tripped on three infra timeouts, not on real review iterations.',
    },
    notes: [
      'This is the UNPARK verb for a review-fix chain. A chain parked for operator disposition (review_loop_abandon_candidate, or a stuck/standoff terminal outcome) is released by exactly this operation and by nothing else.',
      'DOES: archive the FAILED review/fix tasks on the PR so they leave the per-PR cap census, zero the per-chain iteration counter, stamp the reset marker that also releases the merge-gate exact-target respawn budget, clear the abandon-candidate park marker, and clear the approval hold ONLY when that hold provably belongs to this cap episode.',
      'DOES NOT re-spawn anything. The next orchestrator cycle or an explicit rollup has to pick the chain back up.',
      'DOES NOT close, merge, or otherwise touch the PR, and DOES NOT stamp resolved:true on the terminal outcome, because a reset is not evidence that the loop converged.',
      'Requires the task to carry context.review_loop.pr_number or context.rollup_pr_number; a task with no PR is refused 400.',
      'Same implementation as the operator lever POST /api/admin/tasks/{id}/reset-iteration-cap. One code path, not a parallel one.',
    ],
  },
  set_task_priority: {
    kind: 'json' as const,
    required: ['target_task_id', 'priority', 'reason'],
    fields: ['target_task_id', 'priority', 'reason'],
    example: {
      target_task_id: '7b48ce0f-55c6-4f62-904f-ec624bc32763',
      priority: 'high',
      reason: 'This unblocks two other directions and should dispatch ahead of queued research.',
    },
    notes: [
      'DOES: rewrite Task.priority, the integer the dispatch loop orders its candidate queue by. priority accepts high|medium|low (100|50|0) or an explicit integer 0-100.',
      'DOES NOT dispatch the task, raise a concurrency cap, bypass a dependency or landing gate, or unblock a BLOCKED row. Priority only decides ORDER among rows that are already eligible, so raising it on a gated task changes nothing.',
      'To reprioritise a DIRECTION use update-direction with priority=high|medium|low. This op is for tasks only.',
      'Scoped to the calling project. A cross-project target is refused 403.',
    ],
  },
  unhold_direction: {
    kind: 'json' as const,
    required: ['direction_id', 'reason'],
    fields: ['direction_id', 'reason'],
    example: {
      direction_id: 'd-1210b9',
      reason: 'The three consecutive failures were one platform defect, now root-fixed.',
    },
    notes: [
      'DOES: clear the consecutive-failure hold that stops a direction from dispatching new tasks. Idempotent: was_held=false when the direction was already healthy.',
      'DOES NOT change the direction status, reset saturation or refractory state, re-activate a retired or staged direction (use activate-direction), or spawn any task. It only removes the failure hold.',
      'The hold is control-plane state re-evaluated each cycle, so clearing it without fixing the underlying failure just lets the direction fail back into a hold.',
      'Same implementation as the operator lever POST /api/admin/projects/{id}/directions/{direction_id}/unhold.',
    ],
  },
  set_running_update: markdownInput(),
} satisfies Record<string, StrategicOpInputContract>;

function inputFor(op: string): StrategicOpInputContract {
  return (INPUTS as Record<string, StrategicOpInputContract>)[op] ?? jsonInput();
}

/**
 * The canonical, ordered strategist mutating-op registry. Order defines the CLI
 * `case`/usage order and the bootstrap prompt op list order, so the first 19
 * surfaced ops are listed in the sequence the CLI historically used; the five
 * internal-only ops follow.
 */
export const STRATEGIC_OPS_REGISTRY: readonly StrategicOpEntry[] = [
  { op: 'set_problem_model', alias: 'set-problem-model', cli: true, prompt: true, internalReason: null,
    description: "Compatibility alias for patching the project's problem model.", input: inputFor('set_problem_model') },
  { op: 'patch_problem_model', alias: 'patch-problem-model', cli: true, prompt: true, internalReason: null,
    description: "Patch the project's problem model without clobbering omitted fields.", input: inputFor('patch_problem_model') },
  { op: 'replace_problem_model', alias: 'replace-problem-model', cli: true, prompt: true, internalReason: null,
    description: 'Guardedly replace a problem model for bootstrap or recovery.', input: inputFor('replace_problem_model') },
  { op: 'set_running_update', alias: 'set-running-update', cli: true, prompt: true, internalReason: null,
    description: 'Replace the operator-facing live-status snapshot for task, PR, blocker, HEAD, and next-step state.', input: inputFor('set_running_update') },
  { op: 'reflect', alias: 'reflect', cli: true, prompt: true, internalReason: null,
    description: 'Record a strategic self-reflection note for the cycle.' },
  { op: 'pending_confirm', alias: 'pending-confirm', cli: true, prompt: true, internalReason: null,
    description: 'Queue a pending user-confirmation item on the problem model.' },
  { op: 'resolve_pending_confirm', alias: 'resolve-pending-confirm', cli: true, prompt: true, internalReason: null,
    description: 'Clear a pending user-confirmation item by id (strategist obtained the decision/credential or it became moot).' },
  { op: 'set_vision', alias: 'set-vision', cli: true, prompt: true, internalReason: null,
    description: 'Update the project vision (purpose, systems, priorities, context).' },
  { op: 'create_direction', alias: 'create-direction', cli: true, prompt: true, internalReason: null,
    description: 'Open a distinct durable strategic outcome lane (optionally staged); use spawn-task for bounded or parallel execution.', input: inputFor('create_direction') },
  { op: 'update_direction', alias: 'update-direction', cli: true, prompt: true, internalReason: null,
    description: "Update an existing direction's fields, status, or gap linkage. Terminal status updates require the same outcome or typed zero-attempt reason as retire_direction.", input: inputFor('update_direction') },
  { op: 'retire_direction', alias: 'retire-direction', cli: true, prompt: true, internalReason: null,
    description: 'Retire a direction after recording its outcome. A direction with no attempted cycle requires rationale plus terminal_reason=duplicate|superseded|invalidated|explicit_abandonment; gated by the challenge.' },
  { op: 'stage_direction', alias: 'stage-direction', cli: true, prompt: true, internalReason: null,
    description: 'Stage an existing direction to begin after the current version promotes.' },
  { op: 'approve_direction', alias: 'approve-direction', cli: true, prompt: true, internalReason: null,
    description: 'Approve a staged/proposed direction.' },
  { op: 'reject_direction', alias: 'reject-direction', cli: true, prompt: true, internalReason: null,
    description: 'Reject a staged/proposed direction.' },
  { op: 'activate_direction', alias: 'activate-direction', cli: true, prompt: true, internalReason: null,
    description: 'Reactivate a staged or paused direction back to active.' },
  { op: 'resolve_discovery', alias: 'resolve-discovery', cli: true, prompt: true, internalReason: null,
    description: 'Resolve the exact discovery cycle and revision named by an operator event by continuing with a complete next bet, parking it, or closing it.', input: inputFor('resolve_discovery') },
  { op: 'create_message', alias: 'create-message', cli: true, prompt: false, internalReason: null,
    description: 'Post a message to the operator (available to all agent types).' },
  { op: 'resolve_message', alias: 'resolve-message', cli: true, prompt: false, internalReason: null,
    description: 'Resolve an addressed operator message (available to all agent types).' },
  { op: 'spawn_task', alias: 'spawn-task', cli: true, prompt: true, internalReason: null,
    description: 'Durably dispatch bounded implementation, research, plan, design, or compute work under a strategic outcome.', input: inputFor('spawn_task') },
  { op: 'cancel_task', alias: 'cancel-task', cli: true, prompt: true, internalReason: null,
    description: 'Cancel a spawned QUEUED/PROVISIONING task or deferred spawn request.', input: inputFor('cancel_task') },
  { op: 'watch_task', alias: 'watch-task', cli: true, prompt: true, internalReason: null,
    description: 'Register a durable one-shot notification for a same-project task reaching an actionable boundary. Knowledge work fires at terminal; write work waits for landing, failure, blocker, approval, or no-PR completion.' },
  { op: 'ack_watch', alias: 'ack-watch', cli: true, prompt: true, internalReason: null,
    description: 'Acknowledge a fired task watch after reporting, acting, or deciding no action is needed.' },
  { op: 'cancel_watch', alias: 'cancel-watch', cli: true, prompt: true, internalReason: null,
    description: 'Cancel a task watch that is no longer relevant.' },
  { op: 'dispose_completion_debt', alias: 'dispose-completion-debt', cli: true, prompt: true, internalReason: null,
    description: 'Dispose of an open completion-debt ledger entry.' },
  { op: 'consume_knowledge_followup', alias: 'consume-knowledge-followup', cli: true, prompt: true, internalReason: null,
    description: 'Consume a pending knowledge follow-up (spawned direction, incorporated knowledge, etc.).' },
  { op: 'dismiss_knowledge_followup', alias: 'dismiss-knowledge-followup', cli: true, prompt: true, internalReason: null,
    description: 'Dismiss a pending knowledge follow-up as not currently actionable.' },
  { op: 'set_retirement_critiques', alias: 'set-retirement-critiques', cli: true, prompt: true, internalReason: null,
    description: 'Record retirement critiques for the cycle.' },
  { op: 'set_evidence_progress', alias: 'set-evidence-progress', cli: true, prompt: true, internalReason: null,
    description: 'Record evidence-progress verdicts (gated by the challenge).' },
  { op: 'set_acceptance_criteria', alias: 'set-acceptance-criteria', cli: true, prompt: true, internalReason: null,
    description: 'Author acceptance criteria for a direction/task.' },
  { op: 'record_finding_resolution', alias: 'record-finding-resolution', cli: true, prompt: true, internalReason: null,
    description: 'Record a carried audit finding as resolved or code-verified stale in the re-qualification resolution ledger.' },
  { op: 'set_memory', alias: 'set-memory', cli: true, prompt: true, internalReason: null,
    description: 'Persist a behavioral lesson or operator preference for future sessions.' },
  { op: 'set_session_summary', alias: 'set-session-summary', cli: true, prompt: true, internalReason: null,
    description: 'Record a running summary of session context and progress for continuity across restarts.' },
  { op: 'report_autonomous_finding', alias: 'report-autonomous-finding', cli: true, prompt: false, internalReason: null,
    description: 'Privately hand one leased autonomous Workday question, blocker, or finding to Fast for receipt-bound public delivery.', input: inputFor('report_autonomous_finding') },

  // ── Internal-only / auto-invoked backend ops (deliberately NOT on the CLI or
  //    bootstrap prompt). They are accepted by the dispatch chain but are invoked
  //    programmatically by the one-shot cycle / operator seams, never as a
  //    `./strategic <alias>` command. Excluded EXPLICITLY (flagged), not by
  //    omission, so the drift-guard covers them too.
  { op: 'begin_autonomous_turn', alias: null, cli: false, prompt: false,
    internalReason: 'Agent-runtime-only server lease minting for one typed-autonomous Workday turn.',
    description: 'Mint or replay the exact server-owned private autonomous-turn lease.' },
  { op: 'end_autonomous_turn', alias: null, cli: false, prompt: false,
    internalReason: 'Agent-runtime-only settlement of the exact typed-autonomous Workday turn lease.',
    description: 'Settle one exact server-owned private autonomous-turn lease.' },
  { op: 'decide_churn_retirement', alias: null, cli: false, prompt: false,
    internalReason: 'Autonomous keep/retire decision for a churn/zombie-flagged direction; mirrors the operator seam and is not surfaced as a standalone CLI verb.',
    description: 'Decide keep/retire for a direction the churn/zombie ladder paused pending review.' },
  { op: 'declare_capability_adoption', alias: null, cli: false, prompt: false,
    internalReason: 'Records a capability-adoption trial verdict; reserved/auto-invoked, not surfaced on the CLI.',
    description: 'Declare a capability-adoption verdict for a named capability.' },
  { op: 'cite_memos', alias: null, cli: false, prompt: false,
    internalReason: 'Invoked programmatically by the one-shot cycle to record cited memo ids, not via the CLI.',
    description: 'Record the memo task_ids the thinker cited in its analysis.' },
  { op: 'cite_knowledge', alias: null, cli: false, prompt: false,
    internalReason: 'Invoked programmatically by the one-shot cycle to record cited knowledge ids, not via the CLI.',
    description: 'Record the knowledge-document ids the strategist cited in its analysis.' },
  { op: 'set_strategic_challenge', alias: null, cli: false, prompt: false,
    internalReason: 'Persists the independent adversarial challenge record; invoked programmatically by the cycle, not via the CLI.',
    description: "Persist the independent adversarial second-opinion record over the strategist's self-judgments." },
  { op: 'consume_knowledge', alias: 'consume-knowledge', cli: true, prompt: true, internalReason: null,
    description: 'Mark a knowledge document as consumed — the strategist has read and acted on it.' },
  { op: 'dismiss_knowledge', alias: 'dismiss-knowledge', cli: true, prompt: true, internalReason: null,
    description: 'Mark a knowledge document as dismissed — not actionable, with an optional reason.' },
  { op: 'request_qualification', alias: 'request-qualification', cli: true, prompt: true, internalReason: null,
    description: 'Request a fresh release qualification at a stable checkpoint, with only explicitly selected product probes.',
    input: INPUTS.request_qualification },

  // ── Pipeline ACTION verbs. Direction and task lifecycle, promotion, and
  //    recovery must not bottleneck on the operator, so the strategist owns
  //    these levers directly rather than asking for them. Each one is wired to
  //    the SAME server-side implementation the equivalent operator lever calls —
  //    there is no parallel code path — and each description states what the
  //    verb does NOT do, so the strategist cannot overstate a lever when it
  //    reports back.
  { op: 'force_promote', alias: 'force-promote', cli: true, prompt: true, internalReason: null,
    description: 'Promote the open release version, SKIPPING the release audit gate — it does NOT skip CI: the promotion PR must still go green before it merges.',
    input: INPUTS.force_promote },
  { op: 'requeue_task', alias: 'requeue-task', cli: true, prompt: true, internalReason: null,
    description: 'Re-drive one same-project failed, cancelled, blocked, evaluating, or parked task back to QUEUED (this is also the retry verb). Does NOT re-run a qualification and does NOT unpark a review loop.',
    input: INPUTS.requeue_task },
  { op: 'reset_iteration_cap', alias: 'reset-iteration-cap', cli: true, prompt: true, internalReason: null,
    description: 'UNPARK a parked review-fix chain and reset its exhausted per-PR/per-chain iteration cap. Does NOT re-spawn any task and never touches the PR.',
    input: INPUTS.reset_iteration_cap },
  { op: 'set_task_priority', alias: 'set-task-priority', cli: true, prompt: true, internalReason: null,
    description: 'Reprioritise one same-project task by rewriting the integer the dispatch loop orders by. Does NOT dispatch it, raise a cap, or clear a dependency gate.',
    input: INPUTS.set_task_priority },
  { op: 'unhold_direction', alias: 'unhold-direction', cli: true, prompt: true, internalReason: null,
    description: 'Clear a consecutive-failure hold so a direction dispatches work again. Does NOT change its status, reset saturation, or spawn anything.',
    input: INPUTS.unhold_direction },
];

/** Registry entries surfaced on the `./strategic` CLI, in canonical order. */
export function cliOps(): StrategicOpEntry[] {
  return STRATEGIC_OPS_REGISTRY.filter(e => e.cli);
}

/** Return the discoverability contract for a surfaced operation. */
export function cliOpInput(opOrAlias: string): StrategicOpInputContract | undefined {
  const entry = cliOps().find(entry => entry.op === opOrAlias || entry.alias === opOrAlias);
  return entry?.input ?? (entry ? jsonInput() : undefined);
}

/** Registry entries advertised in the strategist bootstrap prompt, in order. */
export function promptOps(): StrategicOpEntry[] {
  return STRATEGIC_OPS_REGISTRY.filter(e => e.prompt);
}

/** Every backend op name the dispatch chain must accept (the full registry). */
export function allBackendOps(): string[] {
  return STRATEGIC_OPS_REGISTRY.map(e => e.op);
}

/** Shape of one entry in the checked-in cross-language manifest. */
export interface StrategicOpManifestEntry {
  op: string;
  alias: string | null;
  description: string;
  cli: boolean;
  prompt: boolean;
  internal_reason: string | null;
  input?: StrategicOpInputContract;
}

/**
 * Project the registry to the checked-in shared manifest shape
 * (`backend/app/strategic_ops_manifest.json`). The manifest is the
 * cross-language contract: the agent test asserts this equals the JSON file,
 * and the backend test asserts the dispatch chain accepts exactly these `op`s.
 * Rendering-only details (CLI case bodies) are deliberately excluded. Declared
 * input contracts are included so backend drift guards can compare their
 * accepted envelopes with the same metadata that renders CLI help.
 */
export function strategicOpsManifest(): StrategicOpManifestEntry[] {
  return STRATEGIC_OPS_REGISTRY.map(e => {
    const entry: StrategicOpManifestEntry = {
      op: e.op,
      alias: e.alias,
      description: e.description,
      cli: e.cli,
      prompt: e.prompt,
      internal_reason: e.internalReason,
    };
    if (e.input) entry.input = e.input;
    return entry;
  });
}
