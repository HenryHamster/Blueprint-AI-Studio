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

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function powerShellQuote(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

export function runnerSetupSnippets(
  token: string,
  origin: string,
  preset: RunnerSetupPreset = 'default',
): RunnerSetupSnippetCommands {
  const psLines = [
    `$env:BLUEPRINT_RUNNER_TOKEN = ${powerShellQuote(token)}`,
    `$env:BLUEPRINT_ORCHESTRATOR_URL = ${powerShellQuote(origin)}`,
  ];
  const shellEnv = [
    `BLUEPRINT_RUNNER_TOKEN=${shellQuote(token)}`,
    `BLUEPRINT_ORCHESTRATOR_URL=${shellQuote(origin)}`,
  ];

  if (preset === 'strategic_local') {
    psLines.push("$env:BLUEPRINT_RUNNER_MAX_CONCURRENT = '1'");
    shellEnv.push('BLUEPRINT_RUNNER_MAX_CONCURRENT=1');
  }

  return {
    windowsPowerShell: [...psLines, 'py -3 scripts/local_runner.py'].join('\n'),
    macosLinux: `${shellEnv.join(' ')} python3 scripts/local_runner.py`,
  };
}
