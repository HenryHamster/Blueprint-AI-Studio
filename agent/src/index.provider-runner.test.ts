import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Pre-import setup (hoisted before any module loads) ───────────────────────
// index.ts guards main() and process event handlers behind an isMain check,
// so they do NOT run when the file is imported by vitest. The hoisted block
// is still needed to share the fs mock instances with the vi.mock factory.
const { mockReadFileSync } = vi.hoisted(() => {
  // Build the fs mock object here so both vi.mock factory and test bodies
  // can reference the same vi.fn() instances.
  const mockReadFileSync = vi.fn();
  // Keep module-level provider/auth defaults deterministic across local and CI
  // environments. Some test cases call helpers without explicit provider args.
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  // Clear before index.ts loads so the module-level LLM_CLI_TRANSLATION
  // constant is null. Tests that verify translation behavior set the env var
  // themselves and call parseLLMCliTranslation() directly.
  delete process.env.LLM_CLI_TRANSLATION;
  return { mockReadFileSync };
});

// ── Module mocks ─────────────────────────────────────────────────────────────
vi.mock('fs', () => ({
  default: {
    readFileSync: mockReadFileSync,
    writeFileSync: vi.fn(),
    unlinkSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    openSync: vi.fn().mockReturnValue(3),
    closeSync: vi.fn(),
    chmodSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  spawn: vi.fn().mockReturnValue({
    stdin: { on: vi.fn(), end: vi.fn() },
    stderr: { on: vi.fn() },
    stdout: { on: vi.fn() },
    on: vi.fn(),
    kill: vi.fn(),
  }),
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
  execSync: vi.fn().mockReturnValue(''),
  // reset.ts (now imported transitively via index.ts) wraps execFile with
  // promisify; supply the (cmd, args, opts, callback) shape so reset's
  // own dependency surface doesn't break unrelated index tests.
  execFile: (
    _cmd: string,
    _args: string[],
    _opts: unknown,
    cb: (err: Error | null, stdout: string, stderr: string) => void,
  ) => {
    void Promise.resolve().then(() => { cb(null, '', ''); });
  },
}));

vi.mock('ws', () => ({
  default: class MockWebSocket {
    static OPEN = 1;
    readyState = 0;
    on = vi.fn();
    send = vi.fn();
    close = vi.fn();
  },
}));

// ── Imports (after mocks are registered) ─────────────────────────────────────
import {
  testFixLoop,
  captureTestBaseline,
  RateLimitError,
  NoProgressTimeoutError,
  setupWorkspace,
  registerSizeCapMergeDriver,
  matchesRateLimit,
  matchesAuthFailure,
  matchesVmConfigMissing,
  VmConfigMissingError,
  getProviderRunner,
  providerSupportsNativeSubagents,
  parseLLMCliTranslation,
  execFileAsync,
  runProviderAgent,
  persistAndPostStatus,
  parseStaleResultFile,
  markStaleResultFailure,
  shouldAbandonStaleResult,
  refreshRuntimeState,
  _resetCredentialStateForTesting,
} from './index.js';
import { shouldDeferProviderValidationToResident } from './resident-startup.js';
import {
  __setCommandExistsOverrideForSmoke,
  __resetClaudeCapabilityCacheForTesting,
  _setLastProviderRunErrorReportForTesting,
  claudeHelpSupportsPartialMessages,
  probeClaudeCapabilities,
  stripUnsupportedClaudeOptionalArgs,
  type ProviderRunErrorReport,
} from './providers/runners.js';
import { execFileSync, spawn, execSync } from 'child_process';
import fs from 'fs';

/** Asserts a value is defined and returns it (narrowed). */
function defined<T>(v: T | undefined | null, msg = 'expected value to be defined'): T {
  if (v === undefined || v === null) throw new Error(msg);
  return v;
}
const makeFetchMock = () => vi.fn<(input: unknown, init?: RequestInit) => Promise<unknown>>();
type FetchMock = ReturnType<typeof makeFetchMock>;

describe('Resident startup provider validation', () => {
  it('defers only the long-lived Strategic Resident to its canonical stream launch', () => {
    expect(shouldDeferProviderValidationToResident('strategic_review', '1')).toBe(true);
    expect(shouldDeferProviderValidationToResident('strategic_review', undefined)).toBe(false);
    expect(shouldDeferProviderValidationToResident('fix', '1')).toBe(false);
    expect(shouldDeferProviderValidationToResident(undefined, '1')).toBe(false);
  });
});

beforeEach(() => {
  // Bypass the pre-flight CLI-exists check: provider binaries (claude, codex, etc.)
  // are not installed in the test environment; spawn is mocked separately.
  __setCommandExistsOverrideForSmoke(() => true);
  __resetClaudeCapabilityCacheForTesting();
  process.env.PROVIDER = 'anthropic';
  process.env.AUTH_MODE = 'oauth';
  delete process.env.LLM_MODEL;
  delete process.env.LLM_SETTINGS;
  delete process.env.LLM_CLI_TRANSLATION;
  delete process.env.LLM_EXECUTION_MODE;
  delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
  delete process.env.CLAUDE_CODE_EFFORT_LEVEL;
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.ANTHROPIC_AUTH_TOKEN;
  delete process.env.ANTHROPIC_BASE_URL;
  delete process.env.ANTHROPIC_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_OPUS_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_SONNET_MODEL;
  delete process.env.ANTHROPIC_DEFAULT_HAIKU_MODEL;
  delete process.env.CLAUDE_CODE_SUBAGENT_MODEL;
  delete process.env.ZAI_API_KEY;
  delete process.env.ZAI_ANTHROPIC_BASE_URL;
  delete process.env.ZAI_BASE_URL;
  delete process.env.DEEPSEEK_API_KEY;
  delete process.env.DEEPSEEK_ANTHROPIC_BASE_URL;
  delete process.env.DEEPSEEK_BASE_URL;
  delete process.env.OPENAI_API_KEY;
  delete process.env.CODEX_API_KEY;
  delete process.env.CODEX_HOME;
  delete process.env.GEMINI_API_KEY;
  delete process.env.GOOGLE_APPLICATION_CREDENTIALS;
  delete process.env.GOOGLE_CLOUD_PROJECT;
  delete process.env.GOOGLE_CLOUD_LOCATION;
  _resetCredentialStateForTesting();
  refreshRuntimeState();
});

function configureSpawnMock(failCmds: Record<string, { exitCode: number; stderr: string }> = {}) {
  vi.mocked(spawn).mockImplementation((cmd: unknown, argsRaw: unknown) => {
    const cmdStr = String(cmd);
    const args = Array.isArray(argsRaw) ? argsRaw.map(String) : [];
    const isNpmInstall = cmdStr === 'npm' && (args[0] === 'ci' || args[0] === 'install');
    const failure = isNpmInstall ? undefined : failCmds[cmdStr];
    const stderrCbs: ((d: Buffer) => void)[] = [];
    const proc = {
      pid: 100 + Math.floor(Math.random() * 1000),
      stderr: {
        on: vi.fn((event: string, cb: (d: Buffer) => void) => {
          if (event === 'data') stderrCbs.push(cb);
        }),
      },
      stdout: { on: vi.fn() },
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (event === 'close') {
          setImmediate(() => {
            if (failure) for (const scb of stderrCbs) scb(Buffer.from(failure.stderr));
            cb(failure ? failure.exitCode : 0);
          });
        }
      }),
      kill: vi.fn(),
    };
    return proc as unknown as ReturnType<typeof spawn>;
  });
}

