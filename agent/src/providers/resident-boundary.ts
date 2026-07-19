/** Ephemeral process boundary for a no-tool Strategic Resident provider call. */

import fs from 'fs';
import os from 'os';
import path from 'path';
import type { ProviderName, ProviderRunOptions } from './runners.js';

const RUNTIME_ENV_KEYS = [
  'PATH', 'PATHEXT', 'SystemRoot', 'LANG', 'TERM',
] as const;
const MODEL_ENV_KEYS = [
  'CLAUDE_CODE_EFFORT_LEVEL',
  'ANTHROPIC_MODEL',
  'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL',
  'ANTHROPIC_DEFAULT_HAIKU_MODEL',
  'CLAUDE_CODE_SUBAGENT_MODEL',
] as const;
const PROVIDER_ENV_KEYS: Readonly<Record<'anthropic' | 'zai' | 'deepseek', readonly string[]>> = {
  anthropic: [
    'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
  ],
  zai: [
    'ZAI_API_KEY', 'ZAI_ANTHROPIC_BASE_URL', 'ZAI_BASE_URL',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
  ],
  deepseek: [
    'DEEPSEEK_API_KEY', 'DEEPSEEK_ANTHROPIC_BASE_URL', 'DEEPSEEK_BASE_URL',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
  ],
};

export interface ResidentProviderBoundary {
  root: string;
  runOptions: ProviderRunOptions;
  dispose(): void;
}

export function createResidentProviderBoundary(provider: ProviderName): ResidentProviderBoundary {
  if (provider !== 'anthropic' && provider !== 'zai' && provider !== 'deepseek') {
    throw new Error(`Resident provider ${provider} has no isolated no-tool boundary`);
  }
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'blueprint-resident-'));
  const cwd = path.join(root, 'work');
  const home = path.join(root, 'home');
  const config = path.join(root, 'claude-config');
  const temp = path.join(root, 'tmp');
  for (const directory of [cwd, home, config, temp]) {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  return {
    root,
    runOptions: {
      processBoundary: {
        cwd,
        promptDirectory: temp,
        allowedEnvKeys: [
          ...RUNTIME_ENV_KEYS,
          ...MODEL_ENV_KEYS,
          ...PROVIDER_ENV_KEYS[provider],
        ],
        envOverrides: {
          HOME: home,
          CLAUDE_CONFIG_DIR: config,
          TMPDIR: temp,
          TMP: temp,
          TEMP: temp,
        },
        skipAmbientHelpers: true,
        enforceNoTools: true,
      },
    },
    dispose: () => {
      try { fs.rmSync(root, { recursive: true, force: true }); } catch { /* best effort */ }
    },
  };
}
