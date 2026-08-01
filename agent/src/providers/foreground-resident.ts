import fs from 'fs';
import os from 'os';
import path from 'path';
import { getProviderRunner } from './runners.js';
import { startResidentClaudeSession, type ResidentSession } from './resident-session.js';
import { withoutCallSpeechSecrets } from '../call-speech/config.js';

export const FAST_CALL_TOOL_NAMES = [
  'mcp__call__complete_turn',
  'mcp__call__request_workday',
  'mcp__call__show_display_blocks',
] as const;

/** Typed conversation has no call-media/display capability. */
export const FAST_TYPED_TOOL_NAMES = [
  'mcp__call__complete_turn',
  'mcp__call__request_workday',
] as const;

/** Environment names deliberately permitted to reach the foreground child. */
const PROVIDER_ENV = new Set([
  'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL',
  'ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
  'CLAUDE_CODE_SUBAGENT_MODEL',
  'ZAI_API_KEY', 'ZAI_API_BASE_URL', 'DEEPSEEK_API_KEY', 'DEEPSEEK_API_BASE',
  'CLAUDE_CODE_EFFORT_LEVEL', 'NO_PROXY', 'no_proxy',
]);
const BASE_ENV = new Set(['PATH', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR', 'TEMP', 'TMP']);
const FORBIDDEN_ENV = /(?:^|_)(?:GITHUB|GH|BLUEPRINT|AGENT|ORCHESTRATOR|GOOGLE|AWS|AZURE|TOKEN_FILE|CODEX_HOME)(?:_|$)/i;

export interface ForegroundResidentProfile {
  cwd: string;
  env: NodeJS.ProcessEnv;
  /** A deployment firewall must enforce this list; Node itself cannot enforce egress. */
  allowedEgress: readonly ['provider', 'local-coordinator'];
  allowedTools: readonly string[];
}

/**
 * Creates an empty, non-repository cwd.  It lives outside the checkout and is
 * mode 0700 so a hostile prompt has neither project files nor inherited helper
 * binaries to discover.  The caller owns lifecycle cleanup after the call.
 */
export function createForegroundWorkingDirectory(root = os.tmpdir()): string {
  const cwd = fs.mkdtempSync(path.join(root, 'blueprint-fast-resident-'));
  fs.chmodSync(cwd, 0o700);
  if (fs.existsSync(path.join(cwd, '.git'))) throw new Error('foreground cwd must not be a repository');
  return cwd;
}

export function sanitizeForegroundEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  // Speech provider keys remain in the deterministic Coordinator only. Keep
  // this explicit deny-list even though the normal allow-list also excludes it.
  for (const [name, value] of Object.entries(withoutCallSpeechSecrets(source))) {
    if (FORBIDDEN_ENV.test(name)) continue;
    if (BASE_ENV.has(name) || PROVIDER_ENV.has(name)) result[name] = value;
  }
  // The foreground process never needs an inherited home: it could expose a
  // developer's config/token cache.  CLI state is made ephemeral instead.
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'blueprint-fast-home-'));
  fs.chmodSync(home, 0o700);
  result.HOME = home;
  result.USERPROFILE = home;
  return result;
}

export function createForegroundResidentProfile(
  source = process.env,
  allowedTools: readonly string[] = FAST_CALL_TOOL_NAMES,
): ForegroundResidentProfile {
  return {
    cwd: createForegroundWorkingDirectory(),
    env: sanitizeForegroundEnvironment(source),
    allowedEgress: ['provider', 'local-coordinator'],
    allowedTools: [...allowedTools],
  };
}

export interface ClaudeForegroundLaunch {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
  cwd: string;
  allowedTools: readonly string[];
}

export function mergeForegroundProviderEnvironment(
  profileEnvironment: NodeJS.ProcessEnv,
  runnerEnvironment: Record<string, string>,
): NodeJS.ProcessEnv {
  const env = { ...profileEnvironment };
  for (const [name, value] of Object.entries(runnerEnvironment)) {
    if (PROVIDER_ENV.has(name)) env[name] = value;
  }
  return env;
}

/** Provider-neutral foreground seam.  Only Claude is executable in Phase 0. */
export interface ForegroundResidentAdapter {
  readonly id: string;
  readonly validation: 'qualified' | 'unvalidated';
  start(input: {
    profile: ForegroundResidentProfile;
    mcpConfigPath: string;
    model?: string;
    effort?: string;
  }): ResidentSession;
}