describe('matchesRateLimit', () => {
  it('detects "you\'ve hit your limit"', () => {
    expect(matchesRateLimit("you've hit your limit")).toBe(true);
  });

  it('detects "you\'ve hit your usage limit"', () => {
    expect(matchesRateLimit("you've hit your usage limit")).toBe(true);
  });

  it('detects "claude ai usage limit reached"', () => {
    expect(matchesRateLimit('claude ai usage limit reached')).toBe(true);
  });

  it('detects "you\'re out of extra usage"', () => {
    expect(matchesRateLimit("you're out of extra usage")).toBe(true);
  });

  it('detects "5-hour limit reached"', () => {
    expect(matchesRateLimit('5-hour limit reached')).toBe(true);
  });

  it('detects "api error: rate limit reached"', () => {
    expect(matchesRateLimit('api error: rate limit reached')).toBe(true);
  });

  it('detects "this request would exceed your account\'s rate limit"', () => {
    expect(matchesRateLimit("this request would exceed your account's rate limit")).toBe(true);
  });

  it('detects the org monthly spend-limit banner (curly apostrophes)', () => {
    expect(matchesRateLimit(
      'You’ve hit your org’s monthly spend limit · ask your admin to raise it at claude.ai/settings/usage',
    )).toBe(true);
  });

  it('detects the org monthly spend-limit banner (ascii apostrophes)', () => {
    expect(matchesRateLimit(
      "You've hit your org's monthly spend limit · ask your admin to raise it at claude.ai/settings/usage",
    )).toBe(true);
  });

  it('detects the org monthly spend-limit banner for anthropic/api_key', () => {
    expect(matchesRateLimit("you've hit your org's monthly spend limit", 'anthropic', 'api_key')).toBe(true);
  });

  it('matches case-insensitively (uppercase)', () => {
    expect(matchesRateLimit('API ERROR: RATE LIMIT REACHED')).toBe(true);
  });

  it('matches case-insensitively (mixed case)', () => {
    expect(matchesRateLimit('Claude AI Usage Limit Reached')).toBe(true);
  });

  it('matches when pattern is embedded in surrounding text', () => {
    expect(matchesRateLimit('Error from Anthropic: API Error: Rate limit reached. Please retry later.')).toBe(true);
  });

  it('matches when pattern appears after other log lines', () => {
    expect(matchesRateLimit('line 1\nline 2\nyou\'ve hit your usage limit\nline 4')).toBe(true);
  });

  it('returns false for a bare "429"', () => {
    expect(matchesRateLimit('HTTP 429 received')).toBe(false);
  });

  it('returns false for the word "rate" alone', () => {
    expect(matchesRateLimit('increased rate of errors')).toBe(false);
  });

  it('returns false for the word "limit" alone', () => {
    expect(matchesRateLimit('limit exceeded on some resource')).toBe(false);
  });

  it('returns false for "overloaded"', () => {
    expect(matchesRateLimit('server is overloaded')).toBe(false);
  });

  it('detects provider-specific API-key capacity bodies', () => {
    expect(matchesRateLimit(
      'api.deepseek.com overloaded: authentication fails',
      'deepseek',
      'api_key',
    )).toBe(true);
    expect(matchesRateLimit('api.z.ai service capacity exceeded', 'zai', 'api_key')).toBe(true);
    expect(matchesRateLimit('OpenAI API error: model is temporarily unavailable', 'openai', 'api_key')).toBe(true);
  });

  it('does not detect generic API-key capacity words without provider context', () => {
    expect(matchesRateLimit('unit test prints service unavailable', 'openai', 'api_key')).toBe(false);
    expect(matchesRateLimit('unit test expected OpenAI service unavailable response', 'openai', 'api_key')).toBe(false);
    expect(matchesRateLimit('unit test expected gpt-5 temporarily unavailable response', 'openai', 'api_key')).toBe(false);
    expect(matchesRateLimit('docs mention GLM service unavailable fallback', 'zai', 'api_key')).toBe(false);
  });

  it('returns false for an empty string', () => {
    expect(matchesRateLimit('')).toBe(false);
  });

  it('returns false for unrelated error output', () => {
    expect(matchesRateLimit('TypeError: cannot read property of undefined')).toBe(false);
  });

  it('detects OpenAI API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'openai', 'api_key')).toBe(true);
  });

  it('detects Gemini service-account rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('RESOURCE_EXHAUSTED while calling model', 'gemini', 'service_account')).toBe(true);
  });

  it('detects Z.AI API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Too many requests: rate_limit exceeded', 'zai', 'api_key')).toBe(true);
  });

  it('detects DeepSeek API-key rate limits when provider/auth are supplied', () => {
    expect(matchesRateLimit('Too many requests: rate_limit exceeded', 'deepseek', 'api_key')).toBe(true);
  });

  it('detects DeepSeek balance/quota bodies even when they include auth-ish text', () => {
    expect(matchesRateLimit(
      'HTTP 401 Unauthorized from api.deepseek.com: authentication fails: insufficient_balance',
      'deepseek',
      'api_key',
    )).toBe(true);
  });

  it('detects Z.AI balance/quota bodies even when they include auth-ish text', () => {
    expect(matchesRateLimit(
      'HTTP 401 Unauthorized from api.z.ai: authentication_failed: insufficient balance',
      'zai',
      'api_key',
    )).toBe(true);
  });

  it('detects Gemini API-key quota metric bodies', () => {
    const msg = "Quota exceeded for quota metric 'GenerateRequests'. Please retry in 39.8s.";
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini API-key billing quota bodies', () => {
    const msg = 'You exceeded your current quota, please check your plan and billing details.';
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini Code Assist fallback quota messages', () => {
    const msg = 'Automatically switching from gemini-2.5-pro. Switching to the gemini-2.5-flash model.';
    expect(matchesRateLimit(msg, 'gemini', 'api_key')).toBe(true);
  });

  it('detects Gemini service-account Vertex quota bodies', () => {
    const msg = 'Resource exhausted. Please try again later. Please refer to https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429';
    expect(matchesRateLimit(msg, 'gemini', 'service_account')).toBe(true);
  });

  it('detects Gemini service-account quota metric bodies', () => {
    const msg = 'Quota exceeded for metric: aiplatform.googleapis.com/generate_content_requests';
    expect(matchesRateLimit(msg, 'gemini', 'service_account')).toBe(true);
  });

  it('does not cross-match provider-specific rate limits', () => {
    expect(matchesRateLimit('Rate limit reached for gpt-5 in organization test', 'anthropic', 'oauth')).toBe(false);
  });

  it('detects "you\'ve hit your session limit" for anthropic/oauth', () => {
    expect(matchesRateLimit("You've hit your session limit · resets 7:10am (UTC)", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your session limit" case-insensitively', () => {
    expect(matchesRateLimit("YOU'VE HIT YOUR SESSION LIMIT", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your session limit" for default provider (anthropic/oauth)', () => {
    expect(matchesRateLimit("you've hit your session limit · resets 7:10am (UTC)")).toBe(true);
  });

  it('detects "you\'ve hit your weekly limit" for anthropic/oauth', () => {
    expect(matchesRateLimit("You've hit your weekly limit · resets 9pm (UTC)", 'anthropic', 'oauth')).toBe(true);
  });

  it('detects "you\'ve hit your weekly limit" for default provider (anthropic/oauth)', () => {
    expect(matchesRateLimit("You've hit your weekly limit · resets 9pm (UTC)")).toBe(true);
  });

  it('detects structured api_error_status:429 in transcript regardless of provider', () => {
    const transcript = '{"type":"result","is_error":true,"api_error_status":429,"error":"rate_limit"}';
    expect(matchesRateLimit(transcript)).toBe(true);
    expect(matchesRateLimit(transcript, 'openai', 'api_key')).toBe(true);
  });

  it('does not match bare 429 without JSON key context', () => {
    expect(matchesRateLimit('HTTP 429 received')).toBe(false);
  });
});

describe('matchesAuthFailure', () => {
  it('does NOT treat a bare apiKeySource=none init event as an auth failure (normal for OAuth/Max-plan)', () => {
    // The documented default auth mode (AUTH_MODE=oauth / Max-plan subscription)
    // uses no API key, so EVERY healthy init event reports apiKeySource:"none".
    // Matching on it alone would misclassify healthy OAuth VMs on any transient
    // non-zero-exit validation.
    const init = '{"type":"system","subtype":"init","apiKeySource":"none","model":"claude-opus-4-8"}';
    expect(matchesAuthFailure(init)).toBe(false);
  });

  it('detects the canonical apiKeySource=none + 401 authentication_failed rejection', () => {
    // The real rejection signature is apiKeySource:none accompanied by an explicit
    // HTTP 401 authentication_failed — the authentication_failed token is what makes
    // it a confirmed rejection rather than a healthy OAuth init.
    const rejection = '{"type":"system","subtype":"init","apiKeySource":"none"}\nAPI Error: 401 {"type":"error","error":{"type":"authentication_failed"}}';
    expect(matchesAuthFailure(rejection)).toBe(true);
  });

  it('detects Claude stream 401 authentication failures', () => {
    const msg = '{"type":"system","subtype":"api_retry","error_status":401,"error":"authentication_failed"}';
    expect(matchesAuthFailure(msg)).toBe(true);
  });

  it('detects provider invalid credential messages', () => {
    expect(matchesAuthFailure('Failed to authenticate. API Error: 401 Invalid authentication credentials')).toBe(true);
  });

  it('detects an HTTP 401 authentication_failed body', () => {
    const body = 'API Error: 401 {"type":"error","error":{"type":"authentication_failed","message":"invalid x-api-key"}}';
    expect(matchesAuthFailure(body)).toBe(true);
  });

  it('detects an expired OAuth token message', () => {
    expect(matchesAuthFailure('OAuth token has expired. Please run /login.')).toBe(true);
  });

  it('detects OAuth org-disabled errors', () => {
    expect(matchesAuthFailure('oauth_org_not_allowed')).toBe(true);
  });

  it('matches case-insensitively', () => {
    expect(matchesAuthFailure('AUTHENTICATION_FAILED')).toBe(true);
  });

  it('detects OpenAI invalid_api_key for the openai provider', () => {
    expect(matchesAuthFailure('Error code: 401 - invalid_api_key', 'openai')).toBe(true);
  });

  it('detects Gemini "API key not valid" for the gemini provider', () => {
    expect(matchesAuthFailure('API key not valid. Please pass a valid API key.', 'gemini')).toBe(true);
  });

  it('detects Z.AI invalid-key failures for the zai provider', () => {
    expect(matchesAuthFailure('invalid z.ai api key', 'zai')).toBe(true);
  });

  it('detects DeepSeek invalid-key failures for the deepseek provider', () => {
    expect(matchesAuthFailure('invalid deepseek api key', 'deepseek')).toBe(true);
  });

  it('does not classify a rate-limit body as an auth failure', () => {
    expect(matchesAuthFailure('API Error: Rate limit reached. Please retry later.')).toBe(false);
  });

  it('returns false for a bare 401 with no auth context', () => {
    expect(matchesAuthFailure('HTTP 401 received')).toBe(false);
  });

  it('does not match unrelated numeric output', () => {
    expect(matchesAuthFailure('processed 401 files successfully')).toBe(false);
  });

  it('returns false for unrelated error output', () => {
    expect(matchesAuthFailure('TypeError: cannot read property of undefined')).toBe(false);
  });

  it('returns false for an empty string', () => {
    expect(matchesAuthFailure('')).toBe(false);
  });

  it('does not cross-match provider-specific auth failures', () => {
    expect(matchesAuthFailure('API key not valid', 'anthropic')).toBe(false);
  });

  it('detects a Codex OAuth refresh-token-reuse 401 (prod 2026-06-14 SavarinCodex)', () => {
    // The codex CLI emits this when its stored refresh token was already
    // consumed/rotated. Before coverage was added, runClaudeCode returned a bare
    // non-zero exit instead of throwing AuthFailureError.
    const body =
      'ERROR codex_login::auth::manager: Failed to refresh token: 401 Unauthorized: ' +
      '{"error":{"message":"Your refresh token has already been used to generate a new ' +
      'access token. Please try signing in again.","type":"invalid_request_error",' +
      '"code":"refresh_token_reused"}}';
    expect(matchesAuthFailure(body, 'openai')).toBe(true);
  });

  it('detects the "refresh token was already used / sign in again" variant', () => {
    const body =
      'Failed to refresh token: Your access token could not be refreshed because your ' +
      'refresh token was already used. Please log out and sign in again.';
    expect(matchesAuthFailure(body, 'openai')).toBe(true);
  });

  it('treats OAuth refresh-token death as provider-agnostic (generic pattern)', () => {
    expect(matchesAuthFailure('error: refresh_token_reused')).toBe(true);
  });

  it('does not classify a benign token-refresh log line as an auth failure', () => {
    expect(matchesAuthFailure('Scheduled proactive token refresh in 1800s')).toBe(false);
  });
});
describe('RateLimitError', () => {
  it('is an instance of RateLimitError and Error', () => {
    const err = new RateLimitError('Claude API usage exhausted — banner');
    expect(err).toBeInstanceOf(RateLimitError);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe('RateLimitError');
    expect(err.message).toBe('Claude API usage exhausted — banner');
  });

  it('plain Error is not a RateLimitError', () => {
    const err = new Error('Claude API usage exhausted — banner');
    expect(err).not.toBeInstanceOf(RateLimitError);
  });
});

describe('NoProgressTimeoutError', () => {
  it('carries the transient no_progress_timeout failureCategory and is distinct from RateLimitError', () => {
    const err = new NoProgressTimeoutError('no model output for >15 min');
    expect(err).toBeInstanceOf(NoProgressTimeoutError);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(RateLimitError);
    expect(err.name).toBe('NoProgressTimeoutError');
    // The agent catch block reads .failureCategory for non-RateLimitError errors;
    // 'no_progress_timeout' must NOT trigger the account-model block.
    expect(err.failureCategory).toBe('no_progress_timeout');
  });
});

describe('matchesVmConfigMissing', () => {
  it('matches the Claude CLI configless death banner (case-insensitive)', () => {
    expect(matchesVmConfigMissing('Claude configuration file not found at /home/agent/.claude.json')).toBe(true);
    expect(matchesVmConfigMissing('claude CONFIGURATION FILE NOT FOUND')).toBe(true);
  });

  it('does not match rate-limit or auth-failure text', () => {
    expect(matchesVmConfigMissing("you've hit your usage limit")).toBe(false);
    expect(matchesVmConfigMissing('OAuth token has expired. Please run /login.')).toBe(false);
  });

  it('does not match unrelated file-not-found errors', () => {
    expect(matchesVmConfigMissing('ENOENT: no such file or directory, open /tmp/foo.json')).toBe(false);
    expect(matchesVmConfigMissing('')).toBe(false);
  });
});

describe('VmConfigMissingError', () => {
  it('carries the vm_config_missing failureCategory and is distinct from RateLimitError', () => {
    const err = new VmConfigMissingError('Claude Code provider config file missing on VM');
    expect(err).toBeInstanceOf(VmConfigMissingError);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(RateLimitError);
    expect(err.name).toBe('VmConfigMissingError');
    // runRoleOnce's catch reads .failureCategory; vm_config_missing maps to
    // PROVISION_FAILURE backend-side — never RATE_LIMIT ("usage exhausted").
    expect(err.failureCategory).toBe('vm_config_missing');
  });
});

describe('provider runner prompt transport', () => {
  it('passes the documented all-tools deny rule to Claude-compatible CLIs', () => {
    const runner = getProviderRunner('anthropic', 'api_key');
    const deniedTools = ['*', 'Task', 'Agent', 'Workflow', 'Skill'];
    const command = runner.buildRunCommand(
      'Bounded prompt', undefined, undefined, deniedTools,
    );

    const denied = command.args.flatMap((arg, index) => (
      arg === '--disallowedTools' ? [command.args[index + 1]] : []
    ));
    expect(denied).toEqual(deniedTools);
  });

  it('uses stdin for OpenAI run commands instead of argv prompts', () => {
    const runner = getProviderRunner('openai', 'api_key');
    const command = runner.buildRunCommand('Very long prompt body', 'gpt-5');

    expect(command.promptInput).toBe('stdin');
    // Codex CLI invocation uses the canonical bypass flag at the legacy
    // fallback path — disables approval policy and sandbox in one knob,
    // equivalent to Claude's --dangerously-skip-permissions. Agent VMs
    // are ephemeral; sandboxing inside them stalls on approval prompts
    // nobody can answer. `--full-auto` is explicitly avoided since it
    // aliases --sandbox workspace-write and would override the bypass.
    // --json: emit JSONL events (added 2026-05-08) so the agent wrapper can
    // parse Codex tool calls and populate last_tool_use_at. Without it, every
    // Codex task is structurally invisible to the watchdog.
    expect(command.args).toEqual(['exec', '--skip-git-repo-check', '--json', '--dangerously-bypass-approvals-and-sandbox', '-m', 'gpt-5']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
    expect(command.outputMode).toBe('codex-jsonl');
  });

  it('uses stdin for Gemini run commands instead of argv prompts', () => {
    const runner = getProviderRunner('gemini', 'api_key');
    const command = runner.buildRunCommand('Very long prompt body', 'gemini-2.5-pro');

    expect(command.promptInput).toBe('stdin');
    expect(command.args).toEqual(['--approval-mode=yolo', '-o', 'text', '-m', 'gemini-2.5-pro']);
    expect(command.args.join(' ')).not.toContain('Very long prompt body');
    // Sandbox disabled via env var since Gemini's approval-mode and
    // sandbox are separate knobs (YOLO auto-enables docker sandbox
    // unless GEMINI_SANDBOX=false pins it off).
    expect(command.env.GEMINI_SANDBOX).toBe('false');
    // Trust gate opt-out — ephemeral VM is the isolation boundary.
    expect(command.env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildRunCommand (service_account)', () => {
    const runner = getProviderRunner('gemini', 'service_account');
    const command = runner.buildRunCommand('Some prompt', 'gemini-2.5-pro');
    expect(command.env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildValidationCommand (api_key)', () => {
    const runner = getProviderRunner('gemini', 'api_key');
    const command = runner.buildValidationCommand('gemini-2.5-pro');
    expect(command).not.toBeNull();
    expect(defined(command).env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('sets GEMINI_CLI_TRUST_WORKSPACE=true on Gemini buildValidationCommand (service_account)', () => {
    const runner = getProviderRunner('gemini', 'service_account');
    const command = runner.buildValidationCommand('gemini-2.5-pro');
    expect(command).not.toBeNull();
    expect(defined(command).env.GEMINI_CLI_TRUST_WORKSPACE).toBe('true');
  });

  it('passes --include-partial-messages to the Claude run command so slow opus turns stream progress deltas', () => {
    const runner = getProviderRunner('anthropic', 'oauth');
    const command = runner.buildRunCommand('prompt body', 'claude-opus-4-8', 'high');
    // The flag must accompany the streaming-json output mode — it only emits
    // partial-message stream_event lines when stream-json is active.
    expect(command.outputMode).toBe('claude-stream-json');
    expect(command.args).toContain('--include-partial-messages');
    expect(command.args).toContain('--output-format');
    expect(command.args).toContain('stream-json');
    expect(command.args).toContain('--verbose');
  });

  it('uses the Claude effort environment control instead of the version-sensitive CLI flag', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      args: ['--model', 'claude-sonnet-4-6', '--effort', 'high'],
      env: {},
      prompt_fragments: [],
      unsupported: [],
    });
    refreshRuntimeState();

    const runner = getProviderRunner('anthropic', 'oauth');
    const command = runner.buildRunCommand('prompt body', 'claude-sonnet-4-6', 'high');

    expect(command.args).toContain('--model');
    expect(command.args).toContain('claude-sonnet-4-6');
    expect(command.args).not.toContain('--effort');
    expect(command.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('high');
  });

  it('detects whether the installed Claude CLI supports partial-message streaming', () => {
    expect(
      claudeHelpSupportsPartialMessages(
        'Usage: claude [options]\n  --include-partial-messages  emit stream deltas',
      ),
    ).toBe(true);
    expect(
      claudeHelpSupportsPartialMessages(
        'Usage: claude [options]\n  --output-format <format>',
      ),
    ).toBe(false);
  });

  it('caches Claude capabilities by binary version and preserves model routing while stripping unsupported optional flag values', () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync)
      .mockReturnValueOnce('claude 1.2.3')
      .mockReturnValueOnce('Usage: claude [options]\n  --model <model>\n  --effort <level>')
      .mockReturnValueOnce('Usage: claude [options]\n  --model <model>\n  --effort <level>');
    const first = probeClaudeCapabilities('claude', process.env);
    const second = probeClaudeCapabilities('claude', process.env);

    expect(first).toEqual(new Set(['--model', '--effort']));
    expect(second).toBe(first);
    expect(execFileSync).toHaveBeenCalledTimes(3);
    expect(stripUnsupportedClaudeOptionalArgs(
      ['--model', 'glm-5.2', '--effort', 'high', '--include-partial-messages'], first,
    )).toEqual(['--model', 'glm-5.2', '--effort', 'high']);
    expect(stripUnsupportedClaudeOptionalArgs(
      ['--model', 'glm-5.2', '--effort', 'high', '--include-partial-messages'], new Set(['--model']),
    )).toEqual(['--model', 'glm-5.2']);
  });

  it('retries a truncated Claude help surface before caching Resident capabilities', () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync)
      .mockReturnValueOnce('claude 2.1.220')
      .mockReturnValueOnce([
        'Usage: claude [options]',
        '  --model <model>',
        '  --effort <level>',
        '  --include-partial-messages',
        '  --input-format <format>',
        '  --output-format <format>',
        '  --verbose',
      ].join('\n'))
      .mockReturnValueOnce([
        'Usage: claude [options]',
        '  --model <model>',
        '  --append-system-prompt <prompt>',
        '  --mcp-config <path>',
        '  --strict-mcp-config',
        '  --allowedTools <tool>',
      ].join('\n'));

    const capabilities = probeClaudeCapabilities('claude', process.env);

    expect(capabilities).toEqual(new Set([
      '--model',
      '--append-system-prompt',
      '--mcp-config',
      '--strict-mcp-config',
      '--allowedTools',
    ]));
    expect(probeClaudeCapabilities('claude', process.env)).toBe(capabilities);
    expect(execFileSync).toHaveBeenCalledTimes(3);
  });

  it('does not reuse Claude capabilities across distinct executable environments', () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockImplementation((_command, args, options) => {
      if (args?.[0] === '--version') return '2.1.220 (Claude Code)\n';
      if (args?.[0] === '--help') {
        const probePath = (options as { env?: NodeJS.ProcessEnv } | undefined)?.env?.PATH;
        return probePath === '/modern/bin'
          ? '--append-system-prompt --mcp-config --strict-mcp-config --allowedTools'
          : '--mcp-config --strict-mcp-config --allowedTools';
      }
      return '';
    });

    const legacy = probeClaudeCapabilities('claude', {
      ...process.env,
      PATH: '/legacy/bin',
      HOME: '/legacy/home',
    });
    const modern = probeClaudeCapabilities('claude', {
      ...process.env,
      PATH: '/modern/bin',
      HOME: '/modern/home',
    });

    expect(legacy?.has('--append-system-prompt')).toBe(false);
    expect(modern?.has('--append-system-prompt')).toBe(true);
    expect(execFileSync).toHaveBeenCalledTimes(5);
  });

  it('fails open when the Claude capability probe is ambiguous or fails', () => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValueOnce('claude 1.2.4').mockReturnValueOnce('');
    expect(probeClaudeCapabilities('claude', process.env)).toBeNull();
    expect(stripUnsupportedClaudeOptionalArgs(['--effort', 'high'], null)).toEqual(['--effort', 'high']);

    __resetClaudeCapabilityCacheForTesting();
    vi.mocked(execFileSync).mockImplementationOnce(() => { throw new Error('help unavailable'); });
    expect(probeClaudeCapabilities('claude', process.env)).toBeNull();
  });

  it('uses Claude Code with the Anthropic-compatible endpoint for Z.AI', () => {
    process.env.PROVIDER = 'zai';
    process.env.AUTH_MODE = 'oauth';
    process.env.ZAI_API_KEY = 'zai-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'glm-5.2', 'high');

    expect(runner.provider).toBe('zai');
    expect(runner.authMode).toBe('api_key');
    expect(providerSupportsNativeSubagents('zai')).toBe(true);
    expect(command.command).toBe('claude');
    expect(command.outputMode).toBe('claude-stream-json');
    expect(command.promptInput).toBe('stdin-file');
    expect(command.displayName).toBe('Z.AI GLM (Claude compat)');
    expect(command.args).toContain('--model');
    expect(command.args).toContain('glm-5.2');
    expect(command.args).not.toContain('--effort');
    expect(command.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('high');
    expect(command.env.ZAI_API_KEY).toBe('zai-test-key');
    expect(command.env.ANTHROPIC_API_KEY).toBe('zai-test-key');
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBe('zai-test-key');
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.z.ai/api/anthropic');
    expect(runner.missingCredentials()).toEqual([]);
  });

  it('does not reuse a generic Anthropic key for Z.AI', () => {
    process.env.PROVIDER = 'zai';
    process.env.AUTH_MODE = 'oauth';
    process.env.ANTHROPIC_API_KEY = 'anthropic-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'glm-5.2', 'high');

    expect(runner.missingCredentials()).toEqual(['ZAI_API_KEY']);
    expect(command.env.ZAI_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.z.ai/api/anthropic');
  });

  it('prefers provider-specific Z.AI base URL over stale generic Anthropic base URL', () => {
    process.env.PROVIDER = 'zai';
    process.env.AUTH_MODE = 'oauth';
    process.env.ZAI_API_KEY = 'zai-test-key';
    process.env.ZAI_ANTHROPIC_BASE_URL = 'https://proxy.test/zai/';
    process.env.ANTHROPIC_BASE_URL = 'https://stale.test/anthropic/';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'glm-5.2', 'high');

    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://proxy.test/zai');
  });

  it('uses Claude Code with the Anthropic-compatible endpoint for DeepSeek', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.AUTH_MODE = 'oauth';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');

    expect(runner.provider).toBe('deepseek');
    expect(runner.authMode).toBe('api_key');
    expect(providerSupportsNativeSubagents('deepseek')).toBe(true);
    expect(command.command).toBe('claude');
    expect(command.outputMode).toBe('claude-stream-json');
    expect(command.promptInput).toBe('stdin-file');
    expect(command.displayName).toBe('DeepSeek (Claude compat)');
    expect(command.args).toContain('--model');
    expect(command.args).toContain('deepseek-v4-flash');
    expect(command.args).not.toContain('--effort');
    expect(command.env.CLAUDE_CODE_EFFORT_LEVEL).toBe('low');
    expect(command.env.DEEPSEEK_API_KEY).toBe('deepseek-test-key');
    expect(command.env.ANTHROPIC_API_KEY).toBe('deepseek-test-key');
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBe('deepseek-test-key');
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.deepseek.com/anthropic');
    expect(runner.missingCredentials()).toEqual([]);
  });

  it('does not reuse a generic Anthropic key for DeepSeek', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.AUTH_MODE = 'oauth';
    process.env.ANTHROPIC_API_KEY = 'anthropic-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');

    expect(runner.missingCredentials()).toEqual(['DEEPSEEK_API_KEY']);
    expect(command.env.DEEPSEEK_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_API_KEY).toBeUndefined();
    expect(command.env.ANTHROPIC_AUTH_TOKEN).toBeUndefined();
    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://api.deepseek.com/anthropic');
  });

  it('prefers provider-specific DeepSeek base URL over stale generic Anthropic base URL', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.AUTH_MODE = 'oauth';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    process.env.DEEPSEEK_ANTHROPIC_BASE_URL = 'https://proxy.test/deepseek/';
    process.env.ANTHROPIC_BASE_URL = 'https://stale.test/anthropic/';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');

    expect(command.env.ANTHROPIC_BASE_URL).toBe('https://proxy.test/deepseek');
  });

  it('sets the documented DeepSeek Claude Code model env and aligns ANTHROPIC_MODEL with the requested model', () => {
    // DeepSeek's documented integration selects models via env; without it the
    // CLI resolves its claude-* aliases, which DeepSeek silently remaps
    // (unknown/sonnet/haiku ids → v4-flash) — the Pro-task failure
    // mode. The env must carry the documented defaults, and ANTHROPIC_MODEL
    // must track the explicitly requested model so the env-driven paths
    // (stream-json session, subagent spawns) resolve the same model
    // the --model flag names.
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'deepseek-v4-pro', 'high');

    expect(command.env.ANTHROPIC_MODEL).toBe('deepseek-v4-pro');
    expect(command.env.ANTHROPIC_DEFAULT_OPUS_MODEL).toBe('deepseek-v4-pro');
    expect(command.env.ANTHROPIC_DEFAULT_SONNET_MODEL).toBe('deepseek-v4-pro');
    expect(command.env.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe('deepseek-v4-flash');
    expect(command.env.CLAUDE_CODE_SUBAGENT_MODEL).toBe('deepseek-v4-flash');

    // A different requested model re-aligns ANTHROPIC_MODEL (defaults stay).
    const flash = runner.buildRunCommand('prompt body', 'deepseek-v4-flash', 'low');
    expect(flash.env.ANTHROPIC_MODEL).toBe('deepseek-v4-flash');
    expect(flash.env.ANTHROPIC_DEFAULT_OPUS_MODEL).toBe('deepseek-v4-pro');
  });

  it('lets operator-provided model env override the DeepSeek defaults', () => {
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    process.env.ANTHROPIC_DEFAULT_HAIKU_MODEL = 'deepseek-v4-pro';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', undefined, 'low');

    // Account/pool-materialized env wins over the built-in defaults.
    expect(command.env.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe('deepseek-v4-pro');
    // No explicit model requested → the documented default stands.
    expect(command.env.ANTHROPIC_MODEL).toBe('deepseek-v4-pro');
  });

  it('preserves LLM_CLI_TRANSLATION env over DeepSeek defaults, below process.env', () => {
    // Precedence contract for the deepseek model env: process.env (account
    // materialization) > translation env (settings bag) > documented default.
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    process.env.ANTHROPIC_DEFAULT_SONNET_MODEL = 'deepseek-v4-flash'; // materialized
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      env: {
        ANTHROPIC_DEFAULT_HAIKU_MODEL: 'deepseek-v4-pro',   // translation layer
        ANTHROPIC_DEFAULT_SONNET_MODEL: 'deepseek-v4-pro',  // loses to process.env
      },
      args: [],
      prompt_fragments: [],
      unsupported: [],
    });
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', undefined, 'low');

    // Translation env survives the default fill (was clobbered pre-fix).
    expect(command.env.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe('deepseek-v4-pro');
    // Materialized process.env still outranks the translation layer.
    expect(command.env.ANTHROPIC_DEFAULT_SONNET_MODEL).toBe('deepseek-v4-flash');
    // Untouched keys fall through to the documented defaults.
    expect(command.env.CLAUDE_CODE_SUBAGENT_MODEL).toBe('deepseek-v4-flash');
  });

  it('does not leak DeepSeek model env into Anthropic runs on a retargeted VM', () => {
    // A warm VM retargeted deepseek→anthropic may still carry the DeepSeek
    // model env in process.env; the anthropic branch must not propagate it,
    // or the Claude CLI would request a nonexistent Anthropic model.
    process.env.PROVIDER = 'anthropic';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'oauth-token';
    process.env.ANTHROPIC_MODEL = 'deepseek-v4-pro';
    process.env.CLAUDE_CODE_SUBAGENT_MODEL = 'deepseek-v4-flash';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const command = runner.buildRunCommand('prompt body', 'claude-opus-4-8', 'high');

    expect(command.env.ANTHROPIC_MODEL).toBeUndefined();
    expect(command.env.CLAUDE_CODE_SUBAGENT_MODEL).toBeUndefined();
  });

  it('validates the requested model instead of a bare auth probe (Claude-compat providers)', () => {
    // A validation that never names the model proves only that the key
    // authenticates — AGENT_AUTH_OK could pass while the requested model is
    // broken (the DeepSeek broken-model failure mode: Pro tasks validated OK,
    // then every session died on its first turn).
    process.env.PROVIDER = 'deepseek';
    process.env.DEEPSEEK_API_KEY = 'deepseek-test-key';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const validation = defined(runner.buildValidationCommand('deepseek-v4-pro'));

    expect(validation.command).toBe('claude');
    expect(validation.args).toContain('--model');
    expect(validation.args[validation.args.indexOf('--model') + 1]).toBe('deepseek-v4-pro');
    expect(validation.env.ANTHROPIC_MODEL).toBe('deepseek-v4-pro');

    // Model-less validation still works (no --model flag, env defaults only).
    const bare = defined(runner.buildValidationCommand());
    expect(bare.args).not.toContain('--model');
  });

  it('passes --model to the Anthropic validation command when the pool selected one', () => {
    process.env.PROVIDER = 'anthropic';
    process.env.CLAUDE_CODE_OAUTH_TOKEN = 'oauth-token';
    refreshRuntimeState();

    const runner = getProviderRunner();
    const validation = defined(runner.buildValidationCommand('claude-opus-4-8'));
    expect(validation.args).toContain('--model');
    expect(validation.args[validation.args.indexOf('--model') + 1]).toBe('claude-opus-4-8');
    // No DeepSeek-style env pinning outside the deepseek branch.
    expect(validation.env.ANTHROPIC_MODEL).toBeUndefined();
  });
});

