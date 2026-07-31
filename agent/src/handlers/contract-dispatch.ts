import { handleCreate } from './standard.js';
import { handleInvestigate } from './specialized.js';
import { isReadOnlyTaskType } from '../taskCapabilities.js';
import type { Handler } from './index.js';

// ── Contract-driven dispatch ─────────────────────────────────────────────────
// The backend ships the claimed task's capability contract in the claim
// response under `task_capability` (see backend/app/routers/agent.py →
// agent_capability_slice). It is the same catalog-shaped object that the
// operator registry publishes, with the agent resolving only the runtime
// dispatch keys it needs. The agent dispatches from this contract so a task
// type the backend REGISTERS becomes runnable WITHOUT an agent code edit: a
// bespoke handler is used when one exists for the exact type, otherwise a
// GENERIC runner is resolved from run_shape × agent_mode × output_kind.

export interface PublishedTaskCapabilityContract {
  id?: string;
  label?: string;
  description?: string;
  agent_mode?: 'write' | 'read_only';
  default_category?: string | null;
  user_launchable?: boolean;
  provider_configurable?: boolean;
  filter_visible?: boolean;
  output_kind?: string;
  requires_code?: boolean;
  requires_web?: boolean;
  requires_compute?: boolean;
  workspace_requirement?: string;
  evaluation_strategy?: string;
  cost_ceiling_usd?: number | null;
  run_shape?: string;
}

export interface TaskCapabilityContract extends PublishedTaskCapabilityContract {
  run_shape: string;
  agent_mode: 'write' | 'read_only';
  output_kind: string;
  requires_code: boolean;
  requires_web: boolean;
  requires_compute: boolean;
  is_bespoke_dispatch: boolean;
}

export interface GenericRunner {
  handler: Handler;
  /** Human-readable label for the resolved generic runner (for logs/telemetry). */
  label: string;
}

// Read-only output kinds that the generic single-agent investigation runner can
// safely handle. Verdict outputs need an explicit handler or schema-specific
// runner because the generic investigation-report schema is not consumable by
// structured-verdict backends.
const GENERIC_INVESTIGATION_OUTPUT_KINDS = new Set(['knowledge', 'none']);

function asString(v: unknown, fallback = ''): string {
  return typeof v === 'string' && v ? v : fallback;
}

// The backend emits run_shape with underscores ("single_agent"); accept the
// hyphenated spelling too so a contract from either source normalizes the same.
function normalizeRunShape(raw: unknown): string {
  return asString(raw, 'single_agent').replace(/-/g, '_');
}

/**
 * Resolve the capability contract for a claimed task.
 *
 * Prefers the backend-published `task.task_capability` slice. When it is ABSENT
 * (older backend, or an idempotent claim replay that omits it) we fall back to
 * deriving run-shape / agent-mode / output-kind from the local capability table
 * plus the payload so existing behavior is unchanged.
 */
export function resolveTaskCapabilityContract(
  task: Record<string, unknown>,
  payload: Record<string, unknown>,
): TaskCapabilityContract {
  const raw = task.task_capability;
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    const c = raw as Record<string, unknown>;
    return {
      ...(c as PublishedTaskCapabilityContract),
      run_shape: normalizeRunShape(c.run_shape),
      agent_mode: c.agent_mode === 'read_only' ? 'read_only' : 'write',
      output_kind: asString(c.output_kind, 'none'),
      requires_code: c.requires_code !== false,
      requires_web: c.requires_web === true,
      requires_compute: c.requires_compute === true,
      is_bespoke_dispatch: c.is_bespoke_dispatch === true,
    };
  }
  // Contract absent — derive from the local table + payload. read-only-ness comes
  // from the local capability table; the run-shape mirrors the backend vocabulary
  // so the derived contract routes through the same generic runners a published
  // contract would: read-only analyses run as a `single_agent`, write/PR code
  // types run through the create→review→fix `workflow`.
  const taskType = asString(payload.type, 'create');
  const readOnly = isReadOnlyTaskType(taskType);
  return {
    run_shape: readOnly ? 'single_agent' : 'workflow',
    agent_mode: readOnly ? 'read_only' : 'write',
    output_kind: readOnly ? 'knowledge' : 'pr',
    requires_code: true,
    requires_web: false,
    requires_compute: false,
    is_bespoke_dispatch: false,
  };
}

/**
 * Resolve a GENERIC runner for a contract, or null when no generic runner exists
 * yet for that run-shape (`experiment_loop` is reserved-but-unused, and any
 * unrecognized shape must still fail loudly upstream).
 *
 * The two foundational runners reuse existing machinery exactly, and each keys
 * off the run-shape the backend actually emits (see RunShape in
 * backend/app/task_capabilities.py):
 *   - read-only knowledge (`single_agent`) → handleInvestigate (clone, read-only
 *     prompt, persist findings via the status/artifact path → graded by the
 *     forward-quality evaluator like `investigate`).
 *   - write/PR (`workflow`) → handleCreate (push a branch + open a PR → honored
 *     by the backend WRITE_TASK_TYPES / PR pipeline like `create`). The backend
 *     emits run_shape='workflow' for every PR-producing code type, so this is
 *     the shape a newly-registered write type carries.
 */
export function resolveGenericRunner(contract: TaskCapabilityContract): GenericRunner | null {
  if (
    contract.run_shape === 'single_agent' &&
    contract.agent_mode === 'read_only' &&
    GENERIC_INVESTIGATION_OUTPUT_KINDS.has(contract.output_kind)
  ) {
    return { handler: handleInvestigate, label: 'single-agent read-only knowledge' };
  }
  if (
    contract.run_shape === 'workflow' &&
    contract.agent_mode === 'write' &&
    contract.output_kind === 'pr'
  ) {
    return { handler: handleCreate, label: 'workflow write/PR' };
  }
  return null;
}
