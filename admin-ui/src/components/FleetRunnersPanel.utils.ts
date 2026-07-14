import type { FleetRunner } from '../api';

export type RunnerSetupPreset = 'default' | 'strategic_local';

/**
 * Ordered map from the runner's isolation-backend availability flag (as reported
 * in `capabilities.isolation_backends`) to the short label shown in the fleet
 * panel. Order here is the display order of the "available" pills.
 */
const ISOLATION_BACKEND_LABELS: ReadonlyArray<{ flag: string; label: string }> = [
  { flag: 'bwrap_ok', label: 'bwrap' },
  { flag: 'docker_usable', label: 'docker' },
  { flag: 'podman_usable', label: 'podman' },
  { flag: 'apptainer_ok', label: 'apptainer' },
  { flag: 'enroot_ok', label: 'enroot' },
];

export interface BackendInventory {
  /** Short labels for isolation backends the runner reported as usable. */
  available: string[];
  /** Selected agent-task isolation backend, or null if the runner didn't report one. */
  agentBackend: string | null;
  /** Selected compute-job isolation backend, or null if the runner didn't report one. */
  computeBackend: string | null;
}

/**
 * Reduce a runner's `capabilities` into display-ready isolation-backend data:
 * the list of usable backends (only truthy flags), plus the selected agent and
 * compute backends. Older runners that never reported `isolation_backends`
 * yield an empty `available` list, so the panel can hide the section entirely.
 */
export function formatBackendInventory(
  capabilities: FleetRunner['capabilities'],
): BackendInventory {
  const flags = capabilities?.isolation_backends;
  const available: string[] = [];
  if (flags) {
    for (const { flag, label } of ISOLATION_BACKEND_LABELS) {
      if (flags[flag as keyof typeof flags]) available.push(label);
    }
  }
  return {
    available,
    agentBackend: capabilities?.agent_backend ?? null,
    computeBackend: capabilities?.compute_backend ?? null,
  };
}

/** Human-readable binary byte size: 1536 → "1.5 KiB", 2147483648 → "2 GiB". */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = value >= 100 || Number.isInteger(value) ? Math.round(value) : Number(value.toFixed(1));
  return `${rounded} ${units[unitIndex]}`;
}

export interface RunnerSetupSnippetCommands {
  windowsPowerShell: string;
  macosLinux: string;
}

/**
 * Baked-in production orchestrator URL — mirrors DEFAULT_ORCHESTRATOR_URL in
 * scripts/blueprint_runner.py. When the project's API base matches this, the
 * enroll snippet omits --url entirely (the launcher already defaults to it);
 * only a non-prod backend needs the explicit override.
 */
