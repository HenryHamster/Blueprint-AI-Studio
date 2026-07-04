export type RunnerSetupPreset = 'default' | 'strategic_local';

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