// ── LLM_CLI_TRANSLATION parsing ───────────────────────────────────────────────
// The runtime consumes a JSON-encoded translation blob from the backend's
// settings-bag resolver. Validate parsing is tolerant of missing fields and
// fails-safe to null on malformed JSON so legacy dispatch still works.
describe('parseLLMCliTranslation', () => {
  const originalEnv = process.env.LLM_CLI_TRANSLATION;

  afterEach(() => {
    if (originalEnv === undefined) delete process.env.LLM_CLI_TRANSLATION;
    else process.env.LLM_CLI_TRANSLATION = originalEnv;
  });

  it('returns null when env var is absent', () => {
    delete process.env.LLM_CLI_TRANSLATION;
    expect(parseLLMCliTranslation()).toBeNull();
  });

  it('parses a valid translation', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({
      env: { FOO: 'bar' },
      args: ['--model', 'claude-opus-4-7', '--effort', 'high'],
      prompt_fragments: ['Plan carefully.'],
      unsupported: ['advisor.enabled (fallback: prompt fragment only)'],
    });
    const parsed = defined(parseLLMCliTranslation());
    expect(parsed.args).toEqual(['--model', 'claude-opus-4-7', '--effort', 'high']);
    expect(parsed.env).toEqual({ FOO: 'bar' });
    expect(parsed.prompt_fragments).toEqual(['Plan carefully.']);
    expect(parsed.unsupported[0]).toMatch(/advisor/);
  });

  it('tolerates missing fields with safe defaults', () => {
    process.env.LLM_CLI_TRANSLATION = JSON.stringify({ args: ['-m', 'gpt-5'] });
    const parsed = defined(parseLLMCliTranslation());
    expect(parsed.args).toEqual(['-m', 'gpt-5']);
    expect(parsed.env).toEqual({});
    expect(parsed.prompt_fragments).toEqual([]);
    expect(parsed.unsupported).toEqual([]);
  });

  it('returns null on malformed JSON (fail-safe to legacy dispatch)', () => {
    process.env.LLM_CLI_TRANSLATION = '{not valid json';
    expect(parseLLMCliTranslation()).toBeNull();
  });
});

