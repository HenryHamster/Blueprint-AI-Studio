import { describe, expect, it } from 'vitest';
import fs from 'fs';
import {
  assertForegroundLaunchIsRestricted, buildClaudeForegroundLaunch,
  configuredClaudeForegroundAdapter, createForegroundResidentProfile,
  mergeForegroundProviderEnvironment, sanitizeForegroundEnvironment,
  typedFastRuntimeConfig,
} from './foreground-resident.js';

describe('Fast Resident foreground profile', () => {
  it('drops ambient runner, GitHub, and backend credentials while retaining only provider auth', () => {
    const env = sanitizeForegroundEnvironment({
      PATH: '/bin', ANTHROPIC_API_KEY: 'provider-only', GITHUB_TOKEN: 'nope',
      BLUEPRINT_AGENT_TOKEN: 'nope', CODEX_HOME: '/private', HTTP_PROXY: 'http://bad',
      CALL_DEEPGRAM_API_KEY: 'speech-nope', CALL_CARTESIA_API_KEY: 'speech-nope',
    });
    expect(env).toMatchObject({ PATH: '/bin', ANTHROPIC_API_KEY: 'provider-only' });
    expect(env.GITHUB_TOKEN).toBeUndefined();
    expect(env.BLUEPRINT_AGENT_TOKEN).toBeUndefined();
    expect(env.HTTP_PROXY).toBeUndefined();
    expect(env.CALL_DEEPGRAM_API_KEY).toBeUndefined();
    expect(env.CALL_CARTESIA_API_KEY).toBeUndefined();
    expect(env.HOME).toBeTruthy();
  });

  it('creates a non-repository cwd and rejects broad CLI permissions', () => {
    const profile = createForegroundResidentProfile({ PATH: process.env.PATH, ANTHROPIC_API_KEY: 'key' });
    try {
      expect(fs.existsSync(`${profile.cwd}/.git`)).toBe(false);
      const safe = {
        command: 'claude', args: [
          '-p', '--strict-mcp-config', '--tools', '', '--disable-slash-commands',
          '--no-chrome', '--no-session-persistence',
        ], env: profile.env,
        cwd: profile.cwd, allowedTools: profile.allowedTools,
      };
      expect(() => { assertForegroundLaunchIsRestricted(safe); }).not.toThrow();
      expect(() => {
        assertForegroundLaunchIsRestricted({
          ...safe, args: [...safe.args, '--dangerously-skip-permissions'],
        });
      }).toThrow(/broad-permission/);
      expect(() => {
        assertForegroundLaunchIsRestricted({
          ...safe, args: safe.args.filter((arg) => arg !== '--tools' && arg !== ''),
        });
      }).toThrow(/disable Claude built-in tools/);
    } finally {
      fs.rmSync(profile.cwd, { recursive: true, force: true });
      if (profile.env.HOME) fs.rmSync(profile.env.HOME, { recursive: true, force: true });
    }
  });

  it('keeps the model/provider seam explicit without claiming Codex app-server validation', () => {
    expect(buildClaudeForegroundLaunch).toBeTypeOf('function');
  });

  it('routes effort through the compatible provider environment instead of a CLI flag', () => {
    const profile = createForegroundResidentProfile({ PATH: process.env.PATH });
    try {
      const launch = buildClaudeForegroundLaunch(profile, '/tmp/strict-mcp.json', 'haiku', 'low');
      expect(launch.args).not.toContain('--effort');
      expect(launch.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('low');
    } finally {
      fs.rmSync(profile.cwd, { recursive: true, force: true });
      if (profile.env.HOME) fs.rmSync(profile.env.HOME, { recursive: true, force: true });
    }
  });

  it('requires an exact deployment qualification attestation', () => {
    expect(configuredClaudeForegroundAdapter({}).validation).toBe('unvalidated');
    expect(configuredClaudeForegroundAdapter({
      CALL_FOREGROUND_QUALIFICATION: 'claude-warm-stream-json:phase0-v1',
    }).validation).toBe('qualified');
    expect(configuredClaudeForegroundAdapter({
      CALL_FOREGROUND_QUALIFICATION: 'claude-warm-stream-json',
    }).validation).toBe('unvalidated');
  });

  it('activates typed Fast only for an explicitly qualified compatible provider', () => {
    const qualification = {
      CALL_FOREGROUND_QUALIFICATION: 'claude-warm-stream-json:phase0-v1',
    };
    expect(typedFastRuntimeConfig(false, qualification, 'anthropic')).toMatchObject({
      activated: false, reason: 'not requested',
    });
    const unqualified = typedFastRuntimeConfig(true, {}, 'anthropic');
    expect(unqualified.activated).toBe(false);
    expect(unqualified.reason).toContain('qualification');
    const incompatible = typedFastRuntimeConfig(true, qualification, 'openai');
    expect(incompatible.activated).toBe(false);
    expect(incompatible.reason).toContain('no qualified foreground adapter');
    expect(typedFastRuntimeConfig(true, qualification, 'anthropic')).toMatchObject({
      activated: true, model: 'haiku', effort: 'low',
    });
    expect(typedFastRuntimeConfig(true, {
      ...qualification,
      TYPED_FAST_MODEL: 'provider-fast-model',
      TYPED_FAST_EFFORT: 'medium',
    }, 'zai')).toMatchObject({
      activated: true, model: 'provider-fast-model', effort: 'medium',
    });
  });

  it('uses the server-owned Resident payload without runner environment toggles', () => {
    expect(typedFastRuntimeConfig({
      enabled: true,
      qualification: 'claude-warm-stream-json:phase0-v1',
      models: { anthropic: 'claude-haiku-4-5-20251001' },
      effort: 'low',
    }, {}, 'anthropic')).toMatchObject({
      activated: true,
      model: 'claude-haiku-4-5-20251001',
      effort: 'low',
      adapter: { validation: 'qualified' },
    });
  });

  it('does not inherit runner qualification or model for payload-owned activation', () => {
    expect(typedFastRuntimeConfig({
      enabled: true,
      models: {},
    }, {
      CALL_FOREGROUND_QUALIFICATION: 'claude-warm-stream-json:phase0-v1',
      TYPED_FAST_MODEL: 'runner-local-model',
    }, 'anthropic')).toMatchObject({
      activated: false,
      reason: 'foreground adapter is not qualification-approved',
    });

    expect(typedFastRuntimeConfig({
      enabled: true,
      qualification: 'claude-warm-stream-json:phase0-v1',
      models: {},
    }, {
      TYPED_FAST_MODEL: 'runner-local-model',
    }, 'anthropic')).toMatchObject({
      activated: false,
      reason: 'TYPED_FAST_MODEL is required for anthropic',
    });
  });

  it('keeps Claude-compatible derived auth without copying runner authority', () => {
    expect(mergeForegroundProviderEnvironment(
      { PATH: '/bin', HOME: '/isolated' },
      {
        ANTHROPIC_API_KEY: 'derived-provider-key',
        ANTHROPIC_AUTH_TOKEN: 'derived-provider-key',
        ANTHROPIC_BASE_URL: 'https://compatible.example',
        ANTHROPIC_MODEL: 'deepseek-v4-pro',
        ANTHROPIC_DEFAULT_SONNET_MODEL: 'deepseek-v4-pro',
        AGENT_API_TOKEN: 'must-not-pass',
        ORCHESTRATOR_URL: 'must-not-pass',
      },
    )).toEqual({
      PATH: '/bin',
      HOME: '/isolated',
      ANTHROPIC_API_KEY: 'derived-provider-key',
      ANTHROPIC_AUTH_TOKEN: 'derived-provider-key',
      ANTHROPIC_BASE_URL: 'https://compatible.example',
      ANTHROPIC_MODEL: 'deepseek-v4-pro',
      ANTHROPIC_DEFAULT_SONNET_MODEL: 'deepseek-v4-pro',
    });
  });
});
