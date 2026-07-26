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
  spawn_task: {
    kind: 'json' as const,
    required: ['type', 'title'],
    fields: [
      'type', 'title', 'prompt', 'direction_id', 'criterion_ids',
      'acceptance_evidence', 'debt_id', 'idempotency_key', 'watch',
      'watch_purpose', 'preview_only', 'pr_number', 'branch', 'original_task_id',
      'original_prompt', 'command', 'hypothesis', 'spend_cap_usd',
      'deadline_seconds', 'success_criterion', 'success_criteria', 'machine_type',
      'runner_target', 'accelerator', 'vram_gb', 'min_compute_capability',
      'cwd', 'env', 'container_image', 'allow_host_fallback',
    ],
    example: {
      type: 'create',
      title: 'Implement the verified pipeline repair',
      prompt: 'Apply the scoped fix and add regression coverage.',
      idempotency_key: 'pipeline-repair-v1',
    },
    notes: [
      'Use this operation for implementation, research, plan, design, and compute tasks.',
      'type=fix additionally requires pr_number and branch.',
      'Spawned tasks are watched by default; set watch=false to opt out.',
      'Exceptional project bursts return a durable deferred request instead of HTTP 429.',
      'For compute_experiment, set preview_only=true to validate and inspect the normalized launch without dispatching.',
      'Omit accelerator for a CPU-only job. Preview reports the selected resource and estimated billable cost.',
      'Use success_criteria for simple all-required (AND) measurement contracts; success_criterion remains supported.',
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
  set_running_update: markdownInput(),
} satisfies Record<string, StrategicOpInputContract>;

function inputFor(op: string): StrategicOpInputContract {
  return INPUTS[op as keyof typeof INPUTS] ?? jsonInput();
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
    description: 'Update the operator-facing running-update snapshot.', input: inputFor('set_running_update') },
  { op: 'reflect', alias: 'reflect', cli: true, prompt: true, internalReason: null,
    description: 'Record a strategic self-reflection note for the cycle.' },
  { op: 'pending_confirm', alias: 'pending-confirm', cli: true, prompt: true, internalReason: null,
    description: 'Queue a pending user-confirmation item on the problem model.' },
  { op: 'resolve_pending_confirm', alias: 'resolve-pending-confirm', cli: true, prompt: true, internalReason: null,
    description: 'Clear a pending user-confirmation item by id (strategist obtained the decision/credential or it became moot).' },
  { op: 'set_vision', alias: 'set-vision', cli: true, prompt: true, internalReason: null,
    description: 'Update the project vision (purpose, systems, priorities, context).' },
  { op: 'create_direction', alias: 'create-direction', cli: true, prompt: true, internalReason: null,
    description: 'Open a new strategic direction (optionally staged for the next version).' },
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
  { op: 'create_message', alias: 'create-message', cli: true, prompt: false, internalReason: null,
    description: 'Post a message to the operator (available to all agent types).' },
  { op: 'resolve_message', alias: 'resolve-message', cli: true, prompt: false, internalReason: null,
    description: 'Resolve an addressed operator message (available to all agent types).' },
  { op: 'spawn_task', alias: 'spawn-task', cli: true, prompt: true, internalReason: null,
    description: 'Durably dispatch an implementation, research, plan, design, or compute task.', input: inputFor('spawn_task') },
  { op: 'spawn_fix', alias: null, cli: false, prompt: false,
    internalReason: 'Deprecated compatibility alias; use spawn-task.',
    description: 'Deprecated compatibility alias for spawn-task.' },
  { op: 'cancel_task', alias: 'cancel-task', cli: true, prompt: true, internalReason: null,
    description: 'Cancel a spawned QUEUED/PROVISIONING task or deferred spawn request.', input: inputFor('cancel_task') },
  { op: 'watch_task', alias: 'watch-task', cli: true, prompt: true, internalReason: null,
    description: 'Register a durable one-shot notification for a same-project task reaching a terminal state.' },
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

  // ── Internal-only / auto-invoked backend ops (deliberately NOT on the CLI or
  //    bootstrap prompt). They are accepted by the dispatch chain but are invoked
  //    programmatically by the one-shot cycle / operator seams, never as a
  //    `./strategic <alias>` command. Excluded EXPLICITLY (flagged), not by
  //    omission, so the drift-guard covers them too.
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
    description: 'Request a fresh release-qualification run directly, without going through set_problem_model.' },
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
}

/**
 * Project the registry to the checked-in shared manifest shape
 * (`backend/app/strategic_ops_manifest.json`). The manifest is the
 * cross-language contract: the agent test asserts this equals the JSON file,
 * and the backend test asserts the dispatch chain accepts exactly these `op`s.
 * Rendering-only details (CLI case bodies) are deliberately excluded.
 */
export function strategicOpsManifest(): StrategicOpManifestEntry[] {
  return STRATEGIC_OPS_REGISTRY.map(e => ({
    op: e.op,
    alias: e.alias,
    description: e.description,
    cli: e.cli,
    prompt: e.prompt,
    internal_reason: e.internalReason,
  }));
}