// ── captureTestBaseline timeout selection ─────────────────────────────────────
// execFileAsync passes its timeoutMs through to spawn's options dict (Node
// ignores it, so it doesn't affect runtime — see the comment in execFileAsync).
// That makes the configured timeout per runner observable here.
describe('captureTestBaseline timeout selection', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => ({}),
      text: () => '',
      headers: { get: () => null },
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (10min) for runners containing scripts/test-backend.sh', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect((defined(bashCall)[2] as { timeout: number }).timeout).toBe(600_000);
  });

  it('uses TEST_BASELINE_DOCKER_TIMEOUT (10min) for runners with docker in args', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'docker run --rm myimage pytest' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const dockerCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'docker');
    expect(dockerCall).toBeDefined();
    expect((defined(dockerCall)[2] as { timeout: number }).timeout).toBe(600_000);
  });

  it('uses setup timeout for npm ci and the passed baseline timeout for the suite', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'node', test_cmd: 'npm test' }],
    };
    await captureTestBaseline(null, { value: '' }, buildConfig);
    const npmCalls = vi.mocked(spawn).mock.calls.filter(c => c[0] === 'npm');
    const setupCall = npmCalls.find(c => (c[1] as string[])[0] === 'ci');
    const testCall = npmCalls.find(c => (c[1] as string[])[0] === 'test');
    expect(setupCall).toBeDefined();
    expect(testCall).toBeDefined();
    expect((defined(setupCall)[2] as { timeout: number }).timeout).toBe(180_000);
    expect((defined(testCall)[2] as { timeout: number }).timeout).toBe(60_000);
  });
});