/** The existing warm Claude stream-json transport is the first real adapter. */
export const claudeForegroundAdapter: ForegroundResidentAdapter = {
  id: 'claude-warm-stream-json',
  // Unit tests prove the launch contract, not real-account call latency,
  // interruption, or isolation. Production remains gated until that benchmark.
  validation: 'unvalidated',
  start({ profile, mcpConfigPath, model, effort }) {
    const launch = buildClaudeForegroundLaunch(profile, mcpConfigPath, model, effort);
    assertForegroundLaunchIsRestricted(launch);
    return startResidentClaudeSession({
      foregroundLaunch: launch,
      // Foreground turns are short and direct; this is an SLO guard, not a
      // claim that the provider qualifies for voice latency yet.
      turnTimeoutMs: 30_000,
      hardTurnTimeoutMs: 90_000,
    });
  },
};

const CLAUDE_FOREGROUND_QUALIFICATION = 'claude-warm-stream-json:phase0-v1';
const CLAUDE_COMPATIBLE_PROVIDERS = new Set(['anthropic', 'zai', 'deepseek']);

export interface TypedFastRuntimeConfig {
  activated: boolean;
  reason: string;
  model?: string;
  effort?: string;
  adapter: ForegroundResidentAdapter;
}

/** Backend-owned Resident payload contract for the restricted Fast child. */
export interface TypedFastActivation {
  enabled: boolean;
  models?: Record<string, string>;
  effort?: string;
  qualification?: string;
}

/**
 * Typed Fast is an explicitly provisioned capability, not something inferred
 * from the mere existence of a Workday Resident.  This check happens before a
 * durable public-turn claim, so an unavailable adapter leaves the operator
 * input untouched for the normal Resident path.
 */
export function typedFastRuntimeConfig(
  activation: boolean | TypedFastActivation,
  env: NodeJS.ProcessEnv = process.env,
  provider = getProviderRunner().provider,
): TypedFastRuntimeConfig {
  const payloadOwned = typeof activation === 'object';
  const requested = payloadOwned ? activation.enabled === true : activation;
  const payloadQualification = payloadOwned ? activation.qualification : undefined;
  // The deployed Resident receives this exact qualification from the backend
  // task payload. A payload-owned activation must never inherit a runner-local
  // qualification; the boolean form preserves only the legacy call sites.
  const adapter = payloadOwned
    ? (
        payloadQualification === CLAUDE_FOREGROUND_QUALIFICATION
          ? { ...claudeForegroundAdapter, validation: 'qualified' as const }
          : claudeForegroundAdapter
      )
    : configuredClaudeForegroundAdapter(env);
  if (!requested) {
    return { activated: false, reason: 'not requested', adapter };
  }
  if (adapter.validation !== 'qualified') {
    return { activated: false, reason: 'foreground adapter is not qualification-approved', adapter };
  }
  if (!CLAUDE_COMPATIBLE_PROVIDERS.has(provider)) {
    return {
      activated: false,
      reason: `active provider ${provider} has no qualified foreground adapter`,
      adapter,
    };
  }
  const payloadModel = payloadOwned ? activation.models?.[provider]?.trim() : undefined;
  const model = payloadOwned
    ? payloadModel
    : (env.TYPED_FAST_MODEL?.trim() || (provider === 'anthropic' ? 'haiku' : undefined));
  if (!model) {
    return {
      activated: false,
      reason: `TYPED_FAST_MODEL is required for ${provider}`,
      adapter,
    };
  }
  return {
    activated: true,
    reason: 'qualified',
    model,
    effort: payloadOwned
      ? (activation.effort?.trim() || 'low')
      : (env.TYPED_FAST_EFFORT?.trim() || 'low'),
    adapter,
  };
}

/**
 * A deployment may enable only the adapter/benchmark contract it actually
 * qualified with a real account.  Keeping this outside the static adapter
 * prevents unit tests from accidentally turning a launch-contract proof into
 * a production latency/isolation claim.
 */
export function configuredClaudeForegroundAdapter(
  env: NodeJS.ProcessEnv = process.env,
): ForegroundResidentAdapter {
  if (env.CALL_FOREGROUND_QUALIFICATION !== CLAUDE_FOREGROUND_QUALIFICATION) {
    return claudeForegroundAdapter;
  }
  return { ...claudeForegroundAdapter, validation: 'qualified' };
}

