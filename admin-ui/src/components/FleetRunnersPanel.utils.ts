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