// ── Auto-detect: scripts/test-backend.sh postgres flag follows Docker availability ─
// Without --postgres, test-backend.sh defaults to in-memory SQLite. CI runs
// Postgres. SQLite-passing-Postgres-failing fixes get pushed and only fail on
// CI, costing a full review-fix iteration. When Docker is available, auto-detect
// passes --postgres so the local gate matches CI. When Docker is absent, pass
// --no-postgres so tests still run via the in-memory SQLite path.
describe('detectTestRunners auto-detect postgres flag', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
    vi.mocked(execSync).mockReset();
    // Default: Docker is available (execSync('docker version') succeeds)
    vi.mocked(execSync).mockReturnValue('');
    vi.mocked(spawn).mockReset();
    configureSpawnMock();
    vi.mocked(fs.existsSync).mockReset();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => ({}),
      text: () => '',
      headers: { get: () => null },
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function fakeSelfRepoFs() {
    // Simulate Blueprint's own repo layout: backend/pytest.ini exists AND
    // scripts/test-backend.sh exists. No top-level package.json, no agent/
    // package.json — so the only auto-detected runner is the backend script.
    vi.mocked(fs.existsSync).mockImplementation((p: fs.PathLike) => {
      const s = String(p);
      return s.endsWith('/backend/pytest.ini') || s.endsWith('/scripts/test-backend.sh');
    });
  }

  it('appends --postgres when Docker is available (backend/pytest.ini + scripts/test-backend.sh)', async () => {
    fakeSelfRepoFs();
    // No buildConfig = auto-detect path
    await captureTestBaseline(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--postgres');
    expect(args).not.toContain('--no-postgres');
  });

  it('appends --postgres when Docker is available in testFixLoop', async () => {
    fakeSelfRepoFs();
    await testFixLoop(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--postgres');
    expect(args).not.toContain('--no-postgres');
  });

  it('runs targeted pytest selectors before impact coverage when requested', async () => {
    const buildConfig = {
      directories: [{ path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh --postgres' }],
    };

    await testFixLoop(
      null, { value: '' }, undefined, undefined, buildConfig,
      undefined, undefined, undefined,
      {
        targetPytestSelectors: ['backend/testing/test_models.py::test_create'],
        runImpactAfterTargeted: true,
        requireTestmonForImpact: true,
      },
    );

    const bashCalls = vi.mocked(spawn).mock.calls.filter(c => c[0] === 'bash');
    expect(bashCalls).toHaveLength(2);
    expect(bashCalls[0][1]).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
    expect(bashCalls[1][1]).toEqual(['scripts/test-backend.sh', '--postgres', '--require-testmon']);
  });

  it('does not scope away backend targeted runners when related files point elsewhere', async () => {
    const buildConfig = {
      directories: [
        { path: 'agent', type: 'node', test_cmd: 'npm test' },
        { path: '', type: 'python', test_cmd: 'bash scripts/test-backend.sh --postgres' },
      ],
    };

    await testFixLoop(
      null, { value: '' }, undefined, undefined, buildConfig,
      undefined, ['agent/src/index.ts'], undefined,
      { targetPytestSelectors: ['backend/testing/test_models.py::test_create'] },
    );

    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    expect(defined(bashCall)[1]).toEqual([
      'scripts/test-backend.sh',
      '--postgres',
      'testing/test_models.py::test_create',
    ]);
  });

  it('appends --no-postgres when Docker is unavailable (captureTestBaseline)', async () => {
    fakeSelfRepoFs();
    // Simulate Docker absent: execSync throws
    vi.mocked(execSync).mockImplementation(() => { throw new Error('docker not found'); });
    await captureTestBaseline(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--no-postgres');
    expect(args).not.toContain('--postgres');
  });

  it('appends --no-postgres when Docker is unavailable in testFixLoop', async () => {
    fakeSelfRepoFs();
    vi.mocked(execSync).mockImplementation(() => { throw new Error('docker not found'); });
    await testFixLoop(null, { value: '' });
    const bashCall = vi.mocked(spawn).mock.calls.find(c => c[0] === 'bash');
    expect(bashCall).toBeDefined();
    const args = defined(bashCall)[1] as string[];
    expect(args).toContain('scripts/test-backend.sh');
    expect(args).toContain('--no-postgres');
    expect(args).not.toContain('--postgres');
  });
});

// ── Agent reliability medium fixes ───────────────────────────────────────────
describe('execFileAsync process reliability', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not SIGKILL the original process group after the child exits during timeout cleanup', async () => {
    vi.useFakeTimers();
    vi.mocked(spawn).mockReset();
    const killSpy = vi.spyOn(process, 'kill').mockImplementation(() => true);
    let closeCb: ((code: number | null, signal: NodeJS.Signals | null) => void) | undefined;
    const proc = {
      pid: 4321,
      exitCode: null as number | null,
      signalCode: null as NodeJS.Signals | null,
      stdout: { on: vi.fn() },
      stderr: { on: vi.fn() },
      on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
        if (event === 'close') closeCb = cb;
      }),
    };
    vi.mocked(spawn).mockReturnValue(proc as unknown as ReturnType<typeof spawn>);

    const promise = execFileAsync('npm', ['test'], { timeoutMs: 1000 }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1000);
    expect(killSpy).toHaveBeenCalledWith(-4321, 'SIGTERM');

    proc.exitCode = 143;
    await vi.advanceTimersByTimeAsync(5000);
    expect(killSpy).not.toHaveBeenCalledWith(-4321, 'SIGKILL');

    closeCb?.(143, 'SIGTERM');
    await promise;
    killSpy.mockRestore();
  });

  it('tags external SIGKILL as EOOM instead of ETIMEDOUT', async () => {
    vi.mocked(spawn).mockReset();
    vi.mocked(spawn).mockImplementation(() => {
      const proc = {
        pid: 4322,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdout: { on: vi.fn() },
        stderr: { on: vi.fn() },
        on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              cb(null, 'SIGKILL');
            });
          }
        }),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });

    await expect(execFileAsync('node', ['worker.js'], { timeoutMs: 60_000 }))
      .rejects.toMatchObject({ code: 'EOOM', signal: 'SIGKILL', killed: true });
  });
});

