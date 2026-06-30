export type RunnerSetupPreset = 'default' | 'strategic_local';

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