export const DEFAULT_ORCHESTRATOR_URL = 'https://blueprint-orchestrator-mmtfi5aysa-uc.a.run.app';

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function powerShellQuote(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

function normalizeOrigin(origin: string): string {
  return origin.replace(/\/+$/, '');
}

/**
 * Build the conventional `blueprint-runner start` command for each host OS.
 *
 * The orchestrator URL is baked into blueprint-runner, so the operator supplies
 * only a token — the historic wrong/blank-URL failure class is gone. --url is
 * appended only when the project's backend differs from the baked-in prod URL.
 *
 * Windows has no bash wrapper, so it invokes the Python launcher directly; macOS
 * and Linux invoke the wrapper via `bash blueprint-runner` (robust regardless of
 * the tracked file's exec bit — the repo runs its other scripts the same way).
 * Both are run from the repo root.
 */
export function runnerSetupSnippets(
  token: string,
  origin: string,
): RunnerSetupSnippetCommands {
  const url = normalizeOrigin(origin);
  const needsUrl = url !== DEFAULT_ORCHESTRATOR_URL;

  const psParts = ['py -3 scripts/blueprint_runner.py start', '--token', powerShellQuote(token)];
  const shParts = ['bash blueprint-runner start', '--token', shellQuote(token)];
  if (needsUrl) {
    psParts.push('--url', powerShellQuote(url));
    shParts.push('--url', shellQuote(url));
  }

  return {
    windowsPowerShell: psParts.join(' '),
    macosLinux: shParts.join(' '),
  };
}

// ── Live-config helpers ──────────────────────────────────────────────────────
// Shared between the read-only status badges in FleetRunnersPanel and the
// editable form in FleetRunnerConfigPanel, so both read the runner's config the
// same way. Every value-extractor prefers the server-authoritative desired
// config and falls back to the runner-reported capabilities for runners
// enrolled before the live-config contract.

// Per-job GPU reservation bounds. Must match backend runner_config.GPUS_PER_JOB_MIN
// / GPUS_PER_JOB_MAX so the UI clamp agrees with the server-side clamp.
export const GPUS_PER_JOB_MIN = 1;
export const GPUS_PER_JOB_MAX = 8;

// Agent-task concurrency bounds, mirroring the backend _normalize_max_concurrent
// clamp so the UI never offers a value the server would reject.
export const MAX_CONCURRENT_MIN = 1;
export const MAX_CONCURRENT_MAX = 64;

/** True when this runner's concurrency/task-types are pinned by the strategic
 *  workload invariant (backend clamps max_concurrent→1, task_types→strategic
 *  set). Such fields are shown read-only in the config form. */
export function runnerPinsStrategicConfig(runner: FleetRunner): boolean {
  return (
    runner.capabilities?.runner_preset === 'strategic_local' ||
    runner.capabilities?.workload === 'strategic'
  );
}

/** Current agent-task max-concurrency for a runner, from config or capabilities. */
export function maxConcurrentValue(runner: FleetRunner): number {
  const raw = runner.config?.desired?.max_concurrent ?? runner.capabilities?.max_concurrent;
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return 2;
  return Math.max(MAX_CONCURRENT_MIN, Math.min(MAX_CONCURRENT_MAX, Math.trunc(raw)));
}

/** Current GPU allowlist for a runner, from the server-authoritative desired
 *  config. Absent / "all" means no restriction (all visible GPUs eligible). */
export function gpuAllowlistValue(runner: FleetRunner): 'all' | number[] {
  const allow = runner.config?.gpu_indices_allowed;
  if (Array.isArray(allow)) return allow;
  return 'all';
}

/** Parse an operator-typed index list ("0, 1 2") into a deduped, sorted,
 *  non-negative integer array — the shape the PATCH endpoint normalizes to. */
export function parseGpuIndexList(text: string): number[] {
  const seen = new Set<number>();
  const out: number[] = [];
  for (const token of text.split(/[\s,]+/)) {
    if (!token) continue;
    if (!/^\d+$/.test(token)) continue;
    const value = Number(token);
    if (!Number.isInteger(value) || value < 0 || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out.sort((a, b) => a - b);
}

/** Current per-job GPU count for a runner, from the server-authoritative config.
 *  Absent means the default of 1 GPU per job (backward compatible). */
export function gpusPerJobValue(runner: FleetRunner): number {
  const raw = runner.config?.gpus_per_job ?? runner.config?.desired?.gpus_per_job;
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return GPUS_PER_JOB_MIN;
  return Math.max(GPUS_PER_JOB_MIN, Math.min(GPUS_PER_JOB_MAX, Math.trunc(raw)));
}

/** Current task-type allowlist for a runner. Prefers the desired config; falls
 *  back to runner-reported capabilities. Absent / "all" means every type. */
export function taskTypesValue(runner: FleetRunner): 'all' | string[] {
  const desired = runner.config?.desired?.task_types;
  if (Array.isArray(desired)) return desired;
  if (desired === 'all') return 'all';
  const reported = runner.capabilities?.task_types;
  if (Array.isArray(reported)) return reported;
  return 'all';
}

/** Parse an operator-typed task-type list into a deduped, trimmed array. */
export function parseTaskTypeList(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const token of text.split(/[\s,]+/)) {
    const trimmed = token.trim();
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    out.push(trimmed);
  }
  return out;
}

export function gpuAllowlistLabel(runner: FleetRunner): string {
  const allow = gpuAllowlistValue(runner);
  if (allow === 'all') return 'GPUs: all';
  return allow.length > 0 ? `GPUs: ${allow.join(',')}` : 'GPUs: none';
}

export function taskTypesLabel(runner: FleetRunner): string {
  const value = taskTypesValue(runner);
  if (value === 'all') return 'tasks: all';
  return value.length > 0 ? `tasks: ${value.join(', ')}` : 'tasks: none';
}