describe('provider runner reliability', () => {
  function mockProviderRun(stderrChunks: string[], code: number | null, signal: NodeJS.Signals | null = null) {
    vi.mocked(spawn).mockImplementation(() => {
      const stderrCbs: Array<(d: Buffer) => void> = [];
      const proc = {
        pid: 9876,
        exitCode: null as number | null,
        signalCode: null as NodeJS.Signals | null,
        stdin: { on: vi.fn(), end: vi.fn() },
        stdout: { on: vi.fn() },
        stderr: {
          on: vi.fn((event: string, cb: (d: Buffer) => void) => {
            if (event === 'data') stderrCbs.push(cb);
          }),
        },
        on: vi.fn((event: string, cb: (code: number | null, signal: NodeJS.Signals | null) => void) => {
          if (event === 'close') {
            setImmediate(() => {
              for (const chunk of stderrChunks) {
                for (const stderrCb of stderrCbs) {
                  stderrCb(Buffer.from(chunk));
                }
              }
              proc.exitCode = code;
              proc.signalCode = signal;
              cb(code, signal);
            });
          }
        }),
        kill: vi.fn(),
      };
      return proc as unknown as ReturnType<typeof spawn>;
    });
  }

  let savedTaskId: string | undefined;
  beforeEach(() => {
    vi.stubEnv('TASK_ID', '');
    vi.mocked(spawn).mockReset();
    vi.mocked(fs.writeFileSync).mockClear();
    vi.mocked(fs.chmodSync).mockClear();
    vi.mocked(fs.unlinkSync).mockClear();
    vi.mocked(fs.openSync).mockClear();
    vi.mocked(fs.openSync).mockReturnValue(3);
    savedTaskId = process.env.TASK_ID;
    delete process.env.TASK_ID;
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    if (savedTaskId !== undefined) process.env.TASK_ID = savedTaskId;
    else delete process.env.TASK_ID;
  });

  it('uses the stderr tail so late rate-limit text is still classified', async () => {
    mockProviderRun(['x'.repeat(60_000), "\nyou've hit your usage limit\n"], 1);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(RateLimitError);
  });

  it('classifies clean-exit (code 0) with rate-limit text in tail as RateLimitError', async () => {
    // The Anthropic CLI sometimes emits the weekly-limit message as a normal
    // assistant turn then exits CLEAN (code 0); without this catch, every
    // handler treats the run as success and posts a degenerate artifact —
    // most recently the release_audit handler posted a `{audit_score: 0}`
    // stub that the qualifier scored as a legitimate 0.43 verdict instead
    // of routing through account rotation.
    mockProviderRun(["You've hit your weekly limit · resets May 29, 4am (UTC)\n"], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(RateLimitError);
  });

  it('clean-exit with rate-limit text >5KB from the tail does NOT match', async () => {
    // LOG_TAIL_BYTES = 5_000. Anything beyond that window from the end must
    // not match — a mid-conversation mention of the phrase (e.g. an earlier
    // tool result quoting documentation) is not a real rate-limit signal.
    // Chunks are concatenated to stderr in this mock; stderrBuffer is bounded
    // to its own tail by appendBoundedTail, so a leading rate-limit phrase
    // followed by 60KB of benign output drops the phrase out of the tail.
    mockProviderRun(["you've hit your usage limit\n", 'x'.repeat(60_000)], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('clean-exit with no rate-limit text resolves normally (regression)', async () => {
    mockProviderRun(['ordinary tool output\n'], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('classifies a non-zero exit with "Claude configuration file not found" as VmConfigMissingError', async () => {
    // Poisoned-VM signature (2026-06-06, tasks 1330d291 / cf28097f): the
    // re-target destroyed ~/.claude.json, so the CLI dies in seconds. This must
    // surface the distinct vm_config_missing infra category — NOT the generic
    // fatal/claude_code_failed path whose init-banner-only tail the
    // orchestrator reclassifies as usage exhaustion.
    mockProviderRun(['Claude configuration file not found at /home/agent/.claude.json\n'], 1);

    const err = await runProviderAgent('prompt', null, { value: '' }).then(
      () => { throw new Error('expected rejection'); },
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(VmConfigMissingError);
    expect((err as VmConfigMissingError).failureCategory).toBe('vm_config_missing');
  });

  it('config-missing wins over coincidental rate-limit text in the same tail', async () => {
    // The whole point of the distinct signal is to stop these deaths being
    // read as usage exhaustion — so the config-missing check must take
    // precedence over the rate-limit pattern match.
    mockProviderRun([
      "Claude configuration file not found\nyou've hit your usage limit\n",
    ], 1);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .rejects.toBeInstanceOf(VmConfigMissingError);
  });

  it('a clean exit mentioning the config phrase is NOT swept (exit-code gated)', async () => {
    // e.g. a tool result quoting this very incident report — a zero-exit run
    // must never be converted into an infra failure.
    mockProviderRun(['discussing "Claude configuration file not found" handling\n'], 0);

    await expect(runProviderAgent('prompt', null, { value: '' }))
      .resolves.toMatchObject({ exitCode: 0 });
  });

  it('writes the stdin prompt file as owner-only', async () => {
    vi.stubEnv('TASK_ID', '');
    mockProviderRun([], 0);

    await runProviderAgent('private prompt', null, { value: '' });

    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(
      '/tmp/agent_prompt_unknown.txt',
      'private prompt',
      { encoding: 'utf-8', mode: 0o600 },
    );
    expect(vi.mocked(fs.chmodSync)).toHaveBeenCalledWith('/tmp/agent_prompt_unknown.txt', 0o600);
  });
});

describe('structured error report shipping (enrichStatusPayload)', () => {
  const makeReport = (taskId: string): ProviderRunErrorReport => ({
    schema: 1,
    task_id: taskId,
    provider: 'anthropic',
    auth_mode: 'oauth',
    exit_code: 1,
    exit_signal: null,
    duration_ms: 5_000,
    model_output_seen: false,
    result_event_seen: false,
    provider_errors: [],
    stderr_tail: '',
    last_stream_event: 'system',
  });

  const stubStatusFetch = (): FetchMock => {
    const fetchMock = makeFetchMock().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
      headers: { get: () => null },
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  };

  const postedStatusBody = (fetchMock: FetchMock): Record<string, unknown> => {
    const statusCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes('/api/agent/status'));
    if (!statusCall) throw new Error('expected a /api/agent/status POST');
    const init = statusCall[1] as RequestInit;
    if (typeof init.body !== 'string') throw new Error('expected a string request body');
    return JSON.parse(init.body) as Record<string, unknown>;
  };

  afterEach(() => {
    vi.unstubAllGlobals();
    _setLastProviderRunErrorReportForTesting(null);
  });

  it('attaches the last run report to FAILED status posts for the same task', async () => {
    const fetchMock = stubStatusFetch();
    _setLastProviderRunErrorReportForTesting(makeReport('task-er-1'));

    await persistAndPostStatus('task-er-1', {
      task_id: 'task-er-1',
      status: 'FAILED',
      failure_category: 'claude_code_failed',
      logs: 'Claude subprocess exited with code 1 without shipping a PR.',
    });

    const body = postedStatusBody(fetchMock);
    expect(body.error_report).toMatchObject({
      task_id: 'task-er-1',
      exit_code: 1,
      model_output_seen: false,
      result_event_seen: false,
    });
  });

  it('does NOT attach a report to COMPLETED posts', async () => {
    const fetchMock = stubStatusFetch();
    _setLastProviderRunErrorReportForTesting(makeReport('task-er-2'));

    await persistAndPostStatus('task-er-2', { task_id: 'task-er-2', status: 'COMPLETED' });

    expect(postedStatusBody(fetchMock).error_report).toBeUndefined();
  });

  it('does NOT leak a stale report from a different task (warm-VM guard)', async () => {
    // A warm VM runs sequential roles in one process. A failure that never ran
    // the provider CLI (e.g. clone_failed) must not ship the PRIOR role's
    // report and misclassify itself as a provider death.
    const fetchMock = stubStatusFetch();
    _setLastProviderRunErrorReportForTesting(makeReport('task-er-prior-role'));

    await persistAndPostStatus('task-er-3', {
      task_id: 'task-er-3',
      status: 'FAILED',
      failure_category: 'clone_failed',
    });

    expect(postedStatusBody(fetchMock).error_report).toBeUndefined();
  });

});

describe('stale result retry envelopes', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('persists status payloads in a retry envelope before posting', async () => {
    vi.mocked(fs.writeFileSync).mockClear();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: 'ok' }),
      text: () => Promise.resolve(''),
    }));

    await persistAndPostStatus('task-1', { task_id: 'task-1', status: 'COMPLETED' });

    const writeCall = vi.mocked(fs.writeFileSync).mock.calls.find(call => call[0] === '/tmp/task_task-1_result.json');
    if (!writeCall) throw new Error('expected stale result file write');
    const rawWritten = writeCall[1];
    if (typeof rawWritten !== 'string') throw new Error('expected stale result write payload to be a string');
    const written = JSON.parse(rawWritten) as {
      payload: { status?: unknown };
      retry: { attempts?: unknown; nextAttemptAt?: unknown };
    };
    expect(written.payload.status).toBe('COMPLETED');
    expect(written.retry.attempts).toBe(0);
    expect(written.retry.nextAttemptAt).toBe(0);
  });

  it('parses legacy payload files and records per-file backoff after failures', () => {
    const legacy = parseStaleResultFile(JSON.stringify({ task_id: 'legacy-task', status: 'FAILED' }), 1000);
    expect(legacy.payload.task_id).toBe('legacy-task');
    expect(legacy.retry.createdAt).toBe(1000);

    const failed = markStaleResultFailure(legacy, new Error('network down'), 2000);
    expect(failed.retry.attempts).toBe(1);
    expect(failed.retry.nextAttemptAt).toBe(32_000);
    expect(failed.retry.lastError).toBe('network down');
  });

  it('abandons stale result files after the retry ceiling', () => {
    const stale = parseStaleResultFile(JSON.stringify({
      payload: { task_id: 'old-task' },
      retry: { attempts: 10, createdAt: 1000, nextAttemptAt: 0 },
    }), 1000);

    expect(shouldAbandonStaleResult(stale, 2000)).toBe(true);
  });

  it('fails final status with oauth_rotation_failed when OAuth rotation cannot persist', async () => {
    const oldProvider = process.env.PROVIDER;
    const oldAuthMode = process.env.AUTH_MODE;
    process.env.PROVIDER = 'openai';
    process.env.AUTH_MODE = 'oauth';
    vi.resetModules();
    vi.mocked(fs.existsSync).mockReturnValue(true);
    mockReadFileSync.mockImplementation((path, encoding) => {
      if (String(path).includes('/opt/agent/openai/auth.json')) {
        return encoding ? '{"refresh_token":"rotated"}' : Buffer.from('{"refresh_token":"rotated"}');
      }
      return '';
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes('/api/agent/credentials/rotate')) {
        return Promise.resolve({
          ok: false,
          status: 400,
          text: () => Promise.resolve('revoked token'),
          json: () => Promise.resolve({}),
          headers: { get: () => null },
        } as unknown as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({ status: 'ok' }),
        headers: { get: () => null },
      } as unknown as Response);
    });
    vi.stubGlobal('fetch', fetchMock);

    try {
      const mod = await import('./index.js');
      await mod.persistAndPostStatus('task-oauth', {
        task_id: 'task-oauth',
        status: 'COMPLETED',
        logs: 'original completion',
      });
    } finally {
      process.env.PROVIDER = oldProvider;
      process.env.AUTH_MODE = oldAuthMode;
    }

    const statusCall = fetchMock.mock.calls.find(([url]) => {
      const u = typeof url === 'string' ? url : url instanceof URL ? url.toString() : url.url;
      return u.includes('/api/agent/status');
    });
    expect(statusCall).toBeTruthy();
    const rawBody = statusCall?.[1]?.body;
    const bodyStr = typeof rawBody === 'string' ? rawBody : '';
    const body = JSON.parse(bodyStr) as Record<string, unknown>;
    expect(body.status).toBe('FAILED');
    expect(body.oauth_rotation_failed).toBe(true);
    expect(body.failure_category).toBe('oauth_rotation_failed');
    expect(body.logs).toContain('OAuth credential rotation failed');
  });
});

