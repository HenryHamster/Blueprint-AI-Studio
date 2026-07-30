// ── Agent task-type coverage + runtime capability contract ──────────────────
//
// The agent hand-writes exactly one handler per task type, so the SET of ids
// the agent can dispatch is unavoidably a compile-time contract: the handlers
// registry is typed `Record<AgentTaskType, Handler>`, and a new type cannot run
// without a new handler. That coverage tuple stays here.
//
// What is NO LONGER hand-maintained here is the read_only/write partition that
// gates write-permission enforcement. That partition is sourced at runtime from
// the published backend capability contract (GET /api/agent/task-capabilities) -
// the same registry operator surfaces consume. This keeps one source of truth:
// adding a backend task type flows its `agent_mode` through automatically; there
// is no second table to drift. The contract MUST be loaded (once per VM, at
// startup) before any permission classification happens; classifying before it
// is loaded throws (fail explicit) so a missing contract can never silently
// grant write permissions.

export const AGENT_TASK_TYPE_IDS = [
  'create',
  'review',
  'fix',
  'plan',
  'research',
  'discovery_review',
  'strategic_review',
  'design_doc_update',
  'security_audit',
  'qa',
  'deps',
  'docs',
  'migrate',
  'smoketest',
  'walkthrough_e2e',
  'refactor',
  'debug',
  'perf',
  'investigate',
  'self_audit',
  'release_audit',
  'release_audit_challenge',
  'release_merge_grade',
  'memo',
  'verify_tests',
  'compute_experiment',
  'web_research',
  'design',
  'repo_bootstrap',
  'salvage_bundle',
  'capability_proof_trial',
  'criterion_coverage_trial',
] as const;

export type AgentTaskType = (typeof AGENT_TASK_TYPE_IDS)[number];
export const ALL_AGENT_TASK_TYPES: readonly AgentTaskType[] = AGENT_TASK_TYPE_IDS;

export type AgentMode = 'read_only' | 'write';

// One entry of the published capability contract. The backend emits a richer
// shape (label, badge, default_category, capability flags, run_shape, etc.);
// the agent only requires `agent_mode`, so the rest is intentionally left open.
export interface TaskCapabilityContractEntry {
  agent_mode?: unknown;
  [key: string]: unknown;
}
export type TaskCapabilityContract = Record<string, TaskCapabilityContractEntry>;

interface LoadedPartition {
  readOnly: ReadonlySet<string>;
  write: ReadonlySet<string>;
}

// Module-level cache. Null until loadTaskCapabilityContract() succeeds.
let _partition: LoadedPartition | null = null;

function isAgentMode(value: unknown): value is AgentMode {
  return value === 'read_only' || value === 'write';
}

/**
 * Validate and cache the published capability contract.
 *
 * Throws if the contract is not an object, or if any KNOWN agent task type is
 * absent or carries an `agent_mode` outside {read_only, write}. A partial or
 * malformed contract is rejected rather than partially applied - permission
 * enforcement requires complete, unambiguous coverage of every type the agent
 * can dispatch.
 */
export function loadTaskCapabilityContract(contract: unknown): void {
  if (!contract || typeof contract !== 'object') {
    throw new Error('task-capability contract is missing or not an object');
  }
  const map = contract as Record<string, unknown>;
  const readOnly = new Set<string>();
  const write = new Set<string>();
  const missing: string[] = [];
  const invalid: string[] = [];

  for (const id of AGENT_TASK_TYPE_IDS) {
    const entry = map[id];
    if (!entry || typeof entry !== 'object') {
      missing.push(id);
      continue;
    }
    const mode = (entry as TaskCapabilityContractEntry).agent_mode;
    if (!isAgentMode(mode)) {
      invalid.push(`${id}=${String(mode)}`);
      continue;
    }
    if (mode === 'read_only') readOnly.add(id);
    else write.add(id);
  }

  if (missing.length > 0 || invalid.length > 0) {
    const parts: string[] = [];
    if (missing.length) parts.push(`missing/incomplete: ${missing.join(', ')}`);
    if (invalid.length) parts.push(`invalid agent_mode: ${invalid.join(', ')}`);
    throw new Error(
      `task-capability contract does not cover all agent task types (${parts.join('; ')}). ` +
      'Refusing to load - agent permission enforcement requires a complete contract.',
    );
  }

  _partition = { readOnly, write };
}

function requirePartition(): LoadedPartition {
  if (_partition === null) {
    throw new Error(
      'task-capability contract not loaded - call loadTaskCapabilityContract() before ' +
      'classifying task permissions. Refusing to classify (fail explicit) so a missing ' +
      'contract can never silently grant write permissions.',
    );
  }
  return _partition;
}

/** Read-only task-type ids, derived from the loaded contract. Throws if unloaded. */
export function getReadOnlyTaskTypes(): readonly string[] {
  return [...requirePartition().readOnly];
}

/** Write task-type ids, derived from the loaded contract. Throws if unloaded. */
export function getWriteTaskTypes(): readonly string[] {
  return [...requirePartition().write];
}

/**
 * True when `taskType` runs in read-only mode (no commit/push, Write/Edit
 * stripped). Throws when the contract has not been loaded - callers must ensure
 * loadTaskCapabilityContract() ran first (the agent does this once per VM at
 * startup). Unknown types resolve to false (not read-only) to preserve the
 * historical fallback; dispatch rejects unknown types before this is reached.
 */
export function isReadOnlyTaskType(taskType: string): boolean {
  return requirePartition().readOnly.has(taskType);
}

/** Test-only: clear the cached partition so a fresh contract can be loaded. */
export function __resetTaskCapabilityContractForTesting(): void {
  _partition = null;
}