/**
 * Codex must use app-server rather than the unsafe one-shot `codex exec`
 * runner.  This object is intentionally non-executable until the app-server
 * protocol, pooling/auth behavior, isolation, and benchmark suite are proven.
 */
export const codexAppServerForegroundAdapter = {
  id: 'codex-app-server',
  validation: 'unvalidated' as const,
  start(): never {
    throw new Error('Codex app-server foreground adapter is an unvalidated Phase-0 seam, not a runnable fallback');
  },
};

/**
 * A narrow Claude-compatible foreground launcher.  It intentionally does not
 * reuse the coding runner's --dangerously-skip-permissions launch arguments.
 * ``--allowedTools`` only auto-approves named tools; it does *not* remove
 * Claude's built-ins.  ``--tools ''`` is therefore the actual CLI boundary
 * that leaves only the explicitly-configured local MCP server available.
 * Network egress remains an independent VM/firewall responsibility: CLI tool
 * gating is not an operating-system sandbox.
 */
export function buildClaudeForegroundLaunch(
  profile: ForegroundResidentProfile,
  mcpConfigPath: string,
  model?: string,
  effort?: string,
): ClaudeForegroundLaunch {
  const runner = getProviderRunner();
  const runnerCommand = runner.buildRunCommand('', model, effort);
  if (runnerCommand.outputMode !== 'claude-stream-json') {
    throw new Error(`Fast foreground requires a Claude-compatible provider, got ${runner.provider}/${runnerCommand.outputMode}`);
  }
  const args = ['-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--input-format', 'stream-json'];
  if (model) args.push('--model', model);
  args.push(
    '--mcp-config', mcpConfigPath,
    '--strict-mcp-config',
    // Claude CLI: an empty --tools value disables every built-in tool while
    // preserving the strict MCP configuration below.
    '--tools', '',
    '--disable-slash-commands',
    '--no-chrome',
    '--no-session-persistence',
  );
  for (const tool of profile.allowedTools) args.push('--allowedTools', tool);
  // The normal provider runner derives Claude-compatible ANTHROPIC_* aliases
  // for Z.AI and DeepSeek. Preserve only those explicitly allowed provider
  // fields; never copy its broader task/backend environment into Fast.
  const env = mergeForegroundProviderEnvironment(profile.env, runnerCommand.env);
  return { command: runnerCommand.command, args, env, cwd: profile.cwd, allowedTools: profile.allowedTools };
}

export function assertForegroundLaunchIsRestricted(launch: ClaudeForegroundLaunch): void {
  if (launch.args.some((arg) => /dangerously|skip-permissions|bypass-approvals/i.test(arg))) {
    throw new Error('foreground launch contains a broad-permission flag');
  }
  if (!launch.args.includes('--strict-mcp-config')) throw new Error('foreground launch requires strict MCP config');
  const toolsFlag = launch.args.indexOf('--tools');
  if (toolsFlag < 0 || launch.args[toolsFlag + 1] !== '') {
    throw new Error('foreground launch must disable Claude built-in tools');
  }
  if (!launch.args.includes('--disable-slash-commands')) {
    throw new Error('foreground launch must disable slash-command customizations');
  }
  if (!launch.args.includes('--no-chrome')) throw new Error('foreground launch must disable Chrome integration');
  if (!launch.args.includes('--no-session-persistence')) {
    throw new Error('foreground launch must disable session persistence');
  }
  for (const tool of launch.allowedTools) {
    if (![...FAST_CALL_TOOL_NAMES].includes(tool as typeof FAST_CALL_TOOL_NAMES[number])) {
      throw new Error(`foreground launch exposes unsupported tool ${tool}`);
    }
  }
  for (const tool of FAST_TYPED_TOOL_NAMES) {
    if (!launch.allowedTools.includes(tool)) throw new Error(`foreground launch misses ${tool}`);
  }
  if (fs.existsSync(path.join(launch.cwd, '.git'))) throw new Error('foreground launch cwd is a repository');
  for (const name of Object.keys(launch.env)) {
    if (FORBIDDEN_ENV.test(name)) throw new Error(`foreground launch leaks ${name}`);
  }
}