// ── setupWorkspace: pre-baked git object cache reuse ─────────────────────────
describe('setupWorkspace reference-cache reuse', () => {
  const defaultSpawn = vi.mocked(spawn).getMockImplementation();

  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(fs.existsSync).mockReset();
    vi.mocked(spawn).mockReset();
    if (defaultSpawn) vi.mocked(spawn).mockImplementation(defaultSpawn);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** execFileSync stub: ls-remote throws so the stale-branch path is skipped;
   *  every other git/rm/curl invocation succeeds.
   *  cloneWithRetry now uses gitAsync (spawn-based), so we also stub spawn for
   *  'git clone' to succeed immediately. */
  function stubExecFileSyncNoStaleBranch(): void {
    vi.mocked(execFileSync).mockImplementation((cmd: unknown, args: unknown) => {
      const a = Array.isArray(args) ? (args as string[]) : [];
      if (String(cmd) === 'git' && a[0] === 'ls-remote') {
        throw new Error('fatal: branch not found'); // → staleBranchExists = false
      }
      return '';
    });
    // Stub spawn so gitAsync (used by cloneWithRetry) resolves immediately.
    vi.mocked(spawn).mockImplementation((cmd: unknown) => {
      if (cmd === 'git') {
        const proc = {
          pid: 999,
          stdin: { on: vi.fn(), end: vi.fn() },
          stdout: { on: vi.fn() },
          stderr: { on: vi.fn() },
          on: vi.fn((event: string, cb: (code: number) => void) => {
            if (event === 'close') void Promise.resolve().then(() => { cb(0); });
          }),
          kill: vi.fn(),
        };
        return proc as unknown as ReturnType<typeof spawn>;
      }
      return { on: vi.fn(), kill: vi.fn() } as unknown as ReturnType<typeof spawn>;
    });
  }

  function findCloneArgs(): string[] {
    // cloneWithRetry now uses gitAsync → spawn('git', ...), not execFileSync
    const cloneCall = vi.mocked(spawn).mock.calls.find(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('clone'),
    );
    expect(cloneCall).toBeDefined();
    return cloneCall?.[1] as string[];
  }

  it('clone args include --reference-if-able when the mirror exists', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(true); // mirror present on disk
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const cloneArgs = findCloneArgs();
    expect(cloneArgs).toContain('--reference-if-able');
    // Reference path matches GIT_REFERENCE_CACHE_ROOT/<owner>/<repo>.git.
    const refIdx = cloneArgs.indexOf('--reference-if-able');
    expect(cloneArgs[refIdx + 1]).toBe('/opt/agent-cache/git/owner/repo.git');
  });

  it('clone args exclude --reference-if-able when the mirror is absent', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false); // no mirror seeded for this repo
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const cloneArgs = findCloneArgs();
    expect(cloneArgs).not.toContain('--reference-if-able');
  });

  it('setupWorkspace does not embed token in clone URL', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const cloneArgs = findCloneArgs();
    // Clean URL — no x-access-token embedded.
    const urlArg = cloneArgs[cloneArgs.length - 2]; // second-to-last is the URL
    expect(urlArg).toBe('https://github.com/owner/repo.git');
    expect(urlArg).not.toContain('x-access-token');
    // Credential helper -c flag present.
    expect(cloneArgs.some(a => a.startsWith('credential.https://github.com.helper='))).toBe(true);
  });

  it('registers the sizecap merge driver after configuring user.* git config', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    stubExecFileSyncNoStaleBranch();

    await setupWorkspace('owner', 'repo', 'ghtoken', 'main', 'prefix', 'task-id', null, { value: '' }, 'blueprint');

    const gitConfigCalls = vi.mocked(execFileSync).mock.calls.filter(
      c => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('config'),
    );
    // The driver command is registered…
    const driverCall = gitConfigCalls.find(
      c => (c[1] as string[]).includes('merge.sizecap.driver'),
    );
    expect(driverCall).toBeDefined();
    expect(driverCall?.[1]).toContain('python3 backend/testing/merge_size_limits.py %O %A %B');
    // …after the user.email/user.name config (so warm/cold ordering is stable).
    const idx = (key: string) => gitConfigCalls.findIndex(c => (c[1] as string[]).includes(key));
    expect(idx('merge.sizecap.driver')).toBeGreaterThan(idx('user.email'));
  });
});

// ── registerSizeCapMergeDriver ───────────────────────────────────────────────
describe('registerSizeCapMergeDriver', () => {
  beforeEach(() => {
    vi.mocked(execFileSync).mockReset();
    vi.mocked(execFileSync).mockReturnValue('');
  });

  it('configures both merge.sizecap.name and merge.sizecap.driver in the given workspace', () => {
    registerSizeCapMergeDriver('/custom/ws');

    const calls = vi.mocked(execFileSync).mock.calls.filter(c => c[0] === 'git');
    const argLists = calls.map(c => (c[1] as string[]).join(' '));
    expect(argLists.some(a => a === '-C /custom/ws config merge.sizecap.name max-cap union merge for module_size_limits.json')).toBe(true);
    expect(argLists.some(a => a === '-C /custom/ws config merge.sizecap.driver python3 backend/testing/merge_size_limits.py %O %A %B')).toBe(true);
  });

  it('swallows git config failures (best-effort registration)', () => {
    vi.mocked(execFileSync).mockImplementation(() => { throw new Error('git config boom'); });
    // Must not throw — a failed registration only means the JSON falls back to
    // the normal conflict path.
    expect(() => { registerSizeCapMergeDriver('/ws'); }).not.toThrow();
  });
});

// ── Failure category classification ──────────────────────────────────────────
